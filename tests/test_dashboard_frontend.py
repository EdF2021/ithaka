"""Dashboard (Home) start page — frontend regressions.

String-level assertions over the static assets, matching the lightweight
style of the other frontend tests in this suite (see test_dialog_aria.py):
no browser, just guard that the wiring the dashboard depends on stays put.
"""
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_INDEX = (_REPO / "static" / "index.html").read_text(encoding="utf-8")
_DASH = (_REPO / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
_APP = (_REPO / "static" / "app.js").read_text(encoding="utf-8")
_MODALS = (_REPO / "static" / "js" / "modalManager.js").read_text(encoding="utf-8")
_CSS = (_REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_sidebar_has_dashboard_nav_item():
    assert 'id="tool-dashboard-btn"' in _INDEX
    assert '<span class="grow">Home</span>' in _INDEX


def test_dashboard_is_first_tool_item():
    # "Home" must stay the FIRST item in the tools section (before Brain).
    assert _INDEX.index('id="tool-dashboard-btn"') < _INDEX.index('id="tool-memory-btn"')


def test_dashboard_js_fetches_the_five_endpoints():
    for endpoint in (
        "/api/calendar/events?start=today&end=tomorrow",
        "/api/tasks?status=active",
        "/api/email/unread-state",
        "/api/sessions",
        "/api/models",
    ):
        assert endpoint in _DASH, f"dashboard.js no longer fetches {endpoint}"


def test_dashboard_js_exports_open_close_isopen():
    assert "export function openDashboard()" in _DASH
    assert "export function closeDashboard()" in _DASH
    assert "export function isDashboardOpen()" in _DASH


def test_dashboard_startup_pref_roundtrip():
    # Toggle reads AND writes the pref; GET returns {key, value} where an
    # unset key yields value null → treated as ON (only explicit false is off).
    assert "/api/prefs/dashboard_autoopen" in _DASH
    assert "method: 'PUT'" in _DASH
    assert "d.value !== false" in _DASH


def test_app_has_dashboard_route_and_autoopen():
    assert "'/dashboard'" in _APP
    assert "tool-dashboard-btn" in _APP
    # Auto-open only fires when no URL route matched (the else-branch of the
    # route-opener block) and respects the stored pref.
    assert "/api/prefs/dashboard_autoopen" in _APP
    assert "d.value === false" in _APP


def test_modal_manager_registers_dashboard():
    # Dock chip label + auto-wire entry so minimize/restore and the sidebar
    # badge work like the other tools.
    assert "'dashboard-modal'" in _MODALS
    assert "tool-dashboard-btn" in _MODALS


def test_dashboard_styles_present_and_responsive():
    assert ".dashboard-grid" in _CSS
    assert "repeat(auto-fill, minmax(280px, 1fr))" in _CSS
    # Narrow-viewport stacking rule.
    assert ".dashboard-grid { grid-template-columns: 1fr; }" in _CSS
