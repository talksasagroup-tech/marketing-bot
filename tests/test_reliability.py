"""Leads aren't lost to things that say nothing about them: a mail server's "not now", a DNS
timeout, a company's colleague opting out, a bounce report from an unusual sender, a laptop
asleep at 9am, a lead that keeps failing. Temporary database, fake mail server and DNS."""
import smtplib
from datetime import datetime

import dns.resolver
import pytest

import auto
import config
import db
import inbox
import ranking
import sender
import verify
from fakemail import SENT_ID, message, run

LEADS = ["one@one.co.ke", "two@two.co.ke", "three@three.co.ke"]


@pytest.fixture
def waiting(tmp_path, monkeypatch):
    """Three verified SACCO leads (an approved industry), in this sending order."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 10)
    monkeypatch.setattr(config, "SEND_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", False)
    monkeypatch.setattr(ranking, "fill_years", lambda *a, **k: 0)
    monkeypatch.setattr(sender, "save_to_sent", lambda *a: True)
    telegram = []
    monkeypatch.setattr(sender.notify, "send", telegram.append)
    db.init()
    for email in LEADS:
        db.add_lead("Test Sacco", email.split("@")[1], "sacco", email, "search")
        db.set_status(db.find_lead_by_email(email)["id"], "verified", "verify")
    return telegram


def status(email):
    return db.find_lead_by_email(email)["status"]


# --- A-03: only a permanent refusal is a bounce

def test_temporary_refusal_is_not_a_bounce(waiting, smtp):
    smtp["errors"]["two@two.co.ke"] = [smtplib.SMTPRecipientsRefused({"two@two.co.ke": (450, b"Greylisted")})]
    sender.run()
    assert status("two@two.co.ke") == "verified"  # tried again next time
    assert not db.is_suppressed("two@two.co.ke")
    with db.conn() as c:
        failed = c.execute("SELECT detail FROM lead_events WHERE event_type='send_failed'").fetchall()
    assert len(failed) == 1 and "450" in failed[0][0]
    assert len(smtp["delivered"]) == 2


def test_permanent_refusal_is_still_a_bounce(waiting, smtp):
    smtp["errors"]["two@two.co.ke"] = [smtplib.SMTPRecipientsRefused({"two@two.co.ke": (550, b"No such user")})]
    sender.run()
    assert status("two@two.co.ke") == "bounced" and db.is_suppressed("two@two.co.ke")


# --- A-08: DNS trouble doesn't make a lead invalid

@pytest.fixture
def new_lead(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(verify, "_cache", {})
    db.init()
    db.add_lead("Test", "acme.co.ke", "sacco", "info@acme.co.ke", "csv")
    return "info@acme.co.ke"


def fake_dns(monkeypatch, error):
    def resolve(domain, record, lifetime=None):
        raise error
    monkeypatch.setattr(verify.dns.resolver, "resolve", resolve)


def test_dns_timeout_leaves_the_lead_to_check_again(new_lead, monkeypatch):
    fake_dns(monkeypatch, dns.resolver.LifetimeTimeout())
    verify.run()
    assert status(new_lead) == "new"
    assert verify._cache == {}  # not remembered: the next check asks DNS again


def test_domain_that_does_not_exist_is_invalid(new_lead, monkeypatch):
    fake_dns(monkeypatch, dns.resolver.NXDOMAIN())
    verify.run()
    assert status(new_lead) == "invalid"


def test_no_mx_but_a_record_timed_out_is_not_invalid(new_lead, monkeypatch):
    def resolve(domain, record, lifetime=None):
        raise dns.resolver.NoAnswer() if record == "MX" else dns.resolver.LifetimeTimeout()
    monkeypatch.setattr(verify.dns.resolver, "resolve", resolve)
    verify.run()
    assert status(new_lead) == "new"


# --- A-09: the daily limit is held to MAX_DAILY_LIMIT unless overridden

def test_daily_limit_over_the_maximum_is_held_back(monkeypatch):
    monkeypatch.setattr(config, "DAILY_LIMIT", 25)
    monkeypatch.setattr(config, "DAILY_LIMIT_OVERRIDE", False)
    assert config.daily_limit() == config.MAX_DAILY_LIMIT == 10
    assert "unless DAILY_LIMIT_OVERRIDE" in sender.limit_override_note()


def test_override_lets_the_daily_limit_go_higher(monkeypatch):
    monkeypatch.setattr(config, "DAILY_LIMIT", 25)
    monkeypatch.setattr(config, "DAILY_LIMIT_OVERRIDE", True)
    assert config.daily_limit() == 25


def test_held_back_limit_is_what_the_sender_keeps_to(waiting, smtp, monkeypatch):
    monkeypatch.setattr(config, "DAILY_LIMIT", 25)
    monkeypatch.setattr(config, "MAX_DAILY_LIMIT", 2)
    sender.run()
    assert len(smtp["delivered"]) == 2


def test_override_is_announced_on_telegram(waiting, smtp, monkeypatch):
    monkeypatch.setattr(config, "DAILY_LIMIT", 25)
    monkeypatch.setattr(config, "DAILY_LIMIT_OVERRIDE", True)
    sender.run()
    assert any("DAILY_LIMIT_OVERRIDE" in t for t in waiting)


# --- A-10: "today" is Nairobi's

class FakeDatetime(datetime):
    """1 a.m. in Nairobi on 28 Oct is still 27 Oct in UTC."""
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 27, 22, 0, tzinfo=config.NAIROBI.__class__.utc).astimezone(tz)


def test_today_is_the_nairobi_date(monkeypatch):
    monkeypatch.setattr(db, "datetime", FakeDatetime)
    assert db.today() == "2026-10-28"


def test_an_email_sent_after_midnight_counts_for_the_new_day(waiting, smtp, monkeypatch):
    monkeypatch.setattr(db, "datetime", FakeDatetime)
    sender.run()
    with db.conn() as c:
        assert {r[0] for r in c.execute("SELECT sent_on FROM sent")} == {"2026-10-28"}
        assert all(r[0] for r in c.execute("SELECT sent_at FROM sent"))  # the UTC time too
    assert db.sent_today() == 3


# --- A-11: an organisation that opts out isn't emailed at another address

def test_company_opt_out_blocks_its_other_addresses(waiting):
    db.unsubscribe("one@one.co.ke")
    assert db.blocked("sales@one.co.ke")
    assert not db.add_lead("One again", "one.co.ke", "sacco", "sales@one.co.ke", "search")


def test_company_opt_out_stops_a_lead_already_waiting(waiting, smtp):
    db.add_lead("One", "one.co.ke", "sacco", "sales@one.co.ke", "csv")
    db.set_status(db.find_lead_by_email("sales@one.co.ke")["id"], "verified", "verify")
    db.unsubscribe("one@one.co.ke")
    sender.run()
    assert "sales@one.co.ke" not in smtp["delivered"]


def test_free_mail_opt_out_blocks_only_that_address(waiting):
    db.unsubscribe("someone@gmail.com")
    assert db.blocked("someone@gmail.com")
    assert not db.blocked("other@gmail.com")


def test_a_bounce_blocks_only_its_own_address(waiting, smtp):
    smtp["errors"]["two@two.co.ke"] = [smtplib.SMTPRecipientsRefused({"two@two.co.ke": (550, b"No such user")})]
    sender.run()
    assert db.blocked("two@two.co.ke") and not db.blocked("info@two.co.ke")


# --- A-12: delivery reports are recognised by their type, not only their sender

def test_delivery_report_from_any_sender_is_a_bounce(leads, ai, alerts, monkeypatch):
    report = ("Content-Type: multipart/report; report-type=delivery-status; boundary=b\r\n\r\n"
              "--b\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
              "--b\r\nContent-Type: message/delivery-status\r\n\r\n"
              "Final-Recipient: rfc822; info@nyerihigh.com\r\nAction: failed\r\nStatus: 5.1.1\r\n--b--\r\n")
    raw = (f"From: bounces@mailprovider.net\r\nTo: florence@talksasa.co.ke\r\n"
           f"Subject: Undelivered Mail\r\nIn-Reply-To: {SENT_ID}\r\n{report}").encode()
    run(monkeypatch, raw)
    assert db.find_lead_by_email("info@nyerihigh.com")["status"] == "bounced"
    assert not ai


def test_ordinary_mail_from_a_bounces_address_is_not_a_bounce(leads, ai, alerts, monkeypatch):
    run(monkeypatch, message("bounces@mailprovider.net", "Newsletter", "Hello"))
    assert db.find_lead_by_email("info@nyerihigh.com")["status"] == "sent"


# --- A-13: a lead that keeps failing stops taking a slot

def test_lead_that_failed_three_times_is_not_planned(waiting, smtp):
    lead_id = db.find_lead_by_email("one@one.co.ke")["id"]
    with db.conn() as c:
        for _ in range(db.MAX_SEND_FAILURES):
            db.log_event(c, lead_id, "send_failed", error="554 rejected")
    sender.run()
    assert "one@one.co.ke" not in smtp["delivered"]
    assert sorted(smtp["delivered"]) == sorted(LEADS[1:])


def test_lead_that_failed_once_goes_after_the_others(waiting):
    lead_id = db.find_lead_by_email("one@one.co.ke")["id"]
    with db.conn() as c:
        db.log_event(c, lead_id, "send_failed", error="554 rejected")
    assert [l["email"] for l in ranking.waiting()][-1] == "one@one.co.ke"


# --- A-16: a missed 9am still runs that day, once

@pytest.fixture
def marker(tmp_path, monkeypatch):
    monkeypatch.setattr(auto, "MARKER", tmp_path / ".auto_last_run")
    return auto.MARKER


def at(hour, day=28):  # 28 Oct 2026 is a Wednesday
    return datetime(2026, 10, day, hour, 0, tzinfo=config.NAIROBI)


def test_daily_run_catches_up_later_the_same_day(marker):
    assert not auto.daily_due(at(8))
    assert auto.daily_due(at(11))
    marker.write_text(at(11).date().isoformat())
    assert not auto.daily_due(at(14))  # once a day


def test_no_daily_run_at_the_weekend(marker):
    assert not auto.daily_due(at(11, day=31))  # Saturday


# --- A-05: failing inbox checks are alerted once, and their recovery

def test_three_failed_inbox_checks_alert_once_then_recovery(alerts, tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "ERROR_FILE", tmp_path / "inbox_error.txt")
    monkeypatch.setattr(inbox, "_failures", 0)

    def broken():
        raise OSError("Temporary failure in name resolution")
    for _ in range(4):
        with pytest.raises(OSError):
            inbox.checked(broken)
    assert len(alerts) == 1 and "3 times" in alerts[0]
    assert inbox.last_error()["failures"] == 4
    assert inbox.checked(lambda: 2) == 2
    assert len(alerts) == 2 and "again" in alerts[1]
    assert inbox.last_error() is None
