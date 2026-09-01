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

// Whether the document viewer opened from the studio panel is showing. It's
// kept ABOVE the workspace by design (`body.doc-view` + `.doc-editor-pane`)
// but isn't a `.modal`, so `_isModalOpen` above doesn't see it and has no
// Escape-close of its own — without this check Escape would silently close
// the workspace underneath the open artifact.
function _isDocViewerOpen() {
  return document.body.classList.contains('doc-view');
}

// Mirrors ui.js's global Escape arbiter's target-tag guard: that arbiter
// exempts INPUT/TEXTAREA/select/contentEditable targets from closing modals,
// but doesn't stop propagation, so without this check the workspace's own
// handler would still fire and tear the workspace down while the user is
// typing in the composer.
function _isTypingTarget(target) {
  if (!target) return false;
  const tag = target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
  return !!target.isContentEditable;
}

function _bindEscape() {
  if (_escHandler) return;
  _escHandler = (e) => {
    if (e.key !== 'Escape' || e.defaultPrevented) return;
    if (_isModalOpen()) return;
    if (_isDocViewerOpen()) return;
    if (_isTypingTarget(e.target)) return;
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

// ---- Mobile tabs (Task 7) --------------------------------------------------

const _MOBILE_TABS = ['sources', 'chat', 'studio'];

/**
 * Switch the mobile (<=700px) "which panel fills the screen" tab. Toggles
 * .nbws-tab-active on the matching #nbws-tabbar button AND on the matching
 * side panel (style.css's <=700px query only shows a panel with that class)
 * — the button and panel intentionally share the one class name per the task
 * brief. Also flips one of three body classes so style.css can hide chat
 * (and the follow-up chips floating above it) while a side panel owns the
 * screen; on desktop none of this has any effect (the <=700px query is the
 * only place any of these classes are read).
 */
function _setActiveTab(tab) {
  if (!_MOBILE_TABS.includes(tab)) tab = 'chat';
  document.querySelectorAll('.nbws-tab').forEach((btn) => {
    const active = btn.dataset.nbwsTab === tab;
    btn.classList.toggle('nbws-tab-active', active);
    btn.setAttribute('aria-selected', String(active));
  });
  document.getElementById('nbws-sources')?.classList.toggle('nbws-tab-active', tab === 'sources');
  document.getElementById('nbws-studio')?.classList.toggle('nbws-tab-active', tab === 'studio');
  document.body.classList.toggle('nbws-mobile-tab-sources', tab === 'sources');
  document.body.classList.toggle('nbws-mobile-tab-chat', tab === 'chat');
  document.body.classList.toggle('nbws-mobile-tab-studio', tab === 'studio');
}

const _MOBILE_MQL = window.matchMedia('(max-width: 700px)');

// Collapse (desktop-only, see the <=700px query's .nbws-collapse-btn rule)
// leaves a panel's .nbws-collapsed class in place across a resize — without
// this, a panel collapsed on desktop and then viewed through mobile's
// "full-width when active" tab would render its collapsed 32px-strip state
// (title/body hidden) stretched to 100% width instead of showing normally.
function _onMobileBreakpointChange(e) {
  if (!e.matches) return;
  _resetPanel('sources');
  _resetPanel('studio');
  document.body.classList.remove('nbws-sources-collapsed', 'nbws-studio-collapsed');
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

  document.querySelectorAll('.nbws-tab').forEach((btn) => {
    btn.addEventListener('click', () => _setActiveTab(btn.dataset.nbwsTab));
  });
  _MOBILE_MQL.addEventListener('change', _onMobileBreakpointChange);

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

// notebooks.js's own timestamp helpers (naive ISO strings are stored UTC by
// the backend — utcnow_naive — so read them as UTC, not local), copied
// rather than imported since they're private to that module too. Used by the
// studio panel's artifact-date column (Task 6).
function _parseTs(iso) {
  if (!iso) return NaN;
  let s = String(iso);
  if (!/Z$|[+-]\d\d:?\d\d$/.test(s)) s += 'Z';
  return new Date(s).getTime();
}

/** Short date for an artifact row ("14 Aug"). Empty string when unparseable. */
function _shortDate(iso) {
  const t = _parseTs(iso);
  if (!Number.isFinite(t)) return '';
  return new Date(t).toLocaleDateString([], { day: 'numeric', month: 'short' });
}

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

/** The id of the notebook bound to the currently open workspace, or null
 *  when the workspace is closed / no notebook is loaded yet. */
export function getCurrentNotebookId() {
  if (!_open || !_state.notebook) return null;
  return _state.notebook.id;
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
  // Same `_openEpoch` guard as _loadSources: the error line is part of the
  // shared, persistent panel, so a delete that resolves after a switch must
  // not report notebook A's failure into notebook B's UI.
  const epoch = _openEpoch;
  try {
    await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(_state.notebook.id)}/sources/${encodeURIComponent(sourceId)}`,
      { method: 'DELETE' });
    if (epoch !== _openEpoch) return;
    _showSourcesError('');
  } catch (e) {
    if (epoch !== _openEpoch) return;
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

  // Guarded like _loadSources: an upload outlived by a switch must not write
  // its result — or reset the idle zone text — into the next notebook's panel.
  const epoch = _openEpoch;
  try {
    const data = await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(_state.notebook.id)}/sources`,
      { method: 'POST', body: fd });
    const failed = Number(data.failed || 0);
    if (epoch === _openEpoch && failed > 0) {
      _showSourcesError(`${failed} file${failed === 1 ? '' : 's'} failed — see the status of each source below`);
    }
  } catch (e) {
    if (epoch === _openEpoch) _showSourcesError(`Upload failed (${e.message})`);
  } finally {
    // The zone text resets unconditionally: #nbws-upload-zone is injected once
    // (_wireSourcesPanel is wired-once) and never re-rendered on open, so a
    // guarded reset would strand "Uploading…" in the panel after a switch.
    if (zone) zone.textContent = _ZONE_IDLE;
    // The reload is guarded — after a switch, _openImpl already loaded the new
    // notebook's sources, so this would only fire a duplicate fetch.
    if (epoch === _openEpoch) await _loadSources();
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
    <div class="nbws-web-search">
      <input type="text" id="nbws-web-search-input" placeholder="Zoek bronnen op internet&hellip;">
      <button type="button" class="dashboard-action-btn" id="nbws-web-search-btn">Zoek</button>
    </div>
    <div id="nbws-web-search-results" hidden></div>
    <div class="notebook-upload-zone" id="nbws-upload-zone">${_ZONE_IDLE}</div>
    <input type="file" id="nbws-file-input" multiple style="display:none">
    <div class="notebook-error" id="nbws-sources-error"></div>
    <div class="nbws-select-all-row">
      <label class="memory-bulk-check-all"><input type="checkbox" id="nbws-select-all-cb"> Select all</label>
      <span class="nbws-source-count" id="nbws-source-count"></span>
    </div>
    <div id="nbws-source-list"><div class="dashboard-empty">Loading&hellip;</div></div>`;
}

// ---- Web sources (fase 4d): search + add-as-source ------------------------

function _webResultRow(r, i) {
  let domain = '';
  try { domain = new URL(r.url).hostname; } catch (_) { /* leave empty */ }
  return `
    <div class="list-item nbws-web-result" data-idx="${i}">
      <div class="grow nbws-web-result-main">
        <span class="nbws-web-result-title">${_esc(r.title || r.url)}</span>
        <span class="dashboard-row-sub nbws-web-result-domain">${_esc(domain)}</span>
      </div>
      <button type="button" class="dashboard-action-btn nbws-web-add" data-url="${_esc(r.url)}">${_PLUS_ICON}<span>Toevoegen</span></button>
    </div>`;
}

async function _runWebSearch() {
  if (!_state.notebook) return;
  const input = document.getElementById('nbws-web-search-input');
  const box = document.getElementById('nbws-web-search-results');
  const query = (input?.value || '').trim();
  if (!query || !box) return;
  box.hidden = false;
  box.innerHTML = '<div class="dashboard-empty">Searching&hellip;</div>';
  let data;
  try {
    data = await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(_state.notebook.id)}/source-search`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query }) });
  } catch (e) {
    box.innerHTML = `<div class="dashboard-empty">Search failed (${_esc(e.message)})</div>`;
    return;
  }
  const results = data.results || [];
  if (!results.length) {
    box.innerHTML = '<div class="dashboard-empty">No results.</div>';
    return;
  }
  box.innerHTML = results.map(_webResultRow).join('');
  box.querySelectorAll('.nbws-web-add').forEach(btn => {
    btn.addEventListener('click', () => _addWebSource(btn));
  });
}

