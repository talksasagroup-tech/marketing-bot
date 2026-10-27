"""Two follow-ups per lead at most, in the first email's thread, each 5 business days after
the email before it.
Temporary database and fake mail server: nothing is sent."""
from datetime import date

import pytest

import config
import db
import ranking
import sender

FIRST_ID = "<first@talksasa.co.ke>"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", True)
    monkeypatch.setattr(config, "FOLLOWUP_AFTER_DAYS", 5)
    monkeypatch.setattr(config, "DAILY_LIMIT", 10)
    monkeypatch.setattr(config, "SEND_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(config, "FROM_EMAIL", "florence@talksasa.co.ke")
    monkeypatch.setattr(ranking, "fill_years", lambda *a, **k: 0)
    monkeypatch.setattr(sender, "save_to_sent", lambda *a: True)
    monkeypatch.setattr(sender.notify, "send", lambda text: None)
    db.init()


def emailed(address, days_ago, industry="sacco", subject="Loan reminders by SMS", message_id=FIRST_ID):
    """A lead whose first email went out `days_ago` days ago and who hasn't answered."""
    db.add_lead("Hurima Sacco", address.split("@")[1], industry, address, "search")
    lead_id = db.find_lead_by_email(address)["id"]
    db.record_sent(lead_id, message_id, industry, subject, "first email")
    db.set_status(lead_id, "sent", "send")
    with db.conn() as c:
        c.execute("UPDATE sent SET sent_on = date('now', ?) WHERE lead_id = ?", (f"-{days_ago} days", lead_id))
    return lead_id


def sent_to(smtp_world, address):
    return [m for m in smtp_world["messages"] if m["To"] == address]


@pytest.fixture
def mail(smtp, monkeypatch):
    """The fake mail server, also keeping each whole message."""
    smtp["messages"] = []
    real = sender.deliver

    def deliver(s, lead, msg, tname, followup=False, mailbox=None):
        real(s, lead, msg, tname, followup, mailbox)
        smtp["messages"].append(msg)

    monkeypatch.setattr(sender, "deliver", deliver)
    return smtp


def test_follow_up_goes_in_the_same_thread(world, mail):
    emailed("info@hurimasacco.co.ke", days_ago=10)
    sender.run()
    [msg] = sent_to(mail, "info@hurimasacco.co.ke")
    assert msg["Subject"] == "Re: Loan reminders by SMS"
    assert msg["In-Reply-To"] == FIRST_ID and msg["References"] == FIRST_ID
    text = msg.get_body(("plain",)).get_content()
    assert "Hello Hurima Sacco team" in text
    assert ("Just following up on my email about sending loan reminders and deposit confirmations "
            "by SMS. If that's something you're looking at, I'd be glad to visit or set up a quick "
            "call, whichever suits you.") in text
    assert 'just reply "unsubscribe"' in text  # footer, as on every email
    # signature ends at the phone number: no website line
    assert "Kind regards,\nFlorence\nTalkSasa Communications Ltd\n+254 762 000 008\n+254 712 295 880\n\n--\n" in text
    assert not list(msg.iter_attachments())  # no PDF the second time
    assert db.find_lead_by_email("info@hurimasacco.co.ke")["status"] == "sent"  # still waiting
    assert db.sent_count(db.find_lead_by_email("info@hurimasacco.co.ke")["id"]) == 2


def test_not_due_before_five_business_days(world, mail):
    emailed("info@hurimasacco.co.ke", days_ago=1)
    sender.run()
    assert mail["delivered"] == []


def age_all_sends(days):
    with db.conn() as c:
        c.execute("UPDATE sent SET sent_on = date(sent_on, ?)", (f"-{days} days",))


def test_second_follow_up_is_the_last(world, mail):
    lead_id = emailed("info@hurimasacco.co.ke", days_ago=20)
    sender.run()
    sender.run()  # follow-up 2 isn't due on the day follow-up 1 went
    assert mail["delivered"] == ["info@hurimasacco.co.ke"]
    age_all_sends(10)
    sender.run()
    age_all_sends(10)
    sender.run()
    assert mail["delivered"] == ["info@hurimasacco.co.ke"] * 2
    assert db.sent_count(lead_id) == 3


def test_second_follow_up_threads_under_the_first_email(world, mail):
    emailed("info@fintech.co.ke", days_ago=20, industry="fintech", subject="OTPs and transaction alerts by SMS")
    sender.run()
    age_all_sends(10)
    sender.run()
    first, second = sent_to(mail, "info@fintech.co.ke")
    assert "OTPs and transaction alerts by SMS. If that's something you're looking at, I can send over our rates and API docs." \
        in first.get_body(("plain",)).get_content()
    assert second["Subject"] == "Re: OTPs and transaction alerts by SMS"  # the first email's, not "Re: Re:"
    # a reply to our latest email, with every earlier one in References, oldest first
    assert second["In-Reply-To"] == first["Message-ID"]
    assert second["References"] == f"{FIRST_ID} {first['Message-ID']}"
    text = second.get_body(("plain",)).get_content()
    assert "I won't keep filling your inbox. If SMS becomes useful for you later" in text
    assert 'just reply "unsubscribe"' in text
    assert not list(second.iter_attachments())


def test_second_follow_up_waits_five_business_days_after_the_first(world, mail):
    emailed("info@hurimasacco.co.ke", days_ago=20)
    sender.run()
    age_all_sends(1)
    sender.run()
    assert mail["delivered"] == ["info@hurimasacco.co.ke"]


def test_no_second_follow_up_after_a_reply(world, mail):
    lead_id = emailed("info@hurimasacco.co.ke", days_ago=20)
    sender.run()
    db.set_status(lead_id, "replied", "inbox")
    age_all_sends(10)
    sender.run()
    assert mail["delivered"] == ["info@hurimasacco.co.ke"]


@pytest.mark.parametrize("new_status", ["replied", "meeting", "client", "bounced", "unsubscribed", "invalid"])
def test_no_follow_up_once_the_lead_has_moved_on(world, mail, new_status):
    lead_id = emailed("info@hurimasacco.co.ke", days_ago=10)
    db.set_status(lead_id, new_status, "inbox")
    sender.run()
    assert mail["delivered"] == []


def test_recent_out_of_office_holds_the_follow_up(world, mail):
    lead_id = emailed("info@hurimasacco.co.ke", days_ago=10)
    db.add_reply(lead_id, "info@hurimasacco.co.ke", "Automatic reply", "Away", "out_of_office", "away", "1")
    sender.run()
    assert mail["delivered"] == []
    with db.conn() as c:  # a week later they are probably back
        c.execute("UPDATE replies SET received_at = datetime('now', '-8 days')")
    sender.run()
    assert mail["delivered"] == ["info@hurimasacco.co.ke"]


def test_nothing_when_follow_ups_are_not_approved(world, mail, monkeypatch):
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", False)
    emailed("info@hurimasacco.co.ke", days_ago=10)
    sender.run()
    assert mail["delivered"] == []


def test_no_follow_up_without_a_template(world, mail, monkeypatch, tmp_path):
    monkeypatch.setattr(sender, "FOLLOWUP_DIR", tmp_path / "none")
    emailed("info@hurimasacco.co.ke", days_ago=10)
    sender.run()
    assert mail["delivered"] == []


def test_unapproved_industry_gets_no_follow_up(world, mail):
    emailed("info@clinic.co.ke", days_ago=10, industry="health")
    sender.run()
    assert mail["delivered"] == []


def test_first_email_is_never_sent_twice(world, mail):
    lead_id = emailed("info@hurimasacco.co.ke", days_ago=1)
    db.set_status(lead_id, "verified", "dashboard")  # set back by hand
    sender.run()
    assert mail["delivered"] == []


def test_follow_ups_go_first_within_the_daily_limit(world, mail, monkeypatch):
    monkeypatch.setattr(config, "DAILY_LIMIT", 1)
    emailed("info@hurimasacco.co.ke", days_ago=10)
    db.add_lead("New Sacco", "newsacco.co.ke", "sacco", "info@newsacco.co.ke", "search")
    db.set_status(db.find_lead_by_email("info@newsacco.co.ke")["id"], "verified", "verify")
    sender.run()
    assert mail["delivered"] == ["info@hurimasacco.co.ke"]


def test_first_subject_from_the_template_when_not_recorded(world, mail):
    emailed("info@hurimasacco.co.ke", days_ago=10, subject=None)
    sender.run()
    [msg] = sent_to(mail, "info@hurimasacco.co.ke")
    assert msg["Subject"] == "Re: Loan reminders by SMS"  # sacco template's own subject


def test_subject_is_the_one_the_first_email_went_out_with(world, mail):
    emailed("info@zenka.co.ke", days_ago=10, industry="fintech",
            subject="Loan repayment reminders by SMS — can we talk?")
    sender.run()
    [msg] = sent_to(mail, "info@zenka.co.ke")
    assert msg["Subject"] == "Re: Loan repayment reminders by SMS — can we talk?"


def plain(msg):
    return msg.get_body(("plain",)).get_content()


OFFER_LINE = "If that's something you're looking at, I can send over our rates and API docs."


def test_old_wording_lead_gets_the_bulk_sms_line(world, mail):
    emailed("info@zenka.co.ke", days_ago=20, industry="fintech",
            subject="Loan repayment reminders by SMS — can we talk?")
    sender.run()
    text = plain(sent_to(mail, "info@zenka.co.ke")[0])
    assert f"Just following up on my earlier email about bulk SMS. {OFFER_LINE}" in text
    assert "OTPs and transaction alerts" not in text
    assert "Kind regards,\nFlorence\nTalkSasa Communications Ltd\n+254 762 000 008\n+254 712 295 880\n" in text
    age_all_sends(10)
    sender.run()  # follow-up 2 is the same for everyone
    assert "I won't keep filling your inbox." in plain(sent_to(mail, "info@zenka.co.ke")[1])


def test_unknown_first_subject_counts_as_old_wording(world, mail):
    emailed("info@hurimasacco.co.ke", days_ago=10, subject=None)
    sender.run()
    assert "Just following up on my earlier email about bulk SMS." in plain(sent_to(mail, "info@hurimasacco.co.ke")[0])


def test_current_wording_lead_keeps_the_uses_line(world, mail):
    emailed("info@fintech.co.ke", days_ago=10, industry="fintech", subject="otps and transaction  alerts by SMS")
    sender.run()
    text = plain(sent_to(mail, "info@fintech.co.ke")[0])
    assert f"Just following up on my email about sending OTPs and transaction alerts by SMS. {OFFER_LINE}" in text
    assert "bulk SMS" not in text


def test_every_follow_up_1_has_the_line_old_wording_leads_swap(world):
    import personalise
    for k, v in personalise.USES.items():
        if v.get("approved"):
            assert sender.FOLLOWUP1_LINE in sender.followup_template({"industry": k}).read_text(encoding="utf-8")


def test_every_approved_industry_has_a_follow_up():
    import personalise
    approved = [k for k, v in personalise.USES.items() if v.get("approved")]
    missing = [f"followup{n}/{k}" for n in (1, 2) for k in approved
               if not sender.followup_template({"industry": k}, n)]
    assert missing == []
    assert set(approved) <= set(sender.FOLLOWUP_OFFERS)


def test_every_industry_with_an_offer_has_its_follow_ups_written():
    """An industry gets an offer when its emails are written, before it is approved: approving
    it in uses.yaml must be all that's left to do."""
    for k in sender.FOLLOWUP_OFFERS:
        assert (sender.TEMPLATE_DIR / f"{k}.txt").exists(), k
        for n in (1, 2):
            path = sender.followup_template({"industry": k}, n)
            assert path, f"followup{n}/{k}"
        assert sender.FOLLOWUP1_LINE in sender.followup_template({"industry": k}).read_text(encoding="utf-8")


def test_written_but_unapproved_industries_get_nothing(world):
    import personalise
    for k in ("microfinance", "insurance", "health", "logistics", "ecommerce", "real_estate"):
        assert not personalise.approved({"industry": k}), k


@pytest.mark.parametrize("start, expected", [
    ("2026-10-16", "2026-10-23"),  # Friday + 5 working days = next Friday
    ("2026-10-12", "2026-10-19"),  # Monday -> next Monday
    ("2026-10-17", "2026-10-23"),  # Saturday -> Friday
])
def test_business_days(start, expected):
    assert sender.business_days_after(date.fromisoformat(start), 5) == date.fromisoformat(expected)
