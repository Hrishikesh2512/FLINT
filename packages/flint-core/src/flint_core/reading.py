"""Reading a source, rather than reading *about* it.

Venom's research already searches the web, but a grounded search returns a
model's summary of pages it saw. That is fine for "what's the rupee at" and
poor for "read this RFC and tell me whether it applies to us": the specific
paragraph that answers the question is exactly what a summary drops.

So: fetch the thing, get its text, hand that to the model. Three formats cover
essentially everything worth reading — HTML, PDF, and plain text.

Both parsers are optional imports. `beautifulsoup4` is already a dependency
here; `pypdf` may not be installed, and on a 2 GB Pi it may deliberately never
be. A missing parser degrades to a clear message about what could not be read,
which is a usable answer; a hard import error at module load would take the
whole research feature down for want of a library used by one branch.

Nothing here follows links, logs in, or submits anything. It is a reader.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("flint.reading")

DEFAULT_TIMEOUT = 20.0

#: Refuse anything larger before downloading it. A 200 MB PDF on a wearable
#: is a mistake however interesting it is.
MAX_BYTES = 12_000_000

#: Extracted text kept. Past this the middle goes; the start and end of a
#: document carry most of what identifies it.
MAX_TEXT = 60_000

#: Tags whose text is never content.
_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form",
               "noscript", "iframe", "svg")

_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class Document:
    url: str
    title: str
    text: str
    kind: str = "html"
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.text.strip())

    def summary_line(self) -> str:
        if self.error:
            return f"{self.url}: {self.error}"
        words = len(self.text.split())
        return f"{self.title or self.url} ({self.kind}, ~{words} words)"


def _tidy(text: str) -> str:
    text = _WHITESPACE.sub(" ", text or "")
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_LINES.sub("\n\n", text).strip()
    if len(text) <= MAX_TEXT:
        return text
    half = MAX_TEXT // 2
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n\n… [{dropped} characters omitted] …\n\n{text[-half:]}"


def extract_html(raw: bytes | str, url: str = "") -> Document:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return Document(url=url, title="", text="", kind="html",
                        error="I can't read web pages — beautifulsoup4 isn't "
                              "installed here")
    markup = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    soup = BeautifulSoup(markup, "html.parser")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    title = (soup.title.get_text(strip=True) if soup.title else "") or ""
    # `main`/`article` when the page marks it up; otherwise the body. Beats a
    # readability heuristic for the sites worth reading and never does worse.
    body = soup.find("main") or soup.find("article") or soup.body or soup
    return Document(url=url, title=title, text=_tidy(body.get_text("\n")),
                    kind="html")


def extract_pdf(raw: bytes, url: str = "") -> Document:
    try:
        from pypdf import PdfReader
    except ImportError:
        return Document(url=url, title="", text="", kind="pdf",
                        error="I can't read PDFs on this device — pypdf isn't "
                              "installed")
    import io

    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:                # noqa: BLE001 — any malformed PDF
        return Document(url=url, title="", text="", kind="pdf",
                        error=f"that PDF wouldn't open: {exc}")
    title = ""
    try:
        title = str((reader.metadata or {}).get("/Title", "") or "")
    except Exception:                       # noqa: BLE001
        pass
    return Document(url=url, title=title, text=_tidy("\n\n".join(pages)),
                    kind="pdf")


def fetch(url: str, timeout: float = DEFAULT_TIMEOUT, get=None) -> Document:
    """Download one URL and pull the readable text out of it."""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return Document(url=url, title="", text="",
                        error="that isn't a web address I can open")
    if get is None:
        import requests

        get = requests.get

    try:
        response = get(url, timeout=timeout,
                       headers={"User-Agent": "flint-reader/1.0"},
                       stream=True)
    except Exception as exc:                # noqa: BLE001 — any network failure
        return Document(url=url, title="", text="",
                        error=f"couldn't reach it ({type(exc).__name__})")

    status = getattr(response, "status_code", 200)
    if status != 200:
        return Document(url=url, title="", text="",
                        error=f"the site returned {status}")

    declared = int(getattr(response, "headers", {}).get("Content-Length") or 0)
    if declared > MAX_BYTES:
        return Document(url=url, title="", text="",
                        error=f"that's {declared // 1_000_000} MB — too big to read here")

    raw = getattr(response, "content", b"") or b""
    if len(raw) > MAX_BYTES:
        raw = raw[:MAX_BYTES]

    content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        return extract_pdf(raw, url)
    if "html" in content_type or "xml" in content_type or not content_type:
        return extract_html(raw, url)
    if content_type.startswith("text/"):
        return Document(url=url, title="", kind="text",
                        text=_tidy(raw.decode("utf-8", "replace")))
    return Document(url=url, title="", text="", kind=content_type or "unknown",
                    error=f"I can't read {content_type or 'that'} files")


URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')\]]+")


def urls_in(text: str) -> list[str]:
    """Web addresses mentioned in a goal, deduped, in the order they appear."""
    seen: list[str] = []
    for found in URL_IN_TEXT.findall(text or ""):
        cleaned = found.rstrip(".,;:")
        if cleaned not in seen:
            seen.append(cleaned)
    return seen
