"""Web search for discover.py. Tavily basic search when it is set up, DuckDuckGo otherwise.

  SEARCH_PROVIDER=tavily     try Tavily first (anything else: DuckDuckGo only)
  TAVILY_API_KEY             Tavily's key; without it DuckDuckGo answers
  TAVILY_MONTHLY_CAP         Tavily calls allowed per calendar month (UTC); after that
                             DuckDuckGo answers until the next month

Every call is counted in search_usage by month and provider. A Tavily call counts even
when it fails, so the cap is never passed. Results come back as DuckDuckGo gives them:
[{"title", "href", "body"}].

  GOOGLE_PLACES_API_KEY      Google Places (New) key: places() finds businesses on Google
                             Maps with their website and phone. Off without it
  PLACES_MONTHLY_CAP         Places calls allowed per calendar month (UTC)
"""
from datetime import datetime, timezone

import requests

import config
import db

SCHEMA = """CREATE TABLE IF NOT EXISTS search_usage (
    month TEXT NOT NULL,
    provider TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, provider)
)"""


def this_month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def calls(provider, month=None):
    with db.conn() as c:
        c.execute(SCHEMA)
        row = c.execute("SELECT calls FROM search_usage WHERE month=? AND provider=?",
                        (month or this_month(), provider)).fetchone()
    return row[0] if row else 0


def count(provider):
    with db.conn() as c:
        c.execute(SCHEMA)
        c.execute("INSERT INTO search_usage (month, provider, calls) VALUES (?, ?, 1)"
                  " ON CONFLICT (month, provider) DO UPDATE SET calls = search_usage.calls + 1",
                  (this_month(), provider))


def why_not_tavily():
    """Why Tavily won't be asked, or None if it will."""
    if config.SEARCH_PROVIDER != "tavily":
        return f"SEARCH_PROVIDER is {config.SEARCH_PROVIDER or 'not set'}"
    if not config.TAVILY_API_KEY:
        return "no TAVILY_API_KEY"
    if calls("tavily") >= config.TAVILY_MONTHLY_CAP:
        return f"this month's cap of {config.TAVILY_MONTHLY_CAP} Tavily calls is reached"
    return None


def tavily(query, max_results):
    from tavily import TavilyClient
    count("tavily")
    answer = TavilyClient(api_key=config.TAVILY_API_KEY).search(
        query, search_depth="basic", max_results=max_results)
    return [{"title": r.get("title") or "", "href": r.get("url") or "", "body": r.get("content") or ""}
            for r in answer.get("results") or []]


def duckduckgo(query, max_results):
    from ddgs import DDGS
    count("duckduckgo")
    return DDGS().text(query, max_results=max_results) or []


def search(query, max_results=10):
    """[{"title", "href", "body"}] for the query, or [] if no provider could answer."""
    reason = why_not_tavily()
    if reason is None:
        try:
            results = tavily(query, max_results)
            print(f"    (tavily answered: {len(results)} results)")
            return results
        except Exception as e:
            # the message is the error's type only: some carry the request, key included
            reason = f"Tavily failed ({type(e).__name__})"
    try:
        results = duckduckgo(query, max_results)
        print(f"    (duckduckgo answered: {len(results)} results; {reason})")
        return results
    except Exception as e:
        print(f"    search failed: {e}")
        return []


PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
# Only the fields discover needs. Website and phone put the call in Google's "Pro" price band.
PLACES_FIELDS = "places.displayName,places.websiteUri,places.nationalPhoneNumber,places.internationalPhoneNumber"


def why_not_places():
    """Why Google Places won't be asked, or None if it will."""
    if not config.GOOGLE_PLACES_API_KEY:
        return "no GOOGLE_PLACES_API_KEY"
    if calls("places") >= config.PLACES_MONTHLY_CAP:
        return f"this month's cap of {config.PLACES_MONTHLY_CAP} Places calls is reached"
    return None


def places(query, max_results=20):
    """[{"title", "href", "body", "phone"}] for businesses in Kenya matching the query
    ("sacco in Nakuru"), the ones with a website only, or [] when Places is off or fails.
    title is the business's name on Google Maps and phone the number listed there."""
    reason = why_not_places()
    if reason:
        print(f"    (Google Places not asked: {reason})")
        return []
    count("places")  # counted before the call, so a failed call still counts toward the cap
    try:
        r = requests.post(PLACES_URL, timeout=20, json={
            "textQuery": query, "regionCode": "KE", "pageSize": min(20, max_results)},
            headers={"X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY, "X-Goog-FieldMask": PLACES_FIELDS})
        r.raise_for_status()
        found = r.json().get("places") or []
    except (requests.RequestException, ValueError) as e:
        # the message is the error's type only: a request error can carry the key
        print(f"    Google Places failed ({type(e).__name__})")
        return []
    out = [{"title": (p.get("displayName") or {}).get("text") or "", "href": p["websiteUri"], "body": "",
            "phone": p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber") or ""}
           for p in found if p.get("websiteUri")]
    print(f"    (Google Places answered: {len(found)} places, {len(out)} with a website)")
    return out
