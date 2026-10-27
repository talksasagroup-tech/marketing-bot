"""Sends intro emails to verified leads, respecting the daily limit.
Every email that goes out is also filed in the mailbox's Sent folder."""
import html
import imaplib
import os
import re
import smtplib
import socket
import sys
import time
import uuid
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, formataddr
from pathlib import Path

import config
import db
import notify
import personalise
import ranking
import targeting

TEMPLATE_DIR = Path(__file__).parent / "templates"
MAX_PDF_BYTES = 1024 * 1024
ATTACHMENT_LINE = "I've attached a one-page overview of TalkSasa."
PROFILE_LABEL = "Company profile"  # the words before the profile link, unless an admin changed them
# Tried in this order when the server doesn't flag a folder as \Sent
SENT_NAMES = ("Sent", "Sent Items", "Sent Messages", "Sent Mail", "INBOX.Sent",
              "INBOX.Sent Items", "INBOX.Sent Messages", "INBOX/Sent", "[Gmail]/Sent Mail")
# What the website line reads as. In the HTML email it is a link to config.WEBSITE
WEBSITE_LABEL = "TalkSasa"
_warned = set()
_sent_folder = None  # found once per run


def _warn(text):
    if text not in _warned:  # once per run, not once per email
        _warned.add(text)
        print(f"WARNING: {text}")


def setting(key, default):
    """A setting an admin saved in the CRM, read for every email so a change needs no restart;
    `default` (from .env) when it was never set there. An empty string is a real value: "none"."""
    value = db.get_setting(key)
    return default if value is None else value


def profile_url():
    return setting("profile_url", config.PROFILE_URL)


def profile_line():
    """The signature's profile line, e.g. "Company profile: https://...", or "" with no link."""
    url = profile_url()
    return f"{setting('profile_label', PROFILE_LABEL)}: {url}" if url else ""


def attachment():
    """The PDF to attach, or None. Warns if it is missing or over 1MB."""
    name = setting("attach_pdf", config.ATTACH_PDF)
    if not name:
        return None
    path = Path(name)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    if not path.is_file():
        _warn(f"ATTACH_PDF file not found: {path}. Sending without an attachment.")
        return None
    size = path.stat().st_size
    if size > MAX_PDF_BYTES:
        _warn(f"{path.name} is {size / MAX_PDF_BYTES:.1f}MB (over 1MB). "
              "Large attachments are more likely to land in spam.")
    return path


def load_template(industry):
    """Use templates/<industry>.txt if it exists, otherwise templates/default.txt.
    First line is the subject, the rest is the body. The body is fixed text apart from
    {opener} and {uses}, which personalise.py fills for each lead."""
    name = (industry or "default").lower().replace(" ", "_")
    path = TEMPLATE_DIR / f"{name}.txt"
    if not path.exists():
        path, name = TEMPLATE_DIR / "default.txt", "default"
    subject, body = path.read_text(encoding="utf-8").split("\n", 1)
    return name, subject.replace("Subject:", "").strip(), body.strip()


def fill(text, lead, attached=False, for_html=False, extra=None):
    """The template with this lead's details in it. `extra` adds or replaces placeholders
    (the follow-ups' own {uses} and {offer})."""
    values = dict(
        opener=personalise.opener(lead),
        uses=personalise.uses(lead),
        company=lead["company"] or "your organisation",
        industry=lead["industry"] or "",
        sender=config.SENDER_NAME,
        our_company=config.COMPANY,
        phone=config.SENDER_PHONE,
        # plain text shows the name only; the HTML version turns the address into a link
        website=config.WEBSITE if for_html else WEBSITE_LABEL,
        profile_url=profile_url(),
        # own paragraph before the sign-off; empty when nothing is attached
        attachment_line=ATTACHMENT_LINE if attached else "",
        # whole signature line; empty when there is no profile link
        profile_line=profile_line(),
    )
    values.update(extra or {})
    return text.format(**values)


def _link(m):
    """Our own website reads as WEBSITE_LABEL; any other link shows its address."""
    url = m.group(0)
    ours = url.rstrip("/") == html.escape(config.WEBSITE, quote=False).rstrip("/")
    return f'<a href="{url}">{WEBSITE_LABEL if ours else url}</a>'


