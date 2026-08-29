"""Single ingest path for notebook sources: viewer Document + scoped chunks.

Notebooks are bounded, sources-only source sets: uploading a file into a
notebook must produce both a readable `Document` row (so the Library/agent
can page through the full text) and RAG chunks tagged with
`document_id`/`notebook_id` (so `RAGManager.search(..., notebook_id=...)`
can scope retrieval to just this notebook). This module is the single place
that does both.

DB-level guarantee: a failure at any step leaves neither an orphan `Document`
row nor a misleadingly "indexed" `NotebookSource` — the database is left
consistent. If `add_document` embeds chunks 0..k successfully and then raises
on chunk k+1, those already-embedded chunks are cleaned up best-effort via
`rag_manager.remove_notebook(notebook_id, document_id=doc_id)` before the
failure is recorded; a failure of that cleanup call itself is swallowed (it
must not mask the original embedding failure) and logged, leaving the
existing accepted gap only for that rarer double-failure case.
"""
import logging
import os
import re
import tempfile
import uuid
from typing import Optional

from core.database import Document, NotebookSource

logger = logging.getLogger(__name__)

_TEXT_EXTENSIONS = {".txt", ".md", ".py", ".json", ".yaml", ".yml", ".csv",
                    ".html", ".css", ".js"}
_OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".epub"}
ALLOWED_NOTEBOOK_EXTENSIONS = frozenset(_TEXT_EXTENSIONS | _OFFICE_EXTENSIONS | {".pdf"})


def _convert_office_to_text(filename, content_bytes):
    """Extract text from Office/EPUB bytes via markitdown.

    Mirrors the temp-file call pattern in src/document_processor.py /
    src/office_doc.py: markitdown and the pypdf/docx fallbacks all take a
    filesystem path, so the bytes are spooled to an ephemeral temp file
    (same pattern as src/document_processor.py's per-image temp files) and
    cleaned up in a finally block. The suffix is preserved so
    is_markitdown_format()/the native .docx fallback can inspect it.
    """
    from src.personal_docs import extract_office_text

    ext = os.path.splitext(filename)[1].lower()
    fd, tmp_path = tempfile.mkstemp(suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content_bytes)
        return extract_office_text(tmp_path) or ""
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def extract_pdf_text(path):
    """Thin wrapper around src.personal_docs.extract_pdf_text (patchable in tests)."""
    from src.personal_docs import extract_pdf_text as _extract_pdf_text
    return _extract_pdf_text(path)


