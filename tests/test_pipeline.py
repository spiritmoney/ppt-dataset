"""Pipeline tests."""

from pathlib import Path

from src.blocklist import BlocklistFilter
from src.prefilter import _content_type_ok, is_presentation_url_ext
from src.settings import Settings
from src.utils import (
    canonicalize_url,
    extract_file_links,
    file_type_from_url,
    is_presentation_url,
    make_batch_id,
    make_record_id,
)


def test_presentation_url_detection():
    assert is_presentation_url("https://example.com/deck.pptx")
    assert is_presentation_url("https://example.com/deck.PPT")
    assert not is_presentation_url("https://example.com/report.pdf")
    assert not is_presentation_url("https://example.com/page.html")


def test_file_type_from_url():
    assert file_type_from_url("https://x.com/a.pdf") == "pdf"
    assert file_type_from_url("https://x.com/a.pptx") == "pptx"


def test_batch_and_record_id():
    bid = make_batch_id(1)
    assert bid.startswith("BATCH-")
    rid = make_record_id(bid, 42)
    assert rid.startswith(bid)
    assert rid.endswith("00000042")


def test_extract_file_links():
    html = '<a href="/a.pdf">x</a><img src="/b.pptx"><a href="/c.ppt">y</a>'
    links = extract_file_links(html, "https://example.com")
    assert set(links) == {"https://example.com/b.pptx", "https://example.com/c.ppt"}


def test_blocklist_blocks_amazon():
    settings = Settings.load()
    root = settings.config_path.parent.parent
    bl = BlocklistFilter([root / "config" / "blocklists" / "fortune500.yaml"])
    blocked, reason = bl.check("https://aws.amazon.com/deck.pptx")
    assert blocked
    assert reason == "BLOCKLIST_F500"


def test_content_type_matching():
    assert _content_type_ok(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"
    )
    assert not _content_type_ok("application/pdf", "pptx")


def test_signature_detection():
    from src.prefilter import _detect_signature, _looks_like_html

    assert _detect_signature(b"PK\x03\x04" + b"x" * 20, "pptx") == "PK/ZIP"
    assert _detect_signature(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 20, "ppt") == "OLE/PPT"
    assert _detect_signature(b"<!DOCTYPE html>", "pptx") == ""
    assert _looks_like_html(b"<!DOCTYPE html><title>403 Forbidden</title>")


def test_extension_check():
    assert is_presentation_url_ext("https://x.com/file.pptx")
    assert not is_presentation_url_ext("https://x.com/file.doc")


def test_canonicalize_url_dedup_variants():
    a = canonicalize_url("https://www.example.com/Deck.PPTX?utm_source=google")
    b = canonicalize_url("http://example.com/deck.pptx")
    assert a == b


def test_insert_deduplicates(tmp_path):
    from src.database import Database

    db_path = tmp_path / "dedup.db"
    db = Database(f"sqlite:///{db_path}")
    db.init()

    rows = [
        {
            "url": "https://www.example.com/a.pptx",
            "source_url": "https://www.example.com/a.pptx",
            "domain": "example.com",
            "discovery_method": "test",
        },
        {
            "url": "http://example.com/a.pptx?utm_source=x",
            "source_url": "http://example.com/a.pptx?utm_source=x",
            "domain": "example.com",
            "discovery_method": "test",
        },
    ]
    inserted = db.insert_candidates(rows)
    assert inserted == 1
    assert db.total_url_count() == 1


def test_dedupe_existing_merges_variants(tmp_path):
    from src.database import Database

    db_path = tmp_path / "merge.db"
    db = Database(f"sqlite:///{db_path}")
    db.init()

    db.insert_candidates([
        {
            "url": "https://www.example.com/a.pptx",
            "source_url": "https://www.example.com/a.pptx",
            "domain": "example.com",
            "discovery_method": "test",
        },
    ])
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO candidates
            (url, source_url, domain, discovery_method, discovered_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "http://example.com/a.pptx",
                "http://example.com/a.pptx",
                "example.com",
                "test",
                "2026-01-01T00:00:00+00:00",
                "pending",
            ),
        )

    counts = db.dedupe_existing()
    assert counts["deleted"] == 1
    assert db.total_url_count() == 1
    with db.connect() as conn:
        row = conn.execute("SELECT url FROM candidates").fetchone()
    assert row[0] == canonicalize_url("http://example.com/a.pptx")
