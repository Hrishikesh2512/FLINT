"""Reading real sources — and degrading clearly when it can't."""

from __future__ import annotations

from flint_core.reading import (
    MAX_BYTES,
    Document,
    extract_html,
    fetch,
    urls_in,
)


class Response:
    def __init__(self, content=b"", status_code=200, content_type="text/html",
                 length=None):
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        if length is not None:
            self.headers["Content-Length"] = str(length)


def getter(response):
    def get(url, **kw):
        return response

    return get


PAGE = b"""<html><head><title>The Real Title</title></head>
<body>
  <nav>Home About Contact</nav>
  <script>tracking('everything')</script>
  <main><p>The paragraph that actually answers the question.</p></main>
  <footer>Copyright nobody</footer>
</body></html>"""


# ── extracting what matters ─────────────────────────────────────────────────
def test_the_content_survives_and_the_furniture_does_not():
    doc = extract_html(PAGE, "https://example.com")
    assert "actually answers the question" in doc.text
    for junk in ("tracking", "Home About Contact", "Copyright nobody"):
        assert junk not in doc.text


def test_the_title_is_kept():
    assert extract_html(PAGE).title == "The Real Title"


def test_a_page_with_no_main_falls_back_to_the_body():
    doc = extract_html(b"<html><body><p>Just a body.</p></body></html>")
    assert "Just a body." in doc.text


def test_whitespace_is_tidied():
    doc = extract_html(b"<html><body><p>a</p>\n\n\n\n\n<p>b</p></body></html>")
    assert "\n\n\n" not in doc.text


def test_very_long_text_is_truncated_in_the_middle():
    from flint_core.reading import MAX_TEXT

    doc = extract_html(f"<html><body>{'word ' * 100000}</body></html>".encode())
    assert len(doc.text) < MAX_TEXT + 200
    assert "characters omitted" in doc.text


# ── fetching ────────────────────────────────────────────────────────────────
def test_a_page_is_fetched_and_read():
    doc = fetch("https://example.com/x", get=getter(Response(PAGE)))
    assert doc.ok is True
    assert "actually answers the question" in doc.text


def test_a_plain_text_file_is_read_as_is():
    doc = fetch("https://example.com/a.txt",
                get=getter(Response(b"line one\nline two", content_type="text/plain")))
    assert doc.kind == "text" and "line two" in doc.text


def test_an_http_error_is_reported_plainly():
    doc = fetch("https://example.com/gone", get=getter(Response(status_code=404)))
    assert doc.ok is False and "returned 404" in doc.error


def test_an_unreachable_site_is_reported_not_raised():
    def explode(url, **kw):
        raise ConnectionError("no route to host")

    doc = fetch("https://example.com", get=explode)
    assert doc.ok is False and "couldn't reach it" in doc.error


def test_something_that_is_not_a_url_is_refused():
    doc = fetch("just some words", get=getter(Response(PAGE)))
    assert doc.ok is False and "isn't a web address" in doc.error


def test_a_file_url_is_refused():
    """A reader that opens file:// reads the device's own secrets."""
    assert fetch("file:///etc/passwd", get=getter(Response())).ok is False


def test_something_enormous_is_refused_before_downloading():
    doc = fetch("https://example.com/huge.pdf",
                get=getter(Response(length=MAX_BYTES * 20, content_type="application/pdf")))
    assert doc.ok is False and "too big" in doc.error


def test_an_unreadable_content_type_says_which():
    doc = fetch("https://example.com/x.zip",
                get=getter(Response(b"PK", content_type="application/zip")))
    assert doc.ok is False and "application/zip" in doc.error


def test_a_pdf_without_the_library_degrades_to_a_clear_message(monkeypatch):
    """A missing optional parser must not take the whole feature down."""
    import builtins

    real_import = builtins.__import__

    def no_pypdf(name, *args, **kw):
        if name == "pypdf":
            raise ImportError("no module named pypdf")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pypdf)
    doc = fetch("https://example.com/paper.pdf",
                get=getter(Response(b"%PDF-1.4", content_type="application/pdf")))
    assert doc.ok is False
    assert "pypdf isn't installed" in doc.error


def test_a_malformed_pdf_is_reported_not_raised():
    doc = fetch("https://example.com/broken.pdf",
                get=getter(Response(b"not really a pdf",
                                    content_type="application/pdf")))
    assert doc.ok is False


# ── finding sources in a request ────────────────────────────────────────────
def test_urls_are_pulled_out_of_a_spoken_goal():
    found = urls_in("read https://example.com/rfc and https://other.org/x.pdf please")
    assert found == ["https://example.com/rfc", "https://other.org/x.pdf"]


def test_trailing_punctuation_is_not_part_of_the_url():
    assert urls_in("see https://example.com/page.") == ["https://example.com/page"]


def test_duplicates_are_collapsed():
    assert urls_in("https://a.com and https://a.com") == ["https://a.com"]


def test_a_goal_with_no_urls():
    assert urls_in("what is the rupee doing") == []


def test_a_document_describes_itself():
    doc = Document(url="https://x.com", title="A Paper", text="one two three")
    assert doc.summary_line() == "A Paper (html, ~3 words)"
