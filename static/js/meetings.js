/**
 * Meetings Module — record a meeting, upload it in chunks, and watch the
 * server turn it into a set of minutes (Document).
 *
 * Panel shape mirrors notes.js's openPanel/closePanel/togglePanel (simplified:
 * no drag-window, no mobile swipe-dismiss — the minimize button is enough).
 *
 * IMPORTANT: this module must stay importable under plain Node (see
 * tests/test_meetings_js.py) so the pure helpers (formatElapsed,
 * meetingStatusLabel, createChunkUploader) are unit-testable without a DOM.
 * ui.js / modalManager.js / document.js are NOT node-importable (they
 * transitively touch `document`/`window`/`HTMLInputElement` at module top
 * level via colorPicker.js / tileManager.js), so they are loaded lazily
 * behind a `typeof window !== 'undefined'` guard instead of a static
 * import. modalSnap.js has no such issue and is imported statically.
 */

import { applyEdgeDock } from './modalSnap.js';

const MEETING_CHUNK_TIMESLICE_MS = 30000;
export const MEETING_MAX_MS = 3 * 60 * 60 * 1000;

// ── Lazily-resolved browser-only modules ────────────────────────────────
// Populated as soon as the module graph loads in a browser (app.js already
// statically imports both ui.js and modalManager.js, so by the time any
// click handler runs these are long since resolved). Kept null under Node.
let _ui = null;
let _Modals = null;
if (typeof window !== 'undefined') {
  import('./ui.js').then((m) => { _ui = m.default || m; }).catch(() => {});
  import('./modalManager.js').then((m) => { _Modals = m; }).catch(() => {});
}

function _toast(msg, opts) {
  try { _ui?.showToast?.(msg, opts); } catch (_) {}
}
function _toastError(msg) {
  try { _ui?.showError?.(msg); } catch (_) {}
}

// ── Pure helpers (node-testable) ────────────────────────────────────────

