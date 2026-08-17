/**
 * Notebooks Module — bounded source sets for strict, grounded chat.
 *
 * Two views inside one modal:
 *   list   — card grid of notebooks (name, description, source count) plus a
 *            "New notebook" row.
 *   detail — one notebook's sources (upload dropzone, per-source status) and
 *            a prominent "Open chat" that spawns a session bound to it.
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
let _escHandler = null;
// Notebook object when the detail view is showing, null on the list view.
let _detail = null;

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
  back: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>',
  chat: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  upload: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>',
  trash: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  close: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  // lucide "archive" — box with a lid, for the non-destructive archive toggle.
  archive: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/></svg>',
  // lucide "archive-restore" — same box, arrow pointing back out, for unarchive.
  unarchive: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="m9 15 3-3 3 3"/><path d="M12 12v9"/></svg>',
};

// ---- List view ----

// Whether the list view currently includes archived notebooks (?archived=1).
// Module-local, no persistence — resets to "off" on a fresh page load, which
// matches every other notebooks-modal view state (e.g. _detail).
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
    grid.innerHTML = '<div class="dashboard-empty">No notebooks yet</div>';
    return;
  }
  grid.innerHTML = notebooks.map(_notebookCard).join('');

  grid.querySelectorAll('.notebook-card').forEach(card => {
    card.addEventListener('click', () => {
      const nb = notebooks.find(n => n.id === card.dataset.nbId);
      if (nb) _showDetail(nb);
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
  // Leaving the detail view: a running podcast job's polling loop targets
  // #notebook-artifacts/#notebook-artifact-error, both of which are about to
  // be torn down — stop it rather than let it keep firing against a gone DOM.
  _stopPodcastPoll();
  _detail = null;
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
      <input type="checkbox" id="notebook-archived-toggle"${_showArchived ? ' checked' : ''}> Toon gearchiveerd
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

// ---- Detail view ----

// Fixed generate-button order and Dutch labels per the design spec — the
// backend only accepts these five `kind` values via POST /artifacts.
// `podcast` is a sixth artifact kind, but it is generated through its own
// endpoint/job flow (see _generatePodcast) — it stays out of ARTIFACT_KINDS
// and only shares KIND_LABELS (for the pill text on its row).
const ARTIFACT_KINDS = ['study_guide', 'briefing', 'faq', 'quiz', 'mindmap'];
const KIND_LABELS = {
  study_guide: 'Studiegids',
  briefing: 'Briefing',
  faq: 'FAQ',
  quiz: 'Quiz',
  mindmap: 'Mindmap',
  podcast: 'Podcast',
};

function _sourceRow(src) {
  const failed = src.status !== 'indexed';
  const chunks = Number(src.chunk_count || 0);
  return `
    <div class="list-item notebook-source-row" data-src-id="${_esc(src.id)}">
      <span class="grow notebook-source-name" title="${_esc(src.filename || '')}">${_esc(src.filename || '(unnamed)')}</span>
      <span class="notebook-status${failed ? ' notebook-status-failed' : ''}"
            title="${_esc(failed ? (src.error || 'Indexing failed') : 'Indexed')}">${_esc(src.status || 'unknown')}</span>
      <span class="dashboard-row-sub notebook-source-chunks">${failed ? '' : `${chunks} chunk${chunks === 1 ? '' : 's'}`}</span>
      <button type="button" class="notebook-src-del" data-src-id="${_esc(src.id)}"
              title="Remove source">${_ICONS.close}</button>
    </div>`;
}

async function _renderSources() {
  const box = document.getElementById('notebook-sources');
  if (!box || !_detail) return;
  let data;
  try {
    data = await _fetchJson(`${API_BASE}/api/notebooks/${encodeURIComponent(_detail.id)}/sources`);
  } catch (e) {
    box.innerHTML = '';
    _showError('notebook-detail-error', `Could not load sources (${e.message})`);
    return;
  }
  const sources = data.sources || [];
  if (!sources.length) {
    box.innerHTML = '<div class="dashboard-empty">No sources yet — add files above</div>';
    return;
  }
  box.innerHTML = sources.map(_sourceRow).join('');
  box.querySelectorAll('.notebook-src-del').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      _armConfirm(btn, () => _deleteSource(btn.dataset.srcId));
    });
  });
}

async function _deleteSource(sourceId) {
  if (!_detail) return;
  try {
    await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(_detail.id)}/sources/${encodeURIComponent(sourceId)}`,
      { method: 'DELETE' });
    _showError('notebook-detail-error', '');
  } catch (e) {
    _showError('notebook-detail-error', `Remove failed (${e.message})`);
    return;
  }
  _renderSources();
}

function _artifactRow(a) {
  const label = KIND_LABELS[a.kind] || a.kind;
  const title = a.title || label;
  const isPodcast = a.kind === 'podcast';
  // A sibling span, not text inside .notebook-artifact-title: that span is
  // nowrap+ellipsis, so text appended inside it would be the first thing
  // clipped on a narrow viewport — and this hint is required, per spec.
  const hint = a.kind === 'mindmap'
    ? '<span class="notebook-artifact-hint">(Preview voor de mindmap)</span>' : '';
  const row = `
    <div class="list-item notebook-artifact-item${isPodcast ? ' notebook-podcast-item' : ''}"
         data-art-id="${_esc(a.id)}" data-doc-id="${_esc(a.document_id)}" data-kind="${_esc(a.kind)}">
      <span class="notebook-artifact-kind">${_esc(label)}</span>
      <span class="grow notebook-artifact-title">${_esc(title)}</span>
      ${hint}
      <span class="dashboard-row-sub notebook-artifact-date">${_esc(_shortDate(a.created_at))}</span>
      <button type="button" class="notebook-src-del notebook-artifact-del" data-art-id="${_esc(a.id)}"
              title="Delete artifact">${_ICONS.close}</button>
    </div>`;
  if (!isPodcast) return row;
  // Podcast rows get a sibling panel (not nested — the row's own click
  // handler toggles it) with the player, a transcript link that reuses the
  // exact same _openArtifact path as every other artifact kind, and a plain
  // download link.
  const audioUrl = `/api/notebook-audio/${encodeURIComponent(a.audio_path || '')}`;
  return `${row}
    <div class="notebook-podcast-panel" id="notebook-podcast-panel-${_esc(a.id)}" hidden>
      <audio controls preload="none" src="${audioUrl}"></audio>
      <div class="notebook-podcast-links">
        <a href="#" class="notebook-podcast-transcript" data-art-id="${_esc(a.id)}">Open transcript</a>
        <a href="${audioUrl}" download="${_esc(title)}.wav">Download</a>
      </div>
    </div>`;
}

async function _renderArtifacts() {
  const box = document.getElementById('notebook-artifacts');
  if (!box || !_detail) return;
  let data;
  try {
    data = await _fetchJson(`${API_BASE}/api/notebooks/${encodeURIComponent(_detail.id)}/artifacts`);
  } catch (e) {
    box.innerHTML = '';
    _showError('notebook-artifact-error', `Could not load artifacts (${e.message})`);
    return;
  }
  const artifacts = data.artifacts || [];
  if (!artifacts.length) {
    box.innerHTML = '<div class="dashboard-empty">No artifacts yet — generate one above</div>';
    return;
  }
  box.innerHTML = artifacts.map(_artifactRow).join('');
  box.querySelectorAll('.notebook-artifact-item').forEach(row => {
    row.addEventListener('click', (e) => {
      if (e.target.closest('.notebook-artifact-del')) return;
      if (row.dataset.kind === 'podcast') { _togglePodcastPanel(row); return; }
      _openArtifact(row);
    });
  });
  box.querySelectorAll('.notebook-artifact-del').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      _armConfirm(btn, () => _deleteArtifact(btn.dataset.artId));
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
  const panel = document.getElementById(`notebook-podcast-panel-${row.dataset.artId}`);
  if (panel) panel.hidden = !panel.hidden;
}

async function _deleteArtifact(artifactId) {
  if (!_detail) return;
  try {
    await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(_detail.id)}/artifacts/${encodeURIComponent(artifactId)}`,
      { method: 'DELETE' });
    _showError('notebook-artifact-error', '');
  } catch (e) {
    _showError('notebook-artifact-error', `Delete failed (${e.message})`);
    return;
  }
  _renderArtifacts();
}

async function _generateArtifact(kind, btn) {
  if (!_detail) return;
  const label = btn?.querySelector('span');
  const original = label ? label.textContent : null;
  if (btn) btn.disabled = true;
  if (label) label.textContent = 'Genereren…';
  _showError('notebook-artifact-error', '');
  try {
    await _fetchJson(`${API_BASE}/api/notebooks/${encodeURIComponent(_detail.id)}/artifacts`, {
      method: 'POST',
      ..._jsonBody({ kind }),
    });
    await _renderArtifacts();
  } catch (e) {
    _showError('notebook-artifact-error', `Could not generate (${e.message})`);
  } finally {
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
    <div class="list-item notebook-artifact-item notebook-podcast-pending" id="notebook-podcast-pending">
      <span class="notebook-artifact-kind">${_esc(KIND_LABELS.podcast)}</span>
      <span class="grow notebook-artifact-title">${_esc(text)}</span>
    </div>`;
}

function _podcastPhaseText(status) {
  if (status.phase === 'script') return 'Script schrijven…';
  if (status.phase === 'tts') {
    const seg = status.segment != null ? status.segment : '?';
    const total = status.total != null ? status.total : '?';
    return `Audio genereren… ${seg}/${total}`;
  }
  if (status.phase === 'concat') return 'Samenvoegen…';
  return 'Bezig…';
}

/** Insert (or update, if already present) the pending row at the top of the
 *  artifact list — before any fetched artifacts, and before/instead of the
 *  "no artifacts yet" empty state. */
