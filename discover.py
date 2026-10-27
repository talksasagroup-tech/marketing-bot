"""Finds new companies with a web search (websearch.py: Tavily, or DuckDuckGo) and adds them as leads.

  python main.py discover                  go through PRIORITY order (approved industries only)
  python main.py discover sacco --max 5    one category, stop after 5 new leads

Each query is run with a random town from TOWNS in place of {town}.
Some categories also have LIST_QUERIES: searches for pages that list many companies
(accelerator and incubator portfolios). The company sites linked from those are visited too.
Every lead found is also appended to discovered.csv so you can review it in Excel. Leads found
here wait for an admin to confirm their industry in the CRM (To review) before the first email.
"""
import csv
import random
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import db
import personalise
import scraper
import websearch

TOWNS = ["Nairobi", "Mombasa", "Kisumu", "Nakuru", "Eldoret", "Thika", "Nyeri", "Machakos"]

# Edit these freely. 3-5 queries per category; {town} is filled in from TOWNS.
QUERIES = {
    "sacco": [
        "SACCO society {town} Kenya contact us",
        "savings and credit cooperative {town} email",
        "licensed deposit taking SACCO {town}",
        "teachers OR matatu SACCO {town} Kenya contacts",
    ],
    "microfinance": [
        "microfinance institution {town} Kenya contact us",
        "micro lender loans {town} email",
        "credit company business loans {town} Kenya",
    ],
    "school": [
        "private primary school {town} Kenya contact us",
        "secondary school {town} admissions email",
        "academy school {town} Kenya contacts",
        "international school {town} Kenya",
    ],
    "college": [
        "college {town} Kenya admissions contact us",
        "technical training institute {town} email",
        "medical training college {town} Kenya",
    ],
    "church": [
        "church {town} Kenya contact us",
        "ministries church {town} email office",
        "cathedral OR parish {town} Kenya contacts",
    ],
    "chama": [
        "investment group chama {town} Kenya contact",
        "self help group investment club {town}",
        "chama investment company {town} Kenya email",
    ],
    "health": [
        "private hospital {town} Kenya contact us",
        "medical clinic {town} email appointments",
        "dental clinic {town} Kenya contacts",
        "pharmacy chemist {town} Kenya contact",
    ],
    "transport": [
        "bus company {town} Kenya booking contact",
        "shuttle services {town} email",
        "matatu OR coach company {town} Kenya contacts",
    ],
    "retail": [
        "supermarket {town} Kenya contact us",
        "electronics shop {town} email",
        "hardware store {town} Kenya contacts",
    ],
    "insurance": [
        "insurance agency {town} Kenya contact us",
        "insurance brokers {town} email",
        "insurance company {town} Kenya contacts",
    ],
    "real_estate": [
        "property management company {town} Kenya contact us",
        "real estate agents {town} email",
        "apartments to let agency {town} Kenya",
    ],
    "utility": [
        "water and sewerage company {town} Kenya contact us",
        "internet service provider {town} email",
        "solar energy company {town} Kenya contacts",
    ],
    "hospitality": [
        "hotel {town} Kenya reservations contact",
        "restaurant {town} email bookings",
        "lodge OR guest house {town} Kenya contact us",
    ],
    "gym_salon": [
        "gym fitness centre {town} Kenya contact",
        "salon and spa {town} email bookings",
        "barbershop OR beauty parlour {town} Kenya contacts",
    ],
    "driving_school": [
        "driving school {town} Kenya contact us",
        "NTSA approved driving school {town} email",
        "driving lessons {town} Kenya fees",
    ],
    "automotive": [
        "car dealer {town} Kenya contact us",
        "garage auto repair {town} email",
        "motor spares shop {town} Kenya contacts",
    ],
    "agriculture": [
        "agrovet {town} Kenya contact us",
        "farmers cooperative society {town} email",
        "agribusiness company {town} Kenya contacts",
    ],
    "events": [
        "event planners {town} Kenya contact us",
        "wedding planner {town} email",
        "event venue gardens {town} Kenya bookings",
    ],
    "security": [
        "security company {town} Kenya contact us",
        "security guards services {town} email",
        "alarm and CCTV company {town} Kenya contacts",
    ],
    "professional": [
        "law firm advocates {town} Kenya contact us",
        "audit accounting firm {town} email",
        "consulting firm {town} Kenya contacts",
    ],
    "ngo": [
        "NGO {town} Kenya contact us",
        "community based organisation {town} email",
        "foundation charity {town} Kenya contacts",
    ],
    "logistics": [
        "courier company {town} Kenya contact us",
        "logistics clearing and forwarding {town} email",
        "parcel delivery services {town} Kenya contacts",
    ],
    # Tech companies: they send OTPs, payment alerts and order updates all day
    "tech_startup": [
        "tech startup Nairobi Kenya contact us",
        "Kenyan startup launched 2024",
        "startup Kenya seed funding",
        "software startup {town} Kenya contact",
        "mobile app startup Kenya sign up",
    ],
    "fintech": [
        "fintech startup Nairobi",
        "fintech company Kenya contact us",
        "mobile loans app Kenya contact",
        "payments startup Kenya API",
        "digital wallet OR savings app Kenya contact us",
    ],
    "hr_payroll": [
        "payroll software Kenya contact us",
        "HR and payroll outsourcing {town} Kenya",
        "HRMS system Kenya payslips contact",
    ],
    "saas": [
        "SaaS company Nairobi",
        "cloud software company Kenya contact us",
        "payroll OR HR software Kenya contact us",
        "POS OR inventory software Kenya contact",
        "school management system software Kenya contact",
    ],
    "ecommerce": [
        "e-commerce startup Kenya contact",
        "online shop Kenya delivery contact us",
        "online marketplace Kenya sellers contact",
        "buy online {town} Kenya store contact us",
    ],
    "delivery_app": [
        "delivery app Nairobi contact",
        "food delivery startup Kenya",
        "on-demand delivery service Kenya app contact us",
        "grocery OR errand delivery app {town} Kenya",
    ],
    "edtech": [
        "edtech Kenya contact us",
        "e-learning platform Kenya contact",
        "online learning startup Nairobi",
        "edtech startup Kenya schools parents app",
    ],
    "healthtech": [
        "healthtech startup Kenya",
        "telemedicine Kenya contact us",
        "digital health platform Nairobi contact",
        "online pharmacy Kenya delivery contact us",
    ],
    "agritech": [
        "agritech startup Kenya",
        "digital platform for farmers Kenya contact us",
        "farm inputs marketplace Kenya startup",
        "agri-fintech smallholder farmers Kenya app contact",
    ],
    "proptech": [
        "proptech Kenya contact us",
        "property listing platform Kenya contact",
        "rent collection software Kenya landlords",
        "real estate technology startup Nairobi",
    ],
    "insurtech": [
        "insurtech Kenya contact us",
        "digital insurance platform Kenya",
        "buy insurance online Kenya startup contact",
        "micro-insurance startup Kenya mobile",
    ],
    "logistics_tech": [
        "logistics technology startup Kenya",
        "fleet management software Kenya contact us",
        "truck booking platform Kenya contact",
        "last mile delivery platform Nairobi API",
    ],
    "events_ticketing": [
        "event ticketing platform Kenya contact",
        "buy event tickets online Kenya",
        "online ticketing company Nairobi contact us",
        "event registration software Kenya",
    ],
    "digital_marketing_agency": [
        "digital marketing agency Nairobi contact us",
        "digital marketing agency {town} Kenya email",
        "social media marketing company Kenya contact",
        "SEO agency Nairobi contact us",
    ],
    "coworking": [
        "coworking space Nairobi contact",
        "shared office space {town} Kenya contact us",
        "co-working hub Kenya membership",
        "serviced offices Nairobi contact us",
    ],
}

