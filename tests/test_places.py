"""Google Places as a discovery source: off without a key, held to a monthly cap, and its
businesses go through the same checks as web search results. Google is never called here."""
import pytest
import requests

import config
import db
import discover
import websearch


class Answer:
    def __init__(self, places):
        self.places = places

    def raise_for_status(self):
        pass

    def json(self):
        return {"places": self.places}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(discover, "CSV_PATH", tmp_path / "discovered.csv")
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "test-key")
    monkeypatch.setattr(config, "PLACES_MONTHLY_CAP", 2)
    db.init()
    posts = []

    def post(url, json, headers, timeout):
        posts.append((json, headers))
        return Answer([
            {"displayName": {"text": "Umoja Sacco Society"}, "websiteUri": "https://umojasacco.co.ke/",
             "nationalPhoneNumber": "0712 345678"},
            {"displayName": {"text": "No Website Sacco"}},
        ])

    monkeypatch.setattr(websearch.requests, "post", post)
    return posts


def test_no_key_means_no_call(world, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "")
    assert websearch.places("sacco in Nakuru, Kenya") == []
    assert world == []
    assert discover.discover_from_places("sacco", 5, set()) == []


def test_only_businesses_with_a_website_and_only_the_fields_needed(world):
    results = websearch.places("sacco in Nakuru, Kenya")
    assert results == [{"title": "Umoja Sacco Society", "href": "https://umojasacco.co.ke/", "body": "",
                        "phone": "0712 345678"}]
    body, headers = world[0]
    assert (body["textQuery"], body["regionCode"]) == ("sacco in Nakuru, Kenya", "KE")
    assert "websiteUri" in headers["X-Goog-FieldMask"] and "reviews" not in headers["X-Goog-FieldMask"]


def test_the_monthly_cap_stops_the_calls(world):
    for _ in range(3):
        websearch.places("sacco in Nakuru, Kenya")
    assert len(world) == 2
    assert "cap of 2" in websearch.why_not_places()


def test_a_failed_call_still_counts_and_gives_nothing(world, monkeypatch):
    def broken(*a, **k):
        raise requests.ConnectionError("https://places.googleapis.com/?key=test-key")
    monkeypatch.setattr(websearch.requests, "post", broken)
    assert websearch.places("sacco in Nakuru, Kenya") == []
    assert websearch.calls("places") == 1


def test_a_business_becomes_a_lead_with_its_listed_name_and_phone(world, monkeypatch):
    monkeypatch.setattr(discover, "look_at", lambda home: ("Home", True))
    monkeypatch.setattr(discover.scraper, "scrape_contacts", lambda home: (["info@umojasacco.co.ke"], ["+254202345678"]))
    monkeypatch.setattr(discover.random, "choice", lambda towns: "Nakuru")
    added = discover.discover_from_places("sacco", 5, set())
    assert [r["company"] for r in added] == ["Umoja Sacco Society"]
    lead = db.find_lead_by_email("info@umojasacco.co.ke")
    assert (lead["phone"], lead["review_status"], lead["source_query"]) == (
        "+254712345678", "pending", "places: sacco in Nakuru, Kenya")


def test_categories_are_asked_for_in_plain_words():
    assert discover.places_query("school", "Thika") == "private school in Thika, Kenya"
    assert discover.places_query("real_estate", "Thika") == "real estate in Thika, Kenya"
