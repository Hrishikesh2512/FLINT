"""Old WhatsApp chats, from "Export chat", into her archive."""

from flint_core.recall import Archive

from carnage.whatsapp import chat_name, import_export, parse, spoken

ANDROID = """15/03/26, 9:05 pm - Messages and calls are end-to-end encrypted.
15/03/26, 9:05 pm - Rahul Verma: Goa plan final? 
15/03/26, 9:06 pm - Hrishikesh: Haan, 12 April
flight book kar diya
15/03/26, 9:07 pm - Rahul Verma: <Media omitted>
16/03/26, 10:00 am - Rahul Verma: Hotel is Taj, ₹8,400 a night
"""

IPHONE = """[03/15/26, 9:05:12 PM] Rahul: hi there
[03/15/26, 9:06:00 PM] Me: hello
"""


def test_android_export_is_read_with_multiline_and_noise_dropped():
    said = parse(ANDROID)
    assert [m.who for m in said] == ["Rahul Verma", "Hrishikesh", "Rahul Verma"]
    assert said[1].text == "Haan, 12 April\nflight book kar diya"
    assert said[1].when.day == 15 and said[1].when.month == 3 and said[1].when.hour == 21
    assert said[2].when.hour == 10


def test_month_first_dates_are_recognised():
    said = parse(IPHONE)
    assert said[0].when.month == 3 and said[0].when.day == 15 and said[0].when.hour == 21


def test_text_that_is_not_an_export_is_left_alone():
    assert parse("just some notes\nabout nothing") == []


def test_the_chat_name_comes_from_the_file_or_subject():
    assert chat_name("WhatsApp Chat with Rahul Verma.txt") == "Rahul Verma"
    assert chat_name("", "WhatsApp Chat with Family") == "Family"


def test_imported_days_are_searchable_and_never_filed_twice(tmp_path):
    archive = Archive(tmp_path / "a.db")
    ledger = tmp_path / "ledger.json"
    first = import_export(archive, ANDROID, "Rahul Verma", ledger=ledger)
    assert first["messages"] == 3 and first["new_days"] == 2
    assert "3 messages" in spoken(first)
    found = archive.search("Rahul hotel Taj")
    assert found and "₹8,400" in found[0].text and "16 Mar 2026" in found[0].text

    again = import_export(archive, ANDROID, "Rahul Verma", ledger=ledger)
    assert again["new_days"] == 0 and len(archive) == 2
    assert "nothing new" in spoken(again)


def test_a_newer_export_adds_only_what_is_new(tmp_path):
    archive = Archive(tmp_path / "a.db")
    ledger = tmp_path / "ledger.json"
    import_export(archive, ANDROID, "Rahul Verma", ledger=ledger)
    newer = ANDROID + "16/03/26, 11:00 am - Hrishikesh: Booked it\n"
    result = import_export(archive, newer, "Rahul Verma", ledger=ledger)
    assert result["new_days"] == 1
    latest = archive.search("Booked")
    assert latest and "Booked it" in latest[0].text and "Taj" not in latest[0].text
