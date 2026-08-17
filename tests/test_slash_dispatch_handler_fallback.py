"""Static regressions for the slash dispatcher's bare-command fallback.

A command with `subs` but no `default` (only `/setup` today) must fall back
to its own top-level handler when no subcommand matches, instead of dying
on "Unknown subcommand". The welcome screen tells first-time users to type
bare `/setup`, so this path is the front door of onboarding.
"""

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SLASH = (_REPO / "static" / "js" / "slashCommands.js").read_text(encoding="utf-8")
_CHAT = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


def test_dispatcher_falls_back_to_top_level_handler_before_unknown_sub():
    block = _between(
        _SLASH,
        "// No matching sub — use default if defined",
        "// Flat command (no subs)",
    )
    default_idx = block.index("cmdDef.default")
    fallback_idx = block.index("typeof cmdDef.handler === 'function'")
    unknown_idx = block.index("Unknown subcommand")
    # Order matters: explicit default first, then the handler fallback,
    # and only then the unknown-subcommand error.
    assert default_idx < fallback_idx < unknown_idx


def test_setup_command_keeps_top_level_handler():
    setup_block = _between(_SLASH, "  setup: {", "  demo: {")
    assert "handler: _cmdSetup" in setup_block
    # No `default:` sub — bare /setup relies on the dispatcher fallback.
    assert "default:" not in setup_block


def test_bare_setup_handler_opens_wizard_for_empty_topic():
    cmd_block = _between(_SLASH, "async function _cmdSetup", "// ── Shortcuts ──")
    assert "_showSetupEndpointGuide" in cmd_block


def test_stream_error_parser_unwraps_fastapi_detail():
    block = _between(_CHAT, "// Parse nested JSON error if present", "// Auto-switch to chat mode")
    assert '"(?:message|detail)"' in block


def test_no_model_help_keeps_typed_message_in_input():
    block = _between(
        _CHAT,
        "const NO_MODEL_CONNECTED_HELP",
        "const _queuedAgentRequests",
    )
    assert "your message is still in the box" in block
    # The no-session branches must not clear the input anymore.
    no_session = _between(_CHAT, "// Auto-create a session using default chat config", "// --- API key guard")
    assert "el('message').value = ''" not in no_session
    assert "NO_MODEL_CONNECTED_HELP" in no_session
