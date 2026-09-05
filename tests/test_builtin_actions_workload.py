"""Pin: builtin_actions LLM calls must run as background workload.

`_local_model_slot` (src/llm_core.py) treats any call without an explicit
`workload="background"` as foreground, and foreground calls are allowed to
*cancel* an in-flight background call on a local model endpoint. The four
scheduled/background actions in `src/builtin_actions.py` that call
`wait_for_interactive_quiet(...)` and then `llm_call_async_with_fallback(...)`
directly must therefore pass `workload="background"` explicitly, or they can
cancel a real background job (issue #183).

Two layers of coverage:

1. An AST-based pin over every `llm_call_async_with_fallback(...)` call site
   in `src/builtin_actions.py`. This is the narrow AST exception allowed by
   tests/TESTING_STANDARD.md's Behavioral-first policy: the invariant is a
   cross-cutting "every call site must carry this kwarg" property, which is
   awkward to exercise behaviorally for *every* site (each lives in a
   different action with different scaffolding needs), so a structural check
   is the practical way to guarantee none regresses silently.
2. A behavioral, recorder-based test for the memory-consolidation action
   (`action_consolidate_memory`), which is drivable with modest stubs and
   proves the kwarg actually reaches the call at runtime, not just in source.
"""

import ast
import json
import textwrap
from pathlib import Path

import pytest

BUILTIN_ACTIONS_PATH = Path(__file__).resolve().parent.parent / "src" / "builtin_actions.py"


def _llm_call_with_fallback_calls(tree: ast.AST):
    """Yield every ast.Call node invoking llm_call_async_with_fallback."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "llm_call_async_with_fallback":
            yield node


def test_every_llm_call_async_with_fallback_call_site_marks_background_workload():
    source = BUILTIN_ACTIONS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(BUILTIN_ACTIONS_PATH))

    calls = list(_llm_call_with_fallback_calls(tree))
    assert len(calls) >= 3, (
        "expected to find the known llm_call_async_with_fallback call sites in "
        "builtin_actions.py; found none - has the module been restructured?"
    )

    missing = []
    for node in calls:
        workload_kw = next((kw for kw in node.keywords if kw.arg == "workload"), None)
        is_background = (
            workload_kw is not None
            and isinstance(workload_kw.value, ast.Constant)
            and workload_kw.value.value == "background"
        )
        if not is_background:
            missing.append(node.lineno)

    assert not missing, (
        "llm_call_async_with_fallback call(s) at line(s) "
        f"{missing} in {BUILTIN_ACTIONS_PATH} are missing workload=\"background\" - "
        "these can cancel a real background job via _local_model_slot (issue #183)"
    )


@pytest.mark.asyncio
async def test_consolidate_memory_passes_background_workload(monkeypatch, tmp_path):
    """Drive action_consolidate_memory end-to-end with a recording fake for
    llm_call_async_with_fallback and assert the recorded kwargs include
    workload="background"."""
    from src import constants, llm_core, task_endpoint

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))

    memory_file = tmp_path / "memory.json"
    memory_file.write_text(
        json.dumps(
            [
                {
                    "id": "m1",
                    "owner": "alice",
                    "text": "original text one",
                    "category": "fact",
                    "source": "user",
                    "timestamp": 1,
                },
                {
                    "id": "m2",
                    "owner": "alice",
                    "text": "original text two",
                    "category": "fact",
                    "source": "user",
                    "timestamp": 2,
                },
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        task_endpoint,
        "resolve_task_candidates",
        lambda *args, **kwargs: [("http://llm", "alice-model", {})],
    )

    recorded_calls = []

    async def fake_llm_call_async_with_fallback(_candidates, **kwargs):
        recorded_calls.append(kwargs)
        return json.dumps(
            {
                "keep": [
                    {"id": "m1", "text": "cleaned text one", "category": "fact"},
                ],
                "drop": [
                    {"id": "m2", "reason": "duplicate"},
                ],
            }
        )

    monkeypatch.setattr(
        llm_core, "llm_call_async_with_fallback", fake_llm_call_async_with_fallback
    )

    from src.builtin_actions import action_consolidate_memory

    message, ok = await action_consolidate_memory("alice")

    assert ok is True
    assert "AI tidied" in message
    assert len(recorded_calls) == 1
    assert recorded_calls[0].get("workload") == "background"
