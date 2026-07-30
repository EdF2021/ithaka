/**
 * Dageraad intro choreography — Fase 3 of the Dageraad port.
 *
 * A short cinematic overlay (~2.2-2.6s) that plays once per page load, only
 * when the Dageraad theme is active and the visitor hasn't asked for
 * reduced motion. Purely visual, layered on top of the already-initialized
 * app — never blocks session loading. Pattern mirrored from dashboard.js:
 * a small self-contained module, plain DOM building, no framework.
 *
 * Reference choreography: docs/design/dageraad-brief.md ("De opening") and
 * docs/design/dageraad-mockup.html (phases p-warm/p-title/p-collapse/
 * p-done). Ported here compacted to ~2.2-2.6s, driven by fixed setTimeouts
 * toggling phase classes on a dedicated overlay element instead of <html>.
 */

// [delayMs, phaseClass] — mirrors the mockup's PHASES timeline, compacted.
const PHASES = [
  [200, 'dageraad-intro-phase-warm'],
  [600, 'dageraad-intro-phase-title'],
  [1300, 'dageraad-intro-phase-collapse'],
  [2200, 'dageraad-intro-phase-done'],
];
// How long after the last phase fires before the overlay is torn down —
// gives the opacity/transform transitions kicked off at that phase time to
// finish before the node leaves the DOM.
const CLEANUP_DELAY_MS = 700;

let _playedThisLoad = false;
let _active = null; // { overlay, titleEl, timers, cleanupTimer, onKey, onClick }

function _prefersReducedMotion() {
  return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

function _buildOverlay() {
  const overlay = document.createElement('div');
  overlay.className = 'dageraad-intro-overlay';
  overlay.setAttribute('aria-hidden', 'true');
  overlay.innerHTML =
    '<div class="dageraad-intro-sky"></div>' +
    '<div class="dageraad-intro-horizon"></div>' +
    '<div class="dageraad-intro-center">' +
    '<span class="dageraad-intro-title">Ithaka</span>' +
    '<span class="dageraad-intro-tagline">Yours for the voyage.</span>' +
    '</div>';
  return overlay;
}

/** FLIP: aim the centered hero title at the sidebar brand's current rect.
 *  Falls back to a plain fade when the brand isn't visible (mobile /
 *  collapsed sidebar) instead of morphing toward a hidden target. */
function _flipTitleToBrand(titleEl) {
  const brandTitle = document.querySelector('.sidebar-brand-title');
  const visible = brandTitle && brandTitle.offsetParent !== null;
  if (!visible) {
    titleEl.classList.add('dageraad-intro-title-fade-only');
    return;
  }
  const from = titleEl.getBoundingClientRect();
  const to = brandTitle.getBoundingClientRect();
  if (!from.width || !to.width) {
    titleEl.classList.add('dageraad-intro-title-fade-only');
    return;
  }
  const tx = (to.left + to.width / 2) - (from.left + from.width / 2);
  const ty = (to.top + to.height / 2) - (from.top + from.height / 2);
  const scale = Math.max(0.12, to.height / from.height);
  titleEl.style.setProperty('--dageraad-intro-tx', `${tx.toFixed(1)}px`);
  titleEl.style.setProperty('--dageraad-intro-ty', `${ty.toFixed(1)}px`);
  titleEl.style.setProperty('--dageraad-intro-scale', scale.toFixed(3));
}

function _cleanup(state) {
  if (!state) return;
  state.timers.forEach(clearTimeout);
  if (state.cleanupTimer) clearTimeout(state.cleanupTimer);
  document.removeEventListener('keydown', state.onKey);
  state.overlay.removeEventListener('click', state.onClick);
  if (state.overlay.parentElement) state.overlay.remove();
  document.body.classList.remove('dageraad-intro-reveal');
  if (_active === state) _active = null;
}

/** Skip: jump straight to the end state — overlay gone, no half-finished
 *  transitions left showing. The rest of the app was always underneath
 *  (it never itself animates in unless dageraad-intro-reveal is applied),
 *  so removing the overlay immediately is enough. */
function _skip(state) {
  _cleanup(state);
}

export function playDageraadIntro() {
  // Replay clicked mid-animation, or called again for any reason — tear
  // down the in-flight run first so timers/listeners never double up.
  if (_active) _cleanup(_active);

  const overlay = _buildOverlay();
  document.body.appendChild(overlay);
  const titleEl = overlay.querySelector('.dageraad-intro-title');

  const state = { overlay, titleEl, timers: [], cleanupTimer: null, onKey: null, onClick: null };
  _active = state;

  state.onKey = (e) => { if (e.key === 'Escape') _skip(state); };
  state.onClick = () => _skip(state);
  document.addEventListener('keydown', state.onKey);
  overlay.addEventListener('click', state.onClick);

  if (_prefersReducedMotion()) {
    // The CSS reduced-motion guard already hides the overlay outright;
    // still run the full teardown so no listeners/DOM linger.
    _skip(state);
    return;
  }

  PHASES.forEach(([delay, cls]) => {
    state.timers.push(setTimeout(() => {
      if (cls === 'dageraad-intro-phase-collapse') {
        _flipTitleToBrand(titleEl);
        document.body.classList.add('dageraad-intro-reveal');
      }
      overlay.classList.add(cls);
    }, delay));
  });

  const lastPhaseDelay = PHASES[PHASES.length - 1][0];
  state.cleanupTimer = setTimeout(() => _cleanup(state), lastPhaseDelay + CLEANUP_DELAY_MS);
}

/** Auto-play gate: only when Dageraad is the active theme, motion isn't
 *  reduced, and this page load hasn't already played it. No-op otherwise.
 *  Call once, early, after the theme has been applied to <html>. */
export function maybePlayDageraadIntro() {
  if (_playedThisLoad) return;
  if (document.documentElement.dataset.theme !== 'dageraad') return;
  if (_prefersReducedMotion()) return;
  _playedThisLoad = true;
  playDageraadIntro();
}

function _wireReplayButton() {
  const btn = document.getElementById('dageraad-replay-intro-btn');
  if (!btn || btn.dataset.dageraadWired) return;
  btn.dataset.dageraadWired = '1';
  btn.addEventListener('click', () => playDageraadIntro());
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _wireReplayButton, { once: true });
} else {
  _wireReplayButton();
}

export default { maybePlayDageraadIntro, playDageraadIntro };
