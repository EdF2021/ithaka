"""Pin the zoom-compensation math in static/js/uiZoom.js.

Driven through `node --input-type=module` so we exercise the real JS without a
full Vitest/Jest setup (same spirit as test_esc_menu_stack_js.py). Skips when
`node` is not installed rather than failing.

The module source is inlined into the eval'd module body (rather than imported
by path) so the test runs identically on Windows and POSIX — the repo has no
`"type": "module"` in package.json, so a path import of a `.js` file is treated
as CommonJS by node and rejects the ES `export`s. uiZoom.js has no imports of
its own, so inlining is exact.

Background (issue #77, same bug class as PR #76): with the UI text-scale
toggle on (`:root.ui-scale-125 { zoom: 1.25 }`), getBoundingClientRect() and
window.innerWidth/innerHeight report real viewport px, but a px value JS
assigns to style.top/left/right/bottom on a position:fixed, body-portaled
popup renders re-multiplied by the zoom (the popup still lives inside the
zoomed :root). toLocalPx() divides a viewport-space measurement by the zoom
before it's assigned so set-px and rendered-px line up again; zoomOf() reads
an element's effective zoom, defaulting to 1 so callers don't need to special-
case the 100%-scale / non-zoomed-browser case.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "uiZoom.js"
_HAS_NODE = shutil.which("node") is not None
_SRC = _HELPER.read_text(encoding="utf-8") if _HELPER.exists() else ""


def _run(body: str) -> str:
    """Run `body` as a module with the helper's exports already in scope."""
    js = _SRC + "\n" + body
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=js, capture_output=True, text=True, encoding="utf-8",
        cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_to_local_px_is_identity_at_default_scale():
    # 100% UI scale (no zoom): viewport px and local px must coincide exactly,
    # or every popup that was correctly positioned before #76/#77 would drift.
    body = "console.log(JSON.stringify(toLocalPx(240, 1)));"
    assert json.loads(_run(body)) == 240


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_to_local_px_divides_by_the_ui_scale_zoom():
    # :root.ui-scale-125 -> zoom 1.25 — a measured 300 real px must become 240
    # local px so the browser's re-multiplication renders it back at 300.
    body = "console.log(JSON.stringify(toLocalPx(300, 1.25)));"
    assert json.loads(_run(body)) == 240


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_to_local_px_treats_falsy_zoom_as_one():
    # Guards the `zoom || 1` fallback directly (undefined/0/null all mean
    # "no zoom in effect") — same defensiveness as PR #76's `|| 1`.
    body = """
    console.log(JSON.stringify([
      toLocalPx(100, undefined),
      toLocalPx(100, 0),
      toLocalPx(100, null),
    ]));
    """
    assert json.loads(_run(body)) == [100, 100, 100]


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_zoom_of_reads_current_css_zoom():
    body = "console.log(JSON.stringify(zoomOf({ currentCSSZoom: 1.25 })));"
    assert json.loads(_run(body)) == 1.25


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_zoom_of_defaults_to_one_when_unset_or_missing_element():
    # Unscaled UI (property absent/0) and a null/undefined element (an
    # unmounted popup, a browser without currentCSSZoom support) all fall
    # back to no compensation rather than throwing or dividing by zero.
    body = """
    console.log(JSON.stringify([
      zoomOf({ currentCSSZoom: 1 }),
      zoomOf({}),
      zoomOf(null),
      zoomOf(undefined),
    ]));
    """
    assert json.loads(_run(body)) == [1, 1, 1, 1]


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_round_trip_survives_the_zoom_multiplication():
    # Simulates what the browser does at render time: the popup's own zoom
    # (inherited from :root.ui-scale-125) multiplies whatever local px we
    # assign. A viewport-space measurement pushed through toLocalPx() must
    # come back out at the original real px once "rendered".
    body = """
    const z = 1.25;
    const measured = 517; // e.g. a kebab button's getBoundingClientRect().top
    const local = toLocalPx(measured, z);
    const rendered = local * z; // what the browser paints under zoom
    console.log(JSON.stringify(Math.abs(rendered - measured) < 1e-9));
    """
    assert json.loads(_run(body)) is True
