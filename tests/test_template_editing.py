"""Admins edit the email templates from the CRM: what they save is what the bot sends next,
and a template that would break sending is refused and leaves the file as it was."""
import shutil

import pytest
from fastapi import HTTPException

import config
import crm
import db
import sender

FOLLOWUP1 = sender.FOLLOWUP1_LINE


@pytest.fixture
def admin(tmp_path, monkeypatch):
    """A fresh database with one admin, and a copy of the templates the CRM and the bot both use."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    templates = tmp_path / "templates"
    shutil.copytree(sender.TEMPLATE_DIR, templates)
    monkeypatch.setattr(crm, "TEMPLATE_DIR", templates)
    monkeypatch.setattr(crm, "TEMPLATE_KINDS", {"first": templates, "followup": templates / "followup",
                                                "followup2": templates / "followup2"})
    monkeypatch.setattr(sender, "TEMPLATE_DIR", templates)
    monkeypatch.setattr(sender, "FOLLOWUP_DIR", templates / "followup")
    monkeypatch.setattr(sender, "FOLLOWUP2_DIR", templates / "followup2")
    db.init()
    crm.ensure_schema()
    with crm.conn() as c:
        uid = c.execute("INSERT INTO users (name, email, role, password_hash, must_change) VALUES (?,?,?,?,0)"
                        " RETURNING id", ("Joyce", "joyce@example.com", "admin", "x")).fetchone()[0]
    return {"id": uid, "role": "admin", "dir": templates}


def save(admin, kind, name, body, subject=None):
    return crm.save_template(kind, name, crm.TemplateIn(subject=subject, body=body), admin)


def test_the_bot_sends_what_was_saved(admin):
    save(admin, "first", "sacco", "Hello {company} team,\n\nNew wording.\n\n{attachment_line}", "SMS for your members")
    msg, _ = sender.build({"company": "Umoja SACCO", "industry": "sacco", "email": "info@umoja.example"})
    assert msg["Subject"] == "SMS for your members"
    assert "Hello Umoja SACCO team," in sender.plain_of(msg) and "New wording." in sender.plain_of(msg)


def test_saved_file_keeps_its_format(admin):
    save(admin, "first", "school", "Hi {company},\n\nText.", "A subject")
    raw = (admin["dir"] / "school.txt").read_bytes()
    assert raw == b"Subject: A subject\nHi {company},\n\nText.\n"  # no CRLF, subject line first


@pytest.mark.parametrize("body, why", [
    ("Hello {compnay} team", "isn't a gap"),          # a typo in a gap
    ("Hello {company team", "without its partner"),   # a brace left open
    ("Hello {} team", "isn't a gap"),
    ("Hello {company!r} team", "isn't a gap"),
    ("Hello {offer}", "isn't a gap"),                  # only follow-ups have an offer
    ("   ", "can't be empty"),
])
def test_a_template_that_would_break_sending_is_refused(admin, body, why):
    before = (admin["dir"] / "sacco.txt").read_text(encoding="utf-8")
    with pytest.raises(HTTPException) as e:
        save(admin, "first", "sacco", body, "Subject")
    assert e.value.status_code == 400 and why in e.value.detail
    assert (admin["dir"] / "sacco.txt").read_text(encoding="utf-8") == before


def test_first_email_needs_a_subject_and_at_most_three(admin):
    for subject, why in (("", "can't be empty"), ("A\nB\nC\nD", "at most 3"), ("x" * 151, "under 150")):
        with pytest.raises(HTTPException) as e:
            save(admin, "first", "sacco", "Hello {company}", subject)
        assert why in e.value.detail


def test_several_subjects_are_saved_one_per_line(admin, tmp_path):
    save(admin, "first", "school", "Hi {company},\n\nText.", "Fee reminders by SMS\n\nSMS for {company}")
    raw = (crm.TEMPLATE_DIR / "school.txt").read_bytes()
    assert raw == b"Subject: Fee reminders by SMS\nSubject: SMS for {company}\nHi {company},\n\nText.\n"
    t = crm.get_template("first", "school", admin)
    assert t["subject"] == "Fee reminders by SMS\nSMS for {company}"


def test_follow_up_1_keeps_the_line_swapped_for_old_wording(admin):
    with pytest.raises(HTTPException) as e:
        save(admin, "followup", "sacco", "Hello {company} team,\n\nJust checking in. {offer}")
    assert FOLLOWUP1 in e.value.detail
    save(admin, "followup", "sacco", f"Hello {{company}} team,\n\n{FOLLOWUP1} Also: {{offer}}")
    assert "Also:" in (admin["dir"] / "followup" / "sacco.txt").read_text(encoding="utf-8")


def test_history_keeps_each_replaced_version_and_restores_it(admin):
    original = crm.get_template("first", "sacco", admin)
    save(admin, "first", "sacco", "Version two {company}", "Two")
    save(admin, "first", "sacco", "Version three {company}", "Three")
    history = crm.get_template("first", "sacco", admin)["history"]
    assert len(history) == 2 and history[0]["who"] == "Joyce"
    crm.restore_template("first", "sacco", history[-1]["id"], admin)  # the oldest: the original file
    now = crm.get_template("first", "sacco", admin)
    assert (now["subject"], now["body"]) == (original["subject"], original["body"])
    assert len(now["history"]) == 3  # "Version three" was kept before restoring


def test_saving_the_same_text_adds_no_history(admin):
    t = crm.get_template("first", "sacco", admin)
    save(admin, "first", "sacco", t["body"], t["subject"])
    assert crm.get_template("first", "sacco", admin)["history"] == []


@pytest.mark.parametrize("kind, name", [("first", "../config"), ("first", "nosuch"), ("other", "sacco"),
                                        ("followup2", "retail")])
def test_only_existing_templates_can_be_opened(admin, kind, name):
    with pytest.raises(HTTPException) as e:
        crm.get_template(kind, name, admin)
    assert e.value.status_code == 404


def test_preview_shows_the_email_or_the_problem(admin):
    ok = crm.preview_template("first", "sacco", crm.TemplateIn(subject="Hi {company}", body="Dear {company}"), admin)
    assert ok["problem"] is None and ok["subject"] == "Hi Example Company" and "Dear Example Company" in ok["text"]
    bad = crm.preview_template("first", "sacco", crm.TemplateIn(subject="Hi", body="Dear {nme}"), admin)
    assert "isn't a gap" in bad["problem"]
    follow = crm.preview_template("followup2", "sacco", crm.TemplateIn(body="Bye {company}. {offer}"), admin)
    assert follow["subject"].startswith("Re: ") and follow["attachments"] == []


def test_sales_cannot_edit_templates():
    with pytest.raises(HTTPException) as e:
        crm.admin_only({"id": 2, "role": "sales"})
    assert e.value.status_code == 403
