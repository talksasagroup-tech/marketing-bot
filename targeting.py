"""Adds leads from published directories and scores how much each lead needs SMS.
Nothing here sends email.

  python targeting.py import-lenders [PDF path or URL] [--source NAME] [--source-url URL]
      every digital credit provider in the Central Bank of Kenya's directory becomes a lead
      in industry digital_lender. Without a path the September 2026 directory is downloaded.
      A lead whose email, company name or own email domain is already in leads is skipped. Each new address
      gets the same check as `main.py verify` (MX record), so it ends up verified or invalid.
  python targeting.py import-microfinance [PDF path or URL] [--source NAME] [--source-url URL]
      the same for the CBK directory of licensed microfinance banks (industry microfinance).
  python targeting.py import-names FILE --industry NAME --source NAME [--max N]
      for registers that list names only (SASRA's licensed SACCOs, IRA's licensed insurers,
      both published in the Kenya Gazette): FILE has one organisation per line. Each name not
      already a lead is searched for on the web, and its website goes through the same checks
      as `main.py discover`. Leads found this way wait in the CRM's To review queue, where a
      person checks the website really is that organisation's.
  python targeting.py swap-emails
      for imported leads whose address is a legal@, compliance@, report..@, accounts@ or
      finance@ inbox or a person's: looks on their website for info@, hello@, sales@ or
      contact@ on the same domain and uses that instead. The directory's address is kept
      in source_email. import-lenders does this too.
  python targeting.py score
      gives every lead a need score from 0 to 100, with its reasons (rules below, no AI).
      Each lead's homepage and about page are read for the words the rules look for.
  python targeting.py report [--md FILE]
      what the last import found, added and skipped, the first 20 leads it added and the
      30 leads with the highest need score, the emails swapped and the websites that
      couldn't be read; --md also writes it as a Markdown file

The sender emails the highest need score first and leaves leads below MIN_SCORE (.env)
waiting. A lead in an industry that uses.yaml doesn't approve waits until it is approved.
"""
import io
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import config
import db
import personalise
import ranking
import scraper
import verify
from ranking import FREE_MAIL

CBK_DCP_URL = ("https://www.centralbank.go.ke/wp-content/uploads/2026/09/"
               "Directory-of-Digital-Credit-Providers-September-2026.pdf")
CBK_DCP_SOURCE = "CBK DCP directory Sep 2026"
INDUSTRY = "digital_lender"
CBK_MFB_URL = ("https://www.centralbank.go.ke/wp-content/uploads/2026/02/"
               "Directory-of-Licenced-Microfinance-Banks-Feb-2026.pdf")
CBK_MFB_SOURCE = "CBK microfinance bank directory Feb 2026"
# A lead from a regulator's register is a real, licensed organisation of the kind we sell to
REGISTERED = 10

# Page headers and footers repeated on every page of a CBK directory, and page numbers
PAGE_JUNK = re.compile(r"^(?:CENTRAL BANK OF KENYA|(?:NO\.\s+)?DIRECTORY OF .*|"
                       r"UPDATED ON .*|C2: CBK - Official|Page \d+ of \d+|\d+)$")
ENTRY = re.compile(r"^(\d+)\.\s+(.+)$")
# The label each line starts with. Spellings vary ("E-mail address:", "Telephone No:",
# "Physical Location:", "Telephone+254..." with no colon); a line with no label continues
# the one before it.
LABELS = [
    ("postal", re.compile(r"^Postal\s+Address\s*:", re.I)),
    ("phone", re.compile(r"^Telephone(?:\s+No\.?|\s+Number)?\s*:?\s*-?", re.I)),
    ("email", re.compile(r"^(?:Official\s+)?E-?mail(?:\s+address)?\s*:", re.I)),
    ("address", re.compile(r"^Physical\s+(?:Address|Location)\s*:", re.I)),
    ("licensed", re.compile(r"^(?:Date\s+)?Licen[sc]ed\s*:", re.I)),
    ("website", re.compile(r"^Website\s*:", re.I)),
    ("other", re.compile(r"^(?:L\.R\s*NO|Fax|Branches)\s*:", re.I)),
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Words left out when comparing company names, so "Zenka Digital Ltd" matches "Zenka Digital Limited"
LEGAL_WORDS = {"limited", "ltd", "plc", "inc", "llc", "company", "co", "kenya", "k", "ke", "the"}
TRADING = re.compile(r"\(?\s*\b(?:trading\s+(?:as|under)|t/a)\s+([^)]+)\)?", re.I)

RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY,
    source TEXT,
    source_url TEXT,
    industry TEXT,
    run_at TEXT DEFAULT CURRENT_TIMESTAMP,
    found INTEGER,
    -- JSON: added (lead ids), duplicates, missing_email, suppressed, invalid (lead ids)
    result TEXT
);
"""


def runs_table(c):
    c.executescript(RUNS_SCHEMA)
    if "industry" not in db.columns(c, "import_runs"):  # made before other directories were imported
        c.execute("ALTER TABLE import_runs ADD COLUMN industry TEXT")


def run_industry(run):
    """The industry a run's leads went into; runs from before this was recorded were all lenders."""
    return (run["industry"] if "industry" in run.keys() else None) or INDUSTRY


