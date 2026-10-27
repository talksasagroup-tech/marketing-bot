"""Reading the CBK directory of digital credit providers and adding its lenders as leads.
The directory's text is given here, so no PDF is read and nothing is downloaded."""
import pytest

import config
import db
import targeting
import verify

DIRECTORY = """CENTRAL BANK OF KENYA
DIRECTORY OF DIGITAL CREDIT PROVIDERS
UPDATED ON SEPTEMBER 29, 2026
1. Amaze Credit Limited
Postal Address: P.O. Box 13234-00400, Nairobi
E-mail address: amaze25c@gmail.com/info@amazecreditlimited.com
Telephone No: +2542022613869 / +254721726845
Physical Address: Motorworld Centre, Jogoo road near city stadium.
Date Licensed: April 10, 2026
2
C2: CBK - Official
CENTRAL BANK OF KENYA
2. Inventure Mobile Limited (Trading as Tala)
Postal Address: P.O. Box 1-00100, Nairobi
Telephone-254708358800
Email: hellokenya@talamobile.com
Physical Address: The Oval, 6th Floor,
Ring Road, Westlands, Nairobi
Date Licensed: January 30, 2023
3. Kaltris Limited
Email: tmatharu@me.com
Physical Address: Physical Address. Gate 04, Dunga Close, Nairobi,
KenyaDate Licensed: July 9, 2026
4. Quiet Lender Limited
Telephone: 0700000000
Licensed: September 2, 2025
5. Zenka Digital Ltd
Email: hello@zenka.example
6. Twin Credit Limited
Email: info@amazecreditlimited.com
"""


def test_reads_every_entry_and_its_fields():
    amaze, tala, kaltris, quiet, zenka, twin = targeting.parse_directory(DIRECTORY)
    assert amaze["email"] == "info@amazecreditlimited.com"  # the business address, not gmail
    assert amaze["website"] == "amazecreditlimited.com"
    assert amaze["licensed"] == "2026-04-10"
    assert tala["phone"] == "+254708358800"
    assert tala["address"] == "The Oval, 6th Floor, Ring Road, Westlands, Nairobi"
    assert kaltris["website"] is None  # me.com is free mail
    assert kaltris["address"] == "Gate 04, Dunga Close, Nairobi, Kenya"
    assert kaltris["licensed"] == "2026-07-09"
    assert quiet["email"] is None and quiet["licensed"] == "2025-09-02"
    assert [e["number"] for e in (amaze, tala, kaltris, quiet, zenka, twin)] == [1, 2, 3, 4, 5, 6]


def test_a_lead_already_on_that_domain_is_a_duplicate(world):
    db.add_lead("Amaze", "amazecreditlimited.com", "fintech", "sales@amazecreditlimited.com", "search")
    targeting.import_lenders("directory.pdf")
    _, result, added = targeting.last_run()
    assert "Amaze Credit Limited" not in [l["company"] for l in added]
    assert {"name": "Amaze Credit Limited", "reason": "amazecreditlimited.com is Amaze's domain"}.items() \
        <= next(d for d in result["duplicates"] if d["number"] == 1).items()


def test_an_opted_out_organisation_is_not_imported(world):
    with db.conn() as c:  # someone at Amaze asked us to stop, from another address
        c.execute("INSERT INTO suppressed_domains (domain, reason) VALUES ('amazecreditlimited.com', 'unsubscribed')")
    targeting.import_lenders("directory.pdf")
    _, result, added = targeting.last_run()
    assert "Amaze Credit Limited" not in [l["company"] for l in added]
    assert "Amaze Credit Limited" in [s["name"] for s in result["suppressed"]]


def test_company_names_match_without_legal_words():
    assert targeting.name_keys("Zenka Digital Ltd") == targeting.name_keys("Zenka Digital Limited")
    assert "tala" in targeting.name_keys("Inventure Mobile Limited (Trading as Tala)")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(targeting, "fetch", lambda src: b"%PDF")
    monkeypatch.setattr(targeting, "pdf_text", lambda data: DIRECTORY)
    monkeypatch.setattr(verify, "domain_ok", lambda d: d != "talamobile.com")
    db.init()
    db.add_lead("Zenka Digital Limited", "zenka.co.ke", "fintech", "info@zenka.co.ke", "manual")


