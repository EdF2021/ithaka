// welcomeActions.js — first-run action-row visibility on the welcome screen
// (Connect a model / Take the tour / Help). Extracted out of models.js so
// this pure DOM-toggle logic stays node-testable without pulling in
// models.js's heavy import graph (ui.js, sessions.js, dragSort.js,
// chatRenderer.js), which reference browser-only globals at module scope.
//
// Regression test: tests/test_welcome_actions_js.py

/**
 * Show/hide the #welcome-actions row based on whether any model endpoints
 * are configured yet (first-run signal) and whether the current user is an
 * admin. Only admins can add endpoints, so only admins get the
 * Connect-a-model / Take-the-tour buttons; non-admins in a first-run state
 * see just the (always-present) Help button.
 *
 * @param {boolean} hasEndpoints - true when _cachedItems has entries.
 * @param {boolean} isAdmin - window._isAdmin.
 */
export function _setWelcomeFirstRun(hasEndpoints, isAdmin) {
  const actions = document.getElementById('welcome-actions');
  if (!actions) return;
  if (hasEndpoints) {
    // Configured installs should feel ready, not stuck in onboarding —
    // Help remains permanently reachable from the sidebar (see helpPanel.js).
    actions.style.display = 'none';
    return;
  }
  actions.style.display = 'flex';
  const connectBtn = document.getElementById('welcome-connect-btn');
  const tourBtn = document.getElementById('welcome-tour-btn');
  if (connectBtn) connectBtn.style.display = isAdmin ? '' : 'none';
  if (tourBtn) tourBtn.style.display = isAdmin ? '' : 'none';
}

export default { _setWelcomeFirstRun };
