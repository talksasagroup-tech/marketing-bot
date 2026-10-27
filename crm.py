"""LeadBot CRM: sign-in with two roles (admin, sales), open leads, claim, reply, notes, team.

Install (web.py, right after `app = FastAPI(...)`):
    from crm import install as install_crm
    install_crm(app)

First admin (you type the password yourself; it is never shown or stored in plain text):
    python crm.py create-admin

Other commands:
    python crm.py reset-password someone@talksasa.co.ke
    python crm.py list-users

Settings (.env, all optional):
    CRM_FROM_EMAIL   address replies go out from (default FROM_EMAIL, e.g. florence@...; later sales@...)
    SENDER_NAME      the name every reply is signed with ("Mary from TalkSasa"), whoever sends it
    SENDER_PHONE     the phone in every reply's signature, whoever sends it
    CRM_SMTP_USER    / CRM_SMTP_PASS   login for that mailbox (default SMTP_USER / SMTP_PASS)
    CRM_SENT_FOLDER  IMAP folder to file a copy of each reply in (default "Sent"; blank = don't)
    CRM_DRY_RUN      true = record replies but don't actually send them (for testing)
    SESSION_HOURS    how long a sign-in lasts (default 12)
    COOKIE_SECURE    true once the dashboard is served over HTTPS
"""
import base64
import binascii
import getpass
import hashlib
import hmac
import html
import imaplib
import io
import json
import os
import re
import secrets
import smtplib
import ssl
import string
import sys
import time
import unicodedata
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

import config
import db

HERE = Path(__file__).parent
COOKIE = "lb_session"