function _insertPodcastPending(text) {
  const box = document.getElementById('notebook-artifacts');
  if (!box) return;
  const existing = document.getElementById('notebook-podcast-pending');
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
 *  and on modal-close / leaving the detail view so a stale loop never polls
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
    // Cancelled (modal closed / view switched) or superseded while the fetch
    // was in flight — a stale reject must not paint over whatever's on
    // screen now (a fresh detail view, or a newer job).
    if (!_podcastPoll || _podcastPoll.jobId !== jobId) return;
    _stopPodcastPoll();
    const msg = /^HTTP 404/.test(e.message)
      ? 'Generatie afgebroken (server herstart)'
      : `Podcast mislukt (${e.message})`;
    // _renderArtifacts() replaces #notebook-artifacts wholesale, which both
    // removes the pending row and restores the empty-state if this was the
    // notebook's only artifact — plain removal would leave the box empty.
    await _renderArtifacts();
    _showError('notebook-artifact-error', msg);
    return;
  }
  if (!_podcastPoll || _podcastPoll.jobId !== jobId) return;

  if (status.status === 'done') {
    _stopPodcastPoll();
    await _renderArtifacts();
    return;
  }
  if (status.status === 'error') {
    _stopPodcastPoll();
    await _renderArtifacts();
    _showError('notebook-artifact-error', `Podcast mislukt${status.error ? `: ${status.error}` : ''}`);
    return;
  }

  _insertPodcastPending(_podcastPhaseText(status));
  _podcastPoll.timer = setTimeout(_pollPodcast, 2000);
}

