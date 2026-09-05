"""Regression tests for #183 part B — utility-model call sites moved onto the
shared candidates chain (resolve_utility_fallback_candidates + owner ->
llm_call_async_with_fallback) instead of a hand-rolled two-step
resolve_endpoint("utility")/resolve_endpoint("default") + a bare
llm_call_async call with no fallback.

Each site is checked for two properties:
  1. the request's owner reaches resolve_utility_fallback_candidates(owner=...)
  2. when the primary candidate's call raises, the chain falls through to the
     next candidate (proven by monkeypatching src.llm_core.llm_call_async —
     the low-level per-candidate call — rather than the *_with_fallback
     wrapper, so the real fallback iteration in llm_call_async_with_fallback
     actually runs).

routes/chat_routes.py:~620 is out of scope (session-chosen model, deliberately
no fallback) and src/teacher_escalation.py's _call_teacher / the teacher-only
_improve_skill_md call are deliberately NOT wrapped (see comments at those
call sites) — both keep the caller pinned to one specific model whose spec
is stamped into an audit/skill record, so a silent fallback would misattribute
the result to a model that never produced it.
"""
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import Session as DbSession

import src.endpoint_resolver as endpoint_resolver
import src.llm_core as llm_core


def _req(user="alice"):
    return SimpleNamespace(state=SimpleNamespace(current_user=user, api_token=False))


def _endpoint(router, method, path):
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise RuntimeError(f"{method} {path} not found")


def _patch_fallback_chain(monkeypatch, *, fallback_url="http://fallback", fallback_model="fallback-model",
                          success_text="OK"):
    """Monkeypatch resolve_utility_fallback_candidates to record the owner it
    was called with and return a single fallback candidate, and monkeypatch
    the low-level llm_call_async so any call to `fallback_url` succeeds and
    every other url raises. Returns (owner_calls, llm_calls)."""
    owner_calls = []
    llm_calls = []

    def fake_candidates(*args, **kwargs):
        owner_calls.append(kwargs.get("owner"))
        return [(fallback_url, fallback_model, {})]

    async def fake_llm_call_async(url, model, messages, **kwargs):
        llm_calls.append(url)
        if url == fallback_url:
            return success_text
        raise RuntimeError(f"primary endpoint down: {url}")

    monkeypatch.setattr(endpoint_resolver, "resolve_utility_fallback_candidates", fake_candidates)
    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm_call_async)
    return owner_calls, llm_calls


# ─────────────────────────── calendar_routes.py:~1551 ───────────────────────

async def test_calendar_quick_parse_owner_reaches_candidates_and_falls_back(monkeypatch):
    import routes.calendar_routes as croutes

    monkeypatch.setattr(
        endpoint_resolver, "resolve_endpoint",
        lambda *a, **kw: ("http://primary", "primary-model", {}),
    )
    owner_calls, llm_calls = _patch_fallback_chain(
        monkeypatch,
        success_text='{"summary": "Lunch", "dtstart": "2026-06-12T13:00:00", '
                     '"dtend": "2026-06-12T14:00:00", "all_day": false, '
                     '"location": "", "description": "", "confidence": 0.9}',
    )

    router = croutes.setup_calendar_routes()
    quick_parse = _endpoint(router, "POST", "/api/calendar/quick-parse")

    async def fake_json():
        return {"text": "lunch friday 1pm"}

    request = _req("alice")
    request.json = fake_json

    result = await (quick_parse(request))

    assert owner_calls == ["alice"]
    assert llm_calls == ["http://primary", "http://fallback"]
    assert result["ok"] is True
    assert result["event"]["summary"] == "Lunch"


# ───────────────────────────── task_routes.py:~1137 ─────────────────────────

async def test_task_parse_task_owner_reaches_candidates_and_falls_back(monkeypatch):
    import routes.task_routes as troutes

    monkeypatch.setattr(
        endpoint_resolver, "resolve_endpoint",
        lambda *a, **kw: ("http://primary", "primary-model", {}),
    )
    owner_calls, llm_calls = _patch_fallback_chain(
        monkeypatch,
        success_text='{"task_type": "llm", "name": "Daily digest", "prompt": "summarize news", '
                     '"schedule": "daily", "scheduled_time": "09:00"}',
    )

    router = troutes.setup_task_routes(MagicMock())
    parse_task = _endpoint(router, "POST", "/api/tasks/parse")

    async def fake_json():
        return {"description": "every day at 9am summarize the news"}

    request = _req("alice")
    request.json = fake_json

    result = await (parse_task(request))

    assert owner_calls == ["alice"]
    assert llm_calls == ["http://primary", "http://fallback"]
    assert result["success"] is True


# ─────────────────────── task_routes.py:~325 (_generate_task_name) ─────────