# Searches for pages that list many companies. Each result page is opened and the
# company sites it links to are visited and added under that category.
LIST_QUERIES = {
    "tech_startup": [
        "accelerator portfolio startups Kenya",
        "incubator Nairobi portfolio companies",
        "startup accelerator Kenya cohort startups",
        "Kenya startup hub member companies portfolio",
        "venture capital Kenya portfolio companies Nairobi",
    ],
}
LIST_QUERIES_PER_RUN = 2    # picked at random from the category's list
LIST_PAGES_PER_QUERY = 4    # result pages opened per list query
LINKS_PER_LIST = 25         # company links followed per page
# A site with a page like this is an accelerator, incubator or investor: its
# portfolio companies are visited and the site itself is not added
PORTFOLIO_LINK = re.compile(r"portfolio|our[- ]startups|our[- ]companies|alumni|cohort|investments|"
                            r"/startups|/companies|/ventures", re.I)
PORTFOLIO_PAGES_PER_SITE = 2

# Search order when no category is given
PRIORITY = [
    "sacco", "microfinance",
    "tech_startup", "fintech", "saas", "hr_payroll", "ecommerce", "delivery_app", "edtech", "healthtech",
    "agritech", "proptech", "insurtech", "logistics_tech", "events_ticketing",
    "digital_marketing_agency", "coworking",
    "school", "college", "church", "chama", "health", "transport",
    "retail", "insurance", "real_estate", "utility", "hospitality", "gym_salon",
    "driving_school", "automotive", "agriculture", "events", "security",
    "professional", "ngo", "logistics",
]

