"""Notebook "slide_deck" artifact: registry, JSON-extractie/validatie,
viewer-renderer, generatie-retry en de report-route-dispatch.

Route-test fixtures mirror tests/test_notebook_flashcards.py.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-slides")

import json
import uuid
from datetime import datetime

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import routes.notebook_routes as nbr
import src.notebook_artifacts as artifacts
from src.notebook_artifacts import ARTIFACT_KINDS
from src.notebook_slides import extract_slide_deck, generate_slide_deck
from src.notebook_report import ENGLISH_KIND_LABELS
from tests.helpers.sqlite_db import make_temp_sqlite

_DECK = {
    "title": "Studiesucces in het mbo",
    "slides": [
        {"title": "Waarom dit telt", "bullets": ["42% hogere slagingskans", "12 weken"], "notes": "Open warm."},
        {"title": "Drie panelen", "bullets": ["Bronnen", "Gesprek", "Studio"], "notes": ""},
    ],
}
_DECK_MD = "```json\n" + json.dumps(_DECK, ensure_ascii=False) + "\n```\n"


# ---- kind registration ------------------------------------------------

def test_slide_deck_kind_registered_with_dutch_label():
    assert "slide_deck" in ARTIFACT_KINDS
    assert ARTIFACT_KINDS["slide_deck"]["label"] == "Diapresentatie"
    assert "taal van de bronnen" in ARTIFACT_KINDS["slide_deck"]["prompt"]


def test_slide_deck_in_english_kind_labels():
    assert ENGLISH_KIND_LABELS["slide_deck"] == "Slide deck"


# ---- extractie/validatie ----------------------------------------------

def test_extract_valid_deck():
    deck = extract_slide_deck(_DECK_MD)
    assert deck["title"] == "Studiesucces in het mbo"
    assert [s["title"] for s in deck["slides"]] == ["Waarom dit telt", "Drie panelen"]
    assert deck["slides"][0]["bullets"][0] == "42% hogere slagingskans"


def test_extract_accepts_bare_json_without_fence():
    deck = extract_slide_deck(json.dumps(_DECK))
    assert len(deck["slides"]) == 2


def test_extract_rejects_prose():
    with pytest.raises(ValueError):
        extract_slide_deck("Dit is een verhaaltje zonder JSON.")


def test_extract_rejects_missing_fields():
    bad = {"title": "X", "slides": [{"bullets": ["a"]}]}
    with pytest.raises(ValueError, match="title"):
        extract_slide_deck("```json\n" + json.dumps(bad) + "\n```")


def test_extract_rejects_empty_slides():
    with pytest.raises(ValueError):
        extract_slide_deck('```json\n{"title": "X", "slides": []}\n```')


def test_extract_rejects_too_many_slides():
    big = {"title": "X", "slides": [{"title": f"S{i}", "bullets": []} for i in range(25)]}
    with pytest.raises(ValueError, match="te veel"):
        extract_slide_deck("```json\n" + json.dumps(big) + "\n```")


# ---- renderer ---------------------------------------------------------

def test_renderer_renders_slides_and_nav():
    out = generate_slide_deck(None, _DECK_MD, "NB", datetime(2026, 8, 22))
    assert "Studiesucces in het mbo" in out
    assert out.count("sd-slide") >= 2
    assert "sd-next" in out and "sd-prev" in out
    assert "Drie panelen" in out


def test_renderer_escapes_html():
    deck = {"title": "<script>x</script>", "slides": [{"title": "V", "bullets": ["<b>a</b>"], "notes": ""}]}
    out = generate_slide_deck(None, "```json\n" + json.dumps(deck) + "\n```", "NB", datetime(2026, 8, 22))
    assert "<script>x</script>" not in out
    assert "&lt;b&gt;" in out


def test_renderer_degrades_on_malformed_content():
    out = generate_slide_deck("Eigen titel", "kapotte inhoud", "NB", datetime(2026, 8, 22))
    assert "Kon de slides niet lezen" in out
    assert "Eigen titel" in out


def test_renderer_no_external_resources():
    out = generate_slide_deck(None, _DECK_MD, "NB", datetime(2026, 8, 22))
    assert "http://" not in out and "https://" not in out
    assert "<link" not in out


# ---- generatie-retry (validator-seam in generate_artifact) -------------

@pytest.mark.asyncio
async def test_generate_artifact_retries_slide_deck_until_valid(monkeypatch, ts):
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return "gewoon proza, geen json"
        return _DECK_MD

    monkeypatch.setattr(artifacts, "task_llm_call_async", fake_llm)
    s = ts()
    try:
        nb = db.Notebook(id=str(uuid.uuid4()), name="NB", owner="ed")
        s.add(nb)
        doc_id = str(uuid.uuid4())
        s.add(db.Document(id=doc_id, title="bron", owner="ed", language="markdown",
                          current_content="Inhoud over studiesucces.", session_id=None))
        s.add(db.NotebookSource(id=str(uuid.uuid4()), notebook_id=nb.id, document_id=doc_id,
                                filename="bron.md", status="indexed", chunk_count=1))
        s.commit()
        art = await artifacts.generate_artifact(nb.id, "ed", "slide_deck", s)
        assert art.kind == "slide_deck"
        assert len(calls) == 2
        # De correctie-retry voedt de fout en het foute antwoord terug.
        last_msgs = calls[1]
        joined = "\n".join(m["content"] for m in last_msgs)
        assert "gewoon proza" in joined
    finally:
        s.close()


@pytest.mark.asyncio
async def test_generate_artifact_slide_deck_fails_after_max_attempts(monkeypatch, ts):
    async def fake_llm(messages, **kwargs):
        return "nooit json"

    monkeypatch.setattr(artifacts, "task_llm_call_async", fake_llm)
    s = ts()
    try:
        nb = db.Notebook(id=str(uuid.uuid4()), name="NB", owner="ed")
        s.add(nb)
        doc_id = str(uuid.uuid4())
        s.add(db.Document(id=doc_id, title="bron", owner="ed", language="markdown",
                          current_content="Inhoud.", session_id=None))
        s.add(db.NotebookSource(id=str(uuid.uuid4()), notebook_id=nb.id, document_id=doc_id,
                                filename="bron.md", status="indexed", chunk_count=1))
        s.commit()
        with pytest.raises(RuntimeError):
            await artifacts.generate_artifact(nb.id, "ed", "slide_deck", s)
        # Geen half artifact achtergelaten.
        assert s.query(db.NotebookArtifact).count() == 0
    finally:
        s.close()


# ---- route ------------------------------------------------------------

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


def test_route_slide_deck_uses_viewer(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = c.post("/api/notebooks", json={"name": "NB"}).json()["id"]
    s = ts()
    try:
        document_id = str(uuid.uuid4())
        s.add(db.Document(id=document_id, title="NB — Diapresentatie", owner="ed",
                          language="markdown", current_content=_DECK_MD, session_id=None))
        art = db.NotebookArtifact(id=str(uuid.uuid4()), notebook_id=nb_id,
                                  document_id=document_id, kind="slide_deck")
        s.add(art)
        s.commit()
        art_id = art.id
    finally:
        s.close()

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert "sd-slide" in r.text
    assert "Waarom dit telt" in r.text