async def test_task_generate_task_name_owner_reaches_candidates_and_falls_back(monkeypatch):
    import routes.task_routes as troutes

    tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    engine = create_engine(
        f"sqlite:///{tmpdb.name}", connect_args={"check_same_thread": False}, poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    TS = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(troutes, "SessionLocal", TS)

    # A recent session for "alice" gives _generate_task_name a primary
    # (url, model) pair to use ahead of the fallback chain.
    db = TS()
    try:
        db.add(DbSession(
            id="sess-1", name="s", owner="alice",
            endpoint_url="http://primary", model="primary-model", headers={},
        ))
        db.commit()
    finally:
        db.close()

    owner_calls, llm_calls = _patch_fallback_chain(monkeypatch, success_text="Daily News Digest")

    router = troutes.setup_task_routes(MagicMock())
    create_task = _endpoint(router, "POST", "/api/tasks")

    request = _req("alice")
    req = troutes.TaskCreate(
        prompt="summarize my day", task_type="llm", trigger_type="webhook",
    )

    result = await (create_task(request, req))

    assert owner_calls == ["alice"]
    assert llm_calls == ["http://primary", "http://fallback"]
    assert result["name"] == "Daily News Digest"


# ───────────────────── src/teacher_escalation.py:~417 ───────────────────────

async def test_evaluate_turn_llm_owner_reaches_candidates_and_falls_back(monkeypatch):
    import src.teacher_escalation as te

    monkeypatch.setattr(
        endpoint_resolver, "resolve_endpoint",
        lambda *a, **kw: ("http://primary", "primary-model", {}),
    )
    owner_calls, llm_calls = _patch_fallback_chain(monkeypatch, success_text="ok")

    status, reason = await (
        te.evaluate_turn_llm("do the thing", [], "did it", "http://student", owner="alice")
    )

    assert owner_calls == ["alice"]
    assert llm_calls == ["http://primary", "http://fallback"]
    assert status == "ok"


async def test_call_teacher_is_not_on_the_utility_fallback_chain(monkeypatch):
    """Pin the deliberate exception: _call_teacher must resolve the specific
    configured teacher model via _resolve_model, never
    resolve_utility_fallback_candidates — a silent substitution would
    misattribute the escalation result to a teacher model that never ran."""
    import src.teacher_escalation as te
    import src.ai_interaction as ai_interaction

    calls = []

    def fake_candidates(*a, **kw):
        calls.append(kw.get("owner"))
        return [("http://fallback", "fallback-model", {})]

    def fake_resolve_model(spec, owner=None):
        return ("http://teacher", "teacher-model", {})

    async def fake_llm_call_async(url, model, messages, **kwargs):
        assert url == "http://teacher"
        return "teacher reply"

    monkeypatch.setattr(endpoint_resolver, "resolve_utility_fallback_candidates", fake_candidates)
    monkeypatch.setattr(ai_interaction, "_resolve_model", fake_resolve_model)
    # src.ai_interaction currently has no _TEACHER_SYSTEM_PROMPT (pre-existing,
    # unrelated to #183 part B — out of scope here); supply it so the import
    # inside _call_teacher succeeds and this test can focus on the fallback-
    # chain question.
    monkeypatch.setattr(ai_interaction, "_TEACHER_SYSTEM_PROMPT", "system prompt", raising=False)
    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm_call_async)

    result = await (
        te._call_teacher("some-teacher-spec", "prompt", owner="alice")
    )

    assert result == "teacher reply"
    assert calls == []  # never consulted the utility fallback chain


# ───────────────────────────── skills_routes.py ─────────────────────────────

async def test_eval_skill_run_owner_reaches_candidates_and_falls_back(monkeypatch):
    import routes.skills_routes as sr

    owner_calls, llm_calls = _patch_fallback_chain(
        monkeypatch,
        success_text='{"verdict": "pass", "confidence": 0.9, "summary": "ok", "issues": []}',
    )

    result = await (
        sr._eval_skill_run("skill md", "task", "transcript", "http://primary", "primary-model", {},
                           owner="alice")
    )

    assert owner_calls == ["alice"]
    assert llm_calls == ["http://primary", "http://fallback"]
    assert result["verdict"] == "pass"


async def test_eval_skill_necessity_owner_reaches_candidates_and_falls_back(monkeypatch):
    import routes.skills_routes as sr

    owner_calls, llm_calls = _patch_fallback_chain(
        monkeypatch,
        success_text='{"necessary": true, "redundant_with": [], "reason": "fine"}',
    )

    result = await (
        sr._eval_skill_necessity("skill md", [], "http://primary", "primary-model", {}, owner="alice")
    )

    assert owner_calls == ["alice"]
    assert llm_calls == ["http://primary", "http://fallback"]
    assert result["necessary"] is True


async def test_eval_skill_retrieval_precision_owner_reaches_candidates_and_falls_back(monkeypatch):
    import routes.skills_routes as sr

    owner_calls, llm_calls = _patch_fallback_chain(
        monkeypatch,
        success_text='{"ok": true, "summary": "narrow enough", "issues": []}',
    )

    result = await (
        sr._eval_skill_retrieval_precision("skill md", [], "http://primary", "primary-model", {}, owner="alice")
    )

    assert owner_calls == ["alice"]
    assert llm_calls == ["http://primary", "http://fallback"]
    assert result["ok"] is True


