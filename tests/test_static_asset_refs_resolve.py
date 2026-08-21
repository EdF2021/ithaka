"""Every literal /static/<file> path in the frontend must resolve to a real file.

Five dead `/static/favicon.ico` / `/static/favicon.png` references sat in the
desktop-Notification calls of tasks.js, notes.js, settings.js and
calendar/reminders.js: the file never existed (the repo ships `static/icon.ico`
and `static/icons/icon-192.png`), so every notification fetched a 404 and
rendered without an icon. Nothing failed loudly, so nothing caught it.

Scope is deliberately narrow — only quoted literals that look like an asset
path with a file extension. Dynamically built paths and template
interpolations are out of reach of a static check and are skipped rather than
guessed at.
"""
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_STATIC = _REPO / "static"

# '/static/foo/bar.png' or "/static/foo.ico" — a quoted literal, no ${} or +
# concatenation inside, ending in a short extension.
_ASSET_REF = re.compile(r"""['"](/static/[A-Za-z0-9_\-./]+\.[A-Za-z0-9]{2,5})['"]""")

# Served by a route rather than a file on disk, or intentionally absent.
_ROUTE_SERVED: set[str] = set()


def _sources():
    for path in sorted(_STATIC.rglob("*.js")):
        yield path
    index = _STATIC / "index.html"
    if index.exists():
        yield index


def _refs():
    for path in _sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _ASSET_REF.finditer(text):
            ref = match.group(1)
            if ref in _ROUTE_SERVED:
                continue
            line = text.count("\n", 0, match.start()) + 1
            yield path.relative_to(_REPO), line, ref


def test_static_asset_references_exist_on_disk():
    missing = [
        f"{rel}:{line} -> {ref}"
        for rel, line, ref in _refs()
        if not (_REPO / ref.lstrip("/")).is_file()
    ]
    assert not missing, "dead /static asset references:\n  " + "\n  ".join(missing)


def test_the_check_actually_finds_references():
    """Guard against the regex silently matching nothing — a test that scans an
    empty set would pass forever while the real check rots."""
    assert sum(1 for _ in _refs()) >= 10


@pytest.mark.parametrize("asset", [
    "static/icons/icon-192.png",   # the notification icon the dead refs now point at
    "static/icon.ico",
])
def test_referenced_icon_assets_are_present(asset):
    assert (_REPO / asset).is_file()