def _extract_text(filename, content_bytes):
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        # reuse src.personal_docs.extract_pdf_text via a temp file, as
        # routes/personal_routes.py does
        fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content_bytes)
            return extract_pdf_text(tmp_path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    if ext in _OFFICE_EXTENSIONS:
        return _convert_office_to_text(filename, content_bytes)
    return content_bytes.decode("utf-8", errors="replace")


def _is_heading_like(line: str) -> bool:
    """Heuristic: does this line look like a heading/section title?

    A line is heading-like if it starts with ``#``, OR ends with ``:`` and is
    short, OR is predominantly ALL CAPS (≥60% uppercase letters) and short.
    All checks use a < 80 char length gate so body paragraphs are excluded.
    """
    line = line.strip()
    if not line or len(line) >= 80:
        return False
    if line.startswith("#"):
        return True
    if line.endswith(":"):
        return True
    letters = [c for c in line if c.isalpha()]
    if letters:
        upper = sum(1 for c in letters if c.isupper())
        if upper / len(letters) >= 0.6:
            return True
    return False


def _last_heading_before(text: str, offset: int) -> Optional[str]:
    """Return the stripped text of the last heading-like line before ``offset``.

    Leading ``#`` markers are stripped from the returned hint. Returns ``None``
    if no heading-like line is found in ``text[:offset]``.
    """
    scan = text[:offset] if offset > 0 else ""
    last_heading = None
    for line in scan.splitlines():
        if _is_heading_like(line):
            last_heading = line.strip().lstrip("#").strip()
    return last_heading


def _cleanup_orphan_chunks(rag_manager, notebook_id, doc_id):
    """Best-effort delete of chunks embedded before a mid-loop failure.

    Called when `add_document` raises after embedding chunks 0..k for this
    doc_id: those chunks are already in Chroma with no Document row for a
    search hit to resolve against. A cleanup failure here must not mask the
    original embedding failure, so it is caught and logged, not re-raised.
    """
    try:
        rag_manager.remove_notebook(notebook_id, document_id=doc_id)
    except Exception as exc:
        logger.warning("notebook ingest cleanup failed for notebook=%s doc=%s: %s",
                       notebook_id, doc_id, exc)


def ingest_notebook_file(notebook_id, owner, filename, content_bytes,
                         rag_manager, db_session, url=None):
    """Ingest one uploaded file into a notebook: Document + notebook-scoped chunks.

    Returns a NotebookSource, always persisted (status "indexed" or
    "failed"). On any failure path (disallowed extension, extraction error,
    empty text, or every chunk failing to embed) no Document row is created
    — only the failed NotebookSource.
    """
    ext = os.path.splitext(filename)[1].lower()

    def _failed(msg):
        src = NotebookSource(id=str(uuid.uuid4()), notebook_id=notebook_id,
                             filename=filename, status="failed", error=msg,
                             url=url)
        db_session.add(src)
        db_session.commit()
        return src

    if ext not in ALLOWED_NOTEBOOK_EXTENSIONS:
        return _failed(f"extension not allowed: {ext}")
    try:
        text = _extract_text(filename, content_bytes)
    except Exception as exc:
        logger.warning("notebook ingest parse failed for %s: %s", filename, exc)
        return _failed(f"parse failed: {exc}")
    if not text or not text.strip():
        return _failed("no extractable text")

    doc_id = str(uuid.uuid4())
    # get_rag_manager() hands routes the bare VectorRAG, while tests pass a
    # RAGManager-shaped wrapper — accept both.
    vector_rag = getattr(rag_manager, "vector_rag", rag_manager)
    chunks = vector_rag._split_into_chunks(text)
    embedded = 0
    # Track the cumulative character offset so we can derive a section
    # heading from the text preceding each chunk's start position.
    char_offset = 0
    try:
        for i, chunk in enumerate(chunks):
            paragraph_ref = f"¶{i + 1}"
            section_hint = _last_heading_before(text, char_offset) or filename
            metadata = {"source": filename, "filename": filename, "type": ext,
                        "chunk_id": i, "owner": owner,
                        "document_id": doc_id, "notebook_id": notebook_id,
                        "paragraph_ref": paragraph_ref,
                        "section_hint": section_hint}
            if rag_manager.add_document(chunk, metadata):
                embedded += 1
            char_offset += len(chunk)
    except Exception as exc:
        logger.warning("notebook ingest embedding failed for %s: %s", filename, exc)
        _cleanup_orphan_chunks(rag_manager, notebook_id, doc_id)
        return _failed(f"embedding failed: {exc}")
    if embedded == 0:
        return _failed("embedding failed")

    doc = Document(id=doc_id, title=filename, owner=owner, current_content=text)
    db_session.add(doc)
    src = NotebookSource(id=str(uuid.uuid4()), notebook_id=notebook_id,
                         document_id=doc_id, filename=filename,
                         status="indexed", chunk_count=embedded, url=url)
    db_session.add(src)
    db_session.commit()
    return src


# --------------------------------------------------------------------------
# Web sources (fase 4d)
# --------------------------------------------------------------------------

_FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9 _\-]+")


def _filename_for_page(title, url):
    """Derive a safe `<title>.md` filename for a fetched web page."""
    base = (title or "").strip()
    if not base:
        try:
            from urllib.parse import urlparse
            base = urlparse(url).netloc or "webpagina"
        except Exception:
            base = "webpagina"
    base = _FILENAME_UNSAFE_RE.sub(" ", base)
    base = re.sub(r"\s+", " ", base).strip()[:80] or "webpagina"
    return f"{base}.md"


def ingest_notebook_url(notebook_id, owner, url, rag_manager, db_session,
                        fetcher=None):
    """Fetch a web page and ingest it as a notebook source.

    The page is fetched through services.search.content.fetch_webpage_content
    (which carries its own private-address/SSRF guard and a short disk
    cache), converted to a small markdown document and pushed through the
    unchanged ingest_notebook_file path, so Document row, notebook-scoped
    Chroma chunks and the indexed/failed status lifecycle all behave exactly
    like a file upload. The page URL is persisted on the NotebookSource row
    (`url` column) as provenance.

    Returns a NotebookSource (indexed or failed) — a fetch failure becomes a
    failed source row, mirroring how a broken file upload is reported.
    `fetcher` is injectable for tests.
    """
    if fetcher is None:
        from services.search.content import fetch_webpage_content as fetcher

    def _failed(msg):
        src = NotebookSource(id=str(uuid.uuid4()), notebook_id=notebook_id,
                             filename=_filename_for_page(None, url),
                             status="failed", error=msg, url=url)
        db_session.add(src)
        db_session.commit()
        return src

    try:
        result = fetcher(url)
    except Exception as exc:
        logger.warning("notebook url fetch failed for %s: %s", url, exc)
        return _failed(f"fetch failed: {exc}")
    if not isinstance(result, dict) or not result.get("success"):
        reason = (result or {}).get("error") if isinstance(result, dict) else None
        return _failed(f"fetch failed: {reason or 'unknown error'}")
    content = (result.get("content") or "").strip()
    if not content:
        return _failed("no extractable text")

    title = (result.get("title") or "").strip()
    filename = _filename_for_page(title, url)
    markdown = f"# {title or filename[:-3]}\n\nBron: {url}\n\n{content}\n"
    return ingest_notebook_file(notebook_id, owner, filename,
                                markdown.encode("utf-8"), rag_manager,
                                db_session, url=url)