async function _addWebSource(btn) {
  if (!_state.notebook) return;
  const url = btn.dataset.url;
  btn.disabled = true;
  const label = btn.querySelector('span');
  const original = label ? label.textContent : null;
  if (label) label.textContent = 'Adding…';
  try {
    const data = await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(_state.notebook.id)}/sources/url`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }) });
    const failed = data.source && data.source.status !== 'indexed';
    if (label) label.textContent = failed ? 'Failed' : 'Added';
    if (failed && btn) btn.disabled = false;
    await _loadSources();
  } catch (e) {
    if (label && original != null) label.textContent = original;
    btn.disabled = false;
    _showSourcesError(`Could not add source (${e.message})`);
  }
}

function _setupWebSearch() {
  const btn = document.getElementById('nbws-web-search-btn');
  const input = document.getElementById('nbws-web-search-input');
  btn?.addEventListener('click', _runWebSearch);
  input?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _runWebSearch(); }
    if (e.key === 'Escape') {
      const box = document.getElementById('nbws-web-search-results');
      if (box) { box.hidden = true; box.innerHTML = ''; }
    }
  });
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
  _setupWebSearch();

  document.getElementById('nbws-select-all-cb')?.addEventListener('change', (e) => {
    const checked = !!e.target.checked;
    _state.selection = checked ? new Set(_selectableIds()) : new Set();
    _persistSelection();
    _renderSourceList();
  });
}

// ---- Session dropdown + follow-up chips (Task 5) ---------------------------

const _NEW_CHAT_VALUE = '__new__';

// notebooks.js's own timestamp-fallback ordering (`last_message_at` ||
// `updated_at` || `created_at`), mirrored here rather than imported since
// that helper is private to notebooks.js — sort newest-first so the
// dropdown's top option is the session the user was most recently in,
// matching openOrCreateSessionForNotebook's own resume choice.
function _sessionTs(s) {
  const raw = s && (s.last_message_at || s.updated_at || s.created_at);
  const t = raw ? Date.parse(raw) : NaN;
  return Number.isFinite(t) ? t : 0;
}

/** Rebuild `#nbws-session-select` from this notebook's own sessions only
 *  (never an unrelated session — see the Task 4 review handoff note this
 *  guards against) plus a trailing "New chat" option. */
function _populateSessionSelect() {
  const sel = document.getElementById('nbws-session-select');
  if (!sel || !_state.notebook) return;
  const sm = window.sessionModule;
  const nbId = _state.notebook.id;
  const sessions = (sm?.getSessions?.() || [])
    .filter(s => s.notebook_id === nbId)
    .sort((a, b) => _sessionTs(b) - _sessionTs(a));
  const activeId = sm?.getCurrentSessionId?.();
  const options = sessions.map(s =>
    `<option value="${_esc(s.id)}"${s.id === activeId ? ' selected' : ''}>${_esc(s.name || 'Untitled chat')}</option>`);
  options.push(`<option value="${_NEW_CHAT_VALUE}">New chat</option>`);
  sel.innerHTML = options.join('');
}

async function _onSessionSelectChange(e) {
  const val = e.target.value;
  const nb = _state.notebook;
  if (!nb) return;
  if (val === _NEW_CHAT_VALUE) {
    try {
      const notebooksMod = await _importNotebooks();
      await notebooksMod.createSessionForNotebook(nb);
    } catch (err) {
      uiModule.showToast?.(`Could not start a new chat (${err.message})`);
    }
  } else if (val) {
    try { await window.sessionModule?.selectSession?.(val); } catch (_) {}
  }
  _clearChips();
  _populateSessionSelect();
}

// Wired once (dataset-flag guard) — #nbws-session-select is static chrome in
// index.html that persists across opens/closes.
function _wireSessionSelect() {
  const sel = document.getElementById('nbws-session-select');
  if (!sel || sel.dataset.nbwsWired === '1') return;
  sel.dataset.nbwsWired = '1';
  sel.addEventListener('change', _onSessionSelectChange);
}

const _CHIP_MAX = 3;

function _clearChips() {
  const box = document.getElementById('nbws-chips');
  if (box) box.innerHTML = '';
}

/** Render up to 3 follow-up chips; click fills the composer and focuses it —
 *  deliberately no auto-send, the user reviews/edits before sending. */
function _renderChips(questions) {
  const box = document.getElementById('nbws-chips');
  if (!box) return;
  const list = (Array.isArray(questions) ? questions : [])
    .filter(q => typeof q === 'string' && q.trim())
    .slice(0, _CHIP_MAX);
  if (!list.length) { box.innerHTML = ''; return; }
  box.innerHTML = list.map(q => `<button type="button" class="nbws-chip">${_esc(q)}</button>`).join('');
  box.querySelectorAll('.nbws-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      const msgInput = document.getElementById('message');
      if (!msgInput) return;
      msgInput.value = btn.textContent || '';
      msgInput.focus();
    });
  });
}

/** Plain text of a message bubble's `.body`, with any collapsible
 *  reasoning-model `.thinking-section` (chat.js/markdown.js's "View thinking
 *  process" block) stripped out first — live-verified against a real
 *  reasoning model (deepseek-r1) that the raw textContent otherwise pulls in
 *  the whole scratch-thinking trace ahead of the actual answer, which is
 *  exactly the noise suggest_questions shouldn't be asked to summarize. */
function _bodyText(msgNode) {
  const body = msgNode?.querySelector('.body');
  if (!body) return '';
  const clone = body.cloneNode(true);
  clone.querySelectorAll('.thinking-section').forEach(el => el.remove());
  return (clone.textContent || '').trim();
}

// chat.js marks its three error/timeout `.msg-ai` render sites (stream
// failure at chat.js:3428-3441, the research-clarification timeout at
// chat.js:3519-3524, the background-stream error banner at
// chat.js:3980-3983) with an inert `chat-error` class — no styling or
// behavior attached, added purely so a failed exchange can be told apart
// from a normal answer without sniffing message text or inline colors
// (a text/color heuristic tried first here false-positived on any *normal*
// answer that happened to start with "Error:" or contain a --red/
// --color-error token, e.g. explaining a source's own error message or
// quoting a CSS snippet — flagged in review and replaced with this
// semantic check).
function _isErrorBubble(msgNode) {
  return !!msgNode && msgNode.classList.contains('chat-error');
}

/** Plain-text {question, answer} of the most recently finished exchange,
 *  read straight from the last two `.msg-user`/`.msg-ai` bubbles in
 *  #chat-history — the same DOM chat.js itself renders into, so there is no
 *  separate state to keep in sync. Returns null when the tail isn't a clean
 *  user->assistant pair: still streaming (last bubble isn't `.msg-ai` yet),
 *  no preceding user bubble, empty text, or the assistant bubble is one of
 *  chat.js's error/timeout renders (see `_isErrorBubble`) — a failed
 *  exchange must never be POSTed to suggest_questions as if it were a real
 *  answer. */
function _lastQAPair() {
  const box = document.getElementById('chat-history');
  if (!box) return null;
  const nodes = [...box.querySelectorAll('.msg-user, .msg-ai')];
  if (!nodes.length) return null;
  const last = nodes[nodes.length - 1];
  if (!last.classList.contains('msg-ai')) return null;
  if (_isErrorBubble(last)) return null;
  let userNode = null;
  for (let i = nodes.length - 2; i >= 0; i--) {
    if (nodes[i].classList.contains('msg-user')) { userNode = nodes[i]; break; }
  }
  if (!userNode) return null;
  const answer = _bodyText(last);
  const question = _bodyText(userNode);
  if (!answer || !question) return null;
  return { question, answer };
}

