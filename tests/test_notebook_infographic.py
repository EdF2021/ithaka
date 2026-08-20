"""Notebook "infographic" artifact: parser/renderer units + report route.

Route-test fixtures mirror tests/test_notebook_report.py (file-backed temp
sqlite via make_temp_sqlite, monkeypatched nbr.SessionLocal /
nbr.get_current_user, real Document + NotebookArtifact rows written
directly so owner-scoping and the kind guard are exercised against real
data, not a fake).
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-infographic")

import uuid
from datetime import datetime

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import routes.notebook_routes as nbr
from src.notebook_artifacts import ARTIFACT_KINDS
from src.notebook_infographic import _parse_infographic_markdown, generate_infographic
from src.notebook_report import ENGLISH_KIND_LABELS
from tests.helpers.sqlite_db import make_temp_sqlite

_FULL_MD = """# Course Facts at a Glance

## Key numbers
- **42%** — students passed on the first try
- **3** — panels in the workspace
- **12 weeks** — course duration

## Highlights
- Attendance strongly predicts the outcome
- The course covers three core modules

## Risks
- Some students skip the practice sessions
- Feedback loops are slow in week 1

> The course works, but attendance drives the outcome.
"""


# ---- kind registration ------------------------------------------------

def test_infographic_kind_registered_with_dutch_label():
    assert "infographic" in ARTIFACT_KINDS
    assert ARTIFACT_KINDS["infographic"]["label"] == "Infographic"
    assert "Key numbers" in ARTIFACT_KINDS["infographic"]["prompt"]


def test_infographic_in_english_kind_labels():
    assert ENGLISH_KIND_LABELS["infographic"] == "Infographic"


# ---- parser (pure) -----------------------------------------------------

def test_parser_extracts_full_structure():
    parsed = _parse_infographic_markdown(_FULL_MD)
    assert parsed["title"] == "Course Facts at a Glance"
    assert parsed["stats"] == [
        ("42%", "students passed on the first try"),
        ("3", "panels in the workspace"),
        ("12 weeks", "course duration"),
    ]
    headings = [h for h, _, _ in parsed["sections"]]
    assert headings == ["Highlights", "Risks"]
    assert parsed["sections"][0][1] == [
        "Attendance strongly predicts the outcome",
        "The course covers three core modules",
    ]
    assert parsed["takeaway"] == "The course works, but attendance drives the outcome."


def test_parser_tolerates_missing_structure():
    parsed = _parse_infographic_markdown("Just some plain prose, no headings at all.")
    assert parsed["title"] is None
    assert parsed["stats"] == []
    assert parsed["sections"] == []
    assert parsed["takeaway"] is None
    assert parsed["leftover_paragraphs"] == ["Just some plain prose, no headings at all."]


def test_parser_empty_input_does_not_raise():
    parsed = _parse_infographic_markdown("")
    assert parsed["stats"] == []
    assert parsed["sections"] == []


def test_parser_promotes_non_english_key_numbers_heading():
    """The generation prompt orders the model to write in the sources'
    language, so a Dutch source set can plausibly emit '## Kerncijfers'
    instead of the literal '## Key numbers' heading. The stat grid must
    still populate via structural detection (every bullet fits the strict
    stat-bullet shape)."""
    md = (
        "# Cursusfeiten\n\n"
        "## Kerncijfers\n"
        "- **42%** — geslaagd in eerste poging\n"
        "- **3** — panelen in de werkruimte\n\n"
        "## Aandachtspunten\n"
        "- Sommige studenten missen de oefensessies\n"
    )
    parsed = _parse_infographic_markdown(md)
    assert parsed["stats"] == [
        ("42%", "geslaagd in eerste poging"),
        ("3", "panelen in de werkruimte"),
    ]
    headings = [h for h, _, _ in parsed["sections"]]
    assert "Kerncijfers" not in headings
    assert headings == ["Aandachtspunten"]


def test_parser_accepts_hyphen_and_endash_separators():
    md = (
        "# T\n\n"
        "## Key numbers\n"
        "- **42%** — en-dash label\n"
        "- **7** - hyphen label\n"
    )
    parsed = _parse_infographic_markdown(md)
    assert parsed["stats"] == [
        ("42%", "en-dash label"),
        ("7", "hyphen label"),
    ]


def test_parser_no_key_numbers_section_still_renders_sections():
    md = (
        "# T\n\n"
        "## Highlights\n"
        "- Fact one\n"
        "- Fact two\n\n"
        "> Takeaway line.\n"
    )
    parsed = _parse_infographic_markdown(md)
    assert parsed["stats"] == []
    assert [h for h, _, _ in parsed["sections"]] == ["Highlights"]
    assert parsed["takeaway"] == "Takeaway line."


def test_parser_malformed_stat_bullet_without_bold_degrades_gracefully():
    md = (
        "# T\n\n"
        "## Key numbers\n"
        "- 42% zonder bold — label\n"
        "- **3** — wel goed\n"
    )
    parsed = _parse_infographic_markdown(md)
    assert ("3", "wel goed") in parsed["stats"]
    flat = str(parsed)
    assert "42% zonder bold" in flat  # niet stil weggegooid


# ---- renderer (pure) ----------------------------------------------------

def test_renderer_full_structure_has_stat_cards():
    html_out = generate_infographic(
        title="fallback", markdown=_FULL_MD, notebook_name="My Notebook",
        generated_at=datetime(2026, 8, 20),
    )
    assert "Course Facts at a Glance" in html_out
    assert "42%" in html_out
    assert "students passed on the first try" in html_out
    assert "3" in html_out
    assert "panels in the workspace" in html_out
    assert "Highlights" in html_out
    assert "Attendance strongly predicts the outcome" in html_out
    assert "The course works, but attendance drives the outcome." in html_out
    assert "My Notebook" in html_out


def test_renderer_falls_back_to_title_arg_when_no_h1():
    html_out = generate_infographic(
        title="Provided Title", markdown="Just prose, no heading.",
        notebook_name="NB", generated_at=datetime(2026, 8, 20),
    )
    assert "Provided Title" in html_out


def test_renderer_never_empty_or_broken_on_missing_structure():
    html_out = generate_infographic(
        title=None, markdown="- a loose bullet\n- another loose bullet",
        notebook_name="NB", generated_at=datetime(2026, 8, 20),
    )
    assert "<html" in html_out
    assert "a loose bullet" in html_out
    # No title arg and no markdown heading: falls back to the generic label,
    # never an empty <title>.
    assert "<title>Infographic</title>" in html_out


def test_renderer_escapes_html_content():
    html_out = generate_infographic(
        title="<script>alert(1)</script>",
        markdown="# <script>alert(2)</script>\n\n## Key numbers\n"
                  "- **<b>10</b>** — <i>students</i>\n",
        notebook_name="<img src=x onerror=alert(3)>",
        generated_at=datetime(2026, 8, 20),
    )
    assert "<script>" not in html_out
    assert "<img " not in html_out
    assert "&lt;script&gt;" in html_out
    assert "&lt;img" in html_out


def test_renderer_converts_bold_markers_to_strong_not_literal_asterisks():
    html_out = generate_infographic(
        title="T",
        markdown="# Title\n\n## Highlights\n- **Attendance** drives the outcome\n",
        notebook_name="NB", generated_at=datetime(2026, 8, 20),
    )
    assert "<strong>Attendance</strong> drives the outcome" in html_out
    assert "**Attendance**" not in html_out


def test_renderer_single_leftover_card_spans_full_width():
    html_out = generate_infographic(
        title=None, markdown="- a loose bullet",
        notebook_name="NB", generated_at=datetime(2026, 8, 20),
    )
    assert 'class="ig-sections-grid ig-single-card"' in html_out


def test_renderer_has_no_external_resources():
    html_out = generate_infographic(
        title="T", markdown=_FULL_MD, notebook_name="NB",
        generated_at=datetime(2026, 8, 20),
    )
    assert "http://" not in html_out
    assert "https://" not in html_out
    assert "<link" not in html_out
    assert "<script" not in html_out
    assert "cdn." not in html_out.lower()


# ---- route ---------------------------------------------------------------

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


def _make_artifact(ts, notebook_id, kind="infographic", title=None,
                    content=_FULL_MD, audio_path=None):
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


def test_route_infographic_200_contains_title(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    art_id = _make_artifact(ts, nb_id, kind="infographic", content=_FULL_MD)

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Course Facts at a Glance" in r.text


def test_route_infographic_uses_artifact_title_fallback(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    art_id = _make_artifact(
        ts, nb_id, kind="infographic", title="A Distinctive Title",
        content="No heading here, just prose.",
    )

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert "A Distinctive Title" in r.text


def test_route_podcast_still_404(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    art_id = _make_artifact(ts, nb_id, kind="podcast", content="", audio_path="somefile.wav")

    r = c.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 404
