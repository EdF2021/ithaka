# Realtime-gesprek fase 2 (tool-calling via `ask_ithaka`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the OpenAI Realtime voice session one function tool, `ask_ithaka(question)`, that the browser forwards to a new backend endpoint which runs the question through Ithaka's normal agent loop (tools/MCP/RAG on the task/utility model) and returns the spoken answer.

**Architecture:** `build_session_config()` declares the tool when `realtime_tools_enabled`; `services/realtime/realtime_ask.py::answer_question()` wraps `stream_agent_loop()` one-shot (collect deltas, 60 s timeout, ≤1500 chars); `POST /api/realtime/ask` exposes it; `realtimeVoice.js` classifies `response.function_call_arguments.done`, POSTs the question, and replies over the data channel with `conversation.item.create` (`function_call_output`) + `response.create`, serialized one call at a time.

**Tech Stack:** FastAPI, httpx, vanilla ES modules, pytest (asyncio_mode=auto), Node for JS pure-function tests.

**Spec:** `docs/superpowers/specs/2026-09-04-realtime-voice-tools-design.md`

## Global Constraints

- All user-facing strings Dutch; code/comments English. No Unicode emoji in UI or code.
- `realtime_tools_enabled` is a **global** setting (NOT in `_PER_USER_KEYS`), default `True`.
- `answer_question` never passes `workload="background"` to `stream_agent_loop` and never calls `wait_for_interactive_quiet` (self-deadlock inside a tracked request — CLAUDE.md Notebooks gotcha).
- Backend errors surface as Dutch `RuntimeError`/`ValueError` messages → route 400; unexpected exceptions → 500 with a generic Dutch message, never internal details.
- Tool output sent to OpenAI is always a JSON **string** (`JSON.stringify({answer})` / `{error}`).
- Only one function call handled at a time in the browser (promise chain); after every `await` re-check `this._active`.
- `/api/realtime/ask` is NOT added to `_PASSIVE_PATTERNS` in `src/interactive_gate.py`.
- Reuse existing CSS classes (`admin-switch`, `admin-slider`, `settings-label`); no new colors/sizes.
- Commits: Conventional Commits, body ends with exactly `Ed de Feber, in nauwe samenwerking met Claude`, no `Co-Authored-By` trailer.

---

### Task 1: Setting + session-config tool declaration

**Files:**
- Modify: `src/settings.py` (DEFAULT_SETTINGS, right after `"realtime_max_minutes": 10,`)
- Modify: `services/realtime/realtime_service.py` (`_load_settings`, `build_session_config`, new constant `ASK_ITHAKA_TOOL`)
- Test: `tests/test_settings_realtime_keys.py`, `tests/test_realtime_service.py`

**Interfaces:**
- Produces: `services.realtime.realtime_service.ASK_ITHAKA_TOOL` (dict), `build_session_config(settings)` now reads `settings["realtime_tools_enabled"]` and emits `tools` + `tool_choice`.

- [ ] **Step 1: Failing tests**

Append to `tests/test_settings_realtime_keys.py`:

```python
def test_realtime_tools_enabled_default_true_and_global():
    from src.settings import DEFAULT_SETTINGS, _PER_USER_KEYS
    assert DEFAULT_SETTINGS["realtime_tools_enabled"] is True
    assert "realtime_tools_enabled" not in _PER_USER_KEYS
```