SKIP_WORDS = ("facebook", "linkedin", "wikipedia", "yellowpages", "twitter",
              "instagram", "youtube", "tiktok",
              # more social, app stores and link shorteners
              "whatsapp", "telegram", "pinterest", "threads.net", "medium.com",
              "google.", "linktr.ee", "github.com",
              # directories, listings and job boards
              "businesslist", "kenyabizlist", "africabz", "crunchbase", "tracxn", "f6s.com",
              "wellfound", "angel.co", "glassdoor", "brightermonday", "startuplist", "zoominfo",
              "pitchbook", "rocketreach", "dnb.com", "cybo.com",
              # news sites
              "tuko.co.ke", "nation.africa", "standardmedia", "businessdailyafrica", "kenyans.co.ke",
              "techweez", "techcabal", "disruptafrica", "disrupt-africa", "techcrunch", "capitalfm",
              "techinsider", "startupmap", "failory", "dealroom", "nairobionline")
# Short addresses that must match exactly ("t.me" is also inside "smart.media")
SKIP_EXACT = ("x.com", "t.me", "wa.me", "bit.ly", "apple.com")
# Banks without "bank" in their address, and internet providers
BANK_NAMES = ("kcbgroup", "equitygroup", "absa.", "ncbagroup", "stanbic", "dtbafrica", "stanchart", "imbank")
ISP_NAMES = ("safaricom", "airtel", "telkom", "zuku", "faiba", "liquid.tech", "liquidtelecom",
             "wananchi", "poainternet", "mawingu", "starlink", "vilcom")
ISP_TITLE = re.compile(r"internet service provider|\bISP\b|fibre internet|fiber internet|"
                       r"home internet|broadband", re.I)
# Other bulk SMS companies are competitors, not leads
RIVAL_TITLE = re.compile(r"bulk sms", re.I)
RESULTS_PER_QUERY = 10
# How a category is asked for on Google Maps, where it isn't its own name
PLACES_TERMS = {"school": "private school", "church": "church", "tech_startup": "technology company",
                "hr_payroll": "payroll services company", "saas": "software company",
                "digital_lender": "loan company", "fintech": "fintech company"}
DEFAULT_MAX = 30
CSV_PATH = Path(__file__).parent / "discovered.csv"
CSV_COLUMNS = ["company", "website", "industry", "email", "phone", "query"]


def domain_of(url):
    if "//" not in url:
        url = "//" + url
    return urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")


def skip_domain(domain):
    if not domain or any(w in domain for w in SKIP_WORDS):
        return True
    if any(domain == d or domain.endswith("." + d) for d in SKIP_EXACT):
        return True
    if any(w in domain for w in BANK_NAMES + ISP_NAMES):
        return True
    # Banks are out, microfinance banks are in
    return "bank" in domain and "microfinance" not in domain


def skip_title(title, category):
    """A reason to skip a site going by its page title, or None."""
    # The utility category searches for internet providers on purpose
    if category != "utility" and ISP_TITLE.search(title or ""):
        return "internet provider"
    if RIVAL_TITLE.search(title or ""):
        return "another bulk SMS company"
    return None


def known_domains():
    with db.conn() as c:
        rows = c.execute("SELECT website FROM leads WHERE website IS NOT NULL").fetchall()
    return {domain_of(r["website"]) for r in rows if r["website"]}


def company_name(title, domain):
    """Pick the company name out of a page <title> like 'Home | Acme SACCO Ltd'."""
    parts = [p.strip() for p in re.split(r"\s*[|–—·»]\s*|\s+-\s+|:\s+", title or "")]
    parts = [p for p in parts
             if p and not re.fullmatch(r"(home|homepage|welcome|index|contact( us)?|about( us)?)", p, re.I)]
    return (parts[0] if parts else domain)[:120]


