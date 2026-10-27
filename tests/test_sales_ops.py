"""The CRM's daily to-do tools: a next step on each lead, a reason when a deal is lost, a
person correcting a reply's label, and the admin's health strip. The real web app and a
temporary database; no email is sent."""
import db
from test_crm_access import add_lead, app, team  # noqa: F401  (pytest fixtures)


def lead_row(lead_id):
    with db.conn() as c:
        return c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()


# --- next step

def test_owner_sets_a_next_step_and_it_shows_as_due(team):
    asha, (lead_id, _) = team["asha"], team["leads"]["asha"]
    r = asha.post(f"/api/crm/leads/{lead_id}/next", json={"action": "Call about pricing", "due": "2026-01-05"})
    assert r.status_code == 200
    mine = asha.get("/api/crm/leads?scope=mine").json()
    row = next(l for l in mine["leads"] if l["id"] == lead_id)
    assert row["next_action"] == "Call about pricing" and row["due"]  # 5 Jan is long past
    assert mine["counts"]["due"] == 1
    assert asha.get(f"/api/crm/leads/{lead_id}").json()["lead"]["next_action_due"] == "2026-01-05"


def test_clearing_the_next_step(team):
    asha, (lead_id, _) = team["asha"], team["leads"]["asha"]
    asha.post(f"/api/crm/leads/{lead_id}/next", json={"action": "Send rates", "due": "2026-01-05"})
    asha.post(f"/api/crm/leads/{lead_id}/next", json={"action": ""})
    assert (lead_row(lead_id)["next_action"], lead_row(lead_id)["next_action_due"]) == (None, None)


def test_next_step_rules(team):
    asha = team["asha"]
    ben_lead, _ = team["leads"]["ben"]
    assert asha.post(f"/api/crm/leads/{ben_lead}/next", json={"action": "x"}).status_code == 404  # a colleague's
    own, _ = team["leads"]["asha"]
    assert asha.post(f"/api/crm/leads/{own}/next", json={"action": "x", "due": "next week"}).status_code == 400


# --- lost reason

def test_lost_needs_a_reason(team):
    asha, (lead_id, _) = team["asha"], team["leads"]["asha"]
    assert asha.post(f"/api/crm/leads/{lead_id}/status", json={"status": "lost"}).status_code == 400
    assert asha.post(f"/api/crm/leads/{lead_id}/status", json={"status": "lost", "reason": "other"}).status_code == 400
    r = asha.post(f"/api/crm/leads/{lead_id}/status", json={"status": "lost", "reason": "has_provider"})
    assert r.status_code == 200
    assert (lead_row(lead_id)["status"], lead_row(lead_id)["lost_reason"]) == ("lost", "has_provider")


def test_other_reason_keeps_its_words_and_the_report_counts_reasons(team):
    asha, (lead_id, _) = team["asha"], team["leads"]["asha"]
    asha.post(f"/api/crm/leads/{lead_id}/status", json={"status": "lost", "reason": "other", "note": "Closed down"})
    assert lead_row(lead_id)["lost_reason"] == "other: Closed down"
    report = team["admin"].get("/api/crm/report").json()
    assert report["lost_reasons"] == [{"name": "Other", "n": 1}]


def test_reopening_a_lost_lead_clears_its_reason(team):
    admin, (lead_id, _) = team["admin"], team["leads"]["asha"]
    admin.post(f"/api/crm/leads/{lead_id}/status", json={"status": "lost", "reason": "price"})
    admin.post(f"/api/crm/leads/{lead_id}/status", json={"status": "replied"})
    assert lead_row(lead_id)["lost_reason"] is None


# --- label correction

def test_correcting_a_label_keeps_its_history(team):
    asha, (lead_id, reply_id) = team["asha"], team["leads"]["asha"]
    assert asha.post(f"/api/crm/replies/{reply_id}/label", json={"label": "question"}).status_code == 200
    with db.conn() as c:
        assert c.execute("SELECT label FROM replies WHERE id = ?", (reply_id,)).fetchone()[0] == "question"
        history = c.execute("SELECT old_label, new_label FROM reply_labels WHERE reply_id = ?", (reply_id,)).fetchall()
    assert [tuple(h) for h in history] == [("interested", "question")]


def test_an_auto_reply_that_was_a_person_moves_the_lead_to_replied(team):
    lead_id, reply_id = add_lead("info@away.com", team["ids"]["asha"], status="sent", label="out_of_office")
    assert team["asha"].post(f"/api/crm/replies/{reply_id}/label", json={"label": "interested"}).status_code == 200
    assert lead_row(lead_id)["status"] == "replied"  # no more follow-ups for it


def test_opt_outs_and_bounces_are_not_relabelled(team):
    _, reply_id = add_lead("info@stop.co.ke", team["ids"]["asha"], status="unsubscribed", label="unsubscribe")
    assert team["asha"].post(f"/api/crm/replies/{reply_id}/label", json={"label": "interested"}).status_code == 409
    _, own_reply = team["leads"]["asha"]
    assert team["asha"].post(f"/api/crm/replies/{own_reply}/label", json={"label": "unsubscribe"}).status_code == 400


def test_sales_cannot_relabel_a_colleagues_reply(team):
    _, ben_reply = team["leads"]["ben"]
    assert team["asha"].post(f"/api/crm/replies/{ben_reply}/label", json={"label": "question"}).status_code == 404


# --- health strip

def test_health_is_for_admins_and_names_each_check(team):
    assert team["asha"].get("/api/crm/health").status_code == 403
    items = {i["key"]: i for i in team["admin"].get("/api/crm/health").json()["items"]}
    assert {"inbox", "bounces", "sent"} <= set(items)
    assert all(i["level"] in ("ok", "warn", "bad") for i in items.values())