Append to `tests/test_realtime_service.py` (uses the file's existing `_settings(**overrides)` helper — add `"realtime_tools_enabled": True` to its base dict):

```python
def test_build_session_config_declares_ask_ithaka_tool():
    from services.realtime.realtime_service import ASK_ITHAKA_TOOL
    cfg = RealtimeService().build_session_config(_settings(realtime_tools_enabled=True))
    assert cfg["tools"] == [ASK_ITHAKA_TOOL]
    assert cfg["tool_choice"] == "auto"
    assert ASK_ITHAKA_TOOL["type"] == "function"
    assert ASK_ITHAKA_TOOL["name"] == "ask_ithaka"
    assert ASK_ITHAKA_TOOL["parameters"]["required"] == ["question"]
    assert "Momentje" in ASK_ITHAKA_TOOL["description"]


def test_build_session_config_without_tools_when_disabled():
    cfg = RealtimeService().build_session_config(_settings(realtime_tools_enabled=False))
    assert cfg["tools"] == []
    assert "tool_choice" not in cfg
```

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/python -m pytest tests/test_settings_realtime_keys.py tests/test_realtime_service.py -q` (KeyError / AssertionError / ImportError on `ASK_ITHAKA_TOOL`).

- [ ] **Step 3: Implement**

`src/settings.py`, directly after the `"realtime_max_minutes": 10,` line:

```python
    # Fase 2: one delegation tool (ask_ithaka) in the Realtime session. See
    # docs/superpowers/specs/2026-09-04-realtime-voice-tools-design.md.
    "realtime_tools_enabled": True,
```

`services/realtime/realtime_service.py`: module-level constant after `logger = ...`:

```python
# Fase 2 — the single function tool declared in the Realtime session. The
# preamble guidance ("Momentje, ik zoek het op.") lives here, not in
# realtime_instructions, so existing custom instructions keep working.
ASK_ITHAKA_TOOL = {
    "type": "function",
    "name": "ask_ithaka",
    "description": (
        "Stel een vraag aan Ithaka, de assistent met toegang tot internet-zoeken, "
        "notities, agenda, e-mail, documenten en andere tools. Gebruik dit voor elke "
        "vraag die actuele feiten, persoonlijke gegevens van de gebruiker of opzoekwerk "
        "vereist — gok niet. Zeg vóór de aanroep één korte zin zoals 'Momentje, ik zoek "
        "het op.' Vat het antwoord daarna kort samen in het Nederlands."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "De volledige, zelfstandig begrijpelijke vraag in het Nederlands, "
                    "inclusief context uit het gesprek."
                ),
            }
        },
        "required": ["question"],
    },
}
```

In `_load_settings()` add `"realtime_tools_enabled": saved.get("realtime_tools_enabled", True),`.

In `build_session_config()`, replace the line `"tools": [],` with:

```python
            "tools": [ASK_ITHAKA_TOOL] if settings.get("realtime_tools_enabled", True) else [],
```

and after building the dict (assign it to `config` first), add:

```python
        if config["tools"]:
            config["tool_choice"] = "auto"
        return config
```

- [ ] **Step 4: Run, expect PASS** — same command; also `.venv/bin/python -m pytest tests/test_realtime_service.py -q` must stay fully green (the existing exact-equality test on `create_session`'s return value is unaffected; if an existing test asserts `cfg["tools"] == []` with default settings, update that test's `_settings()` call to `realtime_tools_enabled=False`).

- [ ] **Step 5: Commit** — `feat(realtime): declare ask_ithaka tool in Realtime session config`

---

### Task 2: `answer_question` — one-shot agent-loop delegation

**Files:**
- Create: `services/realtime/realtime_ask.py`
- Test: `tests/test_realtime_ask.py`

**Interfaces:**
- Consumes: `src.agent_loop.stream_agent_loop` (async generator of SSE strings `data: {...}`), `src.task_endpoint.resolve_task_candidates(owner=...) -> list[(url, model, headers)]`.
- Produces: `async def answer_question(question: str, owner: str | None) -> str`; constants `ASK_TIMEOUT_S = 60.0`, `ASK_MAX_ROUNDS = 6`, `ASK_MAX_CHARS = 1500`, `ASK_SYSTEM_PROMPT`.

- [ ] **Step 1: Failing tests** — create `tests/test_realtime_ask.py`:

```python
"""services/realtime/realtime_ask.py — one-shot delegation of a Realtime
voice question to the agent loop. See
docs/superpowers/plans/2026-09-04-realtime-voice-tools.md, Task 2."""

import asyncio
import json

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
```

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/python -m pytest tests/test_realtime_ask.py -q` (ModuleNotFoundError).

- [ ] **Step 3: Implement** — create `services/realtime/realtime_ask.py`:

```python
"""Fase 2 of the Realtime voice mode: the browser forwards the model's
`ask_ithaka(question)` function call here, and we answer it by running the
question once through Ithaka's normal agent loop (tools, MCP, RAG) on the
background-task / utility model chain. See
docs/superpowers/specs/2026-09-04-realtime-voice-tools-design.md.

Gotcha (CLAUDE.md, Notebooks): this runs inside a tracked foreground
request, so it must never wait on the interactive gate or use
workload="background" — stream_agent_loop's default "foreground" is the
correct, non-deadlocking choice.
"""

import asyncio
import json
import logging
import re

from src.agent_loop import stream_agent_loop
from src.task_endpoint import resolve_task_candidates

logger = logging.getLogger(__name__)

ASK_TIMEOUT_S = 60.0
ASK_MAX_ROUNDS = 6
ASK_MAX_CHARS = 1500

ASK_SYSTEM_PROMPT = (
    "Je bent Ithaka en beantwoordt een vraag die via een gesproken gesprek binnenkomt. "
    "Het antwoord wordt voorgelezen: antwoord in het Nederlands, beknopt (maximaal "
    "ongeveer 80 woorden), als platte lopende tekst zonder markdown, opsommingstekens, "
    "koppen of links. Gebruik je tools (zoeken, notities, agenda, e-mail, documenten) "
    "wanneer de vraag actuele of persoonlijke informatie vereist, en vat het resultaat "
    "samen in plaats van het te citeren. Geef geen denkstappen, alleen het antwoord."
)

_WS = re.compile(r"\s+")


async def _collect(question: str, owner, candidates) -> str:
    url, model, headers = candidates[0]
    fallbacks = list(candidates[1:])
    messages = [
        {"role": "system", "content": ASK_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    parts: list[str] = []
    async for event_str in stream_agent_loop(
        endpoint_url=url,
        model=model,
        messages=messages,
        headers=headers,
        session_id=None,
        owner=owner,
        max_rounds=ASK_MAX_ROUNDS,
        fallbacks=fallbacks,
    ):
        if not event_str.startswith("data: ") or event_str.startswith("data: [DONE]"):
            continue
        try:
            data = json.loads(event_str[6:])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and "delta" in data and not data.get("thinking"):
            parts.append(str(data["delta"]))
    return "".join(parts)


async def answer_question(question: str, owner) -> str:
    """Run `question` through the agent loop once and return plain spoken
    text. Raises ValueError (bad input / no model) or RuntimeError (timeout,
    empty answer) with Dutch messages; the route maps both to HTTP 400."""
    question = (question or "").strip()
    if not question:
        raise ValueError("Lege vraag")

    candidates = resolve_task_candidates(owner=owner)
    if not candidates:
        raise ValueError("Geen model beschikbaar voor ask_ithaka")

    try:
        raw = await asyncio.wait_for(_collect(question, owner, candidates), ASK_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("ask_ithaka timed out after %.0fs (owner=%s)", ASK_TIMEOUT_S, owner)
        raise RuntimeError("Het opzoeken duurde te lang") from None

    text = _WS.sub(" ", raw).strip()
    if not text:
        raise RuntimeError("Ithaka gaf geen antwoord")
    if len(text) > ASK_MAX_CHARS:
        text = text[:ASK_MAX_CHARS].rstrip() + "…"
    return text
```

- [ ] **Step 4: Run, expect PASS** — `.venv/bin/python -m pytest tests/test_realtime_ask.py -q`; `.venv/bin/python -m py_compile services/realtime/realtime_ask.py`.

- [ ] **Step 5: Commit** — `feat(realtime): answer_question one-shot agent-loop delegation for ask_ithaka`

---

### Task 3: `POST /api/realtime/ask` route

**Files:**
- Modify: `routes/realtime_routes.py`
- Test: `tests/test_routes_realtime.py`

**Interfaces:**
- Consumes: `services.realtime.realtime_ask.answer_question`, `src.auth_helpers.effective_user(request)`, `src.settings.get_setting`.
- Produces: `POST /api/realtime/ask` body `{"question": str, "call_id"?: str}` → `{"answer": str}`.

- [ ] **Step 1: Failing tests** — append to `tests/test_routes_realtime.py` (reuse its `_get_endpoint`; the route calls the module-level names `answer_question`, `effective_user`, `get_setting` imported into `routes.realtime_routes`, so patch them there):

