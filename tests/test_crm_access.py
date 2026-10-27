"""Who sees what in the CRM and the dashboard, and which replies wait for someone to read
them. Run against the real web app (web.py with the CRM installed) and a temporary
database; no email is sent: the CRM's mail sending is replaced by one that fails the test."""
import pytest
from fastapi.testclient import TestClient

import config
import db

PASSWORD = "correct horse battery"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "crm.db"))
    import crm  # imported here, after the test database is chosen: web.py sets up its schema on import
    import web
    crm.ensure_schema()
    web.ensure_schema()
    crm._fails.clear()
    monkeypatch.setattr(crm, "CRM_FROM_EMAIL", "sales@talksasa.co.ke")
    monkeypatch.setattr(crm, "_deliver", lambda msg: pytest.fail("the CRM tried to send an email"))
    monkeypatch.setattr(crm, "_file_in_sent", lambda msg: None)
    return web.app


def add_user(name, email, role):
    import crm
    with crm.conn() as c:
        c.execute("INSERT INTO users (name, email, role, password_hash, must_change) VALUES (?,?,?,?,0)",
                  (name, email, role, crm.hash_password(PASSWORD)))
        return c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


def signed_in(app, email):
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    return client


def add_lead(email, claimed_by=None, status="replied", label="interested", **cols):
    db.add_lead(email.split("@")[1], email.split("@")[1], "school", email, "search")
    lead_id = db.find_lead_by_email(email)["id"]
    sets = dict(status=status, claimed_by=claimed_by, **cols)
    with db.conn() as c:
        c.execute(f"UPDATE leads SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", (*sets.values(), lead_id))
    reply_id = db.add_reply(lead_id, email, "Re: Hi", "Tell me more", label, "a reply", "uid-" + email)
    return lead_id, reply_id


@pytest.fixture
def team(app):
    """An admin, two sales people, and a lead for each case: Asha's, Ben's and unclaimed."""
    ids = {"admin": add_user("Ada", "ada@talksasa.co.ke", "admin"),
           "asha": add_user("Asha", "asha@talksasa.co.ke", "sales"),
           "ben": add_user("Ben", "ben@talksasa.co.ke", "sales")}
    leads = {"asha": add_lead("info@asha-school.com", ids["asha"]),
             "ben": add_lead("info@ben-school.com", ids["ben"]),
             "free": add_lead("info@free-school.com")}
    return {"ids": ids, "leads": leads,
            "admin": signed_in(app, "ada@talksasa.co.ke"), "asha": signed_in(app, "asha@talksasa.co.ke")}


def handled(reply_id):
    with db.conn() as c:
        return c.execute("SELECT COALESCE(handled, 0) h FROM replies WHERE id = ?", (reply_id,)).fetchone()["h"]


def test_sales_get_403_on_the_dashboard_and_its_routes(team):
    lead_id, reply_id = team["leads"]["free"]
    gets = ["/", "/api/all", f"/api/leads/{lead_id}/emails", "/api/sent-today", "/api/crm/report"]
    for path in gets:
        assert team["asha"].get(path, follow_redirects=False).status_code == 403, path
        assert team["admin"].get(path).status_code == 200, path  # admins keep the dashboard
    posts = [(f"/api/leads/{lead_id}/status", {"status": "client"}),
             (f"/api/replies/{reply_id}/handled", {"handled": True})]
    for path, body in posts:
        assert team["asha"].post(path, json=body).status_code == 403, path
    assert db.find_lead_by_email("info@free-school.com")["status"] == "replied"  # nothing changed
    assert handled(reply_id) == 0