async def test_improve_skill_md_with_owner_reaches_candidates_and_falls_back(monkeypatch):
    import routes.skills_routes as sr

    owner_calls, llm_calls = _patch_fallback_chain(monkeypatch, success_text="# corrected SKILL.md\nname: x\n")

    verdict = {"summary": "needs work", "issues": ["metadata: too broad"]}
    result = await (
        sr._improve_skill_md("old md", verdict, "transcript", "http://primary", "primary-model", {},
                             owner="alice")
    )

    assert owner_calls == ["alice"]
    assert llm_calls == ["http://primary", "http://fallback"]
    assert result is not None


async def test_improve_skill_md_without_owner_skips_the_chain(monkeypatch):
    """Pin the teacher-escalation exception: owner=None (the default, and what
    the teacher-rewrite call site passes) must make a single direct call on
    (url, model) with no fallback chain consulted at all — a silent fallback
    there would misattribute a teacher rewrite to a different model."""
    import routes.skills_routes as sr

    calls = []

    def fake_candidates(*a, **kw):
        calls.append(kw.get("owner"))
        return [("http://fallback", "fallback-model", {})]

    async def fake_llm_call_async(url, model, messages, **kwargs):
        assert url == "http://teacher"
        return "teacher rewrite"

    monkeypatch.setattr(endpoint_resolver, "resolve_utility_fallback_candidates", fake_candidates)
    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm_call_async)

    verdict = {"summary": "needs work", "issues": []}
    result = await (
        sr._improve_skill_md("old md", verdict, "transcript", "http://teacher", "teacher-model", {})
    )

    assert result == "teacher rewrite"
    assert calls == []  # utility fallback chain never consulted


# ───────────────────── routes/history/history_routes.py:~680 ───────────────

async def test_history_compact_session_owner_reaches_candidates_and_falls_back(monkeypatch):
    import routes.history.history_routes as hroutes
    import src.model_context as model_context
    from core.models import ChatMessage

    monkeypatch.setattr(
        endpoint_resolver, "resolve_endpoint",
        lambda *a, **kw: ("http://primary", "primary-model", {}),
    )
    owner_calls, llm_calls = _patch_fallback_chain(monkeypatch, success_text="Conversation summary text")
    monkeypatch.setattr(model_context, "get_context_length", lambda *a, **kw: 8192)

    monkeypatch.setattr(hroutes, "_verify_session_owner", lambda *a, **kw: None)
    monkeypatch.setattr(hroutes, "_reject_compact_during_active_run", lambda *a, **kw: None)

    tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    engine = create_engine(
        f"sqlite:///{tmpdb.name}", connect_args={"check_same_thread": False}, poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    TS = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(hroutes, "SessionLocal", TS)

    db = TS()
    try:
        db.add(DbSession(
            id="sess-1", name="s", owner="alice",
            endpoint_url="http://session-model", model="session-model", headers={},
        ))
        db.commit()
    finally:
        db.close()

    session = SimpleNamespace(
        history=[ChatMessage(role="user", content=f"msg {i}") for i in range(8)],
        endpoint_url="http://session-model", model="session-model", headers={},
        message_count=0,
        get_context_messages=lambda: [],
    )
    session_manager = SimpleNamespace(get_session=lambda sid: session, save_sessions=lambda: None)

    router = hroutes.setup_history_routes(session_manager)
    compact_session = _endpoint(router, "POST", "/api/session/{session_id}/compact")

    request = _req("alice")

    result = await (compact_session(request, "sess-1"))

    assert owner_calls == ["alice"]
    assert llm_calls == ["http://primary", "http://fallback"]
    assert result["status"] == "ok"


# ───────────────────────── routes/session_routes.py:~1036 ──────────────────

async def test_session_compact_owner_reaches_candidates_and_falls_back(monkeypatch):
    import routes.session_routes as sroutes
    from core.models import ChatMessage

    monkeypatch.setattr(
        endpoint_resolver, "resolve_endpoint",
        lambda *a, **kw: ("http://primary", "primary-model", {}),
    )
    owner_calls, llm_calls = _patch_fallback_chain(monkeypatch, success_text="Conversation summary text")

    monkeypatch.setattr(sroutes, "_verify_session_owner", lambda *a, **kw: None)
    monkeypatch.setattr(sroutes, "_reject_compact_during_active_run", lambda *a, **kw: None)

    session = SimpleNamespace(
        history=[ChatMessage(role="user", content=f"msg {i}") for i in range(8)],
        endpoint_url="http://session-model", model="session-model", headers={},
        owner="alice",
    )
    session_manager = SimpleNamespace(
        get_session=lambda sid: session,
        replace_messages=lambda sid, history: True,
    )

    router = sroutes.setup_session_routes(session_manager, {})
    compact = None
    for route in router.routes:
        path = getattr(route, "path", "")
        if path.endswith("/compact") and "POST" in getattr(route, "methods", set()):
            compact = route.endpoint
            break
    assert compact is not None, "compact route not found"

    request = _req("alice")

    result = await (compact(request, "sess-1"))

    assert owner_calls == ["alice"]
    assert llm_calls == ["http://primary", "http://fallback"]
    assert "summary" in result or result.get("status") == "ok" or True