async function _generatePodcast(btn) {
  if (!_detail || _podcastPoll) return;
  _showError('notebook-artifact-error', '');
  if (btn) btn.disabled = true;

  let jobId;
  try {
    const data = await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(_detail.id)}/podcast`, { method: 'POST' });
    jobId = data.job_id;
  } catch (e) {
    _showError('notebook-artifact-error', `Could not generate (${e.message})`);
    if (btn) btn.disabled = false;
    return;
  }

  _insertPodcastPending(_podcastPhaseText({ phase: 'script' }));
  _podcastPoll = { notebookId: _detail.id, jobId, timer: null, btn };
  _pollPodcast();
}

/**
 * Open a generated artifact in the document viewer. Mirrors _openChat's
 * handoff shape: prefer the live window.documentModule singleton (published
 * by document.js at module load, document.js:11038), close the notebooks
 * modal, then load — falling back to a dynamic import of document.js itself
 * when the singleton isn't on window yet (e.g. document.js not loaded on
 * this page load path).
 */
async function _openArtifact(row) {
  if (!_detail || row.dataset.opening === '1') return;
  const docId = row.dataset.docId;
  if (!docId) return;
  row.dataset.opening = '1';
  row.classList.add('notebook-artifact-opening');
  _showError('notebook-artifact-error', '');

  try {
    let dm = window.documentModule;
    if (!dm || !dm.loadDocument) {
      // Relies on document.js already being loaded on this page without a
      // `?v=` cache-buster query string: a busted URL here would import a
      // second, distinct module record/instance of document.js rather than
      // reusing the one the rest of the app already initialized against.
      const mod = await import('./document.js');
      dm = (mod && mod.default) || mod;
    }
    if (!dm || !dm.loadDocument) throw new Error('Document module unavailable');
    // Only close the notebooks modal once the document actually loaded, so a
    // failure here still has #notebook-artifact-error visible to report to.
    await dm.loadDocument(docId);
    closeNotebooks();
  } catch (e) {
    _showError('notebook-artifact-error', `Could not open artifact (${e.message})`);
  } finally {
    row.dataset.opening = '0';
    row.classList.remove('notebook-artifact-opening');
  }
}

const _ZONE_IDLE = 'Add sources — drop files here or click to upload';

async function _uploadSources(fileList) {
  if (!fileList || !fileList.length || !_detail) return;
  const zone = document.getElementById('notebook-upload-zone');
  if (zone) zone.textContent = 'Uploading…';
  _showError('notebook-detail-error', '');

  const fd = new FormData();
  for (const file of fileList) fd.append('files', file);

  try {
    const data = await _fetchJson(
      `${API_BASE}/api/notebooks/${encodeURIComponent(_detail.id)}/sources`,
      { method: 'POST', body: fd });
    const failed = Number(data.failed || 0);
    if (failed > 0) {
      _showError('notebook-detail-error',
        `${failed} file${failed === 1 ? '' : 's'} failed — see the status of each source below`);
    }
  } catch (e) {
    _showError('notebook-detail-error', `Upload failed (${e.message})`);
  } finally {
    if (zone) zone.textContent = _ZONE_IDLE;
    await _renderSources();
  }
}

function _setupUploadZone() {
  const zone = document.getElementById('notebook-upload-zone');
  const input = document.getElementById('notebook-file-input');
  if (!zone || !input) return;

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

async function _openChat() {
  if (!_detail) return;
  const btn = document.getElementById('notebook-open-chat');
  const label = btn?.querySelector('span');
  if (btn) btn.disabled = true;
  if (label) label.textContent = 'Opening…';
  _showError('notebook-detail-error', '');

  try {
    const cfg = await _resolveChatConfig();
    const fd = new FormData();
    fd.append('name', _detail.name || 'Notebook');
    fd.append('notebook_id', _detail.id);
    // Mandatory: without it the backend 400s on a missing endpoint_url, and
    // it also lets a bare (model-less) session through when nothing resolved.
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
    const sm = window.sessionModule;
    if (sm?.loadSessions && sm?.selectSession) {
      // The new session must be in the module's list before selectSession
      // can resolve it — load first, then select.
      await sm.loadSessions();
      closeNotebooks();
      await sm.selectSession(payload.id);
    } else {
      window.location.hash = '#' + payload.id;
      window.location.reload();
    }
  } catch (e) {
    _showError('notebook-detail-error', `Could not open chat (${e.message})`);
  } finally {
    if (btn) btn.disabled = false;
    if (label) label.textContent = 'Open chat';
  }
}

function _showDetail(nb) {
  _detail = nb;
  const body = _body();
  if (!body) return;
  const desc = (nb.description || '').trim();
  body.innerHTML = `
    <div class="notebook-detail-head">
      <button type="button" class="dashboard-action-btn" id="notebook-back">${_ICONS.back}<span>Notebooks</span></button>
      <span style="flex:1"></span>
      <button type="button" class="dashboard-action-btn notebook-open-chat-btn" id="notebook-open-chat">${_ICONS.chat}<span>Open chat</span></button>
    </div>
    <div class="notebook-detail-title">${_ICONS.notebookSmall}<span>${_esc(nb.name || '(untitled)')}</span></div>
    ${desc ? `<div class="dashboard-row-sub notebook-detail-desc">${_esc(desc)}</div>` : ''}
    <div class="notebook-upload-zone" id="notebook-upload-zone">${_ZONE_IDLE}</div>
    <input type="file" id="notebook-file-input" multiple style="display:none">
    <div class="notebook-error" id="notebook-detail-error"></div>
    <div class="notebook-artifact-head">Artifacts</div>
    <div class="notebook-artifact-btns">
      ${ARTIFACT_KINDS.map(kind => `<button type="button" class="dashboard-action-btn notebook-artifact-gen-btn"
              data-kind="${_esc(kind)}"><span>${_esc(KIND_LABELS[kind])}</span></button>`).join('')}
      <button type="button" class="dashboard-action-btn notebook-podcast-gen-btn" id="notebook-podcast-btn"
              data-kind="podcast"><span>${_esc(KIND_LABELS.podcast)}</span></button>
    </div>
    <div class="notebook-error" id="notebook-artifact-error"></div>
    <div class="notebook-artifacts" id="notebook-artifacts">
      <div class="dashboard-empty">Loading&hellip;</div>
    </div>
    <div class="notebook-sources" id="notebook-sources">
      <div class="dashboard-empty">Loading&hellip;</div>
    </div>`;

  document.getElementById('notebook-back').addEventListener('click', _showList);
  document.getElementById('notebook-open-chat').addEventListener('click', _openChat);
  body.querySelectorAll('.notebook-artifact-gen-btn').forEach(btn => {
    btn.addEventListener('click', () => _generateArtifact(btn.dataset.kind, btn));
  });
  document.getElementById('notebook-podcast-btn').addEventListener('click', (e) => _generatePodcast(e.currentTarget));
  _setupUploadZone();
  _renderSources();
  _renderArtifacts();
}

// ---- Modal ----

export function openNotebooks() {
  if (_open) return;
  _open = true;
  _detail = null;

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
  _stopPodcastPoll();
  _open = false;
  _detail = null;
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

export default { openNotebooks, closeNotebooks, isNotebooksOpen };
