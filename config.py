"""All settings come from the .env file. Copy .env.example to .env and fill it in."""
import os
from datetime import timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

def env(key, default=None):
    return os.getenv(key, default)

# Database: PostgreSQL when DATABASE_URL is set (postgresql://user:password@host:5432/name),
# otherwise the SQLite file DB_PATH
DATABASE_URL = env("DATABASE_URL", "")
DB_PATH = env("DB_PATH", "leads.db")

# Sending (SMTP)
SMTP_HOST = env("SMTP_HOST")
SMTP_PORT = int(env("SMTP_PORT", "587"))
SMTP_USER = env("SMTP_USER")
SMTP_PASS = env("SMTP_PASS")
FROM_NAME = env("FROM_NAME", "TalkSasa")
FROM_EMAIL = env("FROM_EMAIL", SMTP_USER)
# The day every count is kept in ("today" for the daily limit, follow-up timing, charts)
NAIROBI = timezone(timedelta(hours=3))  # Kenya has no daylight saving
DAILY_LIMIT = int(env("DAILY_LIMIT", "10"))         # start low, raise slowly
# DAILY_LIMIT is held to MAX_DAILY_LIMIT unless DAILY_LIMIT_OVERRIDE=true, so a typo or a
# forgotten one-off change in .env can't raise the agreed volume
MAX_DAILY_LIMIT = 10
DAILY_LIMIT_OVERRIDE = env("DAILY_LIMIT_OVERRIDE", "false").strip().lower() == "true"


def daily_limit():
    """The most emails the bot sends in one day: DAILY_LIMIT, at most MAX_DAILY_LIMIT
    unless DAILY_LIMIT_OVERRIDE is on."""
    return DAILY_LIMIT if DAILY_LIMIT_OVERRIDE else min(DAILY_LIMIT, MAX_DAILY_LIMIT)


SEND_DELAY_SECONDS = int(env("SEND_DELAY_SECONDS", "90"))  # gap between emails
# First emails go to the highest need score first (targeting.py); a lead scored below
# MIN_SCORE, or not scored yet, is left waiting. Follow-ups don't depend on it.
MIN_SCORE = int(env("MIN_SCORE", "50"))
# At most this many first emails a day to one industry, so a high-scoring industry doesn't
# take every slot. Slots no other industry can fill still go to it. 0 = no cap.
MAX_PER_INDUSTRY = int(env("MAX_PER_INDUSTRY", "0"))
# Bounce gate: sending pauses while more than BOUNCE_LIMIT_PERCENT of the companies emailed in
# the last BOUNCE_WINDOW_DAYS have bounced. It reopens by itself as those sends age out of the
# window. With fewer than BOUNCE_MIN_SENDS companies in the window there is too little to judge.
BOUNCE_LIMIT_PERCENT = float(env("BOUNCE_LIMIT_PERCENT", "3"))
BOUNCE_WINDOW_DAYS = int(env("BOUNCE_WINDOW_DAYS", "14"))
BOUNCE_MIN_SENDS = int(env("BOUNCE_MIN_SENDS", "20"))
# "true" lets each lead get two follow-ups without an answer: templates/followup/<industry>.txt
# FOLLOWUP_AFTER_DAYS business days after the first email, then templates/followup2/<industry>.txt
# FOLLOWUP_AFTER_DAYS business days after that. A first email is never sent twice.
FOLLOWUPS_APPROVED = env("FOLLOWUPS_APPROVED", "false").strip().lower() == "true"
FOLLOWUP_AFTER_DAYS = int(env("FOLLOWUP_AFTER_DAYS", "5"))

# Reading replies (IMAP)
IMAP_HOST = env("IMAP_HOST")
IMAP_USER = env("IMAP_USER", SMTP_USER)
IMAP_PASS = env("IMAP_PASS", SMTP_PASS)
# The mailbox replies were first read from. Mail read there is remembered by folder and UID
# alone; UIDs restart in every mailbox, so mail read in any other one is remembered with
# its account in front.
FIRST_IMAP_USER = env("FIRST_IMAP_USER", IMAP_USER)
POLL_MINUTES = int(env("POLL_MINUTES", "5"))
# An interested reply or a question nobody has answered from the CRM after this many hours
# gets a second Telegram alert (weekdays 8:00-19:00 Nairobi time), and shows as late in the CRM
STALE_REPLY_HOURS = float(env("STALE_REPLY_HOURS", "2"))

