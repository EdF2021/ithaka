"""Static regressions for the notebooks workspace skeleton (Task 3 of the
notebooks-workspace SDD plan): the NotebookLM-style 3-panel fullscreen shell
that opens around the existing chat.

Source-text assertions only, following the precedent set by
test_settings_admin_managed_tabs_static.py: this repo has no build step and
no JS DOM test runner, so the DOM/CSS wiring added to static/index.html,
static/style.css, static/js/notebookWorkspace.js and static/js/notebooks.js
can't practically be driven at runtime here.
"""

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_WS = (_REPO / "static" / "js" / "notebookWorkspace.js").read_text(encoding="utf-8")
_NB = (_REPO / "static" / "js" / "notebooks.js").read_text(encoding="utf-8")
_HTML = (_REPO / "static" / "index.html").read_text(encoding="utf-8")
_CSS = (_REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_workspace_module_exports_open_close():
    assert "export function openNotebookWorkspace" in _WS
    assert "export function closeNotebookWorkspace" in _WS


def test_body_class_drives_layout_and_chat_stays_untouched():
    assert "notebook-workspace-open" in _WS and "notebook-workspace-open" in _CSS
    # the workspace must not move or hide chat-container:
    assert "chat-container" not in _WS or "appendChild" not in _WS


def test_grid_click_opens_workspace_not_detail():
    assert "openNotebookWorkspace" in _NB


def test_index_html_has_workspace_root():
    assert 'id="nbws-root"' in _HTML
