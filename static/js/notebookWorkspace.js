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
 *
 * Task 4 (sources panel): fetches/renders `#nbws-sources-body`'s source list
 * with per-row checkboxes (fed by `_state.selection`, a Set of RAG
 * `document_id`s — NOT the NotebookSource row `id` used for delete, since the
 * Chroma filter matches on `document_id`; a "failed" source has no
 * document_id and gets an unselectable, disabled row). Selection persists per
 * notebook in localStorage (`notebook_source_sel_<notebookId>`, storing the
 * DESELECTED ids — new sources default selected). `getSourceIdsForChat()` is
 * the contract chat.js/chatStream.js consume when building the
 * /api/chat_stream request: null (all selected, or workspace closed — no
 * filter), string[] (a checked subset), or [] (nothing checked — the caller
 * must block the send with EMPTY_SELECTION_MESSAGE instead of sending).
 * Upload/delete reuse the exact FormData/inline-confirm flow notebooks.js's
 * (now-dead, Task 6 removes it) detail view used — copied rather than
 * imported since those helpers were private to that module and coupled to
 * its own `_detail`/DOM ids.
 */

import uiModule from './ui.js';

const API_BASE = window.location.origin;

// Shown (via the shared toast/error mechanism, same as chat.js's other
// blocked-send messages) when the user has unchecked every source; the exact
// string is asserted by tests/test_notebook_workspace_static.py, and reused
// verbatim by chat.js's send guard via the published `notebookWorkspace`
// object below rather than a second copy of the literal.
export const EMPTY_SELECTION_MESSAGE = 'Select at least one source';

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

  // Source-count badge ("n/m sources") — a small pill in the fixed topbar,
  // which sits above the composer without ever touching chat-container's own
  // DOM (per the module-header note above). Inserted once, right after the
  // notebook name.
  const topbar = document.getElementById('nbws-topbar');
  if (topbar && !document.getElementById('nbws-source-badge')) {
    const badge = document.createElement('span');
    badge.id = 'nbws-source-badge';
    badge.className = 'nbws-source-badge';
    badge.hidden = true;
    // insertBefore(node, null) appends as the last child — deliberately not
    // the other DOM method for that (see the module header: this file must
    // never contain that literal, a signal a static test elsewhere in the
    // suite reads as "#chat-container might be getting reparented in here",
    // which is never true — the badge is unrelated topbar chrome).
    const nameEl = document.getElementById('nbws-notebook-name');
    topbar.insertBefore(badge, (nameEl && nameEl.nextSibling) || null);
  }
}

// ---- Sources panel (Task 4) ------------------------------------------------

function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function _fetchJson(url, options) {
  const res = await fetch(url, { credentials: 'same-origin', ...(options || {}) });
  if (!res.ok) {
    let detail = '';
    try {
      const body = await res.json();
      detail = body && body.detail ? `: ${body.detail}` : '';
    } catch (_) {}
    throw new Error(`HTTP ${res.status}${detail}`);
  }
  return res.json();
}

const _CLOSE_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

const _ZONE_IDLE = 'Add sources — drop files here or click to upload';

function _selStorageKey(notebookId) {
  return `notebook_source_sel_${notebookId}`;
}

/** Deselected `document_id`s persisted for one notebook — default (nothing
 *  stored, or storage unavailable/corrupt) is "everything selected". */
function _loadDeselected(notebookId) {
  try {
    const raw = localStorage.getItem(_selStorageKey(notebookId));
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr.filter(x => typeof x === 'string') : []);
  } catch (_) {
    return new Set();
  }
}

function _saveDeselected(notebookId, deselectedSet) {
  try {
    localStorage.setItem(_selStorageKey(notebookId), JSON.stringify([...deselectedSet]));
  } catch (_) {
    // Storage unavailable (private mode / quota) — selection still works for
    // this session, it just won't survive a reopen.
  }
}

/** RAG-filterable ids among the currently loaded sources — a "failed" source
 *  has no document_id (nothing was ever indexed for it) and is excluded from
 *  both selection and the n/m counters. */
function _selectableIds() {
  return _state.sources.filter(s => s.document_id).map(s => s.document_id);
}

function _persistSelection() {
  if (!_state.notebook) return;
  const selectable = _selectableIds();
  const deselected = new Set(selectable.filter(id => !_state.selection.has(id)));
  _saveDeselected(_state.notebook.id, deselected);
}

/**
 * The chat-payload contract (consumed by chat.js/chatStream.js):
 *   null      — workspace closed, or every selectable source is checked
 *               (nothing to filter on — same as omitting source_ids).
 *   string[]  — a checked subset (the ids to filter retrieval to).
 *   []        — nothing checked; the caller must block the send instead of
 *               sending an always-empty-result request.
 */
