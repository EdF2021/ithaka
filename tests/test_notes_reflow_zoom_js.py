"""Pin the ui-scale-125 zoom fix in notes.js's FLIP reflow animations
(issue #122 remainder, found by independent review of PR #139).

Source-text assertions only, same precedent as test_window_resize_zoom_js.py
/ test_notes_checklist_grab_zoom_js.py: this repo has no build step and no JS
DOM test runner, so the card-reflow DOM wiring in static/js/notes.js can't
practically be driven at runtime here.

Background: `_animateReflow` (note-grid re-layout after pin/unpin/delete) and
the drag-reorder swap handler in `_bindCardEvents` both compute a FLIP delta
(`dx`/`dy`) between two `getBoundingClientRect()` reads and assigned it
straight to `card.style.transform = translate(${dx}px, ${dy}px)`. Both reads
are viewport-space (real px), but the card renders inside the zoomed
`:root.ui-scale-125` subtree, so the transform must be divided by the
effective zoom (`toLocalPx`) or the reflow animation overshoots — the same
re-multiplication bug class already fixed for position:top/left (#76/#77/
#121/#127/#130) and the swipe-dismiss transform (this PR's own
documentLibrary.js/memory.js fix), just on a third FLIP-animation site.
notes.js already imports `zoomOf`/`toLocalPx` (since #130), so this was a
two-site division fix with no new import needed.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = (_REPO / "static" / "js" / "notes.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    """Return the source text of a top-level `function <name>(...) { ... }`
    declaration, from its `function` keyword up to (not including) the next
    top-level `function` declaration. Same helper as
    test_notes_checklist_grab_zoom_js.py."""
    start_match = re.search(rf"^function {re.escape(name)}\(", src, re.MULTILINE)
    assert start_match, f"function {name} not found in notes.js"
    start = start_match.start()
    next_match = re.search(r"^function \w+\(", src[start_match.end():], re.MULTILINE)
    end = start_match.end() + next_match.start() if next_match else len(src)
    return src[start:end]


def test_animate_reflow_divides_flip_delta_by_zoom():
    body = _extract_function(_SRC, "_animateReflow")
    assert "toLocalPx(dx" in body, (
        "_animateReflow must divide the FLIP dx delta by the effective zoom "
        "(toLocalPx) before assigning it to the invert transform, or the "
        "reflow animation overshoots under ui-scale-125."
    )
    assert "toLocalPx(dy" in body


def test_drag_reorder_swap_divides_flip_delta_by_zoom():
    # `_maybeSwap` is a nested function inside `_bindCardEvents` (not a
    # top-level declaration), so anchor on its distinctive FLIP-comment
    # instead of extracting a function body.
    idx = _SRC.index("// FLIP across ALL siblings.")
    window = _SRC[idx:idx + 1500]
    assert "toLocalPx(dx" in window, (
        "The drag-reorder swap's FLIP animation must divide dx/dy by the "
        "effective zoom before assigning the transform, same as "
        "_animateReflow."
    )
    assert "toLocalPx(dy" in window


def test_durations_and_easing_untouched():
    # Guard against scope creep: only the transform assignment should
    # change, not the animation timing.
    assert "transform 0.25s cubic-bezier(0.34, 1.2, 0.64, 1)" in _SRC
    assert "transform 0.22s cubic-bezier(0.34, 1.2, 0.64, 1)" in _SRC
