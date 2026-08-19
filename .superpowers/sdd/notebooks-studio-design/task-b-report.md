# Task B report — studio panel redesign (Generate / Files)

## What

Redesigned the notebooks Studio panel so it reads as two distinct sections instead of
one undifferentiated row of look-alike elements:

- **Generate** — the 6 generator buttons (5 artifact kinds + podcast), each with an
  inline monochrome plus-icon SVG, English label, laid out in a 2-column grid so all 6
  sit tidily in the 320px panel.
- **Files** — the generated-artifact list. Each row now carries a small file-type SVG
  icon, kind pill, title (falls back to the kind label — no `title` field exists on
  `NotebookArtifact.to_dict()`, confirmed in `core/database.py`), the existing short
  date (`created_at`, already wired via `_shortDate`), and a secondary "Open source
  document" icon button. Empty state: "Nothing generated yet — use Generate above."

Row click behavior (non-podcast, non-mindmap) now opens Task A's visual report in a new
tab: `window.open('${API_BASE}/api/notebooks/<id>/artifacts/<id>/report', '_blank')`.
Mindmap keeps its existing preview as the primary click. Every non-podcast row also gets
a secondary icon button that always routes through the unchanged `_openArtifact`
doc-viewer path (workspace stays open, per the existing controller ruling).