```python
class _Req:
    """Minimal Request stand-in: json() body + state for effective_user."""

    def __init__(self, body):
        self._body = body
        self.state = type("S", (), {"current_user": "ed"})()

    async def json(self):
        return self._body


def _wire_ask(monkeypatch, *, enabled=True, tools=True, answer=None, raises=None):
    import routes.realtime_routes as rr
    monkeypatch.setattr(rr, "effective_user", lambda request: "ed")
    monkeypatch.setattr(
        rr, "get_setting",
        lambda key, default=None, owner=None: {"realtime_enabled": enabled, "realtime_tools_enabled": tools}.get(key, default),
    )
    seen = {}

    async def _answer(question, owner):
        seen["question"], seen["owner"] = question, owner
        if raises:
            raise raises
        return answer
    monkeypatch.setattr(rr, "answer_question", _answer)
    return seen


async def test_ask_route_returns_answer(monkeypatch):
    from routes.realtime_routes import setup_realtime_routes
    seen = _wire_ask(monkeypatch, answer="Het is 18 graden.")
    endpoint = _get_endpoint(setup_realtime_routes(_FakeRealtimeServiceOk()), "/api/realtime/ask")
    out = await endpoint(_Req({"question": "  Wat is het weer?  ", "call_id": "call_1"}))
    assert out == {"answer": "Het is 18 graden."}
    assert seen == {"question": "Wat is het weer?", "owner": "ed"}


async def test_ask_route_400_on_empty_question(monkeypatch):
    from routes.realtime_routes import setup_realtime_routes
    _wire_ask(monkeypatch, answer="x")
    endpoint = _get_endpoint(setup_realtime_routes(_FakeRealtimeServiceOk()), "/api/realtime/ask")
    with pytest.raises(HTTPException) as ei:
        await endpoint(_Req({"question": "   "}))
    assert ei.value.status_code == 400


async def test_ask_route_400_when_tools_disabled(monkeypatch):
    from routes.realtime_routes import setup_realtime_routes
    _wire_ask(monkeypatch, tools=False, answer="x")
    endpoint = _get_endpoint(setup_realtime_routes(_FakeRealtimeServiceOk()), "/api/realtime/ask")
    with pytest.raises(HTTPException) as ei:
        await endpoint(_Req({"question": "hoi"}))
    assert ei.value.status_code == 400
    assert "Realtime-tools staan uit" in ei.value.detail["message"]


async def test_ask_route_400_on_dutch_runtime_error(monkeypatch):
    from routes.realtime_routes import setup_realtime_routes
    _wire_ask(monkeypatch, raises=RuntimeError("Het opzoeken duurde te lang"))
    endpoint = _get_endpoint(setup_realtime_routes(_FakeRealtimeServiceOk()), "/api/realtime/ask")
    with pytest.raises(HTTPException) as ei:
        await endpoint(_Req({"question": "hoi"}))
    assert ei.value.status_code == 400
    assert ei.value.detail == {"message": "Het opzoeken duurde te lang"}


async def test_ask_route_500_generic_on_unexpected(monkeypatch):
    from routes.realtime_routes import setup_realtime_routes
    _wire_ask(monkeypatch, raises=KeyError("boom"))
    endpoint = _get_endpoint(setup_realtime_routes(_FakeRealtimeServiceOk()), "/api/realtime/ask")
    with pytest.raises(HTTPException) as ei:
        await endpoint(_Req({"question": "hoi"}))
    assert ei.value.status_code == 500
    assert "boom" not in str(ei.value.detail)
```

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/python -m pytest tests/test_routes_realtime.py -q` (StopIteration from `_get_endpoint`: no `/api/realtime/ask`).

- [ ] **Step 3: Implement** — in `routes/realtime_routes.py`, change imports to:

```python
from fastapi import APIRouter, HTTPException, Request
import logging

from services.realtime.realtime_ask import answer_question
from src.auth_helpers import effective_user
from src.settings import get_setting
```

and add inside `setup_realtime_routes`, after the `/session` route:

```python
    @router.post("/ask")
    async def ask_ithaka(request: Request):
        """Fase 2: the browser forwards the Realtime model's ask_ithaka
        function call here. Runs the question through the normal agent
        loop (tools/MCP/RAG) one-shot and returns plain text for speech.
        Auth: global AuthMiddleware (like /session); owner = effective_user."""
        owner = effective_user(request)
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail={"message": "body must be a JSON object"})
        question = body.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise HTTPException(status_code=400, detail={"message": "Lege of te lange vraag"})
        if not get_setting("realtime_enabled", False) or not get_setting("realtime_tools_enabled", True):
            raise HTTPException(status_code=400, detail={"message": "Realtime-tools staan uit"})
        try:
            answer = await answer_question(question.strip(), owner)
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail={"message": str(e)})
        except Exception as e:
            logger.error(f"ask_ithaka failed (call_id={body.get('call_id')}): {e}", exc_info=True)
            raise HTTPException(status_code=500, detail={"message": "Opzoeken via Ithaka mislukt"})
        return {"answer": answer}
