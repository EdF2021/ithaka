"""Notebook "flashcards" + "data_table" artifacts: registry, parser/renderer
units for the flashcards flip-card page, and the report route dispatch.

Route-test fixtures mirror tests/test_notebook_infographic.py (file-backed
temp sqlite via make_temp_sqlite, monkeypatched nbr.SessionLocal /
nbr.get_current_user, real Document + NotebookArtifact rows written
directly so owner-scoping and the kind dispatch are exercised against real
data, not a fake).
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-flashcards")

import uuid
from datetime import datetime

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import routes.notebook_routes as nbr
from src.notebook_artifacts import ARTIFACT_KINDS
from src.notebook_flashcards import _parse_flashcards_markdown, generate_flashcards
from src.notebook_report import ENGLISH_KIND_LABELS
from tests.helpers.sqlite_db import make_temp_sqlite

_FULL_MD = """# Begrippen studiesucces

### Wat is een skillspaspoort?
Een overzicht van iemands aangetoonde vaardigheden, los van diploma's.

### Welke drie panelen heeft de werkruimte?
Bronnen, gesprek en studio.

### Wat voorspelt studiesucces het sterkst?
Aanwezigheid bij de practica, sterker dan vooropleiding.
"""

_TABLE_MD = """# Kerncijfers

| Indicator | Waarde | Bron |
|---|---|---|
| Slagingspercentage | 42% | Onderzoek 2026 |
| Duur | 12 weken | Studiegids |
"""


# ---- kind registration ------------------------------------------------

def test_flashcards_kind_registered_with_dutch_label():
    assert "flashcards" in ARTIFACT_KINDS
    assert ARTIFACT_KINDS["flashcards"]["label"] == "Flashcards"
    assert "taal van de bronnen" in ARTIFACT_KINDS["flashcards"]["prompt"]


def test_data_table_kind_registered_with_dutch_label():
    assert "data_table" in ARTIFACT_KINDS
    assert ARTIFACT_KINDS["data_table"]["label"] == "Gegevenstabel"
    assert "taal van de bronnen" in ARTIFACT_KINDS["data_table"]["prompt"]


def test_new_kinds_in_english_kind_labels():
    assert ENGLISH_KIND_LABELS["flashcards"] == "Flashcards"
    assert ENGLISH_KIND_LABELS["data_table"] == "Data table"


# ---- parser -----------------------------------------------------------

def test_parser_extracts_title_and_cards():
    parsed = _parse_flashcards_markdown(_FULL_MD)
    assert parsed["title"] == "Begrippen studiesucces"
    fronts = [c["front"] for c in parsed["cards"]]
    assert fronts == [
        "Wat is een skillspaspoort?",
        "Welke drie panelen heeft de werkruimte?",
        "Wat voorspelt studiesucces het sterkst?",
    ]
    assert parsed["cards"][1]["back"] == "Bronnen, gesprek en studio."


def test_parser_multiline_back_joined_as_paragraphs():
    md = "### Vraag\nRegel een.\n\nRegel twee.\n"
    parsed = _parse_flashcards_markdown(md)
    assert parsed["cards"][0]["back"] == "Regel een.\n\nRegel twee."


def test_parser_skips_card_without_back():
    md = "### Vraag zonder antwoord\n### Echte vraag\nHet antwoord.\n"
    parsed = _parse_flashcards_markdown(md)
    assert [c["front"] for c in parsed["cards"]] == ["Echte vraag"]


def test_parser_tolerates_no_h1_and_preamble():
    md = "Wat inleidende prosa.\n\n### Vraag\nAntwoord.\n"
    parsed = _parse_flashcards_markdown(md)
    assert parsed["title"] == ""
    assert len(parsed["cards"]) == 1


def test_parser_empty_input_does_not_raise():
    for md in ("", "   ", None):
        parsed = _parse_flashcards_markdown(md or "")
        assert parsed["cards"] == []


# ---- renderer ---------------------------------------------------------

def test_renderer_renders_cards_with_front_and_back():
    html_out = generate_flashcards(
        title=None, markdown=_FULL_MD, notebook_name="NB",
        generated_at=datetime(2026, 8, 22),
    )
    assert "Begrippen studiesucces" in html_out
    assert "Wat is een skillspaspoort?" in html_out
    assert "Bronnen, gesprek en studio." in html_out
    # Flip affordance: cards are interactive, count is shown.
    assert "fc-card" in html_out
    assert "3" in html_out


def test_renderer_falls_back_to_title_arg_when_no_h1():
    html_out = generate_flashcards(
        title="Eigen titel", markdown="### V\nA.\n", notebook_name="NB",
        generated_at=datetime(2026, 8, 22),
    )
    assert "Eigen titel" in html_out


def test_renderer_never_empty_on_missing_structure():
    html_out = generate_flashcards(
        title=None, markdown="alleen prosa, geen kaarten",
        notebook_name="NB", generated_at=datetime(2026, 8, 22),
    )
    assert "<html" in html_out.lower()
    assert "Flashcards" in html_out


def test_renderer_escapes_html_content():
    html_out = generate_flashcards(
        title=None,
        markdown="### <script>alert(1)</script>\nEn <b>dit</b> ook.\n",
        notebook_name="<i>NB</i>", generated_at=datetime(2026, 8, 22),
    )
    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;" in html_out
    assert "<i>NB</i>" not in html_out


def test_renderer_has_no_external_resources():
    html_out = generate_flashcards(
        title=None, markdown=_FULL_MD, notebook_name="NB",
        generated_at=datetime(2026, 8, 22),
    )
    assert "http://" not in html_out
    assert "https://" not in html_out
    assert "<link" not in html_out
    assert "cdn." not in html_out.lower()


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


def _make_notebook(c, name="NB"):
    return c.post("/api/notebooks", json={"name": name}).json()["id"]


def _make_artifact(ts, notebook_id, kind, title=None, content=_FULL_MD):
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
            kind=kind,
        )
        s.add(artifact)
        s.commit()
        s.refresh(artifact)
        return artifact.id
    finally:
        s.close()


def test_route_flashcards_uses_flip_card_renderer(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    art_id = _make_artifact(ts, nb_id, kind="flashcards", content=_FULL_MD)

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "fc-card" in r.text
    assert "Wat is een skillspaspoort?" in r.text


def test_route_data_table_renders_via_generic_report(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    art_id = _make_artifact(ts, nb_id, kind="data_table", content=_TABLE_MD)

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert "Slagingspercentage" in r.text
    # Generic editorial template, not the flashcards renderer.
    assert "fc-card" not in r.text


# ---- format-validator (validator-seam in generate_artifact) ------------
#
# Productie-artifact van 2026-08-22 bewees dat een model het kaartformat kan
# negeren ("## "-secties, één "### ") — de renderer vond toen maar één kaart.
# Zelfde retry-seam als slide_deck/infographic.

_VALID_CARDS_MD = """# Kernbegrippen SamenWijzer

