"""services/realtime/realtime_ask.py — one-shot delegation of a Realtime
voice question to the agent loop. See
docs/superpowers/plans/2026-09-04-realtime-voice-tools.md, Task 2."""

import asyncio
import json
import logging

import pytest

import services.realtime.realtime_ask as ask_mod
from services.realtime.realtime_ask import (
    ASK_MAX_CHARS,
    ASK_SYSTEM_PROMPT,
    answer_question,
)


def _sse(obj):
    return "data: " + json.dumps(obj)


def _fake_loop(events, *, capture=None):
    async def _gen(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        for e in events:
            yield e
    return _gen


@pytest.fixture
def candidates(monkeypatch):
    monkeypatch.setattr(ask_mod, "_voice_session_id", lambda owner, url, model: "voice-sess")
    monkeypatch.setattr(
        ask_mod,
        "resolve_task_candidates",
        lambda owner=None: [
            ("http://primary/v1", "kimi-k3", {"Authorization": "Bearer p"}),
            ("http://fallback/v1", "gpt-4o-mini", {}),
        ],
    )


async def test_concatenates_deltas_and_skips_thinking(monkeypatch, candidates):
    captured = {}
    monkeypatch.setattr(ask_mod, "stream_agent_loop", _fake_loop([
        _sse({"delta": "Denk...", "thinking": True}),
        _sse({"type": "tool_start", "tool": "web_search"}),
        _sse({"delta": "Het is "}),
        _sse({"delta": "18 graden."}),
        "data: [DONE]",
    ], capture=captured))

    out = await answer_question("Wat is het weer?", "ed")

    assert out == "Het is 18 graden."
    assert captured["endpoint_url"] == "http://primary/v1"
    assert captured["model"] == "kimi-k3"
    assert captured["headers"] == {"Authorization": "Bearer p"}
    assert captured["fallbacks"] == [("http://fallback/v1", "gpt-4o-mini", {})]
    assert captured["owner"] == "ed"
    assert captured["session_id"] == "voice-sess"
    assert captured["max_rounds"] == ask_mod.ASK_MAX_ROUNDS
    assert "workload" not in captured
    assert captured["messages"][0] == {"role": "system", "content": ASK_SYSTEM_PROMPT}
    assert captured["messages"][1] == {"role": "user", "content": "Wat is het weer?"}


async def test_normalizes_whitespace_and_truncates(monkeypatch, candidates):
    long = "woord " * 600
    monkeypatch.setattr(ask_mod, "stream_agent_loop", _fake_loop([_sse({"delta": long})]))
    out = await answer_question("lang", None)
    assert len(out) <= ASK_MAX_CHARS + 1
    assert out.endswith("…")
    assert "  " not in out


async def test_empty_answer_raises_dutch_runtime_error(monkeypatch, candidates):
    monkeypatch.setattr(ask_mod, "stream_agent_loop", _fake_loop([_sse({"type": "metrics"})]))
    with pytest.raises(RuntimeError, match="Ithaka gaf geen antwoord"):
        await answer_question("x", None)


async def test_timeout_raises_dutch_runtime_error(monkeypatch, candidates):
    async def _slow(**kwargs):
        await asyncio.sleep(5)
        yield _sse({"delta": "te laat"})
    monkeypatch.setattr(ask_mod, "stream_agent_loop", _slow)
    monkeypatch.setattr(ask_mod, "ASK_TIMEOUT_S", 0.05)
    with pytest.raises(RuntimeError, match="duurde te lang"):
        await answer_question("x", None)


async def test_no_candidates_raises_value_error(monkeypatch):
    monkeypatch.setattr(ask_mod, "resolve_task_candidates", lambda owner=None: [])
    with pytest.raises(ValueError, match="Geen model"):
        await answer_question("x", None)


async def test_blank_question_raises_value_error(candidates):
    with pytest.raises(ValueError, match="Lege vraag"):
        await answer_question("   ", None)


async def test_tool_start_is_logged(monkeypatch, candidates, caplog):
    # I2: no visible tool trail exists anywhere else for the Realtime voice
    # path (answer_question drops tool_start/tool_output from the spoken
    # answer) — this log line is the only server-side record of which tools
    # a voice-triggered agent loop ran.
    monkeypatch.setattr(ask_mod, "stream_agent_loop", _fake_loop([
        _sse({"type": "tool_start", "tool": "web_search"}),
        _sse({"delta": "Het is 18 graden."}),
    ]))
    with caplog.at_level(logging.INFO, logger="services.realtime.realtime_ask"):
        out = await answer_question("Wat is het weer?", "ed")

    assert out == "Het is 18 graden."
    assert any(
        "tool_start" in r.getMessage() and "owner=ed" in r.getMessage() and "tool=web_search" in r.getMessage()
        for r in caplog.records
    )


async def test_passes_explicit_relevant_tools_so_loop_never_takes_direct_path(monkeypatch, candidates):
    # Prod 2026-09-04: a one-turn Dutch question is "low-signal" for the
    # English-only classifier; without caller-provided tools the loop replies
    # directly and ask_ithaka never reaches web_search/email/calendar.
    captured = {}
    monkeypatch.setattr(ask_mod, "stream_agent_loop", _fake_loop([_sse({"delta": "ok"})], capture=captured))

    class _Idx:
        def get_tools_for_query(self, query, k=8):
            assert query == "Stuur een mail naar Jan"
            return {"send_email", "resolve_contact", "manage_memory"}

    monkeypatch.setattr(ask_mod, "get_tool_index", lambda: _Idx())

    await answer_question("Stuur een mail naar Jan", "ed")

    tools = captured["relevant_tools"]
    assert tools, "relevant_tools must be non-empty to bypass the direct low-signal path"
    assert ask_mod.ASK_BASE_TOOLS <= tools
    assert {"send_email", "resolve_contact", "web_search", "web_fetch", "manage_calendar", "create_document"} <= tools


async def test_tool_index_failure_falls_back_to_base_tools(monkeypatch, candidates):
    captured = {}
    monkeypatch.setattr(ask_mod, "stream_agent_loop", _fake_loop([_sse({"delta": "ok"})], capture=captured))

    def _boom():
        raise RuntimeError("chroma down")

    monkeypatch.setattr(ask_mod, "get_tool_index", _boom)

    await answer_question("Wat is het weer?", None)

    assert captured["relevant_tools"] == set(ask_mod.ASK_BASE_TOOLS)


class _FakeQuery:
    def __init__(self, existing):
        self._existing = existing

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._existing


class _FakeDb:
    def __init__(self, existing=None):
        self._existing = existing
        self.added = []
        self.committed = False

    def query(self, *a, **k):
        return _FakeQuery(self._existing)

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.committed = True

    def close(self):
        pass


def test_voice_session_reuses_existing_session(monkeypatch):
    import src.database as dbmod
    existing = type("Row", (), {"id": "sess-existing"})()
    db = _FakeDb(existing)
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: db)
    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: None)

    assert ask_mod._voice_session_id("ed", "http://x/v1", "m") == "sess-existing"
    assert db.added == []


