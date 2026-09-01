"""NotebookArtifact model: roundtrip + cascade behavior.

Uses the suite's documented convention for a real, isolated DB
(tests.helpers.sqlite_db.make_temp_sqlite, see tests/TESTING_STANDARD.md and
tests/README.md) rather than the shared core.database.engine/SessionLocal —
see tests/test_services_notebooks_db.py for the rationale (the shared engine
lives for the whole pytest session; committing real rows there would leak
into every other test).

core.database registers a global `PRAGMA foreign_keys=ON` listener on the
SQLAlchemy Engine class (core/database.py, set_sqlite_pragma), so FK-level
ON DELETE CASCADE is enforced on this module's own sqlite engine too, once
core.database has been imported (it is, via the `import core.database as db`
below).

Fixtures (_TS/_ENGINE/_TMPDB, make_notebook, make_document) are kept at
module level / as plain helper functions so a later task in this feature can
extend this file with artifact-service tests against the same schema.
"""
import uuid

import core.database as db
from tests.helpers.sqlite_db import make_temp_sqlite

_TS, _ENGINE, _TMPDB = make_temp_sqlite(db.Base.metadata)


def make_notebook(session, owner="ed", name="Thesis"):
    nb = db.Notebook(id=str(uuid.uuid4()), owner=owner, name=name)
    session.add(nb)
    session.commit()
    return nb


def make_document(session, title="Report", owner="ed", content=""):
    doc = db.Document(id=str(uuid.uuid4()), title=title, owner=owner,
                      current_content=content)
    session.add(doc)
    session.commit()
    return doc


def make_source(session, notebook, filename="a.txt", content="inhoud",
                status="indexed", with_document=True, owner="ed"):
    """Attach a NotebookSource (plus its backing Document) to `notebook`."""
    doc_id = None
    if with_document:
        doc_id = make_document(session, title=filename, owner=owner,
                               content=content).id
    src = db.NotebookSource(id=str(uuid.uuid4()), notebook_id=notebook.id,
                            document_id=doc_id, filename=filename,
                            status=status, chunk_count=1)
    session.add(src)
    session.commit()
    return src


def test_notebook_artifact_roundtrip():
    s = _TS()
    try:
        nb = make_notebook(s)
        doc = make_document(s)
        art = db.NotebookArtifact(id="a1", notebook_id=nb.id, document_id=doc.id, kind="faq")
        s.add(art)
        s.commit()
        d = art.to_dict()
        assert d["kind"] == "faq"
        assert d["document_id"] == doc.id
        assert d["notebook_id"] == nb.id
        assert d["id"] == "a1"
        assert "created_at" in d
        assert "updated_at" in d
    finally:
        s.close()


def test_notebook_artifact_title_defaults_to_none():
    s = _TS()
    try:
        nb = make_notebook(s)
        doc = make_document(s)
        art = db.NotebookArtifact(id="a-title-none", notebook_id=nb.id, document_id=doc.id, kind="faq")
        s.add(art)
        s.commit()
        d = art.to_dict()
        assert d["title"] is None
    finally:
        s.close()


def test_notebook_artifact_title_roundtrip():
    s = _TS()
    try:
        nb = make_notebook(s)
        doc = make_document(s)
        art = db.NotebookArtifact(id="a-title-set", notebook_id=nb.id, document_id=doc.id,
                                  kind="faq", title="Eigen titel")
        s.add(art)
        s.commit()
        reloaded = s.query(db.NotebookArtifact).filter_by(id="a-title-set").one()
        assert reloaded.title == "Eigen titel"
        assert reloaded.to_dict()["title"] == "Eigen titel"
    finally:
        s.close()


def test_artifact_cascade_on_notebook_delete():
    s = _TS()
    try:
        nb = make_notebook(s)
        doc = make_document(s)
        art = db.NotebookArtifact(id=str(uuid.uuid4()), notebook_id=nb.id,
                                  document_id=doc.id, kind="summary")
        s.add(art)
        s.commit()
        s.refresh(nb)
        assert len(nb.artifacts) == 1

        s.delete(nb)
        s.commit()

        assert s.query(db.NotebookArtifact).filter_by(notebook_id=nb.id).count() == 0
    finally:
        s.close()


