"""Static regressions for issue #13: settings tabs that are functionally
admin-only (every field persists through an admin-only backend) but were
visually indistinguishable from regular tabs for non-admins, whose only
feedback on save was a bare "Admin only" backend string (or, worse, a false
"Saved"/"Shortcut saved" toast on a 403 that nothing checked for).

Source-text assertions only: this repo has no build step and no JS DOM test
runner (package.json carries no test/jsdom tooling; see the existing
test_setup_device_auth_static.py / test_admin_device_flow_static.py
precedent), so the gating logic added to static/js/settings.js and the
friendly-error copy in static/js/admin.js can't practically be driven at
runtime here. Assertions anchor on structural code (const names, function
bodies) rather than prose to survive incidental wording tweaks.
"""

from pathlib import Path


_REPO = Path(__file__).resolve().parent.parent
_SETTINGS = (_REPO / "static" / "js" / "settings.js").read_text(encoding="utf-8")
_ADMIN = (_REPO / "static" / "js" / "admin.js").read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


def test_admin_managed_tabs_cover_every_tab_whose_saves_hit_the_admin_only_endpoint():
    # ai/search/reminders/shortcuts render natively in settings.js and every
    # field on them persists via POST /api/auth/settings, which
    # routes/auth_routes.py rejects for non-admins. services/integrations
    # post to their own admin-only endpoints (/api/model-endpoints,
    # /api/auth/integrations) via admin.js. None of these are hidden by the
    # .admin-only CSS class the way tools/users/system are, so non-admins
    # could open, edit, and "save" them with no indication the write fails.
    assert (
        "const ADMIN_MANAGED_DISABLE_TABS = new Set(['ai', 'search', 'reminders', 'shortcuts']);"
        in _SETTINGS
    )
    assert (
        "const ADMIN_MANAGED_MARK_ONLY_TABS = new Set(['services', 'integrations']);"
        in _SETTINGS
    )


def test_sync_admin_visibility_calls_the_admin_managed_tab_gating_every_open():
    # open() is the single choke point every caller in the app uses to show
    # the settings modal (modelPicker.js, keyboard-shortcuts.js,
    # slashCommands.js, admin.js, calendar.js), and it always runs
    # syncAdminVisibility(). Wiring the gate there (rather than at
    # initAll()-time, when window._isAdmin may not be populated yet) means
    # it re-applies correctly on every open, including a tab switch routed
    # through admin.js's open().
    sync_block = _between(
        _SETTINGS, "function syncAdminVisibility() {", "\n}\n"
    )
    assert "syncAdminManagedTabs(isAdmin)" in sync_block


def test_admin_managed_tab_gating_marks_nav_items_and_disables_static_panels():
    gate_block = _between(
        _SETTINGS,
        "function syncAdminManagedTabs(isAdmin) {",
        "\n}\n\n/* ═",
    )
    # Every admin-managed tab gets a visible "Admin" pill on its nav item.
    assert "ADMIN_MANAGED_TABS.forEach(tab => {" in gate_block
    assert "admin-managed-pill" in gate_block
    assert "admin-badge" in gate_block  # reuse of the existing badge style, not a new component

    # Only the statically-rendered tabs (ai/search/reminders/shortcuts) get
    # their fields disabled and an explainer banner; services/integrations
    # are excluded because admin.js rebuilds their DOM asynchronously after
    # the tab opens, which would race a one-shot disable pass here.
    assert "ADMIN_MANAGED_DISABLE_TABS.forEach(tab => {" in gate_block
    assert "field.disabled = true" in gate_block
    assert "field.disabled = false" in gate_block
    assert "admin-managed-banner" in gate_block


def test_admin_managed_explainer_copy_matches_the_issue_wording():
    assert (
        "const ADMIN_MANAGED_EXPLAINER = "
        "'These settings are managed by an admin — ask your admin to change them.';"
        in _SETTINGS
    )


def test_add_models_admin_only_error_is_translated_to_friendly_copy():
    # admin.js:_friendlyAdminError is used at both Add Models save sites
    # (hosted-provider form and local-endpoint form) so a non-admin who
    # still manages to submit sees actionable copy instead of a bare
    # "Admin only" backend string.
    assert (
        "function _friendlyAdminError(detail) {\n"
        "  return detail === 'Admin only' ? 'Only an admin can change this. Ask your admin.' "
        ": (detail || 'Failed');\n"
        "}" in _ADMIN
    )
    assert _ADMIN.count("msg.textContent = _friendlyAdminError(d.detail);") == 2


def test_integrations_save_error_is_also_translated():
    # The Integrations tab (in ADMIN_MANAGED_MARK_ONLY_TABS) saves via
    # settings.js directly against the admin-only /api/auth/integrations
    # endpoint, so it needs the same friendly-error treatment as admin.js.
    assert (
        "function friendlyAdminError(detail) {\n"
        "  return detail === 'Admin only' ? 'Only an admin can change this. Ask your admin.' "
        ": (detail || 'Save failed');\n"
        "}" in _SETTINGS
    )
    assert "statusEl.textContent = friendlyAdminError(err.detail);" in _SETTINGS
