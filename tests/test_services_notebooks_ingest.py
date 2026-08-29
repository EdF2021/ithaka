"""Notebook ingest: one path -> Document row + notebook-scoped chunks.

Uses the suite's documented convention for a real, isolated DB
(tests.helpers.sqlite_db.make_temp_sqlite, see tests/TESTING_STANDARD.md and
tests/README.md) rather than the shared core.database.engine/SessionLocal —
committing real, un-torn-down rows there would leak into other tests.
"""
import uuid

import pytest

import core.database as db
from tests.helpers.sqlite_db import make_temp_sqlite
from src import notebook_ingest

_TS, _ENGINE, _TMPDB = make_temp_sqlite(db.Base.metadata)


class _FakeRag:
    """Stands in for RAGManager: exposes add_document + vector_rag._split_into_chunks."""

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []  # (text, metadata)
        self.vector_rag = self

    def add_document(self, text, metadata):
        self.calls.append((text, metadata))
        return self.ok

    def _split_into_chunks(self, text, chunk_size=1000, overlap=200):
        if not text:
            return []
        step = max(chunk_size - overlap, 1)
        return [text[i:i + chunk_size] for i in range(0, len(text), step)] or [text]


@pytest.fixture()
def dbs():
    s = _TS()
    nb_id = str(uuid.uuid4())
    try:
        nb = db.Notebook(id=nb_id, owner="ed", name="Thesis")
        s.add(nb)
        s.commit()
        yield s, nb_id
    finally:
        s.close()


def test_txt_ingest_creates_chunks_and_document(dbs):
    s, nb_id = dbs
    rag = _FakeRag()
    src = notebook_ingest.ingest_notebook_file(
        nb_id, "ed", "notes.txt", b"hello world " * 200, rag, s)
    assert src.status == "indexed"
    assert src.chunk_count == len(rag.calls) and rag.calls
    meta = rag.calls[0][1]
    assert meta["notebook_id"] == nb_id and meta["owner"] == "ed"
    assert meta["document_id"] == src.document_id
    doc = s.get(db.Document, src.document_id)
    assert doc is not None and doc.owner == "ed" and doc.title == "notes.txt"


def test_multiline_body_ingests_and_sets_section_hints(dbs):
    """Regression: a document with short non-heading body lines must ingest.

    The heading heuristic (`_is_heading_like`) is only reached for stripped
    lines under 80 chars, so a single-long-line fixture never exercises it.
    A `c.is_upper()` typo (should be `isupper()`) crashed on every short body
    line and surfaced as a misleading "embedding failed" status; this fixture
    drives real short lines — lowercase body, an ALL-CAPS heading, a `#`
    heading and a trailing-`:` heading — across more than one chunk and
    asserts the section-hint metadata lands on a later chunk.
    """
    s, nb_id = dbs
    rag = _FakeRag()
    body = (
        "# Introductie\n"
        "de kat zat op de mat\n"
        "nog wat gewone tekst hier\n"
        "HOOFDSTUK TWEE\n"
        + ("een korte regel body tekst\n" * 60)
        + "Samenvatting:\n"
        "de laatste alinea met inhoud\n"
    )
    src = notebook_ingest.ingest_notebook_file(
        nb_id, "ed", "hoofdstukken.txt", body.encode("utf-8"), rag, s)
    assert src.status == "indexed"
    assert src.chunk_count == len(rag.calls) and len(rag.calls) > 1
    # A chunk beyond the first should carry a section_hint resolved from the
    # last heading-like line before it (proves the heuristic ran, not crashed).
    hints = [m.get("section_hint") for _, m in rag.calls]
    assert any(h for h in hints[1:]), f"no section_hint on later chunks: {hints}"


def test_docx_goes_through_markitdown(dbs, monkeypatch):
    s, nb_id = dbs
    monkeypatch.setattr(notebook_ingest, "_convert_office_to_text",
                        lambda filename, content: "converted text " * 50)
    rag = _FakeRag()
    src = notebook_ingest.ingest_notebook_file(
        nb_id, "ed", "report.docx", b"PK\x03\x04zipbytes", rag, s)
    assert src.status == "indexed"
    assert "converted text" in rag.calls[0][0]
    assert "�" not in rag.calls[0][0]  # no replacement-char soup


def test_disallowed_extension_fails_cleanly(dbs):
    s, nb_id = dbs
    before = s.query(db.Document).count()
    rag = _FakeRag()
    src = notebook_ingest.ingest_notebook_file(
        nb_id, "ed", "malware.exe", b"MZ", rag, s)
    assert src.status == "failed" and src.document_id is None
    assert rag.calls == []
    assert s.query(db.Document).count() == before


def test_embed_failure_leaves_no_orphan_document(dbs):
    s, nb_id = dbs
    before = s.query(db.Document).count()
    rag = _FakeRag(ok=False)
    src = notebook_ingest.ingest_notebook_file(
        nb_id, "ed", "notes.txt", b"some text here " * 100, rag, s)
    assert src.status == "failed" and src.document_id is None
    assert s.query(db.Document).count() == before


def test_embed_partial_failure_cleans_up_orphan_chunks(dbs):
    """add_document succeeds once then raises: the already-embedded chunk
    must be cleaned up best-effort via rag_manager.remove_notebook, and the
    ingest must still end in a clean "failed" NotebookSource with no
    Document row (the carried-in ruling from Task 3's review)."""
    s, nb_id = dbs
    before = s.query(db.Document).count()

    class _FlakyRag(_FakeRag):
        def __init__(self):
            super().__init__()
            self.removed = []
            self.calls_made = 0

        def add_document(self, text, metadata):
            self.calls_made += 1
            if self.calls_made == 1:
                self.calls.append((text, metadata))
                return True
            raise RuntimeError("embedding service unavailable")

        def remove_notebook(self, notebook_id, document_id=None):
            self.removed.append((notebook_id, document_id))

    rag = _FlakyRag()
    src = notebook_ingest.ingest_notebook_file(
        nb_id, "ed", "notes.txt", b"some text here " * 200, rag, s)

    assert src.status == "failed" and src.document_id is None
    assert s.query(db.Document).count() == before
    assert len(rag.removed) == 1
    removed_notebook_id, removed_doc_id = rag.removed[0]
    assert removed_notebook_id == nb_id
    assert removed_doc_id is not None


def test_empty_text_fails_cleanly(dbs):
    s, nb_id = dbs
    rag = _FakeRag()
    src = notebook_ingest.ingest_notebook_file(
        nb_id, "ed", "empty.txt", b"   \n\t  ", rag, s)
    assert src.status == "failed" and src.document_id is None
    assert "no extractable text" in (src.error or "")


def test_pdf_goes_through_pypdf(dbs, monkeypatch):
    s, nb_id = dbs
    monkeypatch.setattr(notebook_ingest, "extract_pdf_text",
                        lambda path: "pdf extracted text " * 50)
    rag = _FakeRag()
    src = notebook_ingest.ingest_notebook_file(
        nb_id, "ed", "doc.pdf", b"%PDF-1.4 fake bytes", rag, s)
    assert src.status == "indexed"
    assert "pdf extracted text" in rag.calls[0][0]
