/**
 * Notebooks Module — bounded source sets for strict, grounded chat.
 *
 * One view inside the modal: the card grid of notebooks (name, description,
 * source count) plus a "New notebook" row. A grid-card click never opens an
 * in-modal detail view — it dynamic-imports notebookWorkspace.js and hands
 * off to the full-screen NotebookLM-style 3-panel shell (see _openWorkspace
 * below). The detail view (sources/artifacts/podcast, all now living in
 * notebookWorkspace.js's sources/studio panels) was removed in Task 6 of the
 * notebooks-workspace SDD plan; this module still exports the session
 * find-or-create/create helpers (`openOrCreateSessionForNotebook`,
 * `createSessionForNotebook`) that notebookWorkspace.js's open flow and
 * session dropdown call via dynamic import.
 *
 * Structure mirrors dashboard.js (module-scope state, open/close/is*Open,
 * template-string modal, makeWindowDraggable, Escape/click-outside close,
 * an _ICONS dict of inline SVGs). Every fetch degrades on its own: a failure
 * paints short inline error text, it never throws uncaught and never blocks
 * the rest of the view.
 *
 * Deletes use an inline two-step confirm ("Sure?") — never window.confirm,
 * which blocks browser automation.
 */

import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';

const API_BASE = window.location.origin;
let _open = false;
// Bumped on every openNotebooks(); async flows capture it before an await and
// only closeNotebooks() when unchanged, so a close-then-reopen during the
// await can't tear down the freshly opened modal.
let _openEpoch = 0;
let _escHandler = null;

// ---- Helpers ----