# Words every SACCO or insurer has in its name, so they can't show a site is the one named
COMMON_NAME_WORDS = {"sacco", "saccos", "society", "societies", "savings", "credit", "cooperative", "co",
                     "operative", "insurance", "assurance", "reinsurance", "general", "life", "company",
                     "microfinance", "bank", "group", "and", "of", "the", "limited", "ltd", "plc", "kenya",
                     "brokers", "broker", "agency", "agencies", "investment", "investments", "services"}


def name_words(name):
    """The distinctive words of an organisation's name: "Stima Sacco Society Ltd" gives {"stima"}."""
    return frozenset(w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
                     if w not in COMMON_NAME_WORDS and len(w) >= 3)


def looks_like(name, title, domain):
    """Whether a site's title or domain has a distinctive word from `name`: for "Stima Sacco
    Society Ltd", "stima" in "stimasacco.com" or "Stima DT Sacco | Home"."""
    words = name_words(name)
    where = f"{title or ''} {domain}".lower()
    return any(w in where for w in words)


def look_at(url):
    """Fetch the homepage. Returns (title, mentions_kenya) or None if unreachable."""
    try:
        r = requests.get(url, headers=scraper.HEADERS, timeout=10)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.content, "html.parser")  # bytes: lets bs4 detect the encoding
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return title, "kenya" in soup.get_text(" ").lower()


def search(query):
    return websearch.search(query, RESULTS_PER_QUERY)


def save_csv(row):
    is_new = not CSV_PATH.exists()
    if not is_new:
        with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
            old = list(csv.DictReader(f))
        if old and "phone" not in old[0]:  # a file from before phones were kept: rewrite it with the column
            with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, restval="")
                w.writeheader()
                w.writerows(old)
    with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if is_new:
            w.writeheader()
        w.writerow(row)


def consider(url, category, query, seen, name=None, company=None, phone=None):
    """Visit one company site and add it as a lead if it qualifies.
    `name` is the organisation being looked for (a name from a register): the site is skipped
    unless its title or domain shares a distinctive word with that name, and the lead gets
    that name rather than the page title. `company` and `phone` are what a business listing
    (Google Places) says, used instead of the page title and the numbers on the site.
    Returns the new lead's row, or None."""
    domain = domain_of(url)
    if domain in seen or skip_domain(domain):
        return None
    seen.add(domain)
    home = f"{urlparse(url).scheme or 'https'}://{urlparse(url).netloc}"
    page = look_at(home)
    if not page:
        return None
    title, mentions_kenya = page
    if not (domain.endswith(".ke") or mentions_kenya):
        return None
    reason = skip_title(title, category) or (name and not looks_like(name, title, domain) and f"not {name}")
    if reason:
        print(f"    {domain}: skipped ({reason})")
        return None
    emails, phones = scraper.scrape_contacts(home)
    email = scraper.pick_best(emails)
    if not email:
        print(f"    {domain}: no email found")
        return None
    if db.blocked(email):
        print(f"    {domain}: {email} is on the suppression list")
        return None
    name = name or company or company_name(title, domain)
    # held for a person to confirm its industry: the search that found it is only a guess
    phone = scraper.phone_number(phone or "") or scraper.pick_phone(phones)
    if not db.add_lead(name, domain, category, email, "search", query, url, review_status="pending",
                       phone=phone):
        print(f"    {domain}: {email} already a lead")
        return None
    row = {"company": name, "website": domain, "industry": category,
           "email": email, "phone": phone or "", "query": query}
    save_csv(row)
    print(f"    + {name}  <{email}>")
    return row


def listed_sites(page_url):
    """The outside websites a list page links to, e.g. an accelerator's portfolio."""
    soup = scraper.fetch(page_url)
    if soup is None:
        return []
    own, got, out = domain_of(page_url), set(), []
    for a in soup.select('a[href^="http"]'):
        d = domain_of(a["href"])
        if not d or d in got or d == own or d.endswith("." + own) or own.endswith("." + d) or skip_domain(d):
            continue
        got.add(d)
        out.append(a["href"])
    return out[:LINKS_PER_LIST]


