"""Dageraad theme preset — frontend regressions.

Phase 1 of the Dageraad port: theme tokens + a CSS-only ambient background,
no layout changes, no intro animation. String-level assertions over the
static assets, matching the lightweight style of test_dashboard_frontend.py:
no browser, just guard that the wiring stays put.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_THEME_JS = (_REPO / "static" / "js" / "theme.js").read_text(encoding="utf-8")
_CSS = (_REPO / "static" / "style.css").read_text(encoding="utf-8")
_INDEX = (_REPO / "static" / "index.html").read_text(encoding="utf-8")
_DASHBOARD_JS = (_REPO / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")


def test_dageraad_preset_registered_with_base_tokens():
    assert "dageraad:" in _THEME_JS
    assert "bg:'#0A1220'" in _THEME_JS
    assert "fg:'#EDF2F8'" in _THEME_JS
    assert "panel:'#111927'" in _THEME_JS
    assert "border:'#232C3B'" in _THEME_JS
    assert "red:'#D77A8C'" in _THEME_JS


def test_dageraad_advanced_tokens_use_camelcase_keys():
    # ADV_KEYS are camelCase (sendBtnBg, not send-btn-bg) — a kebab key
    # would silently fall back to computeAdvancedDefaults().
    for key, value in (
        ("sendBtnBg", "#F0A868"),
        ("sendBtnHover", "#F5B87E"),
        ("userBubbleBg", "#1A2333"),
        ("aiBubbleBg", "#0E1624"),
        ("bubbleBorder", "#242E40"),
        ("sidebarBg", "#050B14"),
        ("brandColor", "#F0A868"),
        ("inputBg", "#0E1624"),
        ("inputBorder", "#242E40"),
        ("codeBg", "#0B111C"),
        ("codeFg", "#EDF2F8"),
        ("toggleActive", "#F0A868"),
    ):
        assert f"{key}: '{value}'" in _THEME_JS, f"missing advanced token {key}"


def test_dageraad_gloed_pattern_registered_in_theme_js():
    assert "dageraad:   'dageraad-gloed'" in _THEME_JS  # THEME_DEFAULT_PATTERN
    assert "'bg-pattern-dageraad-gloed'" in _THEME_JS  # _BG_CLASSES
    assert "'dageraad-gloed'" in _THEME_JS  # _STATIC_PATTERNS (no live sliders)


def test_dageraad_gloed_css_rule_present_and_css_only():
    assert "body.bg-pattern-dageraad-gloed" in _CSS
    # CSS-only pattern: no canvas element / JS init function for it.
    assert "dageraad-gloed-canvas" not in _CSS


def test_dageraad_gloed_option_in_bg_pattern_select():
    assert '<option value="dageraad-gloed">Dageraad-gloed</option>' in _INDEX


def test_theme_identity_hook_present():
    # data-theme mirrors the active theme name on <html>, kept in sync from
    # save() plus the sites that apply a theme without calling save() —
    # the hook later Dageraad phases scope CSS off of.
    assert "documentElement.dataset.theme" in _THEME_JS
    assert "_setThemeIdentity" in _THEME_JS
    assert "documentElement.dataset.theme" in _INDEX


# ── Phase 2: layout-polish, scoped to [data-theme="dageraad"] ──────────────


def test_dageraad_scoped_layout_block_present():
    # One dedicated, clearly-marked section — not scattered inline overrides.
    assert "Dageraad theme-scoped layout" in _CSS
    assert ':root[data-theme="dageraad"] .msg' in _CSS
    assert ':root[data-theme="dageraad"] .chat-input-bar' in _CSS
    assert ':root[data-theme="dageraad"] .modal-content' in _CSS
    assert ':root[data-theme="dageraad"] .dashboard-card' in _CSS
    assert ':root[data-theme="dageraad"] .list-item span' in _CSS


def test_dageraad_composer_focus_glow_is_amber():
    assert ':root[data-theme="dageraad"] .chat-input-bar:focus-within' in _CSS
    assert "rgba(240, 168, 104, 0.25)" in _CSS or "240, 168, 104, 0.25" in _CSS


def test_dageraad_dashboard_card_hover_uses_transition_not_transform():
    # Dageraad convention: things light up, no tilts/transforms on hover.
    hover_block = re.search(
        r':root\[data-theme="dageraad"\] \.dashboard-card-clickable:hover \{[^}]*\}',
        _CSS,
    )
    assert hover_block, "dageraad dashboard-card hover rule not found"
    assert "transform" not in hover_block.group(0)
    card_block = re.search(
        r':root\[data-theme="dageraad"\] \.dashboard-card \{[^}]*\}', _CSS
    )
    assert card_block and "cubic-bezier(0.22, 1, 0.36, 1)" in card_block.group(0)


def test_dageraad_dashboard_greeting_helper_present():
    assert "_dashboardGreeting" in _DASHBOARD_JS
    assert "Goedenacht" in _DASHBOARD_JS
    assert "Goedemorgen" in _DASHBOARD_JS
    assert "Goedemiddag" in _DASHBOARD_JS
    assert "Goedenavond" in _DASHBOARD_JS
    # Only the Dageraad theme swaps the header; every other theme keeps "Home".
    assert "dataset.theme === 'dageraad'" in _DASHBOARD_JS
    assert "'Home'" in _DASHBOARD_JS


def test_legacy_light_colors_removed():
    # .agent-controls / .agent-progress / .workflow-info hard-coded
    # #f8f9fa / #ffebee collided with every dark theme. Neither hex appears
    # in @media print either (that block already uses its own #fff/#000
    # literals for the forced-light printout), so a flat "not in" holds.
    assert "#ffebee" not in _CSS
    assert "#f8f9fa" not in _CSS
