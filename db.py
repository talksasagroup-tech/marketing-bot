"""Storage for leads, sent emails, replies and the history of each lead: PostgreSQL when
DATABASE_URL is set, otherwise the SQLite file DB_PATH.

SQL in this project is written once, in SQLite's style, and runs on both. On PostgreSQL the
connection (Postgres below) rewrites ? placeholders, SQLite's time functions and the few
schema words that differ. Dates and times are kept as UTC text, "YYYY-MM-DD HH:MM:SS", on both.
"""
import csv
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import psycopg

import config

# What a database error looks like, whichever database is in use
Error = (sqlite3.Error, psycopg.Error)
IntegrityError = (sqlite3.IntegrityError, psycopg.IntegrityError)

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    company TEXT,
    website TEXT,
    industry TEXT,
    email TEXT UNIQUE,
    status TEXT DEFAULT 'new',
    -- new -> verified / invalid -> sent -> replied / bounced / unsubscribed
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    -- the AI's two slots in the email (see personalise.py): opener is its sentence about
    -- the lead's website, uses is a JSON list picked from uses.yaml. confidence is NULL
    -- until that has been tried, then 1 if the opener is usable and 0 if the fallback is used.
    -- use_case and subject_topic are from the older way of writing emails and no longer used.
    opener TEXT,
    use_case TEXT,
    subject_topic TEXT,
    uses TEXT,
    confidence REAL,
    -- year the company started, estimated for ranking (see ranking.py);
    -- est_year_source is 'site', 'whois' or 'unknown', NULL until it has been tried
    est_year INTEGER,
    est_year_source TEXT,
    -- where the lead came from: source is 'search', 'csv', 'manual', or the name of a
    -- directory imported by targeting.py (e.g. 'CBK DCP directory Sep 2026')
    source TEXT,
    source_query TEXT,
    source_url TEXT,
    discovered_at TEXT,
    -- from an imported directory (targeting.py): as listed there; date_licensed is YYYY-MM-DD.
    -- source_email is the address the directory gave when targeting.py swapped it for a
    -- general inbox found on the lead's website (NULL = email is still the directory's)
    source_email TEXT,
    phone TEXT,
    physical_address TEXT,
    date_licensed TEXT,
    imported_at TEXT,
    -- when the status first became meeting / client
    meeting_at TEXT,
    client_at TEXT,
    -- need score 0-100 (see targeting.py): score_reasons is a JSON list of where the points
    -- came from, scored_at when it was worked out (NULL = not scored yet)
    score INTEGER,
    score_reasons TEXT,
    score_version INTEGER,
    scored_at TEXT,
    -- when someone first emailed this lead from the CRM: the bot sends it nothing more
    -- (no first email, no follow-ups) from then on
    crm_emailed_at TEXT,
    -- a person's check before the first email, for leads found by web search (their industry
    -- is only the search that found them): 'pending' (not emailed until checked), 'approved'
    -- or 'rejected' (not a fit). NULL = no check needed (CSV and directory imports).
    review_status TEXT,
    reviewed_by INTEGER,
    reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS sent (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id),
    message_id TEXT,
    template TEXT,
    -- the Nairobi date it went out (today()), and the UTC time
    sent_on TEXT DEFAULT (date('now')),
    sent_at TEXT,
    -- exactly what went out: the subject and the plain-text body
    sent_subject TEXT,
    sent_body TEXT
);
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id),
    from_email TEXT,
    subject TEXT,
    body TEXT,
    label TEXT,
    summary TEXT,
    imap_uid TEXT UNIQUE,
    -- the email's own Message-ID, so a copy found in another folder isn't stored twice
    message_id TEXT,
    received_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- mail that isn't from a lead, so the inbox check doesn't look at it again
CREATE TABLE IF NOT EXISTS skipped (
    imap_uid TEXT PRIMARY KEY
);
-- everything that happens to a lead, one row each; detail is JSON
CREATE TABLE IF NOT EXISTS lead_events (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id),
    event_type TEXT,
    -- discovered, verified, invalid, email_sent, bounced, replied, unsubscribed, status_changed
    detail TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS lead_events_lead ON lead_events (lead_id);