def test_import_skips_duplicates_and_marks_invalid(world, tmp_path):
    targeting.import_lenders("directory.pdf")
    run, result, added = targeting.last_run()
    assert run["found"] == 6
    assert [l["company"] for l in added] == ["Amaze Credit Limited",
                                             "Inventure Mobile Limited (Trading as Tala)", "Kaltris Limited"]
    assert {l["industry"] for l in added} == {"digital_lender"}
    assert {l["source"] for l in added} == {targeting.CBK_DCP_SOURCE}
    assert all(l["imported_at"] and l["source_url"] == targeting.CBK_DCP_URL for l in added)
    assert [l["status"] for l in added] == ["verified", "invalid", "verified"]
    assert [d["name"] for d in result["duplicates"]] == ["Zenka Digital Ltd", "Twin Credit Limited"]
    assert [m["name"] for m in result["missing_email"]] == ["Quiet Lender Limited"]
    targeting.report(tmp_path / "report.md")
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "| Skipped as duplicates | 2 |" in md and "Kaltris Limited" in md


def lender(**fields):
    return {"industry": "digital_lender", "website": "zenka.co.ke", "email": "info@zenka.co.ke",
            "status": "verified", "date_licensed": "2025-12-24", **fields}


SITE = "Download our app on Google Play. Repay via M-Pesa paybill 123456."


def test_lender_with_everything_scores_100():
    score, reasons = targeting.need_score(lender(), SITE)
    assert score == 100
    assert reasons == ["+50 start (digital lender)", "+15 own website domain (zenka.co.ke)",
                       '+15 site mentions an app or Play Store ("app")',
                       '+10 site mentions M-Pesa or paybill ("M-Pesa")',
                       "+10 licensed within the last 2 years (2025-12-24)"]


def test_lender_on_gmail_with_an_invalid_email_scores_0():
    score, reasons = targeting.need_score(
        lender(website=None, email="lender@gmail.com", status="invalid", date_licensed="2023-03-27"), None)
    assert score == 0
    assert reasons == ["+50 start (digital lender)", "-20 email is free mail (gmail.com)",
                       "-30 email invalid", "no website to read"]


def test_a_website_that_cant_be_read_gives_10():
    score, reasons = targeting.need_score(lender(date_licensed="2023-03-27"), None)
    assert score == 75 and reasons[-1] == "+10 couldn't check website"  # 50 + own domain + 10
    assert targeting.need_score({"industry": "sacco", "website": "hurima.co.ke"}, None)[0] == 50


def test_other_industries_gain_10_per_sign_on_their_site():
    text = ("We send SMS alerts to our 12,000+ members across 14 branches. Pay via paybill. "
            "Log in to the member portal or download the app.")
    score, reasons = targeting.need_score({"industry": "sacco", "website": "hurima.co.ke"}, text)
    assert score == 90
    assert len([r for r in reasons if r.startswith("+10")]) == 5  # app or portal counts once
    assert targeting.need_score({"industry": "sacco"}, "Welcome to our SACCO.")[0] == 40


def test_score_is_saved_with_its_reasons(world, monkeypatch):
    monkeypatch.setattr(targeting, "site_text", lambda website: SITE)
    targeting.import_lenders("directory.pdf")
    targeting.score_all()
    top = targeting.top_scored()
    assert [l["company"] for l in top[:1]] == ["Amaze Credit Limited"]
    assert top[0]["score"] == 100 and top[0]["score_version"] == targeting.SCORE_VERSION
    assert top[0]["scored_at"] and "+10 site mentions M-Pesa or paybill (\"M-Pesa\")" in top[0]["reasons"]


def test_lenders_get_their_own_first_email(world):
    import sender
    targeting.import_lenders("directory.pdf")
    lead = db.find_lead_by_email("info@amazecreditlimited.com")
    msg, tname = sender.build(lead)
    assert tname == "digital_lender" and msg["Subject"] == "Loan repayment reminders by SMS"
    text = msg.get_body(("plain",)).get_content()
    assert ("Hello Amaze Credit Limited team,\n\nI'm Florence from TalkSasa Communications in Nairobi. "
            "We help digital lenders send loan repayment reminders, disbursement confirmations and OTPs "
            "by SMS.\n\nWho handles SMS on your side today? If it's worth comparing, I can send over "
            "our rates and API docs.\n\nI've attached a one-page overview of TalkSasa.") in text
    assert "Kind regards,\nFlorence\nTalkSasa Communications Ltd\n+254 762 000 008\n+254 712 295 880" in text
    assert 'just reply "unsubscribe"' in text
    assert [p.get_filename() for p in msg.iter_attachments()] == ["TalkSasa_Overview.pdf"]
    assert sender.followup_template(lead).name == "digital_lender.txt"
    assert sender.followup_template(lead, 2).name == "digital_lender.txt"
    assert sender.followup_values(lead) == {
        "uses": "loan repayment reminders and disbursement confirmations", "offer": sender.API_OFFER}


