"""Notebook artifact visual-report adapter + route.

Route tests mirror tests/test_notebook_suggest.py / test_routes_notebook_
artifacts.py (file-backed temp sqlite via make_temp_sqlite, monkeypatched
nbr.SessionLocal / nbr.get_current_user, real Document + NotebookArtifact
rows written directly so owner-scoping and the kind guard are exercised
against real data, not a fake).
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-report")

import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from starlette.testclient import TestClient

import core.database as db
import routes.notebook_routes as nbr
from src.notebook_report import ENGLISH_KIND_LABELS, generate_notebook_artifact_report
from tests.helpers.sqlite_db import make_temp_sqlite


# ---- adapter unit test (pure, no DB/HTTP) ----

def test_adapter_embeds_markdown_content():
    html_out = generate_notebook_artifact_report(
        notebook_name="Course Notes",
        kind="faq",
        document_title="Course Notes — FAQ",
        document_content="# Frequently Asked Questions\n\nWhat is X? X is Y.",
    )
    assert "Frequently Asked Questions" in html_out
    assert "What is X? X is Y." in html_out


def test_adapter_falls_back_to_kind_label_without_title():
    html_out = generate_notebook_artifact_report(
        notebook_name="Course Notes", kind="quiz",
        document_title=None, document_content="Q1. What is X?\n\nQ2. What is Y?",
    )
    assert ENGLISH_KIND_LABELS["quiz"] in html_out


def test_english_kind_labels_complete():
    assert ENGLISH_KIND_LABELS == {
        "study_guide": "Study guide",
        "briefing": "Briefing",
        "faq": "FAQ",
        "quiz": "Quiz",
        "mindmap": "Mindmap",
        "infographic": "Infographic",
    }


# ---- route ----

class _FakeRagManager:
    def __init__(self):
        self.vector_rag = self

    def add_document(self, text, metadata):
        return True

    def remove_notebook(self, notebook_id, document_id=None):
        pass

    def _split_into_chunks(self, text):
        return [text]


@pytest.fixture()
def ts(monkeypatch):
    test_session_local, engine, tmpfile = make_temp_sqlite(db.Base.metadata)
    monkeypatch.setattr(nbr, "SessionLocal", test_session_local)
    yield test_session_local
    tmpfile.close()


def _client(monkeypatch, user="ed"):
    monkeypatch.setattr(nbr, "get_current_user", lambda request: user)
    app = FastAPI()
    app.include_router(nbr.setup_notebook_routes(rag_manager=_FakeRagManager()))
    return TestClient(app, raise_server_exceptions=False)


def _make_notebook(c, name="NB"):
    return c.post("/api/notebooks", json={"name": name}).json()["id"]


def _make_artifact(ts, notebook_id, kind="faq", title=None,
                    content="Q1. What is X?\n\nA1. X is Y.", audio_path=None):
    """Write a real Document + NotebookArtifact row directly, bypassing
    generate_artifact (no LLM in these tests)."""
    s = ts()
    try:
        document_id = str(uuid.uuid4())
        s.add(db.Document(
            id=document_id,
            title=title if title is not None else f"NB — {kind}",
            owner="ed", language="markdown", current_content=content, session_id=None,
        ))
        artifact = db.NotebookArtifact(
            id=str(uuid.uuid4()), notebook_id=notebook_id, document_id=document_id,
            kind=kind, audio_path=audio_path,
        )
        s.add(artifact)
        s.commit()
        s.refresh(artifact)
        return artifact.id
    finally:
        s.close()


def test_report_200_contains_artifact_title(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    # No markdown heading in the content: generate_visual_report's title
    # extraction prefers the markdown's own heading over the passed-in
    # title, so it only falls back to the artifact's Document.title when
    # the content has none — this content is written that way on purpose.
    art_id = _make_artifact(
        ts, nb_id, kind="faq",
        title="A Distinctive Artifact Title",
        content="No heading here, just a question and an answer.",
    )

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "A Distinctive Artifact Title" in r.text


def test_report_uses_artifact_title_over_document_title(monkeypatch, ts):
    """NotebookArtifact.title, once set (e.g. via rename), must win over the
    linked Document's title as the report's title fallback — the
    document_title kwarg passed to generate_notebook_artifact_report is the
    *effective* title, not always Document.title. See requirement 5/2 of the
    title-column task."""
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    art_id = _make_artifact(
        ts, nb_id, kind="faq",
        title="Document Title (should lose)",
        content="No heading here, just a question and an answer.",
    )
    s = ts()
    try:
        row = s.get(db.NotebookArtifact, art_id)
        row.title = "Artifact Title (should win)"
        s.commit()
    finally:
        s.close()

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert "Artifact Title (should win)" in r.text
    assert "Document Title (should lose)" not in r.text


def test_report_embeds_document_content(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    art_id = _make_artifact(
        ts, nb_id, kind="study_guide",
        content="# Study Guide\n\n## Key concept\n\nThe key concept is Z.",
    )

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert "Key concept" in r.text
    assert "The key concept is Z." in r.text


def test_report_unknown_notebook_404(monkeypatch, ts):
    c = _client(monkeypatch)
    r = c.get("/api/notebooks/nope/artifacts/also-nope/report")
    assert r.status_code == 404


def test_report_foreign_notebook_404(monkeypatch, ts):
    c_ed = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c_ed)
    art_id = _make_artifact(ts, nb_id, kind="faq")

    c_eve = _client(monkeypatch, user="eve")
    r = c_eve.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 404


def test_report_unknown_artifact_404(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/does-not-exist/report")
    assert r.status_code == 404


def test_report_podcast_kind_404(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    art_id = _make_artifact(ts, nb_id, kind="podcast",
                             content="", audio_path="somefile.wav")

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 404


def test_report_mindmap_kind_allowed(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    art_id = _make_artifact(
        ts, nb_id, kind="mindmap",
        content="```mermaid\nmindmap\n  root((Topic))\n    Branch\n```\nA mindmap of the sources.",
    )

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert "mindmap" in r.text


def test_report_artifact_belongs_to_other_notebook_404(monkeypatch, ts):
    c = _client(monkeypatch)
    nb1_id = _make_notebook(c, name="NB1")
    nb2_id = _make_notebook(c, name="NB2")
    art_id = _make_artifact(ts, nb1_id, kind="faq")

    r = c.get(f"/api/notebooks/{nb2_id}/artifacts/{art_id}/report")
    assert r.status_code == 404


def test_report_document_missing_is_404_not_500(monkeypatch, ts):
    """Mirrors test_list_artifacts_title_none_safe_when_document_missing:
    a data inconsistency (Document hard-deleted, artifact row left behind)
    must 404, not 500."""
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    art_id = _make_artifact(ts, nb_id, kind="faq")

    s = ts()
    try:
        # Same trick as test_list_artifacts_title_none_safe_when_document_
        # missing: document_id is ondelete=CASCADE and SQLite FK enforcement
        # is on, so deleting the Document through normal app code always
        # takes the artifact row with it too. Turn enforcement off for this
        # one connection to produce the orphaned-artifact state directly.
        s.execute(text("PRAGMA foreign_keys=OFF"))
        doc = s.get(db.Document, s.get(db.NotebookArtifact, art_id).document_id)
        s.delete(doc)
        s.commit()
    finally:
        s.close()

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 404
