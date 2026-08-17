"""Regressions for the bare-`/setup` front door and chat error rendering.

The welcome screen tells first-time users to type bare `/setup`; it must
open the guided wizard, never "Unknown subcommand". HTTP error bodies
(FastAPI `{"detail": ...}`) must render as readable text, not raw JSON.

Where possible these run the actual extracted JS in node (repo pattern,
see test_slash_setup_provider_aliases.py). The remaining source-text
assertions cover dispatcher wiring that cannot practically run outside
the full DOM app (COMMANDS references dozens of DOM-bound handlers);
they anchor on structural code, not comments, per TESTING_STANDARD.md.
"""

import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SLASH = (_REPO / "static" / "js" / "slashCommands.js").read_text(encoding="utf-8")
_CHAT = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


def _extract_function(src: str, name: str, end_marker: str) -> str:
    """Extract `function name(...) {...}` up to a unique marker after it.

    Brace counting breaks on braces inside string/regex literals, so the
    caller supplies a structural end anchor (the next declaration).
    """
    start = src.index(f"function {name}(")
    end = src.index(end_marker, start)
    return src[start:end]


def test_setup_routes_bare_and_unknown_topics_to_cmdsetup():
    # Bare `/setup` relies on the per-command default-sub mechanism (the
    # same one chats/toggle/memory/rag use), pointing at the wizard sub.
    setup_block = _between(_SLASH, "  setup: {", "  demo: {")
    assert "default: 'wizard'" in setup_block
    assert "wizard:" in setup_block
    assert "handler: _cmdSetup" in setup_block


def test_bare_setup_handler_opens_wizard_for_empty_topic():
    cmd_block = _extract_function(_SLASH, "_cmdSetup", "// ── Shortcuts ──")
    assert "_showSetupEndpointGuide" in cmd_block


def test_error_body_parser_behaviour():
    """Run the real _parseErrorBodyMessage in node against the body shapes
    seen in production: plain detail, detail with escaped nested JSON,
    422 list detail, provider message nesting, and non-JSON text."""
    fn = _extract_function(_CHAT, "_parseErrorBodyMessage", "// Shown when a message is sent")
    script = fn + r"""
function assert(cond, msg) { if (!cond) throw new Error(msg); }
// FastAPI HTTPException — plain string detail
assert(_parseErrorBodyMessage('{"detail":"Selected model endpoint was removed. Pick another model in Settings."}')
  === 'Selected model endpoint was removed. Pick another model in Settings.', 'plain detail failed');
// detail containing escaped nested provider JSON must come back intact
const nested = JSON.stringify({detail: 'Upstream http://prov -> 429: {"error":{"message":"rate limited"}}'});
assert(_parseErrorBodyMessage(nested) === 'Upstream http://prov -> 429: {"error":{"message":"rate limited"}}',
  'escaped nested detail failed');
// 422 validation errors: detail is a list -> no readable string, but never raw JSON
assert(_parseErrorBodyMessage('{"detail":[{"loc":["body","x"],"msg":"field required"}]}') === null,
  '422 list detail should return null');
// provider nesting under error.message
assert(_parseErrorBodyMessage('{"error":{"message":"invalid api key"}}') === 'invalid api key',
  'error.message failed');
// short non-JSON text passes through; long or JSON-ish garbage does not
assert(_parseErrorBodyMessage('Service Unavailable') === 'Service Unavailable', 'plain text failed');
assert(_parseErrorBodyMessage('{"unknown":true}') === null, 'unparseable JSON should return null');
"""
    subprocess.run(["node", "-e", script], check=True)


def test_no_model_help_keeps_typed_message_in_input():
    help_fn = _extract_function(_CHAT, "_noModelConnectedHelp", "const _queuedAgentRequests")
    assert "your message is still in the box" in help_fn
    assert "Ask an admin" in help_fn  # role-aware branch
    # The no-session branches must not clear the input anymore.
    no_session = _between(_CHAT, "let dc = null;", "// --- API key guard")
    assert "el('message').value = ''" not in no_session
    assert "_noModelConnectedHelp()" in no_session