def test_highest_score_first_and_none_below_min_score(world, monkeypatch):
    import sender
    monkeypatch.setattr(config, "MIN_SCORE", 50)
    for name, score in (("low", 45), ("high", 90), ("mid", 60), ("unscored", None)):
        db.add_lead(name, f"{name}.co.ke", "fintech", f"info@{name}.co.ke", "manual")
        lead_id = db.find_lead_by_email(f"info@{name}.co.ke")["id"]
        db.set_status(lead_id, "verified", "verify")
        if score is not None:
            with db.conn() as c:
                c.execute("UPDATE leads SET score=?, scored_at=CURRENT_TIMESTAMP WHERE id=?", (score, lead_id))
    assert [l["company"] for l in sender.approved_waiting()] == ["high", "mid"]


def test_max_per_industry_mixes_the_batch_and_fills_spare_slots(monkeypatch):
    import sender
    leads = [{"id": i, "industry": ind} for i, ind in enumerate(
        ["digital_lender"] * 5 + ["school", "church"])]
    monkeypatch.setattr(config, "MAX_PER_INDUSTRY", 2)
    assert [l["id"] for l in sender.spread(leads, 4)] == [0, 1, 5, 6]
    # only two other leads: the spare slots go to the next lenders, in order
    assert [l["id"] for l in sender.spread(leads, 6)] == [0, 1, 2, 3, 5, 6]
    monkeypatch.setattr(config, "MAX_PER_INDUSTRY", 0)
    assert [l["id"] for l in sender.spread(leads, 4)] == [0, 1, 2, 3]


def test_sender_scores_waiting_leads_first(world, monkeypatch):
    monkeypatch.setattr(targeting, "site_text", lambda website: SITE)
    db.add_lead("New Fintech", "newfin.co.ke", "fintech", "info@newfin.co.ke", "manual")
    db.set_status(db.find_lead_by_email("info@newfin.co.ke")["id"], "verified", "verify")
    assert targeting.score_unscored() == 1
    lead = db.find_lead_by_email("info@newfin.co.ke")
    assert lead["score"] == 60 and lead["scored_at"]  # 40 + app + M-Pesa
    assert targeting.score_unscored() == 0


@pytest.mark.parametrize("email, company, swap", [
    ("legal@little.africa", "Little Limited", True),
    ("compliance@4g-capital.com", "Fourth Generation Capital Limited", True),
    ("reports@iloan.co.ke", "Easy Asset Management Limited", True),
    ("accounts@chime.llc", "Chime Limited", True),
    ("finance@x.co.ke", "X Limited", True),
    ("nzilani.mweu@kopokopo.com", "Kopo Kopo Inc. Kenya Limited", True),
    ("pgathondu@bimaskenya.com", "Bimas Kenya Limited", True),
    ("info@leja.co.ke", "Leja Ltd", False),
    ("support@radicredit.com", "Radi Credit Limited", False),
    ("ceo@jambofin.co.ke", "Jambofin Credit Limited", False),
    ("autochekkenya@autochek.africa", "Autochek Limited", False),
    ("pocket-dealer@shekelmobility.com", "Pocket Dealers Limited", False),
    ("legal.person@gmail.com", "Gmail Lender Ltd", True),   # a role inbox, even on free mail
    ("janedoe@gmail.com", "Gmail Lender Ltd", False),        # no website to look on anyway
])
def test_which_addresses_are_swapped(email, company, swap):
    assert bool(targeting.swap_reason(email, company)) is swap


def test_swap_keeps_the_directory_address(world, monkeypatch):
    targeting.import_lenders("directory.pdf")
    lead = db.find_lead_by_email("info@amazecreditlimited.com")
    with db.conn() as c:
        c.execute("UPDATE leads SET email='compliance@amazecreditlimited.com' WHERE id=?", (lead["id"],))
    monkeypatch.setattr(targeting, "site_emails", lambda website: [
        "compliance@amazecreditlimited.com", "sales@amazecreditlimited.com", "hello@amazecreditlimited.com"])
    assert targeting.swap_emails() == 1
    lead = dict(db.find_lead_by_email("hello@amazecreditlimited.com"))  # hello@ before sales@
    assert lead["source_email"] == "compliance@amazecreditlimited.com"
    swapped, kept = targeting.contact_status()
    assert [l["id"] for l in swapped] == [lead["id"]] and kept == []
    assert targeting.swap_emails() == 0  # done once


def test_no_swap_without_a_general_inbox_on_the_site(world, monkeypatch):
    targeting.import_lenders("directory.pdf")
    with db.conn() as c:
        c.execute("UPDATE leads SET email='legal@amazecreditlimited.com' WHERE email='info@amazecreditlimited.com'")
    monkeypatch.setattr(targeting, "site_emails", lambda website: ["ceo@amazecreditlimited.com"])
    assert targeting.swap_emails() == 0
    assert db.find_lead_by_email("legal@amazecreditlimited.com")["source_email"] is None
    assert [l["email"] for l, _ in targeting.contact_status()[1]] == ["legal@amazecreditlimited.com"]