def test_sales_get_404_on_a_colleagues_lead(team):
    asha, (ben_lead, ben_reply) = team["asha"], team["leads"]["ben"]
    assert asha.get(f"/api/crm/leads/{ben_lead}").status_code == 404
    for path, body in [(f"/api/crm/leads/{ben_lead}/claim", None), (f"/api/crm/leads/{ben_lead}/release", None),
                       (f"/api/crm/leads/{ben_lead}/notes", {"body": "hi"}),
                       (f"/api/crm/leads/{ben_lead}/status", {"status": "lost"}),
                       (f"/api/crm/leads/{ben_lead}/reply", {"body": "Hello"}),
                       (f"/api/crm/replies/{ben_reply}/done", None)]:
        r = asha.post(path, json=body) if body else asha.post(path)
        assert r.status_code == 404, path
        assert "Ben" not in r.text
    lead = db.find_lead_by_email("info@ben-school.com")
    assert lead["claimed_by"] == team["ids"]["ben"] and lead["status"] == "replied" and handled(ben_reply) == 0
    assert team["admin"].get(f"/api/crm/leads/{ben_lead}").status_code == 200


def test_sales_lists_show_only_unclaimed_and_their_own(team):
    asha, admin, L = team["asha"], team["admin"], team["leads"]
    with db.conn() as c:  # one of Asha's and one of Ben's leads are won (which marks their replies done)
        c.execute("UPDATE leads SET status = 'client' WHERE id IN (?, ?)", (L["asha"][0], L["ben"][0]))
        c.execute("UPDATE replies SET handled = 1 WHERE lead_id IN (?, ?)", (L["asha"][0], L["ben"][0]))
    add_lead("info@asha-open.com", team["ids"]["asha"])
    add_lead("info@ben-open.com", team["ids"]["ben"])

    def emails(client, scope):
        return {l["email"] for l in client.get(f"/api/crm/leads?scope={scope}").json()["leads"]}

    assert emails(asha, "open") == {"info@asha-open.com", "info@free-school.com"}
    assert emails(asha, "mine") == {"info@asha-open.com"}
    assert emails(asha, "closed") == {"info@asha-school.com"}
    assert asha.get("/api/crm/leads?scope=open").json()["counts"] == {"open": 2, "waiting": 2, "unclaimed": 1, "mine": 1, "due": 0}
    assert emails(admin, "open") == {"info@asha-open.com", "info@ben-open.com", "info@free-school.com"}
    assert emails(admin, "closed") == {"info@asha-school.com", "info@ben-school.com"}
    assert [u["name"] for u in asha.get("/api/crm/users").json()["users"]] == ["Asha"]


def test_sales_lead_page_leaves_out_score_reasons_source_and_backup_email(team):
    lead_id, _ = team["leads"]["free"]
    with db.conn() as c:
        c.execute("UPDATE leads SET score = 72, score_reasons = ?, source = 'CBK DCP directory Sep 2026', "
                  "source_email = 'old@cbk-listed.com', phone = '+254 700 000000' WHERE id = ?",
                  ('["+40 licensed lender"]', lead_id))
    team["admin"].post(f"/api/crm/leads/{lead_id}/assign", json={"user_id": team["ids"]["ben"]})
    team["admin"].post(f"/api/crm/leads/{lead_id}/release")
    d = team["asha"].get(f"/api/crm/leads/{lead_id}").json()
    lead = d["lead"]
    assert (lead["company"], lead["email"], lead["phone"], lead["industry"], lead["score"]) == \
        ("free-school.com", "info@free-school.com", "+254 700 000000", "school", 72)
    for hidden in ("score_reasons", "source", "source_email", "source_url", "source_query"):
        assert hidden not in lead
    assert d["events"] == [] and "Ben" not in str(d)  # the history would say who it was assigned to
    assert d["thread"] and d["can_act"] and d["can_reply"]
    full = team["admin"].get(f"/api/crm/leads/{lead_id}").json()["lead"]
    assert full["source_email"] == "old@cbk-listed.com" and full["score_reasons"] == ["+40 licensed lender"]