def portfolio_pages(home):
    """Links on a site's homepage to its own portfolio or startups pages."""
    soup = scraper.fetch(home)
    if soup is None:
        return []
    own, out = domain_of(home), []
    for a in soup.select("a[href]"):
        url = urljoin(home, a["href"]).split("#")[0]
        if domain_of(url) != own or url.rstrip("/") == home.rstrip("/") or url in out:
            continue
        if PORTFOLIO_LINK.search(a["href"]) or PORTFOLIO_LINK.search(a.get_text(" ", strip=True)):
            out.append(url)
    return out[:PORTFOLIO_PAGES_PER_SITE]


def discover_from_lists(category, limit, seen):
    """Find list pages for this category and visit the companies they link to."""
    added, pages = [], set()
    queries = LIST_QUERIES.get(category, [])
    for query in random.sample(queries, min(LIST_QUERIES_PER_RUN, len(queries))):
        print(f"  list search: {query}")
        results = search(query)
        time.sleep(random.uniform(2, 4))
        for res in results[:LIST_PAGES_PER_QUERY]:
            page_url = res.get("href") or ""
            if not page_url or page_url in pages:
                continue
            pages.add(page_url)
            sites = [u for u in listed_sites(page_url) if domain_of(u) not in seen]
            print(f"   list page: {page_url} ({len(sites)} sites to visit)")
            for url in sites:
                home = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
                hub_pages = portfolio_pages(home)
                if not hub_pages:  # an ordinary company
                    targets = [(url, page_url)]
                else:              # an accelerator or investor: go to the companies it lists
                    seen.add(domain_of(url))
                    targets = [(u, hp) for hp in hub_pages for u in listed_sites(hp) if domain_of(u) not in seen]
                    print(f"    {domain_of(url)}: has a portfolio page, visiting {len(targets)} companies listed there")
                for target, source in targets:
                    row = consider(target, category, f"listed on {source}", seen)
                    if row:
                        added.append(row)
                        if len(added) >= limit:
                            return added
    return added


def places_query(category, town):
    """What to ask Google Maps for: "sacco in Nakuru, Kenya"."""
    return f"{PLACES_TERMS.get(category, category.replace('_', ' '))} in {town}, Kenya"


def discover_from_places(category, limit, seen):
    """Businesses Google Maps lists for this category in a random town (only when
    GOOGLE_PLACES_API_KEY is set). Returns the leads added."""
    if websearch.why_not_places():
        return []
    added = []
    query = places_query(category, random.choice(TOWNS))
    print(f"  places: {query}")
    for res in websearch.places(query):
        row = consider(res["href"], category, f"places: {query}", seen, company=res["title"] or None,
                       phone=res["phone"])
        if row:
            added.append(row)
            if len(added) >= limit:
                break
    return added


def discover_category(category, limit, seen):
    """Returns the leads added for this category (at most `limit`): Google Places first when
    it is set up, then list pages, then web searches."""
    added = discover_from_places(category, limit, seen)
    if len(added) >= limit:
        return added
    added += discover_from_lists(category, limit - len(added), seen)
    if len(added) >= limit:
        return added
    for template in QUERIES[category]:
        query = template.format(town=random.choice(TOWNS))
        print(f"  search: {query}")
        results = search(query)
        time.sleep(random.uniform(2, 4))
        for res in results:
            row = consider(res.get("href") or "", category, query, seen)
            if row:
                added.append(row)
                if len(added) >= limit:
                    return added
    return added


def approved_categories():
    """PRIORITY without the industries uses.yaml doesn't approve for sending: leads found
    there would only wait. Name one (`discover school`) to search it anyway."""
    approved = [c for c in PRIORITY if personalise.approved({"industry": c})]
    skipped = [c for c in PRIORITY if c not in approved]
    if skipped:
        print(f"Not searching {len(skipped)} industries that aren't approved in uses.yaml: {', '.join(skipped)}")
    return approved


def run(category=None, max_new=DEFAULT_MAX):
    if category and category not in QUERIES:
        print(f"Unknown category '{category}'. Choose from: {', '.join(PRIORITY)}")
        return []
    seen = known_domains()
    added = []
    categories = [category] if category else approved_categories()
    for cat in categories:
        if len(added) >= max_new:
            break
        print(f"[{cat}]")
        added += discover_category(cat, max_new - len(added), seen)
    print(f"Added {len(added)} new leads. Review them in {CSV_PATH.name}.")
    return added