def pdf_text(data):
    """All the PDF's text, page after page. Space characters in the PDF itself are ignored
    and spaces are put back from the gaps between letters: the directory has stray spaces
    drawn over the first letter of many names, which otherwise come out as "Z enka"."""
    import pdfplumber
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            page = page.filter(lambda o: not (o.get("object_type") == "char" and o["text"].isspace()))
            pages.append(page.extract_text(x_tolerance=1.5) or "")
    return "\n".join(pages)


def parse_directory(text):
    """The directory's entries in order, as dicts of the fields found in each. An entry starts
    at the next number in sequence ("12. Amaze Credit Limited"), so a numbered line inside
    an address can't start one."""
    entries, current, field = [], None, None
    for line in text.splitlines():
        line = " ".join(line.split())
        # a label run onto the end of the line before it ("Nairobi, KenyaDate Licensed: ...")
        for part in re.split(r"(?=Date\s+Licen[sc]ed\s*:)", line):
            if not part or PAGE_JUNK.match(part):
                continue
            m = ENTRY.match(part)
            if m and int(m.group(1)) == len(entries) + 1:
                current = {"number": int(m.group(1)), "name": m.group(2)}
                entries.append(current)
                field = "name"
                continue
            if current is None:
                continue
            for name, label in LABELS:
                found = label.match(part)
                if found:
                    field = name
                    part = part[found.end():]
                    break
            current[field] = " ".join(filter(None, [current.get(field), part.strip()]))
    return [clean(e) for e in entries]


def clean(entry):
    emails = list(dict.fromkeys(e.lower() for e in EMAIL.findall(entry.get("email", ""))))
    address = re.sub(r"^Physical\s+Address\s*[.:]\s*", "", entry.get("address", ""), flags=re.I)
    phone = entry.get("phone", "").strip(" :-–")
    # "+254 - 020 - 3877290/3/7; 0711 - 074074": the best number in the list, else the text as it is
    numbers = [n for n in map(scraper.phone_number, re.split(r"[,/;|]", phone)) if n]
    if numbers:
        phone = scraper.pick_phone(numbers)
    elif phone.startswith("254"):
        phone = "+" + phone
    email = pick_email(emails)
    listed = (entry.get("website") or "").strip(" ;,")
    return {
        "number": entry["number"],
        "name": entry["name"].strip(" .,"),
        "emails": emails,
        "email": email,
        "website": (ranking.urlparse_domain(listed) if listed else None) or website_of(email),
        "phone": phone or None,
        "address": address.strip() or None,
        "licensed": iso_date(entry.get("licensed", "")),
    }


def domain_of(email):
    return email.rsplit("@", 1)[-1]


def own_domains(emails, website):
    """The company's own domains: its website's and its emails', leaving out free mail."""
    found = {domain_of(e.lower()) for e in emails if e}
    if website:
        found.add(ranking.urlparse_domain(website))
    return found - FREE_MAIL


def pick_email(emails):
    """The first address on the company's own domain, else the first one listed."""
    business = [e for e in emails if domain_of(e) not in FREE_MAIL]
    return (business or emails or [None])[0]


def website_of(email):
    if email and domain_of(email) not in FREE_MAIL:
        return domain_of(email)
    return None


