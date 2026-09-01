# Notebooks "Rapporten" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Rapporten" tile to the notebook Studio panel that opens a modal offering fixed report templates (Overzichtsdocument, Studiemateriaal, Blogpost, plus a free-text "Zelf rapport maken") and up to 4 AI-recommended, content-aware report layouts, generating the chosen report as a normal notebook artifact.

**Architecture:** New artifact kind `"report"` in the existing `src/notebook_artifacts.py` generation pipeline, driven by a per-call `layout_instruction` (fixed template text, an AI suggestion, or free text) appended to the user-role message — never the system role. A new `src/notebook_report_layouts.py` module generates and caches the 4 AI-recommended layouts (keyed on a fingerprint of the notebook's sources). Frontend adds one tile + one self-contained modal to the existing `notebookWorkspace.js`.

**Tech Stack:** FastAPI + SQLAlchemy (SQLite) backend, vanilla-JS ES module frontend, no build step.

**Spec:** `docs/superpowers/specs/2026-09-01-notebooks-rapporten-design.md`

## Global Constraints

- All generated Dutch UI/content text must go through `DUTCH_OUTPUT_RULE` (from `src/notebook_language.py`) in every new generation prompt — per repo CLAUDE.md, this rule is changed only in that module, never inlined elsewhere.
- Untrusted notebook-source text is never placed in the system role of any LLM call — only via `untrusted_context_message` in the user role. `layout_instruction` (whatever its origin) also goes in the user role, never system — it may be influenced by untrusted source content via the recommendation call.
- No Unicode emoji in UI or code — inline monochrome SVG only, matching the existing 14x14 `stroke="currentColor"` icon style in `static/js/notebookWorkspace.js`.
- Reuse existing CSS variables (`--red`, `--fg`, `--bg`, `--panel`, `--border`, `--accent-warm`, `--color-subheader`, etc.) and existing button/card classes (`.dashboard-action-btn`, `.dashboard-empty`, `.modal`/`.modal-content`/`.modal-header`/`.modal-body`/`.close-btn`) — no new color values.
- Migrations use the `_add_column_if_missing('table', 'column', 'SQLTYPE')` helper in `core/database.py`, called from the single migration list near the end of that file — never a bespoke ALTER TABLE.
- `src/notebook_report.py` already exists and does something unrelated (the "Open Visual Report" print-view adapter) — the new recommendation module is named `src/notebook_report_layouts.py` to avoid confusion; `src/notebook_report.py` gets only a one-line addition (Task 4).

---

## Task 1: Database migration — Notebook report-layout cache columns

**Files:**
- Modify: `core/database.py` (Notebook class ~line 1512-1535; migration functions ~line 793-802; migration call list ~line 1755-1798)
- Test: `tests/test_services_notebook_artifacts.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Notebook.report_layouts_json` (TEXT, nullable), `Notebook.report_layouts_fingerprint` (VARCHAR, nullable) — Task 2 reads/writes these directly on the ORM object.

- [ ] **Step 1: Write the failing migration test**

Add to `tests/test_services_notebook_artifacts.py`, right after `test_migrate_add_notebook_artifact_title_column_missing_db_is_noop` (around line 185):

```python
def test_migrate_add_notebook_report_layouts_columns(tmp_path, monkeypatch):
    """Mirrors test_migrate_add_notebook_artifact_title_column."""
    import sqlite3

    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE notebooks (
            id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at DATETIME,
            updated_at DATETIME
        );
        INSERT INTO notebooks(id, owner, name) VALUES ('n1', 'ed', 'Thesis');
        """
    )
    conn.close()

    monkeypatch.setattr(db, "DATABASE_URL", f"sqlite:///{db_path}")

    conn = sqlite3.connect(db_path)
    try:
        columns_before = [row[1] for row in conn.execute("PRAGMA table_info(notebooks)")]
    finally:
        conn.close()
    assert "report_layouts_json" not in columns_before
    assert "report_layouts_fingerprint" not in columns_before

    db._migrate_add_notebook_report_layouts_columns()

    conn = sqlite3.connect(db_path)
    try:
        columns_after = [row[1] for row in conn.execute("PRAGMA table_info(notebooks)")]
        assert "report_layouts_json" in columns_after
        assert "report_layouts_fingerprint" in columns_after
        row = conn.execute(
            "SELECT report_layouts_json, report_layouts_fingerprint FROM notebooks WHERE id = 'n1'"
        ).fetchone()
        assert row == (None, None)
    finally:
        conn.close()

    # Idempotent: running it again on an already-migrated DB must not raise.
    db._migrate_add_notebook_report_layouts_columns()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_services_notebook_artifacts.py::test_migrate_add_notebook_report_layouts_columns -v`
Expected: FAIL with `AttributeError: module 'core.database' has no attribute '_migrate_add_notebook_report_layouts_columns'`

- [ ] **Step 3: Add the Notebook columns and migration function**

In `core/database.py`, in the `Notebook` class (find `cover_image = Column(String, nullable=True)` around line 1522), add directly below it:

```python
    # Cached AI-recommended report layouts (Rapporten feature) — a JSON array
    # of {title, description, instruction}, keyed by a fingerprint of the
    # notebook's indexed sources so re-opening the modal doesn't re-run the
    # LLM call when nothing changed. Both null until the first fetch.
    report_layouts_json = Column(Text, nullable=True)
    report_layouts_fingerprint = Column(String, nullable=True)
```

Add `Text` to the sqlalchemy imports at the top of `core/database.py` if not already imported — check first with:
`grep -n "^from sqlalchemy import" core/database.py`
If `Text` is missing from that import line, add it.

Then, right after `_migrate_add_notebook_cover_image_column` (around line 793-794), add:

```python
def _migrate_add_notebook_report_layouts_columns():
    _add_column_if_missing('notebooks', 'report_layouts_json', 'TEXT')
    _add_column_if_missing('notebooks', 'report_layouts_fingerprint', 'VARCHAR')
```

Finally, in the migration call list (find `_migrate_add_notebook_cover_image_column()` around line 1771), add right after it:

```python
    _migrate_add_notebook_cover_image_column()
    _migrate_add_notebook_report_layouts_columns()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_services_notebook_artifacts.py::test_migrate_add_notebook_report_layouts_columns -v`
Expected: PASS

- [ ] **Step 5: Run the full notebook-artifacts test file to check nothing broke**

Run: `.venv/bin/python -m pytest tests/test_services_notebook_artifacts.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add core/database.py tests/test_services_notebook_artifacts.py
git commit -m "feat(notebooks): add report-layouts cache columns to Notebook"
```

---

## Task 2: `src/notebook_report_layouts.py` — templates, fingerprint cache, AI recommendations

**Files:**
- Create: `src/notebook_report_layouts.py`
- Test: `tests/test_notebook_report_layouts.py`

**Interfaces:**
- Consumes: `src.notebook_artifacts._source_entries`, `src.notebook_artifacts._strip_think_blocks`, `src.notebook_artifacts.gather_source_text`, `src.notebook_artifacts._VALIDATION_ATTEMPTS` (existing private cross-module import pattern — see `src/notebook_audio.py:66` for precedent); `src.notebook_language.DUTCH_OUTPUT_RULE`; `src.prompt_security.UNTRUSTED_CONTEXT_POLICY`, `untrusted_context_message`; `src.task_endpoint.task_llm_call_async`; `core.database.Notebook`.
- Produces: `FIXED_TEMPLATES: list[dict]` (module constant, 3 items, each `{key, title, description, instruction}`), `async def get_recommended_layouts(notebook, db_session, owner: str) -> list[dict]` (each item `{title, description, instruction}`, 0-4 items) — consumed by Task 3's route wiring (Task 5).

- [ ] **Step 1: Write the failing tests for the fixed templates and fingerprint helper**

Create `tests/test_notebook_report_layouts.py`:

```python
"""Tests for src/notebook_report_layouts.py.

The LLM call is always monkeypatched (same convention as
tests/test_services_notebook_artifacts.py — hermetic, never reaches a real
endpoint).
"""
import uuid

import pytest

import core.database as db
import src.notebook_report_layouts as report_layouts
from tests.helpers.sqlite_db import make_temp_sqlite

_TS, _ENGINE, _TMPDB = make_temp_sqlite(db.Base.metadata)


def make_notebook(session, owner="ed", name="Thesis"):
    nb = db.Notebook(id=str(uuid.uuid4()), owner=owner, name=name)
    session.add(nb)
    session.commit()
    return nb


def make_document(session, title="Bron", owner="ed", content="inhoud"):
    doc = db.Document(id=str(uuid.uuid4()), title=title, owner=owner,
                      current_content=content)
    session.add(doc)
    session.commit()
    return doc


def make_source(session, notebook, filename="a.txt", content="inhoud",
                status="indexed", owner="ed"):
    doc = make_document(session, title=filename, owner=owner, content=content)
    src = db.NotebookSource(id=str(uuid.uuid4()), notebook_id=notebook.id,
                            document_id=doc.id, filename=filename,
                            status=status, chunk_count=1)
    session.add(src)
    session.commit()
    return src


def test_fixed_templates_have_three_entries_with_required_fields():
    assert len(report_layouts.FIXED_TEMPLATES) == 3
    keys = {t["key"] for t in report_layouts.FIXED_TEMPLATES}
    assert keys == {"overview", "study_material", "blogpost"}
    for t in report_layouts.FIXED_TEMPLATES:
        assert t["title"] and t["description"] and t["instruction"]


def test_fingerprint_stable_for_same_entries_regardless_of_order():
    a = report_layouts._fingerprint_sources([("b.txt", "y"), ("a.txt", "x")])
    b = report_layouts._fingerprint_sources([("a.txt", "x"), ("b.txt", "y")])
    assert a == b


def test_fingerprint_changes_when_content_changes():
    a = report_layouts._fingerprint_sources([("a.txt", "x")])
    b = report_layouts._fingerprint_sources([("a.txt", "y")])
    assert a != b


def test_parse_layout_suggestions_valid_json():
    content = '''```json
[
  {"title": "T1", "description": "D1", "instruction": "I1"},
  {"title": "T2", "description": "D2", "instruction": "I2"}
]
```'''
    result = report_layouts._parse_layout_suggestions(content)
    assert result == [
        {"title": "T1", "description": "D1", "instruction": "I1"},
        {"title": "T2", "description": "D2", "instruction": "I2"},
    ]


def test_parse_layout_suggestions_no_json_raises():
    with pytest.raises(ValueError, match="geen JSON"):
        report_layouts._parse_layout_suggestions("gewoon tekst, geen json")


def test_parse_layout_suggestions_missing_field_raises():
    content = '```json\n[{"title": "T1", "description": "D1"}]\n```'
    with pytest.raises(ValueError, match="instruction"):
        report_layouts._parse_layout_suggestions(content)


def test_parse_layout_suggestions_caps_at_four():
    items = [{"title": f"T{i}", "description": "D", "instruction": "I"} for i in range(6)]
    import json
    content = "```json\n" + json.dumps(items) + "\n```"
    result = report_layouts._parse_layout_suggestions(content)
    assert len(result) == 4


async def test_get_recommended_layouts_no_sources_returns_empty_no_llm_call(monkeypatch):
    calls = []

    async def _fake_llm(messages, **kwargs):
        calls.append(messages)
        return "should not be called"

    monkeypatch.setattr(report_layouts, "task_llm_call_async", _fake_llm)
    s = _TS()
    try:
        nb = make_notebook(s)
        result = await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert result == []
        assert calls == []
    finally:
        s.close()


async def test_get_recommended_layouts_generates_and_caches(monkeypatch):
    content = '''```json
[
  {"title": "T1", "description": "D1", "instruction": "I1"},
  {"title": "T2", "description": "D2", "instruction": "I2"},
  {"title": "T3", "description": "D3", "instruction": "I3"},
  {"title": "T4", "description": "D4", "instruction": "I4"}
]
```'''
    calls = []

    async def _fake_llm(messages, **kwargs):
        calls.append(messages)
        return content

    monkeypatch.setattr(report_layouts, "task_llm_call_async", _fake_llm)
    s = _TS()
    try:
        nb = make_notebook(s)
        make_source(s, nb, filename="a.txt", content="inhoud over AI-geletterdheid")

        result = await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert len(result) == 4
        assert result[0]["title"] == "T1"
        assert len(calls) == 1

        # Cached on the Notebook row.
        s.refresh(nb)
        assert nb.report_layouts_fingerprint is not None
        assert nb.report_layouts_json is not None

        # Second call with unchanged sources must not call the LLM again.
        result2 = await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert result2 == result
        assert len(calls) == 1
    finally:
        s.close()


async def test_get_recommended_layouts_regenerates_when_sources_change(monkeypatch):
    content = '```json\n[{"title": "T1", "description": "D1", "instruction": "I1"}]\n```'
    calls = []

    async def _fake_llm(messages, **kwargs):
        calls.append(messages)
        return content

    monkeypatch.setattr(report_layouts, "task_llm_call_async", _fake_llm)
    s = _TS()
    try:
        nb = make_notebook(s)
        make_source(s, nb, filename="a.txt", content="eerste versie")
        await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert len(calls) == 1

        make_source(s, nb, filename="b.txt", content="tweede bron")
        await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert len(calls) == 2
    finally:
        s.close()


async def test_get_recommended_layouts_bad_json_after_retries_returns_empty(monkeypatch):
    async def _fake_llm(messages, **kwargs):
        return "geen geldige json, ooit"

    monkeypatch.setattr(report_layouts, "task_llm_call_async", _fake_llm)
    s = _TS()
    try:
        nb = make_notebook(s)
        make_source(s, nb)
        result = await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert result == []
    finally:
        s.close()


def _system_content(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "system")


def _user_content(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "user")


async def test_source_text_never_in_system_role(monkeypatch):
    content = '```json\n[{"title": "T1", "description": "D1", "instruction": "I1"}]\n```'
    captured = {}

    async def _fake_llm(messages, **kwargs):
        captured["messages"] = messages
        return content

    monkeypatch.setattr(report_layouts, "task_llm_call_async", _fake_llm)
    s = _TS()
    try:
        nb = make_notebook(s)
        make_source(s, nb, filename="geheim.txt", content="STRIKT_VERTROUWELIJKE_MARKER")
        await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert "STRIKT_VERTROUWELIJKE_MARKER" not in _system_content(captured["messages"])
        assert "STRIKT_VERTROUWELIJKE_MARKER" in _user_content(captured["messages"])
    finally:
        s.close()


def test_dutch_output_rule_in_suggestion_prompt():
    from src.notebook_language import DUTCH_OUTPUT_RULE
    assert DUTCH_OUTPUT_RULE in report_layouts._LAYOUT_SUGGESTION_PROMPT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_notebook_report_layouts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.notebook_report_layouts'`

- [ ] **Step 3: Implement `src/notebook_report_layouts.py`**

```python
"""AI-recommended report layouts for the notebook "Rapporten" feature.

Distinct from src/notebook_report.py, which is an unrelated adapter that
renders any notebook artifact through the shared visual-report print-view
pipeline — this module generates and caches the 4 content-aware report
layout *suggestions* shown in the "Aanbevolen indeling" section of the
"Rapport maken" modal, plus the 3 fixed built-in templates shown in the
"Indeling" section.

The actual report generation (turning a chosen layout's instruction into a
markdown Document) happens through the existing generate_artifact pipeline
in src/notebook_artifacts.py with kind="report" — this module only produces
layout *proposals*, it never writes a Document or NotebookArtifact row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from src.notebook_artifacts import (
    _source_entries,
    _strip_think_blocks,
    _VALIDATION_ATTEMPTS,
    gather_source_text,
)
from src.notebook_language import DUTCH_OUTPUT_RULE
from src.prompt_security import UNTRUSTED_CONTEXT_POLICY, untrusted_context_message
from src.task_endpoint import task_llm_call_async

logger = logging.getLogger(__name__)

MAX_SUGGESTIONS = 4

FIXED_TEMPLATES = [
    {
        "key": "overview",
        "title": "Overzichtsdocument",
        "description": "Overzicht van je bronnen met belangrijke inzichten en citaten",
        "instruction": (
            "Schrijf een overzichtsdocument: een heldere samenvatting van de "
            "belangrijkste inzichten uit de bronnen, met per inzicht een korte "
            "toelichting en waar relevant een citaat of concreet voorbeeld uit de bron."
        ),
    },
    {
        "key": "study_material",
        "title": "Studiemateriaal",
        "description": (
            "Quiz met korte antwoorden, voorgestelde essayvragen en woordenlijst "
            "met belangrijke begrippen"
        ),
        "instruction": (
            "Schrijf studiemateriaal: een korte quiz met korte antwoorden, een "
            "aantal voorgestelde essayvragen zonder antwoord, en een woordenlijst "
            "met de belangrijkste begrippen uit de bronnen en hun definitie."
        ),
    },
    {
        "key": "blogpost",
        "title": "Blogpost",
        "description": "Waardevolle inzichten in de vorm van een goed leesbaar artikel",
        "instruction": (
            "Schrijf een blogpost: een goed leesbaar artikel in journalistieke stijl "
            "dat de waardevolste inzichten uit de bronnen toegankelijk overbrengt aan "
            "een lezer die de bronnen niet kent."
        ),
    },
]

_LAYOUT_SUGGESTION_PROMPT = f"""Je bent een assistent die rapportvormen voorstelt op basis van een set bronnen.

Harde regels:
- {DUTCH_OUTPUT_RULE}
- Stel exact 4 rapportvormen voor die aantoonbaar aansluiten bij de daadwerkelijke inhoud van de bronnen hieronder. Verzin geen onderwerpen die niet in de bronnen voorkomen.
- Elke rapportvorm krijgt een korte titel (maximaal 5 woorden), een korte omschrijving van één zin (maximaal 20 woorden) die uitlegt wat het rapport oplevert, en een instructie van 2 tot 4 zinnen die de structuur, stijl en toon van dat rapport beschrijft.
- De 4 rapportvormen moeten onderling verschillen in invalshoek of doel — geen twee bijna-identieke voorstellen.

Lever exact één codefence met taalaanduiding "json" en daarin één JSON-array van 4 objecten, niets anders. Schema:

[
  {{"title": "korte titel", "description": "korte omschrijving", "instruction": "structuur/stijl/toon-instructie van 2 tot 4 zinnen"}}
]

Gebruik geen markdown binnen de JSON-strings; alleen platte tekst."""

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _fingerprint_sources(entries: list[tuple[str, str]]) -> str:
    """Stable hash of a notebook's (filename, text) source pairs —
    order-independent. Mirrors _fingerprint_entries in
    services/memory/memory_extractor.py."""
    items = sorted(entries)
    h = hashlib.sha256()
    for filename, text in items:
        h.update((filename + "\x1f" + text + "\x1e").encode("utf-8"))
    return h.hexdigest()


def _parse_layout_suggestions(content: str) -> list[dict]:
    """Parse the suggestion LLM's reply into a list of {title, description,
    instruction} dicts. Raises ValueError (Dutch, fed back to the model on
    retry) on any format miss — mirrors extract_slide_deck in
    src/notebook_slides.py."""
    m = _JSON_FENCE_RE.search(content or "")
    raw = (m.group(1) if m else (content or "")).strip()
    if not raw:
        raise ValueError("geen JSON gevonden in het antwoord")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"ongeldige JSON: {e}") from e
    if not isinstance(data, list) or not data:
        raise ValueError("JSON is geen niet-lege array")
    data = data[:MAX_SUGGESTIONS]
    cleaned = []
    for i, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"suggestie {i} is geen object")
        title = item.get("title")
        description = item.get("description")
        instruction = item.get("instruction")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f'suggestie {i}: veld "title" ontbreekt of is leeg')
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f'suggestie {i}: veld "description" ontbreekt of is leeg')
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError(f'suggestie {i}: veld "instruction" ontbreekt of is leeg')
        cleaned.append({
            "title": title.strip(),
            "description": description.strip(),
            "instruction": instruction.strip(),
        })
    return cleaned


async def get_recommended_layouts(notebook, db_session, owner: str) -> list[dict]:
    """Return up to 4 AI-recommended report layouts for `notebook`, cached on
    the Notebook row keyed by a fingerprint of its indexed sources.

    Returns [] (no LLM call, no exception) when the notebook has no usable
    sources, or when the model fails to produce valid output after retries —
    the "Rapport maken" modal still shows the fixed templates either way.
    """
    entries = _source_entries(notebook, db_session)
    if not entries:
        return []

    fingerprint = _fingerprint_sources(entries)
    if notebook.report_layouts_fingerprint == fingerprint and notebook.report_layouts_json:
        try:
            cached = json.loads(notebook.report_layouts_json)
            if isinstance(cached, list):
                return cached
        except (json.JSONDecodeError, TypeError):
            pass  # fall through and regenerate

    source_text = gather_source_text(notebook, db_session)
    user_msg = untrusted_context_message(f"notebook-bronnen: {notebook.name}", source_text)
    messages = [
        {"role": "system", "content": f"{UNTRUSTED_CONTEXT_POLICY}\n\n{_LAYOUT_SUGGESTION_PROMPT}"},
        user_msg,
    ]

    content = ""
    last_error = ""
    suggestions: list[dict] = []
    for attempt in range(_VALIDATION_ATTEMPTS):
        attempt_messages = list(messages)
        if attempt > 0:
            attempt_messages.append({"role": "assistant", "content": content})
            attempt_messages.append({
                "role": "user",
                "content": (
                    "Je vorige antwoord voldeed niet aan het gevraagde formaat "
                    f"({last_error}). Lever het antwoord opnieuw, exact volgens de "
                    "instructie hierboven."
                ),
            })
        try:
            content = await task_llm_call_async(
                attempt_messages, owner=owner, wait_for_quiet=False, workload="foreground"
            )
        except Exception as e:
            logger.warning("Report-layout suggestie-call mislukt: %s", e)
            return []
        content = _strip_think_blocks(content or "").strip()
        if not content:
            last_error = "leeg antwoord"
            continue
        try:
            suggestions = _parse_layout_suggestions(content)
            break
        except ValueError as e:
            last_error = str(e)
            logger.info(
                "Report-layout suggesties: formaat-misser op poging %d/%d: %s",
                attempt + 1, _VALIDATION_ATTEMPTS, last_error,
            )
            continue
    else:
        logger.warning(
            "Report-layout suggesties mislukt na %d pogingen: %s",
            _VALIDATION_ATTEMPTS, last_error,
        )
        return []

    notebook.report_layouts_json = json.dumps(suggestions)
    notebook.report_layouts_fingerprint = fingerprint
    db_session.commit()
    return suggestions
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_notebook_report_layouts.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/notebook_report_layouts.py tests/test_notebook_report_layouts.py
git commit -m "feat(notebooks): AI-recommended report layouts with source-fingerprint cache"
```

---

## Task 3: `src/notebook_artifacts.py` — "report" kind + `layout_instruction` parameter

**Files:**
- Modify: `src/notebook_artifacts.py` (`_KIND_INSTRUCTIONS` ~line 104, `_KIND_LABELS` ~line 241, `generate_artifact` ~line 406-453)
- Test: `tests/test_services_notebook_artifacts.py`

**Interfaces:**
- Consumes: nothing new (extends existing `generate_artifact`).
- Produces: `ARTIFACT_KINDS["report"]` entry; `generate_artifact(notebook_id, owner, kind, db_session, focus=None, layout_instruction=None)` — the new keyword-only-by-convention `layout_instruction` param, consumed by Task 5's route.

- [ ] **Step 1: Write the failing tests**

`test_artifact_kinds_registry_complete` (around line 256-278) does an exact
set-equality check on `ARTIFACT_KINDS` and its labels — adding `"report"`
to the registry will break it unless it's updated. Replace the two
exact-equality blocks in that existing test (do not just add a new test
alongside it):

```python
def test_artifact_kinds_registry_complete():
    assert set(artifacts.ARTIFACT_KINDS) == {
        "study_guide", "briefing", "faq", "quiz", "mindmap", "infographic",
        "flashcards", "data_table", "slide_deck", "report",
    }
    labels = {k: v["label"] for k, v in artifacts.ARTIFACT_KINDS.items()}
    assert labels == {
        "study_guide": "Studiegids", "briefing": "Briefing", "faq": "FAQ",
        "quiz": "Quiz", "mindmap": "Mindmap", "infographic": "Infographic",
        "flashcards": "Flashcards", "data_table": "Gegevenstabel",
        "slide_deck": "Diapresentatie", "report": "Rapport",
    }
    from src.notebook_language import DUTCH_OUTPUT_RULE

    for kind, spec in artifacts.ARTIFACT_KINDS.items():
        assert spec["prompt"].strip(), kind
        # Every prompt forces Dutch output, regardless of the source language.
        assert DUTCH_OUTPUT_RULE in spec["prompt"], kind
        # No leftover per-kind "follow the source language" clause outside
        # the shared rule itself (which legitimately mentions "de taal van
        # de bronnen" while overriding it).
        remainder = spec["prompt"].replace(DUTCH_OUTPUT_RULE, "")
        assert "taal van de bronnen" not in remainder, kind
```

(Only the two literal set/dict blocks changed — the DUTCH_OUTPUT_RULE loop
at the bottom is unchanged and already covers the new "report" kind
automatically once it's added to `_KIND_INSTRUCTIONS`, since every kind's
prompt is built from the shared `_BASE_RULES` template.)

Then add the new generation-behavior tests right after this updated test:

```python
async def test_report_kind_without_layout_instruction_generates(monkeypatch):
    s = _TS()
    try:
        nb = make_notebook(s)
        make_source(s, nb)
        fake = _patch_llm(monkeypatch, _FakeLLM())
        art = await artifacts.generate_artifact(nb.id, "own", "report", s)
        assert art.kind == "report"
        assert fake.calls == 1
    finally:
        s.close()


async def test_report_layout_instruction_lands_in_user_role_not_system(monkeypatch):
    s = _TS()
    try:
        nb = make_notebook(s)
        make_source(s, nb)
        fake = _patch_llm(monkeypatch, _FakeLLM())
        await artifacts.generate_artifact(
            nb.id, "own", "report", s, layout_instruction="Schrijf kort en zakelijk."
        )
        assert "Schrijf kort en zakelijk." not in _system_content(fake.messages)
        assert "Schrijf kort en zakelijk." in _user_content(fake.messages)
    finally:
        s.close()


async def test_layout_instruction_ignored_for_other_kinds(monkeypatch):
    """layout_instruction is only meaningful for kind="report" — passing it
    for another kind must not raise and must not appear in the prompt."""
    s = _TS()
    try:
        nb = make_notebook(s)
        make_source(s, nb)
        fake = _patch_llm(monkeypatch, _FakeLLM())
        await artifacts.generate_artifact(
            nb.id, "own", "faq", s, layout_instruction="irrelevant hier"
        )
        assert "irrelevant hier" not in _user_content(fake.messages)
    finally:
        s.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_services_notebook_artifacts.py -k "report or artifact_kinds_registry" -v`
Expected: FAIL — `test_artifact_kinds_registry_complete` fails on the set/labels equality (no `"report"` in `ARTIFACT_KINDS` yet); the other new tests fail with `TypeError: generate_artifact() got an unexpected keyword argument 'layout_instruction'`.

- [ ] **Step 3: Add the "report" kind**

In `src/notebook_artifacts.py`, in `_KIND_INSTRUCTIONS` (the dict starting at line 104), add a new entry — place it right before the closing `}` of the dict, after the existing `"slide_deck"` entry (find it around line 238, just before `}`):

```python
    "report": """Maak een rapport op basis van de bronnen.

Structuur:
- "# " met een titel in het Nederlands die bij het onderwerp past.
- Volg de indeling-instructie die in het bericht hierna is meegegeven voor structuur, secties, stijl en toon. Geeft die geen duidelijke sectie-indeling, kies dan zelf een heldere indeling met "## "-koppen die recht doet aan de bronnen.
- Gebruik doorlopende alinea's; gebruik bullets of een tabel alleen waar dat de leesbaarheid echt dient.

Regels:
- Is er geen indeling-instructie meegegeven, schrijf dan een overzichtelijk, zakelijk rapport van 500 tot 900 woorden.""",
```

In `_KIND_LABELS` (the dict starting around line 241), add:

```python
    "report": "Rapport",
```

- [ ] **Step 4: Add the `layout_instruction` parameter to `generate_artifact`**

In `src/notebook_artifacts.py`, change the `generate_artifact` signature (around line 406-408):

```python
async def generate_artifact(
    notebook_id: str, owner: str, kind: str, db_session, focus: str | None = None,
    layout_instruction: str | None = None,
) -> NotebookArtifact:
```

Then, right after the existing `focus` block (find it around line 442-449 — `if focus and focus.strip(): ... user_msg = {"role": user_msg["role"], "content": ...}`), add a parallel block for `layout_instruction`:

```python
    if kind == "report" and layout_instruction and layout_instruction.strip():
        layout_instruction_text = (
            f"\n\nIndeling-instructie voor dit rapport: {layout_instruction.strip()} "
            f"Volg deze instructie voor de structuur, stijl en toon van het rapport."
        )
        user_msg = {"role": user_msg["role"], "content": user_msg["content"] + layout_instruction_text}
```

Both this block and the existing `focus` block modify `user_msg` before it is placed into `messages` — check the existing code around line 450 (`messages = [...]`) to confirm both blocks run *before* that line; if the `focus` block already sits before `messages = [...]`, add the new block directly after it, still before `messages = [...]`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_services_notebook_artifacts.py -v`
Expected: all PASS (including the pre-existing tests — this confirms the new kind/param didn't regress anything).

- [ ] **Step 6: Commit**

```bash
git add src/notebook_artifacts.py tests/test_services_notebook_artifacts.py
git commit -m "feat(notebooks): add report artifact kind with layout_instruction"
```

---

## Task 4: `src/notebook_report.py` — English label for the "report" kind

**Files:**
- Modify: `src/notebook_report.py` (`ENGLISH_KIND_LABELS` ~line 23-33)
- Test: `tests/test_notebook_report.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ENGLISH_KIND_LABELS["report"] = "Report"`.

- [ ] **Step 1: Write the failing test**

Check `tests/test_notebook_report.py` for how `ENGLISH_KIND_LABELS` is currently tested:

Run: `grep -n "ENGLISH_KIND_LABELS" tests/test_notebook_report.py`

Add a test near any existing `ENGLISH_KIND_LABELS` assertion in that file:

```python
def test_english_kind_labels_includes_report():
    from src.notebook_report import ENGLISH_KIND_LABELS
    assert ENGLISH_KIND_LABELS["report"] == "Report"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_notebook_report.py::test_english_kind_labels_includes_report -v`
Expected: FAIL with `KeyError: 'report'`

- [ ] **Step 3: Add the entry**

In `src/notebook_report.py`, in the `ENGLISH_KIND_LABELS` dict (line 23-33), add:

```python
    "report": "Report",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_notebook_report.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/notebook_report.py tests/test_notebook_report.py
git commit -m "feat(notebooks): add English label for report kind"
```

---

## Task 5: Routes — `GET /report-layouts` + extend `POST /artifacts`

**Files:**
- Modify: `routes/notebook_routes.py` (imports ~line 20; `create_artifact` ~line 435-484; add new route near it)
- Test: `tests/test_routes_notebook_artifacts.py`

**Interfaces:**
- Consumes: `src.notebook_report_layouts.FIXED_TEMPLATES`, `get_recommended_layouts` (Task 2); `generate_artifact(..., layout_instruction=...)` (Task 3).
- Produces: `GET /api/notebooks/{notebook_id}/report-layouts` → `{"templates": [...], "recommended": [...]}`; `POST /api/notebooks/{notebook_id}/artifacts` now accepts optional `layout_instruction` in the body — consumed by Task 6's frontend.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_routes_notebook_artifacts.py`, right after `test_generate_artifact_non_string_focus_is_400` (find it around line 132-138):

```python
def test_generate_artifact_passes_layout_instruction_through(monkeypatch, ts):
    captured = {}

    async def _fake_generate_artifact_captures_layout(notebook_id, owner, kind, db_session,
                                                        focus=None, layout_instruction=None):
        captured["layout_instruction"] = layout_instruction
        return await _fake_generate_artifact_ok(notebook_id, owner, kind, db_session, focus=focus)

    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_captures_layout)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/artifacts",
               json={"kind": "report", "layout_instruction": "Schrijf kort."})
    assert r.status_code == 200
    assert captured["layout_instruction"] == "Schrijf kort."


def test_generate_artifact_non_string_layout_instruction_is_400(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/artifacts",
               json={"kind": "report", "layout_instruction": 123})
    assert r.status_code == 400


def test_generate_artifact_layout_instruction_too_long_is_400(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/artifacts",
               json={"kind": "report", "layout_instruction": "x" * 2001})
    assert r.status_code == 400
```

Then, at the end of the file, add a new test block for the `report-layouts` route:

```python
# ---- GET /report-layouts ----

def test_report_layouts_returns_fixed_templates_and_empty_recommended_without_sources(monkeypatch, ts):
    async def _fake_get_recommended_layouts(notebook, db_session, owner):
        return []

    monkeypatch.setattr(nbr, "get_recommended_layouts", _fake_get_recommended_layouts)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.get(f"/api/notebooks/{nb_id}/report-layouts")
    assert r.status_code == 200
    body = r.json()
    assert len(body["templates"]) == 3
    assert body["recommended"] == []


def test_report_layouts_returns_recommended_when_available(monkeypatch, ts):
    async def _fake_get_recommended_layouts(notebook, db_session, owner):
        return [{"title": "T1", "description": "D1", "instruction": "I1"}]

    monkeypatch.setattr(nbr, "get_recommended_layouts", _fake_get_recommended_layouts)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.get(f"/api/notebooks/{nb_id}/report-layouts")
    assert r.status_code == 200
    body = r.json()
    assert body["recommended"] == [{"title": "T1", "description": "D1", "instruction": "I1"}]


def test_report_layouts_unknown_notebook_is_404(monkeypatch, ts):
    async def _fake_get_recommended_layouts(notebook, db_session, owner):
        return []

    monkeypatch.setattr(nbr, "get_recommended_layouts", _fake_get_recommended_layouts)
    c = _client(monkeypatch)

    r = c.get("/api/notebooks/does-not-exist/report-layouts")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_routes_notebook_artifacts.py -k "layout_instruction or report_layouts" -v`
Expected: FAIL — the `layout_instruction`-passthrough tests fail because the route doesn't read that field yet (captured stays `{}` or a 400 doesn't happen); the `report_layouts` tests fail with 404 (route doesn't exist).

- [ ] **Step 3: Extend `create_artifact` to accept `layout_instruction`**

In `routes/notebook_routes.py`, in `create_artifact` (around line 435-464), change:

```python
        kind = body.get("kind") if isinstance(body, dict) else None
        focus = body.get("focus") if isinstance(body, dict) else None
        if focus is not None and not isinstance(focus, str):
            raise HTTPException(status_code=400, detail="focus moet een string zijn")
```

to:

```python
        kind = body.get("kind") if isinstance(body, dict) else None
        focus = body.get("focus") if isinstance(body, dict) else None
        layout_instruction = body.get("layout_instruction") if isinstance(body, dict) else None
        if focus is not None and not isinstance(focus, str):
            raise HTTPException(status_code=400, detail="focus moet een string zijn")
        if layout_instruction is not None:
            if not isinstance(layout_instruction, str):
                raise HTTPException(status_code=400, detail="layout_instruction moet een string zijn")
            if len(layout_instruction) > 2000:
                raise HTTPException(status_code=400, detail="layout_instruction is te lang (max 2000 tekens)")
```

Then change the `generate_artifact` call (around line 464):

```python
                artifact = await generate_artifact(notebook_id, user, kind, db_session, focus=focus)
```

to:

```python
                artifact = await generate_artifact(
                    notebook_id, user, kind, db_session,
                    focus=focus, layout_instruction=layout_instruction,
                )
```

- [ ] **Step 4: Add the `GET /report-layouts` route and its imports**

In `routes/notebook_routes.py`, change the import at line 20:

```python
from src.notebook_artifacts import ARTIFACT_KINDS, generate_artifact
```

to:

```python
from src.notebook_artifacts import ARTIFACT_KINDS, generate_artifact
from src.notebook_report_layouts import FIXED_TEMPLATES, get_recommended_layouts
```

Then add a new route right after `create_artifact` (after its closing `finally: db_session.close()` block, before the `DELETE /artifacts/{artifact_id}` route at line ~486):

```python
    @router.get("/api/notebooks/{notebook_id}/report-layouts")
    async def get_report_layouts(request: Request, notebook_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            recommended = await get_recommended_layouts(nb, db_session, user)
            return {
                "templates": FIXED_TEMPLATES,
                "recommended": recommended,
            }
        finally:
            db_session.close()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_routes_notebook_artifacts.py -v`
Expected: all PASS

- [ ] **Step 6: Run the full notebook-related test suite as a regression check**

Run: `.venv/bin/python -m pytest tests/ -k notebook -q`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add routes/notebook_routes.py tests/test_routes_notebook_artifacts.py
git commit -m "feat(notebooks): report-layouts endpoint + layout_instruction on artifacts POST"
```

---

## Task 6: Frontend — "Rapporten" tile + "Rapport maken" modal

**Files:**
- Modify: `static/js/notebookWorkspace.js` (`KIND_LABELS` ~line 918, `_KIND_ICONS` ~line 936, `_studioPanelSkeleton` ~line 1585, `_wireStudioPanel` ~line 1615)

**Interfaces:**
- Consumes: `GET /api/notebooks/{id}/report-layouts`, `POST /api/notebooks/{id}/artifacts` (Task 5); existing module-scope helpers `_fetchJson`, `API_BASE`, `_state`, `_openEpoch`, `_esc`, `_loadArtifacts`, `_RENAME_ICON` (already defined in this file).
- Produces: a "Rapporten" tile that opens the modal; no new exports (this file has no other consumers of these symbols).

This is UI/DOM code with no existing unit-test harness in this repo (per CLAUDE.md, visual/DOM changes are verified with `node --check` for syntax plus a real-browser smoke test, not JS unit tests) — steps below use that convention instead of TDD.

- [ ] **Step 1: Add `KIND_LABELS.report` and `_KIND_ICONS.report`**

In `static/js/notebookWorkspace.js`, in the `KIND_LABELS` object (line 918-930), add:

```javascript
  report: 'Rapporten',
```

In the `_KIND_ICONS` object (line 936-948), add (a 2x2 layout/grid glyph, distinct from `data_table`'s 3-column icon):

```javascript
  report: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="12" y1="3" x2="12" y2="21"/><line x1="3" y1="12" x2="21" y2="12"/></svg>',
```

- [ ] **Step 2: Add the "Rapporten" tile to the studio skeleton**

In `_studioPanelSkeleton()` (line 1585-1610), find the generate-buttons block:

```javascript
        <button type="button" class="nbws-tile notebook-video-gen-btn nbws-tile--video" id="nbws-video-btn"
                data-kind="video"><span class="nbws-tile-icon">${_KIND_ICONS.video}</span><span class="nbws-tile-label">${_esc(KIND_LABELS.video)}</span></button>
        ${ARTIFACT_KINDS.map(kind => `<button type="button" class="nbws-tile notebook-artifact-gen-btn nbws-tile--${_esc(kind)}"
```

Insert a new button right after the video button, before the `${ARTIFACT_KINDS.map(...)}` line:

```javascript
        <button type="button" class="nbws-tile notebook-report-open-btn nbws-tile--report" id="nbws-report-btn"
                data-kind="report"><span class="nbws-tile-icon">${_KIND_ICONS.report}</span><span class="nbws-tile-label">${_esc(KIND_LABELS.report)}</span></button>
```

- [ ] **Step 3: Wire the tile's click handler**

In `_wireStudioPanel()` (line 1615-1628), find:

```javascript
  document.getElementById('nbws-podcast-btn')?.addEventListener('click', (e) => _generatePodcast(e.currentTarget));
  document.getElementById('nbws-video-btn')?.addEventListener('click', (e) => _generateVideo(e.currentTarget));
```

Add right after:

```javascript
  document.getElementById('nbws-report-btn')?.addEventListener('click', _openReportModal);
```

- [ ] **Step 4: Add the report-modal module section**

Add a new section at the end of the file (after the last function, before any trailing exports/wiring code — check the file's tail first with `tail -30 static/js/notebookWorkspace.js` to place it correctly relative to any module-level initialization):

```javascript
// ---- Reports: "Rapport maken" layout-picker modal --------------------------
//
// "Rapporten" is a separate tile (not part of ARTIFACT_KINDS) because unlike
// every other kind, it needs a configuration step before generating: pick a
// fixed template, an AI-recommended layout, or write a free-text instruction,
// then POST /artifacts with kind="report" + layout_instruction. See
// docs/superpowers/specs/2026-09-01-notebooks-rapporten-design.md.

const _MAGIC_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8"/></svg>';

// "Zelf rapport maken" is client-side only — it never comes from the
// backend, so it is prepended to whatever /report-layouts returns for the
// Indeling grid. instruction: null is the signal _reportCardHtml uses to
// omit the pencil (edit) icon and _openReportEditor uses to start from a
// blank textarea.
const _REPORT_CUSTOM_CARD = {
  title: 'Zelf rapport maken',
  description: 'Maak rapporten volgens jouw wensen door onder meer de structuur, stijl en toon aan te passen.',
  instruction: null,
};

let _reportModalEpoch = 0;
let _reportEscHandler = null;

function _reportCardHtml(item, idx) {
  const editable = item.instruction != null;
  return `
    <button type="button" class="nbrp-card" data-idx="${idx}">
      <div class="nbrp-card-title">${_esc(item.title)}${editable ? _RENAME_ICON : ''}</div>
      <div class="nbrp-card-desc">${_esc(item.description)}</div>
    </button>`;
}

function _wireReportTemplateCard(item, grid, idx) {
  const btn = document.querySelector(`#nbrp-${grid}-grid [data-idx="${idx}"]`);
  if (btn) btn.addEventListener('click', () => _openReportEditor(item));
}

function _reportGridsSkeletonHtml() {
  return `
    <div class="nbrp-section-head">Indeling</div>
    <div class="nbrp-grid" id="nbrp-templates-grid">
      ${_reportCardHtml(_REPORT_CUSTOM_CARD, 0)}
    </div>
    <div class="nbrp-section-head nbrp-recommended-head">${_MAGIC_ICON}Aanbevolen indeling</div>
    <div class="nbrp-grid" id="nbrp-recommended-grid">
      <div class="dashboard-empty">Loading&hellip;</div>
    </div>`;
}

async function _loadReportLayouts(epoch) {
  if (!_state.notebook) return;
  const nbId = _state.notebook.id;
  let data;
  try {
    data = await _fetchJson(`${API_BASE}/api/notebooks/${encodeURIComponent(nbId)}/report-layouts`);
  } catch (e) {
    if (epoch !== _reportModalEpoch) return;
    const recGrid = document.getElementById('nbrp-recommended-grid');
    if (recGrid) recGrid.innerHTML = `<div class="dashboard-empty">Could not load suggestions (${_esc(e.message)})</div>`;
    return;
  }
  if (epoch !== _reportModalEpoch) return;

  const templates = [_REPORT_CUSTOM_CARD, ...(data.templates || [])];
  const templatesGrid = document.getElementById('nbrp-templates-grid');
  if (templatesGrid) {
    templatesGrid.innerHTML = templates.map((item, idx) => _reportCardHtml(item, idx)).join('');
    templates.forEach((item, idx) => _wireReportTemplateCard(item, 'templates', idx));
  }

  const recommended = data.recommended || [];
  const recGrid = document.getElementById('nbrp-recommended-grid');
  if (recGrid) {
    if (!recommended.length) {
      recGrid.innerHTML = '<div class="dashboard-empty">No suggestions yet — add sources to this notebook first.</div>';
    } else {
      recGrid.innerHTML = recommended.map((item, idx) => _reportCardHtml(item, idx)).join('');
      recommended.forEach((item, idx) => _wireReportTemplateCard(item, 'recommended', idx));
    }
  }
}

function _openReportEditor(item) {
  const body = document.getElementById('nbrp-body');
  if (!body) return;
  body.innerHTML = `
    <button type="button" class="nbrp-back" id="nbrp-editor-back">&larr; Terug</button>
    <div class="nbrp-editor-title">${_esc(item.title)}</div>
    <textarea id="nbrp-editor-instruction" class="nbrp-editor-textarea" rows="6"
      placeholder="Beschrijf structuur, stijl en toon in eigen woorden…">${_esc(item.instruction || '')}</textarea>
    <div class="nbrp-editor-error" id="nbrp-editor-error"></div>
    <button type="button" class="dashboard-action-btn nbrp-generate-btn" id="nbrp-generate-btn">Genereer</button>`;
  document.getElementById('nbrp-editor-back')?.addEventListener('click', () => {
    if (!body) return;
    body.innerHTML = _reportGridsSkeletonHtml();
    _wireReportTemplateCard(_REPORT_CUSTOM_CARD, 'templates', 0);
    _loadReportLayouts(_reportModalEpoch);
  });
  document.getElementById('nbrp-generate-btn')?.addEventListener('click', _generateReport);
}

async function _generateReport() {
  if (!_state.notebook) return;
  const btn = document.getElementById('nbrp-generate-btn');
  const errEl = document.getElementById('nbrp-editor-error');
  const textarea = document.getElementById('nbrp-editor-instruction');
  const instruction = textarea ? textarea.value.trim() : '';
  if (errEl) errEl.textContent = '';
  if (btn) { btn.disabled = true; btn.textContent = 'Generating…'; }
  const epoch = _openEpoch;
  const payload = { kind: 'report' };
  if (instruction) payload.layout_instruction = instruction;
  try {
    await _fetchJson(`${API_BASE}/api/notebooks/${encodeURIComponent(_state.notebook.id)}/artifacts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    _closeReportModal();
    if (epoch === _openEpoch) await _loadArtifacts();
  } catch (e) {
    if (errEl) errEl.textContent = `Could not generate (${e.message})`;
    if (btn) { btn.disabled = false; btn.textContent = 'Genereer'; }
  }
}

function _openReportModal() {
  if (!_state.notebook) return;
  const epoch = ++_reportModalEpoch;
  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'nbrp-modal';
  modal.innerHTML = `
    <div class="modal-content nbrp-modal-content" role="dialog" aria-label="Rapport maken">
      <div class="modal-header">
        <h4 style="position:relative;top:-2px;">${_KIND_ICONS.report}Rapport maken</h4>
        <span style="flex:1"></span>
        <button class="close-btn" id="nbrp-close" aria-label="Close">&#10006;</button>
      </div>
      <div class="modal-body nbrp-body" id="nbrp-body">${_reportGridsSkeletonHtml()}</div>
    </div>`;
  document.body.appendChild(modal);

  document.getElementById('nbrp-close')?.addEventListener('click', _closeReportModal);
  modal.addEventListener('click', (e) => { if (e.target === modal) _closeReportModal(); });
  _reportEscHandler = (e) => { if (e.key === 'Escape') _closeReportModal(); };
  document.addEventListener('keydown', _reportEscHandler);
  _wireReportTemplateCard(_REPORT_CUSTOM_CARD, 'templates', 0);

  _loadReportLayouts(epoch);
}

function _closeReportModal() {
  const modal = document.getElementById('nbrp-modal');
  if (modal) modal.remove();
  if (_reportEscHandler) {
    document.removeEventListener('keydown', _reportEscHandler);
    _reportEscHandler = null;
  }
  _reportModalEpoch++;
}
```

- [ ] **Step 5: Syntax-check the file**

Run: `node --check static/js/notebookWorkspace.js`
Expected: no output (success)

- [ ] **Step 6: Commit**

```bash
git add static/js/notebookWorkspace.js
git commit -m "feat(notebooks): Rapporten tile + Rapport maken modal"
```

---

## Task 7: CSS — modal grid, cards, editor

**Files:**
- Modify: `static/style.css` (append near the existing `.nbws-tile*` block, ~line 41151-41217)

**Interfaces:**
- Consumes: existing CSS variables only (`--red`, `--fg`, `--panel`, `--border`, `--accent-warm`, `--color-subheader`, `--input-bg`, `--input-border`).
- Produces: `.nbrp-*` classes consumed by Task 6's JS.

- [ ] **Step 1: Add the CSS block**

In `static/style.css`, find the end of the `.nbws-tile--video { ... }` line (around line 41183) and insert right after it, before the `Web-source search` comment block:

```css
.nbws-tile--report { --nbws-tile-accent: var(--accent-warm); }

/* Rapporten: "Rapport maken" modal — layout-picker grid + one shared
   edit-and-generate step. Existing .modal/.modal-content/.modal-header/
   .modal-body chrome; only the grid/card/editor internals are new. */
.nbrp-modal-content { max-width: 720px; width: 92vw; }
.nbrp-body { display: flex; flex-direction: column; gap: 4px; }
.nbrp-section-head {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-subheader);
  margin: 12px 0 8px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.nbrp-section-head:first-child { margin-top: 0; }
.nbrp-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}
@media (max-width: 640px) {
  .nbrp-grid { grid-template-columns: repeat(2, 1fr); }
}
.nbrp-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--panel);
  color: var(--fg);
  font: inherit;
  text-align: left;
  cursor: pointer;
  min-height: 96px;
}
.nbrp-card:hover {
  background: color-mix(in srgb, var(--red) 8%, var(--panel));
  border-color: color-mix(in srgb, var(--red) 30%, var(--border));
}
.nbrp-card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
}
.nbrp-card-title svg { flex-shrink: 0; opacity: 0.6; }
.nbrp-card-desc {
  font-size: 11px;
  opacity: 0.75;
  line-height: 1.4;
}
.nbrp-back {
  background: none;
  border: none;
  color: var(--fg);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  padding: 0;
  margin-bottom: 10px;
  align-self: flex-start;
}
.nbrp-editor-title { font-size: 14px; font-weight: 600; margin-bottom: 8px; }
.nbrp-editor-textarea {
  width: 100%;
  background: var(--input-bg, var(--panel));
  color: var(--fg);
  border: 1px solid var(--input-border, var(--border));
  border-radius: 8px;
  padding: 10px;
  font: inherit;
  font-size: 13px;
  resize: vertical;
  box-sizing: border-box;
}
.nbrp-editor-error { color: var(--red); font-size: 12px; min-height: 16px; margin: 6px 0; }
.nbrp-generate-btn { align-self: flex-end; }
```

- [ ] **Step 2: Sanity-check no duplicate selectors were introduced**

Run: `grep -c "^\.nbrp-modal-content {" static/style.css`
Expected: `1`

- [ ] **Step 3: Commit**

```bash
git add static/style.css
git commit -m "style(notebooks): CSS for the Rapport maken modal grid/cards"
```

---

## Task 8: Full-suite verification + browser smoke

**Files:** none (verification only)

**Interfaces:** none — this task closes out the plan.

- [ ] **Step 1: Run the full backend test suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all tests pass (no regressions), matching the pre-existing pass count plus the new tests added in Tasks 1-5.

- [ ] **Step 2: Run syntax checks**

Run: `.venv/bin/python -m py_compile core/database.py src/notebook_report_layouts.py src/notebook_artifacts.py src/notebook_report.py routes/notebook_routes.py`
Expected: no output (success)

Run: `node --check static/js/notebookWorkspace.js`
Expected: no output (success)

- [ ] **Step 3: Browser smoke — desktop**

Start an isolated smoke instance per CLAUDE.md's convention (fresh `ITHAKA_DATA_DIR`, port 7001; create the first account via `POST /api/auth/setup`). In the browser:

1. Open a notebook with at least one indexed source (upload a small text file first if the notebook is empty — "Aanbevolen indeling" needs real content to be worth checking).
2. Click the "Rapporten" tile in the Studio panel.
3. Confirm the modal opens with "Indeling" showing "Zelf rapport maken" + 3 templates immediately, and "Aanbevolen indeling" showing a loading state that resolves to up to 4 suggestions.
4. Click a fixed template card (e.g. "Overzichtsdocument") — confirm the editor step opens with the instruction pre-filled and editable.
5. Click "Genereer" — confirm the modal closes, a new "Rapport" (or "Rapporten", per `KIND_LABELS.report`) artifact appears in the Files list, and clicking it opens the report content.
6. Repeat once with "Zelf rapport maken" — confirm the textarea starts empty and generation still succeeds with an empty instruction (falls back to the generic report prompt).
7. Check the browser console for errors throughout.

- [ ] **Step 4: Browser smoke — mobile (360px)**

Resize/emulate to 360px width and repeat steps 2-5 above. Confirm the modal and card grid remain usable (2-column grid per the CSS media query) and no layout breakage occurs.

- [ ] **Step 5: Verify the timeout-exemption question from the spec**

While doing the desktop smoke test, time the `GET /report-layouts` call in the browser's network tab for a notebook with a realistically large source set (near `MAX_CONTEXT_CHARS` = 60,000 chars, per `src/notebook_artifacts.py`). If it consistently completes well under 45 seconds, no further action is needed. If it approaches or exceeds 45 seconds, add a narrow timeout exemption: in `src/notebook_artifacts.py`, extend `ARTIFACTS_GENERATE_PATH_RE`'s usage pattern by adding a second regex + check function for `^/api/notebooks/[^/]+/report-layouts$`, wire it into `app.py` alongside the existing `is_artifacts_generate_request` check (see `app.py:189-197`), and add a regression test mirroring however `is_artifacts_generate_request` is tested today.

- [ ] **Step 6: Paste the full smoke output into the PR description**

Per CLAUDE.md's UI-smoke-test policy: the commands run and their results (not just "tests passed") must be visible before this work is merged.

- [ ] **Step 7: Final commit (if Step 5 required a follow-up change)**

```bash
git add -A
git commit -m "fix(notebooks): add timeout exemption for report-layouts if needed"
```

If no follow-up was needed, skip this step — Task 7's commit is the last one.
