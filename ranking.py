"""Decides which waiting leads get emailed first: the highest need score (targeting.py),
then younger companies, niches that send messages all day, and leads with an address on
their own domain.

The year a company started is estimated from its own website ("founded in 2021",
"since 2019", the first year in a footer copyright range) and from the WHOIS
record of its domain. The earliest believable year wins.
"""
import re
import threading
from datetime import date

import config
import db
import scraper

EARLIEST_YEAR = 1950
WHOIS_TIMEOUT = 5  # seconds
YEAR = r"((?:19|20)\d{2})"
STARTED = re.compile(r"\b(?:founded|established|since|est)\b\.?\s*(?:in\s+)?" + YEAR + r"\b", re.I)
# Only ranges count: a lone "© 2026" is just this year. The first year is taken.
COPYRIGHT_RANGE = re.compile(r"(?:©|\(c\)|copyright)[\s©]*(?:copyright\s*)?" + YEAR + r"\s*[-–—]\s*" + YEAR, re.I)

# Niches that send messages constantly
BUSY_NICHES = {"fintech", "microfinance", "sacco", "ecommerce", "delivery_app", "saas",
               "healthtech", "logistics_tech", "transport", "school"}
FREE_MAIL = db.FREE_MAIL


def believable(year):
    return EARLIEST_YEAR <= year <= date.today().year


def site_year(pages):
    """Earliest believable start year mentioned in the site's text, or None."""
    years = []
    for text in pages:
        years += [int(y) for y in STARTED.findall(text)]
        years += [int(first) for first, _ in COPYRIGHT_RANGE.findall(text)]
    years = [y for y in years if believable(y)]
    return min(years) if years else None


def whois_year(domain):
    """Year the domain was registered, or None. Gives up after WHOIS_TIMEOUT
    seconds and never raises, whatever the lookup does."""
    found = []

    def look():
        try:
            import whois
            created = whois.whois(domain, quiet=True, timeout=WHOIS_TIMEOUT).creation_date
            if isinstance(created, (list, tuple)):
                created = min(created, key=str)
            match = re.search(YEAR, str(getattr(created, "year", created) or ""))
            if match:
                found.append(int(match.group(1)))
        except BaseException:  # the library can raise anything, including SystemExit
            pass

    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(WHOIS_TIMEOUT)
    return found[0] if found and believable(found[0]) else None


def estimate(lead):
    """Returns (est_year, source) where source is "site", "whois" or "unknown"."""
    website = lead["website"]
    if not website:
        return None, "unknown"
    from_site = site_year(scraper.site_pages(website))
    domain = urlparse_domain(website)
    from_whois = whois_year(domain) if domain else None
    if from_site and (not from_whois or from_site <= from_whois):
        return from_site, "site"
    if from_whois:
        return from_whois, "whois"
    return None, "unknown"


def urlparse_domain(website):
    host = re.sub(r"^https?://", "", website.strip().lower()).split("/")[0].split(":")[0]
    return host.removeprefix("www.")


def fill_years(leads=None):
    """Estimate the start year for verified leads that haven't been checked yet.
    Returns how many were checked."""
    if leads is None:
        leads = db.leads_by_status("verified")
    leads = [l for l in leads if dict(l).get("est_year_source") is None]
    for i, lead in enumerate(leads, 1):
        try:
            year, source = estimate(lead)
        except Exception as e:  # a lead we can't date is still a lead
            print(f"[{i}/{len(leads)}] {lead['company']}: could not estimate ({e})")
            year, source = None, "unknown"
        db.save_est_year(lead["id"], year, source)
        print(f"[{i}/{len(leads)}] {lead['company'] or lead['email']}: "
              f"{f'started about {year} ({source})' if year else 'start year unknown'}")
    return len(leads)


def score_reasons(lead):
    """Where a lead's points come from: start year, niche SMS fit and email type."""
    lead = dict(lead)
    year, points = lead.get("est_year"), 0
    if year:
        age = date.today().year - year
        points = 3 if age <= 3 else 2 if age <= 6 else 0
    niche = (lead.get("industry") or "").lower().replace(" ", "_")
    own_domain = (lead.get("email") or "").rsplit("@", 1)[-1].lower() not in FREE_MAIL
    return {"est_year": {"value": year, "points": points},
            "niche": {"value": niche or None, "points": 2 if niche in BUSY_NICHES else 0},
            "email_type": {"value": "company_domain" if own_domain else "free_mail",
                           "points": 1 if own_domain else 0}}


def score(lead):
    return sum(r["points"] for r in score_reasons(lead).values())


def need_score(lead):
    """The lead's saved need score (targeting.py), or None if it hasn't been scored."""
    lead = dict(lead)
    return lead.get("score") if lead.get("scored_at") else None


def waiting():
    """Verified leads in the order they will be emailed: highest need score first (see
    targeting.py; not scored yet comes last), then the points above, then oldest-found first. An address on the suppression list is never waiting,
    whatever its score, and neither is a lead that has already been emailed, even if
    its status was set back to verified: a first email is never sent twice. A lead someone
    has emailed from the CRM is left to them, and one found by web search waits until a
    person approves it in the CRM (review_status). A lead whose email failed to go out before
    comes after the ones that haven't failed, so it can't hold up the day's sending."""
    is_blocked = db.blocker()
    emailed = db.emailed_lead_ids()
    failures = db.failure_counts()
    leads = [l for l in db.leads_by_status("verified")
             if not is_blocked(l["email"]) and l["id"] not in emailed and not l["crm_emailed_at"]
             and l["review_status"] not in db.NOT_REVIEWED]
    return sorted(leads, key=lambda l: (failures.get(l["id"], 0) > 0,
                                        -(need_score(l) if need_score(l) is not None else -1),
                                        -score(l), l["id"]))


def show(top=20):
    fill_years()
    leads = waiting()
    if not leads:
        print("No verified leads waiting.")
        return
    print(f"{'#':>2}  {'COMPANY':34}  {'NICHE':24}  {'EST.':>4}  {'SOURCE':7}  NEED")
    for i, l in enumerate(leads[:top], 1):
        company = l["company"] or l["email"]
        company = company if len(company) <= 34 else company[:33] + "…"
        print(f"{i:>2}  {company:34}  {(l['industry'] or 'default'):24}  "
              f"{l['est_year'] or '':>4}  {(l['est_year_source'] or 'unknown'):7}  "
              f"{need_score(l) if need_score(l) is not None else '-':>4}")
    print(f"{min(top, len(leads))} of {len(leads)} waiting leads.")