/**
 * Listens for chatStream.js's unconditional end-of-stream event (fires for
 * every chat, notebook or not). Only reacts when the workspace is open AND
 * the finished stream belongs to the currently active session — inert for
 * the regular chat and for any background/other-session stream. Fetch
 * failures are swallowed: a missing suggestion strip must never surface as a
 * chat error.
 */
async function _onChatStreamDone(e) {
  if (!_open || !_state.notebook) return;
  const sm = window.sessionModule;
  const sessionId = e?.detail?.sessionId;
  if (!sessionId || sessionId !== sm?.getCurrentSessionId?.()) return;
  const pair = _lastQAPair();
  if (!pair) return;
  const nbId = _state.notebook.id;
  try {
    const data = await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(nbId)}/suggest_questions`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: pair.question, answer: pair.answer }),
      });
    // Stale by the time the fetch resolved (workspace closed, or the user
    // moved to a different notebook/session) — drop it silently.
    if (!_open || !_state.notebook || _state.notebook.id !== nbId ||
        sessionId !== sm?.getCurrentSessionId?.()) return;
    _renderChips(data.questions);
  } catch (_) {
    // Never surface as a chat error — see the doc comment above.
  }
}
document.addEventListener('ithaka:chat-stream-done', _onChatStreamDone);

/**
 * Clears stale chips the instant a new question is sent — reuses chat.js's
 * existing `ithaka:chat-busy-change` event (dispatched at the start of every
 * foreground send) instead of inventing a second "send started" signal.
 */
function _onChatBusyChange(e) {
  if (!_open || !e?.detail?.active) return;
  _clearChips();
}
window.addEventListener('ithaka:chat-busy-change', _onChatBusyChange);

// ---- Studio panel: artifacts + podcast (Task 6) ----------------------------
//
// Moved here from notebooks.js's now-deleted in-modal detail view (see that
// module's file header) — same fetch/render/poll shapes, adapted to
// #nbws-studio-body's DOM ids and to `_state.notebook` in place of that
// view's private `_detail`. Two differences from the original, both required
// by this task's controller ruling: `_openArtifact` never closes anything
// (the workspace stays open behind the document viewer — see the CSS z-index
// fix below), and the podcast poll's lifecycle is tied to the workspace via
// `registerCloseHook(_stopPodcastPoll)` instead of notebooks.js's old
// modal-close/leave-detail-view hooks.

// Fixed generate-button order and English labels per the Task B redesign
// (studio panel is Generate-buttons + Files-list, no more Dutch strings) —
// the backend only accepts these six `kind` values via POST /artifacts.
// `podcast` is a seventh artifact kind, but it is generated through its own
// endpoint/job flow (see _generatePodcast) — it stays out of ARTIFACT_KINDS
// and only shares KIND_LABELS (for the pill text on its row). `infographic`
// is a plain text artifact like the first five (POST /artifacts, opens via
// the same report endpoint) — no row-click branching needed, see the click
// handler below.
const ARTIFACT_KINDS = ['slide_deck', 'mindmap', 'briefing', 'flashcards', 'quiz', 'infographic', 'data_table', 'study_guide', 'faq'];
const KIND_LABELS = {
  slide_deck: 'Slides',
  study_guide: 'Study guide',
  briefing: 'Briefing',
  faq: 'FAQ',
  quiz: 'Quiz',
  mindmap: 'Mindmap',
  infographic: 'Infographic',
  flashcards: 'Flashcards',
  data_table: 'Data table',
  podcast: 'Podcast',
  video: 'Video',
  // Deliberately English, not "Rapporten" — this object is the English-only
  // studio-chrome map (see the "no more Dutch strings" comment above) and
  // also feeds the Files-list kind pill (_artifactRow's `label`) and the
  // in-panel viewer's kind pill (_showArtifactViewer's `kindLabel`), both
  // unconditional lookups with no per-kind override. A Dutch value here
  // would show as a lone "Rapporten" pill next to "Briefing"/"FAQ"/etc.
  // Matches the backend's own English mirror, ENGLISH_KIND_LABELS["report"]
  // in src/notebook_report.py. The tile itself still reads "Rapporten" —
  // hardcoded literally in _studioPanelSkeleton, same as the podcast tile's
  // literal "Audio" label diverging from KIND_LABELS.podcast.
  report: 'Report',
};

// Per-kind studio-tile icons — 14px monochrome outline SVGs (stroke:
// currentColor so the tile's accent variable colors them; no Unicode
// emoji, per repo convention). Podcast included: its tile renders first
// in the grid even though it generates through its own job flow.
const _KIND_ICONS = {
  video: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="5" width="14" height="14" rx="2"/><polygon points="22 7 16 12 22 17 22 7"/></svg>',
  slide_deck: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="12" y1="17" x2="12" y2="21"/><line x1="8" y1="21" x2="16" y2="21"/></svg>',
  podcast: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/></svg>',
  mindmap: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="2.5"/><circle cx="4.5" cy="5" r="2"/><circle cx="19.5" cy="5" r="2"/><circle cx="4.5" cy="19" r="2"/><circle cx="19.5" cy="19" r="2"/><line x1="10.2" y1="10.4" x2="6" y2="6.3"/><line x1="13.8" y1="10.4" x2="18" y2="6.3"/><line x1="10.2" y1="13.6" x2="6" y2="17.7"/><line x1="13.8" y1="13.6" x2="18" y2="17.7"/></svg>',
  briefing: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
  flashcards: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="7" width="14" height="12" rx="2"/><path d="M8 3h11a2 2 0 0 1 2 2v11"/></svg>',
  quiz: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><polyline points="8 12 11 15 16 9"/></svg>',
  infographic: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="6" y1="20" x2="6" y2="12"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="18" y1="20" x2="18" y2="8"/><line x1="3" y1="20" x2="21" y2="20"/></svg>',
  data_table: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="9" y1="10" x2="9" y2="20"/><line x1="15" y1="10" x2="15" y2="20"/></svg>',
  study_guide: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
  faq: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12" y2="17.01"/></svg>',
  report: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="12" y1="3" x2="12" y2="21"/><line x1="3" y1="12" x2="21" y2="12"/></svg>',
};

// Small inline monochrome icons (no Unicode emoji, per repo convention) used
// to disambiguate the Generate buttons (a plus/add glyph — these CREATE
// something) from the Files rows (a file glyph — these ARE something) and
// the per-row "open source document" affordance (a document-with-lines
// glyph). All three are 1px-stroke outline icons matching _CLOSE_ICON below.
const _PLUS_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';
const _FILE_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" class="notebook-artifact-file-icon"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
const _DOC_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>';
// Rename affordance — same pencil glyph sessions.js's per-session rename
// menu item uses, so "rename" reads as the same action everywhere in the app.
const _RENAME_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>';

function _artifactRow(a) {
  const label = KIND_LABELS[a.kind] || a.kind;
  const title = a.title || label;
  const isPodcast = a.kind === 'podcast';
  // A sibling span, not text inside .notebook-artifact-title: that span is
  // nowrap+ellipsis, so text appended inside it would be the first thing
  // clipped on a narrow viewport — and this hint is required, per spec.
  const hint = '';
  // Every non-podcast row gets a secondary "open source document" icon
  // button (Task B requirement 3) — it always routes through the existing
  // _openArtifact doc-viewer path, independent of what the row's own click
  // does (report view for most kinds, the mindmap preview for mindmap).
  const openSrcBtn = (isPodcast || a.kind === 'video') ? '' : `
      <button type="button" class="notebook-artifact-opendoc" data-art-id="${_esc(a.id)}"
              title="Open source document">${_DOC_ICON}</button>`;
  // Rename button lives in the same button group as the open-source-document
  // button (same class for shared icon-button chrome, distinct second class
  // so its own listener can target it). Every row gets one, including
  // podcast rows (they carry a title too — see the download link's
  // filename below), unlike the doc button which only exists where there's
  // a source document to open.
  const renameBtn = `
      <button type="button" class="notebook-artifact-opendoc notebook-artifact-rename" data-art-id="${_esc(a.id)}"
              title="Rename">${_RENAME_ICON}</button>`;
  const row = `
    <div class="list-item notebook-artifact-item${isPodcast ? ' notebook-podcast-item' : ''}"
         data-art-id="${_esc(a.id)}" data-doc-id="${_esc(a.document_id)}" data-kind="${_esc(a.kind)}">
      ${_FILE_ICON}
      <span class="notebook-artifact-kind">${_esc(label)}</span>
      <span class="grow notebook-artifact-title">${_esc(title)}</span>
      ${hint}
      <span class="dashboard-row-sub notebook-artifact-date">${_esc(_shortDate(a.created_at))}</span>
      ${openSrcBtn}
      ${renameBtn}
      <button type="button" class="notebook-src-del notebook-artifact-del" data-art-id="${_esc(a.id)}"
              title="Delete artifact">${_CLOSE_ICON}</button>
    </div>`;
  if (a.kind === 'video') {
    // Video rows mirror the podcast shape: a sibling panel with the player,
    // an "Open script" link through the shared _openArtifact path (the
    // linked Document holds the readable script) and a download link.
    const videoUrl = `/api/notebook-video/${encodeURIComponent(a.video_path || '')}`;
    return `${row}
    <div class="notebook-podcast-panel" id="nbws-video-panel-${_esc(a.id)}" hidden>
      <video controls preload="metadata" src="${videoUrl}"></video>
      <div class="notebook-podcast-links">
        <a href="#" class="notebook-podcast-transcript" data-art-id="${_esc(a.id)}">Open script</a>
        <a href="${videoUrl}" download="${_esc(title)}.mp4">Download</a>
      </div>
    </div>`;
  }
  if (!isPodcast) return row;
  // Podcast rows get a sibling panel (not nested — the row's own click
  // handler toggles it) with the player, a transcript link that reuses the
  // exact same _openArtifact path as every other artifact kind, and a plain
  // download link.
  const audioUrl = `/api/notebook-audio/${encodeURIComponent(a.audio_path || '')}`;
  return `${row}
    <div class="notebook-podcast-panel" id="nbws-podcast-panel-${_esc(a.id)}" hidden>
      <audio controls preload="none" src="${audioUrl}"></audio>
      <div class="notebook-podcast-links">
        <a href="#" class="notebook-podcast-transcript" data-art-id="${_esc(a.id)}">Open transcript</a>
        <a href="${audioUrl}" download="${_esc(title)}.wav">Download</a>
      </div>
    </div>`;
}

function _showArtifactError(msg) {
  const el = document.getElementById('nbws-artifact-error');
  if (el) el.textContent = msg || '';
}

/** Fetch and (re)render this notebook's artifact list. Guarded by
 *  `_openEpoch` so a slow fetch outlived by a close can't paint over
 *  whatever the workspace shows now — same reasoning as `_loadSources`. */
async function _loadArtifacts() {
  if (!_state.notebook) return;
  const epoch = _openEpoch;
  const nbId = _state.notebook.id;
  const box = document.getElementById('nbws-artifacts');
  let data;
  try {
    data = await _fetchJson(`${API_BASE}/api/notebooks/${encodeURIComponent(nbId)}/artifacts`);
  } catch (e) {
    if (epoch !== _openEpoch) return;
    if (box) box.innerHTML = '';
    _showArtifactError(`Could not load artifacts (${e.message})`);
    return;
  }
  if (epoch !== _openEpoch || !box) return;
  _showArtifactError('');
  const artifacts = data.artifacts || [];
  if (!artifacts.length) {
    box.innerHTML = '<div class="dashboard-empty">Nothing generated yet — use Generate above.</div>';
    return;
  }
  box.innerHTML = artifacts.map(_artifactRow).join('');
  box.querySelectorAll('.notebook-artifact-item').forEach(row => {
    row.addEventListener('click', (e) => {
      if (e.target.closest('.notebook-artifact-del')) return;
      if (e.target.closest('.notebook-artifact-opendoc')) return;
      if (e.target.closest('.notebook-artifact-rename-input')) return;
      const kind = row.dataset.kind;
      if (kind === 'podcast') { _togglePodcastPanel(row); return; }
      if (kind === 'video') { _toggleVideoPanel(row); return; }
      // Every text kind opens the visual report the backend renders, in a
      // new tab — since the interactive mindmap viewer this includes the
      // mindmap (the raw mermaid document stays reachable via the
      // secondary open-source-document button).
      _openArtifactReport(row);
    });
  });
  box.querySelectorAll('.notebook-artifact-del').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      _armConfirm(btn, () => _deleteArtifact(btn.dataset.artId));
    });
  });
  // Secondary "open source document" icon button on every non-podcast row —
  // always routes through the shared _openArtifact doc-viewer path,
  // regardless of what the row's own click does. Excludes the rename button:
  // it shares this class for icon-button chrome (same tokens, no new color)
  // but has its own click handler below, and both share a `.closest()` guard
  // in the row's own click handler above, so it must never double-match here.
  box.querySelectorAll('.notebook-artifact-opendoc:not(.notebook-artifact-rename)').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const row = box.querySelector(`.notebook-artifact-item[data-art-id="${CSS.escape(btn.dataset.artId)}"]`);
      if (row) _openArtifact(row);
    });
  });
  // Rename: click swaps the title span for an inline <input> (never
  // window.prompt — browser dialogs block automated/headless drivers, the
  // same reason _armConfirm above uses a two-step button instead of
  // window.confirm for delete). Enter saves via PATCH; Escape or blur cancels.
  box.querySelectorAll('.notebook-artifact-rename').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const row = box.querySelector(`.notebook-artifact-item[data-art-id="${CSS.escape(btn.dataset.artId)}"]`);
      if (row) _startArtifactRename(row);
    });
  });
  // "Open transcript" reuses _openArtifact on the row it belongs to (found
  // by artifact id, since the panel is a sibling of the row, not a
  // descendant — the row already carries data-doc-id).
  box.querySelectorAll('.notebook-podcast-transcript').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const row = box.querySelector(`.notebook-artifact-item[data-art-id="${CSS.escape(link.dataset.artId)}"]`);
      if (row) _openArtifact(row);
    });
  });
}

/** Toggle a podcast row's player/links panel (its sibling, not a descendant —
 *  so this never routes through the shared _openArtifact document-viewer path). */
function _togglePodcastPanel(row) {
  const panel = document.getElementById(`nbws-podcast-panel-${row.dataset.artId}`);
  if (panel) panel.hidden = !panel.hidden;
}

/** Toggle a video row's player/links panel — same sibling-panel shape as
 *  the podcast one above. */
function _toggleVideoPanel(row) {
  const panel = document.getElementById(`nbws-video-panel-${row.dataset.artId}`);
  if (panel) panel.hidden = !panel.hidden;
}

/** Inline rename: swap `row`'s title span for a text input (never
 *  window.prompt — see the rename-button click handler's comment above).
 *  Enter saves via PATCH and re-renders the Files list from the server
 *  response; Escape or blur (and an unchanged/empty value on Enter) restore
 *  the original span without a network call. Reuses .session-rename-input
 *  (sessions.js's own inline rename box) — same tokens/border/radius, no
 *  new styling. */
function _startArtifactRename(row) {
  if (!_state.notebook) return;
  const titleEl = row.querySelector('.notebook-artifact-title');
  if (!titleEl || row.querySelector('.notebook-artifact-rename-input')) return;
  const artifactId = row.dataset.artId;
  const original = titleEl.textContent;
  const input = document.createElement('input');
  input.type = 'text';
  input.value = original;
  // `.grow` (flex:1, same as the title span it replaces): without it the
  // input's own `.session-rename-input` width:100% takes a full line in
  // this wrapping flex row, pushing the date + buttons onto a second line.
  input.className = 'grow session-rename-input notebook-artifact-rename-input';
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  let settled = false;
  const restore = () => {
    if (settled) return;
    settled = true;
    input.replaceWith(titleEl);
  };
  const commit = async () => {
    if (settled) return;
    const newTitle = input.value.trim();
    if (!newTitle || newTitle === original) { restore(); return; }
    settled = true;
    try {
      await _fetchJson(
        `${API_BASE}/api/notebooks/${encodeURIComponent(_state.notebook.id)}/artifacts/${encodeURIComponent(artifactId)}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: newTitle }),
        });
      _showArtifactError('');
      await _loadArtifacts();
    } catch (e) {
      _showArtifactError(`Rename failed (${e.message})`);
      titleEl.textContent = original;
      input.replaceWith(titleEl);
    }
  };
  input.addEventListener('click', (e) => e.stopPropagation());
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { e.preventDefault(); restore(); }
  });
  input.addEventListener('blur', restore);
}

