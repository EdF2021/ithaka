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
// Regression test: tests/test_help_panel_js.py

// Tour key -> slash command. Mirrors TOUR_FOR_MODAL in tourAutoplay.js (the
// 7 per-feature tours) plus the general `/tour` walkthrough.
export const TOURS = [
  { key: 'general', label: 'General tour', cmd: '/tour' },
  { key: 'library', label: 'Library', cmd: '/tour-library' },
  { key: 'cookbook', label: 'Cookbook', cmd: '/tour-cookbook' },
  { key: 'research', label: 'Research', cmd: '/tour-research' },
  { key: 'compare', label: 'Compare', cmd: '/tour-compare' },
  { key: 'theme', label: 'Theme', cmd: '/tour-theme' },
  { key: 'settings', label: 'Settings', cmd: '/tour-settings' },
  { key: 'gallery', label: 'Gallery', cmd: '/tour-gallery' },
];

// Key slash commands surfaced as fill-only chips (user completes them).
export const COMMANDS = [
  { cmd: '/setup', desc: 'Connect a model or provider' },
  { cmd: '/tour', desc: 'Guided tour of the app' },
  { cmd: '/theme', desc: 'Change the look and feel' },
  { cmd: '/research', desc: 'Start a deep research task' },
  { cmd: '/compare', desc: 'Compare responses across models' },
];

const MOBILE_BREAKPOINT = 768;

/** Pure: tour key -> its slash command, or null if unknown. */
export function _tourCommandFor(key) {
  const t = TOURS.find((x) => x.key === key);
  return t ? t.cmd : null;
}

/** Pure: is this viewport width the mobile layout (tours are desktop-only)? */
export function _isMobileWidth(width) {
  return width <= MOBILE_BREAKPOINT;
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
    // Fill only — the user completes the command themselves.
    chip.addEventListener('click', () => _fillMessage(c.cmd));
    const desc = document.createElement('span');
    desc.className = 'help-command-desc';
    desc.textContent = c.desc;
    row.appendChild(chip);
    row.appendChild(desc);
    list.appendChild(row);
  });
}

// Tours are desktop-only (see tourAutoplay.js header comment): hide the
// section entirely on mobile and show a plain-text note instead.
function _renderResponsiveState() {
  const toursSection = document.getElementById('help-tours-section');
  const mobileNote = document.getElementById('help-tours-mobile-note');
  const mobile = _isMobileWidth(window.innerWidth || 0);
  if (toursSection) toursSection.style.display = mobile ? 'none' : '';
  if (mobileNote) mobileNote.style.display = mobile ? '' : 'none';
}

export function open() {
  const panel = document.getElementById('help-panel');
  if (!panel) return;
  _renderResponsiveState();
  // Evaluated per-open: window._isAdmin is set asynchronously after auth,
  // so an init-time check would race it.
  const modelSection = document.getElementById('help-model-section');
  if (modelSection) modelSection.style.display = window._isAdmin ? '' : 'none';
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

const helpPanelModule = { open, close, init, TOURS, COMMANDS, _tourCommandFor, _isMobileWidth };
export default helpPanelModule;