def iso_date(text):
    """'April 10, 2026' as '2026-04-10'. Text that isn't a date is kept as it is."""
    text = text.strip(" .")
    for fmt in ("%B %d, %Y", "%B %d %Y", "%d %B %Y", "%d %B, %Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return text or None


def name_keys(name):
    """Normalised forms of a company name to match on: the legal name and, if there is
    one, the trading name ("Inventure Mobile Limited (Trading as Tala)" gives both)."""
    def norm(n):
        words = re.findall(r"[a-z0-9]+", n.lower().replace("&", " and "))
        return " ".join(w for w in words if w not in LEGAL_WORDS)
    keys = {norm(TRADING.split(name)[0])}
    keys |= {norm(t) for t in TRADING.findall(name)}
    return keys - {""}


def fetch(src):
    if re.match(r"https?://", src):
        r = requests.get(src, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.content
    else:
        with open(src, "rb") as f:
            data = f.read()
    if not data.startswith(b"%PDF"):
        raise SystemExit(f"{src} is not a PDF.")
    return data


def import_lenders(src=CBK_DCP_URL, source=CBK_DCP_SOURCE, source_url=CBK_DCP_URL):
    import_directory(src, INDUSTRY, source, source_url)


def import_microfinance(src=CBK_MFB_URL, source=CBK_MFB_SOURCE, source_url=CBK_MFB_URL):
    import_directory(src, "microfinance", source, source_url)


def import_directory(src, industry, source, source_url):
    """Every entry in a CBK directory PDF becomes a lead in `industry` (see import-lenders)."""
    entries = parse_directory(pdf_text(fetch(src)))
    if not entries:
        raise SystemExit(f"No entries found in {src}.")
    import_entries(entries, industry, source, source_url)


def import_entries(entries, industry, source, source_url):
    """Add directory entries (parse_directory's dicts) as leads: skip the ones already leads
    or opted out, check each new address's domain takes mail, and record the run."""
    db.init()
    added, duplicates, missing, suppressed = [], [], [], []
    with db.conn() as c:
        runs_table(c)
        by_email, by_name, by_domain = {}, {}, {}
        for lead in c.execute("SELECT id, company, email, website FROM leads"):
            if lead["email"]:
                by_email[lead["email"].lower()] = lead["company"]
            for key in name_keys(lead["company"] or ""):
                by_name.setdefault(key, lead["company"])
            for d in own_domains([lead["email"]], lead["website"]):
                by_domain.setdefault(d, lead["company"])
        for e in entries:
            row = {"number": e["number"], "name": e["name"], "email": e["email"], "phone": e["phone"]}
            if not e["email"]:
                missing.append(row)
                continue
            same_email = next((x for x in e["emails"] if x in by_email), None)
            same_name = next((k for k in name_keys(e["name"]) if k in by_name), None)
            # "4G Capital" at contact@4g-capital.com is "Fourth Generation Capital Limited" here
            same_domain = next((d for d in own_domains(e["emails"], e["website"]) if d in by_domain), None)
            if same_email or same_name or same_domain:
                why = (f"email {same_email} is {by_email[same_email]}'s" if same_email
                       else f"same company as {by_name[same_name]}" if same_name
                       else f"{same_domain} is {by_domain[same_domain]}'s domain")
                duplicates.append({**row, "reason": why})
                continue
            if db._blocked(c, e["email"]):  # opted out, itself or its whole organisation
                suppressed.append(row)
                continue
            lead_id = c.execute(
                "INSERT INTO leads (company, website, industry, email, phone, physical_address,"
                " date_licensed, source, source_url, discovered_at, imported_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) RETURNING id",
                (e["name"], e["website"], industry, e["email"], e["phone"], e["address"],
                 e["licensed"], source, source_url)).fetchone()[0]
            db.log_event(c, lead_id, "discovered", source=source, url=source_url)
            added.append(lead_id)
            by_email[e["email"]] = e["name"]
            for key in name_keys(e["name"]):
                by_name[key] = e["name"]
            for d in own_domains(e["emails"], e["website"]):
                by_domain.setdefault(d, e["name"])
    # The same check as `main.py verify`: the address looks right and its domain takes mail
    invalid = []
    for lead_id in added:
        with db.conn() as c:
            lead = c.execute("SELECT email FROM leads WHERE id=?", (lead_id,)).fetchone()
        email = lead["email"]
        ok = bool(verify.SYNTAX.match(email)) and verify.domain_ok(domain_of(email))
        if ok is None:  # DNS didn't answer: left new, so `main.py verify` checks it again
            continue
        db.set_status(lead_id, "verified" if ok else "invalid", "verify")
        if not ok:
            invalid.append(lead_id)
    result = {"added": added, "duplicates": duplicates, "missing_email": missing,
              "suppressed": suppressed, "invalid": invalid}
    with db.conn() as c:
        c.execute("INSERT INTO import_runs (source, source_url, industry, found, result) VALUES (?,?,?,?,?)",
                  (source, source_url, industry, len(entries), json.dumps(result, ensure_ascii=False)))
    print(f"Found {len(entries)}, added {len(added)} ({len(invalid)} invalid), "
          f"skipped {len(duplicates)} duplicates, {len(missing)} with no email"
          + (f", {len(suppressed)} opted out" if suppressed else "") + ".")
    swap_emails()


def read_names(path):
    """The organisations in a names file: one per line, any "12." numbering and blank lines ignored."""
    with open(path, encoding="utf-8-sig") as f:
        lines = [re.sub(r"^\s*\d+[.)]?\s+", "", line.strip()).strip(" \t.,") for line in f]
    return list(dict.fromkeys(n for n in lines if n))


def import_names(path, industry, source, limit=None):
    """Look up each named organisation on the web and add the ones found (see import-names).
    Returns (added rows, names not found)."""
    import discover
    import websearch
    db.init()
    names = read_names(path)
    # "Mwalimu National Sacco" is "Mwalimu National Sacco Society Ltd" on the register
    with db.conn() as c:
        known = {discover.name_words(company) for (company,) in c.execute("SELECT company FROM leads")} - {frozenset()}
    seen = discover.known_domains()
    added, not_found, already = [], [], 0
    for name in names:
        if limit and len(added) >= limit:
            break
        if discover.name_words(name) in known:
            already += 1
            continue
        print(f"  {name}")
        row = None
        for res in websearch.search(f'"{name}" Kenya contact', 5):
            row = discover.consider(res.get("href") or "", industry, f"named in {source}", seen, name=name)
            if row:
                break
        if row:
            added.append(row)
            known.add(discover.name_words(name))
        else:
            not_found.append(name)
    print(f"{len(names)} names: added {len(added)}, {already} already leads, {len(not_found)} not found."
          + (" Check the new ones in the CRM's To review." if added else ""))
    if not_found:
        print("Not found (no website with an email, or none that looks like theirs):\n  " + "\n  ".join(not_found))
    return added, not_found


def last_run():
    """(run, result, added leads) for the last import, or None if nothing was imported."""
    db.init()
    with db.conn() as c:
        runs_table(c)
        run = c.execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
        if not run:
            return None
        result = json.loads(run["result"])
        marks = ",".join("?" * len(result["added"]))
        leads = {r["id"]: dict(r) for r in c.execute(
            f"SELECT * FROM leads WHERE id IN ({marks})", result["added"])} if marks else {}
    return dict(run), result, [leads[i] for i in result["added"] if i in leads]


def report(md_path=None):
    last, top = last_run(), top_scored()
    if not last and not top:
        raise SystemExit("Nothing imported or scored yet: run `python targeting.py import-lenders`"
                         " or `python targeting.py score` first.")
    if last:
        print_import(*last)
    if top:
        print(f"\nTop {len(top)} by need score (MIN_SCORE is {config.MIN_SCORE}):")
        for i, l in enumerate(top, 1):
            print(f"  {i:>2}. {l['score']:>3}  #{l['id']:<4} {(l['company'] or '')[:40]:40} "
                  f"{(l['industry'] or '-'):15} {l['status']:9} {l['email']}")
            print(f"           {'; '.join(l['reasons'])}")
    else:
        print("\nNo lead has a need score yet: run `python targeting.py score`.")
    swapped, kept = contact_status()
    unreadable = unreadable_sites()
    print(f"\nEmails swapped for a general inbox: {len(swapped)}")
    for l in swapped:
        print(f"  #{l['id']:<4} {l['company']}: {l['source_email']} -> {l['email']}")
    print(f"Still on a role or personal address (no general inbox found): {len(kept)}")
    for l, why in kept:
        print(f"  #{l['id']:<4} {l['company']}: {l['email']} ({why})")
    print(f"Websites still unreadable (+10 {UNREADABLE}): {len(unreadable)}")
    if md_path:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown(last, top))
        print(f"\nWritten to {md_path}")


def import_counts(run, result, added):
    counts = [("Found in the directory", run["found"]), ("Added", len(added)),
              ("Skipped as duplicates", len(result["duplicates"])),
              ("Missing email (not added)", len(result["missing_email"])),
              ("Invalid email (added, marked invalid)", len(result["invalid"]))]
    if result["suppressed"]:
        counts.append(("Opted out before (not added)", len(result["suppressed"])))
    return counts


def print_import(run, result, added):
    counts = import_counts(run, result, added)
    first = added[:20]

    print(f"{run['source']}: imported {run['run_at']} UTC into {config.DB_PATH}")
    for label, n in counts:
        print(f"  {label + ':':40} {n}")
    print(f"\nFirst {len(first)} added:")
    for l in first:
        print(f"  #{l['id']:<4} {l['company'][:40]:40} {l['email']:38} {l['status']:9} "
              f"{l['website'] or '-':26} {l['phone'] or '-'}  licensed {l['date_licensed'] or '-'}")
    for title, rows, extra in (("Duplicates", result["duplicates"], "reason"),
                               ("No email", result["missing_email"], "phone")):
        if rows:
            print(f"\n{title}:")
            for r in rows:
                print(f"  {r['number']:>3}. {r['name']} ({r[extra] or '-'})")


def markdown(last, top):
    def cell(v):
        return str(v if v not in (None, "") else "—").replace("|", "\\|")

    def table(headers, rows):
        out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
        out += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
        return "\n".join(out)

    parts = [f"# Targeting report: `{config.DB_PATH}`"]
    if last:
        parts += import_markdown(*last, table)
    if top:
        parts += [f"## Top {len(top)} by need score",
                  f"Highest first. First emails go to leads scoring at least MIN_SCORE "
                  f"({config.MIN_SCORE}), in an approved industry, still waiting to be emailed.",
                  table(["#", "Score", "Lead", "Company", "Industry", "Status", "Email", "Reasons"],
                        [(i, l["score"], f"#{l['id']}", l["company"], l["industry"], l["status"],
                          l["email"], "; ".join(l["reasons"])) for i, l in enumerate(top, 1)])]
    swapped, kept = contact_status()
    unreadable = unreadable_sites()
    parts += ["## Contact emails",
              f"{len(swapped)} directory addresses (legal@, compliance@, report..@, accounts@, "
              f"finance@ or a person's) were swapped for a general inbox on the lead's own website; "
              f"the directory's address is kept in `source_email`. {len(kept)} still have such an "
              f"address because their website shows no info@, hello@, sales@ or contact@."]
    if swapped:
        parts.append(table(["Lead", "Company", "Directory email", "Now"],
                           [(f"#{l['id']}", l["company"], l["source_email"], l["email"]) for l in swapped]))
    if kept:
        parts += ["### Not swapped",
                  table(["Lead", "Company", "Email", "Why it was a candidate"],
                        [(f"#{l['id']}", l["company"], l["email"], why) for l, why in kept])]
    parts += [f"## Websites still unreadable: {len(unreadable)}",
              f"Tried as LeadBot, then as a normal browser with a {BROWSER_TIMEOUT}s wait. "
              f"Each scores +10 ({UNREADABLE}) instead of the points its site might have given."]
    if unreadable:
        parts.append(table(["Lead", "Company", "Website", "Score"],
                           [(f"#{l['id']}", l["company"], l["website"], l["score"]) for l in unreadable]))
    return "\n\n".join(parts) + "\n"


def import_markdown(run, result, added, table):
    counts = import_counts(run, result, added)
    by_id = {l["id"]: l for l in added}
    parts = [
        f"## Directory import: {run['source']}",
        f"Imported {run['run_at']} UTC into `{config.DB_PATH}` from {run['source_url']}.\n\n"
        f"Every lead is in industry `{run_industry(run)}`.",
        table(["", "Count"], counts),
        f"### First {min(20, len(added))} added",
        table(["Lead", "Company", "Email", "Status", "Website", "Phone", "Licensed"],
              [(f"#{l['id']}", l["company"], l["email"], l["status"], l["website"], l["phone"],
                l["date_licensed"]) for l in added[:20]]),
    ]
    if result["invalid"]:
        parts += ["### Invalid email (domain takes no mail)",
                  table(["Lead", "Company", "Email"],
                        [(f"#{i}", by_id[i]["company"], by_id[i]["email"])
                         for i in result["invalid"] if i in by_id])]
    if result["duplicates"]:
        parts += ["### Skipped as duplicates",
                  table(["No.", "Company", "Email", "Why"],
                        [(r["number"], r["name"], r["email"], r["reason"]) for r in result["duplicates"]])]
    if result["missing_email"]:
        parts += ["### Missing email (not added)",
                  table(["No.", "Company", "Phone"],
                        [(r["number"], r["name"], r["phone"]) for r in result["missing_email"]])]
    if result["suppressed"]:
        parts += ["### Opted out before (not added)",
                  table(["No.", "Company", "Email"],
                        [(r["number"], r["name"], r["email"]) for r in result["suppressed"]])]
    return parts


# --- Websites ---
# Some sites turn away LeadBot's own user-agent (scraper.py) or answer slowly: they are
# tried again as a normal browser with a longer wait.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
BROWSER_TIMEOUT = 15  # seconds
_browser_pages = {}


def browser_fetch(url):
    """One page as parsed HTML, fetched as a normal browser would, or None."""
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=BROWSER_TIMEOUT)
    except requests.RequestException:
        return None
    return BeautifulSoup(r.text, "html.parser") if r.status_code == 200 else None


