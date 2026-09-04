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
    assert captured["session_id"] is None
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
