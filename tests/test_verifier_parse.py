"""Tests for the verifier-subagent output parsing in src/agent_loop.py.

_run_verifier_subagent() calls an LLM to independently judge whether a
completed task actually satisfies the request, then parses a trailing
``VERIFICATION: SUCCESS`` / ``VERIFICATION: FAIL: ...`` line out of the raw
completion. The parsing itself is pure and has been extracted into
_parse_verifier_output() so it can be tested directly without touching the
network. A second test drives _run_verifier_subagent() end-to-end with the
LLM call monkeypatched, to pin the wiring between the two.
"""
import sys
from unittest.mock import MagicMock

import pytest

from tests.helpers.import_state import preserve_import_state

_MOCKED_IMPORTS = [
    'sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.ext', 'sqlalchemy.ext.declarative',
    'sqlalchemy.ext.hybrid', 'sqlalchemy.sql', 'sqlalchemy.sql.expression',
    'src.database',
    'src.agent_tools',
    'core.models', 'core.database',
]

with preserve_import_state(*_MOCKED_IMPORTS, "src.agent_loop"):
    for _mod in _MOCKED_IMPORTS:
        if _mod not in sys.modules:
            sys.modules[_mod] = MagicMock()
    from src.agent_loop import _parse_verifier_output, _run_verifier_subagent


# ---------------------------------------------------------------------------
# _parse_verifier_output — pure parsing, no network
# ---------------------------------------------------------------------------

def test_parse_success_returns_no_reasons():
    raw = "Everything requested was done correctly.\nVERIFICATION: SUCCESS"
    assert _parse_verifier_output(raw) == []


def test_parse_fail_returns_reasons_split_on_semicolon():
    raw = (
        "The file was never written; the tests were not run.\n"
        "VERIFICATION: FAIL: file not written; tests not run"
    )
    assert _parse_verifier_output(raw) == ["file not written", "tests not run"]


def test_parse_fail_single_reason():
    raw = "VERIFICATION: FAIL: the requested endpoint was not added"
    assert _parse_verifier_output(raw) == ["the requested endpoint was not added"]


def test_parse_garbage_output_fails_open_to_success():
    """No VERIFICATION line at all must never block a valid completion."""
    raw = "I'm not sure what happened here, the model rambled unrelated text."
    assert _parse_verifier_output(raw) == []


def test_parse_empty_string_fails_open_to_success():
    assert _parse_verifier_output("") == []
    assert _parse_verifier_output(None) == []


def test_parse_uses_last_verification_line_when_multiple_present():
    """If the model repeats/echoes the instructions, only the final line counts."""
    raw = (
        "VERIFICATION: SUCCESS\n"
        "wait, let me reconsider...\n"
        "VERIFICATION: FAIL: actually the docs were not updated"
    )
    assert _parse_verifier_output(raw) == ["actually the docs were not updated"]


def test_parse_strips_think_blocks_before_parsing():
    raw = (
        "<think>internal reasoning about whether this passes</think>\n"
        "VERIFICATION: FAIL: missing test coverage"
    )
    assert _parse_verifier_output(raw) == ["missing test coverage"]


# ---------------------------------------------------------------------------
# _run_verifier_subagent — end-to-end with the LLM call monkeypatched
# ---------------------------------------------------------------------------

async def _fake_llm_call(response_text):
    async def _call(*args, **kwargs):
        return response_text
    return _call


async def test_run_verifier_subagent_success(monkeypatch):
    import src.llm_core as llm_core
    monkeypatch.setattr(
        llm_core, "llm_call_async",
        await _fake_llm_call("Looks complete.\nVERIFICATION: SUCCESS"),
    )
    reasons = await _run_verifier_subagent(
        "write a test", "ran pytest -> 5 passed",
        endpoint_url="http://x", model="m", headers={},
    )
    assert reasons == []


async def test_run_verifier_subagent_fail(monkeypatch):
    import src.llm_core as llm_core
    monkeypatch.setattr(
        llm_core, "llm_call_async",
        await _fake_llm_call("Not done.\nVERIFICATION: FAIL: no tests were added"),
    )
    reasons = await _run_verifier_subagent(
        "write a test", "made unrelated edits",
        endpoint_url="http://x", model="m", headers={},
    )
    assert reasons == ["no tests were added"]


async def test_run_verifier_subagent_llm_error_fails_open(monkeypatch):
    import src.llm_core as llm_core

    async def _raise(*args, **kwargs):
        raise RuntimeError("endpoint unreachable")

    monkeypatch.setattr(llm_core, "llm_call_async", _raise)
    reasons = await _run_verifier_subagent(
        "write a test", "some actions",
        endpoint_url="http://x", model="m", headers={},
    )
    assert reasons == []