function _esc(s) {
  // Escapes quotes too (unlike textContent->innerHTML) so values are safe
  // in attribute position (e.g. data-nb-id="...") as well as text.
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

function _jsonBody(payload) {
  return {
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  };
}

/** Parse an ISO timestamp; naive strings (no Z / offset) are stored as UTC
 *  by the backend (utcnow_naive), so read them as UTC — not local. */
function _parseTs(iso) {
  if (!iso) return NaN;
  let s = String(iso);
  if (!/Z$|[+-]\d\d:?\d\d$/.test(s)) s += 'Z';
  return new Date(s).getTime();
}

/** Short date for a notebook card ("14 Aug"). Empty string when unparseable. */
function _shortDate(iso) {
  const t = _parseTs(iso);
  if (!Number.isFinite(t)) return '';
  return new Date(t).toLocaleDateString([], { day: 'numeric', month: 'short' });
}

function _body() { return document.getElementById('notebooks-body'); }

function _showError(id, msg) {
  const el = document.getElementById(id);
  if (el) el.textContent = msg || '';
}

/**
 * Show (or, passed '', clear) an error in the list view's error slot
 * (#notebook-list-error). Exported so notebookWorkspace.js can report a
 * failed session-resolve when opening the workspace straight from a
 * grid-card click: the list view is what's showing behind the still-open
 * notebooks modal at that point.
 */
export function showListError(message) {
  _showError('notebook-list-error', message);
}

// Original label of every currently-armed confirm button, so disarming can
// restore it. A WeakMap (not a data-attribute) because the label contains
// markup — and because re-arming must never capture "Sure?" as the original.
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

/**
 * Inline two-step confirm. First click arms the button (label becomes
 * "Sure?"), a second click within 5s runs `action`. Replaces window.confirm,
 * which blocks browser automation. The button is always disarmed BEFORE the
 * action runs — a failing delete leaves no row re-render behind to rebuild
 * it, so an un-restored button would keep reading "Sure?" forever.
 */
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

// ---- Icons ----

const _ICONS = {
  // lucide "notebook" — deliberately NOT the book glyph used by the Library
  // rail button (#rail-archive), which would be indistinguishable next to it.
  notebook: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><path d="M2 6h4"/><path d="M2 10h4"/><path d="M2 14h4"/><path d="M2 18h4"/><rect x="4" y="2" width="16" height="20" rx="2"/><path d="M16 2v20"/></svg>',
  notebookSmall: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 6h4"/><path d="M2 10h4"/><path d="M2 14h4"/><path d="M2 18h4"/><rect x="4" y="2" width="16" height="20" rx="2"/><path d="M16 2v20"/></svg>',
  plus: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
  trash: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  // lucide "archive" — box with a lid, for the non-destructive archive toggle.
  archive: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/></svg>',
  // lucide "archive-restore" — same box, arrow pointing back out, for unarchive.
  unarchive: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="m9 15 3-3 3 3"/><path d="M12 12v9"/></svg>',
};

// ---- List view ----

// Whether the list view currently includes archived notebooks (?archived=1).
// Module-local, no persistence — resets to "off" on a fresh page load.
let _showArchived = false;

function _notebookCard(nb) {
  const desc = (nb.description || '').trim();
  const archived = !!nb.archived;
  // Non-destructive toggle: a single click, no _armConfirm two-step (that
  // pattern is for delete, per the file header's doc comment).
  const archiveBtn = archived
    ? `<button type="button" class="notebook-archive-btn" data-nb-id="${_esc(nb.id)}" data-archived="1"
               title="Unarchive notebook">${_ICONS.unarchive}<span>Unarchive</span></button>`
    : `<button type="button" class="notebook-archive-btn" data-nb-id="${_esc(nb.id)}" data-archived="0"
               title="Archive notebook">${_ICONS.archive}<span>Archive</span></button>`;
  return `
    <div class="dashboard-card dashboard-card-clickable notebook-card${archived ? ' notebook-card-archived' : ''}" data-nb-id="${_esc(nb.id)}">
      <div class="dashboard-card-title">${_ICONS.notebookSmall}<span class="notebook-card-name">${_esc(nb.name || '(untitled)')}</span></div>
      <div class="dashboard-card-body">
        <div class="dashboard-row-sub notebook-card-desc">${desc ? _esc(desc) : ''}</div>
        <div class="notebook-card-foot">
          <span class="dashboard-row-sub notebook-card-count" data-count-for="${_esc(nb.id)}">&hellip;</span>
          <span class="dashboard-row-sub">${_esc(_shortDate(nb.created_at))}</span>
          <span style="flex:1"></span>
          ${archiveBtn}
          <button type="button" class="notebook-del-btn" data-nb-id="${_esc(nb.id)}"
                  title="Delete notebook">${_ICONS.trash}<span>Delete</span></button>
        </div>
      </div>
    </div>`;
}

/** Fill each card's source count from its own fetch — a slow or failing
 *  count degrades to "—" in that card alone and never gates the first paint. */
function _loadCounts(notebooks) {
  return Promise.allSettled(notebooks.map(async nb => {
    let label = '—';
    try {
      const data = await _fetchJson(`${API_BASE}/api/notebooks/${encodeURIComponent(nb.id)}/sources`);
      const n = (data.sources || []).length;
      label = `${n} source${n === 1 ? '' : 's'}`;
    } catch (_) {}
    const el = document.querySelector(`[data-count-for="${CSS.escape(nb.id)}"]`);
    if (el) el.textContent = label;
  }));
}

/**
 * Grid-card click → the full-screen 3-panel workspace (NotebookLM-style).
 * Dynamic import, same handoff shape as notebookWorkspace.js's own
 * _openArtifact load of document.js: notebookWorkspace.js carries its own
 * <script> tag (so window.notebookWorkspace is usually already the live
 * singleton by the time this runs) with a dynamic-import fallback for the
 * rare case it isn't loaded yet.
 */
async function _openWorkspace(nb) {
  let ws = window.notebookWorkspace;
  if (!ws || typeof ws.openNotebookWorkspace !== 'function') {
    const mod = await import('./notebookWorkspace.js');
    ws = (mod && mod.default) || mod;
  }
  if (!ws || typeof ws.openNotebookWorkspace !== 'function') return;
  await ws.openNotebookWorkspace(nb);
}

async function _renderNotebookGrid() {
  const grid = document.getElementById('notebook-grid');
  if (!grid) return;
  let data;
  try {
    const url = _showArchived ? `${API_BASE}/api/notebooks?archived=1` : `${API_BASE}/api/notebooks`;
    data = await _fetchJson(url);
  } catch (e) {
    grid.innerHTML = '';
    _showError('notebook-list-error', `Could not load notebooks (${e.message})`);
    return;
  }
  _showError('notebook-list-error', '');
  const notebooks = data.notebooks || [];
  if (!notebooks.length) {
    // ?archived=1 is inclusive (all notebooks, not archived-only — see
    // routes/notebook_routes.py's list_notebooks), so an empty result here
    // means no notebooks exist at all, same as the unfiltered case.
    grid.innerHTML = `<div class="dashboard-empty">${_showArchived
      ? 'No notebooks yet.'
      : 'No notebooks yet (archived ones are hidden — check "Show archived").'} A notebook groups your own files and lets you chat, quiz, or make a podcast strictly from those sources. Name one above to start.</div>`;
    return;
  }
  grid.innerHTML = notebooks.map(_notebookCard).join('');

  grid.querySelectorAll('.notebook-card').forEach(card => {
    card.addEventListener('click', () => {
      const nb = notebooks.find(n => n.id === card.dataset.nbId);
      if (nb) _openWorkspace(nb);
    });
  });
  grid.querySelectorAll('.notebook-del-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      _armConfirm(btn, () => _deleteNotebook(btn.dataset.nbId));
    });
  });
  grid.querySelectorAll('.notebook-archive-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      // One-click toggle, no arm/confirm step — archiving isn't destructive.
      _toggleArchived(btn.dataset.nbId, btn.dataset.archived !== '1');
    });
  });

  _loadCounts(notebooks);
}

