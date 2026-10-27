"""Extra read-only routes for the LeadBot dashboard.

Add to web.py (two lines, after `app = FastAPI(...)`):
    from web_extra import router as extra_router
    app.include_router(extra_router)

Routes:
    GET /api/leads/{lead_id}/emails  -> every email sent to one lead + every reply, oldest first
                                        (the dashboard's email history; web.py has no route of its own for it)
    GET /api/sent-today               -> who was emailed today

Works with older and newer databases: if the sent table has no step,
sent_subject or sent_body column yet, those fields come back as null.
"""


from fastapi import APIRouter, HTTPException

import db
from crm import screen_text

router = APIRouter()


def _conn():
    return db.conn()


def _cols(c, table):
    return db.columns(c, table)


def _pick(cols, *names):
    return next((n for n in names if n in cols), None)


def _iso_utc(ts):
    """SQLite CURRENT_TIMESTAMP is UTC without a zone; mark it so browsers convert it."""
    if ts and "T" not in ts and len(ts) > 10:
        return ts.replace(" ", "T") + "Z"
    return ts


@router.get("/api/leads/{lead_id}/emails")
def lead_emails(lead_id: int):
    with _conn() as c:
        lead = c.execute("SELECT id, company, email, industry, status FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not lead:
            raise HTTPException(404, "Lead not found")

        sc = _cols(c, "sent")
        step = "step" if "step" in sc else None
        subj = _pick(sc, "sent_subject", "subject")
        body = _pick(sc, "sent_body", "body")
        when = _pick(sc, "sent_at", "created_at")
        emails = [dict(r) for r in c.execute(
            f"""SELECT id, sent_on,
                       {step or 'NULL'} AS step,
                       {subj or 'NULL'} AS subject,
                       {body or 'NULL'} AS body,
                       {when or 'NULL'} AS sent_at
                FROM sent WHERE lead_id = ? ORDER BY id""", (lead_id,))]
        for i, e in enumerate(emails):
            if not e["step"]:
                e["step"] = i + 1
            e["sent_at"] = _iso_utc(e["sent_at"])

        rc = _cols(c, "replies")
        handled = "COALESCE(handled, 0)" if "handled" in rc else "0"
        replies = [dict(r) for r in c.execute(
            f"""SELECT id, received_at, label, summary, subject, body, {handled} AS handled
                FROM replies WHERE lead_id = ? ORDER BY id""", (lead_id,))]
        for r in replies:
            r["received_at"] = _iso_utc(r["received_at"])
            r["handled"] = bool(r["handled"])
            r["summary"] = screen_text(r["summary"])

    return {"lead": dict(lead), "emails": emails, "replies": replies}


@router.get("/api/sent-today")
def sent_today():
    today = db.today()  # same rule the bot uses to count its daily limit
    with _conn() as c:
        sc = _cols(c, "sent")
        step = "s.step" if "step" in sc else "NULL"
        subj = _pick(sc, "sent_subject", "subject")
        rows = [dict(r) for r in c.execute(
            f"""SELECT s.id, s.lead_id, l.company, l.industry, {step} AS step,
                       {('s.' + subj) if subj else 'NULL'} AS subject
                FROM sent s JOIN leads l ON l.id = s.lead_id
                WHERE s.sent_on = ? AND {db.real('l')} ORDER BY s.id DESC""", (today,))]
    return {"date": today, "emails": rows}