def test_migrate_add_notebook_artifact_title_column(tmp_path, monkeypatch):
    """Mirrors test_migrate_add_notebook_artifact_audio_path_column in
    tests/test_services_notebook_audio_model.py — same tmp_path + raw
    sqlite3 table_info convention (see that test's module docstring for the
    precedent this follows: tests/test_session_search.py's FTS migration
    test)."""
    import sqlite3

    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE notebook_artifacts (
            id TEXT PRIMARY KEY,
            notebook_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            audio_path VARCHAR,
            created_at DATETIME,
            updated_at DATETIME
        );
        INSERT INTO notebook_artifacts(id, notebook_id, document_id, kind)
        VALUES ('a1', 'n1', 'd1', 'faq');
        """
    )
    conn.close()

    monkeypatch.setattr(db, "DATABASE_URL", f"sqlite:///{db_path}")

    conn = sqlite3.connect(db_path)
    try:
        columns_before = [row[1] for row in conn.execute("PRAGMA table_info(notebook_artifacts)")]
    finally:
        conn.close()
    assert "title" not in columns_before

    db._migrate_add_notebook_artifact_title_column()

    conn = sqlite3.connect(db_path)
    try:
        columns_after = [row[1] for row in conn.execute("PRAGMA table_info(notebook_artifacts)")]
        assert "title" in columns_after

        row = conn.execute("SELECT title FROM notebook_artifacts WHERE id = 'a1'").fetchone()
        assert row == (None,)
    finally:
        conn.close()

    # Idempotent: running it again on an already-migrated DB must not raise.
    db._migrate_add_notebook_artifact_title_column()


def test_migrate_add_notebook_artifact_title_column_missing_db_is_noop(tmp_path, monkeypatch):
    missing_path = tmp_path / "does-not-exist.db"
    monkeypatch.setattr(db, "DATABASE_URL", f"sqlite:///{missing_path}")

    # No DB file at all yet (fresh install) — must not raise.
    db._migrate_add_notebook_artifact_title_column()


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


def test_artifact_cascade_on_document_delete():
    s = _TS()
    try:
        nb = make_notebook(s)
        doc = make_document(s)
        art = db.NotebookArtifact(id=str(uuid.uuid4()), notebook_id=nb.id,
                                  document_id=doc.id, kind="podcast")
        s.add(art)
        s.commit()
        artifact_id = art.id

        # DB-level ON DELETE CASCADE (not ORM cascade): delete the Document
        # row directly, bypassing the Notebook relationship.
        s.delete(doc)
        s.commit()

        assert s.query(db.NotebookArtifact).filter_by(id=artifact_id).count() == 0
    finally:
        s.close()


# --------------------------------------------------------------------------
# Artifact generation service (src/notebook_artifacts.py)
#
# The LLM call is always monkeypatched: these tests are hermetic and never
# reach an endpoint. fire_event is stubbed too - the real one schedules a
# background task on the running loop, which would drag the shared task
# machinery into an isolated-DB test.
# --------------------------------------------------------------------------
import pytest

import src.notebook_artifacts as artifacts


class _FakeLLM:
    """Async stand-in for task_llm_call_async that records its arguments."""

    def __init__(self, result="# Gegenereerd", exc=None):
        self.result = result
        self.exc = exc
        self.messages = None
        self.kwargs = None
        self.calls = 0

    async def __call__(self, messages, **kwargs):
        self.calls += 1
        self.messages = messages
        self.kwargs = kwargs
        if self.exc is not None:
            raise self.exc
        return self.result


def _patch_llm(monkeypatch, fake):
    monkeypatch.setattr(artifacts, "task_llm_call_async", fake)
    monkeypatch.setattr(artifacts, "fire_event", lambda *a, **k: None)
    return fake


def _system_content(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "system")


def _user_content(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "user")


# --- registry -------------------------------------------------------------

def test_artifact_kinds_registry_complete():
    assert set(artifacts.ARTIFACT_KINDS) == {
        "study_guide", "briefing", "faq", "quiz", "mindmap", "infographic",
        "flashcards", "data_table", "slide_deck",
    }
    labels = {k: v["label"] for k, v in artifacts.ARTIFACT_KINDS.items()}
    assert labels == {
        "study_guide": "Studiegids", "briefing": "Briefing", "faq": "FAQ",
        "quiz": "Quiz", "mindmap": "Mindmap", "infographic": "Infographic",
        "flashcards": "Flashcards", "data_table": "Gegevenstabel",
        "slide_deck": "Diapresentatie",
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


def test_mindmap_prompt_requires_single_mermaid_fence():
    prompt = artifacts.ARTIFACT_KINDS["mindmap"]["prompt"]
    assert "mermaid" in prompt
    assert "mindmap" in prompt


# --- gather_source_text ---------------------------------------------------

def test_gather_skips_failed_and_docless():
    s = _TS()
    try:
        nb = make_notebook(s, name="Bronnen")
        make_source(s, nb, filename="goed.txt", content="GOEDE INHOUD")
        make_source(s, nb, filename="stuk.txt", content="MISLUKT",
                    status="failed")
        make_source(s, nb, filename="leeg.txt", with_document=False)

        text = artifacts.gather_source_text(nb, s)

        assert "=== BRON: goed.txt ===" in text
        assert "GOEDE INHOUD" in text
        assert "stuk.txt" not in text
        assert "MISLUKT" not in text
        assert "leeg.txt" not in text
    finally:
        s.close()


def test_gather_returns_empty_without_sources():
    s = _TS()
    try:
        nb = make_notebook(s, name="Leeg")
        assert artifacts.gather_source_text(nb, s) == ""
    finally:
        s.close()


def test_gather_cap_proportional():
    """Two equally oversized sources: both exceed their fair share, so both
    get truncated with the marker, headers stay intact, and the total stays
    within budget."""
    s = _TS()
    try:
        nb = make_notebook(s, name="Groot")
        make_source(s, nb, filename="a.txt", content="a" * 50_000)
        make_source(s, nb, filename="b.txt", content="b" * 50_000)

        text = artifacts.gather_source_text(nb, s)

        assert len(text) <= artifacts.MAX_CONTEXT_CHARS
        blocks = text.split("\n\n")
        assert len(blocks) == 2
        for block in blocks:
            assert block.endswith("(bron ingekort)")
        assert blocks[0].startswith("=== BRON: a.txt ===\n")
        assert blocks[1].startswith("=== BRON: b.txt ===\n")
        # Proportional: two equally sized sources keep equally sized blocks.
        assert abs(len(blocks[0]) - len(blocks[1])) <= 1
        # ... and each keeps a real share instead of being cut to nothing.
        assert len(blocks[0]) > 20_000
    finally:
        s.close()


def test_gather_keeps_small_sources_intact():
    s = _TS()
    try:
        nb = make_notebook(s, name="Klein")
        make_source(s, nb, filename="a.txt", content="korte tekst")

        text = artifacts.gather_source_text(nb, s)

        assert "(bron ingekort)" not in text
        assert text.endswith("korte tekst")
    finally:
        s.close()


def test_gather_cap_only_truncates_oversized_source():
    """F2: a small source that fits its fair share stays complete and
    unmarked; only the source that overflows the remaining budget is cut."""
    s = _TS()
    try:
        nb = make_notebook(s, name="Gemengd")
        make_source(s, nb, filename="klein.txt", content="k" * 5_000)
        make_source(s, nb, filename="groot.txt", content="g" * 100_000)

        text = artifacts.gather_source_text(nb, s)

        assert len(text) <= artifacts.MAX_CONTEXT_CHARS
        blocks = text.split("\n\n")
        assert len(blocks) == 2
        klein_block, groot_block = blocks

        assert klein_block.startswith("=== BRON: klein.txt ===\n")
        assert klein_block.endswith("k" * 5_000)
        assert "(bron ingekort)" not in klein_block

        assert groot_block.startswith("=== BRON: groot.txt ===\n")
        assert groot_block.endswith("(bron ingekort)")
        # Truncated, not the full 100k source.
        assert "g" * 100_000 not in groot_block
    finally:
        s.close()


def test_gather_cap_overhead_at_or_above_budget_does_not_crash(monkeypatch):
    """F3: when the fixed overhead alone meets/exceeds MAX_CONTEXT_CHARS, the
    function must degrade sanely instead of producing negative slice lengths
    or raising."""
    s = _TS()
    try:
        nb = make_notebook(s, name="Overhead")
        make_source(s, nb, filename="a-rather-long-filename.txt",
                    content="inhoud die er niet toe doet")

        monkeypatch.setattr(artifacts, "MAX_CONTEXT_CHARS", 5)

        text = artifacts.gather_source_text(nb, s)

        assert isinstance(text, str)
    finally:
        s.close()


# --- generate_artifact ----------------------------------------------------

async def test_generate_creates_document_and_row(monkeypatch):
    s = _TS()
    try:
        _patch_llm(monkeypatch, _FakeLLM())
        nb = make_notebook(s, owner="own", name="Testboek")
        make_source(s, nb, filename="a.txt", content="brontekst", owner="own")

        art = await artifacts.generate_artifact(nb.id, "own", "faq", s)

        assert art.kind == "faq"
        assert art.notebook_id == nb.id
        # The artifact gets its own (renamable) title, seeded from the same
        # value as the Document's — not left NULL to only ever fall back.
        assert art.title == "Testboek — FAQ"
        doc = s.get(db.Document, art.document_id)
        assert doc.title == "Testboek — FAQ"
        assert doc.owner == "own"
        assert doc.current_content == "# Gegenereerd"
        assert doc.session_id is None
        assert doc.language == "markdown"
    finally:
        s.close()


async def test_prompt_contains_source_blocks_and_kind_prompt(monkeypatch):
    s = _TS()
    try:
        fake = _patch_llm(monkeypatch, _FakeLLM())
        nb = make_notebook(s, owner="own", name="Testboek")
        make_source(s, nb, filename="a.txt", content="brontekst", owner="own")

        await artifacts.generate_artifact(nb.id, "own", "faq", s)

        assert artifacts.ARTIFACT_KINDS["faq"]["prompt"] in _system_content(fake.messages)
        user = _user_content(fake.messages)
        assert "=== BRON: a.txt ===" in user
        assert "brontekst" in user
        # Source text is untrusted input and must stay inside the guard block.
        assert "<<<UNTRUSTED_SOURCE_DATA>>>" in user
        assert fake.kwargs.get("owner") == "own"
        # wait_for_quiet=False skips the interactive-quiet gate (that wait
        # would block on this very request's own tracked-request entry).
        # workload="foreground" tells the local-model slot in llm_core.py
        # this is a synchronous in-request call, not a genuine background
        # job - without it the call defaults to "background" and deadlocks
        # a second time on has_foreground_activity() for its own lifetime.
        assert fake.kwargs.get("wait_for_quiet") is False
        assert fake.kwargs.get("workload") == "foreground"
    finally:
        s.close()


async def test_generate_fires_document_created_after_commit(monkeypatch):
    s = _TS()
    try:
        monkeypatch.setattr(artifacts, "task_llm_call_async", _FakeLLM())
        fired = []
        monkeypatch.setattr(artifacts, "fire_event",
                            lambda name, owner=None: fired.append((name, owner)))
        nb = make_notebook(s, owner="own", name="Testboek")
        make_source(s, nb, filename="a.txt", content="brontekst", owner="own")

        await artifacts.generate_artifact(nb.id, "own", "briefing", s)

        assert fired == [("document_created", "own")]
    finally:
        s.close()


async def test_llm_failure_leaves_no_rows(monkeypatch):
    s = _TS()
    try:
        _patch_llm(monkeypatch, _FakeLLM(exc=RuntimeError("endpoint down")))
        nb = make_notebook(s, owner="own", name="Testboek")
        make_source(s, nb, filename="a.txt", content="brontekst", owner="own")
        docs_before = s.query(db.Document).count()

        with pytest.raises(RuntimeError):
            await artifacts.generate_artifact(nb.id, "own", "quiz", s)

        assert s.query(db.NotebookArtifact).filter_by(notebook_id=nb.id).count() == 0
        assert s.query(db.Document).count() == docs_before
    finally:
        s.close()


async def test_empty_llm_answer_leaves_no_rows(monkeypatch):
    s = _TS()
    try:
        _patch_llm(monkeypatch, _FakeLLM(result="   "))
        nb = make_notebook(s, owner="own", name="Testboek")
        make_source(s, nb, filename="a.txt", content="brontekst", owner="own")
        docs_before = s.query(db.Document).count()

        with pytest.raises(RuntimeError):
            await artifacts.generate_artifact(nb.id, "own", "quiz", s)

        assert s.query(db.NotebookArtifact).filter_by(notebook_id=nb.id).count() == 0
        assert s.query(db.Document).count() == docs_before
    finally:
        s.close()


async def test_think_block_stripped_from_saved_content(monkeypatch):
    """F1: reasoning-model <think> blocks must never land in the saved
    artifact - they are stripped before the empty-answer check and before
    saving."""
    s = _TS()
    try:
        _patch_llm(monkeypatch, _FakeLLM(
            result="<think>redenering</think>\n# Studiegids"))
        nb = make_notebook(s, owner="own", name="Testboek")
        make_source(s, nb, filename="a.txt", content="brontekst", owner="own")

        art = await artifacts.generate_artifact(nb.id, "own", "study_guide", s)

        doc = s.get(db.Document, art.document_id)
        assert doc.current_content == "# Studiegids"
        assert "redenering" not in doc.current_content
        assert "<think>" not in doc.current_content
    finally:
        s.close()


async def test_think_only_answer_counts_as_empty(monkeypatch):
    """F1: an answer that is only a think block has nothing left after
    stripping, so it must raise RuntimeError and leave no rows behind."""
    s = _TS()
    try:
        _patch_llm(monkeypatch, _FakeLLM(
            result="<think>alleen redenering, geen inhoud</think>"))
        nb = make_notebook(s, owner="own", name="Testboek")
        make_source(s, nb, filename="a.txt", content="brontekst", owner="own")
        docs_before = s.query(db.Document).count()

        with pytest.raises(RuntimeError):
            await artifacts.generate_artifact(nb.id, "own", "faq", s)

        assert s.query(db.NotebookArtifact).filter_by(notebook_id=nb.id).count() == 0
        assert s.query(db.Document).count() == docs_before
    finally:
        s.close()


async def test_no_sources_raises(monkeypatch):
    s = _TS()
    try:
        fake = _patch_llm(monkeypatch, _FakeLLM())
        nb = make_notebook(s, owner="own", name="Leeg")

        with pytest.raises(ValueError):
            await artifacts.generate_artifact(nb.id, "own", "faq", s)

        assert fake.calls == 0
    finally:
        s.close()


async def test_unknown_kind_raises(monkeypatch):
    s = _TS()
    try:
        fake = _patch_llm(monkeypatch, _FakeLLM())
        nb = make_notebook(s, owner="own", name="Testboek")
        make_source(s, nb, filename="a.txt", content="brontekst", owner="own")

        with pytest.raises(ValueError):
            await artifacts.generate_artifact(nb.id, "own", "podcast", s)

        assert fake.calls == 0
    finally:
        s.close()


async def test_other_owner_raises(monkeypatch):
    s = _TS()
    try:
        fake = _patch_llm(monkeypatch, _FakeLLM())
        nb = make_notebook(s, owner="own", name="Testboek")
        make_source(s, nb, filename="a.txt", content="brontekst", owner="own")

        with pytest.raises(ValueError):
            await artifacts.generate_artifact(nb.id, "iemand-anders", "faq", s)

        assert fake.calls == 0
    finally:
        s.close()


async def test_regenerate_creates_second_artifact(monkeypatch):
    s = _TS()
    try:
        _patch_llm(monkeypatch, _FakeLLM())
        nb = make_notebook(s, owner="own", name="Testboek")
        make_source(s, nb, filename="a.txt", content="brontekst", owner="own")

        first = await artifacts.generate_artifact(nb.id, "own", "faq", s)
        second = await artifacts.generate_artifact(nb.id, "own", "faq", s)

        assert first.id != second.id
        assert first.document_id != second.document_id
        assert s.query(db.NotebookArtifact).filter_by(notebook_id=nb.id).count() == 2
    finally:
        s.close()


# --------------------------------------------------------------------------
# Fase 2 fix-wave regression: the gate-bypass seam that the tests above mock
# away by monkeypatching artifacts.task_llm_call_async directly. The Critical
# review finding was in task_llm_call_async itself (src/task_endpoint.py):
# it unconditionally awaited wait_for_interactive_quiet, but the
# artifacts-POST request that calls generate_artifact is itself tracked as
# foreground activity (app.py's _InteractiveActivityMiddleware /
# src.interactive_gate.track_interactive_request), so the wait could never
# see foreground activity go to zero and hung forever
# (BACKGROUND_TASK_MAX_WAIT_SECONDS defaults to 0 = wait indefinitely). The
# fix is generate_artifact passing wait_for_quiet=False. This test exercises
# the *real* task_llm_call_async (only its two network-reaching dependencies,
# resolve_task_candidates and llm_call_async_with_fallback, are faked) so the
# bypass is actually proven end-to-end rather than assumed.
# --------------------------------------------------------------------------
import asyncio

import src.task_endpoint as task_endpoint
from src.interactive_gate import track_interactive_request


async def test_generate_artifact_does_not_deadlock_against_own_foreground_request(monkeypatch):
    def _fake_candidates(**kwargs):
        return [("http://fake-endpoint.invalid", "fake-model", {})]

    async def _fake_fallback(candidates, messages, **kwargs):
        return "# Gegenereerd"

    monkeypatch.setattr(task_endpoint, "resolve_task_candidates", _fake_candidates)
    monkeypatch.setattr(task_endpoint, "llm_call_async_with_fallback", _fake_fallback)
    monkeypatch.setattr(artifacts, "fire_event", lambda *a, **k: None)

    s = _TS()
    try:
        nb = make_notebook(s, owner="own", name="Testboek")
        make_source(s, nb, filename="a.txt", content="brontekst", owner="own")

        async def _run():
            # Mirrors what _InteractiveActivityMiddleware does for the real
            # artifacts-POST request: this generate_artifact call happens
            # *while* the request is tracked as active foreground traffic.
            async with track_interactive_request("/api/notebooks/x/artifacts", "POST"):
                return await artifacts.generate_artifact(nb.id, "own", "faq", s)

        # A short deadline: if the gate-bypass regresses, this call hangs
        # forever (the default max-wait is 0 = indefinite), so wait_for turns
        # that hang into a clean test failure instead of a stuck test run.
        art = await asyncio.wait_for(_run(), timeout=5)

        assert art.kind == "faq"
        assert s.query(db.NotebookArtifact).filter_by(id=art.id).count() == 1
    finally:
        s.close()


# --------------------------------------------------------------------------
# Timeout-exemption predicate (src/notebook_artifacts.is_artifacts_generate_request)
#
# Lives here rather than in app.py so it is importable and unit-testable
# without pulling in the full app module (app.py wires ~40 routers, reads
# .env, and touches disk at import time).
# --------------------------------------------------------------------------

def test_artifacts_generate_predicate_matches_post_only():
    assert artifacts.is_artifacts_generate_request("POST", "/api/notebooks/xyz/artifacts") is True
    assert artifacts.is_artifacts_generate_request("post", "/api/notebooks/xyz/artifacts") is True


def test_artifacts_generate_predicate_rejects_other_methods():
    assert artifacts.is_artifacts_generate_request("GET", "/api/notebooks/xyz/artifacts") is False
    assert artifacts.is_artifacts_generate_request("DELETE", "/api/notebooks/xyz/artifacts") is False
    assert artifacts.is_artifacts_generate_request("PUT", "/api/notebooks/xyz/artifacts") is False


def test_artifacts_generate_predicate_rejects_other_paths():
    # Sources upload/ingest must keep the hard timeout.
    assert artifacts.is_artifacts_generate_request("POST", "/api/notebooks/xyz/sources") is False
    # A sub-path (e.g. a future /artifacts/{id} POST) must not match either.
    assert artifacts.is_artifacts_generate_request("POST", "/api/notebooks/xyz/artifacts/abc") is False
    # Bare prefix without a notebook id segment.
    assert artifacts.is_artifacts_generate_request("POST", "/api/notebooks/artifacts") is False
    assert artifacts.is_artifacts_generate_request("POST", "/api/notebooks//artifacts") is False