def to_html(text):
    """A plain copy of the text: paragraphs and clickable links, nothing else,
    so it reads like a normal personal email."""
    paras = []
    for block in re.split(r"\n\s*\n", text.strip()):
        block = html.escape(block, quote=False)
        block = re.sub(r"https?://[^\s<]+[^\s<.,;:!?)]", _link, block)
        paras.append("<p>" + block.replace("\n", "<br>\n") + "</p>")
    unsubscribe = (f"mailto:{config.FROM_EMAIL}?subject=unsubscribe"
                   "&amp;body=Please%20unsubscribe%20me")
    paras.append(f"<p>--<br>\nIf you'd rather not hear from us, "
                 f'<a href="{unsubscribe}">click here to unsubscribe</a>.</p>')
    return "<html><body>\n" + "\n".join(paras) + "\n</body></html>"


def build(lead):
    """The first email to a lead: its industry's template, with the PDF if there is one."""
    tname, subject, body = load_template(lead["industry"])
    return compose(lead, fill(subject, lead), body, attachment()), tname


def compose(lead, subject, body, pdf=None, in_reply_to=None, extra=None, references=None):
    """The email itself: the template body filled in for this lead, as plain text and HTML,
    with our unsubscribe footer, and the PDF if given. in_reply_to (a Message-ID) makes it
    part of that email's thread; references lists the thread's Message-IDs oldest first
    (default: in_reply_to alone). `extra` is passed on to fill."""
    def tidy(t):
        return re.sub(r"\n{3,}", "\n\n", t.rstrip())  # close the gap left by an empty placeholder line
    text = tidy(fill(body, lead, attached=bool(pdf), extra=extra))
    html_body = to_html(tidy(fill(body, lead, attached=bool(pdf), for_html=True, extra=extra)))
    text += ("\n\n--\nIf you'd rather not hear from us, just reply \"unsubscribe\" "
             "and we won't email you again.")
    msg = EmailMessage()
    msg["From"] = formataddr((config.FROM_NAME, config.FROM_EMAIL))
    msg["To"] = lead["email"]
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=config.FROM_EMAIL.split("@")[1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(references or [in_reply_to])
    msg["List-Unsubscribe"] = f"<mailto:{config.FROM_EMAIL}?subject=unsubscribe>"
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    if pdf:  # turns the message into multipart/mixed: (plain + HTML) + PDF
        msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
    return msg


# --- Follow-ups ---

FOLLOWUP_DIR = TEMPLATE_DIR / "followup"     # follow-up 1
FOLLOWUP2_DIR = TEMPLATE_DIR / "followup2"   # follow-up 2, the last one
MAX_FOLLOWUPS = 2
# A lead whose out-of-office reply came in this recently is probably still away: its
# follow-up waits until then
OUT_OF_OFFICE_HOLD_DAYS = 7
# {offer} in the follow-ups, by industry. An industry missing here can't get a follow-up
# built, so it is never sent one by mistake.
VISIT_OFFER = "I'd be glad to visit or set up a quick call, whichever suits you."
API_OFFER = "I can send over our rates and API docs."
FOLLOWUP_OFFERS = {"church": VISIT_OFFER, "sacco": VISIT_OFFER, "school": VISIT_OFFER,
                   "fintech": API_OFFER, "hr_payroll": API_OFFER, "saas": API_OFFER,
                   "tech_startup": API_OFFER, "digital_lender": API_OFFER}
FOLLOWUP_USES = 2  # {uses} in a follow-up: the first two on the industry's list
# Follow-up 1's first line, and what replaces it for a lead whose first email had older
# wording than its template has now (that email may not have named these uses)
FOLLOWUP1_LINE = "Just following up on my email about sending {uses} by SMS."
OLD_WORDING_LINE = "Just following up on my earlier email about bulk SMS."


def followup_template(lead, n=1):
    """templates/followup/<industry>.txt (n=1) or templates/followup2/<industry>.txt (n=2),
    or None if that industry has no such follow-up."""
    folder = FOLLOWUP_DIR if n == 1 else FOLLOWUP2_DIR
    path = folder / f"{personalise.industry_key(lead)}.txt"
    return path if path.exists() else None


def followup_values(lead):
    """{uses} and {offer} for a follow-up, e.g. "loan reminders and deposit confirmations"."""
    key = personalise.industry_key(lead)
    return {"uses": " and ".join(personalise.USES[key]["uses"][:FOLLOWUP_USES]),
            "offer": FOLLOWUP_OFFERS[key]}


def _same_subject(a, b):
    return " ".join(a.split()).lower() == " ".join(b.split()).lower()


def old_wording(lead, thread):
    """Whether the lead's first email went out with older wording than its industry's
    template has now: its saved subject differs from the template's subject, or no subject
    was saved (emails from before subjects were recorded, so the wording is unknown).
    `thread` is db.sent_thread for the lead; a lead never emailed has no old wording."""
    if not thread:
        return False
    saved = thread[0]["sent_subject"]
    return not saved or not _same_subject(saved, fill(load_template(lead["industry"])[1], lead))


def build_followup(lead, n=1):
    """Follow-up n (1 or 2) to a lead's first email: its industry's follow-up template, no
    PDF, sent as a reply in the same thread. The subject is "Re: " + the subject the first
    email actually went out with (the template's subject only if none was saved).
    In-Reply-To is the latest email we sent the lead, References all of ours oldest first.
    If the first email had old wording (old_wording), follow-up 1 opens with
    OLD_WORDING_LINE instead of naming the uses.
    `lead` is a row from db.awaiting_followup; the thread is read from the sent table."""
    path = followup_template(lead, n)
    lead = dict(lead)
    thread = db.sent_thread(lead["id"]) if lead.get("id") else []
    body = path.read_text(encoding="utf-8").strip()
    if n == 1 and old_wording(lead, thread):
        if FOLLOWUP1_LINE not in body:  # never send the uses line to an old-wording lead by mistake
            raise RuntimeError(f"{path} doesn't contain the line to swap for old-wording leads: {FOLLOWUP1_LINE!r}")
        body = body.replace(FOLLOWUP1_LINE, OLD_WORDING_LINE)
    first_subject = ((thread[0]["sent_subject"] if thread else lead.get("sent_subject"))
                     or fill(load_template(lead["industry"])[1], lead))
    subject = first_subject if first_subject.lower().startswith("re:") else f"Re: {first_subject}"
    ids = [r["message_id"] for r in thread if r["message_id"]] or [lead.get("message_id")]
    ids = [i for i in ids if i]
    msg = compose(lead, subject, body,
                  in_reply_to=ids[-1] if ids else None, references=ids, extra=followup_values(lead))
    return msg, f"{path.parent.name}/{path.stem}"


def business_days_after(day, n):
    """The date n working days (Monday to Friday) after `day`."""
    while n:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def followups_due():
    """Follow-ups due today as [(lead, n)], follow-up 1 before follow-up 2, oldest first email
    first within each: follow-ups approved, still at 'sent' after exactly n emails,
    FOLLOWUP_AFTER_DAYS business days since the last of them, an approved industry with a
    template for follow-up n, not suppressed, and no out-of-office reply in the last week.
    A lead that replied, bounced or unsubscribed is no longer at 'sent', so it gets none."""
    if not config.FOLLOWUPS_APPROVED:
        return []
    today, is_blocked = date.fromisoformat(db.today()), db.blocker()
    return [(lead, n) for n in range(1, MAX_FOLLOWUPS + 1) for lead in db.awaiting_followup(n)
            if personalise.approved(lead) and followup_template(lead, n)
            and business_days_after(date.fromisoformat(lead["sent_on"]), config.FOLLOWUP_AFTER_DAYS) <= today
            and not is_blocked(lead["email"])
            and not db.recent_out_of_office(lead["id"], OUT_OF_OFFICE_HOLD_DAYS)]


def _folders(box):
    """Every folder on the server as (flags, name)."""
    typ, data = box.list()
    if typ != "OK":
        raise imaplib.IMAP4.error(f"LIST failed: {data}")
    out = []
    for item in data:
        if isinstance(item, tuple):  # name sent as a literal
            head, name = item[0], item[1]
        elif item:
            m = re.match(rb'(\(.*?\)) (?:"[^"]*"|NIL) (.*)', item)
            if not m:
                continue
            head, name = m.group(1), m.group(2).strip()
            if name.startswith(b'"') and name.endswith(b'"'):
                name = name[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
        else:
            continue
        flags = re.search(rb"\((.*?)\)", head)
        out.append((flags.group(1).decode().lower().split() if flags else [],
                    name.decode("utf-8", "replace")))
    return out


def find_sent_folder(box):
    """The folder flagged \\Sent, otherwise the first of SENT_NAMES that exists."""
    global _sent_folder
    if _sent_folder:
        return _sent_folder
    folders = _folders(box)
    found = next((name for flags, name in folders if "\\sent" in flags), None)
    how = "flagged \\Sent by the server"
    if not found:
        by_lower = {name.lower(): name for _, name in folders}
        found = next((by_lower[n.lower()] for n in SENT_NAMES if n.lower() in by_lower), None)
        how = "matched by name"
    if not found:
        raise LookupError("no Sent folder found. Folders: " + ", ".join(n for _, n in folders))
    print(f"Sent folder: {found} ({how})")
    _sent_folder = found
    return found


def wire_bytes(msg):
    """The message exactly as SMTP sends it."""
    return msg.as_bytes(policy=msg.policy.clone(linesep="\r\n"))


def save_to_sent(msg, sent_at):
    """File a copy of a sent email in the Sent folder, marked read.
    A failure here is only a warning: the email has already gone out."""
    try:
        box = imaplib.IMAP4_SSL(config.IMAP_HOST)
        try:
            box.login(config.IMAP_USER, config.IMAP_PASS)
            folder = find_sent_folder(box)
            quoted = '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'
            typ, data = box.append(quoted, "\\Seen", imaplib.Time2Internaldate(sent_at),
                                   wire_bytes(msg))
            if typ != "OK":
                raise imaplib.IMAP4.error(f"APPEND refused: {data}")
        finally:
            try:
                box.logout()
            except Exception:
                pass
        return True
    except Exception as e:
        print(f"WARNING: email to {msg['To']} was sent, but could not be saved "
              f"to the Sent folder: {e}")
        return False


def deliver(smtp, lead, msg, tname, followup=0):
    """Send one email, record exactly what went out, and file it in Sent. `followup` is 0 for
    the first email, else the follow-up's number (1 or 2).
    Every send goes through here, so this is also where the limits are kept: a first email
    only to a lead never emailed, follow-up n only after exactly n emails, at most
    MAX_FOLLOWUPS of them, and only when follow-ups are approved."""
    if not personalise.approved(lead):
        raise RuntimeError(f"{lead['email']}: industry {lead['industry'] or 'default'!r} is not approved in uses.yaml")
    emailed = db.sent_count(lead["id"])
    if followup and not config.FOLLOWUPS_APPROVED:
        raise RuntimeError(f"{lead['email']}: follow-ups are not approved (FOLLOWUPS_APPROVED in .env)")
    if followup and not 1 <= followup <= MAX_FOLLOWUPS:
        raise RuntimeError(f"{lead['email']}: there is no follow-up {followup:d}")
    if followup and emailed != followup:
        raise RuntimeError(f"{lead['email']}: follow-up {followup:d} goes only after exactly "
                           f"{followup:d} email{'s' if followup > 1 else ''} ({emailed} sent)")
    if not followup and emailed:
        raise RuntimeError(f"{lead['email']} has already been emailed")
    smtp.send_message(msg)
    sent_at = time.time()
    variant = ("followup" if followup == 1 else f"followup{followup:d}" if followup
               else "personal" if personalise.picked_uses(lead) else "template")
    db.record_sent(lead["id"], msg["Message-ID"], tname, str(msg["Subject"]), plain_of(msg), variant)
    save_to_sent(msg, sent_at)


def plain_of(msg):
    return msg.get_body(("plain",)).get_content()


def html_of(msg):
    return msg.get_body(("html",)).get_content()


def personal_lines(lead):
    """What previews say about the opener and uses for one lead."""
    return personalise.notes(lead)


def preview(industry=None, limit=1):
    """Show the plain and HTML versions of an email for the next `limit` verified
    leads (or an example lead for one industry). Nothing is sent."""
    leads = ranking.waiting()[:limit]
    if industry or not leads:
        leads = [{"company": "Example Company", "industry": industry or "", "email": "info@example.co.ke"}]
    line = "=" * 60
    for lead in leads:
        msg, tname = build(lead)
        print(f"{line}\nTemplate: {tname}\nFrom: {msg['From']}\nTo: {msg['To']}\nSubject: {msg['Subject']}")
        print(personal_lines(lead))
        print(f"{line}\nPLAIN TEXT VERSION\n{line}\n{plain_of(msg)}")
        print(f"{line}\nHTML VERSION\n{line}\n{html_of(msg)}")
        files = [f"{p.get_filename()} ({p.get_content_type()}, {len(p.get_payload(decode=True)) // 1024} KB)"
                 for p in msg.iter_attachments()]
        print(f"{line}\nATTACHMENTS: {', '.join(files) or 'none'}")


def _cut(text, width):
    text = text or ""
    return text if len(text) <= width else text[:width - 1] + "…"


def sent_log(days=None):
    """List what was sent: date, company, email, step, subject."""
    rows = db.sent_log(days)
    if not rows:
        print("Nothing sent" + (f" in the last {days} days." if days else " yet."))
        return
    print(f"{'DATE':10}  {'COMPANY':28}  {'EMAIL':36}  STEP  SUBJECT")
    for r in rows:
        print(f"{r['sent_on']:10}  {_cut(r['company'], 28):28}  {_cut(r['email'], 36):36}  "
              f"{r['step']:>4}  {r['sent_subject'] or '(not recorded)'}")
    print(f"{len(rows)} emails.")


def show_sent(email):
    """Print the full text of every email sent to one address."""
    rows = db.sent_to(email)
    if not rows:
        print(f"Nothing has been sent to {email}.")
        return
    line = "=" * 60
    for r in rows:
        print(f"{line}\nDate: {r['sent_on']}\nTo: {r['company']} <{r['email']}>\n"
              f"Step: {r['step']}\nTemplate: {r['template']}\nMessage-ID: {r['message_id']}\n"
              f"Subject: {r['sent_subject'] or '(not recorded)'}\n{line}")
        print(r["sent_body"] or "(The text of this email was not recorded. "
                                "It was sent before LeadBot kept copies.)")


def approved_waiting(quiet=False):
    """Waiting leads whose industry is approved in uses.yaml and whose need score is at
    least MIN_SCORE, in sending order (highest score first). The rest are skipped and left
    exactly as they are, so they go out once their industry is approved or their score
    rises. A lead not scored yet counts as 0. A lead whose email already failed to go out
    MAX_SEND_FAILURES times is skipped too."""
    waiting = ranking.waiting()
    leads = [l for l in waiting if personalise.approved(l)]
    if len(leads) < len(waiting) and not quiet:
        skipped = sorted({personalise.industry_key(l) for l in waiting if not personalise.approved(l)})
        print(f"Skipped {len(waiting) - len(leads)} waiting leads in industries not approved "
              f"in uses.yaml: {', '.join(skipped)}")
    low = [l for l in leads if (ranking.need_score(l) or 0) < config.MIN_SCORE]
    if low and not quiet:
        unscored = sum(ranking.need_score(l) is None for l in low)
        print(f"Skipped {len(low)} waiting leads with a need score below MIN_SCORE "
              f"({config.MIN_SCORE})" + (f", {unscored} of them not scored yet" if unscored else ""))
    low_ids = {l["id"] for l in low}
    failures = db.failure_counts()
    given_up = [l for l in leads if failures.get(l["id"], 0) >= db.MAX_SEND_FAILURES]
    if given_up and not quiet:
        print(f"Skipped {len(given_up)} waiting leads whose email failed to go out "
              f"{db.MAX_SEND_FAILURES} times (see send_failed in their history)")
    low_ids |= {l["id"] for l in given_up}
    return [l for l in leads if l["id"] not in low_ids]


def spread(leads, n):
    """The first n of leads (in sending order) with at most MAX_PER_INDUSTRY from one
    industry. Slots the other industries can't fill go to the next leads in order, so the
    daily limit isn't left unused. Sending order is kept."""
    cap = config.MAX_PER_INDUSTRY
    if not cap:
        return leads[:n]
    counts, picked = {}, set()
    for l in leads:
        key = personalise.industry_key(l)
        if counts.get(key, 0) < cap and len(picked) < n:
            counts[key] = counts.get(key, 0) + 1
            picked.add(l["id"])
    for l in leads:
        if len(picked) >= n:
            break
        picked.add(l["id"])
    return [l for l in leads if l["id"] in picked]


def bounce_gate():
    """Why sending must pause, or None. Mail providers start putting a domain's email in
    spam when too many of the addresses it writes to don't exist, so sending pauses while
    more than BOUNCE_LIMIT_PERCENT of the companies emailed in the last BOUNCE_WINDOW_DAYS
    bounced. As those sends age out of the window, sending resumes by itself."""
    emailed, bounced = db.bounce_stats(config.BOUNCE_WINDOW_DAYS)
    if emailed < config.BOUNCE_MIN_SENDS:
        return None
    rate = 100 * bounced / emailed
    if rate <= config.BOUNCE_LIMIT_PERCENT:
        return None
    return (f"{bounced} of the {emailed} companies emailed in the last {config.BOUNCE_WINDOW_DAYS} "
            f"days bounced ({rate:.1f}%), over the {config.BOUNCE_LIMIT_PERCENT:g}% limit")


def _paused(reason):
    text = (f"Sending paused: {reason}. It resumes by itself as those emails age out of the "
            f"{config.BOUNCE_WINDOW_DAYS}-day window, or when you raise BOUNCE_LIMIT_PERCENT in .env.")
    print(text)
    try:
        notify.send("⛔ LeadBot " + text)
    except Exception:
        pass  # the message above says it too


SEND_LOCK = "send"
LOCK_SECONDS = 3600  # renewed after every email; a run that crashes frees it after this


def leftover_claims():
    """Emails claimed by an earlier run that stopped before recording them. Each one may
    or may not have gone out, so that lead is held (never emailed again) until you check
    the Sent folder and remove the claim. Printed at the start of every real send."""
    rows = db.send_claims()
    for r in rows:
        print(f"WARNING: an earlier run stopped while sending to {r['email']} (claimed {r['claimed_at']} UTC). "
              "It may have gone out, so that lead is held. Check the Sent folder, then delete its row "
              "from send_claims.")
    return rows


def plan(dry_run=False):
    """Today's batch as [(lead, followup)], follow-ups first (followup = 0 for a first email,
    else the follow-up's number). A dry run reads only: no
    start years are looked up (WHOIS, websites), no scores saved and no AI asked, so leads
    not ranked yet are shown in the order they would have without those, and leads not
    scored yet are left out as below MIN_SCORE."""
    remaining = config.daily_limit() - db.sent_today()
    if remaining <= 0:
        return []
    # Follow-ups first: they fall due on a set day, and a second note is answered more often
    # than a first one. New leads get what is left of today's limit.
    due = followups_due()[:remaining]
    leads = []
    if remaining > len(due):
        # Highest-scoring leads first (younger companies, busy niches, own-domain email),
        # then oldest-found first. Start years are needed for every waiting lead to rank them.
        if not dry_run:
            ranking.fill_years()
            targeting.score_unscored()
        leads = spread(approved_waiting(), remaining - len(due))
        # Write personal lines for any of today's leads that don't have them yet
        if not dry_run and leads and (personalise.run(leads) or any(l["confidence"] is None for l in leads)):
            leads = spread(approved_waiting(quiet=True), remaining - len(due))
    return due + [(lead, 0) for lead in leads]


def limit_override_note():
    """Says so, every run, when DAILY_LIMIT is above MAX_DAILY_LIMIT: raised by the override,
    or held back because the override is off. None when the limit is as set."""
    if config.DAILY_LIMIT <= config.MAX_DAILY_LIMIT:
        return None
    if config.DAILY_LIMIT_OVERRIDE:
        return (f"DAILY_LIMIT_OVERRIDE is on: sending up to {config.DAILY_LIMIT} today, above the "
                f"usual {config.MAX_DAILY_LIMIT}.")
    return (f"DAILY_LIMIT is {config.DAILY_LIMIT}, but the limit is {config.MAX_DAILY_LIMIT} unless "
            "DAILY_LIMIT_OVERRIDE=true is set in .env.")


def run(dry_run=False):
    note = limit_override_note()
    if note:
        print(f"NOTE: {note}")
        if config.DAILY_LIMIT_OVERRIDE and not dry_run:
            try:
                notify.send("⚠️ LeadBot " + note)
            except Exception:
                pass  # the note above says it too
    if config.daily_limit() - db.sent_today() <= 0:
        print(f"Daily limit of {config.daily_limit()} reached. Try again tomorrow.")
        return
    paused = bounce_gate()
    if paused and not dry_run:
        _paused(paused)
        return
    if paused:
        print(f"NOTE: a real send would be paused: {paused}.")
    if dry_run:
        return _dry_run()
    # One send run at a time, across processes (auto, cron, a manual send): a second one
    # stops here. Each email is also claimed on its own (send_one), so even two runs that
    # both got past this could never send the same email twice or go over the daily limit.
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    if not db.acquire_lock(SEND_LOCK, owner, LOCK_SECONDS):
        held = db.lock_holder(SEND_LOCK)
        print(f"Another send run is in progress (since {held['acquired_at'] if held else '?'} UTC). Not sending.")
        return
    try:
        leftover_claims()
        batch = plan()
        if not batch:
            print("Nothing to send: no follow-ups due and no verified leads waiting in an approved industry.")
            return
        _send_batch(batch, owner)
    finally:
        db.release_lock(SEND_LOCK, owner)


def _dry_run():
    batch = plan(dry_run=True)
    if not batch:
        print("Nothing to send: no follow-ups due and no verified leads waiting in an approved industry.")
        return
    due = [b for b in batch if b[1]]
    for lead, followup in batch[:2] + [b for b in batch if not b[1]][:1 if due else 0]:
        msg, _ = build_followup(lead, followup) if followup else build(lead)
        print("=" * 60, f"\n{f'FOLLOW-UP {followup:d}' if followup else 'FIRST EMAIL'}\nTo: {msg['To']}\n"
              f"Subject: {msg['Subject']}\n" + ("" if followup else personal_lines(lead) + "\n")
              + f"\n{plain_of(msg)}")
    print(f"\n(dry run) would send {len(due)} follow-ups and {len(batch) - len(due)} first emails. "
          "Nothing was sent, looked up or saved.")


def _send_batch(batch, owner):
    counts = {"sent": 0, "bounced": 0, "failed": 0, "skipped": 0}
    in_a_row = 0  # failures since the last email that went out
    s = connect()
    try:
        for i, (lead, followup) in enumerate(batch, 1):
            outcome = send_one(s, lead, followup, owner)
            if outcome == "reconnect":
                # The connection dropped: reconnect once and try this lead again
                close(s)
                try:
                    s = connect()
                except (OSError, smtplib.SMTPException) as e:
                    print(f"Lost the connection to the mail server and could not reconnect ({e}). "
                          f"Stopping: the other leads stay waiting for the next send.")
                    break
                outcome = send_one(s, lead, followup, owner)
                if outcome == "reconnect":
                    print("Lost the connection to the mail server again. Stopping: the other "
                          "leads stay waiting for the next send.")
                    break
            if outcome in ("stop", "limit"):
                break
            counts[outcome] += 1
            in_a_row = in_a_row + 1 if outcome == "failed" else 0
            print(f"[{i}/{len(batch)}] {outcome}{f' follow-up {followup:d}' if followup else ''} {lead['email']}")
            db.refresh_lock(SEND_LOCK, owner, LOCK_SECONDS)
            if outcome == "bounced" and bounce_gate():
                _paused(bounce_gate())
                break
            if in_a_row >= MAX_FAILURES_IN_A_ROW:
                print(f"{in_a_row} emails failed in a row, so the problem is probably not the leads. "
                      "Stopping: the other leads stay waiting for the next send.")
                break
            if i < len(batch) and outcome != "skipped":
                time.sleep(config.SEND_DELAY_SECONDS)
    finally:
        close(s)
    print(f"Sent {counts['sent']}, bounced {counts['bounced']}, failed {counts['failed']}, "
          f"skipped {counts['skipped']} (failed leads stay waiting and are tried again next time).")


# After this many failures in a row the batch stops: the mail server or the email itself is
# the problem, and going on would only fail the rest of the day's leads too.
MAX_FAILURES_IN_A_ROW = 3


def connect():
    """A logged-in SMTP connection. Gives up after 60 seconds without an answer."""
    s = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=60)
    s.starttls()
    s.login(config.SMTP_USER, config.SMTP_PASS)
    return s


def close(s):
    try:
        s.quit()
    except Exception:
        pass  # already gone


def send_one(s, lead, followup=0, owner=None):
    """Try to email one lead its first email (followup=0), or follow-up 1 or 2. Returns what happened:
    "sent", "bounced" (address refused), "failed" (this email didn't go out; the lead stays
    waiting and is tried again next time), "skipped" (no longer to be emailed: opted out,
    suppressed, replied or already emailed since the batch was made), "limit" (today's
    limit reached), "reconnect" (the connection dropped before the email went out) or
    "stop" (the server refuses our sender or login: no point going on).
    The email is claimed first (db.claim_send), which rechecks the lead and the daily limit
    at the last moment. Anything that goes wrong after the email has gone out is not caught
    here, so the batch stops, and the claim stays, instead of risking a second email."""
    why_not = db.claim_send(lead["id"], followup, config.daily_limit(), owner)
    if why_not:
        with db.conn() as c:
            db.log_event(c, lead["id"], "send_skipped", reason=why_not, followup=followup or None)
        print(f"  skipped {lead['email']}: {why_not}")
        return "limit" if why_not.startswith("daily limit") else "skipped"
    try:
        msg, tname = build_followup(lead, followup) if followup else build(lead)
    except Exception as e:  # e.g. a template with a broken {placeholder}
        db.release_claim(lead["id"])
        return _failed(lead, f"could not build the email: {e}")
    try:
        deliver(s, lead, msg, tname, followup)
    except (smtplib.SMTPException, OSError, RuntimeError):
        db.release_claim(lead["id"])  # raised before the email went out (see deliver)
        return _not_sent(sys.exc_info()[1], lead)
    if not followup:  # after a follow-up the lead stays at sent, waiting for an answer
        db.set_status(lead["id"], "sent", "send")
    return "sent"


def _not_sent(error, lead):
    """What send_one returns for an email the mail server or deliver's checks didn't send."""
    try:
        raise error
    except smtplib.SMTPRecipientsRefused as e:
        # Only a permanent refusal (5xx) means the address doesn't exist. A 4xx or 421 is the
        # server saying "not now" (greylisting, busy): the lead stays waiting and is tried again.
        code, msg = e.recipients.get(lead["email"]) or next(iter(e.recipients.values()), (None, b""))
        if code is None or 500 <= int(code) < 600:
            db.set_status(lead["id"], "bounced", "send")
            return "bounced"
        return _failed(lead, f"address refused for now: {code} {msg!r}", code)
    except (smtplib.SMTPSenderRefused, smtplib.SMTPAuthenticationError) as e:
        print(f"The mail server refused our sender or login ({e}). Stopping.")
        _failed(lead, str(e), getattr(e, "smtp_code", None))
        return "stop"
    except smtplib.SMTPResponseException as e:
        if e.smtp_code == 421:  # "service closing": the server is about to hang up
            return "reconnect"
        # 4xx: try again later; 5xx: this email was refused. Either way it didn't go out.
        return _failed(lead, str(e), e.smtp_code)
    except (smtplib.SMTPServerDisconnected, OSError):
        return "reconnect"
    except RuntimeError as e:  # deliver's own checks: industry not approved, already emailed
        return _failed(lead, str(e))
    except smtplib.SMTPException as e:  # any other refusal: this email didn't go out
        return _failed(lead, str(e))


def _failed(lead, error, code=None):
    with db.conn() as c:
        db.log_event(c, lead["id"], "send_failed", error=error[:500], smtp_code=code)
    print(f"  not sent to {lead['email']}: {error}")
    return "failed"
