"""Import from a Google vCard / CSV into the ConnectionStore."""

from __future__ import annotations

from venom.contacts_import import import_file, parse, parse_google_csv, parse_vcards
from venom.stores import ConnectionStore

VCARD = """BEGIN:VCARD
VERSION:3.0
FN:Rahul Sharma
N:Sharma;Rahul;;;
NICKNAME:Rahul Bhai
TEL;TYPE=CELL:+91 98123 45678
TEL;TYPE=HOME:011-2233-4455
NOTE:met at college; likes cricket
X-SOCIALPROFILE;TYPE=instagram:https://www.instagram.com/rahul.s/
END:VCARD
BEGIN:VCARD
VERSION:3.0
N:;Priya;;;
TEL:+91 90000 11111
END:VCARD
"""


def test_parse_vcards_pulls_the_fields():
    people = parse_vcards(VCARD)
    assert len(people) == 2
    rahul = people[0]
    assert rahul["name"] == "Rahul Sharma"
    assert rahul["nickname"] == "Rahul Bhai"
    assert rahul["phones"] == ["+91 98123 45678", "011-2233-4455"]
    assert rahul["instagram"] == "rahul.s"
    assert "college" in rahul["note"]
    # No FN: falls back to the structured N (given + family), here just given.
    assert people[1]["name"] == "Priya"


def test_line_folding_is_stitched():
    # Real 75-octet folding splits mid-token and inserts a structural space
    # that unfolding must drop, so content is preserved either way.
    folded = ("BEGIN:VCARD\nFN:Long Name\nNOTE:this is a ver\n"
              " y long note that wrapped\nEND:VCARD\n")
    note = parse_vcards(folded)[0]["note"]
    assert note == "this is a very long note that wrapped"


def test_quoted_printable_note_decodes():
    qp = ("BEGIN:VCARD\nFN:QP Guy\n"
          "NOTE;ENCODING=QUOTED-PRINTABLE:caf=C3=A9\nEND:VCARD\n")
    assert parse_vcards(qp)[0]["note"] == "café"


def test_google_csv_new_and_old_headers():
    csv_new = ("First Name,Last Name,Nickname,Notes,Phone 1 - Value\n"
               "Aмit,Verma,Amu,friend,+91 99999 88888\n")
    people = parse_google_csv(csv_new)
    assert people[0]["name"] == "Aмit Verma"
    assert people[0]["phones"] == ["+91 99999 88888"]
    assert people[0]["nickname"] == "Amu"


def test_csv_multi_value_phone_cell_splits():
    csv_text = "Name,Phone 1 - Value\nTwo Phones,+911111 ::: +922222\n"
    assert parse_google_csv(csv_text)[0]["phones"] == ["+911111", "+922222"]


def test_parse_prefers_vcard_when_content_looks_like_one():
    # Even with a .csv suffix, real vCard content is treated as vCard.
    assert len(parse(VCARD, ".csv")) == 2


def test_import_file_merges_and_reports(tmp_path):
    vcf = tmp_path / "contacts.vcf"
    vcf.write_text(VCARD, encoding="utf-8")
    store = ConnectionStore(tmp_path / "connections.json")

    summary = import_file(store, vcf)
    assert "2 contacts" in summary
    assert "2 new" in summary

    rec = store.find("Rahul Sharma")
    assert rec is not None
    assert "919812345678" in rec["phones"]      # normalised to digits
    assert "01122334455" in rec["phones"]        # second number folded in
    assert "rahul bhai" in [a.lower() for a in rec["aliases"]]
    assert store.phone_for("Rahul Bhai") == "919812345678"

    # Re-running is idempotent: same person, updated not duplicated.
    again = import_file(store, vcf)
    assert "0 new" in again
    assert len(store.find("Rahul Sharma")["phones"]) == 2


def test_missing_file_is_a_sentence(tmp_path):
    store = ConnectionStore(tmp_path / "c.json")
    assert "can't read" in import_file(store, tmp_path / "nope.vcf")