async function _deleteArtifact(artifactId) {
  if (!_state.notebook) return;
  // Guarded like _loadArtifacts — the error line is shared panel chrome.
  const epoch = _openEpoch;
  try {
    await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(_state.notebook.id)}/artifacts/${encodeURIComponent(artifactId)}`,
      { method: 'DELETE' });
    if (epoch !== _openEpoch) return;
    _showArtifactError('');
  } catch (e) {
    if (epoch !== _openEpoch) return;
    _showArtifactError(`Delete failed (${e.message})`);
    return;
  }
  _loadArtifacts();
}

async function _generateArtifact(kind, btn) {
  if (!_state.notebook) return;
  const label = btn?.querySelector('span');
  const original = label ? label.textContent : null;
  if (btn) btn.disabled = true;
  if (label) label.textContent = 'Generating…';
  _showArtifactError('');
  const epoch = _openEpoch;
  const payload = { kind };
  if (kind === 'mindmap') {
    const focusInput = document.getElementById('nbws-mindmap-focus');
    if (focusInput && focusInput.value.trim()) payload.focus = focusInput.value.trim();
  }
  try {
    await _fetchJson(`${API_BASE}/api/notebooks/${encodeURIComponent(_state.notebook.id)}/artifacts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (epoch === _openEpoch) await _loadArtifacts();
  } catch (e) {
    if (epoch === _openEpoch) _showArtifactError(`Could not generate (${e.message})`);
  } finally {
    // Button and label reset unconditionally: both live in the wired-once
    // studio panel, so a guarded reset would leave the button disabled and
    // stuck on "Generating…" for whichever notebook is on screen next.
    if (btn) btn.disabled = false;
    if (label && original != null) label.textContent = original;
  }
}