/** Format a millisecond duration as "00:00" / "01:05" / "1:02:03". */
export function formatElapsed(ms) {
  const totalSeconds = Math.max(0, Math.floor((ms || 0) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const pad = (n) => String(n).padStart(2, '0');
  if (hours > 0) return `${hours}:${pad(minutes)}:${pad(seconds)}`;
  return `${pad(minutes)}:${pad(seconds)}`;
}

const _PHASE_LABELS = {
  splitting: () => 'Splitting audio',
  transcribing: (m) => `Transcribing ${m.segment}/${m.total}`,
  correcting: (m) => `Correcting ${m.segment}/${m.total}`,
  condensing: (m) => `Condensing (depth ${m.depth})`,
  writing: () => 'Writing minutes',
  saving: () => 'Saving',
};

/**
 * Human-readable status label for a meeting row, per its status/phase.
 *
 * A non-empty `m.error` always wins, regardless of `m.status` — the backend
 * GET already presents an interrupted row as `status:"error"` with a Dutch
 * message, but this keeps the label correct even if a row ever carries an
 * error string alongside some other status.
 */
export function meetingStatusLabel(m) {
  if (!m) return '';
  if (typeof m.error === 'string' && m.error.trim()) return `Error: ${m.error}`;
  switch (m.status) {
    case 'recording':
      return 'Recording';
    case 'processing': {
      const fn = m.phase && _PHASE_LABELS[m.phase];
      return fn ? fn(m) : 'Processing';
    }
    case 'done':
      return 'Done';
    case 'error':
      return `Error: ${m.error}`;
    default:
      return m.status || '';
  }
}

/**
 * Inline style block applied to the meetings pane on mobile (<=768px) to
 * turn it into a full-screen bottom sheet — copied verbatim from notes.js's
 * openPanel (static/js/notes.js, ~lines 1198-1207) so the two panels behave
 * identically on a phone viewport.
 */
export function mobileSheetStyle() {
  return {
    position: 'fixed',
    inset: '0',
    width: '100%',
    maxWidth: '100%',
    zIndex: '170',
    borderRadius: '14px 14px 0 0',
    animation: 'sheet-enter 0.25s cubic-bezier(0.2, 0.8, 0.2, 1) both',
    transformOrigin: 'bottom center',
  };
}

/**
 * Sequential chunk uploader: one request in flight at a time, retries a
 * failing attempt up to `maxAttempts` with `delays[i]` backoff between
 * attempts, and — because a gap in the seq sequence is fatal server-side
 * (409) — permanently halts on the first exhausted chunk. Every chunk still
 * sitting in the queue behind the halt, and every chunk enqueued after it,
 * is counted as failed without ever being posted.
 */
export function createChunkUploader({ post, maxAttempts = 3, delays = [1000, 2000, 4000], onStatus, sleep } = {}) {
  const _sleep = typeof sleep === 'function' ? sleep : (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const _queue = [];
  let _nextSeq = 0;
  let _uploadedChunks = 0;
  let _uploadedBytes = 0;
  let _failedChunks = 0;
  let _halted = false;
  let _processing = false;
  let _drainResolvers = [];

  const _blobSize = (blob) => (blob && typeof blob.size === 'number') ? blob.size : 0;

  function _snapshot() {
    return {
      uploadedBytes: _uploadedBytes,
      uploadedChunks: _uploadedChunks,
      failedChunks: _failedChunks,
      pending: _queue.length + (_processing ? 1 : 0),
    };
  }

  function _emitStatus() {
    if (typeof onStatus === 'function') {
      try { onStatus(_snapshot()); } catch (_) {}
    }
  }

  function _settleIfIdle() {
    if (_processing || _queue.length > 0) return;
    if (_drainResolvers.length === 0) return;
    const resolvers = _drainResolvers;
    _drainResolvers = [];
    const result = { uploaded: _uploadedChunks, failed: _failedChunks };
    resolvers.forEach((resolve) => resolve(result));
  }

  async function _pump() {
    if (_processing) return;
    if (_halted) {
      if (_queue.length > 0) {
        _failedChunks += _queue.length;
        _queue.length = 0;
        _emitStatus();
      }
      _settleIfIdle();
      return;
    }
    if (_queue.length === 0) {
      _settleIfIdle();
      return;
    }
    _processing = true;
    const item = _queue[0];
    let attempt = 0;
    while (attempt < maxAttempts) {
      try {
        await post(item.seq, item.blob);
        _uploadedChunks += 1;
        _uploadedBytes += _blobSize(item.blob);
        _queue.shift();
        _processing = false;
        _emitStatus();
        _pump();
        return;
      } catch (_err) {
        attempt += 1;
        _emitStatus();
        if (attempt < maxAttempts) {
          const delay = delays[attempt - 1] ?? delays[delays.length - 1] ?? 0;
          await _sleep(delay);
        }
      }
    }
    // Exhausted retries — permanent failure. Halt: no further chunk may be
    // posted (the server would 409 on the seq gap).
    _queue.shift();
    _failedChunks += 1;
    _halted = true;
    _processing = false;
    _emitStatus();
    _pump();
  }

  function enqueue(blob) {
    const seq = _nextSeq++;
    if (_halted) {
      _failedChunks += 1;
      _emitStatus();
      // A drain() resolver may already be parked (e.g. a trailing
      // dataavailable chunk arriving after drain() was called) — settle it
      // now, otherwise it would never resolve since nothing else re-enters
      // the pump while halted.
      _settleIfIdle();
      return seq;
    }
    _queue.push({ seq, blob });
    _pump();
    return seq;
  }

  function drain() {
    if (!_processing && _queue.length === 0) {
      return Promise.resolve({ uploaded: _uploadedChunks, failed: _failedChunks });
    }
    return new Promise((resolve) => { _drainResolvers.push(resolve); });
  }

  function stats() { return _snapshot(); }

  return { enqueue, drain, stats };
}

// ── Panel state (browser only) ──────────────────────────────────────────

let _open = false;
let _meetings = [];
let _pollTimer = null;
const _polling = new Map(); // meeting id -> consecutive poll-failure count

let _rec = null; // { id, mediaRecorder, stream, uploader, startedAt, timerHandle, autoStopHandle }

function _clearRecordingState() {
  if (_rec?.timerHandle) clearInterval(_rec.timerHandle);
  if (_rec?.autoStopHandle) clearTimeout(_rec.autoStopHandle);
  if (typeof window !== 'undefined') {
    window.removeEventListener('beforeunload', _beforeUnloadWarn);
  }
  _rec = null;
}

function _beforeUnloadWarn(e) {
  e.preventDefault();
  e.returnValue = '';
  return '';
}

async function _fetchJSON(url, opts) {
  const res = await fetch(url, { credentials: 'same-origin', ...opts });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = body?.detail?.message || body?.detail || detail;
    } catch (_) {}
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  if (res.status === 204) return null;
  return res.json();
}

async function _fetchMeetings() {
  try {
    const data = await _fetchJSON('/api/meetings');
    _meetings = data?.meetings || [];
  } catch (_e) {
    _meetings = [];
  }
}

async function _refreshMeeting(id) {
  try {
    const m = await _fetchJSON(`/api/meetings/${id}`);
    const idx = _meetings.findIndex((x) => x.id === id);
    if (idx >= 0) _meetings[idx] = m; else _meetings.unshift(m);
    return m;
  } catch (_e) {
    return null;
  }
}

function _startPolling(id) {
  if (!_polling.has(id)) _polling.set(id, 0);
  _ensurePollTimer();
}

const POLL_FAILURE_BUDGET = 5;
const POLL_CONNECTION_LOST_ERROR = 'Verbinding met de server verbroken';

/**
 * Pure decision for whether polling of one meeting should continue after a
 * single poll attempt. `ok` is whether that attempt reached the server
 * (regardless of the meeting's status — terminal-status handling stays with
 * the caller). A transient failure must NOT stop polling by itself: only
 * after POLL_FAILURE_BUDGET consecutive failures does polling give up.
 * Fix-wave-2 item 4 (final-review.md [I], meetings.js:277-305).
 */
export function nextPollDecision(prevFailures, ok) {
  if (ok) return { continue: true, failures: 0 };
  const failures = (prevFailures || 0) + 1;
  return { continue: failures < POLL_FAILURE_BUDGET, failures };
}

function _ensurePollTimer() {
  if (_pollTimer) return;
  _pollTimer = setInterval(async () => {
    if (_polling.size === 0) return;
    for (const id of Array.from(_polling.keys())) {
      const prevFailures = _polling.get(id) || 0;
      const m = await _refreshMeeting(id);
      if (m && (m.status === 'done' || m.status === 'error')) {
        _polling.delete(id);
        continue;
      }
      const decision = nextPollDecision(prevFailures, !!m);
      if (!decision.continue) {
        _polling.delete(id);
        // Keep the last known row state, but surface that polling gave up
        // rather than leaving the row frozen on a stale phase forever with
        // no explanation (the "silent background process" pattern).
        const idx = _meetings.findIndex((x) => x.id === id);
        if (idx >= 0) {
          _meetings[idx] = { ..._meetings[idx], status: 'error', error: POLL_CONNECTION_LOST_ERROR };
        }
      } else {
        _polling.set(id, decision.failures);
      }
    }
    _renderList();
  }, 3000);
}

function _stopPollTimer() {
  if (_pollTimer) {
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
  _polling.clear();
}

// ── Recording flow ──────────────────────────────────────────────────────

async function _startRecording() {
  if (_rec) return;
  const titleEl = document.getElementById('meeting-title');
  const agendaEl = document.getElementById('meeting-agenda');
  const termsEl = document.getElementById('meeting-terms');
  const title = (titleEl?.value || '').trim();
  if (!title) {
    _toastError('Title is required');
    titleEl?.focus();
    return;
  }

  // Request the microphone FIRST, before creating any server-side row.
  // Fix-wave-2 item 5 (final-review.md [I], meetings.js:329-351): creating
  // the row before the permission prompt left an invisible, permanently
  // "recording" orphan row on every denial (and on a tab crash before the
  // prompt resolved) — nothing ever cleans those up. Only POST once the
  // stream is actually granted.
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (_e) {
    _toastError('Microphone permission denied');
    return;
  }

  let meeting;
  try {
    meeting = await _fetchJSON('/api/meetings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title,
        agenda: (agendaEl?.value || '').trim() || undefined,
        key_terms: (termsEl?.value || '').trim() || undefined,
      }),
    });
  } catch (e) {
    stream.getTracks().forEach((t) => t.stop());
    _toastError(`Could not create meeting: ${e.message}`);
    return;
  }

  const uploader = createChunkUploader({
    post: (seq, blob) => {
      const form = new FormData();
      form.append('file', blob, `chunk-${seq}.webm`);
      return fetch(`/api/meetings/${meeting.id}/chunks?seq=${seq}`, {
        method: 'POST',
        credentials: 'same-origin',
        body: form,
      }).then((r) => {
        if (!r.ok) throw new Error(String(r.status));
      });
    },
    onStatus: (status) => _onUploadStatus(status),
  });

  let mediaRecorder;
  try {
    mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
  } catch (e) {
    _toastError(`Could not start recording: ${e.message}`);
    stream.getTracks().forEach((t) => t.stop());
    return;
  }

  const startedAt = Date.now();
  _rec = {
    id: meeting.id,
    mediaRecorder,
    stream,
    uploader,
    startedAt,
    timerHandle: null,
    autoStopHandle: null,
    // Kept so a minimized-then-restored panel (openPanel() re-running the
    // form template from scratch) can re-populate what is being recorded —
    // see the recordingUiState()/openPanel() restore path below.
    title,
    agenda: (agendaEl?.value || '').trim(),
    keyTerms: (termsEl?.value || '').trim(),
  };

  mediaRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0 && _rec) _rec.uploader.enqueue(e.data);
  };
  mediaRecorder.onstop = () => _finishRecording();

  window.addEventListener('beforeunload', _beforeUnloadWarn);
  _rec.timerHandle = setInterval(_tickTimer, 500);
  _rec.autoStopHandle = setTimeout(() => _stopRecording(), MEETING_MAX_MS);

  mediaRecorder.start(MEETING_CHUNK_TIMESLICE_MS);
  // Build the optimistic row from what we know locally rather than trusting
  // the POST response shape (spec text is ambiguous between "{id}" and a
  // full meeting object) — a follow-up GET/poll replaces this with the
  // server's version once one lands.
  _meetings.unshift({
    id: meeting.id,
    title,
    agenda: (agendaEl?.value || '').trim() || null,
    status: 'recording',
    phase: null,
    document_id: null,
    created_at: new Date().toISOString(),
  });
  _setRecordingUI(true);
  _renderList();
}

