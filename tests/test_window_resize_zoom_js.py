"""Pin the ui-scale-125 zoom fix in windowResize.js (issue #122, remainder).

Source-text assertions only, following the precedent set by
test_notes_checklist_grab_zoom_js.py / test_notebook_workspace_static.py: this
repo has no build step and no JS DOM test runner, so the edge/corner-resize
pointer wiring in static/js/windowResize.js can't practically be driven at
runtime here.

Background (issue #122): windowDrag.js (#127) and the local-relative-delta
sites (#130) were fixed for the ui-scale-125 (`:root.ui-scale-125 { zoom:
1.25 }`) re-multiplication bug, but its resize twin — windowResize.js's
edge/corner drag helper — was left as the documented remainder. Same bug
class: `content.getBoundingClientRect()` and cursor `clientX/clientY` are
viewport-space (real px), but `content` renders inside the zoomed `:root`,
so any px value assigned to its `style.left/top/width/height` must be
divided by the effective zoom (`toLocalPx`) or it renders ~1.25x too big.

Three sites, mirroring windowDrag.js's own two (initial pin + live delta)
plus a size-persistence round-trip windowDrag.js doesn't have:
  1. begin() — pins the window to `position:fixed` using a fresh
     getBoundingClientRect() read (like windowDrag's `_startDrag`).
  2. move() — folds the live cursor delta into left/top/width/height and
     divides once at assignment (like windowDrag's `_onMove`).
  3. end() — persists the final size to localStorage; must store the LOCAL
     px equivalent, not the raw viewport-space rect, otherwise a saved size
     restored later (assigned directly to style.width/height) renders
     zoom^2 too big. This round-trip has no windowDrag.js equivalent.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = (_REPO / "static" / "js" / "windowResize.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    """Return the source text of a `  function <name>(...) { ... }`
    declaration (2-space indented, nested inside makeWindowResizable), from
    its `function` keyword up to (not including) the next 2-space-indented
    `function` declaration. Good enough for this flat, single-nesting-level
    file."""
    start_match = re.search(rf"^  function {re.escape(name)}\(", src, re.MULTILINE)
    assert start_match, f"function {name} not found in windowResize.js"
    start = start_match.start()
    next_match = re.search(r"^  function \w+\(", src[start_match.end():], re.MULTILINE)
    end = start_match.end() + next_match.start() if next_match else len(src)
    return src[start:end]


def test_imports_ui_zoom_helpers():
    assert "from './uiZoom.js'" in _SRC, (
        "windowResize.js must import zoomOf/toLocalPx, same as its "
        "windowDrag.js sibling (PR #127)."
    )


def test_begin_divides_left_top_width_height_by_zoom():
    body = _extract_function(_SRC, "begin")
    assert "toLocalPx(r.left" in body
    assert "toLocalPx(r.top" in body
    assert "toLocalPx(r.width" in body
    assert "toLocalPx(r.height" in body


def test_move_divides_final_assignment_by_zoom():
    body = _extract_function(_SRC, "move")
    assert "toLocalPx(left" in body
    assert "toLocalPx(top" in body
    assert "toLocalPx(width" in body
    assert "toLocalPx(height" in body


def test_move_min_size_clamps_stay_viewport_space():
    # minW/minH are compared directly against the viewport-space width/
    # height inside move()'s gesture math — same shape as windowDrag.js's
    # SNAP_PX/DOCK_EDGE_PX compared against raw cx/cy (PR #127), NOT #130's
    # "constant added at the local-px assignment site" precedent. Guard
    # against a future "convert everything" overcorrection: this used to be
    # `_minW = minW * _z` in an earlier draft of this fix, which silently
    # raised the real-screen resize floor from 320 to 400px at 125% zoom.
    body = _extract_function(_SRC, "move")
    assert "width < minW" in body
    assert "height < minH" in body
    assert "minW * " not in body and "minH * " not in body


def test_end_persists_local_px_not_raw_viewport_rect():
    body = _extract_function(_SRC, "end")
    assert "toLocalPx(r.width" in body and "toLocalPx(r.height" in body, (
        "end() must persist toLocalPx(r.width/height) to localStorage, not "
        "the raw getBoundingClientRect() value — otherwise a saved size "
        "restored later renders zoom^2 too big under ui-scale-125."
    )


def test_restore_compares_saved_size_against_local_viewport_width():
    # The `requestAnimationFrame` restore block below `end()` (still inside
    # makeWindowResizable) clamps the persisted size against
    # window.innerWidth/innerHeight — those are viewport-space, but the
    # persisted size (post-fix) is local, so the comparison needs
    # toLocalPx(window.innerWidth/innerHeight) too.
    assert "toLocalPx(window.innerWidth" in _SRC
    assert "toLocalPx(window.innerHeight" in _SRC


def test_restore_converts_minw_minh_to_local_too():
    # minW/minH are viewport-space (see move()'s test above), but here
    # they're compared against `saved.w/h`, which is LOCAL (post end()'s
    # fix) — so, unlike in move(), the restore path needs
    # toLocalPx(minW/minH) to put both sides of the Math.max in local units.
    assert "toLocalPx(minW" in _SRC
    assert "toLocalPx(minH" in _SRC
