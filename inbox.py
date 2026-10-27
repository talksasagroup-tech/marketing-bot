"""Polls the inbox, matches replies to leads, classifies them and alerts you."""
import email
import html as html_lib
import imaplib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from email.errors import HeaderParseError
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path

import classifier
import config
import db
import notify
import ranking

# needs_review is everything held for a person: an AI failure, an unsure AI, a reply that
# mentions opting out without plainly asking. Each reply is alerted once (alert_outbox).
ALERT_ON = {"interested", "question", "needs_review"}
BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# Where the quoted copy of the email being answered starts in a plain-text reply
QUOTE_START = re.compile(
    r"\nOn [^\n]{5,200}\n?[^\n]{0,100}wrote:"         # Gmail, Apple Mail
    r"|\n-{2,} ?Original Message"                     # older Outlook
    r"|\n_{10,}"                                       # Outlook's line above the quote
    r"|\n\*?From:\*?[ \t][^\n]*\n\*?(?:Sent|Date):",  # Outlook's From:/Sent: block
    re.I)
# Where the quoted copy starts in an HTML reply
HTML_QUOTE_START = re.compile(r"<blockquote|<div[^>]+(?:gmail_quote|divRplyFwdMsg|appendonsend)", re.I)
# Subjects mail apps give automatic replies ("Automatic reply: ...", "Out of Office: ...")
AUTO_SUBJECT = re.compile(r"^\s*(automatic reply|auto[- ]?reply|autoreply|auto[- ]?response|"
                          r"out of (the )?office|away from (the )?office)\b", re.I)
# Our own footer, plain and HTML wording. It says "unsubscribe", so when a reply quotes it
# without the markers above it would read as an unsubscribe request; it is removed first.
OUR_FOOTER = re.compile(r"[^\n]*(?:rather not hear from us|click here to unsubscribe)[^\n]*", re.I)


def _decoded(part):
    payload = part.get_payload(decode=True) or b""
    try:
        return payload.decode(part.get_content_charset() or "utf-8", "ignore")
    except LookupError:  # a charset Python doesn't know: read it as UTF-8
        return payload.decode("utf-8", "ignore")


def header_text(value):
    """A header (e.g. Subject) as text. A part in a charset Python doesn't know, or one that
    is badly encoded, is read as UTF-8 with the unreadable bits replaced instead of raising."""
    try:
        return str(make_header(decode_header(value or "")))
    except (LookupError, UnicodeError, ValueError, HeaderParseError):
        return "".join(raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                       for raw, _ in decode_header(value or ""))


def html_text(html):
    """The readable text of an HTML email, without the quoted email it answers."""
    html = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", html)
    html = HTML_QUOTE_START.split(html, maxsplit=1)[0]
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", html))


def text_of(msg):
    """Get the reply's own text: the plain-text part (or the HTML part as text), without the
    quoted original email and without our footer."""
    plain = html = None
    for part in msg.walk():
        if part.is_multipart() or "attachment" in str(part.get("Content-Disposition")):
            continue
        if part.get_content_type() == "text/plain" and plain is None:
            plain = _decoded(part)
        elif part.get_content_type() == "text/html" and html is None:
            html = _decoded(part)
    body = plain if plain is not None else html_text(html or "")
    body = QUOTE_START.split("\n" + body.replace("\r\n", "\n"), maxsplit=1)[0]
    body = OUR_FOOTER.sub("", body)
    return "\n".join(l for l in body.splitlines() if not l.startswith(">")).strip()


def is_auto_reply(msg, subject):
    """Whether a mail app sent this by itself (out of office, holiday, auto-response),
    going by the headers it marks such mail with and its subject. The words of the
    reply are not used: a person can write "on leave" in a real reply."""
    auto = (msg.get("Auto-Submitted") or "no").strip().lower()
    if auto != "no":
        return True
    if any(msg.get(h) for h in ("X-Autoreply", "X-Autorespond", "X-Autoresponse")):
        return True
    if (msg.get("Precedence") or "").strip().lower() == "auto_reply":
        return True
    return bool(AUTO_SUBJECT.search(subject))


def status_after(current, label):
    """A lead's status after a reply with this label, or None to leave it as it is. Only
    replies change status here; the dashboard can still set any status by hand.
      - unsubscribe: always unsubscribed, whatever the lead's status (an opt-out is absolute)
      - out_of_office: no change; nobody has answered yet, so the lead still counts as
        waiting for a reply (and for a follow-up)
      - bounce: bounced, except for a lead already at meeting or client, or unsubscribed
      - anything else: replied, but only from new, verified or sent, so a lead at meeting
        or client never drops back to replied when it writes again"""
    if label == "unsubscribe":
        return "unsubscribed"
    if label == "out_of_office":
        return None
    if label == "bounce":
        return None if current in ("meeting", "client", "unsubscribed") else "bounced"
    return "replied" if current in ("new", "verified", "sent") else None