```

- [ ] **Step 4: Run, expect PASS** — `.venv/bin/python -m pytest tests/test_routes_realtime.py -q`; `.venv/bin/python -m py_compile routes/realtime_routes.py app.py`.

- [ ] **Step 5: Commit** — `feat(realtime): POST /api/realtime/ask route for the ask_ithaka tool`

---

### Task 4: Browser — function-call handling + settings toggle

**Files:**
- Modify: `static/js/realtimeVoice.js`
- Modify: `static/app.js` (`rtStateChange` title for the `tool` phase)
- Modify: `static/index.html` (Realtime card: tools toggle row), `static/js/settings.js` (`initRealtimeSettings`)
- Test: `tests/test_realtime_voice_js.py`

**Interfaces:**
- Consumes: `POST /api/realtime/ask` → `{"answer"}` / `{detail: {message}}`; server event `response.function_call_arguments.done` `{call_id, name, arguments}`.
- Produces: exports `buildFunctionCallOutputEvents(callId, output)`; `classifyRealtimeEvent` new action `{type:'function_call', name, callId, arguments}`; new state value `'tool'`.

- [ ] **Step 1: Failing tests** — append to `tests/test_realtime_voice_js.py`:

```python
def test_classify_function_call_arguments_done():
    values = _node_eval(
        """
        const { classifyRealtimeEvent } = await import('./static/js/realtimeVoice.js');
        const action = classifyRealtimeEvent({
          type: 'response.function_call_arguments.done',
          call_id: 'call_1', name: 'ask_ithaka', arguments: '{"question":"weer?"}',
        });
        console.log(JSON.stringify(action));
        """
    )
    assert values == {"type": "function_call", "name": "ask_ithaka", "callId": "call_1", "arguments": '{"question":"weer?"}'}


def test_build_function_call_output_events_shape():
    values = _node_eval(
        """
        const { buildFunctionCallOutputEvents } = await import('./static/js/realtimeVoice.js');
        console.log(JSON.stringify(buildFunctionCallOutputEvents('call_1', '{"answer":"18 graden"}')));
        """
    )
    assert values == [
        {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "call_1", "output": '{"answer":"18 graden"}'}},
        {"type": "response.create"},
    ]


def test_build_function_call_output_events_stringifies_non_string():
    values = _node_eval(
        """
        const { buildFunctionCallOutputEvents } = await import('./static/js/realtimeVoice.js');
        console.log(JSON.stringify(buildFunctionCallOutputEvents('c', { error: 'x' })[0].item.output));
        """
    )
    assert values == '{"error":"x"}'
```

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/python -m pytest tests/test_realtime_voice_js.py -q`.

- [ ] **Step 3: Implement `realtimeVoice.js`**

In `classifyRealtimeEvent`, add before `case 'response.done':`:

```js
    case 'response.function_call_arguments.done':
      return {
        type: 'function_call',
        name: event.name || '',
        callId: event.call_id || '',
        arguments: typeof event.arguments === 'string' ? event.arguments : JSON.stringify(event.arguments || {}),
      }
```

After `shouldCancelForBargeIn`, add the pure helper:

```js
/**
 * Events to send back over the data channel after a tool call finished.
 * `output` must be a string for OpenAI; anything else is JSON-stringified.
 * Pure — unit-tested in Node.
 * @param {string} callId
 * @param {string|object} output
 */
export function buildFunctionCallOutputEvents(callId, output) {
  const text = typeof output === 'string' ? output : JSON.stringify(output)
  return [
    { type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId, output: text } },
    { type: 'response.create' },
  ]
}
```