// ---- Podcast: separate job/polling flow (own endpoint, not /artifacts) ----

// { notebookId, jobId, timer, btn } while a job is running/polling; null
// otherwise. Module-scope (not per-row) because only one podcast job can run
// per open notebook at a time — the button is disabled for the duration.
let _podcastPoll = null;

function _podcastPendingRowHtml(text) {
  return `
    <div class="list-item notebook-artifact-item notebook-podcast-pending" id="nbws-podcast-pending">
      <span class="notebook-artifact-kind">${_esc(KIND_LABELS.podcast)}</span>
      <span class="grow notebook-artifact-title">${_esc(text)}</span>
    </div>`;
}

function _podcastPhaseText(status) {
  if (status.phase === 'script') {
    // A script_attempt > 1 means the model's first draft wasn't a usable
    // dialogue and the job is having another go — surface it so the longer
    // wait reads as progress, not a hang.
    return status.script_attempt > 1
      ? `Rewriting script… (attempt ${status.script_attempt})`
      : 'Writing script…';
  }
  if (status.phase === 'tts') {
    const seg = status.segment != null ? status.segment : '?';
    const total = status.total != null ? status.total : '?';
    return `Generating audio… ${seg}/${total}`;
  }
  if (status.phase === 'concat') return 'Merging…';
  return 'Working…';
}

/** Insert (or update, if already present) the pending row at the top of the
 *  artifact list — before any fetched artifacts, and before/instead of the
 *  "no artifacts yet" empty state. */
function _insertPodcastPending(text) {
  const box = document.getElementById('nbws-artifacts');
  if (!box) return;
  const existing = document.getElementById('nbws-podcast-pending');
  if (existing) {
    const titleEl = existing.querySelector('.notebook-artifact-title');
    if (titleEl) titleEl.textContent = text;
    return;
  }
  if (box.querySelector('.dashboard-empty')) box.innerHTML = '';
  box.insertAdjacentHTML('afterbegin', _podcastPendingRowHtml(text));
}

/** Stop the setTimeout polling loop and restore the generate button — safe
 *  to call whenever (no-op if nothing is running). Called on done/error/404,
 *  and registered below as a workspace close hook so a stale loop never polls
 *  a notebook/job that is no longer on screen. */
function _stopPodcastPoll() {
  if (!_podcastPoll) return;
  clearTimeout(_podcastPoll.timer);
  if (_podcastPoll.btn) _podcastPoll.btn.disabled = false;
  _podcastPoll = null;
}

async function _pollPodcast() {
  if (!_podcastPoll) return;
  const { notebookId, jobId } = _podcastPoll;
  let status;
  try {
    status = await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(notebookId)}/podcast/${encodeURIComponent(jobId)}`);
  } catch (e) {
    // Cancelled (workspace closed / notebook switched) or superseded while
    // the fetch was in flight — a stale reject must not paint over whatever
    // is on screen now (a fresh studio panel, or a newer job).
    if (!_podcastPoll || _podcastPoll.jobId !== jobId) return;
    _stopPodcastPoll();
    const msg = /^HTTP 404/.test(e.message)
      ? 'Generation aborted (server restarted)'
      : `Podcast failed (${e.message})`;
    // _loadArtifacts() replaces #nbws-artifacts wholesale, which both removes
    // the pending row and restores the empty-state if this was the
    // notebook's only artifact — plain removal would leave the box empty.
    await _loadArtifacts();
    _showArtifactError(msg);
    return;
  }
  if (!_podcastPoll || _podcastPoll.jobId !== jobId) return;

  if (status.status === 'done') {
    _stopPodcastPoll();
    await _loadArtifacts();
    return;
  }
  if (status.status === 'error') {
    _stopPodcastPoll();
    await _loadArtifacts();
    _showArtifactError(`Podcast failed${status.error ? `: ${status.error}` : ''}`);
    return;
  }

  _insertPodcastPending(_podcastPhaseText(status));
  _podcastPoll.timer = setTimeout(_pollPodcast, 2000);
}

async function _generatePodcast(btn) {
  if (!_state.notebook || _podcastPoll) return;
  _showArtifactError('');
  if (btn) btn.disabled = true;

  let jobId;
  try {
    const data = await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(_state.notebook.id)}/podcast`, { method: 'POST' });
    jobId = data.job_id;
  } catch (e) {
    _showArtifactError(`Could not generate (${e.message})`);
    if (btn) btn.disabled = false;
    return;
  }

  _insertPodcastPending(_podcastPhaseText({ phase: 'script' }));
  _podcastPoll = { notebookId: _state.notebook.id, jobId, timer: null, btn };
  _pollPodcast();
}

// The podcast poll must stop whenever the workspace closes — a stale
// setTimeout loop must never keep firing against a torn-down studio panel.
// Registered once at module load (this module is a persistent singleton, not
// re-instantiated per open), so no matching unregister call is needed.
registerCloseHook(_stopPodcastPoll);

// ---- Video: separate job/polling flow (own endpoint, not /artifacts) ----

// Same shape as _podcastPoll above; one video job per open notebook.
let _videoPoll = null;

function _videoPendingRowHtml(text) {
  return `
    <div class="list-item notebook-artifact-item notebook-podcast-pending" id="nbws-video-pending">
      <span class="notebook-artifact-kind">${_esc(KIND_LABELS.video)}</span>
      <span class="grow notebook-artifact-title">${_esc(text)}</span>
    </div>`;
}