def send_alerts():
    """Send the Telegram alerts waiting in the queue, oldest first. One that fails stays
    queued and is tried again at the next check, so an alert is never lost to a network
    error. Returns how many were sent."""
    sent = 0
    for alert in db.pending_alerts():
        try:
            notify.send(alert["text"])
        except Exception as e:
            db.alert_failed(alert["id"], str(e))
            print(f"  Telegram alert not sent ({e}); will try again next check.")
            break  # Telegram is down or unreachable: the rest wait too
        db.alert_sent(alert["id"])
        sent += 1
    return sent


def thread_lead(headers):
    """The lead one of our emails went to, if this message answers it (In-Reply-To / References)."""
    ids = re.findall(r"<[^>]+>", f"{headers.get('In-Reply-To', '')} {headers.get('References', '')}")
    return db.lead_by_message_ids(ids)


def colleague_leads(sender):
    """Leads at the sender's company: same email domain, never a free-mail one like gmail.com."""
    domain = sender.rsplit("@", 1)[-1]
    if not domain or domain in ranking.FREE_MAIL:
        return []
    return db.leads_on_domain(domain)


def since_first_send():
    """IMAP date the day before our first email, or None if nothing was sent yet."""
    first = db.first_sent_date()
    if not first:
        return None
    d = date.fromisoformat(first) - timedelta(days=1)
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def delivery_report(headers):
    """Whether a message is a mail server's delivery report (RFC 3464), whatever address it
    came from (bounces@..., a provider's own name): multipart/report, report-type delivery-status."""
    return (headers.get_content_type() == "multipart/report"
            and str(headers.get_param("report-type") or "").lower() == "delivery-status")


def find_original_recipient(msg):
    """For bounces, the failed address is inside the message body."""
    raw = msg.as_string()
    m = re.search(r"Final-Recipient:.*?;\s*([^\s]+@[^\s]+)", raw, re.I)
    return m.group(1).lower() if m else None


# Replies stored for a person to read, never sent to the AI and never acted on ("hold"
# means: alert on Telegram, whatever the label)
HELD = {"label": "needs_review", "hold": True,
        "summary": "Missed by an earlier check and caught up later; not read by the AI. Please read it."}
NOT_LINKED = {"label": "needs_review", "hold": True,
              "summary": "A reply to our email from an address not linked to any lead; not read by the AI."}
# The Message-IDs LeadBot gives its own emails (email.utils.make_msgid): <digits.digits.digits@domain>
LEADBOT_MESSAGE_ID = re.compile(r"<\d+\.\d+\.\d+@([^>\s]+)>")
TEMPLATE_DIR = Path(__file__).parent / "templates"


def outreach_subjects():
    """The subjects our emails go out with: every template's, and every one recorded as sent."""
    import sender
    subjects = {s for path in TEMPLATE_DIR.glob("*.txt")
                for s in sender.split_template(path.read_text(encoding="utf-8"))[0]}
    return {s.lower() for s in subjects | db.sent_subjects() if s}


def answers_our_email(headers, subject, subjects):
    """Whether a message is a reply to one of our emails, whoever sent it: it answers a
    Message-ID that LeadBot made, or its subject contains one of our outreach subjects."""
    domain = config.FROM_EMAIL.rsplit("@", 1)[-1].lower()
    refs = f"{headers.get('In-Reply-To', '')} {headers.get('References', '')}"
    if any(d.lower() == domain for d in LEADBOT_MESSAGE_ID.findall(refs)):
        return True
    return any(s in subject.lower() for s in subjects)


# Folders never read: our own outgoing mail. Everything else is, Junk and Trash included,
# so a reply that was filtered or moved before a check still shows.
SKIP_FOLDER_FLAGS = {"\\sent", "\\drafts", "\\noselect"}
SKIP_FOLDER_NAMES = {"sent", "drafts", "sent items", "sent messages", "sent mail", "[gmail]/sent mail",
                     "[gmail]/drafts", "[gmail]/all mail"}


def mail_folders(box):
    """The folders replies can be in, INBOX first: every folder but Sent and Drafts."""
    from sender import _folders  # the LIST parser that also finds the Sent folder
    names = []
    for flags, name in _folders(box):
        short = name.lower().removeprefix("inbox.").removeprefix("inbox/")
        if not SKIP_FOLDER_FLAGS & set(flags) and short not in SKIP_FOLDER_NAMES:
            names.append(name)
    return sorted(names, key=lambda n: n.upper() != "INBOX")


