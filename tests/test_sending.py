"""F7: one failed email never stops the rest of the day's batch. Runs sender.run against a
temporary database and a fake SMTP server: no real email is sent."""
import smtplib
import sqlite3

import pytest

import config
import db
import ranking
import sender

LEADS = ["one@one.co.ke", "two@two.co.ke", "three@three.co.ke", "four@four.co.ke"]


@pytest.fixture
def waiting(tmp_path, monkeypatch):
    """Four verified SACCO leads (an approved industry), in this sending order."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 10)
    monkeypatch.setattr(config, "SEND_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", False)
    monkeypatch.setattr(config, "FROM_EMAIL", "florence@talksasa.co.ke")
    monkeypatch.setattr(ranking, "fill_years", lambda *a, **k: 0)  # no WHOIS or website lookups
    monkeypatch.setattr(sender, "save_to_sent", lambda *a: True)  # no IMAP
    telegram = []
    monkeypatch.setattr(sender.notify, "send", telegram.append)  # no Telegram
    db.init()
    for email in LEADS:
        db.add_lead("Test Sacco", email.split("@")[1], "sacco", email, "search")
        db.set_status(db.find_lead_by_email(email)["id"], "verified", "verify")
    return telegram


def status(email):
    return db.find_lead_by_email(email)["status"]


def failed_events(email):
    with db.conn() as c:
        return c.execute("SELECT detail FROM lead_events WHERE event_type='send_failed' AND lead_id=?",
                         (db.find_lead_by_email(email)["id"],)).fetchall()


def test_all_sent(waiting, smtp):
    sender.run()
    assert smtp["delivered"] == LEADS
    assert all(status(e) == "sent" for e in LEADS)


def test_rejected_email_does_not_stop_the_batch(waiting, smtp):
    smtp["errors"]["two@two.co.ke"] = [smtplib.SMTPDataError(550, b"Message rejected")]
    sender.run()
    assert smtp["delivered"] == ["one@one.co.ke", "three@three.co.ke", "four@four.co.ke"]
    assert status("two@two.co.ke") == "verified"  # waiting: tried again next time
    assert "550" in failed_events("two@two.co.ke")[0][0]


def test_temporary_error_does_not_stop_the_batch(waiting, smtp):
    smtp["errors"]["two@two.co.ke"] = [smtplib.SMTPDataError(451, b"Try again later")]
    sender.run()
    assert len(smtp["delivered"]) == 3 and status("two@two.co.ke") == "verified"


def test_dropped_connection_reconnects_and_retries_the_lead(waiting, smtp):
    smtp["errors"]["two@two.co.ke"] = [smtplib.SMTPServerDisconnected("Connection unexpectedly closed")]
    sender.run()
    assert smtp["delivered"] == LEADS
    assert smtp["connects"] == 2


def test_server_closing_421_reconnects(waiting, smtp):
    smtp["errors"]["two@two.co.ke"] = [smtplib.SMTPDataError(421, b"Service closing")]
    sender.run()
    assert smtp["delivered"] == LEADS and smtp["connects"] == 2


def test_cannot_reconnect_stops_cleanly_and_leaves_the_rest_waiting(waiting, smtp):
    smtp["connect_ok"] = 1
    smtp["errors"]["two@two.co.ke"] = [ConnectionResetError("reset by peer")]
    sender.run()  # no exception
    assert smtp["delivered"] == ["one@one.co.ke"]
    assert [status(e) for e in LEADS[1:]] == ["verified"] * 3


def test_refused_recipient_is_bounced_and_the_rest_go_out(waiting, smtp):
    smtp["errors"]["two@two.co.ke"] = [smtplib.SMTPRecipientsRefused({"two@two.co.ke": (550, b"No such user")})]
    sender.run()
    assert status("two@two.co.ke") == "bounced"
    assert db.is_suppressed("two@two.co.ke")
    assert len(smtp["delivered"]) == 3


def test_three_failures_in_a_row_stop_the_batch(waiting, smtp):
    for email in LEADS[:3]:
        smtp["errors"][email] = [smtplib.SMTPDataError(554, b"Spam detected")]
    sender.run()
    assert smtp["delivered"] == []
    assert not failed_events("four@four.co.ke")  # never tried
    assert status("four@four.co.ke") == "verified"


def test_sender_refused_stops_at_once(waiting, smtp):
    smtp["errors"]["one@one.co.ke"] = [smtplib.SMTPSenderRefused(553, b"Sender not allowed", "florence@talksasa.co.ke")]
    sender.run()
    assert smtp["delivered"] == []
    assert not failed_events("two@two.co.ke")
    assert not failed_events("one@one.co.ke")  # our mailbox was refused, not this lead


def test_broken_template_skips_only_that_lead(waiting, smtp, monkeypatch):
    real_build = sender.build

    def build(lead):
        if lead["email"] == "two@two.co.ke":
            raise KeyError("phone_number")
        return real_build(lead)

    monkeypatch.setattr(sender, "build", build)
    sender.run()
    assert smtp["delivered"] == ["one@one.co.ke", "three@three.co.ke", "four@four.co.ke"]
    assert status("two@two.co.ke") == "verified"


def test_error_after_the_email_went_out_stops_without_sending_twice(waiting, smtp, monkeypatch):
    def broken_record(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "record_sent", broken_record)
    with pytest.raises(sqlite3.OperationalError):
        sender.run()
    assert smtp["delivered"] == ["one@one.co.ke"]  # gone once, never retried


# --- Bounce gate ---

def history(emailed, bounced, days_ago=1):
    """Companies already emailed `days_ago` days before this send; the first `bounced` of
    them bounced (found by the inbox check later, as most bounces are)."""
    for i in range(emailed):
        email = f"past{days_ago}d{i}@past{days_ago}d{i}.co.ke"
        db.add_lead("Past", email.split("@")[1], "sacco", email, "search")
        lead_id = db.find_lead_by_email(email)["id"]
        db.record_sent(lead_id, f"<{days_ago}d{i}@x>", "sacco")
        db.set_status(lead_id, "bounced" if i < bounced else "sent", "inbox" if i < bounced else "send")
        with db.conn() as c:  # sent on an earlier day: dated events, today's daily limit untouched
            c.execute("UPDATE lead_events SET created_at = datetime('now', ?) WHERE lead_id = ?",
                      (f"-{days_ago} days", lead_id))
            c.execute("UPDATE sent SET sent_on = date('now', ?) WHERE lead_id = ?", (f"-{days_ago} days", lead_id))


def test_gate_pauses_sending_above_the_limit(waiting, smtp):
    history(50, 2)  # 4%
    sender.run()
    assert smtp["connects"] == 0 and smtp["delivered"] == []
    assert all(status(e) == "verified" for e in LEADS)
    assert len(waiting) == 1 and "paused" in waiting[0]  # Telegram told


def test_gate_open_below_the_limit(waiting, smtp):
    history(50, 1)  # 2%
    sender.run()
    assert smtp["delivered"] == LEADS


def test_gate_needs_enough_sends_to_judge(waiting, smtp):
    history(10, 3)  # 30%, but only 10 sent
    sender.run()
    assert smtp["delivered"] == LEADS


def test_gate_limit_can_be_raised(waiting, smtp, monkeypatch):
    monkeypatch.setattr(config, "BOUNCE_LIMIT_PERCENT", 5)
    history(50, 2)  # 4%
    sender.run()
    assert smtp["delivered"] == LEADS


def test_gate_reopens_once_the_bounced_sends_age_out(waiting, smtp):
    history(50, 3, days_ago=20)  # 6%, but sent 20 days ago: outside the 14-day window
    sender.run()
    assert smtp["delivered"] == LEADS


def test_gate_counts_only_sends_inside_the_window(waiting, smtp):
    history(30, 3, days_ago=20)  # old bounces: not counted
    history(30, 0, days_ago=2)   # 0 of 30 recent sends bounced
    sender.run()
    assert smtp["delivered"] == LEADS


def test_late_bounce_of_a_recent_send_counts(waiting, smtp):
    history(25, 0, days_ago=3)
    history(5, 2, days_ago=13)  # bounced; sent 13 days ago, still inside the window
    sender.run()  # 2 of 30 = 6.7%
    assert smtp["delivered"] == []


def test_gate_reopens_when_too_few_recent_sends_to_judge(waiting, smtp):
    history(40, 3, days_ago=20)
    history(10, 1, days_ago=5)  # 10% but only 10 sends in the window
    sender.run()
    assert smtp["delivered"] == LEADS


def test_bounce_during_the_batch_closes_the_gate(waiting, smtp):
    history(49, 1)  # 2.0%; one more bounce makes 2 of 50 = 4%
    smtp["errors"]["one@one.co.ke"] = [smtplib.SMTPRecipientsRefused({"one@one.co.ke": (550, b"No such user")})]
    sender.run()
    assert status("one@one.co.ke") == "bounced"
    assert smtp["delivered"] == []  # stopped before the next lead
    assert all(status(e) == "verified" for e in LEADS[1:])


def test_dry_run_still_previews_when_paused(waiting, smtp, capsys):
    history(50, 3)
    sender.run(dry_run=True)
    out = capsys.readouterr().out
    assert "would be paused" in out and "(dry run) would send 0 follow-ups and 4 first emails" in out
    assert smtp["connects"] == 0
