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
 * own `_openArtifact` uses for document.js.
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

/** Register a function to run on every closeNotebookWorkspace(). */
export function registerCloseHook(fn) {
  if (typeof fn === 'function') _closeHooks.push(fn);
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

function _toggleCollapse(panelId, bodyClass) {
  const panel = document.getElementById(panelId);
  if (!panel) return;
  const collapsed = panel.classList.toggle('nbws-collapsed');
  document.body.classList.toggle(bodyClass, collapsed);
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
    ?.addEventListener('click', () => _toggleCollapse('nbws-sources', 'nbws-sources-collapsed'));
  document.getElementById('nbws-studio-collapse')
    ?.addEventListener('click', () => _toggleCollapse('nbws-studio', 'nbws-studio-collapsed'));
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

// Resume/create the session bound to this notebook, exactly like the detail
// view's "Open chat" button — reused (not duplicated) from notebooks.js via
// its exported openOrCreateSessionForNotebook.
async function _selectSessionForNotebook(nb) {
  const mod = await _importNotebooks();
  if (mod && typeof mod.openOrCreateSessionForNotebook === 'function') {
    await mod.openOrCreateSessionForNotebook(nb);
  }
}

async function _closeNotebooksModal() {
  try {
    const mod = await _importNotebooks();
    if (mod && mod.isNotebooksOpen && mod.isNotebooksOpen()) mod.closeNotebooks();
  } catch (_) {
    // Best-effort only — a missing/broken notebooks module must not stop the
    // workspace itself from opening.
  }
}

async function _openImpl(nb) {
  if (!nb) return;
  const root = _root();
  if (!root) return;

  _openEpoch++;
  const epoch = _openEpoch;

  _state.notebook = nb;
  _state.sources = [];
  _state.selection = new Set();

  const nameEl = document.getElementById('nbws-notebook-name');
  if (nameEl) nameEl.textContent = nb.name || '(untitled)';

  // Resolve/select the bound session BEFORE the notebooks-picker modal
  // closes and BEFORE the body class flips — load-then-close (never
  // close-then-load), same posture as notebooks.js's own _openChat/
  // _openArtifact, so a failure here still has somewhere visible to report
  // to instead of dropping the user into an empty shell.
  try {
    await _selectSessionForNotebook(nb);
  } catch (_) {
    // Swallow: an unresolved session isn't fatal here — the workspace still
    // opens around whatever chat state is already current. Every fetch in
    // this feature degrades on its own rather than blocking the rest of the
    // view (same rule notebooks.js follows throughout).
  }

  // A newer open() or a close() finished first while we were awaiting above
  // — don't clobber it with stale state.
  if (epoch !== _openEpoch) return;

  _wireChrome();
  _open = true;
  document.body.classList.add('notebook-workspace-open');
  root.removeAttribute('aria-hidden');
  _bindEscape();

  await _closeNotebooksModal();
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
  document.getElementById('nbws-sources')?.classList.remove('nbws-collapsed');
  document.getElementById('nbws-studio')?.classList.remove('nbws-collapsed');

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
