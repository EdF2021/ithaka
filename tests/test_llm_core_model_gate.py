"""Regression test for the local-model concurrency gate (_local_model_slot).

Foreground chat must always win the local model's single generation pipe over
background work (scheduled email/tasks). This exercises the real interleaving:
two concurrent foreground calls plus one background call against a mocked
local endpoint, and asserts:
  (a) the waiting-foreground counter is back to 0 once everything settles
      (no leaked increment/decrement — see the waiting_decremented fix), and
  (b) the background call only acquires the slot after both foreground calls
      have released it.
"""
import asyncio

import pytest

from src import llm_core


FAKE_LOCAL_URL = "http://127.0.0.1:11434/v1/chat/completions"


@pytest.fixture(autouse=True)
def _gate_mocks(monkeypatch):
    """Force the gate on for a fake local endpoint, with no real browser activity."""
    monkeypatch.setattr(llm_core, "_local_model_gate_enabled", lambda: True)
    monkeypatch.setattr(llm_core, "is_local_endpoint", lambda url: url == FAKE_LOCAL_URL)
    # has_foreground_activity is imported fresh inside _local_model_slot on each
    # background call, so patch it at its source module.
    import src.interactive_gate as interactive_gate
    monkeypatch.setattr(interactive_gate, "has_foreground_activity", lambda: False)

    # Reset shared gate state so a prior failing test can't leak into this one.
    llm_core._LOCAL_MODEL_WAITING_FOREGROUND = 0
    llm_core._LOCAL_MODEL_CURRENT.clear()
    yield
    llm_core._LOCAL_MODEL_WAITING_FOREGROUND = 0
    llm_core._LOCAL_MODEL_CURRENT.clear()
    if llm_core._LOCAL_MODEL_LOCK.locked():
        llm_core._LOCAL_MODEL_LOCK.release()


async def test_foreground_calls_take_priority_over_background():
    events: list[str] = []

    async def foreground_call(name: str, hold: float):
        async with llm_core._local_model_slot(FAKE_LOCAL_URL, "test-model", workload="foreground"):
            events.append(f"{name}-start")
            await asyncio.sleep(hold)
            events.append(f"{name}-end")

    async def background_call(name: str):
        async with llm_core._local_model_slot(FAKE_LOCAL_URL, "test-model", workload="background"):
            events.append(f"{name}-start")
            await asyncio.sleep(0)
            events.append(f"{name}-end")

    fg1 = asyncio.create_task(foreground_call("fg1", 0.05))
    fg2 = asyncio.create_task(foreground_call("fg2", 0.05))
    # Let both foreground tasks register their "waiting" claim before the
    # background task starts polling the gate.
    await asyncio.sleep(0)
    bg = asyncio.create_task(background_call("bg"))

    await asyncio.wait_for(asyncio.gather(fg1, fg2, bg), timeout=5)

    # (a) no leaked waiting-foreground count.
    assert llm_core._LOCAL_MODEL_WAITING_FOREGROUND == 0

    # (b) background only starts once both foregrounds have fully released the slot.
    bg_start = events.index("bg-start")
    assert bg_start > events.index("fg1-end")
    assert bg_start > events.index("fg2-end")
    # Stronger priority guarantee: even the second foreground call — queued
    # behind the first on the shared lock — still gets the pipe before background.
    assert events.index("fg2-start") < bg_start


async def test_background_call_alone_acquires_slot_without_gate_delay():
    """Sanity check: with no foreground activity at all, background isn't blocked."""
    events: list[str] = []

    async def background_call(name: str):
        async with llm_core._local_model_slot(FAKE_LOCAL_URL, "test-model", workload="background"):
            events.append(f"{name}-start")
            await asyncio.sleep(0)
            events.append(f"{name}-end")

    await asyncio.wait_for(background_call("bg"), timeout=5)

    assert events == ["bg-start", "bg-end"]
    assert llm_core._LOCAL_MODEL_WAITING_FOREGROUND == 0
