"""An interested reply nobody answers gets one more Telegram alert after STALE_REPLY_HOURS, in
working hours only, and shows as "Answer now" in the CRM. Temporary database; nothing is sent."""
from datetime import datetime, timedelta, timezone

import pytest

import config
import db
import inbox
from test_crm_access import add_lead, add_user, app, signed_in  # noqa: F401 (app is a fixture)

# A Tuesday, 11:00 in Nairobi
TUESDAY = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def replies(app, monkeypatch):
    monkeypatch.setattr(config, "STALE_REPLY_HOURS", 2)
    ids = {}
    for email, label, at in (("info@late.co.ke", "interested", "2026-10-27 05:00:00"),
                             ("info@asks.co.ke", "question", "2026-10-27 04:00:00"),
                             ("info@fresh.co.ke", "interested", "2026-10-27 07:30:00"),
                             ("info@no.co.ke", "not_interested", "2026-10-27 04:00:00")):
        lead_id, reply_id = add_lead(email, label=label)
        with db.conn() as c:
            c.execute("UPDATE replies SET received_at = ? WHERE id = ?", (at, reply_id))
        ids[email] = (lead_id, reply_id)
    return ids


def queued():
    with db.conn() as c:
        return [r["text"] for r in c.execute("SELECT text FROM alert_outbox WHERE sent_at IS NULL ORDER BY id")]


def test_late_interested_replies_and_questions_get_one_more_alert(replies):
    assert inbox.remind_late(TUESDAY) == 2
    texts = queued()
    assert len(texts) == 2 and all("STILL NOT ANSWERED" in t for t in texts)
    assert any("info@late.co.ke" in t and "no answer after 2 hours" in t for t in texts)
    assert inbox.remind_late(TUESDAY) == 0  # only once per reply
    assert len(queued()) == 2


def test_an_answered_or_done_reply_is_left_alone(replies):
    lead_id, _ = replies["info@late.co.ke"]
    with db.conn() as c:
        c.execute("INSERT INTO crm_messages (lead_id, to_email, subject, body, status, sent_at)"
                  " VALUES (?, 'info@late.co.ke', 'Re: Hi', 'Hello', 'sent', '2026-10-27 06:00:00')", (lead_id,))
        c.execute("UPDATE replies SET handled = 1 WHERE id = ?", (replies["info@asks.co.ke"][1],))
    assert inbox.remind_late(TUESDAY) == 0


def test_an_answer_sent_before_the_reply_doesnt_count(replies):
    lead_id, _ = replies["info@late.co.ke"]
    with db.conn() as c:
        c.execute("INSERT INTO crm_messages (lead_id, to_email, subject, body, status, sent_at)"
                  " VALUES (?, 'info@late.co.ke', 'Hi', 'Hello', 'sent', '2026-10-26 09:00:00')", (lead_id,))
    assert [r["from_email"] for r in inbox.late_replies(TUESDAY)] == ["info@late.co.ke", "info@asks.co.ke"]


@pytest.mark.parametrize("when", [
    datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc),  # 20:00 Nairobi
    datetime(2026, 10, 31, 8, 0, tzinfo=timezone.utc),   # Saturday
])
def test_no_second_alert_outside_working_hours(replies, when):
    assert inbox.remind_late(when) == 0
    assert queued() == []


def test_the_crm_marks_late_replies(app, monkeypatch):
    monkeypatch.setattr(config, "STALE_REPLY_HOURS", 2)
    add_user("Ada", "ada@talksasa.co.ke", "admin")
    admin = signed_in(app, "ada@talksasa.co.ke")
    now = datetime.now(timezone.utc)
    for email, label, hours_ago in (("info@late.co.ke", "interested", 3), ("info@fresh.co.ke", "interested", 1),
                                    ("info@no.co.ke", "not_interested", 5)):
        _, reply_id = add_lead(email, label=label)
        with db.conn() as c:
            c.execute("UPDATE replies SET received_at = ? WHERE id = ?",
                      ((now - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S"), reply_id))
    leads = admin.get("/api/crm/leads").json()["leads"]
    assert {l["email"] for l in leads if l["late"]} == {"info@late.co.ke"}
