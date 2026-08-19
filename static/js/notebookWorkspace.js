/**
 * Notebook workspace — NotebookLM-style 3-panel fullscreen shell around the
 * existing chat.
 *
 * CSS-primary, deliberately: #chat-container (index.html) is NEVER moved or
 * reparented. Opening the workspace only (a) adds body class
 * `notebook-workspace-open`, which is what actually drives the layout via
 * style.css (chat gets margin-left/margin-right + padding-top, the sidebar
 * hides), and (b) shows/fills the `#nbws-root` chrome that index.html already
 * ships — hidden by default — with a fixed topbar and two <aside> panels
 * (`#nbws-sources` / `#nbws-studio`). This module owns only that chrome and
 * the open/close lifecycle; it never touches #chat-container directly.
 *
 * Session selection reuses notebooks.js's find-or-create logic
 * (`openOrCreateSessionForNotebook`) via a dynamic import (see
 * `_importNotebooks` below for why no window-singleton check is needed here).
 * notebooks.js itself opens this module the other way around — grid-card
 * clicks there prefer `window.notebookWorkspace` (published at the bottom of
 * this file) and fall back to a dynamic import, the same handoff shape its
 * own `_openArtifact` uses for document.js. A failed session-resolve leaves
 * the workspace closed and the notebooks-picker modal open, with the error
 * reported into it via notebooks.js's exported showListError — never a
 * silently-swallowed failure that opens an empty shell anyway.
 *
 * Panel bodies (#nbws-sources-body / #nbws-studio-body) are intentionally
 * left empty here: Task 4 fills sources, Task 5 fills the session
 * picker/chips, Task 6 fills studio (and registers its podcast-poll-stop via
 * registerCloseHook below). `_state` is the shared read/write surface those
 * tasks build on instead of inventing a second source of truth.
 */

let _open = false;
// Bumped on every open/close; a slow session-resolve await (openNotebookWorkspace
// calls out to notebooks.js and awaits a fetch) can be overtaken by a close (or a
// second open) that starts and finishes first — checking the epoch after the
// await keeps a stale open from clobbering the more recent state.
let _openEpoch = 0;
let _escHandler = null;

// Teardown steps later tasks need on close (Task 6: stop the podcast poll)
// register here instead of editing closeNotebookWorkspace directly. Each hook
// runs with no arguments; a throwing hook is swallowed so it can't block the
// others or leave the workspace stuck open.
const _closeHooks = [];

/**
 * Register a function to run on every closeNotebookWorkspace(). Returns an
 * unregister function the caller should invoke from its own teardown (e.g. a
 * re-render that re-registers) so the array never accumulates stale/duplicate
 * entries — same contract as escMenuStack.js's registerMenuDismiss.
 */
export function registerCloseHook(fn) {
  if (typeof fn !== 'function') return () => {};
  _closeHooks.push(fn);
  return () => {
    const i = _closeHooks.indexOf(fn);
    if (i !== -1) _closeHooks.splice(i, 1);
  };
}

// Shared panel state for Task 4 (sources) / Task 5 (session chips) / Task 6
// (studio artifacts) to read and write without inventing a second source of
// truth. `selection` is a Set of source ids currently checked in the sources
// panel (feeds source_ids on the notebook chat request).
const _state = {
  notebook: null,
  sources: [],
  selection: new Set(),
};

function _root() {
  return document.getElementById('nbws-root');
}

// Whether ANY .modal is currently visible — used so Escape closes the
// workspace only when nothing is layered above it. `#nbws-root` is
// deliberately not itself a `.modal` (it's fixed chrome around the chat, not
// a dialog), so it isn't covered by ui.js's global capture-phase Escape
// arbiter, which only ever looks at `.modal` elements.
function _isModalOpen() {
  return [...document.querySelectorAll('.modal')].some(m =>
    !m.classList.contains('hidden') && getComputedStyle(m).display !== 'none');
}

function _bindEscape() {
  if (_escHandler) return;
  _escHandler = (e) => {
    if (e.key !== 'Escape' || e.defaultPrevented) return;
    if (_isModalOpen()) return;
    closeNotebookWorkspace();
  };
  document.addEventListener('keydown', _escHandler);
}

function _unbindEscape() {
  if (!_escHandler) return;
  document.removeEventListener('keydown', _escHandler);
  _escHandler = null;
}

// Collapsing a panel does NOT hide its header row — only style.css's
// `.nbws-collapsed` rule narrows the <aside> to a slim strip that still
// shows the collapse/expand button (title text hides at that width). The
// button that triggers the collapse lives inside the very element being
// collapsed, so it must stay reachable there for the toggle to round-trip —
// zeroing the whole aside (an earlier version of this rule did) traps the
// panel closed with no way back on desktop.
const _PANEL_LABELS = {
  sources: { collapse: 'Collapse sources', expand: 'Expand sources' },
  studio: { collapse: 'Collapse studio', expand: 'Expand studio' },
};

function _setCollapseButtonState(btn, collapsed, labels) {
  if (!btn) return;
  const label = collapsed ? labels.expand : labels.collapse;
  btn.title = label;
  btn.setAttribute('aria-label', label);
  btn.setAttribute('aria-expanded', String(!collapsed));
}

function _toggleCollapse(which) {
  const panelId = which === 'sources' ? 'nbws-sources' : 'nbws-studio';
  const btnId = which === 'sources' ? 'nbws-sources-collapse' : 'nbws-studio-collapse';
  const bodyClass = which === 'sources' ? 'nbws-sources-collapsed' : 'nbws-studio-collapsed';
  const panel = document.getElementById(panelId);
  if (!panel) return;
  const collapsed = panel.classList.toggle('nbws-collapsed');
  document.body.classList.toggle(bodyClass, collapsed);
  _setCollapseButtonState(document.getElementById(btnId), collapsed, _PANEL_LABELS[which]);
}