def test_voice_session_created_once_with_name_owner_and_folder(monkeypatch):
    import src.database as dbmod
    db = _FakeDb(None)
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: db)
    ensured = {}

    class _Mgr:
        def ensure_task_session(self, sid, name, url, model, owner=None, **kw):
            ensured.update(sid=sid, name=name, owner=owner)

    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: _Mgr())

    sid = ask_mod._voice_session_id("ed", "http://x/v1", "m")

    # With a live SessionManager the manager inserts the row (and primes its
    # cache) — a second direct insert would hit UNIQUE(sessions.id).
    assert sid and db.added == [] and not db.committed
    assert ensured == {"sid": sid, "name": ask_mod.VOICE_SESSION_NAME, "owner": "ed"}


def test_voice_session_created_directly_without_session_manager(monkeypatch):
    import src.database as dbmod
    db = _FakeDb(None)
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: db)
    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: None)

    sid = ask_mod._voice_session_id("ed", "http://x/v1", "m")

    assert sid and db.committed
    row = db.added[0]
    assert (row.id, row.name, row.owner, row.folder) == (sid, ask_mod.VOICE_SESSION_NAME, "ed", ask_mod.VOICE_SESSION_FOLDER)


def test_voice_session_db_failure_degrades_to_none(monkeypatch):
    import src.database as dbmod

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(dbmod, "SessionLocal", _boom)
    assert ask_mod._voice_session_id("ed", "http://x/v1", "m") is None


async def test_collect_passes_voice_session_id(monkeypatch, candidates):
    captured = {}
    monkeypatch.setattr(ask_mod, "stream_agent_loop", _fake_loop([_sse({"delta": "ok"})], capture=captured))
    monkeypatch.setattr(ask_mod, "_voice_session_id", lambda owner, url, model: "voice-1")
    await answer_question("Maak een document over fotosynthese", "ed")
    assert captured["session_id"] == "voice-1"