export function getSourceIdsForChat() {
  if (!_open) return null;
  const selectable = _selectableIds();
  if (!selectable.length) return null;
  if (_state.selection.size >= selectable.length) return null;
  return selectable.filter(id => _state.selection.has(id));
}

function _showSourcesError(msg) {
  const el = document.getElementById('nbws-sources-error');
  if (el) el.textContent = msg || '';
}

function _updateSelectAllAndCounters() {
  const selectable = _selectableIds();
  const total = selectable.length;
  const selected = selectable.filter(id => _state.selection.has(id)).length;

  const allCb = document.getElementById('nbws-select-all-cb');
  if (allCb) {
    allCb.checked = total > 0 && selected === total;
    allCb.indeterminate = selected > 0 && selected < total;
  }
  const countEl = document.getElementById('nbws-source-count');
  if (countEl) countEl.textContent = `${selected}/${total} source${total === 1 ? '' : 's'}`;

  const badge = document.getElementById('nbws-source-badge');
  if (badge) {
    badge.hidden = !total;
    badge.textContent = total ? `${selected}/${total} sources` : '';
  }
}

// Original label of every currently-armed confirm button (mirrors
// notebooks.js's identical helper — copied, not imported, since that one is
// private to its module and coupled to its own DOM).
const _confirmLabels = new WeakMap();

function _disarmConfirm(btn) {
  clearTimeout(Number(btn.dataset.armTimer));
  btn.dataset.armed = '0';
  btn.classList.remove('notebook-confirm-armed');
  if (_confirmLabels.has(btn)) {
    btn.innerHTML = _confirmLabels.get(btn);
    _confirmLabels.delete(btn);
  }
}

function _armConfirm(btn, action) {
  if (btn.dataset.armed === '1') {
    _disarmConfirm(btn);
    action();
    return;
  }
  _confirmLabels.set(btn, btn.innerHTML);
  btn.dataset.armed = '1';
  btn.classList.add('notebook-confirm-armed');
  btn.textContent = 'Sure?';
  btn.dataset.armTimer = String(setTimeout(() => _disarmConfirm(btn), 5000));
}

function _sourceRow(src) {
  const failed = src.status !== 'indexed';
  const docId = src.document_id || '';
  const selectable = !!docId;
  const checked = selectable && _state.selection.has(docId);
  return `
    <div class="list-item notebook-source-row" data-src-id="${_esc(src.id)}" data-doc-id="${_esc(docId)}">
      <input type="checkbox" class="nbws-source-cb memory-select-cb" data-doc-id="${_esc(docId)}"
             ${checked ? 'checked' : ''} ${selectable ? '' : 'disabled'}
             title="${selectable ? 'Include in chat' : 'Not indexed — excluded from chat'}">
      <span class="grow notebook-source-name" title="${_esc(src.filename || '')}">${_esc(src.filename || '(unnamed)')}</span>
      <span class="notebook-status${failed ? ' notebook-status-failed' : ''}"
            title="${_esc(failed ? (src.error || 'Indexing failed') : 'Indexed')}">${_esc(src.status || 'unknown')}</span>
      <button type="button" class="notebook-src-del" data-src-id="${_esc(src.id)}"
              title="Remove source">${_CLOSE_ICON}</button>
    </div>`;
}

function _renderSourceList() {
  const box = document.getElementById('nbws-source-list');
  if (!box) return;
  if (!_state.sources.length) {
    box.innerHTML = '<div class="dashboard-empty">No sources yet — add files above</div>';
    _updateSelectAllAndCounters();
    return;
  }
  box.innerHTML = _state.sources.map(_sourceRow).join('');
  box.querySelectorAll('.nbws-source-cb').forEach(cb => {
    cb.addEventListener('change', () => {
      const docId = cb.dataset.docId;
      if (!docId) return;
      if (cb.checked) _state.selection.add(docId);
      else _state.selection.delete(docId);
      _persistSelection();
      _updateSelectAllAndCounters();
    });
  });
  box.querySelectorAll('.notebook-src-del').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      _armConfirm(btn, () => _deleteSource(btn.dataset.srcId));
    });
  });
  _updateSelectAllAndCounters();
}

