"""LeadBot web dashboard.

Run from the leadbot folder:
    pip install fastapi uvicorn
    uvicorn web:app --port 8000
Then open http://localhost:8000

Reads the same database as the bot (DB_PATH in .env, default leads.db).
"""
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import config
import db

app = FastAPI(title="LeadBot")
from crm import install as install_crm, current_user, _require_owner_or_free, screen_text
install_crm(app)
from web_extra import router as extra_router
app.include_router(extra_router)
HERE = Path(__file__).parent
PAGE = HERE / "static" / "index.html"
HEARTBEAT = HERE / "last_check.txt"
ALLOWED_STATUSES = {"new", "verified", "invalid", "sent", "replied", "meeting", "client",
                    "bounced", "unsubscribed", "lost"}


def conn():
    return db.conn()


def columns(c, table):
    return db.columns(c, table)


def ensure_schema():
    """Add the one column the dashboard needs, without touching anything else."""
    db.init()
    with conn() as c:
        if "handled" not in columns(c, "replies"):
            # every new reply waits for someone to read it (crm.py decides what shows as waiting)
            c.execute("ALTER TABLE replies ADD COLUMN handled INTEGER DEFAULT 0")


ensure_schema()


@app.get("/", response_class=HTMLResponse)
def index():
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>"
            "</head><body>" + PAGE.read_text(encoding="utf-8") + "</body></html>")


@app.get("/api/all")
def all_data():
    with conn() as c:
        sent_cols = columns(c, "sent")
        step_expr = "MAX(s.step)" if "step" in sent_cols else "COUNT(s.id)"

        leads = [dict(r) for r in c.execute(f"""
            SELECT l.id, l.company, l.email, l.industry, l.status,
                   COALESCE({step_expr}, 0) AS step,
                   COALESCE(MAX(s.sent_on), substr(l.created_at, 1, 10)) AS last
            FROM leads l LEFT JOIN sent s ON s.lead_id = l.id
            WHERE {db.real('l')}
            GROUP BY l.id ORDER BY last DESC, l.id DESC""")]

        replies = [dict(r) for r in c.execute(f"""
            SELECT r.id, r.lead_id, l.company, l.industry, r.from_email, r.subject, r.label,
                   r.summary, r.body, r.received_at, COALESCE(r.handled, 0) AS handled
            FROM replies r LEFT JOIN leads l ON l.id = r.lead_id
            WHERE {db.real('l')}
            ORDER BY r.id DESC LIMIT 500""")]
        for r in replies:
            r["handled"] = bool(r["handled"])
            r["summary"] = screen_text(r["summary"])
            # SQLite CURRENT_TIMESTAMP is UTC without a zone; mark it so the browser converts it
            if r["received_at"] and "T" not in r["received_at"]:
                r["received_at"] = r["received_at"].replace(" ", "T") + "Z"

        today = date.fromisoformat(db.today())  # the bot's own day, as it counts the daily limit
        start = (today - timedelta(days=29)).isoformat()
        sent_by_day = {r["d"]: r["n"] for r in c.execute(
            "SELECT s.sent_on d, COUNT(*) n FROM sent s LEFT JOIN leads l ON l.id = s.lead_id"
            f" WHERE s.sent_on >= ? AND {db.real('l')} GROUP BY s.sent_on", (start,))}
        rep_by_day = {r["d"]: r["n"] for r in c.execute(
            f"""SELECT substr(r.received_at, 1, 10) d, COUNT(*) n
               FROM replies r LEFT JOIN leads l ON l.id = r.lead_id
               WHERE r.label NOT IN ('bounce', 'out_of_office') AND substr(r.received_at, 1, 10) >= ?
                 AND {db.real('l')}
               GROUP BY d""", (start,))}
        daily = []
        for i in range(29, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            daily.append({"day": d, "sent": sent_by_day.get(d, 0), "replies": rep_by_day.get(d, 0)})

        status_counts = {r["status"]: r["n"] for r in c.execute(
            f"SELECT status, COUNT(*) n FROM leads WHERE {db.real()} GROUP BY status")}

        niches = [dict(r) for r in c.execute(f"""
            SELECT COALESCE(NULLIF(l.industry, ''), 'default') AS industry,
                   COUNT(DISTINCT l.id) AS leads,
                   COUNT(DISTINCT s.lead_id) AS sent,
                   COUNT(DISTINCT CASE WHEN s.lead_id IS NOT NULL AND l.status != 'bounced' THEN l.id END) AS delivered,
                   COUNT(DISTINCT CASE WHEN r.label NOT IN ('bounce','out_of_office') THEN r.lead_id END) AS replies,
                   COUNT(DISTINCT CASE WHEN r.label = 'interested' OR l.status IN ('meeting','client') THEN l.id END) AS interested
            FROM leads l
            LEFT JOIN sent s ON s.lead_id = l.id
            LEFT JOIN replies r ON r.lead_id = l.id
            WHERE {db.real('l')}
            GROUP BY 1 ORDER BY leads DESC, industry""")]  # ties by name, on any database

    last_check = None
    if HEARTBEAT.exists():
        last_check = datetime.utcfromtimestamp(HEARTBEAT.stat().st_mtime).isoformat() + "Z"

    return {
        "leads": leads, "replies": replies, "daily": daily,
        "statusCounts": status_counts, "niches": niches,
        # the bot's own count against its daily limit, so emails to manual leads are in it
        "bot": {"last_check": last_check, "daily_limit": config.daily_limit(),
                "sent_today": db.sent_today()},
    }


class Handled(BaseModel):
    handled: bool


class Status(BaseModel):
    status: str


@app.post("/api/replies/{reply_id}/handled")
def set_handled(reply_id: int, body: Handled, user=Depends(current_user)):
    with conn() as c:
        reply = c.execute("SELECT lead_id FROM replies WHERE id = ?", (reply_id,)).fetchone()
        if not reply:
            raise HTTPException(404, "Reply not found")
        # same rule as the CRM: a claimed lead is only changed by its owner or an admin
        lead = c.execute("SELECT claimed_by FROM leads WHERE id = ?", (reply["lead_id"],)).fetchone()
        if lead:
            _require_owner_or_free(lead, user)
        c.execute("UPDATE replies SET handled = ? WHERE id = ?", (int(body.handled), reply_id))
    return {"ok": True}


@app.post("/api/leads/{lead_id}/status")
def set_status(lead_id: int, body: Status, user=Depends(current_user)):
    if body.status not in ALLOWED_STATUSES:
        raise HTTPException(400, f"Unknown status: {body.status}")
    with conn() as c:
        lead = c.execute("SELECT claimed_by FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not lead:
        raise HTTPException(404, "Lead not found")
    _require_owner_or_free(lead, user)
    # db.set_status also logs the change and stamps meeting_at / client_at
    if not db.set_status(lead_id, body.status, "dashboard"):
        raise HTTPException(404, "Lead not found")
    return {"ok": True}