def test_every_reply_but_a_bounce_waits_and_unsubscribed_leads_cannot_be_emailed(team):
    asha = team["asha"]
    _, not_interested = add_lead("a@nope.com", label="not_interested")
    _, auto = add_lead("b@away.com", status="sent", label="out_of_office")
    unsub_lead, unsub = add_lead("c@stop.com", status="unsubscribed", label="unsubscribe")
    db.unsubscribe("c@stop.com")
    _, bounce = add_lead("d@gone.com", status="bounced", label="bounce")

    rows = {l["email"]: l for l in asha.get("/api/crm/leads?scope=open").json()["leads"]}
    assert all(rows[e]["waiting"] == 1 for e in ("a@nope.com", "b@away.com", "c@stop.com"))
    assert "d@gone.com" not in rows
    assert rows["c@stop.com"]["can_email"] is False and rows["a@nope.com"]["can_email"] is True

    d = asha.get(f"/api/crm/leads/{unsub_lead}").json()
    assert d["can_reply"] is False
    assert asha.post(f"/api/crm/leads/{unsub_lead}/reply", json={"body": "Sorry to see you go"}).status_code == 409
    assert asha.post(f"/api/crm/replies/{unsub}/done").status_code == 200
    assert handled(unsub) == 1 and handled(not_interested) == 0 and handled(auto) == 0 and handled(bounce) == 0
    assert db.is_suppressed("c@stop.com")


def test_replies_marked_handled_by_the_old_rule_wait_again_once(team):
    import crm
    lead_id, unanswered = add_lead("e@old.com", label="not_interested")
    answered_lead, answered = add_lead("f@old.com", label="out_of_office")
    _, bounce = add_lead("g@old.com", label="bounce")
    with crm.conn() as c:
        c.execute("UPDATE replies SET handled = 1")
        c.execute("INSERT INTO crm_messages (lead_id, user_id, status, sent_at) VALUES (?, ?, 'sent', '2999-01-01 00:00:00')",
                  (answered_lead, team["ids"]["asha"]))
        c.execute("DELETE FROM crm_migrations")  # as on a database from before this change
    crm.ensure_schema()
    assert (handled(unanswered), handled(answered), handled(bounce)) == (0, 1, 1)

    team["admin"].post(f"/api/crm/replies/{unanswered}/done")
    crm.ensure_schema()  # starting again doesn't undo what someone marked done
    assert handled(unanswered) == 1


def test_organisations_lists_everyone_emailed_with_the_same_limits_for_sales(team):
    L, ids = team["leads"], team["ids"]
    for name in ("asha", "ben", "free"):
        db.record_sent(L[name][0], f"<{name}@talksasa.co.ke>", "school")
    quiet, _ = add_lead("info@quiet.com", status="sent", label="out_of_office")
    db.record_sent(quiet, "<quiet@talksasa.co.ke>", "school")
    db.record_sent(quiet, "<quiet-2@talksasa.co.ke>", "school")
    by_team, _ = add_lead("info@phoned.com", status="verified", label="out_of_office")
    with db.conn() as c:
        c.execute("INSERT INTO crm_messages (lead_id, user_id, status) VALUES (?, ?, 'sent')", (by_team, ids["admin"]))
        c.execute("UPDATE leads SET score_reasons = '[\"+40 lender\"]', source = 'CBK DCP directory', "
                  "source_email = 'old@cbk-listed.com'")
    never, _ = add_lead("info@never-emailed.com", status="verified", label="out_of_office")

    def orgs(client):
        return {o["id"]: o for o in client.get("/api/crm/organisations").json()["organisations"]}

    sales = orgs(team["asha"])
    assert set(sales) == {L["asha"][0], L["free"][0], quiet, by_team}  # not Ben's, not one never emailed
    assert sales[quiet]["emails"] == 2 and sales[quiet]["replies"] == 0 and sales[quiet]["owner"] is None
    assert sales[by_team]["emails"] == 1 and sales[L["asha"][0]]["mine"] is True
    for o in sales.values():
        for hidden in ("score_reasons", "source", "source_email", "source_url"):
            assert hidden not in o
    assert "Ben" not in str(sales)
    assert set(orgs(team["admin"])) == {L["asha"][0], L["ben"][0], L["free"][0], quiet, by_team}
    assert never not in orgs(team["admin"])


def ids_in(client, scope):
    return {l["id"] for l in client.get(f"/api/crm/leads?scope={scope}").json()["leads"]}