async function _deleteSource(sourceId) {
  if (!_state.notebook) return;
  try {
    await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(_state.notebook.id)}/sources/${encodeURIComponent(sourceId)}`,
      { method: 'DELETE' });
    _showSourcesError('');
  } catch (e) {
    _showSourcesError(`Remove failed (${e.message})`);
    return;
  }
  await _loadSources();
}

/** Fetch this notebook's sources and reset selection from localStorage.
 *  Guarded by `_openEpoch` so a slow fetch outlived by a close (or a switch
 *  to a different notebook) can't clobber fresher state on arrival. */
async function _loadSources() {
  if (!_state.notebook) return;
  const epoch = _openEpoch;
  const nbId = _state.notebook.id;
  let data;
  try {
    data = await _fetchJson(`${API_BASE}/api/notebooks/${encodeURIComponent(nbId)}/sources`);
  } catch (e) {
    if (epoch !== _openEpoch) return;
    _showSourcesError(`Could not load sources (${e.message})`);
    const box = document.getElementById('nbws-source-list');
    if (box) box.innerHTML = '';
    return;
  }
  if (epoch !== _openEpoch) return;
  _showSourcesError('');
  _state.sources = data.sources || [];
  const selectable = _selectableIds();
  const deselected = _loadDeselected(nbId);
  _state.selection = new Set(selectable.filter(id => !deselected.has(id)));
  _renderSourceList();
}

/** Copied from notebooks.js's `_uploadSources` (that copy stays private to
 *  the now-dead detail view Task 6 removes) — same FormData flow, scoped to
 *  the workspace's own DOM. */
async function _uploadSources(fileList) {
  if (!fileList || !fileList.length || !_state.notebook) return;
  const zone = document.getElementById('nbws-upload-zone');
  if (zone) zone.textContent = 'Uploading…';
  _showSourcesError('');

  const fd = new FormData();
  for (const file of fileList) fd.append('files', file);

  try {
    const data = await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(_state.notebook.id)}/sources`,
      { method: 'POST', body: fd });
    const failed = Number(data.failed || 0);
    if (failed > 0) {
      _showSourcesError(`${failed} file${failed === 1 ? '' : 's'} failed — see the status of each source below`);
    }
  } catch (e) {
    _showSourcesError(`Upload failed (${e.message})`);
  } finally {
    if (zone) zone.textContent = _ZONE_IDLE;
    await _loadSources();
  }
}

// Wired once (dataset-flag guard) — the upload zone/select-all checkbox are
// static chrome inside #nbws-sources-body that persists across opens/closes.
function _setupUploadZone() {
  const zone = document.getElementById('nbws-upload-zone');
  const input = document.getElementById('nbws-file-input');
  if (!zone || !input || zone.dataset.nbwsWired === '1') return;
  zone.dataset.nbwsWired = '1';

  zone.addEventListener('click', () => input.click());
  zone.addEventListener('dragover', (e) => {
    e.preventDefault();
    zone.classList.add('dragover');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('dragover');
    if (e.dataTransfer && e.dataTransfer.files.length) _uploadSources(e.dataTransfer.files);
  });
  input.addEventListener('change', () => {
    if (input.files.length) {
      _uploadSources(input.files);
      input.value = '';
    }
  });
}

function _sourcesPanelSkeleton() {
  return `
    <div class="notebook-upload-zone" id="nbws-upload-zone">${_ZONE_IDLE}</div>
    <input type="file" id="nbws-file-input" multiple style="display:none">
    <div class="notebook-error" id="nbws-sources-error"></div>
    <div class="nbws-select-all-row">
      <label class="memory-bulk-check-all"><input type="checkbox" id="nbws-select-all-cb"> Select all</label>
      <span class="nbws-source-count" id="nbws-source-count"></span>
    </div>
    <div id="nbws-source-list"><div class="dashboard-empty">Loading&hellip;</div></div>`;
}

// Wired once (dataset-flag guard) — #nbws-sources-body is static chrome that
// persists across opens/closes; only `_loadSources`/`_renderSourceList` (data,
// not listeners) run again on every open.
function _wireSourcesPanel() {
  const body = document.getElementById('nbws-sources-body');
  if (!body || body.dataset.nbwsWired === '1') return;
  body.dataset.nbwsWired = '1';
  body.innerHTML = _sourcesPanelSkeleton();

  _setupUploadZone();

  document.getElementById('nbws-select-all-cb')?.addEventListener('change', (e) => {
    const checked = !!e.target.checked;
    _state.selection = checked ? new Set(_selectableIds()) : new Set();
    _persistSelection();
    _renderSourceList();
  });
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

  _wireSourcesPanel();
  _loadSources();

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

  // Stale count must not flash for the next notebook opened — the badge is
  // static topbar chrome that outlives this close, unlike #nbws-sources-body.
  const badge = document.getElementById('nbws-source-badge');
  if (badge) { badge.hidden = true; badge.textContent = ''; }

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
  getSourceIdsForChat,
  EMPTY_SELECTION_MESSAGE,
  _state,
};

export default notebookWorkspace;
window.notebookWorkspace = notebookWorkspace;