### Wat is de digitale gids?
Een chatomgeving waarin studenten vragen stellen over hun opleiding.

### Wat is een OER?
De onderwijs- en examenregeling van een opleiding. Die beschrijft toetsing en regels.

### Wat is een kwalificatiedossier?
Het landelijke document dat beschrijft wat een mbo-student moet kennen en kunnen.
"""


def test_validate_accepts_documented_card_structure():
    from src.notebook_flashcards import validate_flashcards_markdown
    validate_flashcards_markdown(_VALID_CARDS_MD)  # geen exception


def test_validate_rejects_too_few_cards():
    from src.notebook_flashcards import validate_flashcards_markdown
    md = "# Titel\n\n### Enige kaart\nEén achterkant.\n"
    with pytest.raises(ValueError, match="kaart"):
        validate_flashcards_markdown(md)


def test_validate_rejects_h2_sections():
    # De vorm van de echte failure: "## "-hoofdstukken i.p.v. "### "-kaarten.
    from src.notebook_flashcards import validate_flashcards_markdown
    md = _VALID_CARDS_MD + "\n## 1. Visie & Strategische doelstelling\ntekst\n"
    with pytest.raises(ValueError, match="##"):
        validate_flashcards_markdown(md)


def test_validate_rejects_card_headings_without_backs():
    from src.notebook_flashcards import validate_flashcards_markdown
    md = "# Titel\n\n### Vraag een\n\n### Vraag twee\n\n### Vraag drie\n"
    with pytest.raises(ValueError, match="achterzijde"):
        validate_flashcards_markdown(md)


def test_flashcards_registered_in_kind_validators():
    from src.notebook_artifacts import _KIND_VALIDATORS
    from src.notebook_flashcards import validate_flashcards_markdown
    assert _KIND_VALIDATORS.get("flashcards") is validate_flashcards_markdown
