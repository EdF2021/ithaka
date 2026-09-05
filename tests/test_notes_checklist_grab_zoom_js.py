"""Pin the ui-scale-125 width fix in notes.js's `_beginChecklistGrab`.

Source-text assertions only, following the precedent set by
test_notebook_workspace_static.py: this repo has no build step and no JS DOM
test runner, so the drag-ghost DOM wiring in static/js/notes.js can't
practically be driven at runtime here.

Background (issue #80/#121/#122): the earlier synthetic verification of the
mobile-only drag-ghost fix (`_isNotesMobileMode()` = touch + <=768px) only
exercised `_beginGrab` (note-card grab). `_beginChecklistGrab` (checklist-row
grab) is the same pattern — `row.getBoundingClientRect()` is viewport-space,
and `row` becomes `position:fixed` while still living inside the zoomed
`:root`, so `rect.width` must be divided by the effective zoom (toLocalPx)
before being assigned as `row.style.width`, same as `rect.left/top` and same
as `_beginGrab`'s `card.style.width/height` (fix-round-1, finding 3). This
test locks that in for the checklist path specifically, since it wasn't
covered by any existing test.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_NOTES = (_REPO / "static" / "js" / "notes.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    """Return the source text of a top-level `function <name>(...) { ... }`
    declaration, from its `function` keyword up to (not including) the next
    top-level `function` declaration. Good enough for a flat file with no
    nested top-level function declarations sharing this name prefix."""
    start_match = re.search(rf"^function {re.escape(name)}\(", src, re.MULTILINE)
    assert start_match, f"function {name} not found in notes.js"
    start = start_match.start()
    next_match = re.search(r"^function \w+\(", src[start_match.end():], re.MULTILINE)
    end = start_match.end() + next_match.start() if next_match else len(src)
    return src[start:end]


def test_begin_checklist_grab_divides_width_by_zoom():
    body = _extract_function(_NOTES, "_beginChecklistGrab")
    assert "toLocalPx(rect.width" in body, (
        "_beginChecklistGrab must divide rect.width by the effective zoom "
        "(toLocalPx) before assigning row.style.width — otherwise the "
        "checklist drag-ghost renders ~1.25x too wide under ui-scale-125, "
        "the same bug class as #76-#80."
    )


def test_begin_checklist_grab_divides_left_and_top_by_zoom():
    body = _extract_function(_NOTES, "_beginChecklistGrab")
    assert "toLocalPx(rect.left" in body
    assert "toLocalPx(rect.top" in body


def test_begin_grab_still_divides_width_and_height_by_zoom():
    # Companion check for the sibling note-card grab path (_beginGrab),
    # already covered by the issue #122 synthetic verification — pinned here
    # too so both drag-ghost paths are locked in by the same test file.
    body = _extract_function(_NOTES, "_beginGrab")
    assert "toLocalPx(rect.width" in body
    assert "toLocalPx(rect.height" in body
