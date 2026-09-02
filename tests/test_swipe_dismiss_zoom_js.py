"""Pin the ui-scale-125 zoom fix in the swipe-down-to-dismiss gesture
(issue #122 remainder — "doclib-pos-restje").

Source-text assertions only, same precedent as
test_window_resize_zoom_js.py / test_notes_checklist_grab_zoom_js.py.

Background: documentLibrary.js's `_showLibDropdown` popup has a
swipe-down-to-dismiss gesture (mobile) that tracks a touch `clientY` delta
and assigns it straight to `style.transform = 'translateY(<dy>px)'`. The
popup lives inside the zoomed `:root` (`:root.ui-scale-125 { zoom: 1.25 }`),
and `zoom` re-multiplies any px length assigned within its subtree — the
same bug class already fixed for position:top/left (#76/#77/#121/#127) and
local-relative deltas (#130), just on a `transform` instead. Undivided, the
popup outruns the finger under ui-scale-125.

memory.js's kebab-menu dropdown carries a literal copy of the same gesture
("mirrors the documents library popup gesture" per its own comment) and has
the identical bug.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DOCLIB = (_REPO / "static" / "js" / "documentLibrary.js").read_text(encoding="utf-8")
_MEMORY = (_REPO / "static" / "js" / "memory.js").read_text(encoding="utf-8")

_TRANSLATE_Y_RE = re.compile(r"style\.transform = 'translateY\('\s*\+\s*(.+?)\s*\+\s*'px\)'")


def _find_swipe_translate_arg(src: str) -> str:
    """Locate the swipe-dismiss `style.transform = 'translateY(' + X + 'px')`
    assignment and return the expression assigned to X."""
    m = _TRANSLATE_Y_RE.search(src)
    assert m, "swipe-dismiss translateY assignment not found"
    return m.group(1)


def test_doclib_swipe_dismiss_divides_dy_by_zoom():
    arg = _find_swipe_translate_arg(_DOCLIB)
    assert "toLocalPx(dy" in arg, (
        "documentLibrary.js's swipe-to-dismiss transform must divide the "
        "viewport-space touch delta (dy) by the effective zoom before "
        "assigning it to translateY, or the popup will render faster than "
        "the finger under ui-scale-125."
    )


def test_memory_swipe_dismiss_divides_dy_by_zoom():
    arg = _find_swipe_translate_arg(_MEMORY)
    assert "toLocalPx(dy" in arg, (
        "memory.js's swipe-to-dismiss transform (a literal copy of "
        "documentLibrary.js's gesture, per its own comment) must divide dy "
        "by the effective zoom the same way."
    )


def test_doclib_release_threshold_stays_viewport_space():
    # The >60px release threshold and the 120px snap-away distance are a
    # gesture-distance tolerance and a handwritten local constant
    # respectively — neither should be wrapped in toLocalPx (same
    # precedent as windowDrag.js's SNAP_PX/DOCK_EDGE_PX and #130's gap
    # constants). Guard against a future "convert everything" overcorrection.
    assert "_swipeDy > 60" in _DOCLIB
    assert "translateY(120px)" in _DOCLIB


def test_memory_release_threshold_stays_viewport_space():
    assert "_swDy > 60" in _MEMORY
    assert "translateY(120px)" in _MEMORY