def site_url(website):
    return website if website.startswith("http") else "https://" + website


def browser_pages(website, paths=scraper.ABOUT_PATHS):
    """[(path, parsed page)] for the paths that load, as a browser."""
    key = (website, tuple(paths))
    if key not in _browser_pages:
        pages = [(path, browser_fetch(urljoin(site_url(website), path))) for path in paths]
        _browser_pages[key] = [(path, soup) for path, soup in pages if soup is not None]
    return _browser_pages[key]


def readable(soup):
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def site_text(website):
    """The readable text of a lead's homepage and about page, or None if there is no
    website or it can't be read, even as a browser."""
    if not website:
        return None
    pages = scraper.site_pages(website) or [readable(soup) for _, soup in browser_pages(website)]
    pages = [p for p in pages if p]
    return " ".join(pages) if pages else None


# --- Contact emails ---
# Directory addresses that reach the wrong desk (or one person) are swapped for a general
# inbox on the same domain, if the lead's website shows one.
SWAP_ROLES = ("legal", "compliance", "report", "accounts", "finance")
GENERAL_INBOXES = ("info", "hello", "sales", "contact")  # in order of preference
# Local parts that are an inbox, not a person, whatever else they contain
ROLE_WORDS = ("info", "hello", "sales", "contact", "support", "customer", "care", "admin",
              "help", "request", "enquir", "inquir", "office", "team", "service", "talk",
              "tell", "ceo", "md", "managing", "director", "partner", "business", "kenya",
              "sema", "loan", "credit", "cx", "mail", "marketing", "operations", "apply",
              "feedback")