function _tickTimer() {
  const el = document.getElementById('meeting-timer');
  if (el && _rec) el.textContent = formatElapsed(Date.now() - _rec.startedAt);
}

function _onUploadStatus(status) {
  const el = document.getElementById('meeting-upload-status');
  if (!el) return;
  if (status.failedChunks > 0) {
    el.textContent = `${status.failedChunks} chunk${status.failedChunks === 1 ? '' : 's'} not saved`;
    el.style.color = 'var(--red)';
  } else {
    el.textContent = `Saved up to ${formatElapsed(status.uploadedChunks * MEETING_CHUNK_TIMESLICE_MS)}`;
    el.style.color = '';
  }
}

function _stopRecording() {
  if (!_rec) return;
  try { _rec.mediaRecorder.stop(); } catch (_) {}
}

async function _finishRecording() {
  if (!_rec) return;
  const { id, stream, uploader, startedAt } = _rec;
  stream.getTracks().forEach((t) => t.stop());
  const durationSeconds = Math.round((Date.now() - startedAt) / 1000);
  _clearRecordingState();
  _setRecordingUI(false);

  const { failed } = await uploader.drain();
  if (failed > 0) {
    _toast(`Recording may be incomplete (${failed} chunk${failed === 1 ? '' : 's'} not saved)`);
  }

  try {
    await _fetchJSON(`/api/meetings/${id}/finish`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ duration_seconds: durationSeconds }),
    });
    _startPolling(id);
    await _refreshMeeting(id);
  } catch (e) {
    // A GET refetch here would show the server's truth, which for a finish
    // failure (e.g. 400 "Geen audio ontvangen" on a zero-byte recording) is
    // still status:"recording" — start_processing_job raises before ever
    // writing status:"processing". Without this, the row stays on
    // "Recording" forever with only a transient toast as explanation
    // (fix-wave-2 item 6, observed in smoke). Update the row locally instead.
    _toastError(`Could not start processing: ${e.message}`);
    const idx = _meetings.findIndex((x) => x.id === id);
    if (idx >= 0) _meetings[idx] = rowAfterFinishFailure(_meetings[idx], e.message);
  }
  _renderList();
}