-- addresses that must never be emailed or added again (unsubscribes and bounces)
CREATE TABLE IF NOT EXISTS suppression (
    email TEXT PRIMARY KEY,
    domain TEXT,
    reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- companies that asked us to stop: no address on their own domain is emailed or added again.
-- Only opt-outs from a company's own domain (never free mail like gmail.com); a bounce only
-- stops its own address.
CREATE TABLE IF NOT EXISTS suppressed_domains (
    domain TEXT PRIMARY KEY,
    reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- Telegram alerts waiting to go out; sent_at stays NULL until Telegram accepts one,
-- so an alert that fails is sent at a later inbox check instead of being lost
CREATE TABLE IF NOT EXISTS alert_outbox (
    id INTEGER PRIMARY KEY,
    reply_id INTEGER UNIQUE REFERENCES replies(id),
    text TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT,
    attempts INTEGER DEFAULT 0,
    last_error TEXT
);
-- an email being sent right now: claimed (in one transaction that rechecks the lead and the
-- daily limit) just before it goes to the mail server, removed once it is recorded in sent
-- or has failed. One that is left over means a run stopped mid-send: that email may have
-- gone out, so the lead is not emailed again until you check (see sender.leftover_claims).
CREATE TABLE IF NOT EXISTS send_claims (
    lead_id INTEGER REFERENCES leads(id),
    step INTEGER,
    claimed_on TEXT,
    claimed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    owner TEXT,
    PRIMARY KEY (lead_id, step)
);
-- settings an admin changed in the CRM (profile_url, profile_label, attach_pdf); each one takes
-- the place of the same setting in .env. A setting with no row here still comes from .env.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_by INTEGER,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- one row per lock held: only one send run at a time, across processes
CREATE TABLE IF NOT EXISTS locks (
    name TEXT PRIMARY KEY,
    owner TEXT,
    acquired_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT
);
"""

def postgres():
    return config.DATABASE_URL.startswith(("postgres://", "postgresql://"))

def conn():
    if postgres():
        return Postgres()
    # waits up to 30 seconds for another process's write instead of failing at once
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c

# Every writer that must see the database unchanged until it commits (immediate) takes this
# PostgreSQL lock first, as BEGIN IMMEDIATE does with SQLite's write lock
WRITE_LOCK = 7262

@contextmanager
def immediate():
    """A transaction that takes the write lock at the start (BEGIN IMMEDIATE), so what it
    reads cannot change before it writes, even with other processes on the same file."""
    if postgres():
        with Postgres() as c:
            c.execute("SELECT pg_advisory_xact_lock(?)", (WRITE_LOCK,))
            yield c
        return
    c = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")
    finally:
        c.close()

def columns(c, table):
    """The column names of a table, on either database."""
    if postgres():
        return {r[0] for r in c.execute("SELECT column_name FROM information_schema.columns"
                                        " WHERE table_schema = current_schema() AND table_name = ?",
                                        (table,))}
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}

# --- PostgreSQL ---

UTC_NOW = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
UTC_TODAY = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
# SQLite's time functions as PostgreSQL text in the same format. The modifier (? or a
# quoted '-14 days', '+600 seconds') is read by PostgreSQL as an interval.
MODIFIER = r"(\?|'[^']*')"
TIME_FUNCTIONS = [
    (re.compile(rf"datetime\('now',\s*{MODIFIER}\)"),
     r"to_char((now() AT TIME ZONE 'UTC') + CAST(\1 AS interval), 'YYYY-MM-DD HH24:MI:SS')"),
    (re.compile(rf"date\('now',\s*{MODIFIER}\)"),
     r"to_char((now() AT TIME ZONE 'UTC') + CAST(\1 AS interval), 'YYYY-MM-DD')"),
    (re.compile(rf"datetime\(([\w.]+),\s*{MODIFIER}\)"),
     r"to_char(CAST(\1 AS timestamp) + CAST(\2 AS interval), 'YYYY-MM-DD HH24:MI:SS')"),
    (re.compile(rf"date\(([\w.]+),\s*{MODIFIER}\)"),
     r"to_char(CAST(\1 AS date) + CAST(\2 AS interval), 'YYYY-MM-DD')"),
    (re.compile(r"datetime\('now'\)"), UTC_NOW),
    (re.compile(r"date\('now'\)"), UTC_TODAY),
    (re.compile(r"\bCURRENT_TIMESTAMP\b"), UTC_NOW),
]
# Schema words: SQLite's auto-numbered id, and its case-insensitive text (emails are
# stored lower case, so plain text matches the same)
SCHEMA_WORDS = [
    (re.compile(r"\bINTEGER PRIMARY KEY\b"), "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"),
    (re.compile(r"\s+COLLATE NOCASE\b"), ""),
    (re.compile(r"\bBLOB\b"), "BYTEA"),
]

def pg_sql(sql, with_params=True):
    """SQLite-style SQL as PostgreSQL SQL."""
    for pattern, replacement in TIME_FUNCTIONS:
        sql = pattern.sub(replacement, sql)
    if not with_params:  # no placeholders: % and ? mean nothing to psycopg
        return sql
    out, quoted = [], False
    for ch in sql:
        if ch == "'":
            quoted = not quoted
        if ch == "%":
            out.append("%%")
        elif ch == "?" and not quoted:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)

class Row(tuple):
    """A row read by position, by column name or as dict(row), like sqlite3.Row."""
    def __new__(cls, values, names, index):
        row = super().__new__(cls, values)
        row._names, row._index = names, index
        return row

    def __getitem__(self, key):
        return tuple.__getitem__(self, self._index[key] if isinstance(key, str) else key)

    def keys(self):
        return list(self._names)

def _rows(cursor):
    names = [d.name for d in cursor.description or ()]
    index = {name: i for i, name in enumerate(names)}
    return lambda values: Row(values, names, index)

class Postgres:
    """A PostgreSQL connection used the way this project uses sqlite3 ones: execute()
    returns the cursor and rows are Row. `with` commits, or rolls back on an error, and
    then closes the connection."""
    def __init__(self):
        self.c = psycopg.connect(config.DATABASE_URL, row_factory=_rows)

    def execute(self, sql, params=()):
        cur = self.c.cursor()
        cur.execute(pg_sql(sql, bool(params)), params or None)
        return cur

    def executemany(self, sql, seq):
        cur = self.c.cursor()
        cur.executemany(pg_sql(sql), seq)
        return cur

    def executescript(self, script):
        for pattern, replacement in SCHEMA_WORDS:
            script = pattern.sub(replacement, script)
        self.c.execute(pg_sql(script, with_params=False))

    def commit(self):
        self.c.commit()

    def close(self):
        self.c.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, error, trace):
        try:
            if kind is None:
                self.c.commit()
            else:
                self.c.rollback()
        finally:
            self.c.close()

def init():
    with conn() as c:
        c.executescript(SCHEMA)
        # Databases made before these columns existed: add them, leave the rows alone
        for table, col, kind in (("sent", "sent_subject", "TEXT"), ("sent", "sent_body", "TEXT"),
                                 ("sent", "sent_at", "TEXT"),
                                 ("leads", "opener", "TEXT"), ("leads", "use_case", "TEXT"), ("leads", "subject_topic", "TEXT"),
                                 ("leads", "uses", "TEXT"),
                                 ("leads", "confidence", "REAL"), ("leads", "est_year", "INTEGER"),
                                 ("leads", "est_year_source", "TEXT"),
                                 ("leads", "source", "TEXT"), ("leads", "source_query", "TEXT"),
                                 ("leads", "source_url", "TEXT"), ("leads", "discovered_at", "TEXT"),
                                 ("leads", "meeting_at", "TEXT"), ("leads", "client_at", "TEXT"),
                                 ("leads", "score", "INTEGER"), ("leads", "score_reasons", "TEXT"),
                                 ("leads", "score_version", "INTEGER"),
                                 ("leads", "phone", "TEXT"), ("leads", "physical_address", "TEXT"),
                                 ("leads", "date_licensed", "TEXT"), ("leads", "imported_at", "TEXT"),
                                 ("leads", "scored_at", "TEXT"), ("leads", "source_email", "TEXT"),
                                 ("leads", "crm_emailed_at", "TEXT"), ("replies", "message_id", "TEXT"),
                                 ("leads", "review_status", "TEXT"), ("leads", "reviewed_by", "INTEGER"),
                                 ("leads", "reviewed_at", "TEXT")):
            if col not in columns(c, table):
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
        # Leads from before these existed
        c.execute("UPDATE leads SET discovered_at=created_at WHERE discovered_at IS NULL")
        for lead in c.execute("SELECT email, status FROM leads WHERE status IN ('bounced','unsubscribed')").fetchall():
            _suppress(c, lead["email"], lead["status"])

def log_event(c, lead_id, event_type, **detail):
    """Add a row to lead_events on connection c. Empty details are left out."""
    detail = {k: v for k, v in detail.items() if v is not None}
    c.execute("INSERT INTO lead_events (lead_id, event_type, detail) VALUES (?,?,?)",
              (lead_id, event_type, json.dumps(detail, ensure_ascii=False) if detail else None))

def today():
    """Today's date in Nairobi, "YYYY-MM-DD": the day the daily limit, follow-up timing and
    the charts all count in, whatever time zone the computer is set to."""
    return datetime.now(config.NAIROBI).date().isoformat()

# Free-mail providers: an address on one of these is a person's, not a company's
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "hotmail.com",
             "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com",
             "protonmail.com", "proton.me", "zoho.com", "mail.com", "gmx.com"}

def domain_of(email):
    return (email or "").lower().strip().rsplit("@", 1)[-1]

def _suppress(c, email, reason):
    email = email.lower().strip()
    c.execute("INSERT INTO suppression (email, domain, reason) VALUES (?,?,?) ON CONFLICT DO NOTHING",
              (email, domain_of(email), reason))
    # an organisation that asked to stop is asked nothing more, at any of its addresses
    if reason == "unsubscribed" and domain_of(email) not in FREE_MAIL:
        c.execute("INSERT INTO suppressed_domains (domain, reason) VALUES (?,?) ON CONFLICT DO NOTHING",
                  (domain_of(email), f"{email} unsubscribed"))

def is_suppressed(email):
    """Whether this exact address is on the suppression list."""
    with conn() as c:
        return c.execute("SELECT 1 FROM suppression WHERE email=?",
                         (email.lower().strip(),)).fetchone() is not None

BLOCKED_SQL = ("SELECT 1 FROM suppression WHERE email=?"
               " UNION ALL SELECT 1 FROM suppressed_domains WHERE domain=?")

def _blocked(c, email):
    email = (email or "").lower().strip()
    return c.execute(BLOCKED_SQL, (email, domain_of(email))).fetchone() is not None

def blocked(email):
    """Whether this address must never be emailed or added: it is suppressed itself, or its
    organisation's domain is."""
    with conn() as c:
        return _blocked(c, email)

def suppressed_emails():
    with conn() as c:
        return {r[0] for r in c.execute("SELECT email FROM suppression")}

def suppressed_domains():
    with conn() as c:
        return {r[0] for r in c.execute("SELECT domain FROM suppressed_domains")}

def blocker():
    """A check like blocked(), for many addresses at once: reads the lists once."""
    emails, domains = suppressed_emails(), suppressed_domains()
    return lambda email: (email or "").lower().strip() in emails or domain_of(email) in domains

def unsubscribe(email):
    """Opt an address out for good, whether or not it is a lead."""
    lead = find_lead_by_email(email.strip())
    if lead:
        set_status(lead["id"], "unsubscribed", "manual")
    with conn() as c:
        _suppress(c, email, "unsubscribed")

# A lead in one of these review states is never emailed by the bot
NOT_REVIEWED = ("pending", "rejected")

def add_lead(company, website, industry, email, source=None, source_query=None, source_url=None,
             review_status=None):
    """Returns True if the lead is new. An address on the suppression list is never added.
    review_status 'pending' holds the lead until a person checks it in the CRM."""
    email = email.lower().strip()
    with conn() as c:
        if _blocked(c, email):
            return False
        added = c.execute(
            "INSERT INTO leads (company, website, industry, email, source, source_query,"
            " source_url, review_status, discovered_at) VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)"
            " ON CONFLICT DO NOTHING RETURNING id",
            (company, website, industry, email, source, source_query, source_url, review_status),
        ).fetchone()
        if not added:
            return False
        log_event(c, added[0], "discovered", source=source, query=source_query, url=source_url)
        return True

def leads_by_status(status, limit=None):
    sql = "SELECT * FROM leads WHERE status=? ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn() as c:
        return c.execute(sql, (status,)).fetchall()

def leads_to_personalise(limit=None):
    sql = "SELECT * FROM leads WHERE status='verified' AND confidence IS NULL ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn() as c:
        return c.execute(sql).fetchall()

def save_personalisation(lead_id, opener, uses, confidence):
    with conn() as c:
        c.execute("UPDATE leads SET opener=?, uses=?, confidence=? WHERE id=?",
                  (opener, uses, confidence, lead_id))

def save_est_year(lead_id, year, source):
    with conn() as c:
        c.execute("UPDATE leads SET est_year=?, est_year_source=? WHERE id=?", (year, source, lead_id))

def set_status(lead_id, status, by=None):
    """Change a lead's status and log it. `by` says what did it (verify, send, inbox,
    dashboard, manual). Returns False if there is no such lead."""
    with conn() as c:
        lead = c.execute("SELECT email, status FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead:
            return False
        old = lead["status"]
        if old == status:
            return True
        c.execute("UPDATE leads SET status=? WHERE id=?", (status, lead_id))
        if status in ("meeting", "client"):
            c.execute(f"UPDATE leads SET {status}_at=COALESCE({status}_at, CURRENT_TIMESTAMP) WHERE id=?",
                      (lead_id,))
        if status in ("verified", "invalid", "bounced", "unsubscribed"):
            log_event(c, lead_id, status, by=by)
        log_event(c, lead_id, "status_changed", old=old, new=status, by=by)
        if status in ("bounced", "unsubscribed"):
            _suppress(c, lead["email"], status)
    return True

def find_lead_by_email(email):
    with conn() as c:
        return c.execute("SELECT * FROM leads WHERE email=?", (email.lower(),)).fetchone()

def record_sent(lead_id, message_id, template, subject=None, body=None, subject_variant=None):
    """Record an email that went out. Its send claim, if any, is removed in the same
    transaction, so the email counts once towards the daily limit."""
    with conn() as c:
        c.execute("DELETE FROM send_claims WHERE lead_id=?", (lead_id,))
        sent_id = c.execute("INSERT INTO sent (lead_id, message_id, template, sent_subject, sent_body,"
                            " sent_on, sent_at) VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP) RETURNING id",
                            (lead_id, message_id, template, subject, body, today())).fetchone()[0]
        step = c.execute("SELECT COUNT(*) FROM sent WHERE lead_id=?", (lead_id,)).fetchone()[0]
        # the campaign is the niche template the email was built from
        log_event(c, lead_id, "email_sent", sent_id=sent_id, step=step, subject=subject,
                  subject_variant=subject_variant, campaign=template)

# step = 1 for the first email to a lead, 2 for the first follow-up, and so on
SENT_LOG = """
SELECT s.id, s.sent_on, s.message_id, s.template, s.sent_subject, s.sent_body,
       l.company, l.email,
       ROW_NUMBER() OVER (PARTITION BY s.lead_id ORDER BY s.id) AS step
FROM sent s LEFT JOIN leads l ON l.id = s.lead_id
"""

def sent_log(days=None):
    sql, args = f"SELECT * FROM ({SENT_LOG})", ()
    if days:
        sql += " WHERE sent_on >= date('now', ?)"
        args = (f"-{int(days) - 1} days",)
    with conn() as c:
        return c.execute(sql + " ORDER BY id", args).fetchall()

def sent_to(email):
    with conn() as c:
        return c.execute(f"SELECT * FROM ({SENT_LOG}) WHERE email=? ORDER BY id",
                         (email.lower().strip(),)).fetchall()

def lead_by_message_ids(ids):
    """The lead one of our emails went to, by that email's Message-ID, or None."""
    if not ids:
        return None
    with conn() as c:
        return c.execute("SELECT l.* FROM sent s JOIN leads l ON l.id = s.lead_id"
                         f" WHERE s.message_id IN ({','.join('?' * len(ids))}) ORDER BY s.id DESC LIMIT 1",
                         ids).fetchone()

def leads_on_domain(domain):
    with conn() as c:
        return c.execute("SELECT * FROM leads WHERE lower(email) LIKE ? ORDER BY id",
                         ("%@" + domain.lower(),)).fetchall()

def bounce_stats(days):
    """(companies emailed, how many of them bounced), for the companies first emailed in the
    last `days` days. A lead counts once, whether its email went out or its address was
    refused at sending; a bounce counts whenever it arrived. Leads added by hand (test
    leads) are left out."""
    with conn() as c:
        rows = c.execute(f"""
            SELECT l.id,
                   EXISTS (SELECT 1 FROM lead_events b WHERE b.lead_id = l.id AND b.event_type = 'bounced')
                       OR l.status = 'bounced' AS bounced
            FROM lead_events e JOIN leads l ON l.id = e.lead_id
            WHERE (e.event_type = 'email_sent'
                   OR (e.event_type = 'bounced' AND e.detail LIKE '%"by": "send"%'))
              AND {real('l')}
            GROUP BY l.id HAVING MIN(e.created_at) >= datetime('now', ?)""",
                         (f"-{int(days)} days",)).fetchall()
    return len(rows), sum(r["bounced"] for r in rows)

def sent_subjects():
    with conn() as c:
        return {r[0] for r in c.execute("SELECT DISTINCT sent_subject FROM sent WHERE sent_subject IS NOT NULL")}

def first_sent_date():
    with conn() as c:
        return c.execute("SELECT MIN(sent_on) FROM sent").fetchone()[0]

def sent_count(lead_id):
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM sent WHERE lead_id=?", (lead_id,)).fetchone()[0]

def awaiting_followup(n=1):
    """Leads still at 'sent' that have had exactly n emails (so are waiting for follow-up n),
    oldest first email first. Each row is the lead plus the first email's sent_id,
    message_id and sent_subject (follow-ups thread under it), and sent_on, the date of the
    last email (follow-ups are timed from it)."""
    with conn() as c:
        return c.execute("""
            SELECT l.*, s.id AS sent_id, s.message_id, s.sent_subject, last.sent_on
            FROM leads l
            JOIN sent s ON s.id = (SELECT MIN(id) FROM sent WHERE lead_id = l.id)
            JOIN sent last ON last.id = (SELECT MAX(id) FROM sent WHERE lead_id = l.id)
            WHERE l.status = 'sent' AND l.crm_emailed_at IS NULL
              AND (SELECT COUNT(*) FROM sent s2 WHERE s2.lead_id = l.id) = ?
            ORDER BY s.id""", (n,)).fetchall()

def sent_thread(lead_id):
    """Every email sent to this lead, oldest first: each row's message_id and sent_subject."""
    with conn() as c:
        return c.execute("SELECT message_id, sent_subject FROM sent WHERE lead_id=? ORDER BY id",
                         (lead_id,)).fetchall()

def recent_out_of_office(lead_id, days):
    """Whether an out-of-office reply from this lead arrived in the last `days` days."""
    with conn() as c:
        return c.execute("SELECT 1 FROM replies WHERE lead_id=? AND label='out_of_office'"
                         " AND received_at >= datetime('now', ?)", (lead_id, f"-{int(days)} days")).fetchone() is not None

def emailed_lead_ids():
    with conn() as c:
        return {r[0] for r in c.execute("SELECT DISTINCT lead_id FROM sent")}

def sent_today():
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM sent WHERE sent_on=?", (today(),)).fetchone()[0]

# --- Sending safely with more than one run (G-03, G-04) ---

def acquire_lock(name, owner, seconds):
    """Take the named lock for `seconds`, unless another owner holds it and it hasn't
    expired. Returns True if `owner` now holds it."""
    with immediate() as c:
        c.execute("DELETE FROM locks WHERE name=? AND (expires_at IS NULL OR expires_at < datetime('now'))",
                  (name,))
        c.execute("INSERT INTO locks (name, owner, expires_at) VALUES (?,?,datetime('now', ?))"
                  " ON CONFLICT DO NOTHING", (name, owner, f"{int(seconds):+d} seconds"))
        return c.execute("SELECT owner FROM locks WHERE name=?", (name,)).fetchone()["owner"] == owner

def refresh_lock(name, owner, seconds):
    with immediate() as c:
        c.execute("UPDATE locks SET expires_at=datetime('now', ?) WHERE name=? AND owner=?",
                  (f"{int(seconds):+d} seconds", name, owner))

def release_lock(name, owner):
    with immediate() as c:
        c.execute("DELETE FROM locks WHERE name=? AND owner=?", (name, owner))

def lock_holder(name):
    with conn() as c:
        return c.execute("SELECT * FROM locks WHERE name=?", (name,)).fetchone()

def claim_send(lead_id, followup, daily_limit, owner=None):
    """Claim one email just before it goes to the mail server. Returns None if it may go,
    otherwise why not. Rechecked here, in one write transaction, whatever the batch was
    built from: the lead still at the right status, not suppressed, not already emailed
    (or being emailed), and today's sends plus emails in flight under the daily limit.
    followup is 0 for a first email, else the follow-up's number."""
    step = int(followup) + 1
    day = today()  # the same day sent_today counts
    with immediate() as c:
        lead = c.execute("SELECT email, status, crm_emailed_at, review_status FROM leads WHERE id=?",
                         (lead_id,)).fetchone()
        if not lead:
            return "no such lead"
        if lead["review_status"] in NOT_REVIEWED:
            return f"review is {lead['review_status']}: a person hasn't approved this lead in the CRM"
        if lead["crm_emailed_at"]:
            return "someone at TalkSasa has emailed this lead from the CRM"
        wanted = "sent" if followup else "verified"
        if lead["status"] != wanted:
            return f"status is now {lead['status']}, not {wanted}"
        if _blocked(c, lead["email"]):
            return "on the suppression list"
        emailed = c.execute("SELECT COUNT(*) FROM sent WHERE lead_id=?", (lead_id,)).fetchone()[0]
        if emailed != step - 1:
            return f"{emailed} emails already sent"
        if c.execute("SELECT 1 FROM send_claims WHERE lead_id=?", (lead_id,)).fetchone():
            return "already being sent by another run, or left over from a run that stopped mid-send"
        used = (c.execute("SELECT COUNT(*) FROM sent WHERE sent_on=?", (day,)).fetchone()[0]
                + c.execute("SELECT COUNT(*) FROM send_claims WHERE claimed_on=?", (day,)).fetchone()[0])
        if used >= daily_limit:
            return f"daily limit of {daily_limit} reached"
        c.execute("INSERT INTO send_claims (lead_id, step, claimed_on, owner) VALUES (?,?,?,?)",
                  (lead_id, step, day, owner))
    return None

# A lead whose email failed this many times is not tried again by the bot (a mail server that
# keeps refusing it would otherwise take a slot every day). Its failures stay in lead_events.
MAX_SEND_FAILURES = 3

def failure_counts():
    """{lead id: how many times an email to it failed to go out}."""
    with conn() as c:
        return {r[0]: r[1] for r in c.execute(
            "SELECT lead_id, COUNT(*) FROM lead_events WHERE event_type='send_failed' GROUP BY lead_id")}

def release_claim(lead_id):
    """The email did not go out: free the claim so the lead can be tried again."""
    with conn() as c:
        c.execute("DELETE FROM send_claims WHERE lead_id=?", (lead_id,))

def send_claims():
    with conn() as c:
        return c.execute("SELECT sc.*, l.email FROM send_claims sc LEFT JOIN leads l ON l.id = sc.lead_id"
                         " ORDER BY claimed_at").fetchall()

def get_setting(key):
    """A setting an admin saved in the CRM, or None if it was never set there (.env decides)."""
    try:
        with conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    except Error as e:  # SQLite says "no such table", PostgreSQL "does not exist"
        if "no such table" in str(e) or "does not exist" in str(e):  # from before settings existed: nothing was set
            return None
        raise
    return row["value"] if row else None

def set_setting(key, value, by=None):
    with conn() as c:
        c.execute("INSERT INTO settings (key, value, updated_by, updated_at) VALUES (?,?,?,CURRENT_TIMESTAMP)"
                  " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by,"
                  " updated_at=excluded.updated_at", (key, value, by))

def reply_seen(uid):
    with conn() as c:
        return c.execute("SELECT 1 FROM replies WHERE imap_uid=?", (uid,)).fetchone() is not None

def queue_alert(reply_id, text):
    with conn() as c:
        c.execute("INSERT INTO alert_outbox (reply_id, text) VALUES (?,?) ON CONFLICT DO NOTHING",
                  (reply_id, text))

def pending_alerts():
    with conn() as c:
        return c.execute("SELECT * FROM alert_outbox WHERE sent_at IS NULL ORDER BY id").fetchall()

def alert_sent(alert_id):
    with conn() as c:
        c.execute("UPDATE alert_outbox SET sent_at=CURRENT_TIMESTAMP, attempts=attempts+1 WHERE id=?",
                  (alert_id,))

def alert_failed(alert_id, error):
    with conn() as c:
        c.execute("UPDATE alert_outbox SET attempts=attempts+1, last_error=? WHERE id=?", (error, alert_id))

def reply_with_message_id(message_id):
    with conn() as c:
        return c.execute("SELECT 1 FROM replies WHERE message_id=?", (message_id,)).fetchone() is not None

def replies_without_message_id():
    """(reply id, INBOX UID) for replies stored before Message-IDs were kept."""
    with conn() as c:
        return [(r[0], r[1]) for r in c.execute(
            "SELECT id, imap_uid FROM replies WHERE message_id IS NULL AND imap_uid NOT LIKE '%:%'"
            " AND imap_uid NOT LIKE '%/%'")]

def set_reply_message_id(reply_id, message_id):
    with conn() as c:
        c.execute("UPDATE replies SET message_id=? WHERE id=?", (message_id, reply_id))

def add_reply(lead_id, from_email, subject, body, label, summary, uid, message_id=None):
    """Returns the new reply's id, or None if it is already stored (same imap_uid): that
    one is left alone and logs nothing, so checking the inbox again never repeats it."""
    with conn() as c:
        added = c.execute(
            "INSERT INTO replies (lead_id, from_email, subject, body, label, summary, imap_uid,"
            " message_id) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING id",
            (lead_id, from_email, subject, body, label, summary, uid, message_id),
        ).fetchone()
        if not added:
            return None
        if lead_id is not None:  # a reply linked to no lead has no lead history to add to
            log_event(c, lead_id, "replied", reply_id=added[0], label=label)
        return added[0]

def skipped_uids():
    with conn() as c:
        return {r[0] for r in c.execute("SELECT imap_uid FROM skipped")}

def unskip(uids):
    with conn() as c:
        c.executemany("DELETE FROM skipped WHERE imap_uid=?", [(u,) for u in uids])

def add_skipped(uids):
    with conn() as c:
        c.executemany("INSERT INTO skipped (imap_uid) VALUES (?) ON CONFLICT DO NOTHING", [(u,) for u in uids])

def real(alias=""):
    """SQL condition that leaves out leads added by hand (source 'manual': test leads).
    Used by stats and the dashboards only; sending and the daily limit still count them."""
    return f"COALESCE({alias + '.' if alias else ''}source, '') != 'manual'"

def stats():
    with conn() as c:
        rows = c.execute(f"SELECT status, COUNT(*) n FROM leads WHERE {real()} GROUP BY status").fetchall()
        labels = c.execute("SELECT r.label, COUNT(*) n FROM replies r LEFT JOIN leads l ON l.id = r.lead_id"
                           f" WHERE {real('l')} GROUP BY r.label").fetchall()
    return {r["status"]: r["n"] for r in rows}, {r["label"]: r["n"] for r in labels}

# file -> (table, what its id column is called in the file). Every file has lead_id to join on.
EXPORTS = {"leads.csv": ("leads", "lead_id"), "events.csv": ("lead_events", "event_id"),
           "replies.csv": ("replies", "reply_id")}

def export(folder="."):
    """Write leads.csv, events.csv and replies.csv. Returns {file: number of rows}."""
    counts = {}
    with conn() as c:
        for name, (table, id_name) in EXPORTS.items():
            cur = c.execute(f"SELECT * FROM {table} ORDER BY id")
            header = [id_name if d[0] == "id" else d[0] for d in cur.description]
            rows = cur.fetchall()
            with open(Path(folder) / name, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(tuple(r) for r in rows)
            counts[name] = len(rows)
    return counts