function _videoPhaseText(status) {
  const seg = status.segment != null ? status.segment : '?';
  const total = status.total != null ? status.total : '?';
  if (status.phase === 'script') {
    return status.script_attempt > 1
      ? `Rewriting script… (attempt ${status.script_attempt})`
      : 'Writing script…';
  }
  if (status.phase === 'render') return `Rendering slides… ${seg}/${total}`;
  if (status.phase === 'tts') return `Generating narration… ${seg}/${total}`;
  if (status.phase === 'compose') return `Composing video… ${seg}/${total}`;
  return 'Working…';
}

function _insertVideoPending(text) {
  const box = document.getElementById('nbws-artifacts');
  if (!box) return;
  const existing = document.getElementById('nbws-video-pending');
  if (existing) {
    const titleEl = existing.querySelector('.notebook-artifact-title');
    if (titleEl) titleEl.textContent = text;
    return;
  }
  if (box.querySelector('.dashboard-empty')) box.innerHTML = '';
  box.insertAdjacentHTML('afterbegin', _videoPendingRowHtml(text));
}

function _stopVideoPoll() {
  if (!_videoPoll) return;
  clearTimeout(_videoPoll.timer);
  if (_videoPoll.btn) _videoPoll.btn.disabled = false;
  _videoPoll = null;
}

async function _pollVideo() {
  if (!_videoPoll) return;
  const { notebookId, jobId } = _videoPoll;
  let status;
  try {
    status = await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(notebookId)}/video/${encodeURIComponent(jobId)}`);
  } catch (e) {
    if (!_videoPoll || _videoPoll.jobId !== jobId) return;
    _stopVideoPoll();
    const msg = /^HTTP 404/.test(e.message)
      ? 'Generation aborted (server restarted)'
      : `Video failed (${e.message})`;
    await _loadArtifacts();
    _showArtifactError(msg);
    return;
  }
  if (!_videoPoll || _videoPoll.jobId !== jobId) return;

  if (status.status === 'done') {
    _stopVideoPoll();
    await _loadArtifacts();
    return;
  }
  if (status.status === 'error') {
    _stopVideoPoll();
    await _loadArtifacts();
    _showArtifactError(`Video failed${status.error ? `: ${status.error}` : ''}`);
    return;
  }

  _insertVideoPending(_videoPhaseText(status));
  _videoPoll.timer = setTimeout(_pollVideo, 2000);
}

async function _generateVideo(btn) {
  if (!_state.notebook || _videoPoll) return;
  _showArtifactError('');
  if (btn) btn.disabled = true;

  let jobId;
  try {
    const data = await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(_state.notebook.id)}/video`, { method: 'POST' });
    jobId = data.job_id;
  } catch (e) {
    _showArtifactError(`Could not generate (${e.message})`);
    if (btn) btn.disabled = false;
    return;
  }

  _insertVideoPending(_videoPhaseText({ phase: 'script' }));
  _videoPoll = { notebookId: _state.notebook.id, jobId, timer: null, btn };
  _pollVideo();
}

// Same close-hook reasoning as the podcast poll above.
registerCloseHook(_stopVideoPoll);
// Same reasoning: close the in-panel artifact viewer so no stale iframe
// persists across a workspace close/reopen.
registerCloseHook(_closeArtifactViewer);
// Same reasoning again: the report layout-picker modal is appended straight
// to document.body (not the studio panel), so nothing else tears it down —
// without this it would survive a workspace close and keep showing layout
// choices for a notebook that's no longer open.
registerCloseHook(_closeReportModal);

/**
 * Open a generated artifact in the document viewer, as an overlay ABOVE the
 * still-open workspace (style.css gives `body.notebook-workspace-open.doc-view
 * .doc-editor-pane` a z-index above #nbws-root's 10005 — see that rule for
 * why). Unlike notebooks.js's old in-modal version, this never closes
 * anything: the workspace (chat, sources, session, any running podcast poll)
 * stays exactly as it was — the controller ruling for this task is that
 * closing on artifact-open would be a silent context loss the user never
 * asked for. Mirrors the same window.documentModule-singleton-or-dynamic-
 * import handoff document.js's other callers use.
 */
async function _openArtifact(row) {
  if (!_state.notebook || row.dataset.opening === '1') return;
  const docId = row.dataset.docId;
  if (!docId) return;
  row.dataset.opening = '1';
  row.classList.add('notebook-artifact-opening');
  _showArtifactError('');

  try {
    let dm = window.documentModule;
    if (!dm || !dm.loadDocument) {
      const mod = await import('./document.js');
      dm = (mod && mod.default) || mod;
    }
    if (!dm || !dm.loadDocument) throw new Error('Document module unavailable');
    await dm.loadDocument(docId);
  } catch (e) {
    _showArtifactError(`Could not open artifact (${e.message})`);
  } finally {
    row.dataset.opening = '0';
    row.classList.remove('notebook-artifact-opening');
  }
}

/**
 * Open the visual report for a generated artifact in a new tab — Task A's
 * backend contract: GET /api/notebooks/<notebookId>/artifacts/<artifactId>/report.
 * A plain new-tab navigation (not fetch+render): if that endpoint 404s the
 * user just sees a 404 page in the new tab, the workspace stays untouched.
 */
function _openArtifactReport(row) {
  if (!_state.notebook) return;
  const artId = row.dataset.artId;
  if (!artId) return;
  // Relative URL so it works regardless of how the app is reached
  // (Tailscale serve, localhost, etc.) — same origin as the page itself.
  const url = `/api/notebooks/${encodeURIComponent(_state.notebook.id)}/artifacts/${encodeURIComponent(artId)}/report`;
  _showArtifactViewer(url, row.dataset.kind);
}

/** Show the artifact report in an in-panel iframe, hiding the Generate/Files
 *  sections. A back button restores the studio panel. The iframe takes the
 *  full studio body height. */
function _showArtifactViewer(url, kind) {
  const body = document.getElementById('nbws-studio-body');
  if (!body) return;
  // Hide the generate+files sections
  const sections = body.querySelectorAll('.nbws-studio-section');
  sections.forEach(s => s.style.display = 'none');
  // Remove any existing viewer
  const old = document.getElementById('nbws-artifact-viewer');
  if (old) old.remove();
  // Build the viewer chrome
  const viewer = document.createElement('div');
  viewer.id = 'nbws-artifact-viewer';
  viewer.className = 'nbws-artifact-viewer';
  const kindLabel = KIND_LABELS[kind] || 'Artifact';
  viewer.innerHTML = `
    <div class="nbws-artifact-viewer-bar">
      <button type="button" class="nbws-artifact-viewer-back" id="nbws-artifact-viewer-back" title="Back to studio">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
        <span>Studio</span>
      </button>
      <span class="nbws-artifact-viewer-kind">${_esc(kindLabel)}</span>
    </div>
    <iframe class="nbws-artifact-viewer-frame" src="${_esc(url)}" title="${_esc(kindLabel)}"></iframe>`;
  body.appendChild(viewer);
  viewer.querySelector('#nbws-artifact-viewer-back')?.addEventListener('click', _closeArtifactViewer);
  document.getElementById('nbws-studio')?.classList.add('nbws-artifact-viewer-active');
}

/** Restore the studio panel's Generate + Files sections, removing the
 *  in-panel iframe viewer. Called from the back button and on workspace
 *  close so no stale iframe persists. */
function _closeArtifactViewer() {
  const body = document.getElementById('nbws-studio-body');
  if (!body) return;
  const viewer = document.getElementById('nbws-artifact-viewer');
  if (viewer) viewer.remove();
  body.querySelectorAll('.nbws-studio-section').forEach(s => s.style.display = '');
  document.getElementById('nbws-studio')?.classList.remove('nbws-artifact-viewer-active');
}

