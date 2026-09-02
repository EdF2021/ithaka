"""Static regressions for issue #135: an uncaught promise rejection out of
``handleChatSubmit`` when a send fails (e.g. no model configured).

Root cause: ``static/js/chat.js`` declared ``const streamingTTS`` (and,
identically, ``const _isAgent``) inside its outer ``try`` block, while the
enclosing ``catch (err)`` block reads both unconditionally. ``try`` and
``catch`` are **sibling block scopes** in JavaScript — a ``const``/``let``
declared inside the ``try`` is simply not part of the ``catch``'s scope
chain, regardless of where in the try it sits relative to the throw. This is
plain lexical scoping, not a temporal-dead-zone timing issue: the catch does
not throw "cannot access before initialization", it throws
``ReferenceError: X is not defined`` (verified with a throwaway node repro).
So *every* foreground error that reaches this catch — a request timeout, a
Stop-button abort, or the ordinary "Stream closed before completion" error —
made the catch itself throw a second, unrelated ``ReferenceError`` that
masked whatever error actually triggered it. That new rejection was, in
turn, never caught by ``handleChatSubmit``'s only caller
(``static/app.js``'s form ``onsubmit`` handler), so it surfaced in the
console as "Uncaught (in promise)".

Note on the #135 repro specifically: the reported "no model configured" 400
itself is handled by the ``!res.ok`` branch, which returns cleanly *without*
reaching this catch. The uncaught rejections observed alongside that 400 in
the #135 report came from a concurrent stream error hitting this same catch
block — the fix below covers exactly that path (and every other path into
this catch, which is the more general form of the bug).

Fix: hoist plain ``let streamingTTS = false;`` / ``let _isAgent = false;``
declarations to handleChatSubmit's top level (so both live in the function's
own scope, which the try and catch blocks both see), and add a ``.catch`` at
the two places outside chat.js that invoke ``handleChatSubmit`` /
``originalSubmit`` without one, so no promise in the submit path can escape
unhandled: ``static/app.js``'s form ``onsubmit`` wrapper.

This repo has no build step and no JS DOM test runner capable of driving
``chat.js`` (it touches dozens of DOM/window globals), so — following the
precedent set by ``test_notebook_workspace_static.py`` and
``test_settings_admin_managed_tabs_static.py`` — these are source-text
assertions of the fix's structural shape rather than a runtime execution.
"""

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHAT_JS = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")
_APP_JS = (_REPO / "static" / "app.js").read_text(encoding="utf-8")

# handleChatSubmit runs from its `export async function` header up to the
# next top-level function in the module (abortCurrentRequest). Slicing the
# text to just this function keeps the "no stray `const X =`" check from
# being satisfied by an unrelated same-named local elsewhere in this
# 5000+ line file.
_SUBMIT_START = _CHAT_JS.index("export async function handleChatSubmit(")
_SUBMIT_END = _CHAT_JS.index("export function abortCurrentRequest(", _SUBMIT_START)
_HANDLE_CHAT_SUBMIT = _CHAT_JS[_SUBMIT_START:_SUBMIT_END]

_LEAKED_VARS = ["streamingTTS", "_isAgent"]


@pytest.mark.parametrize("name", _LEAKED_VARS)
def test_var_is_hoisted_let_not_a_try_scoped_const(name):
    """Each variable must be a hoisted `let` at handleChatSubmit's top
    level, not a `const` declared inside the try (which the catch, as a
    sibling scope, can never see)."""
    assert f"let {name} = false;" in _HANDLE_CHAT_SUBMIT
    assert f"const {name} = " not in _HANDLE_CHAT_SUBMIT


@pytest.mark.parametrize("name, catch_usage", [
    ("streamingTTS", "if (streamingTTS && window.aiTTSManager) window.aiTTSManager.stop();"),
    ("_isAgent", "const timeoutMsg = _isAgent"),
])
def test_var_declaration_precedes_its_catch_block_usage(name, catch_usage):
    decl_idx = _HANDLE_CHAT_SUBMIT.index(f"let {name} = false;")
    catch_idx = _HANDLE_CHAT_SUBMIT.index(catch_usage)
    assert decl_idx < catch_idx


@pytest.mark.parametrize("name", _LEAKED_VARS)
def test_var_declaration_precedes_the_outer_try_block(name):
    """The declaration must sit ahead of handleChatSubmit's outer try (the
    one whose catch reads it) — not just ahead of its own original
    assignment site — so it is in scope no matter what inside that try
    throws first."""
    decl_idx = _HANDLE_CHAT_SUBMIT.index(f"let {name} = false;")
    # The outer try that this bug lives in is the last `try {` before the
    # /api/chat_stream fetch it wraps.
    fetch_idx = _HANDLE_CHAT_SUBMIT.index("await fetch(`${API_BASE}/api/chat_stream`")
    try_idx = _HANDLE_CHAT_SUBMIT.rindex("try {", 0, fetch_idx)
    assert decl_idx < try_idx < fetch_idx


def test_form_submit_handler_catches_handle_chat_submit_rejection():
    """static/app.js wires handleChatSubmit as the chat form's onsubmit
    handler; that call must not let a rejection escape uncaught, mirroring
    the existing `.catch(` pattern already used for the queued-send call
    site in chat.js. The handler must actually do something with the
    error (log/report it), not just swallow it with an empty `.catch(() =>
    {})`."""
    call_idx = _APP_JS.index("originalSubmit.call(chatModule, e)")
    tail = _APP_JS[call_idx:call_idx + 400]
    catch_idx = tail.index(".catch(")
    handler_body = tail[catch_idx:catch_idx + 250]
    assert "console.error" in handler_body or "showError" in handler_body