def swap_reason(email, company):
    """Why this address should be swapped for a general inbox, or None."""
    local, domain = email.lower().split("@", 1)
    role = next((r for r in SWAP_ROLES if local.startswith(r)), None)
    if role:
        return f"{local}@ is a role inbox ({role})"
    if domain in FREE_MAIL or local.startswith(ROLE_WORDS):
        return None
    # A person's name: letters only (first.last allowed), not the company's or domain's name
    own_words = {w for w in re.findall(r"[a-z]+", f"{company} {domain}".lower())
                 if len(w) >= 4 and w not in LEGAL_WORDS}
    if re.fullmatch(r"[a-z]+(?:[._-][a-z]+)*", local) and not any(w in local for w in own_words):
        return f"{local}@ looks like a person's address"
    return None


def site_emails(website):
    """Addresses shown on the lead's website (contact and about pages), on its own domain."""
    domain = ranking.urlparse_domain(website)
    found = []
    for _, soup in browser_pages(website, scraper.CONTACT_PATHS):
        links = [a["href"][7:].split("?")[0] for a in soup.select('a[href^="mailto:"]')]
        for e in links + scraper.EMAIL_RE.findall(soup.get_text(" ")):
            e = e.lower().strip(".")
            if e.endswith("@" + domain) and e not in found:
                found.append(e)
    return found


