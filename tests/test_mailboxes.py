"""More than one sending mailbox: each warms up slowly and keeps to its own limit, the daily
limit still covers them all, a follow-up goes from the mailbox the first email went from, and
the inbox check reads every mailbox. Fake mail servers; no email is sent."""
from datetime import date, timedelta

import pytest

import config
import db
import inbox
import ranking
import sender
from fakemail import FakeMailbox, message

LEADS = [f"info@sacco{n}.co.ke" for n in range(1, 9)]
TODAY = date.fromisoformat("2026-10-27")


@pytest.fixture
def two(tmp_path, monkeypatch):
    """Eight verified SACCO leads, and a second mailbox on another domain started today."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 10)
    monkeypatch.setattr(config, "SEND_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", False)
    monkeypatch.setattr(config, "SMTP_USER", "florence@talksasa.co.ke")
    monkeypatch.setattr(ranking, "fill_years", lambda *a, **k: 0)
    saved = []
    monkeypatch.setattr(sender, "save_to_sent", lambda msg, at, mb=None: saved.append(mb["key"] if mb else "1"))
    monkeypatch.setattr(sender.notify, "send", lambda text: None)
    monkeypatch.setenv("MAILBOX_2_EMAIL", "florence@talksasa-mail.co.ke")
    monkeypatch.setenv("MAILBOX_2_SMTP_PASS", "secret")
    monkeypatch.setenv("MAILBOX_2_START_DATE", db.today())
    db.init()
    for email in LEADS:
        db.add_lead(email.split("@")[1], email.split("@")[1], "sacco", email, "search")
        db.set_status(db.find_lead_by_email(email)["id"], "verified", "verify")
    return saved


def test_one_mailbox_is_the_settings_already_there(monkeypatch):
    for n in range(2, config.MAX_MAILBOXES + 1):
        monkeypatch.delenv(f"MAILBOX_{n}_EMAIL", raising=False)
    boxes = config.mailboxes()
    assert len(boxes) == 1
    assert (boxes[0]["key"], boxes[0]["email"], boxes[0]["max_per_day"]) == ("1", config.FROM_EMAIL, None)
    assert config.mailbox_limit(boxes[0], TODAY) is None


def test_a_new_mailbox_borrows_mailbox_1s_servers(two):
    second = config.mailboxes()[1]
    assert (second["key"], second["smtp_host"], second["smtp_user"], second["imap_user"], second["imap_pass"]) == (
        "2", config.SMTP_HOST, "florence@talksasa-mail.co.ke", "florence@talksasa-mail.co.ke", "secret")


@pytest.mark.parametrize("started, limit", [(0, 5), (6, 5), (7, 10), (21, 20), (100, 30), (None, 5)])
def test_a_new_mailbox_warms_up_week_by_week(started, limit):
    box = {"key": "2", "max_per_day": 30,
           "start_date": (TODAY - timedelta(days=started)).isoformat() if started is not None else None}
    assert config.mailbox_limit(box, TODAY) == limit


def test_emails_are_shared_out_and_the_new_mailbox_keeps_to_its_warm_up(two, smtp, monkeypatch):
    monkeypatch.setattr(config, "DAILY_LIMIT", 10)
    sender.run()
    froms = [f for _, f, _ in smtp["sent"]]
    assert len(froms) == 8
    assert sum("talksasa-mail.co.ke" in f for f in froms) == 4  # shared out: 4 each, under its 5
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM sent WHERE mailbox = '2'").fetchone()[0] == 4
    # each email logged in to the mailbox it says it's from, and was filed in that mailbox's Sent
    for _, f, user in smtp["sent"]:
        assert ("talksasa-mail.co.ke" in f) == (user == "florence@talksasa-mail.co.ke")
    assert sorted(two) == ["1"] * 4 + ["2"] * 4


def test_the_new_mailbox_stops_at_its_limit(two, smtp, monkeypatch):
    for email in [f"info@more{n}.co.ke" for n in range(1, 7)]:
        db.add_lead(email.split("@")[1], email.split("@")[1], "sacco", email, "search")
        db.set_status(db.find_lead_by_email(email)["id"], "verified", "verify")
    monkeypatch.setattr(config, "DAILY_LIMIT", 14)
    monkeypatch.setattr(config, "DAILY_LIMIT_OVERRIDE", True)
    sender.run()
    froms = [f for _, f, _ in smtp["sent"]]
    assert len(froms) == 14
    assert sum("talksasa-mail.co.ke" in f for f in froms) == 5  # its warm-up limit on day one


def test_the_daily_limit_still_covers_every_mailbox(two, smtp, monkeypatch):
    monkeypatch.setattr(config, "DAILY_LIMIT", 3)
    sender.run()
    assert len(smtp["delivered"]) == 3
    assert db.claim_send(db.find_lead_by_email(LEADS[-1])["id"], 0, config.daily_limit(), mailbox="2",
                         mailbox_limit=5) == "daily limit of 3 reached"


def test_a_follow_up_goes_from_the_mailbox_the_first_email_went_from(two, smtp, monkeypatch):
    lead = db.find_lead_by_email(LEADS[0])
    msg, tname = sender.build(lead, config.mailboxes()[1])
    db.record_sent(lead["id"], msg["Message-ID"], tname, str(msg["Subject"]), "Body", "template", 1, "2")
    db.set_status(lead["id"], "sent", "send")
    assert db.first_mailbox_of(lead["id"]) == "2"
    picked = sender.pick_mailbox(lead, 1, config.mailboxes(), set())
    assert picked["key"] == "2"
    follow, _ = sender.build_followup(db.find_lead_by_email(LEADS[0]), 1, picked)
    assert "talksasa-mail.co.ke" in follow["From"] and follow["In-Reply-To"] == msg["Message-ID"]
    assert "florence@talksasa-mail.co.ke?subject=unsubscribe" in follow["List-Unsubscribe"]
    assert sender.pick_mailbox(lead, 1, config.mailboxes(), {"2"}) is None  # waits for its own mailbox


def test_a_refused_login_pauses_that_mailbox_only_and_isnt_the_leads_fault(two, smtp):
    smtp["refused_logins"].add("florence@talksasa-mail.co.ke")
    sender.run()
    assert len(smtp["delivered"]) == 8
    assert all("talksasa.co.ke" in f for _, f, _ in smtp["sent"])
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM lead_events WHERE event_type = 'send_failed'").fetchone()[0] == 0


class Accounts:
    """imaplib.IMAP4_SSL for several mailboxes: login picks which one is read."""

    def __init__(self, boxes, down=()):
        self.boxes, self.down = boxes, set(down)

    def __call__(self, host):
        outer = self

        class Session:
            def login(self, user, password):
                if user in outer.down:
                    raise OSError("mailbox unreachable")
                self.box = outer.boxes[user]

            def __getattr__(self, name):
                return getattr(self.box, name)
        return Session()


@pytest.fixture
def inboxes(tmp_path, monkeypatch, alerts, ai):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(inbox, "Path", lambda *_: tmp_path / "inbox.py")
    monkeypatch.setenv("MAILBOX_2_EMAIL", "florence@talksasa-mail.co.ke")
    monkeypatch.setenv("MAILBOX_2_SMTP_PASS", "secret")
    db.init()
    for email, box in (("info@one.co.ke", None), ("info@two.co.ke", "2")):
        db.add_lead("Test", email.split("@")[1], "school", email, "manual")
        lead = db.find_lead_by_email(email)
        db.record_sent(lead["id"], f"<1.2.{lead['id']}@x>", "school", mailbox=box)
        db.set_status(lead["id"], "sent", "send")
    return alerts


def test_replies_to_either_mailbox_are_handled(inboxes, monkeypatch):
    one = FakeMailbox({b"1": message("info@one.co.ke", "Re: Fees", "Tell me more")})
    two = FakeMailbox({b"1": message("info@two.co.ke", "Re: Fees", "Interested, call me")})
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", Accounts({config.IMAP_USER: one, "florence@talksasa-mail.co.ke": two}))
    assert inbox.check_once() == 2
    assert {db.find_lead_by_email(e)["status"] for e in ("info@one.co.ke", "info@two.co.ke")} == {"replied"}
    with db.conn() as c:
        keys = sorted(r[0] for r in c.execute("SELECT imap_uid FROM replies"))
    assert keys == ["1", "florence@talksasa-mail.co.ke/1"]  # the same UID in two mailboxes is two emails


def test_an_unreachable_second_mailbox_doesnt_stop_the_first(inboxes, monkeypatch):
    one = FakeMailbox({b"1": message("info@one.co.ke", "Re: Fees", "Tell me more")})
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL",
                        Accounts({config.IMAP_USER: one}, down={"florence@talksasa-mail.co.ke"}))
    assert inbox.check_once() == 1
    inbox.check_once()
    assert sum("couldn't read mailbox 2" in a for a in inboxes) == 1  # one alert a day


def test_a_reply_threading_under_the_second_mailboxs_email_is_ours(two):
    headers = {"In-Reply-To": "<17.123.456@talksasa-mail.co.ke>"}
    assert inbox.answers_our_email(headers, "Hello", set())
    assert not inbox.answers_our_email({"In-Reply-To": "<17.123.456@elsewhere.com>"}, "Hello", set())
