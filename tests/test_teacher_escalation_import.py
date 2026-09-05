"""Regression test for #186: _call_teacher's lazy import of _TEACHER_SYSTEM_PROMPT.

_TEACHER_SYSTEM_PROMPT was moved from src.ai_interaction to
src.agent_tools.model_interaction_tools in 56ba144 (#4445), but
src/teacher_escalation.py kept importing it from the old location, so every
real call to _call_teacher raised ImportError. Existing tests (e.g.
tests/test_teacher_audit_owner_scope.py) monkeypatch
`src.ai_interaction._TEACHER_SYSTEM_PROMPT` with `raising=False`, which
*creates* the attribute on that module and hid the missing import.

This test deliberately does NOT touch the prompt import at all, so it fails
loudly (ImportError) if the constant ever goes missing from
src/teacher_escalation.py's import again.
"""
import asyncio

import src.teacher_escalation as teacher_escalation
from src.agent_tools.model_interaction_tools import _TEACHER_SYSTEM_PROMPT


def test_call_teacher_uses_shared_system_prompt(monkeypatch):
    seen = {}

    def fake_resolve_model(spec, owner=None):
        return ("http://endpoint.local/v1", "teacher-model", {})

    async def fake_llm_call_async(url, model, messages, **kwargs):
        seen["messages"] = messages
        return "teacher reply"

    monkeypatch.setattr("src.ai_interaction._resolve_model", fake_resolve_model)
    monkeypatch.setattr("src.llm_core.llm_call_async", fake_llm_call_async)

    result = asyncio.run(
        teacher_escalation._call_teacher("teacher-model", "prompt", owner="alice")
    )

    assert result == "teacher reply"
    system_message = seen["messages"][0]
    assert system_message["role"] == "system"
    assert system_message["content"] == _TEACHER_SYSTEM_PROMPT
