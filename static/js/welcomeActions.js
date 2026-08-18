// welcomeActions.js — first-run action-row visibility on the welcome screen
// (Connect a model / Take the tour / Help). Extracted out of models.js so
// this pure DOM-toggle logic stays node-testable without pulling in
// models.js's heavy import graph (ui.js, sessions.js, dragSort.js,
// chatRenderer.js), which reference browser-only globals at module scope.
//
// Regression test: tests/test_welcome_actions_js.py

/**
 * Pure: does this /api/models item list contain at least one usable chat
 * model (online endpoint with at least one curated or extra model)? Mirrors
 * _hasUsableChatModel()'s items.some(...) branch in app.js (~line 271)
 * field-for-field — models.js and helpPanel.js both call this so the
 * "is there a model to talk to" question is answered identically everywhere.
 *
 * @param {Array<object>} items - /api/models items (or _cachedItems).
 */
export function hasUsableModel(items) {
  return (items || []).some(
    (item) => !item.offline && ((item.models || []).length || (item.models_extra || []).length)
  );
}

/**
 * Show/hide the individual buttons in the (always-visible) #welcome-actions
 * row based on whether there's a usable model yet and whether the current
 * user is an admin.
 *
 * The row itself is always `display:flex` — the welcome screen as a whole
 * (not this row) controls whether the first-run area is shown at all. Within
 * the row:
 *  - Connect a model: only admins can add endpoints, and only when there's
 *    nothing usable yet — it's the primary CTA out of a dead-end first run.
 *  - Take the tour: needs a model to actually run (the tour drives real
 *    chat turns), so it only appears once one is usable — gating it on
 *    hasUsableModel instead of admin-only avoids leaving non-admins on a
 *    first-run screen with nothing but Help.
 *  - Help: always visible (not managed by this row; see helpPanel.js).
 *
 * @param {boolean} hasUsableModel - see hasUsableModel() above.
 * @param {boolean} isAdmin - window._isAdmin.
 */
export function _setWelcomeFirstRun(hasUsableModel, isAdmin) {
  const actions = document.getElementById('welcome-actions');
  if (!actions) return;
  actions.style.display = 'flex';
  const connectBtn = document.getElementById('welcome-connect-btn');
  const tourBtn = document.getElementById('welcome-tour-btn');
  if (connectBtn) connectBtn.style.display = (!hasUsableModel && isAdmin) ? '' : 'none';
  if (tourBtn) tourBtn.style.display = hasUsableModel ? '' : 'none';
}

export default { _setWelcomeFirstRun, hasUsableModel };