// Reset one panel to its default expanded state — used on close so the next
// open always starts with both panels showing.
function _resetPanel(which) {
  const panelId = which === 'sources' ? 'nbws-sources' : 'nbws-studio';
  const btnId = which === 'sources' ? 'nbws-sources-collapse' : 'nbws-studio-collapse';
  document.getElementById(panelId)?.classList.remove('nbws-collapsed');
  _setCollapseButtonState(document.getElementById(btnId), false, _PANEL_LABELS[which]);
}

// Wired once (guarded by a dataset flag) — #nbws-root is static chrome in
// index.html that persists across opens/closes, so listeners must not stack.
function _wireChrome() {
  const root = _root();
  if (!root || root.dataset.nbwsWired === '1') return;
  root.dataset.nbwsWired = '1';

  document.getElementById('nbws-back-btn')
    ?.addEventListener('click', closeNotebookWorkspace);
  document.getElementById('nbws-sources-collapse')
    ?.addEventListener('click', () => _toggleCollapse('sources'));
  document.getElementById('nbws-studio-collapse')
    ?.addEventListener('click', () => _toggleCollapse('studio'));
}

// notebooks.js carries no <script> tag of its own (see app.js's rail-notebooks
// click handler) and publishes no window singleton — it's always reached via
// dynamic import, including from its own grid-card click handler that calls
// openNotebookWorkspace() in the first place. That means by the time this
// runs, notebooks.js is already the loaded module executing the call; this
// import just resolves the browser's cached module record for it (same
// specifier, same URL) rather than re-fetching or re-executing anything.
async function _importNotebooks() {
  const imported = await import('./notebooks.js');
  return (imported && imported.default) || imported;
}

async function _openImpl(nb) {
  if (!nb) return;
  const root = _root();
  if (!root) return;

  _openEpoch++;
  const epoch = _openEpoch;

  // notebooks.js is where the grid-card click that led here lives, so it's
  // already loaded — this resolves the cached module record, not a fresh
  // fetch/execute (see _importNotebooks above). A genuinely failed import
  // (e.g. the module itself is broken) leaves nothing to report an error
  // into either, so just bail without touching the still-open modal.
  let notebooksMod;
  try {
    notebooksMod = await _importNotebooks();
  } catch (_) {
    return;
  }
  notebooksMod?.showListError?.('');

  // Resolve/select the bound session BEFORE the notebooks-picker modal
  // closes and BEFORE the body class flips — load-then-open, never
  // open-then-load, same posture as notebooks.js's own _openChat/
  // _openArtifact. On failure the workspace must NOT open and the picker
  // modal must NOT close: report the error into the list view's
  // #notebook-list-error (the view actually showing behind the modal at
  // this point — the grid, not the detail view) via notebooks.js's exported
  // showListError, exactly how _openChat reports into #notebook-detail-error
  // on its own failure path.
  try {
    if (typeof notebooksMod?.openOrCreateSessionForNotebook === 'function') {
      await notebooksMod.openOrCreateSessionForNotebook(nb);
    }
  } catch (e) {
    if (epoch === _openEpoch) {
      notebooksMod?.showListError?.(`Could not open chat (${e.message})`);
    }
    return;
  }

  // A newer open() or a close() finished first while we were awaiting above
  // — don't clobber it with stale state, and don't close a modal a fresher
  // call may already be relying on staying open.
  if (epoch !== _openEpoch) return;

  _state.notebook = nb;
  _state.sources = [];
  _state.selection = new Set();

  const nameEl = document.getElementById('nbws-notebook-name');
  if (nameEl) nameEl.textContent = nb.name || '(untitled)';

  _wireChrome();
  _open = true;
  document.body.classList.add('notebook-workspace-open');
  root.removeAttribute('aria-hidden');
  _bindEscape();

  if (notebooksMod?.isNotebooksOpen?.()) notebooksMod.closeNotebooks();
}

/** Open the workspace for notebook `nb` ({id, name, ...} from the notebooks list/API). */
export function openNotebookWorkspace(nb) {
  return _openImpl(nb);
}

/**
 * Close the workspace: drop the body class (chat reflows back to full width
 * via style.css, unchanged from before the workspace ever opened), run every
 * registered close hook (Task 6's podcast-poll-stop lands here), and leave
 * the active chat session exactly as-is — closing the workspace is a view
 * change, not a navigation-away.
 */
export function closeNotebookWorkspace() {
  if (!_open) return;
  _openEpoch++;
  _open = false;

  document.body.classList.remove(
    'notebook-workspace-open',
    'nbws-sources-collapsed',
    'nbws-studio-collapsed'
  );
  const root = _root();
  if (root) {
    // A collapse/back button inside #nbws-root can still hold focus at this
    // point (e.g. the button that was just clicked to trigger this close) —
    // setting aria-hidden on an ancestor of the focused element is invalid
    // (browsers log a warning and refuse to apply it), so drop focus out of
    // the subtree first.
    if (root.contains(document.activeElement)) document.activeElement.blur();
    root.setAttribute('aria-hidden', 'true');
  }
  _resetPanel('sources');
  _resetPanel('studio');

  _unbindEscape();

  for (const hook of _closeHooks) {
    try { hook(); } catch (_) {}
  }
}

export function isNotebookWorkspaceOpen() {
  return _open;
}

const notebookWorkspace = {
  openNotebookWorkspace,
  closeNotebookWorkspace,
  isNotebookWorkspaceOpen,
  registerCloseHook,
  _state,
};

export default notebookWorkspace;
window.notebookWorkspace = notebookWorkspace;
