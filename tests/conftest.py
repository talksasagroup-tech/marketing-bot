"""Shared fixtures. Every test gets its own temporary database; the AI and Telegram are
replaced by fakes, so nothing real is called, read or sent.

The database is a SQLite file. With TEST_DATABASE_URL set (a PostgreSQL you can create
schemas in), every test gets its own empty PostgreSQL schema instead:
    TEST_DATABASE_URL=postgresql://user:pw@127.0.0.1:5432/test python -m pytest
"""
import os
import smtplib
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import classifier  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import inbox  # noqa: E402
import notify  # noqa: E402
import sender  # noqa: E402
import targeting  # noqa: E402
from fakemail import SENT_ID  # noqa: E402


@pytest.fixture(autouse=True)
def database(monkeypatch):
    """Never the real database from .env: SQLite (each test sets its own DB_PATH), or a
    fresh PostgreSQL schema per test when TEST_DATABASE_URL is set."""
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        monkeypatch.setattr(config, "DATABASE_URL", "")
        yield
        return
    schema = "test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
    monkeypatch.setattr(config, "DATABASE_URL",
                        f"{url}{'&' if '?' in url else '?'}options=-csearch_path%3D{schema}")
    yield
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any test that tries to reach the network (a real mail server, the AI, Telegram,
    WHOIS, a website) fails instead."""
    import socket

    def blocked(*a, **k):
        pytest.fail("a test tried to use the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture(autouse=True)
def no_need_scores(monkeypatch):
    """Leads go out whatever their need score (tests of the score set MIN_SCORE themselves),
    and targeting.py reads no websites: every site counts as unreadable and shows no emails."""
    monkeypatch.setattr(config, "MIN_SCORE", 0)
    monkeypatch.setattr(targeting, "site_text", lambda website: None)
    monkeypatch.setattr(targeting, "site_emails", lambda website: [])


@pytest.fixture(autouse=True)
def same_mailbox_and_sender(monkeypatch):
    """Tests don't depend on .env: one mailbox (mail keys without an account), the address
    emails go out from (replies to our emails are recognised by its domain) and the
    sender name the expected emails are written with."""
    monkeypatch.setattr(config, "FROM_EMAIL", "florence@talksasa.co.ke")
    monkeypatch.setattr(config, "IMAP_USER", "florence@talksasa.co.ke")
    monkeypatch.setattr(config, "FIRST_IMAP_USER", "florence@talksasa.co.ke")
    monkeypatch.setattr(config, "SENDER_NAME", "Florence")


@pytest.fixture
def leads(tmp_path, monkeypatch):
    """A fresh database with three emailed leads, one of them on gmail.com."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(inbox, "Path", lambda *_: tmp_path / "inbox.py")  # heartbeat file
    db.init()
    ids = {}
    for email in ("info@nyerihigh.com", "info@dazzcode.com", "school@gmail.com"):
        db.add_lead("Test", email.split("@")[1], "school", email, "manual")
        lead = db.find_lead_by_email(email)
        db.record_sent(lead["id"], SENT_ID if email == "info@nyerihigh.com" else f"<{email}>", "school")
        db.set_status(lead["id"], "sent", "send")
        ids[email] = lead["id"]
    return ids


@pytest.fixture
def ai(monkeypatch):
    """Every message the AI is asked about; it always answers "interested"."""
    asked = []

    def fake_ask(prompt, groq_model=None):
        asked.append(prompt)
        return '{"label": "interested", "summary": "wants a call", "confidence": 0.9}'

    monkeypatch.setattr(classifier, "ask", fake_ask)
    return asked


@pytest.fixture
def alerts(monkeypatch):
    """Every Telegram message sent."""
    sent = []
    monkeypatch.setattr(notify, "send", sent.append)
    return sent


@pytest.fixture
def smtp(monkeypatch):
    """A fake mail server. errors[address] = exceptions to raise on its next attempts
    (None = accept); connect_ok = how many connections succeed (None = all);
    refused_logins = SMTP users whose login is refused. sent = (to, From, login) per email."""
    world = {"connects": 0, "delivered": [], "errors": {}, "connect_ok": None, "sent": [],
             "refused_logins": set()}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            world["connects"] += 1
            self.user = None
            if world["connect_ok"] is not None and world["connects"] > world["connect_ok"]:
                raise ConnectionRefusedError("mail server down")

        def starttls(self):
            pass

        def login(self, user=None, password=None):
            if user in world["refused_logins"]:
                raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")
            self.user = user

        def quit(self):
            pass

        def send_message(self, msg):
            planned = world["errors"].get(msg["To"])
            error = planned.pop(0) if planned else None
            if error:
                raise error
            world["delivered"].append(msg["To"])
            world["sent"].append((msg["To"], msg["From"], self.user))

    monkeypatch.setattr(sender.smtplib, "SMTP", FakeSMTP)
    return world