// Two visually distinct sections (Task B): "Generate" is the row of action
// buttons (each carries a plus-icon so they unmistakably read as actions,
// not names), "Files" is the generated-artifact list (each row carries a
// file-icon). Splitting them into separate headed blocks — not just relying
// on button vs. row styling — is the fix for the "which of these are
// buttons vs. file names?" confusion the redesign was requested for.
//
// #nbws-artifact-error lives in the Files section, not Generate: of
// _showArtifactError's 6 call sites, only _generateArtifact's failure is a
// Generate-section concern — load-artifacts, delete-artifact, both podcast-
// poll-failure branches and open-artifact-failed are all Files-section
// failures. One shared error slot (not one per section) is the minimal fix
// that still renders all 6 visibly; splitting it into two slots would need
// every call site to know which section it's reporting for, for no real
// benefit since these errors are rare and never fire concurrently.
function _studioPanelSkeleton() {
  return `
    <div class="nbws-studio-section nbws-studio-generate">
      <div class="nbws-studio-section-head">Generate</div>
      <div class="notebook-artifact-btns" id="nbws-artifact-btns">
        <button type="button" class="nbws-tile notebook-podcast-gen-btn nbws-tile--podcast" id="nbws-podcast-btn"
                data-kind="podcast"><span class="nbws-tile-icon">${_KIND_ICONS.podcast}</span><span class="nbws-tile-label">Audio</span></button>
        <button type="button" class="nbws-tile notebook-video-gen-btn nbws-tile--video" id="nbws-video-btn"
                data-kind="video"><span class="nbws-tile-icon">${_KIND_ICONS.video}</span><span class="nbws-tile-label">${_esc(KIND_LABELS.video)}</span></button>
        <button type="button" class="nbws-tile notebook-report-open-btn nbws-tile--report" id="nbws-report-btn"
                data-kind="report"><span class="nbws-tile-icon">${_KIND_ICONS.report}</span><span class="nbws-tile-label">Rapporten</span></button>
        ${ARTIFACT_KINDS.map(kind => `<button type="button" class="nbws-tile notebook-artifact-gen-btn nbws-tile--${_esc(kind)}"
                data-kind="${_esc(kind)}"><span class="nbws-tile-icon">${_KIND_ICONS[kind] || _PLUS_ICON}</span><span class="nbws-tile-label">${_esc(KIND_LABELS[kind])}</span></button>`).join('')}
      </div>
      <div class="nbws-mindmap-focus-wrap" id="nbws-mindmap-focus-wrap">
        <input type="text" id="nbws-mindmap-focus" class="nbws-mindmap-focus-input"
               placeholder="Focus mindmap op onderwerp…" maxlength="200" />
        <span class="nbws-mindmap-focus-hint">Optioneel — laat leeg voor een algemene mindmap</span>
      </div>
    </div>
    <div class="nbws-studio-section nbws-studio-files">
      <div class="nbws-studio-section-head">Files</div>
      <div class="notebook-error" id="nbws-artifact-error"></div>
      <div class="notebook-artifacts" id="nbws-artifacts">
        <div class="dashboard-empty">Loading&hellip;</div>
      </div>
    </div>`;
}

// Wired once (dataset-flag guard) — #nbws-studio-body is static chrome that
// persists across opens/closes; only `_loadArtifacts` (data, not listeners)
// runs again on every open.
function _wireStudioPanel() {
  const body = document.getElementById('nbws-studio-body');
  if (!body || body.dataset.nbwsWired === '1') return;
  body.dataset.nbwsWired = '1';
  body.innerHTML = _studioPanelSkeleton();

  body.querySelectorAll('.notebook-artifact-gen-btn').forEach(btn => {
    btn.addEventListener('click', () => _generateArtifact(btn.dataset.kind, btn));
  });
  document.getElementById('nbws-podcast-btn')?.addEventListener('click', (e) => _generatePodcast(e.currentTarget));
  document.getElementById('nbws-video-btn')?.addEventListener('click', (e) => _generateVideo(e.currentTarget));
  document.getElementById('nbws-report-btn')?.addEventListener('click', _openReportModal);

  window.addEventListener('message', _handleMindmapNodeClick);
}

function _handleMindmapNodeClick(e) {
  if (!e.data || e.data.type !== 'nbws-mindmap-node-click') return;
  const label = e.data.label;
  if (!label) return;
  _closeArtifactViewer();
  closeNotebookWorkspace();
  const msg = `Geef een samenvatting en uitleg over "${label}" op basis van de bronnen van dit notebook.`;
  const msgInput = document.getElementById('message');
  if (!msgInput) return;
  msgInput.value = msg;
  msgInput.dispatchEvent(new Event('input', { bubbles: true }));
  const form = document.getElementById('chat-form');
  if (form) form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
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
  // open-then-load, same posture as this module's own _openArtifact
  // (load-then-close). On failure the workspace must NOT open and the
  // picker modal must NOT close: report the error into the list view's
  // #notebook-list-error (the only view notebooks.js has left, since Task 6
  // removed its in-modal detail view) via notebooks.js's exported
  // showListError.
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

  // Switching to a *different* notebook supersedes any in-flight podcast poll:
  // it belongs to the notebook we're leaving, and the studio panel it paints
  // into is a shared singleton wired once (see _wireStudioPanel). Without this
  // the pending row and the disabled generate button follow the user into the
  // new notebook. closeNotebookWorkspace has always done this via its close
  // hook — but the notebooks picker switches workspaces by calling open()
  // again, never close(), so that hook doesn't fire on this path.
  // Re-opening the SAME notebook keeps the poll: _loadArtifacts below repaints
  // the list and the next tick restores the pending row, so progress survives.
  if (_state.notebook && _state.notebook.id !== nb.id) _stopPodcastPoll();

  // Same open()-not-close() gap for the report modal: it's an independent
  // document.body overlay, unaffected by _wireStudioPanel/_loadArtifacts
  // repainting the studio panel below it. Unlike the podcast poll this isn't
  // just cosmetic — _generateReport reads _state.notebook.id fresh at POST
  // time, so a still-open modal left over from the notebook being switched
  // away from would silently generate a report against the *new* notebook
  // using layout choices picked for the old one. Close it outright rather
  // than leaving it to the registerCloseHook(_closeReportModal) above, which
  // only fires on an actual closeNotebookWorkspace() call.
  if (_state.notebook && _state.notebook.id !== nb.id) _closeReportModal();

  _state.notebook = nb;
  _state.sources = [];
  _state.selection = new Set();

  const nameEl = document.getElementById('nbws-notebook-name');
  if (nameEl) nameEl.textContent = nb.name || '(untitled)';

  // Hero banner: show the AI-generated cover image full-width with the
  // notebook name overlaid. Falls back to hidden when no cover exists.
  const hero = document.getElementById('nbws-hero');
  const heroName = document.getElementById('nbws-hero-name');
  if (hero && heroName) {
    const coverUrl = nb.cover_image
      ? `${window.location.origin}/api/notebook-cover/${encodeURIComponent(nb.cover_image)}`
      : '';
    if (coverUrl) {
      hero.style.backgroundImage = `url('${coverUrl}')`;
      hero.style.display = '';
      heroName.textContent = nb.name || '(untitled)';
      const mobile = window.matchMedia('(max-width: 768px)').matches;
      root.style.setProperty('--nbws-hero-h', mobile ? '70px' : '90px');
    } else {
      hero.style.backgroundImage = '';
      hero.style.display = 'none';
      root.style.setProperty('--nbws-hero-h', '0px');
    }
  }

  _clearChips();
  _wireSessionSelect();
  _populateSessionSelect();

  _wireChrome();
  _open = true;
  document.body.classList.add('notebook-workspace-open');
  root.removeAttribute('aria-hidden');
  _bindEscape();
  _setActiveTab('chat'); // default tab on every open, desktop and mobile alike

  _wireSourcesPanel();
  _loadSources();

  _wireStudioPanel();
  _loadArtifacts();

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
    if (root.contains(document.activeElement)) document.activeElement.blur();
    root.setAttribute('aria-hidden', 'true');
    root.style.setProperty('--nbws-hero-h', '0px');
  }
  const hero = document.getElementById('nbws-hero');
  if (hero) { hero.style.display = 'none'; hero.style.backgroundImage = ''; }
  _resetPanel('sources');
  _resetPanel('studio');
  document.body.classList.remove(
    'nbws-mobile-tab-sources',
    'nbws-mobile-tab-chat',
    'nbws-mobile-tab-studio'
  );

  // Stale count must not flash for the next notebook opened — the badge is
  // static topbar chrome that outlives this close, unlike #nbws-sources-body.
  const badge = document.getElementById('nbws-source-badge');
  if (badge) { badge.hidden = true; badge.textContent = ''; }

  // Same reasoning for the follow-up chips — #nbws-chips is static chrome
  // that outlives this close too.
  _clearChips();

  _unbindEscape();

  for (const hook of _closeHooks) {
    try { hook(); } catch (_) {}
  }
}