# AI classifier: "ollama" (local, free, open source) or "groq" (free tier API)
AI_PROVIDER = env("AI_PROVIDER", "ollama")
OLLAMA_URL = env("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = env("OLLAMA_MODEL", "qwen2.5:7b")
GROQ_API_KEY = env("GROQ_API_KEY")
GROQ_MODEL = env("GROQ_MODEL", "openai/gpt-oss-20b")      # labels replies (classifier.py)
# "false" = templates only: no AI opener and no AI-picked uses, so every email starts with
# the template's introduction and lists the first three uses for its industry in uses.yaml.
# The AI is then used for reply classification alone. "true" turns the AI slots back on.
USE_AI_OPENER = env("USE_AI_OPENER", "false").strip().lower() == "true"
# Writes openers and picks uses (personalise.py) when AI_PROVIDER is groq.
WRITER_MODEL = env("WRITER_MODEL", "llama-3.3-70b-versatile")

# Telegram alerts
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

# Business details used in emails
COMPANY = env("COMPANY", "TalkSasa Communications Ltd")
SENDER_NAME = env("SENDER_NAME", "Florence")
SENDER_PHONE = env("SENDER_PHONE", "")
WEBSITE = env("WEBSITE", "https://talksasa.com")
# PDF attached to every email. Path is relative to this folder. Empty = no attachment.
ATTACH_PDF = env("ATTACH_PDF", "TalkSasa_Overview.pdf")
PROFILE_URL = env("PROFILE_URL", "")  # optional, e.g. a company profile PDF or page

# Web search for discover.py (websearch.py): Tavily basic search, falling back to DuckDuckGo
# when the key is missing, Tavily fails or this month's TAVILY_MONTHLY_CAP calls are used
SEARCH_PROVIDER = env("SEARCH_PROVIDER", "duckduckgo").strip().lower()
TAVILY_API_KEY = env("TAVILY_API_KEY", "")
TAVILY_MONTHLY_CAP = int(env("TAVILY_MONTHLY_CAP", "900"))
# Google Places (websearch.places): businesses on Google Maps with their website and phone.
# Off unless the key is set. Google bills each call beyond its free monthly allowance, so
# PLACES_MONTHLY_CAP stops the calls for the rest of the month.
GOOGLE_PLACES_API_KEY = env("GOOGLE_PLACES_API_KEY", "")
PLACES_MONTHLY_CAP = int(env("PLACES_MONTHLY_CAP", "500"))


# --- More than one sending mailbox ---
# Mailbox 1 is the SMTP_*/IMAP_*/FROM_* settings above. More can be added in .env as
# MAILBOX_2_EMAIL, MAILBOX_2_SMTP_PASS and so on (see mailboxes()), best on a separate domain
# with SPF, DKIM and DMARC set up. Each new mailbox warms up: it sends MAILBOX_WARMUP_START a day
# in its first week and MAILBOX_WARMUP_STEP more each week after, up to its MAX_PER_DAY. Every
# mailbox together still sends no more than daily_limit().
MAX_MAILBOXES = 9
MAILBOX_WARMUP_START = 5
MAILBOX_WARMUP_STEP = 5
MAILBOX_MAX_PER_DAY = 30


def mailboxes():
    """Every sending mailbox as a dict: key ("1", "2", ...), email, name, smtp_host, smtp_port,
    smtp_user, smtp_pass, imap_host, imap_user, imap_pass, start_date (YYYY-MM-DD or None) and
    max_per_day (None: no limit of its own, only daily_limit()). Read each time, so a change
    to the settings is used from the next run. A new mailbox's host settings default to
    mailbox 1's and its login to its own address."""
    boxes = [dict(key="1", email=FROM_EMAIL, name=FROM_NAME, smtp_host=SMTP_HOST, smtp_port=SMTP_PORT,
                  smtp_user=SMTP_USER, smtp_pass=SMTP_PASS, imap_host=IMAP_HOST, imap_user=IMAP_USER,
                  imap_pass=IMAP_PASS, start_date=env("MAILBOX_1_START_DATE") or None,
                  max_per_day=int(env("MAILBOX_1_MAX_PER_DAY")) if env("MAILBOX_1_MAX_PER_DAY") else None)]
    for n in range(2, MAX_MAILBOXES + 1):
        email = (env(f"MAILBOX_{n}_EMAIL") or "").strip()
        if not email:
            continue
        get = lambda name, default=None: env(f"MAILBOX_{n}_{name}") or default  # noqa: E731
        boxes.append(dict(key=str(n), email=email, name=get("NAME", FROM_NAME),
                          smtp_host=get("SMTP_HOST", SMTP_HOST), smtp_port=int(get("SMTP_PORT", SMTP_PORT)),
                          smtp_user=get("SMTP_USER", email), smtp_pass=get("SMTP_PASS"),
                          imap_host=get("IMAP_HOST", IMAP_HOST), imap_user=get("IMAP_USER", email),
                          imap_pass=get("IMAP_PASS", get("SMTP_PASS")),
                          # a mailbox with no start date is treated as new today: the slowest start
                          start_date=get("START_DATE"), max_per_day=int(get("MAX_PER_DAY", MAILBOX_MAX_PER_DAY))))
    return boxes


def mailbox_limit(box, today):
    """How many emails this mailbox may send on `today` (a date): its warm-up step, at most its
    max_per_day; None when it has no limit of its own (mailbox 1 unless MAILBOX_1_* says so)."""
    from datetime import date
    if box["max_per_day"] is None:
        return None
    if box["key"] == "1" and not box["start_date"]:
        return box["max_per_day"]
    try:
        start = date.fromisoformat(box["start_date"] or "")
    except ValueError:
        start = today
    weeks = max(0, (today - start).days) // 7
    return min(box["max_per_day"], MAILBOX_WARMUP_START + MAILBOX_WARMUP_STEP * weeks)
