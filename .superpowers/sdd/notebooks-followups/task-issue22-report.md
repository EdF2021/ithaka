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
- **Superseded by fix round 1 below**: the capture-at-create design flagged as a concern-free
  implementation detail above turned out to have a real staleness defect. See the fix-round
  section.

## Fix round 1 (review finding: capture-at-create staleness — design amended to read-at-materialize)

Coordinator ruling: the review (`review-issue22-findings.md`) confirmed the implementation
matched the brief line-for-line, including the brief's prescribed capture-at-create expression
(brief line 13) — so this was **not an implementer deviation**, it was a soundness defect in
the brief's own design. Ruling: amend the design rather than re-implement to the (defective)
brief.

**The defect:** `createDirectChat()` captured `notebookId` once, at pending-chat creation time.
`createDirectChat` is a generic "start a new chat" entry point (called from `modelPicker.js`,
`gallery.js`, `documentLibrary.js`, `slashCommands.js`, `models.js`, `cookbookRunning.js`), and
the notebook workspace stays open across notebook switches (`_state.notebook` changes,
`_open` doesn't). Two concrete failure modes followed:
- Silent mis-bind: open workspace on notebook A -> start pending chat (captures A) -> switch
  workspace to notebook B -> send -> session gets bound to stale `notebook_id=A` while the UI
  shows B's sources. The fail-closed check didn't catch this because `getLastMaterializedNotebookId()`
  was truthy (just the *wrong* notebook).
- False-positive block: start a pending chat while the workspace is closed (`notebookId=null`,
  correctly) -> open a notebook workspace before sending -> the fail-closed guard now fires and
  blocks a send that was never meant to be notebook-bound.

Root cause: the bind decision (sessions.js, capture-at-create) and the fail-closed check
(chat.js, read-at-send) used two different points in time as ground truth.

**Fix:** moved the bind decision into `materializePendingSession()` itself, reading live
`window.notebookWorkspace` state (`isNotebookWorkspaceOpen()` + `getCurrentNotebookId()`) at
the exact moment the FormData is built — the same instant `_lastMaterializedNotebookId` is
recorded for chat.js's fail-closed check to read. Both consumers now agree on one ground-truth
instant, eliminating the staleness window entirely.

Changes:
- `static/js/sessions.js`:
  - `createDirectChat()`: reverted to `_pendingChat = { url, modelId, endpointId }` (no
    `notebookId` field); reverted the "never notebook-bound yet" header comment back to its
    original meaning (updated wording to note the bind is decided later).
  - `materializePendingSession()`: now computes `const nbId = window.notebookWorkspace?.isNotebookWorkspaceOpen?.() ? (window.notebookWorkspace?.getCurrentNotebookId?.() || null) : null;`
    right before building the FormData, appends `notebook_id` conditionally on `nbId` (not
    `pending.notebookId`), and sets `_lastMaterializedNotebookId = nbId` from that same read.
- `static/js/chat.js`: **no change** — the fail-closed check already read
  `sessionModule.getLastMaterializedNotebookId()`, which now carries the corrected
  read-at-materialize value automatically. Verified via `git diff` that chat.js has zero diff
  in this round.
- `static/js/notebookWorkspace.js`: **no change** — `getCurrentNotebookId()` itself was never
  the defect (review confirmed it clean); only its call site moved.
- `tests/test_notebook_pending_session_static.py`: replaced the two sessions.js tests —
  `test_create_direct_chat_captures_notebook_id_from_open_workspace` became
  `test_create_direct_chat_does_not_capture_notebook_id` (asserts `createDirectChat`'s body has
  no `getCurrentNotebookId`/`notebookId` reference and the bare 3-field `_pendingChat` literal);
  `test_materialize_pending_session_appends_notebook_id_conditionally` became
  `test_materialize_pending_session_reads_notebook_binding_live` (asserts the live
  `isNotebookWorkspaceOpen?.()`/`getCurrentNotebookId?.()` read, the `const nbId =` assignment,
  the conditional `fd.append('notebook_id', nbId)`, that the read happens before the append
  (index comparison), and that `pending.notebookId` is no longer referenced anywhere in the
  function). Docstring updated with a design note. The `getLastMaterializedNotebookId`
  export/wiring test and the chat.js fail-closed test were left unchanged (both still describe
  the current, correct code).

### Test runs (fix round 1)
```
pytest tests/test_notebook_pending_session_static.py tests/test_notebook_workspace_static.py -q
26 passed

pytest -k "notebook or session" -q
462 passed, 1 failed (same pre-existing test_session_list_owner_scope.py flake, unrelated —
unchanged from round 0)

node --check static/js/sessions.js static/js/chat.js static/js/notebookWorkspace.js
exit 0
```

### Concerns (fix round 1)
- Still no browser smoke test run. The review explicitly named "switch notebooks mid-pending-chat"
  and "open workspace after starting a generic new chat" as the scenarios that would have
  surfaced this defect earlier — worth a real browser check before merge (open workspace on
  notebook A, start new chat, switch to notebook B in the workspace, send, verify the created
  session's `notebook_id` is B not A; and the closed-workspace-then-opened false-positive path).
