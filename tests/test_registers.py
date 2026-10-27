"""Leads from regulators' registers: the CBK directory of microfinance banks (full contacts),
and name-only registers like SASRA's licensed SACCOs, looked up on the web. Directory text and
search results are given here; nothing is downloaded."""
import pytest

import config
import db
import discover
import targeting
import verify
import websearch

# The start of the real February 2026 directory, as pdf_text gives it
MFB = """CENTRAL BANK OF KENYA
NO. DIRECTORY OF LICENCED MICROFINANCE BANKS
1. Caritas Microfinance Bank Limited
Postal Address: P. O. Box 15352 - 00100, Nairobi
Telephone: +254 - 020 - 5151500
Email: info@caritas-mfb.co.ke
Website: www.caritas-mfb.co.ke
Physical Address: Cardinal Maurice Otunga Plaza, Ground floor, Kaunda Street, Nairobi
Date Licenced: 02.06.2015
Branches: 2
2. Faulu Microfinance Bank Limited
Postal Address: P. O. Box 60240 - 00200, Nairobi
Telephone: +254 - 020 - 3877290/3/7; 3872183/4; 3867503, 0711 - 074074, 0708 - 111000
Fax: +254-20-3867504, 3874875
Email: info@faulukenya.com, customercare@faulukenya.com, contact@faulukenya.com
Website: www.faulukenya.com
Physical Address: Faulu Kenya House, Ngong Lane - Off Ngong Road, Nairobi
Date Licenced: 21.05.2009
Branches: 39
Page 1 of 3
C2: CBK - Official
CENTRAL BANK OF KENYA
NO. DIRECTORY OF LICENCED MICROFINANCE BANKS
3. Umba Microfinance Bank Limited
Postal Address: P.O. Box 100854 - 00101, Nairobi
Telephone:– 0746 555 222
Email: info@umba.co.ke
Website: www.umba.com
Physical Address: Timshack House, 2nd Floor, Ngong Road, Nairobi
Date Licenced: 12.01.2015
Branches: 1
"""


def test_reads_the_microfinance_directory():
    caritas, faulu, umba = targeting.parse_directory(MFB)
    assert (caritas["name"], caritas["email"], caritas["website"]) == (
        "Caritas Microfinance Bank Limited", "info@caritas-mfb.co.ke", "caritas-mfb.co.ke")
    assert (caritas["phone"], caritas["licensed"]) == ("+254205151500", "2015-06-02")
    assert faulu["phone"] == "+254711074074"  # the mobile, out of a long list
    assert faulu["address"] == "Faulu Kenya House, Ngong Lane - Off Ngong Road, Nairobi"  # fax and branches left out
    assert umba["website"] == "umba.com"  # the website listed, not the email's domain
    assert umba["phone"] == "+254746555222"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(targeting, "fetch", lambda src: b"%PDF")
    monkeypatch.setattr(targeting, "pdf_text", lambda data: MFB)
    monkeypatch.setattr(targeting, "swap_emails", lambda: 0)
    monkeypatch.setattr(verify, "domain_ok", lambda d: True)
    db.init()


def test_microfinance_banks_become_microfinance_leads(world):
    targeting.import_microfinance("directory.pdf")
    run, result, added = targeting.last_run()
    assert [l["industry"] for l in added] == ["microfinance"] * 3
    assert targeting.run_industry(run) == "microfinance"
    assert "Every lead is in industry `microfinance`" in "\n".join(targeting.import_markdown(run, result, added, lambda h, r: ""))
    assert all(l["status"] == "verified" for l in added)


def test_a_registered_lead_starts_high_enough_to_be_emailed(world):
    targeting.import_microfinance("directory.pdf")
    lead = db.find_lead_by_email("info@faulukenya.com")
    score, reasons = targeting.need_score(lead, "")
    assert score >= config.MIN_SCORE
    assert any("regulator's register" in r for r in reasons)
    found = {**dict(lead), "imported_at": None}  # the same organisation found by web search
    assert targeting.need_score(found, "")[0] < score


def test_the_lender_import_still_records_digital_lenders(world, monkeypatch):
    monkeypatch.setattr(targeting, "pdf_text", lambda data: "1. Pesa Lender Limited\nEmail: info@pesalender.co.ke\n")
    targeting.import_lenders("directory.pdf")
    run, _, added = targeting.last_run()
    assert [l["industry"] for l in added] == ["digital_lender"]
    assert targeting.run_industry(run) == "digital_lender"


def test_a_names_file_ignores_numbering_and_blank_lines(tmp_path):
    path = tmp_path / "sasra.txt"
    path.write_text("1. Stima Sacco Society Ltd\n\n2) Mwalimu National Sacco Society Ltd\nStima Sacco Society Ltd\n")
    assert targeting.read_names(path) == ["Stima Sacco Society Ltd", "Mwalimu National Sacco Society Ltd"]


def test_a_site_must_look_like_the_name():
    assert discover.looks_like("Stima Sacco Society Ltd", "Home", "stimasacco.com")
    assert discover.looks_like("Mwalimu National Sacco Society Ltd", "Mwalimu National | Home", "mwalimusacco.coop")
    assert not discover.looks_like("Stima Sacco Society Ltd", "Best Saccos in Kenya", "saccoreview.co.ke")


def test_named_organisations_are_looked_up_and_held_for_review(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(discover, "CSV_PATH", tmp_path / "discovered.csv")
    db.init()
    db.add_lead("Mwalimu National Sacco", "mwalimusacco.coop", "sacco", "info@mwalimusacco.coop", "csv")
    sites = {"https://saccoreview.co.ke/top": ("Best Saccos in Kenya", True),
             "https://stimasacco.com/contact": ("Stima DT Sacco | Home", True)}
    searched = []

    def search(query, n):
        searched.append(query)
        return [{"href": u} for u in sites] if "Stima" in query else []

    monkeypatch.setattr(websearch, "search", search)
    monkeypatch.setattr(discover, "look_at", lambda home: next(v for u, v in sites.items() if u.startswith(home)))
    monkeypatch.setattr(discover.scraper, "scrape_contacts", lambda home: (["info@stimasacco.com"], ["+254703070707"]))
    path = tmp_path / "sasra.txt"
    path.write_text("Stima Sacco Society Ltd\nMwalimu National Sacco Society Ltd\nUnknown Sacco Society Ltd\n")
    added, not_found = targeting.import_names(path, "sacco", "SASRA 2026")
    assert [r["company"] for r in added] == ["Stima Sacco Society Ltd"]
    assert not_found == ["Unknown Sacco Society Ltd"]
    assert len(searched) == 2  # Mwalimu is already a lead: not searched
    lead = db.find_lead_by_email("info@stimasacco.com")
    assert (lead["industry"], lead["review_status"], lead["phone"], lead["source_query"]) == (
        "sacco", "pending", "+254703070707", "named in SASRA 2026")