All Dutch strings inside the studio-panel/artifact code were swept to English:
`KIND_LABELS.study_guide` ("Studiegids" → "Study guide"), the mindmap hint, the
"Genereren…" button label, and the podcast phase/error strings ("Script schrijven…",
"Audio genereren…", "Samenvoegen…", "Bezig…", "Generatie afgebroken…", "Podcast
mislukt…"). Nothing outside the studio/artifact code was touched.

## Files changed

- `static/js/notebookWorkspace.js` — `KIND_LABELS`, new `_PLUS_ICON`/`_FILE_ICON`/
  `_DOC_ICON` constants, `_artifactRow`, `_loadArtifacts` click wiring, new
  `_openArtifactReport`, `_studioPanelSkeleton` (two headed sections), English string
  sweep.
- `static/style.css` — new rules inside the nbws block (before `/* nbws-end */`):
  `.nbws-studio-section(-head)`, 2-column `.notebook-artifact-btns` grid,
  `.notebook-artifact-file-icon`, `.notebook-artifact-opendoc`, and shared
  `.notebook-artifact-gen-btn`/`.notebook-podcast-gen-btn` icon-color/layout rules.
- `tests/test_notebook_workspace_static.py` — 3 new tests (Task B section).

## TDD

RED (before implementation):
```
FAILED tests/test_notebook_workspace_static.py::test_studio_panel_has_generate_and_files_section_headers
FAILED tests/test_notebook_workspace_static.py::test_artifact_row_click_opens_visual_report_in_new_tab
FAILED tests/test_notebook_workspace_static.py::test_studio_panel_labels_are_english_not_dutch
3 failed, 15 passed
```

GREEN (after implementation):
```
.venv/bin/python -m pytest tests/test_notebook_workspace_static.py -q
18 passed, 1 warning in 0.10s
```

Broader sanity sweep:
```
.venv/bin/python -m pytest -k notebook -q
247 passed, 4667 deselected, 1 warning in 7.88s
```

## node --check

```
node --check static/js/notebookWorkspace.js
OK
```

## Browser verification (worktree smoke instance, port 7004)

Fresh `ITHAKA_DATA_DIR`, admin account created via `/api/auth/setup`, logged in via
chrome-devtools MCP, created a real "Task B smoke notebook".

Desktop (default viewport):
- Studio panel renders two clearly separated headed sections: GENERATE (2-col grid of
  6 plus-icon buttons) and FILES (empty-state message). Screenshot:
  `/tmp/.../scratchpad/taskb-desktop-studio.png`.
- Mocked `fetch('/artifacts')` to return two fake rows (kind `faq`, kind `mindmap`) and
  reopened the workspace to force a fresh `_loadArtifacts()` render. Rows show the file
  icon, kind pill, title, date, "Open source document" button, delete button.
  Screenshot: `/tmp/.../scratchpad/taskb-desktop-files-rows.png`.
- Clicked the FAQ row body: `window.open` fired exactly once with
  `http://127.0.0.1:7004/api/notebooks/<id>/artifacts/art-faq-1/report`, `_blank` — the
  Task A contract. Confirmed via `window.__openCalls`.
- Clicked the Mindmap row body: `window.open` call count stayed at 1 (no report call);
  it went through `_openArtifact` instead — console showed the expected "Document not
  found" (fake docId) and a "Document not found" toast, workspace stayed open.
- Clicked "Open source document" on the FAQ row: `window.open` count stayed at 1,
  confirming it also routes through `_openArtifact`, not the report tab.
- **Bug found and fixed during self-review**: the podcast generate button had briefly
  gained the `.notebook-artifact-gen-btn` class (added purely for the plus-icon/grid
  CSS) — but `_wireStudioPanel` wires click handlers by querying that exact class, so
  the podcast button would have double-fired (`_generateArtifact('podcast', …)` in
  addition to the dedicated `_generatePodcast` handler), POSTing an invalid
  `kind=podcast` to `/artifacts`. Fixed by reverting the podcast button's class list
  (kept `.notebook-podcast-gen-btn` only) and instead extending the CSS selectors to
  cover both classes. Re-verified live: `document.querySelectorAll('.notebook-artifact-gen-btn').length === 5`,
  podcast button `classList` no longer contains that class, and clicking the podcast
  button now fires exactly one POST, to `/podcast` (verified via a mocked-fetch capture
  of `window.__postCalls`).

Mobile (360×740, `emulate({viewport:"360x740x2,mobile,touch"})`):
- Opened the notebook workspace, switched to the Studio tab. Both sections render
  full-width, cleanly separated, all 6 Generate buttons and the Files empty-state text
  fit and are readable/tappable. Screenshot: `/tmp/.../scratchpad/taskb-mobile-studio.png`.

Screenshot paths (session scratchpad, not committed):
- `/tmp/claude-1000/-home-eddef-projects-ithaka/e63be040-0811-4a76-95c3-3f72b2163bd8/scratchpad/taskb-desktop-studio.png`
- `/tmp/claude-1000/-home-eddef-projects-ithaka/e63be040-0811-4a76-95c3-3f72b2163bd8/scratchpad/taskb-desktop-files-rows.png`
- `/tmp/claude-1000/-home-eddef-projects-ithaka/e63be040-0811-4a76-95c3-3f72b2163bd8/scratchpad/taskb-desktop-after-fix.png`
- `/tmp/claude-1000/-home-eddef-projects-ithaka/e63be040-0811-4a76-95c3-3f72b2163bd8/scratchpad/taskb-mobile-studio.png`

## Self-review

- Reviewed the full `git diff` for both changed files line by line before finalizing.
- Caught and fixed the podcast-button double-wiring bug described above — this was a
  real functional regression that pure CSS/visual review would not have surfaced;
  found it by re-reading the click-wiring code path after making the class-list change.
- `_shortDate`/`created_at` were already wired pre-existing (Task 6); no new timestamp
  invented — confirmed against `NotebookArtifact.to_dict()` in `core/database.py`
  (`created_at` is present, `title` is not, hence the label fallback).
- No Unicode emoji introduced; all new icons are inline monochrome SVG matching the
  existing `_CLOSE_ICON` convention (1px stroke, `currentColor`).
- Only existing CSS variables used (`--panel`, `--border`, `--fg`, `--red`,
  `--color-muted`); new CSS lives inside the nbws block, before `/* nbws-end */` is
  preserved as the last line of the file.
- `#nbws-artifact-error` now sits inside the Generate section (unchanged id, only its
  DOM position moved) — delete-artifact errors from the Files section still render
  there; a minor pre-existing UX shape, not something Task B's brief asked to change.

## Concerns

- Task A's `/report` endpoint does not exist in this worktree (as expected — it's the
  other task, built in a separate worktree) so the new-tab open reliably 404s in this
  environment; verified the *call*, not the response, per the brief.
- `NotebookArtifact` has no `title` column, so every row's title currently falls back
  to the kind label (e.g. "FAQ" shown twice — once as kind pill, once as title). This
  is pre-existing (not introduced by Task B) but is now more visible with the file icon
  present. Flagging in case Task A's `/artifacts` POST or PATCH surface sets a
  human-readable title later — the row markup already supports `a.title` when present.