def test_my_leads_show_every_lead_you_handle_until_it_is_won_or_lost(team):
    asha, me = team["asha"], team["ids"]["asha"]
    quiet, quiet_reply = add_lead("info@quiet.com", status="sent", label="not_interested")
    assert asha.post(f"/api/crm/leads/{quiet}/claim").status_code == 200
    asha.post(f"/api/crm/replies/{quiet_reply}/done")      # nothing waiting, never written back
    lost, _ = add_lead("info@gone-cold.com", me, status="lost", label="not_interested")
    assert ids_in(asha, "mine") == {team["leads"]["asha"][0], quiet}
    assert quiet not in ids_in(asha, "open")              # not an open lead, but still yours
    assert ids_in(asha, "closed") == {lost}
    assert asha.get("/api/crm/leads?scope=open").json()["counts"]["mine"] == 2   # the badge counts the same


def test_setting_a_stage_claims_an_unclaimed_lead_and_records_it(team):
    asha, me = team["asha"], team["ids"]["asha"]
    free = team["leads"]["free"][0]
    assert asha.post(f"/api/crm/leads/{free}/status", json={"status": "meeting"}).status_code == 200
    lead = db.find_lead_by_email("info@free-school.com")
    assert lead["claimed_by"] == me and lead["status"] == "meeting" and lead["meeting_at"]
    asha.post(f"/api/crm/leads/{free}/status", json={"status": "client"})
    lead = db.find_lead_by_email("info@free-school.com")
    assert lead["client_at"] and free in ids_in(asha, "closed")   # Asha won it, so it's in her Won and lost
    with db.conn() as c:
        changes = [r["detail"] for r in c.execute("SELECT detail FROM lead_events WHERE lead_id = ? AND "
                                                  "event_type = 'status_changed' ORDER BY id", (free,))]
    assert len(changes) == 2 and all('"by": "crm"' in d for d in changes)


def test_a_won_or_lost_lead_stays_with_whoever_closed_it(team):
    asha, me = team["asha"], team["ids"]["asha"]
    won = team["leads"]["asha"][0]
    asha.post(f"/api/crm/leads/{won}/status", json={"status": "client"})
    r = asha.post(f"/api/crm/leads/{won}/release")
    assert r.status_code == 409 and "admin" in r.json()["detail"]
    assert db.find_lead_by_email("info@asha-school.com")["claimed_by"] == me and won in ids_in(asha, "closed")
    assert team["admin"].post(f"/api/crm/leads/{won}/release").status_code == 200   # an admin still can
    open_lead, _ = add_lead("info@still-open.com", me)
    assert asha.post(f"/api/crm/leads/{open_lead}/release").status_code == 200       # open leads release as before


def test_a_released_lead_is_back_in_waiting_for_anyone_to_claim(team):
    lead_id, reply_id = team["leads"]["free"]
    asha, ben = team["asha"], signed_in(team["admin"].app, "ben@talksasa.co.ke")
    assert ben.post(f"/api/crm/leads/{lead_id}/claim").status_code == 200
    assert ben.post(f"/api/crm/replies/{reply_id}/done").status_code == 200    # Ben answered it
    assert lead_id not in [l["id"] for l in asha.get("/api/crm/leads?scope=open").json()["leads"]]
    waiting_before = asha.get("/api/crm/leads?scope=open").json()["counts"]["waiting"]

    assert ben.post(f"/api/crm/leads/{lead_id}/release").status_code == 200
    d = asha.get("/api/crm/leads?scope=open").json()
    row = next(l for l in d["leads"] if l["id"] == lead_id)
    assert row["claimed_by"] is None and row["waiting"] == 0          # nothing to answer, but nobody has it
    assert d["counts"]["waiting"] == waiting_before + 1 and d["counts"]["unclaimed"] >= 1
    assert lead_id not in [l["id"] for l in ben.get("/api/crm/leads?scope=mine").json()["leads"]]
    assert asha.post(f"/api/crm/leads/{lead_id}/claim").status_code == 200
    assert lead_id in [l["id"] for l in asha.get("/api/crm/leads?scope=mine").json()["leads"]]