// ---- Reports: "Rapport maken" layout-picker modal --------------------------
//
// "Rapporten" is a separate tile (not part of ARTIFACT_KINDS) because unlike
// every other kind, it needs a configuration step before generating: pick a
// fixed template, an AI-recommended layout, or write a free-text instruction,
// then POST /artifacts with kind="report" + layout_instruction. See
// docs/superpowers/specs/2026-09-01-notebooks-rapporten-design.md.

const _MAGIC_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8"/></svg>';

// "Zelf rapport maken" is client-side only — it never comes from the
// backend, so it is prepended to whatever /report-layouts returns for the
// Indeling grid. instruction: null is the signal _reportCardHtml uses to
// omit the pencil (edit) icon and _openReportEditor uses to start from a
// blank textarea.
const _REPORT_CUSTOM_CARD = {
  title: 'Zelf rapport maken',
  description: 'Maak rapporten volgens jouw wensen door onder meer de structuur, stijl en toon aan te passen.',
  instruction: null,
};

let _reportModalEpoch = 0;
let _reportEscHandler = null;

function _reportCardHtml(item, idx) {
  const editable = item.instruction != null;
  return `
    <button type="button" class="nbrp-card" data-idx="${idx}">
      <div class="nbrp-card-title">${_esc(item.title)}${editable ? _RENAME_ICON : ''}</div>
      <div class="nbrp-card-desc">${_esc(item.description)}</div>
    </button>`;
}

function _wireReportTemplateCard(item, grid, idx) {
  const btn = document.querySelector(`#nbrp-${grid}-grid [data-idx="${idx}"]`);
  if (btn) btn.addEventListener('click', () => _openReportEditor(item));
}

function _reportGridsSkeletonHtml() {
  return `
    <div class="nbrp-section-head">Indeling</div>
    <div class="nbrp-grid" id="nbrp-templates-grid">
      ${_reportCardHtml(_REPORT_CUSTOM_CARD, 0)}
    </div>
    <div class="nbrp-section-head nbrp-recommended-head">${_MAGIC_ICON}Aanbevolen indeling</div>
    <div class="nbrp-grid" id="nbrp-recommended-grid">
      <div class="dashboard-empty">Loading&hellip;</div>
    </div>`;
}

async function _loadReportLayouts(epoch) {
  if (!_state.notebook) return;
  const nbId = _state.notebook.id;
  let data;
  try {
    data = await _fetchJson(`${API_BASE}/api/notebooks/${encodeURIComponent(nbId)}/report-layouts`);
  } catch (e) {
    if (epoch !== _reportModalEpoch) return;
    const recGrid = document.getElementById('nbrp-recommended-grid');
    if (recGrid) recGrid.innerHTML = `<div class="dashboard-empty">Could not load suggestions (${_esc(e.message)})</div>`;
    return;
  }
  if (epoch !== _reportModalEpoch) return;

  const templates = [_REPORT_CUSTOM_CARD, ...(data.templates || [])];
  const templatesGrid = document.getElementById('nbrp-templates-grid');
  if (templatesGrid) {
    templatesGrid.innerHTML = templates.map((item, idx) => _reportCardHtml(item, idx)).join('');
    templates.forEach((item, idx) => _wireReportTemplateCard(item, 'templates', idx));
  }

  const recommended = data.recommended || [];
  const recGrid = document.getElementById('nbrp-recommended-grid');
  if (recGrid) {
    if (!recommended.length) {
      recGrid.innerHTML = '<div class="dashboard-empty">No suggestions yet — add sources to this notebook first.</div>';
    } else {
      recGrid.innerHTML = recommended.map((item, idx) => _reportCardHtml(item, idx)).join('');
      recommended.forEach((item, idx) => _wireReportTemplateCard(item, 'recommended', idx));
    }
  }
}

function _openReportEditor(item) {
  const body = document.getElementById('nbrp-body');
  if (!body) return;
  body.innerHTML = `
    <button type="button" class="nbrp-back" id="nbrp-editor-back">&larr; Terug</button>
    <div class="nbrp-editor-title">${_esc(item.title)}</div>
    <textarea id="nbrp-editor-instruction" class="nbrp-editor-textarea" rows="6"
      placeholder="Beschrijf structuur, stijl en toon in eigen woorden…">${_esc(item.instruction || '')}</textarea>
    <div class="nbrp-editor-error" id="nbrp-editor-error"></div>
    <button type="button" class="dashboard-action-btn nbrp-generate-btn" id="nbrp-generate-btn">Genereer</button>`;
  document.getElementById('nbrp-editor-back')?.addEventListener('click', () => {
    if (!body) return;
    body.innerHTML = _reportGridsSkeletonHtml();
    _wireReportTemplateCard(_REPORT_CUSTOM_CARD, 'templates', 0);
    _loadReportLayouts(_reportModalEpoch);
  });
  document.getElementById('nbrp-generate-btn')?.addEventListener('click', _generateReport);
}

async function _generateReport() {
  if (!_state.notebook) return;
  const btn = document.getElementById('nbrp-generate-btn');
  const errEl = document.getElementById('nbrp-editor-error');
  const textarea = document.getElementById('nbrp-editor-instruction');
  const instruction = textarea ? textarea.value.trim() : '';
  if (errEl) errEl.textContent = '';
  if (btn) { btn.disabled = true; btn.textContent = 'Generating…'; }
  const epoch = _openEpoch;
  const payload = { kind: 'report' };
  if (instruction) payload.layout_instruction = instruction;
  try {
    await _fetchJson(`${API_BASE}/api/notebooks/${encodeURIComponent(_state.notebook.id)}/artifacts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    _closeReportModal();
    if (epoch === _openEpoch) await _loadArtifacts();
  } catch (e) {
    if (errEl) errEl.textContent = `Could not generate (${e.message})`;
    if (btn) { btn.disabled = false; btn.textContent = 'Genereer'; }
  }
}

function _openReportModal() {
  if (!_state.notebook) return;
  const epoch = ++_reportModalEpoch;
  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'nbrp-modal';
  modal.innerHTML = `
    <div class="modal-content nbrp-modal-content" role="dialog" aria-label="Rapport maken">
      <div class="modal-header">
        <h4 style="position:relative;top:-2px;">${_KIND_ICONS.report}Rapport maken</h4>
        <span style="flex:1"></span>
        <button class="close-btn" id="nbrp-close" aria-label="Close">&#10006;</button>
      </div>
      <div class="modal-body nbrp-body" id="nbrp-body">${_reportGridsSkeletonHtml()}</div>
    </div>`;
  document.body.appendChild(modal);

  document.getElementById('nbrp-close')?.addEventListener('click', _closeReportModal);
  modal.addEventListener('click', (e) => { if (e.target === modal) _closeReportModal(); });
  _reportEscHandler = (e) => { if (e.key === 'Escape') _closeReportModal(); };
  document.addEventListener('keydown', _reportEscHandler);
  _wireReportTemplateCard(_REPORT_CUSTOM_CARD, 'templates', 0);

  _loadReportLayouts(epoch);
}

function _closeReportModal() {
  const modal = document.getElementById('nbrp-modal');
  if (modal) modal.remove();
  if (_reportEscHandler) {
    document.removeEventListener('keydown', _reportEscHandler);
    _reportEscHandler = null;
  }
  _reportModalEpoch++;
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
  getCurrentNotebookId,
  EMPTY_SELECTION_MESSAGE,
  _state,
};

export default notebookWorkspace;
window.notebookWorkspace = notebookWorkspace;
