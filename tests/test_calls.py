"""The call list: organisations that got every email and never wrote back, phoned by the sales
team. Temporary database, the real web app; no email is sent and nobody is called."""
from datetime import date, timedelta

import pytest

import config
import db
from test_crm_access import add_user, app, signed_in  # noqa: F401 (app is a fixture)

LONG_AGO = (date.today() - timedelta(days=10)).isoformat()


def emailed_out(email, phone="+254712345678", emails=3, last=LONG_AGO, **cols):
    """A lead the bot has finished with: `emails` sent, the last on `last`, no reply."""
    db.add_lead(email.split("@")[1], email.split("@")[1], "sacco", email, "search", phone=phone)
    lead_id = db.find_lead_by_email(email)["id"]
    for n in range(emails):
        db.record_sent(lead_id, f"<{lead_id}.{n}@x>", "sacco", "Subject", "Body")
    sets = dict(status="sent", **cols)
    with db.conn() as c:
        c.execute("UPDATE sent SET sent_on = ? WHERE lead_id = ?", (last, lead_id))
        c.execute(f"UPDATE leads SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", (*sets.values(), lead_id))
    return lead_id


@pytest.fixture
def team(app, monkeypatch):
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", True)
    ids = {"admin": add_user("Ada", "ada@talksasa.co.ke", "admin"),
           "asha": add_user("Asha", "asha@talksasa.co.ke", "sales"),
           "ben": add_user("Ben", "ben@talksasa.co.ke", "sales")}
    return {"ids": ids, "admin": signed_in(app, "ada@talksasa.co.ke"),
            "asha": signed_in(app, "asha@talksasa.co.ke"), "ben": signed_in(app, "ben@talksasa.co.ke")}


def listed(client):
    return [l["email"] for l in client.get("/api/crm/calls").json()["leads"]]


def test_only_leads_the_bot_has_finished_with_and_that_never_replied(team):
    emailed_out("info@ready.co.ke")
    emailed_out("info@nophone.co.ke", phone=None)
    emailed_out("info@followups-left.co.ke", emails=2)
    emailed_out("info@too-soon.co.ke", last=date.today().isoformat())
    emailed_out("info@team-emailed.co.ke", crm_emailed_at="2026-10-01 10:00:00")
    emailed_out("me@test.co.ke", source="manual")  # a test lead
    replied = emailed_out("info@replied.co.ke")
    db.add_reply(replied, "info@replied.co.ke", "Re: Subject", "No thanks", "not_interested", "", "uid-1")
    away = emailed_out("info@away.co.ke")
    db.add_reply(away, "info@away.co.ke", "Auto: away", "Out of office", "out_of_office", "", "uid-2")
    opted = emailed_out("info@opted.co.ke")
    db.unsubscribe("info@opted.co.ke")
    assert sorted(listed(team["asha"])) == ["info@away.co.ke", "info@ready.co.ke"]
    assert opted  # unsubscribed, so never on the list


def test_with_follow_ups_off_one_email_is_enough(team, monkeypatch):
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", False)
    emailed_out("info@one.co.ke", emails=1)
    assert listed(team["asha"]) == ["info@one.co.ke"]


def test_sales_claim_a_call_lead_and_a_colleague_no_longer_sees_it(team):
    lead_id = emailed_out("info@ready.co.ke")
    assert team["asha"].post(f"/api/crm/leads/{lead_id}/claim").status_code == 200
    assert listed(team["ben"]) == []
    assert team["ben"].post(f"/api/crm/leads/{lead_id}/calls", json={"outcome": "no_answer"}).status_code == 404


def test_a_lead_off_the_list_still_cant_be_claimed_before_it_replies(team):
    lead_id = emailed_out("info@followups-left.co.ke", emails=2)
    assert team["asha"].post(f"/api/crm/leads/{lead_id}/claim").status_code == 409
    assert team["asha"].post(f"/api/crm/leads/{lead_id}/calls", json={"outcome": "no_answer"}).status_code == 409


def test_no_answer_keeps_it_on_the_list_and_logging_claims_it(team):
    lead_id = emailed_out("info@ready.co.ke")
    r = team["asha"].post(f"/api/crm/leads/{lead_id}/calls", json={"outcome": "no_answer", "note": "  rang twice "})
    assert r.status_code == 200
    lead = team["asha"].get("/api/crm/calls").json()["leads"][0]
    assert (lead["calls"], lead["last_outcome"], lead["last_note"], lead["mine"]) == (1, "no_answer", "rang twice", True)
    assert db.find_lead_by_email("info@ready.co.ke")["status"] == "sent"


def test_interested_makes_it_an_open_lead(team):
    lead_id = emailed_out("info@ready.co.ke")
    team["asha"].post(f"/api/crm/leads/{lead_id}/calls", json={"outcome": "interested", "note": "Wants prices"})
    assert db.find_lead_by_email("info@ready.co.ke")["status"] == "replied"
    assert listed(team["asha"]) == []
    mine = team["asha"].get("/api/crm/leads?scope=mine").json()["leads"]
    assert [l["id"] for l in mine] == [lead_id]
    assert team["asha"].post(f"/api/crm/leads/{lead_id}/status", json={"status": "meeting"}).status_code == 200
    report = team["admin"].get("/api/crm/report").json()
    assert report["total"]["interested"] == 1
    assert report["calls"]["interested"] == 1


def test_not_interested_closes_it_and_never_opts_them_out(team):
    lead_id = emailed_out("info@ready.co.ke")
    team["asha"].post(f"/api/crm/leads/{lead_id}/calls", json={"outcome": "not_interested"})
    lead = db.find_lead_by_email("info@ready.co.ke")
    assert (lead["status"], lead["lost_reason"]) == ("lost", "not_interested")
    assert not db.blocked("info@ready.co.ke")  # email opt-outs come only from what they write


def test_a_wrong_number_comes_off_the_lead_and_into_the_note(team):
    lead_id = emailed_out("info@ready.co.ke")
    team["asha"].post(f"/api/crm/leads/{lead_id}/calls", json={"outcome": "wrong_number"})
    assert db.find_lead_by_email("info@ready.co.ke")["phone"] is None
    with db.conn() as c:
        assert c.execute("SELECT note FROM crm_calls").fetchone()["note"] == "Wrong number: +254712345678"
    assert listed(team["asha"]) == []


def test_an_unknown_outcome_is_refused(team):
    lead_id = emailed_out("info@ready.co.ke")
    assert team["asha"].post(f"/api/crm/leads/{lead_id}/calls", json={"outcome": "voicemail"}).status_code == 400