def general_inbox(emails):
    for name in GENERAL_INBOXES:
        if f"{name}@" in [e.split("@")[0] + "@" for e in emails]:
            return next(e for e in emails if e.startswith(f"{name}@"))
    return None


def swap_emails():
    """Swap the address of each imported lead that swap_reason picks out, when its website
    shows a general inbox on the same domain. Returns how many were swapped."""
    db.init()
    with db.conn() as c:
        leads = [dict(l) for l in c.execute(
            "SELECT * FROM leads WHERE imported_at IS NOT NULL AND source_email IS NULL"
            " AND status != 'invalid' ORDER BY id")]
    leads = [l for l in leads if l["website"] and swap_reason(l["email"], l["company"])]
    if not leads:
        return 0
    print(f"Looking for a general inbox on {len(leads)} websites...")
    with ThreadPoolExecutor(SITE_WORKERS) as pool:
        found = list(pool.map(lambda l: general_inbox(site_emails(l["website"])), leads))
    swapped = 0
    with db.conn() as c:
        for lead, new in zip(leads, found):
            if not new or new == lead["email"]:
                print(f"  {lead['company']}: kept {lead['email']} (no general inbox on the site)")
                continue
            if c.execute("SELECT 1 FROM leads WHERE email=? UNION SELECT 1 FROM suppression WHERE email=?",
                         (new, new)).fetchone():
                print(f"  {lead['company']}: kept {lead['email']} ({new} is already a lead or opted out)")
                continue
            c.execute("UPDATE leads SET source_email=email, email=? WHERE id=?", (new, lead["id"]))
            db.log_event(c, lead["id"], "email_changed", old=lead["email"], new=new,
                         why=swap_reason(lead["email"], lead["company"]), by="targeting")
            print(f"  {lead['company']}: {lead['email']} -> {new}")
            swapped += 1
    return swapped


