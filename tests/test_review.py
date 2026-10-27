"""Leads found by web search wait for an admin's check before their first email: the search
that found them only guesses their industry. Temporary database, fake mail server, the real
web app; no email is sent."""
import pytest

import config
import db
import discover
import personalise
import ranking
import sender
from test_crm_access import add_user, signed_in


@pytest.fixture
def found(tmp_path, monkeypatch):
    """A lead found by a church search and still pending, and a CSV lead (no review needed)."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 10)
    monkeypatch.setattr(config, "SEND_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", False)
    monkeypatch.setattr(ranking, "fill_years", lambda *a, **k: 0)
    monkeypatch.setattr(sender, "save_to_sent", lambda *a: True)
    monkeypatch.setattr(sender.notify, "send", lambda text: None)
    db.init()
    db.add_lead("Grand Hotel", "grandhotel.co.ke", "church", "info@grandhotel.co.ke", "search",
                "church {town} Kenya contact us", "https://grandhotel.co.ke", review_status="pending")
    db.add_lead("Umoja Sacco", "umojasacco.co.ke", "sacco", "info@umojasacco.co.ke", "csv")
    for email in ("info@grandhotel.co.ke", "info@umojasacco.co.ke"):
        db.set_status(db.find_lead_by_email(email)["id"], "verified", "verify")
    return db.find_lead_by_email("info@grandhotel.co.ke")["id"]


def test_pending_lead_is_never_planned_or_claimed(found, smtp):
    assert [l["email"] for l in ranking.waiting()] == ["info@umojasacco.co.ke"]
    assert "review is pending" in db.claim_send(found, 0, 10)
    sender.run()
    assert smtp["delivered"] == ["info@umojasacco.co.ke"]


def test_discovered_leads_are_held_for_review(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    monkeypatch.setattr(discover, "look_at", lambda url: ("Grand Hotel | Home", True))
    monkeypatch.setattr(discover.scraper, "scrape_contacts", lambda home: (["info@grandhotel.co.ke"], []))
    monkeypatch.setattr(discover, "save_csv", lambda row: None)
    assert discover.consider("https://grandhotel.co.ke", "church", "church Nairobi", set())
    assert db.find_lead_by_email("info@grandhotel.co.ke")["review_status"] == "pending"


def test_run_without_a_category_searches_only_approved_industries(monkeypatch):
    searched = []
    monkeypatch.setattr(discover, "known_domains", lambda: set())
    monkeypatch.setattr(discover, "discover_category", lambda cat, limit, seen: searched.append(cat) or [])
    discover.run()
    assert searched and all(personalise.approved({"industry": c}) for c in searched)
    assert "hotel" not in searched and "hospitality" not in searched


@pytest.fixture
def crm_app(found, monkeypatch):
    import crm
    import web
    crm.ensure_schema()
    web.ensure_schema()
    crm._fails.clear()
    monkeypatch.setattr(crm, "_deliver", lambda msg: pytest.fail("the CRM tried to send an email"))
    add_user("Ada", "ada@talksasa.co.ke", "admin")
    add_user("Asha", "asha@talksasa.co.ke", "sales")
    return {"admin": signed_in(web.app, "ada@talksasa.co.ke"), "asha": signed_in(web.app, "asha@talksasa.co.ke"),
            "lead": found}


def test_admin_sees_the_queue_and_sales_cannot(crm_app):
    queue = crm_app["admin"].get("/api/crm/review").json()
    assert [l["email"] for l in queue["leads"]] == ["info@grandhotel.co.ke"]
    assert queue["leads"][0]["source_query"] == "church {town} Kenya contact us"
    assert crm_app["admin"].get("/api/crm/leads?scope=open").json()["counts"]["review"] == 1
    assert crm_app["asha"].get("/api/crm/review").status_code == 403
    assert crm_app["asha"].post(f"/api/crm/review/{crm_app['lead']}",
                                json={"decision": "approve", "industry": "sacco"}).status_code == 403


def test_approved_lead_goes_out_with_its_corrected_industry(crm_app, smtp):
    r = crm_app["admin"].post(f"/api/crm/review/{crm_app['lead']}", json={"decision": "approve", "industry": "sacco"})
    assert r.status_code == 200
    lead = db.find_lead_by_email("info@grandhotel.co.ke")
    assert (lead["review_status"], lead["industry"]) == ("approved", "sacco")
    sender.run()
    assert "info@grandhotel.co.ke" in smtp["delivered"]
    with db.conn() as c:
        assert c.execute("SELECT template FROM sent WHERE lead_id = ?", (crm_app["lead"],)).fetchone()[0] == "sacco"
    assert crm_app["admin"].get("/api/crm/review").json()["leads"] == []


def test_rejected_lead_is_never_emailed_and_not_shown_as_a_lost_deal(crm_app, smtp):
    assert crm_app["admin"].post(f"/api/crm/review/{crm_app['lead']}", json={"decision": "reject"}).status_code == 200
    assert db.find_lead_by_email("info@grandhotel.co.ke")["status"] == "lost"
    sender.run()
    assert "info@grandhotel.co.ke" not in smtp["delivered"]
    closed = crm_app["admin"].get("/api/crm/leads?scope=closed").json()["leads"]
    assert not closed
    searches = crm_app["admin"].get("/api/crm/report").json()["searches"]
    assert searches[0]["name"] == "church {town} Kenya contact us" and searches[0]["rejected"] == 1


def test_approve_needs_a_real_industry_and_only_once(crm_app):
    url = f"/api/crm/review/{crm_app['lead']}"
    assert crm_app["admin"].post(url, json={"decision": "approve", "industry": "made_up"}).status_code == 400
    assert crm_app["admin"].post(url, json={"decision": "approve", "industry": "church"}).status_code == 200
    assert crm_app["admin"].post(url, json={"decision": "reject"}).status_code == 409


def test_search_key_groups_towns_and_list_pages():
    import crm
    assert crm._search_key("SACCO society Mombasa Kenya contact us") == "SACCO society {town} Kenya contact us"
    assert crm._search_key("listed on https://www.ventures.co.ke/portfolio") == "listed on ventures.co.ke"
