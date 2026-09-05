"""Task 4 (image/video auto-route plan): the "Video Generation" settings
card in static/index.html + its JS wiring in static/js/settings.js.

Mirrors the existing Image Generation card 1:1. This repo has no build step
and no JS DOM test runner (see tests/test_settings_admin_managed_tabs_static.py
precedent) so assertions anchor on structural source text plus a `node
--check` syntax pass, not a runtime DOM.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_INDEX = (_REPO / "static" / "index.html").read_text(encoding="utf-8")
_SETTINGS_JS = (_REPO / "static" / "js" / "settings.js").read_text(encoding="utf-8")


def test_video_card_ids_present_in_index_html():
    for element_id in (
        "set-videoEnabledToggle",
        "set-videoModelSelect",
        "set-videoResolutionSelect",
        "set-videoAspectSelect",
        "set-videoDurationSelect",
        "set-videoCostLine",
        "set-videoSettingsMsg",
    ):
        assert f'id="{element_id}"' in _INDEX, element_id


def test_video_card_sits_directly_under_image_generation_card():
    img_idx = _INDEX.index("Image Generation")
    video_idx = _INDEX.index("Video Generation")
    assert video_idx > img_idx
    # Nothing but the closing tags of the Image Generation card sits between
    # the two card headers (same admin-card sibling pattern as the spec
    # requires — "directly under").
    between = _INDEX[img_idx:video_idx]
    assert between.count("admin-card") == 1  # only the Image Generation card's own opening tag


def test_video_card_has_no_emoji():
    img_idx = _INDEX.index("Video Generation")
    card_start = _INDEX.rindex('<div class="admin-card">', 0, img_idx)
    card_end = _INDEX.index("</div>", _INDEX.index("set-videoSettingsMsg"))
    card_html = _INDEX[card_start:card_end]
    assert "svg" in card_html  # monochrome inline icon, not an emoji glyph
    # A crude but effective emoji-range scan (surrogate-pair astral emoji).
    assert not any(0x1F300 <= ord(ch) <= 0x1FAFF for ch in card_html)


def test_video_model_options_present():
    for model, label_fragment in (
        ("veo-3.1-generate-preview", "best"),
        ("veo-3.1-fast-generate-preview", "Fast"),
        ("veo-3.1-lite-generate-preview", "Lite"),
    ):
        assert f'value="{model}"' in _INDEX
    # Prices called out in the option labels per the plan.
    assert "$0.40/s" in _INDEX
    assert "$0.10/s" in _INDEX
    assert "$0.05/s" in _INDEX


def test_video_resolution_aspect_duration_options_present():
    assert 'id="set-videoResolutionSelect"' in _INDEX
    assert 'value="720p"' in _INDEX
    assert 'value="1080p"' in _INDEX
    assert 'id="set-videoAspectSelect"' in _INDEX
    assert 'value="16:9"' in _INDEX
    assert 'value="9:16"' in _INDEX
    assert 'id="set-videoDurationSelect"' in _INDEX
    for seconds in ("4", "6", "8"):
        assert f'value="{seconds}"' in _INDEX


def test_init_video_settings_defined_and_called():
    assert "async function initVideoSettings()" in _SETTINGS_JS
    assert "initVideoSettings();" in _SETTINGS_JS
    # Called next to initImageSettings() in initAll().
    init_all = _SETTINGS_JS[_SETTINGS_JS.index("function initAll()"):]
    img_pos = init_all.index("initImageSettings();")
    video_pos = init_all.index("initVideoSettings();")
    assert video_pos > img_pos


def test_init_video_settings_posts_the_five_keys():
    fn_start = _SETTINGS_JS.index("async function initVideoSettings()")
    fn_end = _SETTINGS_JS.index("\n}\n", fn_start)
    body = _SETTINGS_JS[fn_start:fn_end]
    for key in (
        "video_gen_enabled",
        "video_model",
        "video_resolution",
        "video_aspect_ratio",
        "video_duration_seconds",
    ):
        assert key in body
    assert "/api/auth/settings" in body
    # Failure path shows the server detail, like saveSTT since PR #146.
    assert "res.ok" in body
    assert "err.detail" in body


def test_init_video_settings_computes_cost_line():
    fn_start = _SETTINGS_JS.index("async function initVideoSettings()")
    fn_end = _SETTINGS_JS.index("\n}\n", fn_start)
    body = _SETTINGS_JS[fn_start:fn_end]
    assert "set-videoCostLine" in body
    # The price table itself may live just above the function (a shared
    # module-level const) rather than inline in the body.
    price_table_start = _SETTINGS_JS.index("VIDEO_PRICE_PER_SECOND")
    price_and_body = _SETTINGS_JS[price_table_start:fn_end]
    assert "0.40" in price_and_body
    assert "0.12" in price_and_body
    assert "0.08" in price_and_body


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_settings_js_syntax_is_valid():
    subprocess.run(
        ["node", "--check", str(_REPO / "static" / "js" / "settings.js")],
        check=True,
    )