def contact_status():
    """(swapped, kept): imported leads whose email was swapped, and those that still
    have an address swap_reason picks out, each as (lead, reason)."""
    with db.conn() as c:
        leads = [dict(l) for l in c.execute("SELECT * FROM leads WHERE imported_at IS NOT NULL ORDER BY id")]
    swapped = [l for l in leads if l["source_email"]]
    kept = [(l, swap_reason(l["email"], l["company"])) for l in leads if not l["source_email"]]
    return swapped, [(l, why) for l, why in kept if why]


def unreadable_sites():
    with db.conn() as c:
        return [dict(l) for l in c.execute(
            "SELECT * FROM leads WHERE score_reasons LIKE ? ORDER BY id", (f'%{UNREADABLE}%',))]


# --- Need score ---
# 0-100 from fixed rules. Digital lenders start at 50; every other industry at 40 and gains
# 10 for each sign on its website that it messages many people, and 10 for being on a
# regulator's register (imported from a CBK directory). A website that can't be read gives
# 10 instead of what reading it might have.
SCORE_VERSION = 4  # 1 was ranking.py's old points; raise this when the rules below change
LENDER_START, OTHER_START = 50, 40
SITE_WORKERS = 8  # websites read at the same time
# A website that can't be read even as a browser: it may still be a busy lender's, so it
# isn't held against the lead
UNREADABLE = "couldn't check website"
APP = re.compile(r"\b(?:google play|play\s?store|app\s?store|mobile app|app)\b", re.I)
PORTAL = re.compile(r"\bportal\b", re.I)
MPESA = re.compile(r"\bm-?\s?pesa\b|\bpay\s?bill\b", re.I)
SMS_ALERTS = re.compile(r"\bsms\s*(?:alerts?|notifications?)\b", re.I)
BRANCHES = re.compile(r"\bbranches\b", re.I)
# "10,000+ customers", "over 2 million members", "500 students": three digits or more
COUNT = re.compile(r"\b(?:\d{1,3}(?:,\d{3})+|\d{3,}|\d+(?:\.\d+)?\s*(?:million|thousand|k|m))\+?"
                   r"\s+(?:[a-z]+\s+){0,2}?(?:members|students|customers)\b", re.I)


def _seen(pattern, text):
    m = pattern.search(text or "")
    return f' ("{m.group(0)}")' if m else None


def need_score(lead, text):
    """(score, reasons) for one lead. `text` is site_text for its website."""
    lead = dict(lead)
    email = (lead.get("email") or "").lower()
    website = ranking.urlparse_domain(lead["website"]) if lead.get("website") else None
    points = []
    if lead.get("industry") == INDUSTRY:
        points.append((LENDER_START, "start (digital lender)"))
        if website and website not in FREE_MAIL:
            points.append((15, f"own website domain ({website})"))
        if (seen := _seen(APP, text)):
            points.append((15, "site mentions an app or Play Store" + seen))
        if (seen := _seen(MPESA, text)):
            points.append((10, "site mentions M-Pesa or paybill" + seen))
        licensed = lead.get("date_licensed") or ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", licensed) and date.fromisoformat(licensed) >= two_years_ago():
            points.append((10, f"licensed within the last 2 years ({licensed})"))
        if domain_of(email) in FREE_MAIL:
            points.append((-20, f"email is free mail ({domain_of(email)})"))
        if lead.get("status") == "invalid":
            points.append((-30, "email invalid"))
    else:
        points.append((OTHER_START, f"start ({lead.get('industry') or 'no industry'})"))
        if lead.get("imported_at"):
            points.append((REGISTERED, f"on a regulator's register ({lead.get('source') or 'directory'})"))
        for pattern, why in ((SMS_ALERTS, "SMS alerts"), (MPESA, "M-Pesa or paybill"),
                             (BRANCHES, "branches"), (COUNT, "how many members, students or customers"),
                             (APP, "an app"), (PORTAL, "a portal")):
            if why == "a portal" and _seen(APP, text):
                continue  # app or portal: 10 points once
            if (seen := _seen(pattern, text)):
                points.append((10, f"site mentions {why}{seen}"))
    if text is None:
        points.append((10, UNREADABLE) if website else (0, "no website to read"))
    total = max(0, min(100, sum(n for n, _ in points)))
    return total, [f"{n:+d} {why}" if n else why for n, why in points]


