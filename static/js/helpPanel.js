// helpPanel.js — floating Help window: tours, a slash-command quick
// reference, and the "Connect a model" shortcut. Static content only, no
// server calls. Also owns the permanent Help entry in the sidebar user-bar
// and the first-run welcome-screen action buttons (Part 1/3 of issue #11).
//
// Follows the small #theme-modal pattern: plain `.modal` / `.modal-content`
// / `.modal-header` / `.close-btn` markup. Backdrop-tap-to-close, Escape-to
// -close, z-index raising, and the mobile bottom-sheet layout are already
// handled generically for any `.modal` element by ui.js — nothing extra
// needed here beyond toggling the `.hidden` class.
//
// Self-initializes on import, mirroring tourAutoplay.js, so a single
// `<script type="module" src=".../helpPanel.js">` tag in index.html is
// enough to wire everything up.
//
// The mobile/desktop split for the Tours section is pure CSS (style.css:
// #help-tours-section / #help-tours-mobile-note under @media (max-width:768px))
// so it's reactive to resize without any JS involvement here.
//
// Regression test: tests/test_help_panel_js.py

import { hasUsableModel } from './welcomeActions.js';

// Tour key -> slash command. Mirrors TOUR_FOR_MODAL in tourAutoplay.js and
// the `tour-*` registry in slashCommands.js (the 10 per-feature tours) plus
// the general `/tour` walkthrough. Kept in sync with the registry by
// test_help_panel_js.py's drift-guard test.
export const TOURS = [
  { key: 'general', label: 'General tour', cmd: '/tour' },
  { key: 'library', label: 'Library', cmd: '/tour-library' },
  { key: 'cookbook', label: 'Cookbook', cmd: '/tour-cookbook' },
  { key: 'research', label: 'Research', cmd: '/tour-research' },
  { key: 'compare', label: 'Compare', cmd: '/tour-compare' },
  { key: 'theme', label: 'Theme', cmd: '/tour-theme' },
  { key: 'settings', label: 'Settings', cmd: '/tour-settings' },
  { key: 'gallery', label: 'Gallery', cmd: '/tour-gallery' },
  { key: 'brain', label: 'Brain', cmd: '/tour-brain' },
  { key: 'task1', label: 'Tasks: built-ins', cmd: '/tour-task-1' },
  { key: 'task2', label: 'Tasks: add & manage', cmd: '/tour-task-2' },
];

// Key slash commands surfaced as fill-only chips (user completes them).
// Descriptions mirror the `help` strings in slashCommands.js's registry.
export const COMMANDS = [
  { cmd: '/setup', desc: 'Add local or API model endpoints' },
  { cmd: '/tour', desc: 'Full guided product tour' },
  { cmd: '/theme', desc: 'Change color theme' },
  { cmd: '/research', desc: 'Open Deep Research' },
  { cmd: '/compare', desc: 'Open Compare' },
];

/** Pure: tour key -> its slash command, or null if unknown. */
export function _tourCommandFor(key) {
  const t = TOURS.find((x) => x.key === key);
  return t ? t.cmd : null;
}

function _fillMessage(cmd) {
  const messageInput = document.getElementById('message');
  if (!messageInput) return;
  messageInput.value = cmd;
  messageInput.dispatchEvent(new Event('input', { bubbles: true }));
  messageInput.focus();
}

// Exact same mechanism as the .setup-trigger-link handler in
// slashCommands.js: fill #message, then submit #chat-form.
function _fillAndSubmit(cmd) {
  _fillMessage(cmd);
  // Don't auto-submit into an in-flight stream: chat.js's #chat-form submit
  // handler treats a submit while streaming as "stop", so dispatching it
  // here would abort the user's active stream instead of sending `cmd`.
  // Leave the filled input for them to send once it's done.
  // hasActiveStream() is keyed by session id (chat.js), so the current
  // session id has to be passed through explicitly — calling it bare would
  // always read as "no active stream".
  const sid = window.sessionModule?.getCurrentSessionId?.();
  if (window.chatModule?.hasActiveStream?.(sid)) return;
  const chatForm = document.getElementById('chat-form');
  if (chatForm) {
    chatForm.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
  }
}

function _connectModel() {
  if (window.adminModule?.open) window.adminModule.open('services');
  else window.settingsModule?.open?.('services');
}

function _buildTourList() {
  const list = document.getElementById('help-tours-list');
  if (!list) return;
  list.innerHTML = '';
  TOURS.forEach((t) => {
    const item = document.createElement('div');
    item.className = 'help-tour-item';
    item.textContent = t.label;
    item.addEventListener('click', () => {
      close();
      _fillAndSubmit(t.cmd);
    });
    list.appendChild(item);
  });
}

function _buildCommandChips() {
  const list = document.getElementById('help-commands-list');
  if (!list) return;
  list.innerHTML = '';
  COMMANDS.forEach((c) => {
    const row = document.createElement('div');
    row.className = 'help-command-row';
    const chip = document.createElement('span');
    chip.className = 'help-command-chip';
    chip.textContent = c.cmd;
    chip.title = c.desc;
    // Fill only (the user completes the command themselves) — but close the
    // panel first so the filled input is actually visible to type into.
    chip.addEventListener('click', () => { close(); _fillMessage(c.cmd); });
    const desc = document.createElement('span');
    desc.className = 'help-command-desc';
    desc.textContent = c.desc;
    row.appendChild(chip);
    row.appendChild(desc);
    list.appendChild(row);
  });
}

export function open() {
  const panel = document.getElementById('help-panel');
  if (!panel) return;
  // Evaluated per-open: window._isAdmin is set asynchronously after auth,
  // and the model list can change between opens, so an init-time check
  // would race both.
  const modelSection = document.getElementById('help-model-section');
  if (modelSection) {
    const items = window.modelsModule?.getCachedItems?.() || [];
    const showModelSection = !!window._isAdmin && !hasUsableModel(items);
    modelSection.style.display = showModelSection ? '' : 'none';
  }
  panel.classList.remove('hidden');
}

export function close() {
  const panel = document.getElementById('help-panel');
  if (!panel) return;
  panel.classList.add('hidden');
}

export function init() {
  _buildTourList();
  _buildCommandChips();

  const closeBtn = document.getElementById('help-panel-close-btn');
  if (closeBtn) closeBtn.addEventListener('click', () => close());

  const connectBtn = document.getElementById('help-connect-btn');
  if (connectBtn) connectBtn.addEventListener('click', () => { close(); _connectModel(); });

  // Permanent sidebar entry (Part 3).
  const sidebarHelpBtn = document.getElementById('user-bar-help');
  if (sidebarHelpBtn) sidebarHelpBtn.addEventListener('click', () => open());

  // Welcome-screen first-run action buttons (Part 1).
  const welcomeHelpBtn = document.getElementById('welcome-help-btn');
  if (welcomeHelpBtn) welcomeHelpBtn.addEventListener('click', () => open());

  const welcomeConnectBtn = document.getElementById('welcome-connect-btn');
  if (welcomeConnectBtn) welcomeConnectBtn.addEventListener('click', () => _connectModel());

  const welcomeTourBtn = document.getElementById('welcome-tour-btn');
  if (welcomeTourBtn) welcomeTourBtn.addEventListener('click', () => _fillAndSubmit('/tour'));
}

if (typeof window !== 'undefined') {
  window.helpPanel = { open, close };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}

const helpPanelModule = { open, close, init, TOURS, COMMANDS, _tourCommandFor };
export default helpPanelModule;
