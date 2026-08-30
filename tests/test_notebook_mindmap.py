"""Mindmap-artifactvalidatie (validator-seam in generate_artifact).

Twee productie-artifacts van 2026-08-20 bewezen dat modellen het gevraagde
mermaid-mindmap-format kunnen negeren (vrije proza, geen fence) — de preview
toonde dan onggerenderde markdown. Zelfde retry-seam als slide_deck (PR #37),
infographic en flashcards.
"""
import pytest

from src.notebook_mindmap import validate_mindmap_markdown

_VALID_MINDMAP_MD = """```mermaid
mindmap
  root((SamenWijzer))
    Doel
      Studiesucces verhogen
      Eigen leerpad
    Pijnpunten
      Gefragmenteerde informatie
      Weinig begeleidingstijd
```
De mindmap toont doelen en pijnpunten van SamenWijzer.
"""


def test_validate_accepts_documented_mindmap():
    validate_mindmap_markdown(_VALID_MINDMAP_MD)  # geen exception


def test_validate_rejects_free_prose():
    with pytest.raises(ValueError, match="mermaid"):
        validate_mindmap_markdown(
            "Dit document fungeert als een zeer uitgebreid strategisch advies "
            "om de integratie van AI te begeleiden."
        )


def test_validate_rejects_fence_without_mindmap_keyword():
    md = _VALID_MINDMAP_MD.replace("mindmap\n", "graph TD\n", 1)
    with pytest.raises(ValueError, match="mindmap"):
        validate_mindmap_markdown(md)


def test_validate_rejects_missing_root():
    md = _VALID_MINDMAP_MD.replace("  root((SamenWijzer))\n", "")
    with pytest.raises(ValueError, match="root"):
        validate_mindmap_markdown(md)


def test_validate_rejects_too_few_branches():
    md = """```mermaid
mindmap
  root((Onderwerp))
    Enige tak
```
Eén zin.
"""
    with pytest.raises(ValueError, match="takken"):
        validate_mindmap_markdown(md)


def test_mindmap_registered_in_kind_validators():
    from src.notebook_artifacts import _KIND_VALIDATORS
    assert _KIND_VALIDATORS.get("mindmap") is validate_mindmap_markdown


# ---- parser -------------------------------------------------------------

def test_parse_builds_tree():
    from src.notebook_mindmap import parse_mermaid_mindmap
    tree = parse_mermaid_mindmap(_VALID_MINDMAP_MD)
    assert tree["label"] == "SamenWijzer"
    assert [c["label"] for c in tree["children"]] == ["Doel", "Pijnpunten"]
    assert [c["label"] for c in tree["children"][0]["children"]] == [
        "Studiesucces verhogen", "Eigen leerpad"]


def test_parse_returns_none_without_fence():
    from src.notebook_mindmap import parse_mermaid_mindmap
    assert parse_mermaid_mindmap("gewoon proza") is None


def test_parse_caption_line_below_fence():
    from src.notebook_mindmap import parse_mermaid_mindmap
    tree = parse_mermaid_mindmap(_VALID_MINDMAP_MD)
    assert "doelen en pijnpunten" in (tree.get("caption") or "")


# ---- viewer-renderer ----------------------------------------------------

def test_viewer_renders_clickable_tree():
    from src.notebook_mindmap import generate_mindmap_viewer
    from datetime import datetime
    out = generate_mindmap_viewer("Eigen titel", _VALID_MINDMAP_MD, "NB", datetime(2026, 8, 23))
    assert "SamenWijzer" in out and "Pijnpunten" in out
    assert "mm-node" in out          # klikbare knoop-knoppen
    assert "mm-edge" in out          # verbindingen tussen knopen
    assert "Alles uitklappen" in out and "Alles inklappen" in out


def test_viewer_escapes_labels():
    from src.notebook_mindmap import generate_mindmap_viewer
    from datetime import datetime
    md = _VALID_MINDMAP_MD.replace("Eigen leerpad", "X <script>alert(1)</script>")
    out = generate_mindmap_viewer(None, md, "NB", datetime(2026, 8, 23))
    assert "<script>alert(1)</script>" not in out


def test_viewer_degrades_on_malformed_content():
    from src.notebook_mindmap import generate_mindmap_viewer
    from datetime import datetime
    out = generate_mindmap_viewer("Eigen titel", "kapotte inhoud", "NB", datetime(2026, 8, 23))
    assert "Kon de mindmap niet lezen" in out
    assert "Eigen titel" in out


def test_viewer_no_external_resources():
    from src.notebook_mindmap import generate_mindmap_viewer
    from datetime import datetime
    out = generate_mindmap_viewer(None, _VALID_MINDMAP_MD, "NB", datetime(2026, 8, 23))
    assert "http://" not in out and "https://" not in out
    assert "<link" not in out


# ---- route-dispatch -----------------------------------------------------

import uuid

from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import routes.notebook_routes as nbr
from tests.helpers.sqlite_db import make_temp_sqlite


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


def test_route_mindmap_uses_interactive_viewer(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = c.post("/api/notebooks", json={"name": "NB"}).json()["id"]
    s = ts()
    try:
        document_id = str(uuid.uuid4())
        s.add(db.Document(id=document_id, title="NB — Mindmap", owner="ed",
                          language="markdown", current_content=_VALID_MINDMAP_MD,
                          session_id=None))
        art = db.NotebookArtifact(id=str(uuid.uuid4()), notebook_id=nb_id,
                                  document_id=document_id, kind="mindmap")
        s.add(art)
        s.commit()
        art_id = art.id
    finally:
        s.close()

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert "mm-node" in r.text
    assert "Pijnpunten" in r.text