def two_years_ago():
    today = date.today()
    try:
        return today.replace(year=today.year - 2)
    except ValueError:  # 29 February
        return today.replace(year=today.year - 2, day=28)


def score_leads(leads, quiet=False):
    """Work out and save the need score of each lead. Returns how many were scored."""
    leads = [dict(l) for l in leads]
    with ThreadPoolExecutor(SITE_WORKERS) as pool:
        texts = list(pool.map(lambda l: site_text(l.get("website")), leads))
    with db.conn() as c:
        for lead, text in zip(leads, texts):
            total, reasons = need_score(lead, text)
            c.execute("UPDATE leads SET score=?, score_reasons=?, score_version=?,"
                      " scored_at=CURRENT_TIMESTAMP WHERE id=?",
                      (total, json.dumps(reasons, ensure_ascii=False), SCORE_VERSION, lead["id"]))
            if not quiet:
                print(f"  {total:>3}  {lead['company'] or lead['email']}")
    return len(leads)


def score_all():
    db.init()
    with db.conn() as c:
        leads = c.execute("SELECT * FROM leads ORDER BY id").fetchall()
    print(f"Scoring {len(leads)} leads (reading their websites)...")
    score_leads(leads)
    print(f"Scored {len(leads)} leads.")


def score_unscored():
    """Score the leads waiting for a first email in an approved industry that have no
    need score yet, or one from older rules. The sender runs this before choosing leads."""
    leads = [l for l in ranking.waiting() if personalise.approved(l)
             and not (l["scored_at"] and l["score_version"] == SCORE_VERSION)]
    if leads:
        print(f"Scoring {len(leads)} waiting leads (reading their websites)...")
        score_leads(leads, quiet=True)
    return len(leads)


def top_scored(n=30):
    with db.conn() as c:
        rows = c.execute("SELECT * FROM leads WHERE scored_at IS NOT NULL"
                         " ORDER BY score DESC, id LIMIT ?", (n,)).fetchall()
    return [dict(r, reasons=json.loads(r["score_reasons"] or "[]")) for r in rows]


def take_option(args, flag):
    """Remove `flag VALUE` from args and return VALUE, or None if the flag isn't there."""
    if flag not in args:
        return None
    i = args.index(flag)
    if i + 1 >= len(args):
        raise SystemExit(f"{flag} needs a value.")
    value = args[i + 1]
    del args[i:i + 2]
    return value


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args.pop(0) if args else ""
    if cmd == "import-lenders":
        source = take_option(args, "--source") or CBK_DCP_SOURCE
        source_url = take_option(args, "--source-url") or CBK_DCP_URL
        import_lenders(args[0] if args else CBK_DCP_URL, source, source_url)
    elif cmd == "import-microfinance":
        source = take_option(args, "--source") or CBK_MFB_SOURCE
        source_url = take_option(args, "--source-url") or CBK_MFB_URL
        import_microfinance(args[0] if args else CBK_MFB_URL, source, source_url)
    elif cmd == "import-names":
        industry, source, limit = take_option(args, "--industry"), take_option(args, "--source"), take_option(args, "--max")
        if not args or not industry or not source:
            raise SystemExit("Usage: python targeting.py import-names FILE --industry sacco --source \"SASRA 2026\"")
        import_names(args[0], industry, source, int(limit) if limit else None)
    elif cmd == "swap-emails":
        print(f"Swapped {swap_emails()} emails.")
    elif cmd == "score":
        score_all()
    elif cmd == "report":
        report(take_option(args, "--md"))
    else:
        print(__doc__)
