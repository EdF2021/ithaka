"""Static regressions for issue #22 — grounding-bypass on the first send of a
lazily-created ("pending") chat session while the notebook workspace is open.

Rootcause: createDirectChat()/materializePendingSession() in sessions.js never
carried the open workspace's notebook_id into the materialized session, so
chat.js's source_ids were dropped server-side (routes/chat_helpers.py enforces
that source_ids require a notebook-bound session). The fix binds the pending
chat to the open workspace's notebook and adds a fail-closed check in chat.js
as a safety net.

Design note (review round 1): the binding is decided READ-AT-MATERIALIZE,
inside materializePendingSession(), not captured at createDirectChat() time.
A pending chat can outlive a notebook switch or a workspace open/close before
the first send — capture-at-create produced both a silent mis-bind (stale
notebook_id survives a switch) and a false-positive fail-closed block (a
workspace opened after the pending chat was created). Reading live workspace
state at the same instant the fail-closed check reads it eliminates both.

Source-text assertions only, following the precedent set by
test_notebook_workspace_static.py: this repo has no build step and no JS DOM
test runner, so the wiring added to static/js/sessions.js, chat.js and
notebookWorkspace.js can't practically be driven at runtime here.

#112 follow-up: the fail-closed check itself used to call
isNotebookWorkspaceOpen() live at the point it runs — past an
await(materializePendingSession()) — which a caller that legitimately closes
the workspace mid-await (the mindmap-node-click entry point) could bypass,
since the guard would then see "closed" and skip. It now consumes a
synchronous, pre-await snapshot instead; see
test_notebook_workspace_static.py's search_hint/#22 tests.
"""

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SESSIONS = (_REPO / "static" / "js" / "sessions.js").read_text(encoding="utf-8")
_CHAT = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")
_WS = (_REPO / "static" / "js" / "notebookWorkspace.js").read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


# ── notebookWorkspace.js: getCurrentNotebookId accessor ────────────────────


def test_get_current_notebook_id_is_exported():
    assert "export function getCurrentNotebookId" in _WS


def test_get_current_notebook_id_registered_on_window_notebook_workspace():
    exports = _between(_WS, "const notebookWorkspace = {", "\n};")
    assert "getCurrentNotebookId" in exports
    assert "window.notebookWorkspace = notebookWorkspace;" in _WS


# ── sessions.js: createDirectChat no longer captures notebookId (round 1) ──


def test_create_direct_chat_does_not_capture_notebook_id():
    # Capture-at-create was the review-round-1 defect (stale bind after a
    # notebook switch, false-positive block when a workspace opens later).
    fn = _between(_SESSIONS, "export function createDirectChat(", "\n}\n")
    assert "getCurrentNotebookId" not in fn
    assert "notebookId" not in fn
    assert "_pendingChat = { url, modelId, endpointId };" in fn


# ── sessions.js: materializePendingSession reads the bind live and appends
# notebook_id conditionally, at the same instant as the fail-closed check ──


def test_materialize_pending_session_reads_notebook_binding_live():
    fn = _between(
        _SESSIONS,
        "export async function materializePendingSession()",
        "\nexport function hasPendingChat()",
    )
    assert "window.notebookWorkspace?.isNotebookWorkspaceOpen?.()" in fn
    assert "window.notebookWorkspace?.getCurrentNotebookId?.()" in fn
    assert "const nbId =" in fn
    assert "if (nbId) {" in fn
    assert "fd.append('notebook_id', nbId);" in fn
    # The live read must happen before the notebook_id append, and both must
    # come from materialize-time state, not a value carried on `pending`.
    read_idx = fn.index("const nbId =")
    append_idx = fn.index("fd.append('notebook_id', nbId);")
    assert read_idx < append_idx
    assert "pending.notebookId" not in fn


def test_materialize_pending_session_exposes_last_notebook_binding():
    # chat.js needs a way to verify the bind after _pendingChat is cleared
    # (issue #22 fail-closed net) — the getter must be exported and wired
    # into the sessionModule object chat.js actually imports.
    assert "export function getLastMaterializedNotebookId()" in _SESSIONS
    session_module_obj = _between(_SESSIONS, "const sessionModule = {", "\n};")
    assert "getLastMaterializedNotebookId," in session_module_obj


# ── chat.js: fail-closed net in the first-send materialize path ────────────


def test_first_send_fail_closed_blocks_ungrounded_pending_session():
    """#112 update: the guard inside this block no longer re-reads
    isNotebookWorkspaceOpen() live (that live read, past the
    materializePendingSession() await, was itself the bug a mindmap-node
    click could bypass — see test_notebook_workspace_static.py's
    test_chat_js_snapshots_workspace_open_state_before_await). It now
    consumes _nbwsWorkspaceOpenAtSubmit, a synchronous snapshot taken before
    this await, further up in handleChatSubmit."""
    block = _between(
        _CHAT,
        "if (sessionModule.hasPendingChat && sessionModule.hasPendingChat()) {",
        "\n    }\n",
    )
    assert "sessionModule.materializePendingSession()" in block
    assert "_nbwsWorkspaceOpenAtSubmit" in block
    assert "isNotebookWorkspaceOpen" not in block
    assert "getLastMaterializedNotebookId" in block
    assert "chat-error" in block
    assert "_releaseSendFlag();" in block
    assert "return;" in block