def _flag(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


SESSION_HOURS = int(os.getenv("SESSION_HOURS", "12"))
COOKIE_SECURE = _flag("COOKIE_SECURE")
CRM_DRY_RUN = _flag("CRM_DRY_RUN")
CRM_FROM_EMAIL = os.getenv("CRM_FROM_EMAIL") or config.FROM_EMAIL or config.SMTP_USER
CRM_SMTP_USER = os.getenv("CRM_SMTP_USER") or config.SMTP_USER
CRM_SMTP_PASS = os.getenv("CRM_SMTP_PASS") or config.SMTP_PASS
CRM_SENT_FOLDER = os.getenv("CRM_SENT_FOLDER", "Sent")

ROLES = ("admin", "sales")
ACTIONABLE = ("interested", "question", "needs_review")
NO_EMAIL = ("unsubscribed", "bounced", "invalid")   # never emailed again
CLOSED = ("client", "lost", "unsubscribed", "bounced", "invalid")
CRM_STATUSES = ("replied", "meeting", "client", "lost")
WON_OR_LOST = ("client", "lost")
MINE = "l.status NOT IN ('client','lost')"   # My leads: claimed by you and not won or lost yet
EMAIL_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[A-Za-z]{2,}$")
MIN_PASSWORD = 10


# ---------------------------------------------------------------- database

def conn():
    c = db.conn()
    if not db.postgres():  # PostgreSQL always checks them
        c.execute("PRAGMA foreign_keys = ON")
    return c


def _cols(c, table):
    return db.columns(c, table)


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def iso(ts):
    """SQLite timestamps are UTC without a zone; mark them so browsers convert them."""
    if ts and "T" not in ts and len(ts) > 10:
        return ts.replace(" ", "T") + "Z"
    return ts


def ensure_schema():
    db.init()  # the CRM's tables refer to leads, which PostgreSQL needs to exist first
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            phone TEXT DEFAULT '',
            role TEXT NOT NULL CHECK (role IN ('admin','sales')),
            password_hash TEXT NOT NULL,
            must_change INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_login TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS crm_messages (
            id INTEGER PRIMARY KEY,
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            user_id INTEGER REFERENCES users(id),
            to_email TEXT,
            subject TEXT,
            body TEXT,
            message_id TEXT,
            in_reply_to TEXT,
            status TEXT,            -- sent / dry_run / failed
            error TEXT,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS crm_notes (
            id INTEGER PRIMARY KEY,
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            user_id INTEGER REFERENCES users(id),
            body TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS crm_events (
            id INTEGER PRIMARY KEY,
            lead_id INTEGER,
            user_id INTEGER,
            action TEXT NOT NULL,
            detail TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        -- an email template as it was before an admin saved over it, so it can be restored
        CREATE TABLE IF NOT EXISTS template_history (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,     -- first / followup / followup2
            name TEXT NOT NULL,     -- the industry, e.g. sacco
            subject TEXT,
            body TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id),   -- who replaced it
            saved_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_crm_messages_lead ON crm_messages(lead_id);
        CREATE INDEX IF NOT EXISTS idx_crm_notes_lead ON crm_notes(lead_id);
        CREATE INDEX IF NOT EXISTS idx_crm_events_lead ON crm_events(lead_id);
        -- files sent with a CRM email (only ones that went out, or were saved in test mode)
        CREATE TABLE IF NOT EXISTS crm_attachments (
            id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL REFERENCES crm_messages(id),
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            filename TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size INTEGER NOT NULL,
            data BLOB NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_crm_attachments_lead ON crm_attachments(lead_id);
        -- every change a person made to a reply's label, so the AI's first answer is never lost
        CREATE TABLE IF NOT EXISTS reply_labels (
            id INTEGER PRIMARY KEY,
            reply_id INTEGER NOT NULL REFERENCES replies(id),
            old_label TEXT,
            new_label TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        -- one-time data changes, so each runs once and never undoes what people did since
        CREATE TABLE IF NOT EXISTS crm_migrations (
            name TEXT PRIMARY KEY,
            done_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        lc = _cols(c, "leads")
        if "claimed_by" not in lc:
            c.execute("ALTER TABLE leads ADD COLUMN claimed_by INTEGER")
        if "claimed_at" not in lc:
            c.execute("ALTER TABLE leads ADD COLUMN claimed_at TEXT")
        # the next thing to do for a lead and the day it's due; why a lost lead was lost
        for col in ("next_action", "next_action_due", "lost_reason"):
            if col not in lc:
                c.execute(f"ALTER TABLE leads ADD COLUMN {col} TEXT")
        if "handled" not in _cols(c, "replies"):
            c.execute("ALTER TABLE replies ADD COLUMN handled INTEGER DEFAULT 0")
        # Every reply but a bounce now needs someone to read it. Replies an older version marked
        # handled by itself (not interested, auto-replies, unsubscribes) go back to waiting,
        # unless someone has answered the lead since. Once only: Mark done after this sticks.
        if not c.execute("SELECT 1 FROM crm_migrations WHERE name = 'review_all_replies'").fetchone():
            c.execute("""UPDATE replies SET handled = 0
                         WHERE COALESCE(handled, 0) = 1 AND COALESCE(label, '') != 'bounce'
                           AND NOT EXISTS (SELECT 1 FROM crm_messages m WHERE m.lead_id = replies.lead_id
                                           AND m.status != 'failed' AND m.sent_at >= replies.received_at)""")
            c.execute("INSERT INTO crm_migrations (name) VALUES ('review_all_replies')")
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now_utc(),))


# Summaries are stored as the inbox check wrote them (they also go to Telegram). On the
# dashboard and CRM screens they are shown without the words about the AI.
SCREEN_WORDING = [
    (re.compile(r"\bnot read by the AI\b"), "not labelled automatically"),
    (re.compile(r"\bThe AI could not read this reply\b"), "This reply couldn't be labelled automatically"),
    (re.compile(r"\bThe AI thinks this may be\b"), "This may be"),
    (re.compile(r"\bThe AI was unsure \(label "), "Unsure of the label ("),
    (re.compile(r"\bAI summary:"), "Summary:"),
    (re.compile(r"\b[Tt]he AI\b"), "LeadBot"),
    (re.compile(r"\bAI\b"), "automatic"),
]


def screen_text(text):
    for pattern, words in SCREEN_WORDING:
        text = pattern.sub(words, text) if text else text
    return text


def log_event(c, lead_id, user_id, action, detail=""):
    c.execute("INSERT INTO crm_events (lead_id, user_id, action, detail) VALUES (?,?,?,?)",
              (lead_id, user_id, action, detail))


# ---------------------------------------------------------------- passwords & sessions

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        test = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(dk) // 2)
        return hmac.compare_digest(test.hex(), dk)
    except (ValueError, TypeError):
        return False


def password_problem(pw: str):
    if len(pw or "") < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if pw.strip() != pw:
        return "The password can't start or end with a space."
    return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(c, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?,?,?)",
              (_token_hash(token), user_id, expires))
    return token


def user_from_token(token):
    if not token:
        return None
    with conn() as c:
        row = c.execute("""SELECT u.id, u.name, u.email, u.phone, u.role, u.must_change, u.active
                           FROM sessions s JOIN users u ON u.id = s.user_id
                           WHERE s.token_hash = ? AND s.expires_at > ?""",
                        (_token_hash(token), now_utc())).fetchone()
    if not row or not row["active"]:
        return None
    u = dict(row)
    u["must_change"] = bool(u["must_change"])
    u.pop("active", None)
    return u


def set_cookie(resp, token):
    resp.set_cookie(COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True,
                    samesite="strict", secure=COOKIE_SECURE, path="/")


# Simple brute-force brake: 5 failed tries per email (20 per network address) in 15 minutes.
_fails = defaultdict(list)


def _too_many(key):
    cutoff = time.time() - 900
    _fails[key] = [t for t in _fails[key] if t > cutoff]
    limit = 20 if key.startswith("ip:") else 5   # an office shares one address
    return len(_fails[key]) >= limit


# ---------------------------------------------------------------- guard

PUBLIC_PATHS = {"/login", "/api/auth/login", "/favicon.ico"}
WHILE_MUST_CHANGE = {"/login", "/api/auth/me", "/api/auth/password", "/api/auth/logout"}
# The dashboard (web.py, web_extra.py) and the What's working report: admins only
ADMIN_PATHS = {"/", "/api/all", "/api/sent-today", "/api/crm/report"}
ADMIN_PREFIXES = ("/api/leads/", "/api/replies/")


def admin_path(path):
    path = path.rstrip("/") or "/"
    return path in ADMIN_PATHS or (path + "/").startswith(ADMIN_PREFIXES)


def _safe_next(path):
    return path if path.startswith("/") and not path.startswith("//") else "/"


def install(app):
    """Protect every page and API route of the dashboard and add the CRM routes."""
    ensure_schema()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        # Cross-site request check for anything that changes data (cookie is SameSite=Strict as well)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Request blocked: it came from another site."}, 403)
        if path in PUBLIC_PATHS:
            return await call_next(request)
        user = user_from_token(request.cookies.get(COOKIE))
        if not user:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Please sign in."}, 401)
            return RedirectResponse("/login?next=" + quote(_safe_next(path)), 303)
        if user["must_change"] and path not in WHILE_MUST_CHANGE:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Set a new password first."}, 403)
            return RedirectResponse("/login", 303)
        if user["role"] != "admin" and admin_path(path):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Only an admin can see this."}, 403)
            return HTMLResponse("<!doctype html><meta charset='utf-8'><title>LeadBot</title>"
                                "<p style='font-family:system-ui;margin:40px'>Only an admin can see the dashboard. "
                                "<a href='/crm'>Go to the CRM</a></p>", 403, headers={"Cache-Control": "no-store"})
        request.state.user = user
        response = await call_next(request)
        if not path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(router)


def current_user(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def admin_only(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Only an admin can do that.")
    return user


router = APIRouter()


def _page(name):
    html = (HERE / "static" / name).read_text(encoding="utf-8")
    return HTMLResponse("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                        "<meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>"
                        "</head><body>" + html + "</body></html>",
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


@router.get("/login", response_class=HTMLResponse)
def login_page():
    return _page("login.html")


@router.get("/crm", response_class=HTMLResponse)
def crm_page():
    return _page("crm.html")


# ---------------------------------------------------------------- auth API

class LoginIn(BaseModel):
    email: str
    password: str


class PasswordIn(BaseModel):
    current: str
    new: str


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request):
    email = body.email.strip().lower()
    ip = request.client.host if request.client else "?"
    if _too_many("e:" + email) or _too_many("ip:" + ip):
        raise HTTPException(429, "Too many tries. Wait 15 minutes and try again.")
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        ok = bool(row) and row["active"] and check_password(body.password, row["password_hash"])
        if not row:
            check_password(body.password, hash_password("timing-equaliser"))  # same delay either way
        if not ok:
            _fails["e:" + email].append(time.time())
            _fails["ip:" + ip].append(time.time())
            raise HTTPException(401, "That email and password don't match.")
        _fails.pop("e:" + email, None)
        token = new_session(c, row["id"])
        c.execute("UPDATE users SET last_login = ? WHERE id = ?", (now_utc(), row["id"]))
        log_event(c, None, row["id"], "login")
    resp = JSONResponse({"ok": True, "must_change": bool(row["must_change"]),
                         "user": {"name": row["name"], "role": row["role"]}})
    set_cookie(resp, token)
    return resp


@router.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        with conn() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/api/auth/me")
def me(user=Depends(current_user)):
    return {"user": user, "dry_run": CRM_DRY_RUN, "from_email": CRM_FROM_EMAIL, "sender_name": sender_name(user)}


@router.post("/api/auth/password")
def change_password(body: PasswordIn, request: Request, user=Depends(current_user)):
    problem = password_problem(body.new)
    if problem:
        raise HTTPException(400, problem)
    if body.new == body.current:
        raise HTTPException(400, "Choose a password different from the current one.")
    with conn() as c:
        row = c.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
        if not check_password(body.current, row["password_hash"]):
            raise HTTPException(400, "Your current password isn't right.")
        c.execute("UPDATE users SET password_hash = ?, must_change = 0 WHERE id = ?",
                  (hash_password(body.new), user["id"]))
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))  # sign out other devices
        token = new_session(c, user["id"])
        log_event(c, None, user["id"], "password_changed")
    resp = JSONResponse({"ok": True})
    set_cookie(resp, token)
    return resp


# ---------------------------------------------------------------- leads

def _sees(user, lead):
    """Sales see leads nobody has claimed and their own; admins see every lead."""
    return user["role"] == "admin" or lead["claimed_by"] in (None, user["id"])


def _lead_or_404(c, lead_id, user):
    """A colleague's lead is "not found" for sales, so they can't tell it exists."""
    lead = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not lead or not _sees(user, lead):
        raise HTTPException(404, "Lead not found.")
    return lead


# What sales see of a lead; admins see every column
SALES_FIELDS = ("id", "company", "email", "phone", "industry", "website", "score", "status",
                "claimed_by", "claimed_at", "created_at", "next_action", "next_action_due", "lost_reason")


def _has_response(c, lead_id):
    """Whether the organisation has written back: any reply but a bounce or an auto-reply."""
    return c.execute("SELECT 1 FROM replies WHERE lead_id = ? AND COALESCE(label, '') NOT IN ('bounce', 'out_of_office')"
                     " LIMIT 1", (lead_id,)).fetchone() is not None


NOT_YET = "This organisation hasn't replied yet, so nobody can claim it. You can claim it once it writes back."


def _can_email(c, lead):
    return lead["status"] not in NO_EMAIL and not db.blocked(lead["email"] or "")


def _score_cols(c):
    """The targeting step adds score, score_reasons and source to leads; older databases don't have them."""
    lc = _cols(c, "leads")
    return {k: (k if k in lc else None) for k in ("score", "score_reasons", "source")}


def _reasons(raw):
    """score_reasons is saved as a JSON list; show it as short readable strings."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return [str(raw)]
    if isinstance(val, dict):
        val = [f"{k} {v}" for k, v in val.items()]
    if not isinstance(val, list):
        return [str(val)]
    out = []
    for x in val:
        if isinstance(x, dict):
            pts = x.get("points", x.get("score", x.get("delta")))
            why = x.get("reason") or x.get("rule") or x.get("why") or ""
            out.append((f"{'+' if isinstance(pts, (int, float)) and pts > 0 else ''}{pts} {why}").strip() if pts is not None else str(why))
        else:
            out.append(str(x))
    return [o for o in out if o][:12]


def _user_names(c):
    return {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM users")}


@router.get("/api/crm/leads")
def open_leads(scope: str = "open", user=Depends(current_user)):
    """Open = the lead has written back and the deal isn't closed yet."""
    if scope not in ("open", "mine", "closed"):
        raise HTTPException(400, "Unknown view.")
    closed = ",".join("'%s'" % s for s in CLOSED)
    actionable = ",".join("'%s'" % s for s in ACTIONABLE)
    no_email = ",".join("'%s'" % s for s in NO_EMAIL)
    waiting = "COALESCE(r.handled,0) = 0 AND COALESCE(r.label,'') != 'bounce'"   # every reply but a bounce
    me = int(user["id"])
    if scope == "closed":  # a lead rejected in review was never a deal: it isn't shown as lost
        where = "l.status IN ('client','lost') AND COALESCE(l.review_status, '') != 'rejected'"
    elif scope == "mine":  # every lead you're handling, whether or not it has written back
        where = MINE
    else:
        where = (f"((l.status NOT IN ({closed}) AND (l.status IN ('replied','meeting') OR EXISTS "
                 f"(SELECT 1 FROM replies x WHERE x.lead_id = l.id AND x.label IN ({actionable})))) OR EXISTS "
                 f"(SELECT 1 FROM replies r WHERE r.lead_id = l.id AND {waiting}))")
    if scope == "mine" or (scope == "closed" and user["role"] != "admin"):
        where += " AND l.claimed_by = %d" % me
    elif user["role"] != "admin":
        where += " AND (l.claimed_by IS NULL OR l.claimed_by = %d)" % me
    is_blocked = db.blocker()
    with conn() as c:
        names = _user_names(c)
        sc = _score_cols(c)
        rows = [dict(r) for r in c.execute(f"""
            SELECT l.id, l.company, l.email, l.industry, l.status, l.website, l.claimed_by, l.claimed_at,
                   l.next_action, l.next_action_due,
                   {('l.' + sc['score']) if sc['score'] else 'NULL'} AS score,
                   (SELECT COUNT(*) FROM replies r WHERE r.lead_id = l.id AND {waiting}) AS waiting,
                   CASE WHEN l.status IN ({no_email}) THEN 0 ELSE 1 END AS can_email,
                   (SELECT r.id FROM replies r WHERE r.lead_id = l.id AND r.label NOT IN ('bounce','out_of_office')
                        ORDER BY r.id DESC LIMIT 1) AS last_reply_id,
                   (SELECT MAX(m.sent_at) FROM crm_messages m WHERE m.lead_id = l.id AND m.status != 'failed') AS last_answer
            FROM leads l WHERE {where}""")]
        for r in rows:
            last = None
            if r["last_reply_id"]:
                last = c.execute("SELECT label, summary, subject, body, received_at FROM replies WHERE id = ?",
                                 (r["last_reply_id"],)).fetchone()
            r["last_label"] = last["label"] if last else None
            r["last_summary"] = (screen_text(last["summary"]) or (last["body"] or "")[:160]) if last else ""
            r["last_received"] = iso(last["received_at"]) if last else None
            r["last_answer"] = iso(r["last_answer"])
            r["claimed_at"] = iso(r["claimed_at"])
            r["owner"] = names.get(r["claimed_by"])
            r["mine"] = r["claimed_by"] == user["id"]
            r["can_email"] = bool(r["can_email"]) and not is_blocked(r["email"])
            r["due"] = _due(r)
            del r["last_reply_id"]
        mine = c.execute(f"SELECT COUNT(*) FROM leads l WHERE {MINE} AND l.claimed_by = ?", (me,)).fetchone()[0]
        review = c.execute(f"SELECT COUNT(*) FROM leads WHERE review_status = 'pending' AND status IN"
                           f" ({','.join('?' * len(REVIEWABLE))})", REVIEWABLE).fetchone()[0]
    # Waiting on us first, then next steps due today or overdue, newest reply first
    rows.sort(key=lambda r: r["last_received"] or "", reverse=True)
    rows.sort(key=lambda r: (r["waiting"] == 0, not r["due"]))
    counts = {"open": len(rows),
              # waiting on us: an email to answer, or nobody has the lead yet (e.g. it was released)
              "waiting": sum(1 for r in rows if r["waiting"] or not r["claimed_by"]),
              "unclaimed": sum(1 for r in rows if not r["claimed_by"]),
              "mine": mine,  # the same leads My leads shows
              "due": sum(1 for r in rows if r["due"] and r["mine"])}  # your next steps due today or overdue
    if user["role"] == "admin":
        counts["review"] = review  # new leads waiting for an admin's check (To review)
    return {"leads": rows, "counts": counts, "me": user["id"]}


@router.get("/api/crm/organisations")
def organisations(user=Depends(current_user)):
    """Every organisation that has been emailed, by the bot or from the CRM, whether or not
    it has written back. Sales see the ones nobody has claimed and their own."""
    no_email = ",".join("'%s'" % s for s in NO_EMAIL)
    where = ("(EXISTS (SELECT 1 FROM sent s WHERE s.lead_id = l.id) OR EXISTS "
             "(SELECT 1 FROM crm_messages m WHERE m.lead_id = l.id AND m.status != 'failed'))")
    if user["role"] != "admin":
        where += " AND (l.claimed_by IS NULL OR l.claimed_by = %d)" % int(user["id"])
    is_blocked = db.blocker()
    with conn() as c:
        names = _user_names(c)
        sc = _score_cols(c)
        rows = [dict(r) for r in c.execute(f"""
            SELECT l.id, l.company, l.email, l.phone, l.industry, l.website, l.status, l.claimed_by,
                   {('l.' + sc['score']) if sc['score'] else 'NULL'} AS score,
                   CASE WHEN l.status IN ({no_email}) THEN 0 ELSE 1 END AS can_email,
                   (SELECT COUNT(*) FROM sent s WHERE s.lead_id = l.id) AS bot_emails,
                   (SELECT COUNT(*) FROM crm_messages m WHERE m.lead_id = l.id AND m.status != 'failed') AS team_emails,
                   (SELECT MAX(s.sent_on) FROM sent s WHERE s.lead_id = l.id) AS last_bot,
                   (SELECT MAX(m.sent_at) FROM crm_messages m WHERE m.lead_id = l.id AND m.status != 'failed') AS last_team,
                   (SELECT COUNT(*) FROM replies r WHERE r.lead_id = l.id
                        AND COALESCE(r.label,'') NOT IN ('bounce','out_of_office')) AS replies,
                   (SELECT COUNT(*) FROM replies r WHERE r.lead_id = l.id AND COALESCE(r.handled,0) = 0
                        AND COALESCE(r.label,'') != 'bounce') AS waiting
            FROM leads l WHERE {where}""")]
    for r in rows:
        r["emails"] = r.pop("bot_emails") + r.pop("team_emails")
        r["last_emailed"] = max(filter(None, (r.pop("last_bot"), (r.pop("last_team") or "")[:10])), default=None)
        r["can_email"] = bool(r["can_email"]) and not is_blocked(r["email"])
        r["owner"] = names.get(r["claimed_by"])
        r["mine"] = r["claimed_by"] == user["id"]
    rows.sort(key=lambda r: (r["last_emailed"] or "", r["id"]), reverse=True)  # most recently emailed first
    return {"organisations": rows}


@router.get("/api/crm/leads/{lead_id}")
def lead_detail(lead_id: int, user=Depends(current_user)):
    with conn() as c:
        row = _lead_or_404(c, lead_id, user)
        admin = user["role"] == "admin"
        lead = dict(row) if admin else {k: row[k] for k in SALES_FIELDS if k in row.keys()}
        names = _user_names(c)
        if admin:
            lead["score_reasons"] = _reasons(lead.get("score_reasons"))
        sc = _cols(c, "sent")
        subj = "sent_subject" if "sent_subject" in sc else ("subject" if "subject" in sc else None)
        body = "sent_body" if "sent_body" in sc else ("body" if "body" in sc else None)
        step = "step" if "step" in sc else None
        items = []
        for i, r in enumerate(c.execute(f"""SELECT id, sent_on, {step or 'NULL'} AS step,
                {subj or 'NULL'} AS subject, {body or 'NULL'} AS body FROM sent WHERE lead_id = ? ORDER BY id""",
                                        (lead_id,))):
            items.append({"kind": "bot", "id": r["id"], "at": r["sent_on"], "step": r["step"] or i + 1,
                          "subject": r["subject"], "body": r["body"], "who": "LeadBot"})
        for r in c.execute("""SELECT id, from_email, subject, body, label, summary, received_at,
                COALESCE(handled,0) AS handled FROM replies WHERE lead_id = ? ORDER BY id""", (lead_id,)):
            items.append({"kind": "in", "id": r["id"], "at": iso(r["received_at"]), "subject": r["subject"],
                          "body": r["body"], "label": r["label"], "summary": screen_text(r["summary"]),
                          "from": r["from_email"], "handled": bool(r["handled"]), "who": lead["company"]})
        files = defaultdict(list)
        for a in c.execute("SELECT id, message_id, filename, content_type, size FROM crm_attachments "
                           "WHERE lead_id = ? ORDER BY id", (lead_id,)):
            files[a["message_id"]].append({"id": a["id"], "name": a["filename"], "type": a["content_type"],
                                           "size": a["size"]})
        for r in c.execute("SELECT * FROM crm_messages WHERE lead_id = ? ORDER BY id", (lead_id,)):
            items.append({"kind": "out", "id": r["id"], "at": iso(r["sent_at"]), "subject": r["subject"],
                          "body": r["body"], "to": r["to_email"], "status": r["status"], "error": r["error"],
                          "who": names.get(r["user_id"], "Someone"), "attachments": files.get(r["id"], [])})
        for r in c.execute("SELECT * FROM crm_notes WHERE lead_id = ? ORDER BY id", (lead_id,)):
            items.append({"kind": "note", "id": r["id"], "at": iso(r["created_at"]), "body": r["body"],
                          "who": names.get(r["user_id"], "Someone")})
        events = [{"action": r["action"], "detail": r["detail"], "at": iso(r["created_at"]),
                   "who": names.get(r["user_id"], "LeadBot")}
                  for r in c.execute("SELECT * FROM crm_events WHERE lead_id = ? ORDER BY id DESC LIMIT 50", (lead_id,))
                  ] if admin else []  # the history names colleagues and who a lead was assigned to
        can_reply = _can_email(c, row)
        has_response = _has_response(c, lead_id)
    # bot rows only have a date; put them at the start of that day so order stays sensible
    items.sort(key=lambda x: (x["at"] or "") if "T" in (x["at"] or "") else (x["at"] or "") + "T00:00:00Z")
    lead["owner"] = names.get(lead.get("claimed_by"))
    lead["mine"] = lead.get("claimed_by") == user["id"]
    lead["claimed_at"] = iso(lead.get("claimed_at"))
    lead["created_at"] = iso(lead.get("created_at"))
    lead["due"] = _due(lead)
    return {"lead": lead, "thread": items, "events": events, "can_reply": can_reply,
            "lost_reasons": LOST_REASONS, "labels": CORRECTABLE_LABELS,
            "can_act": user["role"] == "admin" or not lead.get("claimed_by") or lead["mine"],
            "can_download": user["role"] == "admin" or lead["mine"],
            "has_response": has_response}


def _require_owner_or_free(lead, user):
    owner = lead["claimed_by"]
    if owner and owner != user["id"] and user["role"] != "admin":
        raise HTTPException(409, "Someone else is handling this lead. Ask an admin to reassign it.")


@router.post("/api/crm/leads/{lead_id}/claim")
def claim(lead_id: int, user=Depends(current_user)):
    with conn() as c:
        lead = _lead_or_404(c, lead_id, user)
        if lead["claimed_by"] == user["id"]:
            return {"ok": True}
        if not lead["claimed_by"] and not _has_response(c, lead_id):
            raise HTTPException(409, NOT_YET)
        # Only succeeds if nobody grabbed it in the meantime
        cur = c.execute("UPDATE leads SET claimed_by = ?, claimed_at = ? WHERE id = ? AND claimed_by IS NULL",
                        (user["id"], now_utc(), lead_id))
        if cur.rowcount == 0:
            if user["role"] != "admin":
                raise HTTPException(409, "Someone else claimed this lead just now.")
            owner = _user_names(c).get(lead["claimed_by"], "someone else")
            raise HTTPException(409, f"{owner} already claimed this lead.")
        log_event(c, lead_id, user["id"], "claimed")
    return {"ok": True}


@router.post("/api/crm/leads/{lead_id}/release")
def release(lead_id: int, user=Depends(current_user)):
    with conn() as c:
        lead = _lead_or_404(c, lead_id, user)
        if not lead["claimed_by"]:
            return {"ok": True}
        if lead["claimed_by"] != user["id"] and user["role"] != "admin":
            raise HTTPException(403, "Only the person handling this lead or an admin can release it.")
        if lead["status"] in WON_OR_LOST and user["role"] != "admin":
            raise HTTPException(409, "A won or lost lead stays with the person who closed it. An admin can reassign it.")
        c.execute("UPDATE leads SET claimed_by = NULL, claimed_at = NULL WHERE id = ?", (lead_id,))
        log_event(c, lead_id, user["id"], "released")
    return {"ok": True}


class AssignIn(BaseModel):
    user_id: int


@router.post("/api/crm/leads/{lead_id}/assign")
def assign(lead_id: int, body: AssignIn, admin=Depends(admin_only)):
    with conn() as c:
        _lead_or_404(c, lead_id, admin)
        target = c.execute("SELECT id, name, active FROM users WHERE id = ?", (body.user_id,)).fetchone()
        if not target or not target["active"]:
            raise HTTPException(400, "That person isn't an active user.")
        c.execute("UPDATE leads SET claimed_by = ?, claimed_at = ? WHERE id = ?", (target["id"], now_utc(), lead_id))
        log_event(c, lead_id, admin["id"], "assigned", target["name"])
    return {"ok": True}


class NoteIn(BaseModel):
    body: str


@router.post("/api/crm/leads/{lead_id}/notes")
def add_note(lead_id: int, body: NoteIn, user=Depends(current_user)):
    text = body.body.strip()
    if not text:
        raise HTTPException(400, "The note is empty.")
    if len(text) > 5000:
        raise HTTPException(400, "Keep notes under 5,000 characters.")
    with conn() as c:
        _lead_or_404(c, lead_id, user)
        c.execute("INSERT INTO crm_notes (lead_id, user_id, body) VALUES (?,?,?)", (lead_id, user["id"], text))
    return {"ok": True}


class StatusIn(BaseModel):
    status: str
    reason: Optional[str] = None   # why a lead was lost: a key of LOST_REASONS
    note: Optional[str] = None     # the words for "other"


# Why a deal was lost, so the report can show what to fix
LOST_REASONS = {"not_interested": "Not interested", "has_provider": "Already has an SMS provider",
                "price": "Price or no budget", "wrong_fit": "Not a fit for SMS", "no_response": "Stopped responding",
                "other": "Other"}


def _lost_reason(body):
    if body.reason not in LOST_REASONS:
        raise HTTPException(400, "Say why the lead was lost.")
    note = " ".join((body.note or "").split())[:200]
    if body.reason == "other" and not note:
        raise HTTPException(400, "Write a few words on why the lead was lost.")
    return f"other: {note}" if body.reason == "other" else body.reason


@router.post("/api/crm/leads/{lead_id}/status")
def crm_status(lead_id: int, body: StatusIn, user=Depends(current_user)):
    if body.status not in CRM_STATUSES:
        raise HTTPException(400, "Unknown stage.")
    with conn() as c:
        lead = _lead_or_404(c, lead_id, user)
        _require_owner_or_free(lead, user)
        reason = _lost_reason(body) if body.status == "lost" else None
        if lead["status"] in ("unsubscribed", "bounced"):
            raise HTTPException(409, "This lead unsubscribed or bounced, so its stage can't change here.")
        responded = _has_response(c, lead_id)
        if not lead["claimed_by"] and not responded and user["role"] != "admin":
            raise HTTPException(409, NOT_YET)
        if not lead["claimed_by"] and responded:  # whoever moves it on is handling it, as when replying
            c.execute("UPDATE leads SET claimed_by = ?, claimed_at = ? WHERE id = ?", (user["id"], now_utc(), lead_id))
            log_event(c, lead_id, user["id"], "claimed", "by setting the stage")
        if lead["status"] != body.status:  # as db.set_status: first meeting/client date, and the lead's history
            c.execute("UPDATE leads SET status = ? WHERE id = ?", (body.status, lead_id))
            if body.status in ("meeting", "client"):
                c.execute(f"UPDATE leads SET {body.status}_at = COALESCE({body.status}_at, ?) WHERE id = ?",
                          (now_utc(), lead_id))
            db.log_event(c, lead_id, "status_changed", old=lead["status"], new=body.status, by="crm")
        c.execute("UPDATE leads SET lost_reason = ? WHERE id = ?", (reason, lead_id))
        if body.status in WON_OR_LOST:
            c.execute("UPDATE replies SET handled = 1 WHERE lead_id = ?", (lead_id,))
        log_event(c, lead_id, user["id"], "stage", body.status + (f" ({reason})" if reason else ""))
    return {"ok": True}


# ---------------------------------------------------------------- next step

def _due(lead):
    """Whether the lead's next step is due today or overdue (Nairobi's today, as the bot counts it)."""
    return bool(lead.get("next_action_due")) and lead["next_action_due"] <= db.today()


class NextIn(BaseModel):
    action: str = ""               # empty clears the next step
    due: Optional[str] = None      # YYYY-MM-DD


@router.post("/api/crm/leads/{lead_id}/next")
def set_next(lead_id: int, body: NextIn, user=Depends(current_user)):
    """What to do next for a lead and when, e.g. "Call Mary about pricing" on 2026-11-03."""
    action = " ".join(body.action.split())
    if len(action) > 200:
        raise HTTPException(400, "Keep the next step under 200 characters.")
    due = (body.due or "").strip() or None
    if due:
        try:
            datetime.strptime(due, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Pick a date for the next step.")
    if not action:
        due = None
    with conn() as c:
        lead = _lead_or_404(c, lead_id, user)
        _require_owner_or_free(lead, user)
        if not lead["claimed_by"] and user["role"] != "admin" and not _has_response(c, lead_id):
            raise HTTPException(409, NOT_YET)
        c.execute("UPDATE leads SET next_action = ?, next_action_due = ? WHERE id = ?", (action or None, due, lead_id))
        log_event(c, lead_id, user["id"], "next_step", f"{action} (due {due})" if action and due else action or "cleared")
    return {"ok": True}


# ---------------------------------------------------------------- correcting a reply's label
# The AI's label decides what is alerted and what the report counts as interested, so a person
# can put it right. Opt-outs and bounces aren't changed here: they decide who may be emailed at
# all and come only from the rules (classifier.opt_out) and delivery reports.

CORRECTABLE_LABELS = {"interested": "Interested", "question": "Question", "not_interested": "Not interested",
                      "out_of_office": "Auto-reply", "needs_review": "Needs review"}


class LabelIn(BaseModel):
    label: str


@router.post("/api/crm/replies/{reply_id}/label")
def correct_label(reply_id: int, body: LabelIn, user=Depends(current_user)):
    import inbox
    if body.label not in CORRECTABLE_LABELS:
        raise HTTPException(400, "Unknown label.")
    with conn() as c:
        r = c.execute("SELECT lead_id, label FROM replies WHERE id = ?", (reply_id,)).fetchone()
        if not r or (r["lead_id"] is None and user["role"] != "admin"):
            raise HTTPException(404, "Reply not found.")
        lead = None
        if r["lead_id"] is not None:
            lead = _lead_or_404(c, r["lead_id"], user)
            _require_owner_or_free(lead, user)
        if r["label"] in ("bounce", "unsubscribe"):
            raise HTTPException(409, "Bounces and unsubscribes can't be relabelled here: they decide who may be emailed.")
        if r["label"] == body.label:
            return {"ok": True}
        c.execute("UPDATE replies SET label = ? WHERE id = ?", (body.label, reply_id))
        c.execute("INSERT INTO reply_labels (reply_id, old_label, new_label, user_id) VALUES (?,?,?,?)",
                  (reply_id, r["label"], body.label, user["id"]))
        log_event(c, r["lead_id"], user["id"], "label_corrected", f"{r['label']} → {body.label}")
        if lead is not None:
            db.log_event(c, lead["id"], "label_corrected", reply_id=reply_id, old=r["label"], new=body.label)
    # as if the reply had come in with the right label: an auto-reply that was really a person
    # answering moves the lead to replied, so it gets no more follow-ups
    new_status = inbox.status_after(lead["status"], body.label) if lead is not None else None
    if new_status:
        db.set_status(lead["id"], new_status, "crm")
    return {"ok": True}


@router.post("/api/crm/replies/{reply_id}/done")
def reply_done(reply_id: int, user=Depends(current_user)):
    with conn() as c:
        r = c.execute("SELECT lead_id FROM replies WHERE id = ?", (reply_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Reply not found.")
        if r["lead_id"] is None and user["role"] != "admin":
            raise HTTPException(404, "Reply not found.")
        _require_owner_or_free(_lead_or_404(c, r["lead_id"], user), user)
        c.execute("UPDATE replies SET handled = 1 WHERE id = ?", (reply_id,))
        log_event(c, r["lead_id"], user["id"], "marked_done")
    return {"ok": True}


# ---------------------------------------------------------------- replying

class AttachmentIn(BaseModel):
    name: str
    data: str            # the file's bytes, base64


class ReplyIn(BaseModel):
    body: str
    subject: Optional[str] = None
    reply_id: Optional[int] = None   # the incoming message being answered
    attachments: List[AttachmentIn] = []


class NewEmailIn(BaseModel):
    subject: str
    body: str
    attachments: List[AttachmentIn] = []


# ---------------------------------------------------------------- attachments

MAX_FILES = 5
MAX_TOTAL = 10 * 1024 * 1024   # all files of one email together
FILE_TYPES = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}
ALLOWED_TEXT = "PDF, Word (doc, docx), Excel (xls, xlsx), CSV, PNG and JPG"
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # old Word and Excel files


def clean_filename(name):
    """Just the file's own name, in plain letters, digits, spaces and . _ - ( ), with the
    extension in lower case: no folders, no control characters, at most 80 characters before it."""
    name = unicodedata.normalize("NFKC", name or "")
    name = re.split(r"[\\/]", name)[-1]
    name = re.sub(r"[^A-Za-z0-9 ._()\-]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = stem.strip(" ._")[:80].strip(" ._") or "attachment"
    return f"{stem}.{ext.lower()}" if ext else stem


def file_kinds(data):
    """The extensions a file's bytes really are, read from its contents, not its name."""
    if data.startswith(b"%PDF-"):
        return {"pdf"}
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return {"png"}
    if data.startswith(b"\xff\xd8\xff"):
        return {"jpg", "jpeg"}
    if data.startswith(OLE):
        if "WordDocument".encode("utf-16-le") in data:
            return {"doc"}
        if "Workbook".encode("utf-16-le") in data or "Book".encode("utf-16-le") in data:
            return {"xls"}
        return set()
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
                info = z.getinfo("[Content_Types].xml")
                types = z.read(info) if info.file_size < 1_000_000 else b""
        except (zipfile.BadZipFile, KeyError, RuntimeError, ValueError, OSError):
            return set()
        if not types or b"macroEnabled" in types or any(n.lower().endswith("vbaproject.bin") for n in names):
            return set()   # files with macros are never sent
        if "word/document.xml" in names:
            return {"docx"}
        if "xl/workbook.xml" in names:
            return {"xlsx"}
        return set()
    # CSV is plain text: readable as UTF-8 (or Windows' own encoding), no binary bytes
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError:
            return set()
    if any(ord(ch) < 32 and ch not in "\t\r\n" for ch in text):
        return set()
    return {"csv"}


def read_attachments(items):
    """The files to send, checked: at most MAX_FILES, MAX_TOTAL bytes together, only the
    allowed types, each one really the type its name says. Any problem stops the email."""
    if len(items) > MAX_FILES:
        raise HTTPException(400, f"You can attach at most {MAX_FILES} files to one email.")
    files, total = [], 0
    for item in items:
        name = clean_filename(item.name)
        ext = name.rpartition(".")[2] if "." in name else ""
        if ext not in FILE_TYPES:
            raise HTTPException(400, f"{name} can't be attached. Only {ALLOWED_TEXT} files can be sent.")
        if len(item.data) > MAX_TOTAL * 4 // 3 + 8:
            raise HTTPException(400, "Attachments can be 10 MB in total at most.")
        try:
            data = base64.b64decode(item.data, validate=True)
        except ValueError:
            raise HTTPException(400, f"{name} couldn't be read. Try attaching it again.")
        if not data:
            raise HTTPException(400, f"{name} is empty.")
        total += len(data)
        if total > MAX_TOTAL:
            raise HTTPException(400, "Attachments can be 10 MB in total at most.")
        if ext not in file_kinds(data):
            raise HTTPException(400, f"{name} isn't really a .{ext} file, so it can't be attached. "
                                     f"Only {ALLOWED_TEXT} files can be sent.")
        files.append({"filename": name, "content_type": FILE_TYPES[ext], "size": len(data), "data": data})
    return files


# ---------------------------------------------------------------- the email itself

FONT = "font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#222222"


def sender_name(user):
    """The name every email is signed with: SENDER_NAME in .env, whoever presses Send, so a lead
    hears from one person all along. The sender's own name only if SENDER_NAME is blank."""
    return (config.SENDER_NAME or "").strip() or user["name"]


def signature(user):
    """Name, company and phone. Like the name, the phone is SENDER_PHONE whoever sends;
    the sender's own phone only if SENDER_PHONE is blank."""
    lines = [sender_name(user), config.COMPANY]
    phone = (config.SENDER_PHONE or "").strip() or (user.get("phone") or "").strip()
    if phone:
        lines.append(phone)
    return "\n".join(lines)


def _when_text(ts):
    """A stored UTC time as the reader's "Mon, 20 Oct 2026 at 14:05" (this server's time zone)."""
    try:
        d = datetime.strptime((ts or "")[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return (ts or "")[:16]
    d = d.replace(tzinfo=timezone.utc).astimezone()
    return f"{d:%a}, {d.day} {d:%b %Y} at {d:%H:%M}"


def _readable(text):
    """Some mail programs put web code in the plain-text part ("<p>Hello</p><p><br></p>"):
    the text it stands for, so a reply never quotes raw tags back to the client. Anything
    else is returned as it is (a lone "<" or "a > b" in plain text stays)."""
    text = text or ""
    if not re.search(r"(?i)</(p|div)>|<br\s*/?>", text):
        return text
    text = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", "", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])\s*>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]*>", "", text))
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _quote(text, who, when):
    text = (text or "").strip()
    if not text:
        return ""
    quoted = "\n".join("> " + line for line in text.splitlines()[:60])
    return f"\n\nOn {when}, {who} wrote:\n{quoted}"


def _html_text(text):
    return "<br>\n".join(html.escape(line, quote=False) for line in text.splitlines())


def _html_quote(text, who, when):
    """Their message in a light grey block, without the "> " marks of older quoting."""
    lines = []
    for line in (text or "").strip().splitlines()[:60]:
        line = re.sub(r"^\s*(>\s?)+", "", line)
        line = re.sub(r"<([^<>\s@]+@[^<>\s]+)>", r"\1", line)   # Name <a@b.com> -> Name a@b.com
        lines.append(line.replace(">", "›"))
    if not any(l.strip() for l in lines):
        return ""
    return (f'<div style="margin-top:28px;font-size:13px;color:#666666">On {html.escape(when)}, '
            f'{html.escape(who.replace(">", "›"))} wrote:</div>'
            f'<div style="margin-top:6px;padding:10px 14px;background:#f5f5f5;border-left:3px solid #d6d6d6;'
            f'border-radius:4px;font-size:13px;line-height:1.5;color:#666666">{_html_text(chr(10).join(lines))}</div>')


def _html_signature(user):
    name, *rest = signature(user).splitlines()
    return (f'<div style="margin-top:22px;padding-top:10px;border-top:1px solid #e6e6e6;font-size:13px;'
            f'line-height:1.5;color:#555555"><strong style="color:#222222">{html.escape(name)}</strong><br>'
            + "<br>".join(html.escape(line) for line in rest) + "</div>")


def build_email(user, to, subject, text, orig=None, who="", refs=(), files=()):
    """The email as it goes out: plain text and HTML versions, then any attachments.
    Returns it with its plain text, which is what the conversation keeps."""
    when = _when_text(orig["received_at"]) if orig is not None else ""
    plain = text + "\n\n" + signature(user)
    page = f'<div style="{FONT}">{_html_text(text)}{_html_signature(user)}'
    if orig is not None:
        theirs = _readable(orig["body"])
        plain += _quote(theirs, who, when)
        page += _html_quote(theirs, who, when)
    page += "</div>"

    msg = EmailMessage()
    msg["From"] = formataddr((f"{sender_name(user)} from TalkSasa", CRM_FROM_EMAIL))
    msg["To"] = to
    msg["Reply-To"] = CRM_FROM_EMAIL
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(CRM_FROM_EMAIL or "talksasa.co.ke").split("@")[-1])
    if refs:
        msg["In-Reply-To"] = refs[0]
        msg["References"] = " ".join(reversed(refs))
    msg.set_content(plain)
    msg.add_alternative(f"<!doctype html><html><body style=\"margin:0;padding:16px 0\">{page}</body></html>",
                        subtype="html")
    for f in files:
        maintype, subtype = f["content_type"].split("/")
        msg.add_attachment(f["data"], maintype=maintype, subtype=subtype, filename=f["filename"])
    return msg, plain


def _deliver(msg):
    if CRM_DRY_RUN:
        return
    if not (config.SMTP_HOST and CRM_SMTP_USER and CRM_SMTP_PASS):
        raise RuntimeError("Email isn't set up: SMTP_HOST and the mailbox login are missing from .env.")
    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(config.SMTP_HOST, 465, context=ctx, timeout=30) as s:
            s.login(CRM_SMTP_USER, CRM_SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(CRM_SMTP_USER, CRM_SMTP_PASS)
            s.send_message(msg)


def _file_in_sent(msg):
    """Best effort: put a copy in the mailbox's Sent folder so it shows in webmail."""
    if CRM_DRY_RUN or not CRM_SENT_FOLDER or not config.IMAP_HOST:
        return
    try:
        with imaplib.IMAP4_SSL(config.IMAP_HOST, timeout=20) as m:
            m.login(CRM_SMTP_USER, CRM_SMTP_PASS)
            m.append(CRM_SENT_FOLDER, "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    except Exception:
        pass


def _check_sendable(text):
    if not text:
        raise HTTPException(400, "Write a message first.")
    if not CRM_FROM_EMAIL or not EMAIL_RE.match(CRM_FROM_EMAIL):
        raise HTTPException(503, "Replies aren't set up yet: add FROM_EMAIL (or CRM_FROM_EMAIL) to .env and restart.")
    if len(text) > 20000:
        raise HTTPException(400, "That message is too long.")


def _open_for_email(c, lead_id, user):
    lead = _lead_or_404(c, lead_id, user)
    _require_owner_or_free(lead, user)
    if not _can_email(c, lead):
        raise HTTPException(409, "This lead unsubscribed or their address bounced. Don't email them.")
    if not lead["claimed_by"] and user["role"] != "admin" and not _has_response(c, lead_id):
        raise HTTPException(409, "This organisation hasn't replied yet, so it can't be claimed or emailed from here. "
                                 "You can once it writes back.")
    return lead


def _send(c, lead, user, to, subject, msg, plain, files, in_reply_to, event):
    """Send one CRM email and record it (with its attachments). The lead becomes the
    sender's if nobody had it, and the bot sends it nothing more."""
    lead_id = lead["id"]
    status, error = ("dry_run" if CRM_DRY_RUN else "sent"), None
    try:
        _deliver(msg)
    except Exception as e:  # keep a record of what failed
        status, error = "failed", str(e)[:300]
    message_id = c.execute("""INSERT INTO crm_messages (lead_id, user_id, to_email, subject, body, message_id,
                 in_reply_to, status, error) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id""",
                           (lead_id, user["id"], to, subject, plain, msg["Message-ID"], in_reply_to,
                            status, error)).fetchone()[0]
    if status == "failed":
        log_event(c, lead_id, user["id"], "reply_failed", error)
        c.commit()
        raise HTTPException(502, f"The email didn't send: {error}")
    for f in files:
        c.execute("""INSERT INTO crm_attachments (message_id, lead_id, filename, content_type, size, data)
                     VALUES (?,?,?,?,?,?)""", (message_id, lead_id, f["filename"], f["content_type"], f["size"], f["data"]))
    if not lead["claimed_by"] and _has_response(c, lead_id):  # one that hasn't replied stays unclaimed (admins)
        c.execute("UPDATE leads SET claimed_by = ?, claimed_at = ? WHERE id = ?", (user["id"], now_utc(), lead_id))
        log_event(c, lead_id, user["id"], "claimed", "by emailing")
    c.execute("UPDATE leads SET crm_emailed_at = ? WHERE id = ? AND crm_emailed_at IS NULL", (now_utc(), lead_id))
    log_event(c, lead_id, user["id"], event, to)
    return status


@router.post("/api/crm/leads/{lead_id}/reply")
def send_reply(lead_id: int, body: ReplyIn, user=Depends(current_user)):
    text = body.body.strip()
    _check_sendable(text)
    files = read_attachments(body.attachments)
    with conn() as c:
        lead = _open_for_email(c, lead_id, user)
        orig = None
        if body.reply_id:
            orig = c.execute("SELECT * FROM replies WHERE id = ? AND lead_id = ?", (body.reply_id, lead_id)).fetchone()
            if not orig:
                raise HTTPException(404, "The message you're answering wasn't found.")
        else:
            orig = c.execute("""SELECT * FROM replies WHERE lead_id = ? AND label NOT IN ('bounce','out_of_office')
                                ORDER BY id DESC LIMIT 1""", (lead_id,)).fetchone()
        to = (orig["from_email"] if orig and orig["from_email"] else lead["email"] or "").strip()
        m = re.search(r"<([^>]+)>", to)
        to = (m.group(1) if m else to).strip()
        if not EMAIL_RE.match(to):
            raise HTTPException(400, f"The lead's email address doesn't look valid: {to or 'missing'}.")

        subject = (body.subject or "").strip()
        if not subject:
            base = (orig["subject"] if orig else None) or "TalkSasa"
            subject = base if base.lower().startswith("re:") else "Re: " + base
        subject = subject.replace("\r", " ").replace("\n", " ")[:200]

        # Thread it with the conversation: answer the lead's message, else our last email to them
        refs = []
        rc = _cols(c, "replies")
        if orig is not None and "message_id" in rc and orig["message_id"]:
            refs.append(orig["message_id"])
        last_out = c.execute("SELECT message_id FROM crm_messages WHERE lead_id = ? AND message_id IS NOT NULL "
                             "ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
        last_bot = c.execute("SELECT message_id FROM sent WHERE lead_id = ? AND message_id IS NOT NULL "
                             "ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
        for r in (last_out, last_bot):
            if r and r["message_id"] and r["message_id"] not in refs:
                refs.append(r["message_id"])

        msg, plain = build_email(user, to, subject, text, orig, (orig["from_email"] if orig is not None else "")
                                 or lead["company"], refs, files)
        status = _send(c, lead, user, to, subject, msg, plain, files, refs[0] if refs else None, "replied")
        c.execute("UPDATE replies SET handled = 1 WHERE lead_id = ? AND COALESCE(handled,0) = 0", (lead_id,))
        if lead["status"] in ("sent", "verified", "new"):
            c.execute("UPDATE leads SET status = 'replied' WHERE id = ?", (lead_id,))
    _file_in_sent(msg)
    return {"ok": True, "status": status, "to": to}


@router.post("/api/crm/leads/{lead_id}/email")
def send_new_email(lead_id: int, body: NewEmailIn, user=Depends(current_user)):
    """A fresh email to the lead (its own thread), whenever the person handling it wants."""
    text = body.body.strip()
    subject = body.subject.replace("\r", " ").replace("\n", " ").strip()[:200]
    if not subject:
        raise HTTPException(400, "Add a subject.")
    _check_sendable(text)
    files = read_attachments(body.attachments)
    with conn() as c:
        lead = _open_for_email(c, lead_id, user)
        to = (lead["email"] or "").strip()
        if not EMAIL_RE.match(to):
            raise HTTPException(400, f"The lead's email address doesn't look valid: {to or 'missing'}.")
        msg, plain = build_email(user, to, subject, text, files=files)
        status = _send(c, lead, user, to, subject, msg, plain, files, None, "emailed")
    _file_in_sent(msg)
    return {"ok": True, "status": status, "to": to}


@router.get("/api/crm/attachments/{attachment_id}")
def download_attachment(attachment_id: int, user=Depends(current_user)):
    """Only the person handling the lead, or an admin, can download what was sent to it."""
    with conn() as c:
        a = c.execute("""SELECT a.filename, a.content_type, a.data, l.claimed_by FROM crm_attachments a
                         JOIN leads l ON l.id = a.lead_id WHERE a.id = ?""", (attachment_id,)).fetchone()
    if not a or not _sees(user, a):
        raise HTTPException(404, "Attachment not found.")
    if user["role"] != "admin" and a["claimed_by"] != user["id"]:
        raise HTTPException(403, "Only the person handling this lead or an admin can download its attachments.")
    return Response(bytes(a["data"]), media_type=a["content_type"], headers={
        "Content-Disposition": f"attachment; filename=\"{a['filename']}\"; filename*=UTF-8''{quote(a['filename'])}",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


# ---------------------------------------------------------------- what's working

NOT_REAL_REPLY = ("bounce", "out_of_office")
MIN_SAMPLE = 20


def _band(score):
    if score is None:
        return "Not scored"
    s = float(score)
    if s >= 80:
        return "80–100"
    if s >= 60:
        return "60–79"
    if s >= 50:
        return "50–59"
    return "Below 50"


@router.get("/api/crm/report")
def report(days: int = 0, user=Depends(admin_only)):
    """Reply results by segment, score band and source, counted per lead that was emailed.

    days = 0 means all time; otherwise only leads first emailed in the last N days.
    """
    if days < 0 or days > 3650:
        raise HTTPException(400, "Pick a period between 1 and 3650 days, or all time.")
    with conn() as c:
        sc = _score_cols(c)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d") if days else "0000"
        rows = c.execute(f"""
            SELECT l.id, COALESCE(NULLIF(l.industry, ''), 'default') AS industry, l.status,
                   {('l.' + sc['score']) if sc['score'] else 'NULL'} AS score,
                   {("COALESCE(NULLIF(l." + sc['source'] + ", ''), 'Web search')") if sc['source'] else "'Web search'"} AS source,
                   MIN(s.sent_on) AS first_sent,
                   MAX(CASE WHEN r.label = 'bounce' THEN 1 ELSE 0 END) AS bounced_reply,
                   MAX(CASE WHEN r.id IS NOT NULL AND r.label NOT IN ('bounce','out_of_office') THEN 1 ELSE 0 END) AS replied,
                   MAX(CASE WHEN r.label = 'interested' THEN 1 ELSE 0 END) AS interested_reply
            FROM leads l
            JOIN sent s ON s.lead_id = l.id
            LEFT JOIN replies r ON r.lead_id = l.id
            GROUP BY l.id
            HAVING MIN(s.sent_on) >= ?""", (since,)).fetchall()

    def blank():
        return {"emailed": 0, "bounced": 0, "reachable": 0, "replied": 0, "interested": 0, "meetings": 0, "clients": 0}

    groups = {"segment": {}, "score": {}, "source": {}}
    total = blank()
    for r in rows:
        bounced = r["status"] == "bounced" or bool(r["bounced_reply"])
        interested = bool(r["interested_reply"]) or r["status"] in ("meeting", "client")
        meeting = r["status"] in ("meeting", "client")
        keys = {"segment": r["industry"], "score": _band(r["score"]), "source": r["source"]}
        for g, key in keys.items():
            for bucket in (groups[g].setdefault(key, blank()),) + ((total,) if g == "segment" else ()):
                bucket["emailed"] += 1
                bucket["bounced"] += bounced
                bucket["reachable"] += not bounced
                bucket["replied"] += bool(r["replied"]) and not bounced
                bucket["interested"] += interested
                bucket["meetings"] += meeting
                bucket["clients"] += r["status"] == "client"

    def finish(d):
        reach = d["reachable"]
        d["reply_rate"] = round(100 * d["replied"] / reach, 1) if reach else None
        d["interested_rate"] = round(100 * d["interested"] / reach, 1) if reach else None
        d["small_sample"] = reach < MIN_SAMPLE
        return d

    order = {"score": ["80–100", "60–79", "50–59", "Below 50", "Not scored"]}
    out = {}
    for g, d in groups.items():
        items = [dict(finish(v), name=k) for k, v in d.items()]
        if g in order:
            items.sort(key=lambda x: order[g].index(x["name"]) if x["name"] in order[g] else 99)
        else:
            items.sort(key=lambda x: (-x["emailed"], x["name"]))  # ties by name, on any database
        out[g] = items
    with conn() as c:
        lost = [{"name": LOST_REASONS.get(r["reason"], "Other" if r["reason"] else "No reason given"), "n": r["n"]}
                for r in c.execute("""SELECT CASE WHEN lost_reason LIKE 'other:%' THEN 'other' ELSE lost_reason END AS reason,
                                             COUNT(*) AS n
                                      FROM leads WHERE status = 'lost' AND COALESCE(review_status, '') != 'rejected'
                                      GROUP BY 1 ORDER BY n DESC""")]
    return {"total": finish(total), "groups": out, "days": days, "min_sample": MIN_SAMPLE,
            "has_scores": bool(sc["score"]), "has_source": bool(sc["source"]),
            "searches": _search_results(since), "lost_reasons": lost}


def _search_key(query):
    """Searches grouped the way they are written in discover.QUERIES: the town each run picks
    is put back as {town}, and a company found on a list page counts under that list's site."""
    import discover
    query = query or "(no search recorded)"
    if query.startswith("listed on "):
        return "listed on " + discover.domain_of(query.removeprefix("listed on "))
    for town in discover.TOWNS:
        query = re.sub(rf"\b{re.escape(town)}\b", "{town}", query)
    return query


def _search_results(since):
    """For each web search: what it found and what became of those leads, counted from every
    lead it found (not only the emailed ones), so a search that finds dead addresses or
    wrong-fit organisations stands out. since: first found on or after this date."""
    with conn() as c:
        rows = c.execute("""
            SELECT l.id, l.source_query, l.status, l.review_status,
                   EXISTS (SELECT 1 FROM sent s WHERE s.lead_id = l.id) AS emailed,
                   EXISTS (SELECT 1 FROM replies r WHERE r.lead_id = l.id
                           AND r.label NOT IN ('bounce','out_of_office')) AS replied,
                   EXISTS (SELECT 1 FROM replies r WHERE r.lead_id = l.id AND r.label = 'interested') AS interested
            FROM leads l
            WHERE l.source = 'search' AND COALESCE(l.discovered_at, l.created_at) >= ?""", (since,)).fetchall()
    groups = {}
    for r in rows:
        g = groups.setdefault(_search_key(r["source_query"]), {
            "found": 0, "invalid": 0, "rejected": 0, "emailed": 0, "bounced": 0, "replied": 0, "interested": 0})
        g["found"] += 1
        g["invalid"] += r["status"] == "invalid"
        g["rejected"] += r["review_status"] == "rejected"
        g["emailed"] += bool(r["emailed"])
        g["bounced"] += r["status"] == "bounced"
        g["replied"] += bool(r["replied"])
        g["interested"] += bool(r["interested"]) or r["status"] in ("meeting", "client")
    out = []
    for name, g in groups.items():
        usable = g["found"] - g["invalid"] - g["rejected"] - g["bounced"]
        out.append(dict(g, name=name, usable_rate=round(100 * usable / g["found"], 1),
                        small_sample=g["found"] < MIN_SAMPLE))
    return sorted(out, key=lambda x: (-x["found"], x["name"]))


# ---------------------------------------------------------------- is the bot healthy? (admin)

def _minutes_since(path):
    try:
        return (time.time() - path.stat().st_mtime) / 60
    except OSError:
        return None


@router.get("/api/crm/health")
def health(admin=Depends(admin_only)):
    """One line per thing that can quietly stop the bot, each "ok", "warn" or "bad", so "nothing
    happened today" can be told apart from "something broke"."""
    import inbox
    import sender
    items = []
    age, error = _minutes_since(HERE / "last_check.txt"), inbox.last_error()
    if error:
        items.append({"key": "inbox", "level": "bad", "text": f"Inbox checks failing ({error['failures']} in a row)",
                      "detail": error["error"]})
    elif age is None:
        items.append({"key": "inbox", "level": "warn", "text": "Inbox never checked", "detail": "Start the bot: python start.py"})
    else:
        stale = age > max(20, config.POLL_MINUTES * 4)
        items.append({"key": "inbox", "level": "bad" if age > 24 * 60 else "warn" if stale else "ok",
                      "text": f"Inbox checked {int(age)} min ago" if age < 120 else f"Inbox last checked {int(age // 60)} h ago",
                      "detail": "Replies wait until the bot checks again." if stale else ""})
    gate = sender.bounce_gate()
    items.append({"key": "bounces", "level": "bad" if gate else "ok",
                  "text": "Sending paused: too many bounces" if gate else "Bounce rate fine", "detail": gate or ""})
    sent, limit = db.sent_today(), config.daily_limit()
    items.append({"key": "sent", "level": "ok", "text": f"{sent} of {limit} sent today", "detail": ""})
    claims = db.send_claims()
    if claims:
        items.append({"key": "claims", "level": "bad", "text": f"{len(claims)} send left unfinished",
                      "detail": "A send stopped mid-email: " + ", ".join(r["email"] or "?" for r in claims)
                                + ". Check the Sent folder, then delete the row from send_claims."})
    due = len(sender.followups_due())
    if due:
        items.append({"key": "followups", "level": "ok", "text": f"{due} follow-up{'s' if due != 1 else ''} due", "detail": ""})
    with conn() as c:
        pending = c.execute(f"SELECT COUNT(*) FROM leads WHERE review_status = 'pending' AND status IN"
                            f" ({','.join('?' * len(REVIEWABLE))})", REVIEWABLE).fetchone()[0]
    if pending:
        items.append({"key": "review", "level": "warn", "text": f"{pending} new lead{'s' if pending != 1 else ''} to review",
                      "detail": "They aren't emailed until you approve them."})
    return {"items": items}


# ---------------------------------------------------------------- review new leads (admin)
# Leads found by web search are held (review_status 'pending') until an admin confirms what
# kind of organisation they are, since the search that found them is only a guess: a hotel
# found by a church search would otherwise get the church email.

REVIEWABLE = ("new", "verified")  # pending leads with a dead address aren't worth a look


@router.get("/api/crm/review")
def review_queue(admin=Depends(admin_only)):
    import personalise
    with conn() as c:
        rows = [dict(r) for r in c.execute(f"""
            SELECT id, company, email, website, industry, status, score, source_query, source_url,
                   COALESCE(discovered_at, created_at) AS found_at
            FROM leads WHERE review_status = 'pending' AND status IN ({",".join("?" * len(REVIEWABLE))})
            ORDER BY id""", REVIEWABLE)]
    for r in rows:
        r["found_at"] = iso(r["found_at"])
        r["free_mail"] = db.domain_of(r["email"]) in db.FREE_MAIL
    industries = [{"name": k, "approved": bool(v.get("approved"))}
                  for k, v in sorted(personalise.USES.items()) if k != "default"]
    return {"leads": rows, "industries": industries}


@router.get("/api/crm/review/{lead_id}/site")
def review_site(lead_id: int, admin=Depends(admin_only)):
    """A short look at the lead's website, so its industry can be checked without leaving the CRM."""
    import scraper
    with conn() as c:
        lead = _lead_or_404(c, lead_id, admin)
    try:
        text = scraper.site_text(lead["website"], 1500) if lead["website"] else ""
    except Exception:
        text = ""
    return {"text": text}


class ReviewIn(BaseModel):
    decision: str            # approve / reject
    industry: Optional[str] = None


@router.post("/api/crm/review/{lead_id}")
def review_lead(lead_id: int, body: ReviewIn, admin=Depends(admin_only)):
    import personalise
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "Choose approve or reject.")
    with conn() as c:
        lead = _lead_or_404(c, lead_id, admin)
        if lead["review_status"] != "pending":
            raise HTTPException(409, "This lead was already reviewed.")
        if body.decision == "approve":
            industry = (body.industry or "").strip()
            if industry not in personalise.USES or industry == "default":
                raise HTTPException(400, "Pick the organisation's industry.")
            # scored and personalised again for its real industry before its first email
            c.execute("""UPDATE leads SET industry = ?, review_status = 'approved', reviewed_by = ?, reviewed_at = ?,
                         scored_at = NULL, confidence = NULL WHERE id = ?""",
                      (industry, admin["id"], now_utc(), lead_id))
            db.log_event(c, lead_id, "reviewed", decision="approved", industry=industry,
                         was=lead["industry"] if lead["industry"] != industry else None)
            log_event(c, lead_id, admin["id"], "review_approved", industry)
        else:
            c.execute("UPDATE leads SET review_status = 'rejected', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                      (admin["id"], now_utc(), lead_id))
            db.log_event(c, lead_id, "reviewed", decision="rejected")
            log_event(c, lead_id, admin["id"], "review_rejected", "not a fit")
    if body.decision == "reject":
        db.set_status(lead_id, "lost", "review")  # never emailed; kept so it isn't found and added again
    return {"ok": True}


# ---------------------------------------------------------------- team (admin)

class NewUserIn(BaseModel):
    name: str
    email: str
    role: str = "sales"
    phone: str = ""
    temp_password: str


class EditUserIn(BaseModel):
    role: Optional[str] = None
    active: Optional[bool] = None
    name: Optional[str] = None
    phone: Optional[str] = None
    temp_password: Optional[str] = None


@router.get("/api/crm/users")
def list_users(user=Depends(current_user)):
    with conn() as c:
        rows = [dict(r) for r in c.execute("""
            SELECT u.id, u.name, u.email, u.phone, u.role, u.active, u.must_change, u.last_login,
                   (SELECT COUNT(*) FROM leads l WHERE l.claimed_by = u.id AND l.status NOT IN ('client','lost')) AS open_leads,
                   (SELECT COUNT(*) FROM crm_messages m WHERE m.user_id = u.id AND m.status != 'failed') AS replies_sent
            FROM users u ORDER BY u.active DESC, u.role, u.name""")]
    for r in rows:
        r["active"], r["must_change"] = bool(r["active"]), bool(r["must_change"])
        r["last_login"] = iso(r["last_login"])
    if user["role"] != "admin":  # sales don't see who handles the other leads
        rows = [{"id": r["id"], "name": r["name"], "active": r["active"]} for r in rows if r["id"] == user["id"]]
    return {"users": rows}


def _clean_email(email):
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address.")
    return email


@router.post("/api/crm/users")
def create_user(body: NewUserIn, admin=Depends(admin_only)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Enter a name.")
    if body.role not in ROLES:
        raise HTTPException(400, "Role must be admin or sales.")
    problem = password_problem(body.temp_password)
    if problem:
        raise HTTPException(400, problem)
    email = _clean_email(body.email)
    with conn() as c:
        try:
            c.execute("""INSERT INTO users (name, email, phone, role, password_hash, must_change)
                         VALUES (?,?,?,?,?,1)""",
                      (name, email, body.phone.strip(), body.role, hash_password(body.temp_password)))
        except db.IntegrityError:
            raise HTTPException(409, "Someone with that email already has an account.")
        log_event(c, None, admin["id"], "user_created", f"{email} ({body.role})")
    return {"ok": True}


@router.post("/api/crm/users/{user_id}")
def edit_user(user_id: int, body: EditUserIn, admin=Depends(admin_only)):
    with conn() as c:
        target = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(404, "User not found.")
        demoting = body.role == "sales" and target["role"] == "admin"
        disabling = body.active is False and target["active"]
        if (demoting or disabling) and target["role"] == "admin":
            admins = c.execute("SELECT COUNT(*) n FROM users WHERE role = 'admin' AND active = 1").fetchone()["n"]
            if admins <= 1:
                raise HTTPException(409, "There must always be at least one active admin.")
        if user_id == admin["id"] and (demoting or disabling):
            raise HTTPException(409, "You can't remove your own admin access. Ask another admin.")
        if body.role is not None:
            if body.role not in ROLES:
                raise HTTPException(400, "Role must be admin or sales.")
            c.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
        if body.name is not None and body.name.strip():
            c.execute("UPDATE users SET name = ? WHERE id = ?", (body.name.strip(), user_id))
        if body.phone is not None:
            c.execute("UPDATE users SET phone = ? WHERE id = ?", (body.phone.strip(), user_id))
        if body.active is not None:
            c.execute("UPDATE users SET active = ? WHERE id = ?", (int(body.active), user_id))
            if not body.active:
                c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                c.execute("UPDATE leads SET claimed_by = NULL, claimed_at = NULL WHERE claimed_by = ? "
                          "AND status NOT IN ('client','lost')", (user_id,))
        if body.temp_password:
            problem = password_problem(body.temp_password)
            if problem:
                raise HTTPException(400, problem)
            c.execute("UPDATE users SET password_hash = ?, must_change = 1 WHERE id = ?",
                      (hash_password(body.temp_password), user_id))
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        log_event(c, None, admin["id"], "user_updated", target["email"])
    return {"ok": True}


# ---------------------------------------------------------------- email templates (admin)
# The bot reads templates/<industry>.txt (first email: a "Subject:" line, then the text),
# templates/followup/ and templates/followup2/ (text only) every time it builds an email,
# so a template saved here is used from the next email that goes out.

TEMPLATE_DIR = HERE / "templates"
TEMPLATE_KINDS = {"first": TEMPLATE_DIR, "followup": TEMPLATE_DIR / "followup", "followup2": TEMPLATE_DIR / "followup2"}
TEMPLATE_NAME = re.compile(r"^[a-z0-9_]+$")
# The gaps sender.fill can fill; a follow-up also gets {offer} (sender.FOLLOWUP_OFFERS)
TEMPLATE_FIELDS = ("company", "uses", "opener", "attachment_line", "sender", "our_company", "phone", "website",
                   "profile_line", "profile_url", "industry")
MAX_TEMPLATE_CHARS = 6000


def _fields(kind):
    return set(TEMPLATE_FIELDS) | ({"offer"} if kind != "first" else set())


def _template_path(kind, name):
    """The template's file. Only files that already exist: templates are edited here, never created."""
    if kind not in TEMPLATE_KINDS or not TEMPLATE_NAME.match(name or ""):
        raise HTTPException(404, "No such template.")
    path = TEMPLATE_KINDS[kind] / f"{name}.txt"
    if not path.is_file():
        raise HTTPException(404, "No such template.")
    return path


def _read_template(kind, path):
    """(subject, body) the way sender.load_template reads them; subject is None for a follow-up."""
    text = path.read_text(encoding="utf-8")
    if kind != "first":
        return None, text.strip()
    subject, _, body = text.partition("\n")
    return subject.replace("Subject:", "", 1).strip(), body.strip()


def _write_template(kind, path, subject, body):
    text = (f"Subject: {subject}\n" if kind == "first" else "") + body.strip() + "\n"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:  # "\n" so the file doesn't change to CRLF on Windows
        f.write(text)
    os.replace(tmp, path)  # all at once: the bot never reads half a template


def _sample_lead(name):
    return {"id": None, "company": "Example Company", "industry": "" if name == "default" else name,
            "email": "info@example.co.ke", "opener": None, "uses": None, "confidence": None}


def _render(kind, name, subject, body):
    """The email an example company would get from this template: built the way the bot builds it."""
    import sender
    lead = _sample_lead(name)
    if kind == "first":
        msg = sender.compose(lead, sender.fill(subject, lead), body, sender.attachment())
    else:
        first = sender.fill(sender.load_template(lead["industry"])[1], lead)
        msg = sender.compose(lead, first if first.lower().startswith("re:") else "Re: " + first, body,
                             extra=sender.followup_values(lead))
    return {"subject": str(msg["Subject"]), "text": sender.plain_of(msg),
            "attachments": [p.get_filename() for p in msg.iter_attachments()]}


def _template_problem(kind, name, subject, body):
    """Why this template can't be saved (it would break or change sending in a way nobody meant), or None."""
    import sender
    if kind == "first":
        if not subject:
            return "The subject can't be empty."
        if "\n" in subject or "\r" in subject:
            return "The subject must be one line."
        if len(subject) > 150:
            return "Keep the subject under 150 characters."
    if not body.strip():
        return "The email text can't be empty."
    if len(body) > MAX_TEMPLATE_CHARS:
        return f"Keep the email text under {MAX_TEMPLATE_CHARS:,} characters."
    allowed = _fields(kind)
    for text in (subject or "", body):
        try:
            parts = list(string.Formatter().parse(text))
        except ValueError:
            return "There is a { or } without its partner. Gaps look like {company}; to show a brace itself, write {{ or }}."
        for _, field, spec, conv in parts:
            if field is not None and (field not in allowed or spec or conv):
                return (f"{{{field}}} isn't a gap LeadBot can fill. Use one of: "
                        + ", ".join("{" + f + "}" for f in sorted(allowed)) + ".")
    if kind == "followup" and sender.FOLLOWUP1_LINE not in body:
        # sender.build_followup swaps this line for leads whose first email had older wording
        return f'Keep this line exactly as it is, LeadBot changes it for leads emailed with older wording: "{sender.FOLLOWUP1_LINE}"'
    try:
        _render(kind, name, subject, body)
    except Exception as e:  # anything the bot itself would trip over when sending
        return f"LeadBot couldn't build an email from this ({type(e).__name__}: {e})."
    return None


class TemplateIn(BaseModel):
    subject: Optional[str] = None
    body: str


@router.get("/api/crm/templates")
def list_templates(admin=Depends(admin_only)):
    import personalise
    out = []
    for path in sorted(TEMPLATE_DIR.glob("*.txt")):
        name = path.stem
        if not TEMPLATE_NAME.match(name):
            continue
        out.append({"name": name, "approved": bool(personalise.approved({"industry": name})),
                    "kinds": [k for k, d in TEMPLATE_KINDS.items() if (d / f"{name}.txt").is_file()]})
    return {"templates": out}


@router.get("/api/crm/templates/{kind}/{name}")
def get_template(kind: str, name: str, admin=Depends(admin_only)):
    subject, body = _read_template(kind, _template_path(kind, name))
    with conn() as c:
        names = _user_names(c)
        history = [{"id": r["id"], "at": iso(r["saved_at"]), "who": names.get(r["user_id"], "Someone")}
                   for r in c.execute("SELECT id, user_id, saved_at FROM template_history WHERE kind = ? AND name = ?"
                                      " ORDER BY id DESC LIMIT 10", (kind, name))]
    return {"kind": kind, "name": name, "subject": subject, "body": body, "fields": sorted(_fields(kind)),
            "history": history}


@router.post("/api/crm/templates/{kind}/{name}/preview")
def preview_template(kind: str, name: str, body: TemplateIn, admin=Depends(admin_only)):
    _template_path(kind, name)
    subject = (body.subject or "").strip() if kind == "first" else None
    problem = _template_problem(kind, name, subject, body.body)
    if problem:
        return {"problem": problem}
    return {"problem": None, **_render(kind, name, subject, body.body)}


def _save_template(kind, name, subject, body, user):
    path = _template_path(kind, name)
    problem = _template_problem(kind, name, subject, body)
    if problem:
        raise HTTPException(400, problem)
    old_subject, old_body = _read_template(kind, path)
    if (old_subject, old_body) == (subject, body.strip()):
        return
    with conn() as c:
        c.execute("INSERT INTO template_history (kind, name, subject, body, user_id) VALUES (?,?,?,?,?)",
                  (kind, name, old_subject, old_body, user["id"]))
        _write_template(kind, path, subject, body)
        log_event(c, None, user["id"], "template_saved", f"{kind}/{name}")


@router.post("/api/crm/templates/{kind}/{name}")
def save_template(kind: str, name: str, body: TemplateIn, admin=Depends(admin_only)):
    _save_template(kind, name, (body.subject or "").strip() if kind == "first" else None, body.body, admin)
    return {"ok": True}


@router.post("/api/crm/templates/{kind}/{name}/restore/{history_id}")
def restore_template(kind: str, name: str, history_id: int, admin=Depends(admin_only)):
    with conn() as c:
        row = c.execute("SELECT subject, body FROM template_history WHERE id = ? AND kind = ? AND name = ?",
                        (history_id, kind, name)).fetchone()
    if not row:
        raise HTTPException(404, "That version wasn't found.")
    _save_template(kind, name, row["subject"], row["body"], admin)  # the current text is kept in history too
    return {"ok": True}


# ---------------------------------------------------------------- sent with every email (admin)
# The profile link and the attached PDF. Saved in the settings table (db.set_setting), which the
# bot reads for every email it builds; until an admin saves one here, .env decides.

ATTACH_DIR = HERE / "attachments"   # PDFs uploaded here
MAX_PDF_UPLOAD = 5 * 1024 * 1024
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")


def _settings_out():
    import db
    import sender
    url, label = sender.profile_url(), sender.setting("profile_label", sender.PROFILE_LABEL)
    pdf = sender.attachment()
    return {
        "profile_url": url, "profile_label": label, "profile_line": sender.profile_line(),
        # where each value comes from, so it is clear whether .env still decides
        "profile_from": "crm" if db.get_setting("profile_url") is not None else ".env",
        "attachment": {"name": pdf.name, "size": pdf.stat().st_size} if pdf else None,
        "attachment_from": "crm" if db.get_setting("attach_pdf") is not None else ".env",
        "max_upload": MAX_PDF_UPLOAD,
    }


class ProfileIn(BaseModel):
    profile_url: str = ""
    profile_label: str = ""


class PdfIn(BaseModel):
    filename: str
    data: str   # the file, base64


@router.get("/api/crm/settings")
def get_settings(admin=Depends(admin_only)):
    return _settings_out()


@router.post("/api/crm/settings/profile")
def save_profile(body: ProfileIn, admin=Depends(admin_only)):
    import db
    url, label = body.profile_url.strip(), body.profile_label.strip() or "Company profile"
    if url and (not re.match(r"^https?://[^\s<>\"']+$", url) or len(url) > 300):
        raise HTTPException(400, "The link must start with http:// or https:// and have no spaces.")
    if len(label) > 40 or "\n" in label or "{" in label or "}" in label:
        raise HTTPException(400, "Keep the words before the link short (under 40 characters), on one line.")
    db.set_setting("profile_url", url, admin["id"])  # empty means: no link, whatever .env says
    db.set_setting("profile_label", label, admin["id"])
    with conn() as c:
        log_event(c, None, admin["id"], "profile_link_saved", url or "(none)")
    return _settings_out()


@router.post("/api/crm/settings/attachment")
def upload_pdf(body: PdfIn, admin=Depends(admin_only)):
    import db
    try:
        data = base64.b64decode(body.data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "The file didn't arrive in one piece. Try again.")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "That isn't a PDF. Choose a .pdf file.")
    if len(data) > MAX_PDF_UPLOAD:
        raise HTTPException(400, f"Keep the PDF under {MAX_PDF_UPLOAD // (1024 * 1024)} MB (under 1 MB is best, "
                                 "large attachments land in spam more often).")
    # the name people see on the attachment; never a path, so it can't be written anywhere else
    name = SAFE_FILENAME.sub("", Path(body.filename.replace("\\", "/")).name).strip(" .")[:80]
    name = (name[:-4] if name.lower().endswith(".pdf") else name).strip(" .") or "TalkSasa_profile"
    path = ATTACH_DIR / f"{name}.pdf"
    ATTACH_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    try:  # inside the project folder it is saved relative to it, like ATTACH_PDF in .env
        stored = path.relative_to(HERE).as_posix()
    except ValueError:
        stored = str(path)
    db.set_setting("attach_pdf", stored, admin["id"])
    with conn() as c:
        log_event(c, None, admin["id"], "attachment_uploaded", f"{path.name} ({len(data):,} bytes)")
    return _settings_out()


@router.post("/api/crm/settings/attachment/remove")
def remove_pdf(admin=Depends(admin_only)):
    import db
    db.set_setting("attach_pdf", "", admin["id"])  # empty means: attach nothing, whatever .env says
    with conn() as c:
        log_event(c, None, admin["id"], "attachment_removed")
    return _settings_out()


@router.get("/api/crm/settings/attachment")
def view_pdf(admin=Depends(admin_only)):
    import sender
    pdf = sender.attachment()
    if not pdf:
        raise HTTPException(404, "No PDF is attached to the emails.")
    return FileResponse(pdf, media_type="application/pdf", filename=pdf.name, content_disposition_type="inline")


# ---------------------------------------------------------------- command line

def _ask_password():
    while True:
        pw = getpass.getpass("Password (at least %d characters, hidden as you type): " % MIN_PASSWORD)
        problem = password_problem(pw)
        if problem:
            print(problem)
            continue
        if getpass.getpass("Type it again: ") != pw:
            print("They don't match. Try again.")
            continue
        return pw


def _cli():
    ensure_schema()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "create-admin":
        name = input("Your name: ").strip()
        email = input("Your email: ").strip().lower()
        if not name or not EMAIL_RE.match(email):
            sys.exit("A name and a valid email are needed.")
        pw = _ask_password()
        with conn() as c:
            try:
                c.execute("INSERT INTO users (name, email, role, password_hash, must_change) VALUES (?,?,?,?,0)",
                          (name, email, "admin", hash_password(pw)))
            except db.IntegrityError:
                sys.exit("That email already has an account. Use: python crm.py reset-password " + email)
        print(f"Admin account created for {email}. Open http://127.0.0.1:8000/login to sign in.")
    elif cmd == "reset-password" and len(sys.argv) > 2:
        email = sys.argv[2].strip().lower()
        pw = _ask_password()
        with conn() as c:
            cur = c.execute("UPDATE users SET password_hash = ?, must_change = 0, active = 1 WHERE email = ?",
                            (hash_password(pw), email))
            if cur.rowcount == 0:
                sys.exit("No account with that email.")
            uid = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
            c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        print("Password updated. Any old sign-ins for that account were ended.")
    elif cmd == "list-users":
        with conn() as c:
            for r in c.execute("SELECT name, email, role, active FROM users ORDER BY role, name"):
                print(f"{r['role']:6}  {'active' if r['active'] else 'off   '}  {r['name']}  <{r['email']}>")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