/**
 * Pure helper: the row shape after a POST .../finish failure — surfaces the
 * server's detail as a visible error instead of leaving the row frozen on
 * "Recording" with no explanation. Node-testable without a DOM.
 */
export function rowAfterFinishFailure(row, detail) {
  return { ...row, status: 'error', error: detail };
}

/**
 * Pure decision for how the record button / dot / inputs should look, given
 * "is a recording currently in flight" (the module's `_rec`, or null).
 * Node-testable without a DOM — see fix-wave-2 item 1 (restored panel must
 * not show "Start recording" while `_rec` is still set).
 */
export function recordingUiState(rec) {
  const recording = !!rec;
  return {
    label: recording ? 'Stop' : 'Start recording',
    disabled: recording,
    showDot: recording,
  };
}

function _setRecordingUI(recording) {
  const state = recordingUiState(recording ? (_rec || true) : null);
  const btn = document.getElementById('meeting-record-btn');
  const dot = document.getElementById('meeting-rec-dot');
  const timer = document.getElementById('meeting-timer');
  const status = document.getElementById('meeting-upload-status');
  if (btn) {
    btn.textContent = state.label;
    btn.classList.toggle('danger', recording);
  }
  if (dot) dot.style.display = state.showDot ? 'inline-block' : 'none';
  if (timer && !recording) timer.textContent = '00:00';
  if (status && !recording) status.textContent = '';
  ['meeting-title', 'meeting-agenda', 'meeting-terms'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.disabled = state.disabled;
  });
}