def first_mailbox():
    return (config.IMAP_USER or "").lower() == (config.FIRST_IMAP_USER or "").lower()


def mail_key(folder, uid):
    """How a message is remembered: its UID in INBOX (as it always was), folder:UID elsewhere,
    with "account/" in front for any mailbox but the first one."""
    key = uid if folder.upper() == "INBOX" else f"{folder}:{uid}"
    return key if first_mailbox() else f"{config.IMAP_USER.lower()}/{key}"


def quoted(folder):
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fill_message_ids(box):
    """Give replies stored before Message-IDs were kept their Message-ID, read from INBOX,
    so the same email found later in another folder is recognised as already stored."""
    for reply_id, uid in db.replies_without_message_id():
        typ, head = box.uid("fetch", uid.encode(), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        if typ == "OK" and head and isinstance(head[0], tuple):
            mid = email.message_from_bytes(head[0][1]).get("Message-ID", "").strip()
            if mid:
                db.set_reply_message_id(reply_id, mid)


def check_once(catch_up=False):
    """Reads mail from leads, replies to our emails from anyone (matched by In-Reply-To),
    bounces of our own emails, and unsubscribe requests from a lead's colleagues (same
    company domain). Read and unread mail alike, since our first send: each message is
    handled once, then remembered. Everything else is skipped: never downloaded in full,
    never sent to the AI, and left unread exactly as it was. Every folder but Sent and
    Drafts is read; an email found in two folders (moved after it was stored) counts once.

    A reply to one of our emails from an address linked to no lead (a forward, a personal
    address, a test) is stored too, as needs_review, and sent to you without the AI.

    catch_up=True reads only the mail an earlier check skipped, for when the matching has
    improved since. Whatever now matches a lead is stored as needs_review and sent to you
    on Telegram, without the AI and without acting on it (bounces and auto-replies are
    still recognised by their headers). What still doesn't match stays skipped."""
    send_alerts()  # alerts left over from a check when Telegram was unreachable
    box = imaplib.IMAP4_SSL(config.IMAP_HOST)
    box.login(config.IMAP_USER, config.IMAP_PASS)
    since = since_first_send()
    handled = 0
    skipped, newly_skipped, caught = db.skipped_uids(), [], []
    subjects = outreach_subjects()
    for folder in mail_folders(box) if since else []:
        if box.select(quoted(folder))[0] != "OK":
            continue
        if folder.upper() == "INBOX" and first_mailbox():  # those UIDs are the first mailbox's
            fill_message_ids(box)
        for uid in box.uid("search", None, "SINCE", since)[1][0].split():
            key = mail_key(folder, uid.decode())
            if db.reply_seen(key) or (key in skipped) != catch_up:
                continue  # a normal check leaves skipped mail alone; a catch-up reads only that
            try:
                result = check_message(box, uid, key, subjects, catch_up, newly_skipped)
            except CHECK_ERRORS:
                raise  # the connection or the database, not this email: the next check tries again
            except Exception as e:
                set_aside(folder, uid, key, e, catch_up, newly_skipped)
                continue
            if result:
                if catch_up:
                    caught.append(key)
                handled += 1

    db.add_skipped(newly_skipped)
    db.unskip(caught)
    box.logout()
    # Heartbeat for the dashboard: when the inbox was last checked
    (Path(__file__).parent / "last_check.txt").write_text(datetime.now().isoformat(timespec="seconds"))
    remind_late()
    send_alerts()
    return handled


# Replies that someone is waiting on us to answer
WAITING_LABELS = ("interested", "question")


def late_replies(now=None):
    """Interested replies and questions nobody has answered from the CRM or marked done
    within STALE_REPLY_HOURS, that haven't had their second alert yet. Empty when the CRM
    isn't set up (it adds the column that says a reply was handled)."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=config.STALE_REPLY_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with db.conn() as c:
        if "handled" not in db.columns(c, "replies") or not db.columns(c, "crm_messages"):
            return []
        return c.execute(f"""
            SELECT r.id, r.from_email, r.subject, r.summary, r.label, r.received_at, l.company
            FROM replies r JOIN leads l ON l.id = r.lead_id
            WHERE r.label IN ({','.join('?' * len(WAITING_LABELS))}) AND COALESCE(r.handled, 0) = 0
              AND r.realerted_at IS NULL AND r.received_at <= ?
              AND NOT EXISTS (SELECT 1 FROM crm_messages m WHERE m.lead_id = r.lead_id
                              AND m.status != 'failed' AND m.sent_at >= r.received_at)
            ORDER BY r.id""", (*WAITING_LABELS, cutoff)).fetchall()


def working_hours(now=None):
    """Weekdays 8:00 to 19:00 in Nairobi: when a second alert is worth waking someone for."""
    local = (now or datetime.now(timezone.utc)).astimezone(config.NAIROBI)
    return local.weekday() < 5 and 8 <= local.hour < 19


def remind_late(now=None):
    """Queue one more Telegram alert for each late reply (late_replies), in working hours
    only, so a client who is interested doesn't wait for an answer. Returns how many."""
    if not working_hours(now):
        return 0
    late = late_replies(now)
    for r in late:
        hours = config.STALE_REPLY_HOURS
        db.queue_alert(None, f"⏰ STILL NOT ANSWERED — {r['company'] or r['from_email']}\n"
                             f"{r['label'].replace('_', ' ').capitalize()} reply from {r['from_email']}, "
                             f"no answer after {hours:g} hour{'s' if hours != 1 else ''}.\n"
                             f"Subject: {r['subject']}\n\nSummary: {r['summary'] or ''}")
        with db.conn() as c:
            c.execute("UPDATE replies SET realerted_at = CURRENT_TIMESTAMP WHERE id = ?", (r["id"],))
    return len(late)


# Errors about the mail server connection or the database, not about one email. They stop the
# check, as before, and the next check tries again; anything else is that email's own problem.
CHECK_ERRORS = (imaplib.IMAP4.error, OSError) + db.Error


def set_aside(folder, uid, key, error, catch_up, newly_skipped):
    """An email that could not be handled (e.g. one malformed in a way the code doesn't
    expect): it is left unread in the mailbox, never looked at again by a normal check, and
    alerted once, so one bad email can't stop every check. A catch-up that fails on it again
    leaves it skipped and doesn't alert again."""
    where = f"{folder} message {uid.decode() if isinstance(uid, bytes) else uid}"
    reason = f"{type(error).__name__}: {str(error)[:200]}"
    print(f"  could not read {where} ({reason}); set aside")
    if catch_up:
        return
    newly_skipped.append(key)
    db.queue_alert(None, f"⚠️ LeadBot could not read one email ({where}): {reason}\n\n"
                         "It was left unread and set aside; the other mail was checked as usual. "
                         "Please open it in your mail app and check whether it is a reply from a lead.")


def check_message(box, uid, key, subjects, catch_up, newly_skipped):
    """Handle one message of the selected folder. Returns its result if it was stored,
    None if it was skipped (added to newly_skipped when it should never be looked at again)."""
    # Look at the headers only. BODY.PEEK never marks the mail as read.
    _, head = box.uid("fetch", uid,
                      "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES CONTENT-TYPE)])")
    if not head or not isinstance(head[0], tuple):
        return None
    headers = email.message_from_bytes(head[0][1])
    message_id = headers.get("Message-ID", "").strip() or None
    if message_id and db.reply_with_message_id(message_id):
        if not catch_up:
            newly_skipped.append(key)  # the same email, already stored from another folder
        return None
    sender = parseaddr(headers.get("From", ""))[1].lower()
    is_bounce = sender.startswith(BOUNCE_SENDERS) or delivery_report(headers)
    lead = db.find_lead_by_email(sender) or (None if is_bounce else thread_lead(headers))
    colleagues = [] if lead or is_bounce else colleague_leads(sender)
    to_us = not is_bounce and answers_our_email(
        headers, header_text(headers.get("Subject", "")), subjects)
    if not lead and not is_bounce and not colleagues and not to_us:
        newly_skipped.append(key)  # not a lead and not a reply to us: leave it alone
        return None

    _, msg_data = box.uid("fetch", uid, "(BODY.PEEK[])")
    msg = email.message_from_bytes(msg_data[0][1])
    subject = header_text(msg.get("Subject", ""))
    body = text_of(msg)

    if colleagues:
        # Someone else at a lead's company, not in our email's thread. An unsubscribe
        # request is acted on by the rules alone; a reply to our email is held for you;
        # anything else is left alone. Never sent to the AI.
        rule = classifier.quick_rules(f"Subject: {subject}\n\n{body}")
        opt_out = rule == "unsubscribe"
        if not opt_out and not to_us and rule != "needs_review":
            newly_skipped.append(key)
            return None
        lead = colleagues[0]
        if catch_up or not opt_out:  # held for you to read and act on
            colleagues = []
            result = HELD if catch_up else {
                "label": "needs_review", "hold": True,
                "summary": ("Mentions unsubscribing, but is not a plain request to stop," if rule == "needs_review"
                            else "A reply to our email") +
                           f" from someone else at {sender.rsplit('@', 1)[-1]}; not read by the AI."}
        else:
            result = {"label": "unsubscribe",
                      "summary": f"{sender} asked to unsubscribe {sender.rsplit('@', 1)[-1]}"}
    elif is_bounce:
        # "Delayed Mail" warnings mean the server is still retrying: not a bounce
        action = re.search(r"^Action:\s*(\w+)", msg.as_string(), re.I | re.M)
        if action and action.group(1).lower() != "failed":
            newly_skipped.append(key)
            return None
        failed = find_original_recipient(msg)
        lead = db.find_lead_by_email(failed) if failed else thread_lead(msg)
        if lead:
            result = {"label": "bounce", "summary": f"delivery to {failed or lead['email']} failed"}
        else:  # shown, so you can tell whose email it was; it changes no lead
            result = {"label": "bounce", "summary": "A delivery failure the bot could not link to a "
                                                    "lead; not read by the AI. Check which email it was."}
    elif is_auto_reply(msg, subject):
        result = {"label": "out_of_office", "summary": "automatic reply"}
    elif lead is None:  # a reply to our email from an address linked to no lead
        # A plain request to stop is still kept: that address is never emailed or added
        opted_out = not catch_up and classifier.quick_rules(f"Subject: {subject}\n\n{body}") == "unsubscribe"
        result = {"label": "unsubscribe", "summary": f"{sender} asked to unsubscribe"} if opted_out else NOT_LINKED
    else:
        result = HELD if catch_up else classifier.classify(f"Subject: {subject}\n\n{body}")
    label = result["label"]

    reply_id = db.add_reply(lead["id"] if lead else None, sender, subject, body, label,
                            result["summary"], key, message_id)
    if not reply_id:
        return None  # stored by an earlier check: nothing is logged or alerted twice
    for each in colleagues or ([lead] if lead else []):  # a colleague's opt-out covers the company
        new_status = status_after(each["status"], label)
        if new_status:
            db.set_status(each["id"], new_status, "inbox")
    if label == "unsubscribe" and (not lead or sender != lead["email"]):
        db.unsubscribe(sender)  # the person who asked is never emailed or added either

    if label in ALERT_ON or result.get("hold"):  # one alert per stored reply: reply_id is unique in the outbox
        # Queued, then sent after the loop: a Telegram error can't stop the check
        db.queue_alert(reply_id, notify.alert_text(lead, sender, subject, label, result["summary"], body))
        box.uid("store", uid, "+FLAGS", "(\\Seen)")  # the alert is on its way
    print(f"  {label:15} {sender}  {result['summary']}")
    return result


# After this many failed checks in a row you get one Telegram alert (and one more when the
# checks work again), so replies don't wait unseen while the mailbox can't be reached.
FAILURES_BEFORE_ALERT = 3
# The latest failure while checks are failing, for the CRM's health strip; removed once a
# check works. A file, like last_check.txt, so it shows even when the database is the problem.
ERROR_FILE = Path(__file__).parent / "inbox_error.txt"
_failures = 0


def checked(check):
    """Run one inbox check (check_once) and keep count of failures in a row. Raises what
    the check raised, so the caller still sees it."""
    global _failures
    try:
        n = check()
    except Exception as e:
        _failures += 1
        error = f"{type(e).__name__}: {str(e)[:300]}"
        ERROR_FILE.write_text(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                                          "error": error, "failures": _failures}))
        if _failures == FAILURES_BEFORE_ALERT:
            try:
                notify.send(f"⚠️ LeadBot couldn't check the inbox {_failures} times in a row "
                            f"({error}). Replies wait until it works again.")
            except Exception:
                pass  # Telegram may be down for the same reason; the dashboard shows it too
        raise
    if _failures >= FAILURES_BEFORE_ALERT:
        try:
            notify.send(f"✅ LeadBot is checking the inbox again after {_failures} failed checks.")
        except Exception:
            pass
    _failures = 0
    ERROR_FILE.unlink(missing_ok=True)
    return n


def last_error():
    """{"at", "error", "failures"} while inbox checks are failing, else None."""
    try:
        return json.loads(ERROR_FILE.read_text())
    except (OSError, ValueError):
        return None


def watch():
    print(f"Watching inbox every {config.POLL_MINUTES} min. Ctrl+C to stop.")
    while True:
        try:
            n = checked(check_once)
            if n:
                print(f"Handled {n} replies.")
        except Exception as e:
            print(f"Inbox check failed: {e}")
        time.sleep(config.POLL_MINUTES * 60)
