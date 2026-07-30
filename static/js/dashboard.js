/**
 * Dashboard Module — "Home" start page.
 *
 * One overview card grid on open: today's calendar, active automations,
 * unread mail, recent sessions, model status and a quick-actions row.
 * Frontend-only: every widget fetches an existing API endpoint and
 * degrades on its own (a failed fetch shows "Not available" in that card
 * while the rest keeps working). Pattern mirrored from tasks.js.
 */

import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';

const API_BASE = window.location.origin;
let _open = false;
let _escHandler = null;

// ---- Helpers ----

function _esc(s) {
  // Escapes quotes too (unlike textContent->innerHTML) so values are safe
  // in attribute position (e.g. data-session-id="...") as well as text.
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function _fetchJson(url) {
  const res = await fetch(url, { credentials: 'same-origin' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/** Parse an ISO timestamp; naive strings (no Z / offset) are stored as UTC
 *  by the backend (utcnow_naive), so read them as UTC — not local. */
function _parseTs(iso) {
  if (!iso) return NaN;
  let s = String(iso);
  if (!/Z$|[+-]\d\d:?\d\d$/.test(s)) s += 'Z';
  return new Date(s).getTime();
}

/** Relative time, past ("5m ago") or future ("in 2h"). */
function _relTime(iso) {
  const t = _parseTs(iso);
  if (!Number.isFinite(t)) return '';
  let diff = Math.round((Date.now() - t) / 1000); // >0 = past
  const past = diff >= 0;
  diff = Math.abs(diff);
  let label;
  if (diff < 60) label = past ? 'just now' : 'in <1m';
  else if (diff < 3600) label = `${Math.round(diff / 60)}m`;
  else if (diff < 86400) label = `${Math.round(diff / 3600)}h`;
  else label = `${Math.round(diff / 86400)}d`;
  if (diff < 60) return label;
  return past ? `${label} ago` : `in ${label}`;
}

/** Time-of-day for a calendar event start ("09:30" or "all day"). */
function _eventTime(ev) {
  if (ev.all_day) return 'all day';
  const d = new Date(ev.dtstart);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function _cardBody(id) { return document.getElementById(id); }

function _renderUnavailable(bodyId) {
  const el = _cardBody(bodyId);
  if (el) el.innerHTML = '<div class="dashboard-empty">Not available</div>';
}

function _navigate(fn) {
  // Close the dashboard first so the tool we open isn't buried under it.
  closeDashboard();
  try { fn(); } catch (_) {}
}

// ---- Widgets ----

async function _renderToday() {
  const data = await _fetchJson(`${API_BASE}/api/calendar/events?start=today&end=tomorrow`);
  const el = _cardBody('dash-today-body');
  if (!el) return;
  const events = (data.events || []).slice(0, 6);
  if (!events.length) {
    el.innerHTML = '<div class="dashboard-empty">No events today</div>';
    return;
  }
  el.innerHTML = events.map(ev => `
    <div class="dashboard-row">
      <span class="dashboard-row-time">${_esc(_eventTime(ev))}</span>
      <span class="dashboard-row-main">${_esc(ev.summary || '(untitled)')}</span>
    </div>`).join('');
}

async function _renderAutomations() {
  const data = await _fetchJson(`${API_BASE}/api/tasks?status=active`);
  const el = _cardBody('dash-automations-body');
  if (!el) return;
  const tasks = (data.tasks || []).slice(0, 5);
  if (!tasks.length) {
    el.innerHTML = '<div class="dashboard-empty">No active automations</div>';
    return;
  }
  el.innerHTML = tasks.map(t => `
    <div class="dashboard-row">
      <span class="dashboard-row-main">${_esc(t.name || '(unnamed)')}</span>
      <span class="dashboard-row-sub">${t.next_run ? _esc(_relTime(t.next_run)) : ''}</span>
    </div>`).join('');
}

async function _renderMail() {
  const data = await _fetchJson(`${API_BASE}/api/email/unread-state`);
  const el = _cardBody('dash-mail-body');
  if (!el) return;
  const count = Number(data.unread_count || 0);
  el.innerHTML = `
    <div class="dashboard-count-big">${count}</div>
    <div class="dashboard-row-sub">unread message${count === 1 ? '' : 's'}</div>`;
}

async function _renderSessions() {
  const data = await _fetchJson(`${API_BASE}/api/sessions`);
  const el = _cardBody('dash-sessions-body');
  if (!el) return;
  // GET /api/sessions returns a bare array; tolerate the wrapped shape too.
  const list = Array.isArray(data) ? data : (data.sessions || []);
  const key = s => s.last_message_at || s.updated_at || s.created_at || '';
  const recent = list.slice().sort((a, b) => key(b).localeCompare(key(a))).slice(0, 6);
  if (!recent.length) {
    el.innerHTML = '<div class="dashboard-empty">No sessions yet</div>';
    return;
  }
  el.innerHTML = recent.map(s => `
    <div class="dashboard-row dashboard-clickable" data-session-id="${_esc(s.id)}">
      <span class="dashboard-row-main">${_esc(s.name || '(untitled)')}</span>
      <span class="dashboard-row-sub">${_esc(s.model || '')}</span>
      <span class="dashboard-row-sub">${_esc(_relTime(key(s)))}</span>
    </div>`).join('');
  el.querySelectorAll('[data-session-id]').forEach(row => {
    row.addEventListener('click', () => {
      const sid = row.dataset.sessionId;
      // app.js exposes the session module globally (window.sessionModule);
      // reuse its loader instead of re-importing (avoids a second module
      // instance — app.js imports sessions.js with a cache-busting query).
      _navigate(() => window.sessionModule?.selectSession?.(sid));
    });
  });
}

async function _renderModels() {
  const data = await _fetchJson(`${API_BASE}/api/models?background=false`);
  const el = _cardBody('dash-models-body');
  if (!el) return;
  const items = data.items || [];
  if (!items.length) {
    el.innerHTML = '<div class="dashboard-empty">No endpoints configured</div>';
    return;
  }
  const modelCount = items.reduce(
    (n, it) => n + (it.models || []).length + (it.models_extra || []).length, 0);
  el.innerHTML = `
    <div class="dashboard-count-big">${modelCount}</div>
    <div class="dashboard-row-sub">model${modelCount === 1 ? '' : 's'} on ${items.length} endpoint${items.length === 1 ? '' : 's'}</div>
    <div class="dashboard-chip-row">${items.map(it =>
      `<span class="dashboard-chip${it.offline ? ' dashboard-chip-offline' : ''}">${_esc(it.endpoint_name || it.host || 'endpoint')}</span>`
    ).join('')}</div>`;
}

function _loadWidgets() {
  // Fire all fetches in parallel; each card degrades on its own.
  return Promise.allSettled([
    _renderToday().catch(() => _renderUnavailable('dash-today-body')),
    _renderAutomations().catch(() => _renderUnavailable('dash-automations-body')),
    _renderMail().catch(() => _renderUnavailable('dash-mail-body')),
    _renderSessions().catch(() => _renderUnavailable('dash-sessions-body')),
    _renderModels().catch(() => _renderUnavailable('dash-models-body')),
  ]);
}

// ---- Startup pref toggle ----

async function _initAutoopenToggle() {
  const cb = document.getElementById('dashboard-autoopen-cb');
  if (!cb) return;
  try {
    const d = await _fetchJson(`${API_BASE}/api/prefs/dashboard_autoopen`);
    // GET /api/prefs/{key} returns {key, value}; unset key → value null.
    // Default is ON, so only an explicit false unchecks.
    cb.checked = d.value !== false;
  } catch (_) {
    cb.checked = true;
  }
  cb.addEventListener('change', async () => {
    try {
      await fetch(`${API_BASE}/api/prefs/dashboard_autoopen`, {
        method: 'PUT',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: cb.checked }),
      });
    } catch (_) {}
  });
}

// ---- Modal ----

const _ICONS = {
  home: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><path d="M3 11l9-8 9 8"/><path d="M5 9.5V21h14V9.5"/><path d="M9 21v-7h6v7"/></svg>',
  calendar: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
  clock: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2 2"/><path d="M5 3L2 6"/><path d="M22 6l-3-3"/></svg>',
  mail: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M22 7l-9.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>',
  chat: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  chip: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/></svg>',
  plus: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
  notes: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3h10l4 4v14H5z"/><path d="M15 3v5h5"/><path d="M8 17.5 15.5 10l2.5 2.5L10.5 20H8z"/></svg>',
  tasks: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>',
};

function _card(bodyId, icon, title, clickable) {
  return `
    <div class="dashboard-card${clickable ? ' dashboard-card-clickable' : ''}" id="${bodyId}-card">
      <div class="dashboard-card-title">${icon}<span>${title}</span></div>
      <div class="dashboard-card-body" id="${bodyId}">
        <div class="dashboard-empty">Loading&hellip;</div>
      </div>
    </div>`;
}

export function openDashboard() {
  if (_open) return;
  _open = true;

  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'dashboard-modal';
  modal.innerHTML = `
    <div class="modal-content dashboard-modal-content" role="dialog" aria-label="Home">
      <div class="modal-header">
        <h4 style="position:relative;top:-2px;">${_ICONS.home}Home</h4>
        <span style="flex:1"></span>
        <label class="dashboard-autoopen-toggle" title="Open this dashboard when the app starts">
          <input type="checkbox" id="dashboard-autoopen-cb">
          <span>Open at startup</span>
        </label>
        <button class="close-btn" id="dashboard-close" aria-label="Close">&#10006;</button>
      </div>
      <div class="modal-body dashboard-body">
        <div class="dashboard-actions">
          <button type="button" class="dashboard-action-btn" id="dash-action-new-chat">${_ICONS.plus}<span>New chat</span></button>
          <button type="button" class="dashboard-action-btn" id="dash-action-notes">${_ICONS.notes}<span>Notes</span></button>
          <button type="button" class="dashboard-action-btn" id="dash-action-tasks">${_ICONS.tasks}<span>Tasks</span></button>
          <button type="button" class="dashboard-action-btn" id="dash-action-calendar">${_ICONS.calendar}<span>Calendar</span></button>
        </div>
        <div class="dashboard-grid">
          ${_card('dash-today-body', _ICONS.calendar, 'Today', true)}
          ${_card('dash-automations-body', _ICONS.clock, 'Automations', true)}
          ${_card('dash-mail-body', _ICONS.mail, 'Mail', true)}
          ${_card('dash-sessions-body', _ICONS.chat, 'Recent sessions', false)}
          ${_card('dash-models-body', _ICONS.chip, 'Models', false)}
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  // Draggable via the shared helper (same as tasks/calendar windows).
  {
    const content = modal.querySelector('.modal-content');
    const header = modal.querySelector('.modal-header');
    if (content && header) makeWindowDraggable(modal, { content, header });
  }

  // Close wiring: X button, click-outside, Escape.
  document.getElementById('dashboard-close').addEventListener('click', closeDashboard);
  modal.addEventListener('click', (e) => {
    if (uiModule.isTouchInsideModal()) return;
    if (e.target === modal) closeDashboard();
  });
  _escHandler = (e) => { if (e.key === 'Escape') closeDashboard(); };
  document.addEventListener('keydown', _escHandler);

  // Quick actions — each closes the dashboard, then triggers the existing
  // sidebar/rail mechanism so the tool's own open logic stays authoritative.
  document.getElementById('dash-action-new-chat').addEventListener('click', () =>
    _navigate(() => document.getElementById('rail-new-session')?.click()));
  document.getElementById('dash-action-notes').addEventListener('click', () =>
    _navigate(() => document.getElementById('tool-notes-btn')?.click()));
  document.getElementById('dash-action-tasks').addEventListener('click', () =>
    _navigate(() => document.getElementById('tool-tasks-btn')?.click()));
  document.getElementById('dash-action-calendar').addEventListener('click', () =>
    _navigate(() => document.getElementById('tool-calendar-btn')?.click()));

  // Card-level navigation.
  document.getElementById('dash-today-body-card').addEventListener('click', () =>
    _navigate(() => document.getElementById('tool-calendar-btn')?.click()));
  document.getElementById('dash-automations-body-card').addEventListener('click', () =>
    _navigate(() => document.getElementById('tool-tasks-btn')?.click()));
  document.getElementById('dash-mail-body-card').addEventListener('click', () =>
    // The email library opens from the email section HEADER row (same
    // mechanism the /email route opener uses in app.js).
    _navigate(() => document.querySelector('#email-section .section-header-flex')?.click()));

  _initAutoopenToggle();
  _loadWidgets();
}

export function closeDashboard() {
  if (!_open) return;
  _open = false;
  const modal = document.getElementById('dashboard-modal');
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

export function isDashboardOpen() { return _open; }

export default { openDashboard, closeDashboard, isDashboardOpen };
