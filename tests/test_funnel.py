"""Knowing what sells: a template can test two or three subjects, every first email records
which one it used, and the report shows replies per template and subject and how fast the team
answers. Temporary database and templates, fake mail server; no email is sent."""
import shutil

import pytest

import config
import db
import ranking
import sender
from test_crm_access import add_user, app, signed_in  # noqa: F401 (app is a fixture)

LEADS = ["one@one.co.ke", "two@two.co.ke", "three@three.co.ke", "four@four.co.ke"]
TWO_SUBJECTS = "Subject: Loan reminders by SMS\nSubject: SMS for {company}\n"


@pytest.fixture
def waiting(tmp_path, monkeypatch):
    """Four verified SACCO leads, and a SACCO template that tests two subjects."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 10)
    monkeypatch.setattr(config, "SEND_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", True)
    monkeypatch.setattr(ranking, "fill_years", lambda *a, **k: 0)
    monkeypatch.setattr(sender, "save_to_sent", lambda *a: True)
    monkeypatch.setattr(sender.notify, "send", lambda text: None)
    templates = tmp_path / "templates"
    shutil.copytree(sender.TEMPLATE_DIR, templates)
    sacco = templates / "sacco.txt"
    sacco.write_text(TWO_SUBJECTS + sacco.read_text(encoding="utf-8").split("\n", 1)[1], encoding="utf-8")
    monkeypatch.setattr(sender, "TEMPLATE_DIR", templates)
    db.init()
    for email in LEADS:
        db.add_lead(email.split("@")[0].title() + " Sacco", email.split("@")[1], "sacco", email, "search")
        db.set_status(db.find_lead_by_email(email)["id"], "verified", "verify")


def sent_rows():
    with db.conn() as c:
        return c.execute("SELECT l.email, s.sent_subject, s.subject_no, s.variant, s.template FROM sent s"
                         " JOIN leads l ON l.id = s.lead_id ORDER BY s.id").fetchall()


def test_a_template_reads_one_or_several_subjects():
    assert sender.split_template("Subject: A\nHello\n\nText") == (["A"], "Hello\n\nText")
    assert sender.split_template("Subject: A\nSubject: B\nHello") == (["A", "B"], "Hello")


def test_each_lead_always_gets_the_same_subject():
    assert sender.subject_for({"id": 7}, ["A", "B"]) == sender.subject_for({"id": 7}, ["A", "B"]) == (2, "B")
    assert sender.subject_for({"id": 8}, ["A", "B"]) == (1, "A")
    assert sender.subject_for({"id": 8}, ["A"]) == (1, "A")


def test_leads_are_split_between_the_subjects_and_each_send_says_which(waiting, smtp):
    sender.run()
    rows = sent_rows()
    assert len(rows) == 4
    assert {r["subject_no"] for r in rows} == {1, 2}
    for r in rows:
        lead = db.find_lead_by_email(r["email"])
        expected = "Loan reminders by SMS" if r["subject_no"] == 1 else f"SMS for {lead['company']}"
        assert r["sent_subject"] == expected
        assert (r["variant"], r["template"]) == ("template", "sacco")


def test_a_lead_sent_the_second_subject_is_not_old_wording(waiting, smtp):
    sender.run()
    second = next(r for r in sent_rows() if r["subject_no"] == 2)
    lead = db.find_lead_by_email(second["email"])
    assert not sender.old_wording(lead, db.sent_thread(lead["id"]))
    msg, _ = sender.build_followup(lead, 1)
    assert str(msg["Subject"]) == "Re: " + second["sent_subject"]
    assert sender.FOLLOWUP1_LINE.split("{")[0] in sender.plain_of(msg)  # the uses line, not the old-wording one


def emailed(email, template, subject_no, status="sent", label=None, source="search"):
    """A lead whose first email went out with this template and subject, with a reply if label is given."""
    db.add_lead(email.split("@")[1], email.split("@")[1], "sacco", email, source)
    lead_id = db.find_lead_by_email(email)["id"]
    db.record_sent(lead_id, f"<{lead_id}@x>", template, "Subject", "Body", "template", subject_no)
    db.set_status(lead_id, status, "test")
    reply_id = db.add_reply(lead_id, email, "Re: Subject", "Tell me more", label, "a reply", "uid-" + email) if label else None
    return lead_id, reply_id


def test_report_counts_each_subject_and_leaves_out_test_leads(app, tmp_path, monkeypatch):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "sacco.txt").write_text(TWO_SUBJECTS + "Hello", encoding="utf-8")
    monkeypatch.setattr(sender, "TEMPLATE_DIR", templates)
    add_user("Ada", "ada@talksasa.co.ke", "admin")
    admin = signed_in(app, "ada@talksasa.co.ke")
    emailed("a@a.co.ke", "sacco", 1, "replied", "interested")
    emailed("b@b.co.ke", "sacco", 1)
    emailed("c@c.co.ke", "sacco", 2)
    emailed("d@d.co.ke", "school", 1)
    emailed("me@test.co.ke", "sacco", 2, "replied", "interested", source="manual")  # a test lead
    d = admin.get("/api/crm/report").json()
    rows = {r["name"]: r for r in d["groups"]["template"]}
    assert set(rows) == {"sacco · Loan reminders by SMS", "sacco · SMS for {company}", "school"}
    assert (rows["sacco · Loan reminders by SMS"]["emailed"], rows["sacco · Loan reminders by SMS"]["interested"]) == (2, 1)
    assert (rows["sacco · SMS for {company}"]["emailed"], rows["sacco · SMS for {company}"]["interested"]) == (1, 0)
    assert d["total"]["emailed"] == 4


def test_report_shows_how_fast_interested_replies_are_answered(app):
    add_user("Ada", "ada@talksasa.co.ke", "admin")
    admin = signed_in(app, "ada@talksasa.co.ke")
    answered, reply_a = emailed("a@a.co.ke", "sacco", 1, "replied", "interested")
    emailed("b@b.co.ke", "sacco", 1, "replied", "question")  # nobody has answered yet
    emailed("c@c.co.ke", "sacco", 1, "replied", "not_interested")  # not counted
    with db.conn() as c:
        c.execute("UPDATE replies SET received_at = '2026-10-20 08:00:00' WHERE id = ?", (reply_a,))
        c.execute("INSERT INTO crm_messages (lead_id, to_email, subject, body, status, sent_at)"
                  " VALUES (?, 'a@a.co.ke', 'Re: Subject', 'Hello', 'sent', '2026-10-20 09:30:00')", (answered,))
    a = admin.get("/api/crm/report").json()["answer_time"]
    assert a == {"answered": 1, "median_hours": 1.5, "within_2h": 1, "waiting": 1}
