"""Phone numbers are kept with each lead found on the web, so the sales team can call the
ones that never answer an email. Pages are given as HTML here; nothing is downloaded."""
import csv

import pytest
from bs4 import BeautifulSoup

import config
import db
import discover
import scraper


@pytest.mark.parametrize("written, number", [
    ("0712 345 678", "+254712345678"),
    ("0712-345-678", "+254712345678"),
    ("+254 712 345678", "+254712345678"),
    ("254712345678", "+254712345678"),
    ("0110 123 456", "+254110123456"),
    ("+254 (0) 20 123 4567", "+254201234567"),
    ("020 2345678", "+254202345678"),
    ("041-2223334", "+254412223334"),
])
def test_kenyan_numbers_are_read_however_they_are_written(written, number):
    assert scraper.phone_number(written) == number


@pytest.mark.parametrize("not_a_phone", ["00100", "0712 345", "+254 812 345 678", "2023-2024", "0 0000 0000"])
def test_things_that_are_not_phone_numbers(not_a_phone):
    assert scraper.phone_number(not_a_phone) is None


PAGE = """<html><body>
<p>P.O. Box 12345-00100, Nairobi. Founded 2019.</p>
<p>Call us: 020 2345678 or <a href="tel:+254712345678">0712 345 678</a></p>
<p>WhatsApp 0712345678</p>
</body></html>"""


def test_a_page_gives_each_number_once_tel_links_first():
    phones = scraper.phones_on(BeautifulSoup(PAGE, "html.parser"))
    assert phones == ["+254712345678", "+254202345678"]


def test_a_mobile_is_picked_over_a_landline():
    assert scraper.pick_phone(["+254202345678", "+254712345678"]) == "+254712345678"
    assert scraper.pick_phone(["+254202345678"]) == "+254202345678"
    assert scraper.pick_phone([]) is None


@pytest.fixture
def found(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(discover, "CSV_PATH", tmp_path / "discovered.csv")
    monkeypatch.setattr(discover, "look_at", lambda url: ("Umoja Sacco | Home", True))
    monkeypatch.setattr(discover.scraper, "scrape_contacts",
                        lambda home: (["info@umojasacco.co.ke"], ["+254202345678", "+254712345678"]))
    db.init()
    return tmp_path / "discovered.csv"


def test_a_lead_found_by_search_keeps_its_phone(found):
    assert discover.consider("https://umojasacco.co.ke", "sacco", "sacco Nairobi", set())
    assert db.find_lead_by_email("info@umojasacco.co.ke")["phone"] == "+254712345678"
    with open(found, newline="", encoding="utf-8-sig") as f:
        assert next(csv.DictReader(f))["phone"] == "+254712345678"


def test_an_old_csv_gets_the_phone_column_without_losing_rows(found):
    with open(found, "w", newline="", encoding="utf-8-sig") as f:
        f.write("company,website,industry,email,query\r\nOld Co,old.co.ke,school,info@old.co.ke,school Nairobi\r\n")
    discover.consider("https://umojasacco.co.ke", "sacco", "sacco Nairobi", set())
    with open(found, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert [(r["company"], r["phone"], r["query"]) for r in rows] == [
        ("Old Co", "", "school Nairobi"), ("Umoja Sacco", "+254712345678", "sacco Nairobi")]


def test_a_csv_import_keeps_the_phone_it_was_given(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    path = tmp_path / "companies.csv"
    path.write_text("company,website,industry,email,phone\nUmoja,umoja.co.ke,sacco,info@umoja.co.ke,0712 345 678\n")
    scraper.run(path)
    assert db.find_lead_by_email("info@umoja.co.ke")["phone"] == "+254712345678"
