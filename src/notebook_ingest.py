"""Single ingest path for notebook sources: viewer Document + scoped chunks.

Notebooks are bounded, sources-only source sets: uploading a file into a
notebook must produce both a readable `Document` row (so the Library/agent
can page through the full text) and RAG chunks tagged with
`document_id`/`notebook_id` (so `RAGManager.search(..., notebook_id=...)`
can scope retrieval to just this notebook). This module is the single place
that does both, atomically enough that a failure at any step leaves neither
an orphan Document nor a misleadingly "indexed" NotebookSource.
"""
import logging
import os
import tempfile
import uuid

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


def ingest_notebook_file(notebook_id, owner, filename, content_bytes,
                         rag_manager, db_session):
    """Ingest one uploaded file into a notebook: Document + notebook-scoped chunks.

    Returns a NotebookSource, always persisted (status "indexed" or
    "failed"). On any failure path (disallowed extension, extraction error,
    empty text, or every chunk failing to embed) no Document row is created
    — only the failed NotebookSource.
    """
    ext = os.path.splitext(filename)[1].lower()

    def _failed(msg):
        src = NotebookSource(id=str(uuid.uuid4()), notebook_id=notebook_id,
                             filename=filename, status="failed", error=msg)
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
    chunks = rag_manager.vector_rag._split_into_chunks(text)
    embedded = 0
    try:
        for i, chunk in enumerate(chunks):
            metadata = {"source": filename, "filename": filename, "type": ext,
                        "chunk_id": i, "owner": owner,
                        "document_id": doc_id, "notebook_id": notebook_id}
            if rag_manager.add_document(chunk, metadata):
                embedded += 1
    except Exception as exc:
        logger.warning("notebook ingest embedding failed for %s: %s", filename, exc)
        return _failed(f"embedding failed: {exc}")
    if embedded == 0:
        return _failed("embedding failed")

    doc = Document(id=doc_id, title=filename, owner=owner, current_content=text)
    db_session.add(doc)
    src = NotebookSource(id=str(uuid.uuid4()), notebook_id=notebook_id,
                         document_id=doc_id, filename=filename,
                         status="indexed", chunk_count=embedded)
    db_session.add(src)
    db_session.commit()
    return src
