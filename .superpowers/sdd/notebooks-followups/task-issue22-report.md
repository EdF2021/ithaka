# Report — issue #22 grounding-bypass fix

## Status
DONE

## What changed

1. `static/js/notebookWorkspace.js`
   - Added `export function getCurrentNotebookId()` (right after `getSourceIdsForChat`,
     ~line 374): returns `_state.notebook.id` when the workspace is open and a notebook
     is loaded, else `null`.
   - Registered it on the `notebookWorkspace` export object (same block as
     `getSourceIdsForChat`, `isNotebookWorkspaceOpen`), so `window.notebookWorkspace.getCurrentNotebookId`
     works.
   - Kept this to exactly those two additions to avoid colliding with the concurrent
     FILES-row edit happening elsewhere in this file (studio panel, ~line 1171+ —
     untouched, far from my edits at ~372 and ~1361).

2. `static/js/sessions.js`
   - `createDirectChat(url, modelId, endpointId)`: now computes
     `notebookId = isNotebookWorkspaceOpen() ? getCurrentNotebookId() : null` and stores it
     on `_pendingChat` (`{ url, modelId, endpointId, notebookId }`). Updated the stale
     "a pending chat is never notebook-bound yet" comment.
   - `materializePendingSession()`: appends `fd.append('notebook_id', pending.notebookId)`
     when set (mirrors `notebooks.js:402`).
   - Added a small `_lastMaterializedNotebookId` module var + exported
     `getLastMaterializedNotebookId()` getter (wired into the `sessionModule` object chat.js
     imports). This wasn't in the brief's two bullets verbatim, but was needed plumbing:
     `_pendingChat` is nulled at the top of `materializePendingSession`, so chat.js's
     fail-closed check (piece 3) needed *some* signal that survives past that point to know
     whether the just-created session actually got a notebook_id. The getter records
     `pending.notebookId` before the fetch and is reset to `null` on both failure paths
     (network error / non-2xx), so it can't report a stale success after a failed create.

3. `static/js/chat.js` (fail-closed net)
   - After `materializePendingSession()` succeeds in the pending-chat branch (~853), added:
     if `isNotebookWorkspaceOpen()` is true but `getLastMaterializedNotebookId()` is falsy,
     render a `msg msg-ai chat-error` bubble ("Notebook session could not be bound — retry
     from the workspace") into `#chat-history` (same inert-marker pattern as the two existing
     chat-error sites in this file) and abort the send (`_releaseSendFlag(); return;`)
     instead of sending ungrounded.

## Backend
Untouched, as instructed: `routes/chat_helpers.py`, `core/session_manager.py`,
`routes/session_routes.py` have zero diff (verified via `git diff --name-only` against
those paths — empty).

## Tests
- New file `tests/test_notebook_pending_session_static.py` (same `_between`/static-assert
  style as `tests/test_notebook_workspace_static.py`): 6 tests covering
  `getCurrentNotebookId` export + registration, `createDirectChat`'s notebookId capture,
  `materializePendingSession`'s conditional `fd.append`, the `getLastMaterializedNotebookId`
  plumbing, and chat.js's fail-closed block (scoped to the `hasPendingChat()` branch, not
  loose substrings).
- Verified each new test fails without the fix and passes with it (ran full `-k "notebook or
  session"` suite both stashed and unstashed).
- `tests/test_routes_notebook_chat.py` and `tests/test_rag_source_filter.py`: unchanged,
  still green (pinned per brief).

### Test runs
```
pytest tests/test_notebook_pending_session_static.py tests/test_notebook_workspace_static.py -q
26 passed

pytest -k "notebook or session" -q
462 passed, 1 failed (tests/test_session_list_owner_scope.py::test_list_sessions_excludes_other_users_sessions)
```
The one failure is **pre-existing and unrelated**: confirmed by `git stash`-ing this diff
entirely and re-running the same `-k` selection — it fails identically with or without my
changes (also passes in isolation, `pytest tests/test_session_list_owner_scope.py -q` →
2 passed; only flakes when run alongside the wider `-k` selection, an existing
cross-test-isolation issue, not a regression from this fix).

```
node --check static/js/sessions.js static/js/chat.js static/js/notebookWorkspace.js
→ no output, exit 0
```

## Skipped per brief's explicit allowance

**Route-level round-trip test for `POST /api/session` with `notebook_id`** — the brief said
to add this only if an existing test-setup for `session_routes` makes it light, otherwise
skip and report. Checked `tests/test_session_list_owner_scope.py`,
`tests/test_history_compact_tool_calls.py`, `tests/test_archived_sessions_model_filter.py`:
none of them exercise `POST /api/session` (`create_session`) — only GET/PATCH/auto-sort
endpoints via `setup_session_routes(sm, {})` with a `MagicMock` `SessionManager`. Standing
up a working `create_session` call directly would additionally require handling
`_reject_raw_endpoint_url_for_non_admin` (auth/admin branching), `fire_event("session_created",
...)` (which does `asyncio.run(...)` when no loop is running — touches the real event/task
counters), and the endpoint-validation/webhook branches — none of which have existing
lightweight test scaffolding in this repo. Given the brief's explicit "anders overslaan en
melden" (otherwise skip and report), I skipped it rather than building a bespoke harness.
The static JS tests plus the untouched `tests/test_routes_notebook_chat.py` /
`tests/test_rag_source_filter.py` already pin the backend contract this fix depends on.

## Concerns
- No UI smoke test was run (no browser tooling used in this task per the delegation — this
  was a backend-adjacent JS-only fix executed headless via pytest + `node --check`). The
  controller/reviewer should browser-verify: open a notebook workspace, click a model to
  start a "New Chat" (pending session), send the first message, and confirm the response is
  grounded with `[n]` citations (not a generic ungrounded answer) — this is the actual issue
  #22 repro.
- The `getLastMaterializedNotebookId` addition is plumbing beyond the brief's literal two
  sessions.js bullets, but was necessary to implement bullet 3 (chat.js fail-closed check) at
  all, given `_pendingChat` is nulled before the network call. Flagged explicitly above.
