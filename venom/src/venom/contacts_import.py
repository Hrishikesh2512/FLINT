"""Import a phone / Google address book into Venom's ConnectionStore.

Google Contacts (and iOS, and most phones) export either a vCard (.vcf) or a
CSV. Google killed contacts CardDAV / app-password access, and the live People
API needs full OAuth, which cuts against this project's zero-OAuth grain (see
gmail.py, gcal.py). So the cheap, durable path is a one-time file: export it,
drop it on the Pi, fold it into the connections Venom already uses for
"message <name>" and WhatsApp send-by-name.

Dependency-free on purpose: the handful of fields we want (name, nicknames,
phones, note) is easy to hand-parse, which keeps this off the vobject / pandas
treadmill the rest of the codebase avoids. Re-run any time to refresh; every
field merges through ConnectionStore.save, so a second import never duplicates
a person.
"""

from __future__ import annotations

import csv
import io
import logging
import quopri
from pathlib import Path

log = logging.getLogger("venom.contacts_import")


def _unfold(text: str) -> list[str]:
    """vCard line folding: a line starting with a space/tab continues the one
    before it, so long NOTE/N values wrap. Stitch them back together first."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _unescape(value: str) -> str:
    r"""vCard value escaping: \n -> newline, plus literal \, \; \\ ."""
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in ("n", "N") else nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _decode(value: str, params: str) -> str:
    """Undo quoted-printable (old exports) and vCard escaping."""
    if "quoted-printable" in params:
        try:
            value = quopri.decodestring(
                value.encode("utf-8")).decode("utf-8", "replace")
        except (ValueError, UnicodeError):
            pass
    return _unescape(value)


def parse_vcards(text: str) -> list[dict]:
    """A vCard (.vcf) blob -> [{name, nickname, phones, instagram, note}]."""
    people: list[dict] = []
    cur: dict | None = None
    for line in _unfold(text):
        head, sep, value = line.partition(":")
        if not sep:
            continue
        parts = head.split(";")
        name = parts[0].strip().upper()
        params = ";".join(parts[1:]).lower()
        if name == "BEGIN" and value.strip().upper() == "VCARD":
            cur = {"fn": "", "given": "", "family": "", "nickname": "",
                   "phones": [], "instagram": "", "note": ""}
            continue
        if name == "END":
            if cur is not None:
                display = cur["fn"] or " ".join(
                    p for p in (cur["given"], cur["family"]) if p).strip()
                if display or cur["phones"]:
                    people.append({"name": display, "nickname": cur["nickname"],
                                   "phones": cur["phones"],
                                   "instagram": cur["instagram"],
                                   "note": cur["note"]})
            cur = None
            continue
        if cur is None:
            continue
        val = _decode(value.strip(), params)
        if name == "FN":
            cur["fn"] = val
        elif name == "N":
            bits = val.split(";")
            cur["family"] = bits[0].strip() if bits else ""
            cur["given"] = bits[1].strip() if len(bits) > 1 else ""
        elif name == "NICKNAME" and not cur["nickname"]:
            cur["nickname"] = val.split(",")[0].strip()
        elif name == "TEL" and val.strip():
            cur["phones"].append(val.strip())
        elif name == "NOTE" and not cur["note"]:
            cur["note"] = val.strip()
        elif name == "X-SOCIALPROFILE" and "instagram" in params:
            cur["instagram"] = val.strip().rstrip("/").split("/")[-1]
    return people


def parse_google_csv(text: str) -> list[dict]:
    """A Google Contacts CSV export -> the same record shape. Handles both the
    old ('Name', 'Given Name') and new ('First Name', 'Last Name') headers, and
    the ' ::: '-joined multi-value cells Google writes."""
    people: list[dict] = []
    for row in csv.DictReader(io.StringIO(text)):
        low = {(k or "").strip().lower(): (v or "").strip()
               for k, v in row.items()}
        name = low.get("name") or " ".join(p for p in (
            low.get("first name") or low.get("given name", ""),
            low.get("middle name", ""),
            low.get("last name") or low.get("family name", "")) if p).strip()
        phones: list[str] = []
        for key, val in low.items():
            if key.startswith("phone") and key.endswith("value") and val:
                phones.extend(v.strip() for v in val.split(":::") if v.strip())
        rec = {"name": name, "nickname": low.get("nickname", ""),
               "phones": phones, "instagram": "", "note": low.get("notes", "")}
        if rec["name"] or rec["phones"]:
            people.append(rec)
    return people


def _clean_phones(raw: list[str]) -> list[str]:
    """Split ' ::: '-joined cells and drop duplicates by their digits, keeping
    the first spelling. ConnectionStore normalises to digits on save; this is
    just so the summary count is honest."""
    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        for part in str(value).split(":::"):
            part = part.strip()
            digits = "".join(ch for ch in part if ch.isdigit())
            if digits and digits not in seen:
                seen.add(digits)
                out.append(part)
    return out


def parse(text: str, suffix: str = "") -> list[dict]:
    """Pick the format: vCard wins whenever the content looks like one."""
    if "BEGIN:VCARD" in text.upper():
        return parse_vcards(text)
    if suffix.lower() == ".csv" or ("," in (text.splitlines() or [""])[0]):
        return parse_google_csv(text)
    return parse_vcards(text)


def import_file(store, path) -> str:
    """Read `path`, merge every contact into `store`, and report a summary."""
    p = Path(str(path)).expanduser()
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"can't read {p}: {exc}"
    people = parse(text, p.suffix)
    if not people:
        return (f"no contacts found in {p.name} "
                "(expected a Google vCard .vcf or a Contacts CSV export).")
    new = numbers = saved = 0
    for person in people:
        name = (person.get("name") or "").strip()
        phones = _clean_phones(person.get("phones", []))
        if not name and not phones:
            continue
        existed = store.find(name) is not None if name else False
        store.save(name or phones[0], phone=phones[0] if phones else "",
                   nickname=person.get("nickname", ""),
                   instagram=person.get("instagram", ""),
                   note=person.get("note", ""))
        for extra in phones[1:]:
            store.save(name or phones[0], phone=extra)
        numbers += len(phones)
        saved += 1
        new += 0 if existed else 1
    return (f"imported {saved} contacts from {p.name}: {new} new, "
            f"{saved - new} updated, {numbers} numbers folded in.")
