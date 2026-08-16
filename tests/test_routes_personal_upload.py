"""Tests for the RAG-ingest bugs in routes/personal_routes.py:upload_files_to_rag.

Covers: markitdown routing for Office formats (not raw utf-8 decode of the
zip bytes), an extension allowlist that rejects unknown types with 400 before
writing to disk, and unified chunk sizing (default 1000/200, no 500 override).
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes import personal_routes


def _upload_endpoint():
    router = personal_routes.setup_personal_routes(_FakePersonalDocs(), None, True)
    for route in router.routes:
        if getattr(route, "path", "") == "/api/personal/upload" and "POST" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError("upload endpoint not found")


def _request(privileges):
    class _AuthManager:
        def get_privileges(self, user):
            return privileges

    return SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_manager=_AuthManager(),
            ),
        ),
        client=SimpleNamespace(host="203.0.113.10"),
    )


class _FakePersonalDocs:
    def __init__(self):
        self.added = []

    def add_directory(self, directory, index=False):
        self.added.append((directory, index))


class _FakeRAG:
    def __init__(self):
        self.docs = []
        self.chunk_calls = []

    def _split_into_chunks(self, text, *args, **kwargs):
        self.chunk_calls.append({"text": text, "args": args, "kwargs": kwargs})
        return [text]

    def add_document(self, chunk, metadata):
        self.docs.append((chunk, metadata))
        return True


class _Upload:
    def __init__(self, filename, content=b"hello"):
        self.filename = filename
        self._content = content

    async def read(self, limit):
        return self._content


def test_docx_upload_uses_markitdown_not_raw_decode(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(personal_routes, "UPLOADS_DIR", str(tmp_path))
    rag = _FakeRAG()
    monkeypatch.setattr(personal_routes, "get_rag_manager", lambda: rag)

    converted_text = "converted markdown content from docx"
    calls = []

    def fake_convert_to_markdown(path):
        calls.append(path)
        return converted_text

    monkeypatch.setattr(
        "src.markitdown_runtime.convert_to_markdown", fake_convert_to_markdown
    )

    endpoint = _upload_endpoint()
    # Fake .docx bytes that would be garbage if utf-8-decoded.
    fake_docx_bytes = b"PK\x03\x04not-real-utf8-\xff\xfe-zip-bytes"

    result = asyncio.run(
        endpoint(
            request=_request({"can_use_documents": True}),
            files=[_Upload("report.docx", fake_docx_bytes)],
        )
    )

    assert result["success"] is True
    assert result["indexed_count"] == 1
    assert len(calls) == 1  # markitdown conversion was invoked
    assert rag.docs[0][0] == converted_text


def test_upload_rejects_unknown_extension_with_400_before_writing(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(personal_routes, "UPLOADS_DIR", str(tmp_path))
    rag = _FakeRAG()
    monkeypatch.setattr(personal_routes, "get_rag_manager", lambda: rag)

    endpoint = _upload_endpoint()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            endpoint(
                request=_request({"can_use_documents": True}),
                files=[_Upload("malware.exe", b"whatever")],
            )
        )

    assert exc.value.status_code == 400
    # Nothing should have been indexed or written to disk.
    assert rag.docs == []
    written = list(Path(tmp_path).rglob("*.exe"))
    assert written == []


def test_upload_uses_default_chunk_size_not_500(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(personal_routes, "UPLOADS_DIR", str(tmp_path))
    rag = _FakeRAG()
    monkeypatch.setattr(personal_routes, "get_rag_manager", lambda: rag)

    endpoint = _upload_endpoint()
    result = asyncio.run(
        endpoint(
            request=_request({"can_use_documents": True}),
            files=[_Upload("notes.txt", b"hello from upload")],
        )
    )

    assert result["success"] is True
    assert len(rag.chunk_calls) == 1
    # No explicit chunk_size override — the call must fall through to
    # rag._split_into_chunks' own default (1000/200, same as
    # index_personal_documents in rag_vector.py), not the old 500.
    assert rag.chunk_calls[0]["args"] == ()
    assert rag.chunk_calls[0]["kwargs"] == {}
