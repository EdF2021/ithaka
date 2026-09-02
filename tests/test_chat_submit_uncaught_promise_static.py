"""Static regressions for issue #135: an uncaught promise rejection out of
``handleChatSubmit`` when a send fails (e.g. no model configured).

Root cause: ``static/js/chat.js`` declared ``const streamingTTS`` deep inside
its ``try`` block (after the fetch/`!res.ok` check), but the enclosing
``catch (err)`` block reads ``streamingTTS`` unconditionally. Any exception
thrown earlier in the ``try`` — before that ``const`` executes — lands in the
``catch``, which then throws a *second*, unrelated ``ReferenceError:
Cannot access 'streamingTTS' before initialization`` (temporal dead zone) that
masks the original error and escapes ``handleChatSubmit`` as a rejected
promise. That promise was also never caught by its only caller
(``static/app.js``'s form ``onsubmit`` handler), so it surfaced in the
console as "Uncaught (in promise)".

Fix: hoist a plain ``let streamingTTS = false;`` next to the function's other
early declarations (so it's always initialized before the ``try`` can throw),
and add a ``.catch`` where ``static/app.js`` invokes ``handleChatSubmit`` as
the form's submit handler, mirroring the existing precedent at
``chat.js``'s own queued-send call site (``handleChatSubmit(...).catch(...)``).

This repo has no build step and no JS DOM test runner capable of driving
``chat.js`` (it touches dozens of DOM/window globals), so — following the
precedent set by ``test_notebook_workspace_static.py`` and
``test_settings_admin_managed_tabs_static.py`` — these are source-text
assertions of the fix's structural shape rather than a runtime execution.
"""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CHAT_JS = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")
_APP_JS = (_REPO / "static" / "app.js").read_text(encoding="utf-8")


def test_streaming_tts_is_declared_before_the_try_block():
    """`streamingTTS` must be a hoisted `let`, not a `const` declared deep
    inside the try (which would leave it in the temporal dead zone for any
    exception thrown earlier in the try)."""
    assert "let streamingTTS = false;" in _CHAT_JS
    assert "const streamingTTS = " not in _CHAT_JS


def test_streaming_tts_declaration_precedes_its_catch_block_usage():
    decl_idx = _CHAT_JS.index("let streamingTTS = false;")
    catch_idx = _CHAT_JS.index("if (streamingTTS && window.aiTTSManager) window.aiTTSManager.stop();")
    assert decl_idx < catch_idx, (
        "streamingTTS must be declared before the catch block reads it, "
        "otherwise a throw before the assignment re-throws a TDZ "
        "ReferenceError instead of surfacing the real error"
    )


def test_streaming_tts_declaration_precedes_the_chat_stream_fetch():
    """The declaration must sit ahead of the /api/chat_stream fetch (and
    therefore ahead of everything in the surrounding try that could throw
    before the fetch, including the `!res.ok` handling) — not merely ahead
    of the catch block's own usage — so it is never in the TDZ regardless of
    where inside that try an exception originates."""
    submit_idx = _CHAT_JS.index("export async function handleChatSubmit(")
    decl_idx = _CHAT_JS.index("let streamingTTS = false;", submit_idx)
    fetch_idx = _CHAT_JS.index("await fetch(`${API_BASE}/api/chat_stream`", submit_idx)
    assert submit_idx < decl_idx < fetch_idx


def test_form_submit_handler_catches_handle_chat_submit_rejection():
    """static/app.js wires handleChatSubmit as the chat form's onsubmit
    handler; that call must not let a rejection escape uncaught, mirroring
    the existing `.catch(` pattern already used for the queued-send call
    site in chat.js."""
    call_idx = _APP_JS.index("originalSubmit.call(chatModule, e)")
    tail = _APP_JS[call_idx:call_idx + 200]
    assert ".catch(" in tail