async function _deleteNotebook(id) {
  try {
    await _fetchJson(`${API_BASE}/api/notebooks/${encodeURIComponent(id)}`, { method: 'DELETE' });
  } catch (e) {
    _showError('notebook-list-error', `Delete failed (${e.message})`);
    return;
  }
  _renderNotebookGrid();
}

async function _toggleArchived(id, archived) {
  try {
    await _fetchJson(`${API_BASE}/api/notebooks/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      ..._jsonBody({ archived }),
    });
  } catch (e) {
    _showError('notebook-list-error', `${archived ? 'Archive' : 'Unarchive'} failed (${e.message})`);
    return;
  }
  _renderNotebookGrid();
}

async function _createNotebook() {
  const nameEl = document.getElementById('notebook-new-name');
  const descEl = document.getElementById('notebook-new-desc');
  const name = (nameEl?.value || '').trim();
  if (!name) {
    _showError('notebook-list-error', 'Name is required');
    nameEl?.focus();
    return;
  }
  const btn = document.getElementById('notebook-create-btn');
  if (btn) btn.disabled = true;
  try {
    await _fetchJson(`${API_BASE}/api/notebooks`, {
      method: 'POST',
      ..._jsonBody({ name, description: (descEl?.value || '').trim() || null }),
    });
    if (nameEl) nameEl.value = '';
    if (descEl) descEl.value = '';
    _showError('notebook-list-error', '');
    await _renderNotebookGrid();
  } catch (e) {
    _showError('notebook-list-error', `Could not create notebook (${e.message})`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function _showList() {
  const body = _body();
  if (!body) return;
  body.innerHTML = `
    <div class="notebook-newform">
      <input type="text" id="notebook-new-name" class="memory-search-input notebook-input"
             placeholder="Notebook name" autocomplete="off">
      <input type="text" id="notebook-new-desc" class="memory-search-input notebook-input"
             placeholder="Description (optional)" autocomplete="off">
      <button type="button" class="dashboard-action-btn" id="notebook-create-btn">${_ICONS.plus}<span>New notebook</span></button>
    </div>
    <label class="memory-bulk-check-all notebook-archived-toggle">
      <input type="checkbox" id="notebook-archived-toggle"${_showArchived ? ' checked' : ''}> Show archived
    </label>
    <div class="notebook-error" id="notebook-list-error"></div>
    <div class="dashboard-grid" id="notebook-grid">
      <div class="dashboard-empty">Loading&hellip;</div>
    </div>`;

  document.getElementById('notebook-create-btn').addEventListener('click', _createNotebook);
  ['notebook-new-name', 'notebook-new-desc'].forEach(id => {
    document.getElementById(id)?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); _createNotebook(); }
    });
  });
  document.getElementById('notebook-archived-toggle')?.addEventListener('change', (e) => {
    _showArchived = !!e.target.checked;
    _renderNotebookGrid();
  });

  _renderNotebookGrid();
}

// ---- Session helpers (consumed by notebookWorkspace.js via dynamic import) ----

/**
 * Endpoint/model for the notebook's chat session, resolved exactly like
 * app.js's new-chat path: current session → /api/default-chat → the most
 * recent session that has a model. Null when nothing resolves; the session
 * is then created bare (skip_validation) and the user picks a model in chat.
 *
 * `endpoint_id` is required, not optional: POST /api/session rejects a raw
 * endpoint_url without one with 403 "Choose a registered model endpoint" for
 * every signed-in non-admin (_reject_raw_endpoint_url_for_non_admin,
 * session_routes.py). /api/sessions does not return endpoint_id at all, so
 * the two session-derived branches usually fall through to the bare-session
 * path — which is correct, and mirrors app.js's guard (see
 * _createDirectChatFromPreferredModel).
 */
function _usableConfig(cfg) {
  return !!(cfg && cfg.endpoint_url && cfg.model && cfg.endpoint_id);
}

async function _resolveChatConfig() {
  const sm = window.sessionModule;
  try {
    const sessions = sm?.getSessions?.() || [];
    const current = sessions.find(s => s.id === sm?.getCurrentSessionId?.());
    if (_usableConfig(current)) return current;
  } catch (_) {}
  try {
    const dc = await _fetchJson(`${API_BASE}/api/default-chat`);
    if (_usableConfig(dc)) return dc;
  } catch (_) {}
  try {
    const withModel = (sm?.getSessions?.() || []).filter(_usableConfig);
    if (withModel.length) return withModel[0];
  } catch (_) {}
  return null;
}

/**
 * Create (never reuse) a chat session bound to notebook `nb`, via
 * _resolveChatConfig's endpoint/model resolution, and select it. Split out
 * of openOrCreateSessionForNotebook below so notebookWorkspace.js's "New
 * chat" session-dropdown option can invoke the create-only half directly —
 * that action means "start a fresh conversation", so it must never resolve
 * to an existing session the way the find-or-create flow does. Throws on
 * failure; callers own their own error UI (this function touches none).
 */
async function _createSessionForNotebook(nb) {
  const sm = window.sessionModule;
  const cfg = await _resolveChatConfig();
  const fd = new FormData();
  fd.append('name', nb.name || 'Notebook');
  fd.append('notebook_id', nb.id);
  // Mandatory: without it the backend 400s on a missing endpoint_url, and it
  // also lets a bare (model-less) session through when nothing resolved.
  fd.append('skip_validation', 'true');
  if (cfg) {
    fd.append('endpoint_url', cfg.endpoint_url || '');
    fd.append('model', cfg.model || '');
    if (cfg.endpoint_id) fd.append('endpoint_id', cfg.endpoint_id);
  }

  const payload = await _fetchJson(`${API_BASE}/api/session`, { method: 'POST', body: fd });

  // Reuse app.js's live sessions instance (published on window), exactly as
  // dashboard.js does — one obvious instance, and no risk of a duplicate
  // module record if app.js's import specifier ever grows a ?v= query again.
  if (sm?.loadSessions && sm?.selectSession) {
    // The new session must be in the module's list before selectSession can
    // resolve it — load first, then select.
    await sm.loadSessions();
    await sm.selectSession(payload.id);
  } else {
    window.location.hash = '#' + payload.id;
    window.location.reload();
  }
  return payload;
}

/**
 * Find-or-create + select the chat session bound to notebook `nb`: resume the
 * most recently active session with a matching notebook_id, or create one
 * (via _createSessionForNotebook above) when none exists yet. Exported so
 * notebookWorkspace.js's openNotebookWorkspace() can drive the exact same
 * flow when opening the workspace straight from a notebook-grid click, via a
 * dynamic import of this module. Throws on failure; callers own their own
 * error UI (this function touches none).
 */
export async function openOrCreateSessionForNotebook(nb) {
  // Resume an existing session bound to this notebook rather than spawning a
  // new one on every call. GET /api/sessions (session_routes.py) includes
  // notebook_id on each session object — added specifically so
  // "notebook-bound sessions render a badge ... and hide the RAG toggle"
  // (see that route's comment), so no separate backend lookup is needed:
  // scan the already-loaded window.sessionModule.getSessions() list.
  // Deliberately does NOT call sm.loadSessions() first to force a refetch:
  // loadSessions() also auto-selects/switches the currently open session as
  // a side effect (sessions.js's targetId resolution, ~line 1791) — doing
  // that on every call, including the common case where no notebook session
  // exists yet, would risk silently switching the visible chat before the
  // create path even runs. The already loaded list is fresh enough for what
  // matters here: a session created earlier in this page load (including by
  // this same function, below, which does call loadSessions() — but only
  // after creating one). Only active (non-archived) sessions are in the
  // list, which is fine: an archived match would fall through to creating a
  // new session below, but there is none in that case since it's filtered
  // out entirely.
  const sm = window.sessionModule;
  const candidates = (sm?.getSessions?.() || []).filter(s => s.notebook_id === nb.id);
  if (candidates.length && sm?.selectSession) {
    // The bug this fixes is users already having several duplicate sessions
    // for one notebook — resume the most recently active one (by last
    // message, else last update, else creation), not just whichever order
    // the list happens to return.
    candidates.sort((a, b) =>
      (_parseTs(b.last_message_at || b.updated_at || b.created_at) || 0) -
      (_parseTs(a.last_message_at || a.updated_at || a.created_at) || 0));
    await sm.selectSession(candidates[0].id);
    return;
  }

  await _createSessionForNotebook(nb);
}

/**
 * Always create a fresh session for notebook `nb` — the "New chat" option in
 * notebookWorkspace.js's session dropdown, as opposed to
 * openOrCreateSessionForNotebook's resume-if-possible default. Thin export
 * wrapper so the workspace never needs to reach into this module's private
 * helper directly.
 */
export async function createSessionForNotebook(nb) {
  return _createSessionForNotebook(nb);
}

// ---- Modal ----

export function openNotebooks() {
  if (_open) return;
  _open = true;
  _openEpoch++;

  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'notebooks-modal';
  modal.innerHTML = `
    <div class="modal-content notebooks-modal-content" role="dialog" aria-label="Notebooks">
      <div class="modal-header">
        <h4 style="position:relative;top:-2px;">${_ICONS.notebook}Notebooks</h4>
        <span style="flex:1"></span>
        <button class="close-btn" id="notebooks-close" aria-label="Close">&#10006;</button>
      </div>
      <div class="modal-body notebooks-body" id="notebooks-body"></div>
    </div>
  `;
  document.body.appendChild(modal);

  // Draggable via the shared helper (same as the dashboard/tasks windows).
  {
    const content = modal.querySelector('.modal-content');
    const header = modal.querySelector('.modal-header');
    if (content && header) makeWindowDraggable(modal, { content, header });
  }

  // Close wiring: X button, click-outside, Escape.
  document.getElementById('notebooks-close').addEventListener('click', closeNotebooks);
  modal.addEventListener('click', (e) => {
    if (uiModule.isTouchInsideModal()) return;
    if (e.target === modal) closeNotebooks();
  });
  _escHandler = (e) => { if (e.key === 'Escape') closeNotebooks(); };
  document.addEventListener('keydown', _escHandler);

  _showList();
}

export function closeNotebooks() {
  if (!_open) return;
  _open = false;
  const modal = document.getElementById('notebooks-modal');
  if (modal) {
    const content = modal.querySelector('.modal-content');
    if (content) {
      content.classList.add('modal-closing');
      content.addEventListener('animationend', () => modal.remove(), { once: true });
      setTimeout(() => { if (modal.parentElement) modal.remove(); }, 250);
    } else {
      modal.remove();
    }
  }
  if (_escHandler) {
    document.removeEventListener('keydown', _escHandler);
    _escHandler = null;
  }
}

export function isNotebooksOpen() { return _open; }

export default { openNotebooks, closeNotebooks, isNotebooksOpen, openOrCreateSessionForNotebook, createSessionForNotebook, showListError };