Add to the `RealtimeVoice` object fields: `_toolChain: Promise.resolve(),` and update the state comment to `// idle | connecting | listening | speaking | tool | error`.

In `_onDataChannelMessage`'s switch, add:

```js
      case 'function_call':
        this._toolChain = this._toolChain.then(() => this._handleFunctionCall(action)).catch(() => {})
        break
```

Add the method (after `_onDataChannelMessage`):

```js
  /** @private — one call at a time (chained via _toolChain). */
  async _handleFunctionCall(action) {
    if (!this._active) return
    let output
    if (action.name !== 'ask_ithaka') {
      output = { error: 'Onbekende tool' }
    } else {
      let question = ''
      try { question = String(JSON.parse(action.arguments || '{}').question || '') } catch (e) { question = '' }
      if (!question.trim()) {
        output = { error: 'Ongeldige argumenten' }
      } else {
        this._state = 'tool'
        this._notify()
        if (window.chatRenderer?.addMessage) window.chatRenderer.addMessage('assistant', 'Opgezocht via Ithaka: ' + question, null, null)
        try {
          const res = await fetch('/api/realtime/ask', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question, call_id: action.callId }),
          })
          if (!this._active) return
          if (res.ok) {
            const data = await res.json()
            output = { answer: data.answer || '' }
          } else {
            const err = await res.json().catch(() => ({}))
            const detail = err.detail && (typeof err.detail === 'string' ? err.detail : err.detail.message)
            output = { error: detail || 'Het opzoeken is mislukt' }
          }
        } catch (e) {
          output = { error: 'Het opzoeken is mislukt' }
        }
        if (!this._active) return
        this._state = 'listening'
        this._notify()
      }
    }
    if (!this._dc || this._dc.readyState !== 'open') return
    for (const ev of buildFunctionCallOutputEvents(action.callId, output)) this._dc.send(JSON.stringify(ev))
  },
```

- [ ] **Step 4: `static/app.js`** — in `rtStateChange`, extend the title ternary so `phase === 'tool'` yields `'Realtime gesprek — zoekt op via Ithaka…'` (insert before the `'connecting'` branch).

- [ ] **Step 5: Settings toggle** — `static/index.html`: inside `#set-realtimeConfigWrap`, directly after the "Provider" row `<div>`, add:

```html
              <div style="display:flex;align-items:center;gap:0.75rem;">
                <label class="settings-label" for="set-realtimeToolsToggle">Tools (ask_ithaka)</label>
                <label class="admin-switch"><input type="checkbox" id="set-realtimeToolsToggle"><span class="admin-slider"></span></label>
                <span class="admin-toggle-sub" style="margin:0">Laat het gesprek vragen doorsturen naar Ithaka (zoeken, notities, agenda…)</span>
              </div>
```

`static/js/settings.js`, in `initRealtimeSettings()`: add `var toolsToggle = el('set-realtimeToolsToggle');` next to the other `el(...)` lookups; in the settings-load block add `if (toolsToggle) toolsToggle.checked = settings.realtime_tools_enabled !== false;`; in `saveRealtime()`'s JSON body add `realtime_tools_enabled: toolsToggle ? toolsToggle.checked : true,`; register `if (toolsToggle) toolsToggle.addEventListener('change', saveRealtime);` with the other listeners.

- [ ] **Step 6: Run, expect PASS** — `.venv/bin/python -m pytest tests/test_realtime_voice_js.py -q`; `node --check static/js/realtimeVoice.js static/js/settings.js static/app.js`.

- [ ] **Step 7: Commit** — `feat(realtime): handle ask_ithaka function calls in the browser + tools toggle`

---

### Task 5: Docs

**Files:**
- Modify: `CLAUDE.md` (Architecture bullet on voice mode: one sentence on fase 2 — `ask_ithaka` tool → `POST /api/realtime/ask` → `services/realtime/realtime_ask.py`, tools toggle `realtime_tools_enabled`)
- Modify: `docs/superpowers/specs/2026-09-03-realtime-voice-mode-design.md` — replace the "Tool-calling … is een aparte, latere fase" sentence with a pointer to the fase-2 spec.

- [ ] **Step 1: Edit both files** as described (≤ 6 lines total).
- [ ] **Step 2: Commit** — `docs(realtime): document fase 2 ask_ithaka tool`