// ── Actions on a meeting row ─────────────────────────────────────────────

async function _openMinutes(documentId) {
  if (!documentId) return;
  try {
    let dm = window.documentModule;
    if (!dm || !dm.loadDocument) {
      const mod = await import('./document.js');
      dm = (mod && mod.default) || mod;
    }
    if (!dm || !dm.loadDocument) throw new Error('Document module unavailable');
    await dm.loadDocument(documentId);
  } catch (e) {
    _toastError(`Could not open minutes: ${e.message}`);
  }
}

async function _reprocess(id) {
  try {
    await _fetchJSON(`/api/meetings/${id}/finish`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    _startPolling(id);
    await _refreshMeeting(id);
  } catch (e) {
    _toastError(`Could not reprocess: ${e.message}`);
    const idx = _meetings.findIndex((x) => x.id === id);
    if (idx >= 0) _meetings[idx] = rowAfterFinishFailure(_meetings[idx], e.message);
  }
  _renderList();
}

async function _deleteMeeting(id, title) {
  const confirmed = _ui?.styledConfirm
    ? await _ui.styledConfirm('Delete this meeting?', { confirmText: 'Delete', danger: true })
    : confirm('Delete this meeting?');
  if (!confirmed) return;
  const idx = _meetings.findIndex((m) => m.id === id);
  if (idx >= 0) _meetings.splice(idx, 1);
  _polling.delete(id);
  _renderList();
  try {
    await _fetchJSON(`/api/meetings/${id}`, { method: 'DELETE' });
    _toast(`Deleted "${title || 'meeting'}"`);
  } catch (e) {
    _toastError(`Could not delete: ${e.message}`);
    await _fetchMeetings();
    _renderList();
  }
}

// ── Rendering ────────────────────────────────────────────────────────────

export function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function _formatDate(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString(undefined, {
      day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  } catch (_) { return ''; }
}

function _renderList() {
  const list = document.getElementById('meeting-list');
  if (!list) return;
  if (_meetings.length === 0) {
    list.innerHTML = '<div style="opacity:0.5;padding:12px 4px;font-size:12px;">No meetings yet</div>';
    return;
  }
  list.innerHTML = _meetings.map((m) => renderMeetingRow(m, { recording: !!_rec })).join('');
  _meetings.forEach((m) => {
    const row = list.querySelector(`[data-meeting-id="${m.id}"]`);
    if (!row) return;
    row.querySelector('.meeting-open-minutes-btn')?.addEventListener('click', () => _openMinutes(m.document_id));
    row.querySelector('.meeting-reprocess-btn')?.addEventListener('click', () => _reprocess(m.id));
    row.querySelector('.meeting-delete-btn')?.addEventListener('click', () => _deleteMeeting(m.id, m.title));
  });
}

/**
 * Pure row-HTML builder (node-testable without a DOM). `recording` stands in
 * for "a recording is currently in flight" (module state `!!_rec` in the
 * browser) — Reprocess is suppressed while one is active.
 */
export function renderMeetingRow(m, { recording = false } = {}) {
  const statusLabel = escapeHtml(meetingStatusLabel(m));
  const canReprocess = (m.status === 'error' || m.status === 'done') && !recording;
  const id = escapeHtml(m.id);
  const openMinutesBtn = m.document_id
    ? `<button type="button" class="memory-toolbar-btn meeting-open-minutes-btn">Open minutes</button>`
    : '';
  const audioBtn = `<a class="memory-toolbar-btn" style="text-decoration:none;display:inline-flex;align-items:center;" href="/api/meetings/${id}/audio" download>Audio</a>`;
  const reprocessBtn = canReprocess
    ? `<button type="button" class="memory-toolbar-btn meeting-reprocess-btn">Reprocess</button>`
    : '';
  const deleteBtn = `<button type="button" class="memory-toolbar-btn danger meeting-delete-btn">Delete</button>`;
  return `
    <div class="meeting-row" data-meeting-id="${id}" style="padding:10px 4px;border-bottom:1px solid var(--border);">
      <div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline;">
        <strong style="font-size:13px;">${escapeHtml(m.title)}</strong>
        <span style="font-size:11px;opacity:0.6;white-space:nowrap;">${escapeHtml(_formatDate(m.created_at))}</span>
      </div>
      <div style="font-size:11px;opacity:0.7;margin:2px 0 6px;">${statusLabel}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;">${openMinutesBtn}${audioBtn}${reprocessBtn}${deleteBtn}</div>
    </div>
  `;
}

// ── Panel open/close ─────────────────────────────────────────────────────

const MIC_SVG_PATH = '<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/>';

function _ensureMeetingsChipRegistered() {
  if (!_Modals || _Modals.isRegistered('meetings-panel')) return;
  _Modals.register('meetings-panel', {
    railBtnId: 'rail-meetings',
    sidebarBtnId: 'tool-meetings-btn',
    restoreFn: () => { openPanel(); },
    closeFn: () => { _forceClose(); },
  });
}

function _forceClose() {
  _open = false;
  document.body.classList.remove('meetings-view');
  // A full close (unlike minimize-to-chip) tears everything down — an
  // in-flight recording has nowhere to keep reporting progress to, so stop
  // it; onstop -> _finishRecording still runs (drain + POST /finish) even
  // with the pane already gone, since every DOM touch there is null-safe.
  if (_rec) _stopRecording();
  _stopPollTimer();
  document.getElementById('tool-meetings-btn')?.classList.remove('active');
  try { _Modals?.unregister?.('meetings-panel'); } catch (_) {}
  document.getElementById('meetings-pane')?.remove();
  document.getElementById('meetings-pane-backdrop')?.remove();
}

export function openPanel() {
  if (_open) return;
  _open = true;
  document.body.classList.add('meetings-view');

  const btn = document.getElementById('tool-meetings-btn');
  if (btn) btn.classList.add('active');

  const pane = document.createElement('div');
  pane.id = 'meetings-pane';
  pane.className = 'notes-pane meetings-pane';
  pane.innerHTML = `
    <div class="notes-pane-header">
      <h4 class="notes-pane-title"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2.5px;margin-right:6px">${MIC_SVG_PATH}</svg>Meetings</h4>
      <span style="flex:1"></span>
      <button id="meetings-minimize-btn" class="modal-minimize-btn" title="Minimize" aria-label="Minimize meetings"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round" aria-hidden="true"><line x1="6" y1="18" x2="18" y2="18"/></svg></button>
    </div>
    <div class="notes-pane-body" style="padding:10px;overflow-y:auto;">
      <div class="meeting-form-card" style="display:flex;flex-direction:column;gap:6px;margin-bottom:14px;">
        <input type="text" id="meeting-title" class="memory-search-input" placeholder="Title" autocomplete="off" />
        <textarea id="meeting-agenda" class="memory-search-input" placeholder="Agenda (optional)" rows="2" style="resize:vertical;height:auto;"></textarea>
        <input type="text" id="meeting-terms" class="memory-search-input" placeholder="Key terms: names, acronyms (optional)" autocomplete="off" />
        <div style="display:flex;align-items:center;gap:8px;">
          <button type="button" id="meeting-record-btn" class="memory-toolbar-btn">Start recording</button>
          <span id="meeting-rec-dot" style="display:none;width:8px;height:8px;border-radius:50%;background:var(--red);"></span>
          <span id="meeting-timer" style="font-size:12px;opacity:0.8;">00:00</span>
        </div>
        <div id="meeting-upload-status" style="font-size:11px;min-height:14px;"></div>
      </div>
      <div id="meeting-list"></div>
    </div>
  `;

  // On mobile open as a full-screen bottom sheet (slide up), not the
  // desktop side panel — mirrors notes.js's openPanel. Without this the
  // pane fell back to the centered-card default (min(880px,92vw) wide),
  // which overflows/squeezes badly on a phone viewport.
  if (window.innerWidth <= 768) {
    Object.assign(pane.style, mobileSheetStyle());
  }

  const backdrop = document.createElement('div');
  backdrop.className = 'notes-pane-backdrop';
  backdrop.id = 'meetings-pane-backdrop';
  backdrop.addEventListener('click', (ev) => {
    if (ev.target === backdrop) closePanel('down');
  });
  backdrop.appendChild(pane);
  document.body.appendChild(backdrop);

  if (window.innerWidth > 768) {
    try { applyEdgeDock(pane, 'right'); } catch (_) {}
  }

  document.getElementById('meetings-minimize-btn')?.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    closePanel('down');
  });
  document.getElementById('meeting-record-btn')?.addEventListener('click', () => {
    if (_rec) _stopRecording();
    else _startRecording();
  });

  // A recording started before this panel was minimized keeps running while
  // minimized (see _forceClose()'s comment) — the chip's restoreFn calls
  // openPanel() again, which just rebuilt the form from the literal
  // template above (button "Start recording", dot hidden, 00:00 timer,
  // enabled inputs). Re-apply the recording UI immediately and re-populate
  // the inputs from what _rec remembers, or the restored panel would be
  // internally contradictory (running timer, but "Start recording" visible)
  // and a click would silently end the meeting. Fix-wave-2 item 1 (was [C]
  // in final-review.md).
  if (_rec) {
    const titleEl = document.getElementById('meeting-title');
    const agendaEl = document.getElementById('meeting-agenda');
    const termsEl = document.getElementById('meeting-terms');
    if (titleEl) titleEl.value = _rec.title || '';
    if (agendaEl) agendaEl.value = _rec.agenda || '';
    if (termsEl) termsEl.value = _rec.keyTerms || '';
    _setRecordingUI(true);
    _tickTimer();
  }

  _renderList();
  _fetchMeetings().then(() => {
    _renderList();
    _meetings.filter((m) => m.status === 'processing').forEach((m) => _startPolling(m.id));
  });
}

export function closePanel(direction) {
  if (!_open) return;
  _open = false;
  document.body.classList.remove('meetings-view');
  const minimize = direction === 'down';
  if (minimize) {
    _ensureMeetingsChipRegistered();
  } else {
    // Full close (not minimize-to-chip): stop an in-flight recording rather
    // than leaving a MediaRecorder running against a torn-down panel.
    if (_rec) _stopRecording();
    if (_Modals?.isRegistered?.('meetings-panel')) _Modals.unregister('meetings-panel');
  }
  _stopPollTimer();
  document.getElementById('tool-meetings-btn')?.classList.remove('active');
  document.getElementById('meetings-pane')?.remove();
  document.getElementById('meetings-pane-backdrop')?.remove();
  if (minimize) { try { _Modals?.minimize?.('meetings-panel'); } catch (_) {} }
}

export function togglePanel() {
  if (_open) closePanel();
  else openPanel();
}

export function isPanelOpen() { return _open; }

const meetingsModule = { openPanel, closePanel, togglePanel, isPanelOpen };
export default meetingsModule;
if (typeof window !== 'undefined') {
  window.meetingsModule = meetingsModule;
}
