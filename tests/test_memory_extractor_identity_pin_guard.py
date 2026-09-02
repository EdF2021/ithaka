"""Regression for #101: a single low-signal message (a test greeting) got
auto-extracted as an identity fact and pinned as a permanent "core fact"
injected into every future turn (`src/chat_processor.py` includes every
`pinned` memory unconditionally).

fdb91dd already added a name-vs-name conflict check, but (a) its name
matcher only understood "name is X" word order and missed the paraphrase the
extraction LLM actually produced for the issue's exact repro, and (b) there
was no guard at all for the *no-conflict* case — a bare greeting with no
existing identity memory would still get auto-pinned.

This covers the full path through `extract_and_store`, mirroring the mocking
style of test_memory_extractor_vector_degraded.py: a real MemoryManager on a
temp dir, and `src.llm_core.llm_call_async` monkeypatched to return a fixed
extraction result instead of calling a real model.
"""

import asyncio
import tempfile

import src.llm_core
import src.event_bus
from src.memory import MemoryManager
from services.memory.memory_extractor import extract_and_store


class _FakeSession:
    owner = "alice"
    session_id = "sess-1"

    def __init__(self, messages):
        self._messages = messages

    def get_context_messages(self):
        return self._messages


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _patch_llm(monkeypatch, facts_json):
    async def _fake_llm(url, model, messages, **kwargs):
        return facts_json

    monkeypatch.setattr(src.llm_core, "llm_call_async", _fake_llm)
    monkeypatch.setattr(src.event_bus, "fire_event", lambda *a, **k: None)


def test_greeting_with_a_name_is_stored_but_not_pinned(monkeypatch, caplog):
    """Issue #101's exact repro: 'Goedemiddag Thiermen Naaij.' must not
    become a pinned core fact, even with no conflicting pinned identity
    memory yet."""
    facts_json = '[{"text": "Thiermen Naaij is the user\'s full name", "category": "identity"}]'
    _patch_llm(monkeypatch, facts_json)

    session = _FakeSession([
        {"role": "user", "content": "Goedemiddag Thiermen Naaij."},
        {"role": "assistant", "content": "Goedemiddag!"},
    ])

    with tempfile.TemporaryDirectory() as data_dir:
        mgr = MemoryManager(data_dir)
        with caplog.at_level("WARNING"):
            _run(extract_and_store(session, mgr, None, endpoint_url="http://x", model="m"))

        stored = mgr.load(owner="alice")
        matches = [e for e in stored if "Thiermen Naaij" in e["text"]]
        assert len(matches) == 1, f"expected the fact to be stored, got {stored}"
        assert not matches[0].get("pinned"), (
            "a greeting with a name in vocative position must not be auto-pinned"
        )


def test_self_statement_with_no_conflict_is_pinned(monkeypatch):
    """A genuine self-statement ('My name is Ed') with no existing
    conflicting pinned identity memory should still auto-pin, unchanged
    from prior behavior."""
    facts_json = '[{"text": "User\'s name is Ed.", "category": "identity"}]'
    _patch_llm(monkeypatch, facts_json)

    session = _FakeSession([
        {"role": "user", "content": "My name is Ed."},
        {"role": "assistant", "content": "Nice to meet you, Ed."},
    ])

    with tempfile.TemporaryDirectory() as data_dir:
        mgr = MemoryManager(data_dir)
        _run(extract_and_store(session, mgr, None, endpoint_url="http://x", model="m"))

        stored = mgr.load(owner="alice")
        matches = [e for e in stored if "Ed" in e["text"]]
        assert len(matches) == 1
        assert matches[0].get("pinned") is True


def test_dutch_self_statement_with_no_conflict_is_pinned(monkeypatch):
    facts_json = '[{"text": "User\'s name is Ed.", "category": "identity"}]'
    _patch_llm(monkeypatch, facts_json)

    session = _FakeSession([
        {"role": "user", "content": "Ik ben Ed."},
        {"role": "assistant", "content": "Leuk je te ontmoeten, Ed."},
    ])

    with tempfile.TemporaryDirectory() as data_dir:
        mgr = MemoryManager(data_dir)
        _run(extract_and_store(session, mgr, None, endpoint_url="http://x", model="m"))

        stored = mgr.load(owner="alice")
        matches = [e for e in stored if "Ed" in e["text"]]
        assert len(matches) == 1
        assert matches[0].get("pinned") is True


def test_conflicting_self_stated_name_is_stored_unpinned_with_warning(monkeypatch, caplog):
    """A second, different self-stated name conflicts with an existing
    pinned identity memory ('User's name is Ed') -> stored, not pinned, and
    a conflict warning is logged (option (c) from the issue)."""
    facts_json = '[{"text": "Thiermen Naaij is the user\'s full name", "category": "identity"}]'
    _patch_llm(monkeypatch, facts_json)

    session = _FakeSession([
        {"role": "user", "content": "Ik ben Thiermen Naaij."},
        {"role": "assistant", "content": "Hoi Thiermen!"},
    ])

    with tempfile.TemporaryDirectory() as data_dir:
        mgr = MemoryManager(data_dir)
        existing = mgr.add_entry(
            "User's name is Ed.", source="auto", category="identity", owner="alice"
        )
        existing["pinned"] = True
        mgr.save([existing])

        with caplog.at_level("WARNING"):
            _run(extract_and_store(session, mgr, None, endpoint_url="http://x", model="m"))

        stored = mgr.load(owner="alice")
        matches = [e for e in stored if "Thiermen Naaij" in e["text"]]
        assert len(matches) == 1, f"expected the conflicting fact to still be stored, got {stored}"
        assert not matches[0].get("pinned"), (
            "a name that conflicts with an existing pinned identity fact must not be auto-pinned"
        )
        assert any("conflict" in rec.message.lower() for rec in caplog.records), (
            "expected a conflict warning to be logged"
        )
        # The pre-existing pinned name must survive unchanged.
        ed_entry = next(e for e in stored if e["id"] == existing["id"])
        assert ed_entry.get("pinned") is True
