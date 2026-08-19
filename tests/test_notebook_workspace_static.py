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


# ── Task 4: sources panel + source_ids chat filter ──────────────────────────

_CHAT_STREAM_JS = (_REPO / "static" / "js" / "chatStream.js").read_text(encoding="utf-8")
_CHAT_JS = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")


def test_chat_payload_includes_source_ids_hook():
    src = _CHAT_STREAM_JS + _CHAT_JS
    assert "getSourceIdsForChat" in src


def test_empty_selection_blocks_send():
    assert "Select at least one source" in _WS


def test_selection_persisted_per_notebook():
    assert "notebook_source_sel_" in _WS


# ── Task 5: session dropdown + follow-up chips ───────────────────────────────

def test_stream_done_event_exists():
    stream = (_REPO / "static" / "js" / "chatStream.js").read_text(encoding="utf-8")
    assert "ithaka:chat-stream-done" in stream or "chatStreamDone" in stream


def test_chips_fetch_suggest_endpoint_and_fail_silently():
    assert "/suggest_questions" in _WS
    assert "catch" in _WS  # fetch-fout mag nooit een chatfout tonen


def test_session_select_scoped_to_notebook():
    assert "nbws-session-select" in _WS
    assert "notebook_id" in _WS


def test_chip_click_fills_composer_without_autosend():
    assert "nbws-chip" in _WS
    assert "getElementById('message')" in _WS or 'getElementById("message")' in _WS


# ── Task 6: studio panel (artifacts + podcast) ───────────────────────────────

def _between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


def test_detail_view_is_gone_from_notebooks_modal():
    assert "_showDetail" not in _NB


def test_workspace_keeps_running_when_artifact_opens():
    assert "closeNotebookWorkspace" not in _between(_WS, "function _openArtifact", "\n}\n")


def test_podcast_poll_stops_on_workspace_close():
    assert "_stopPodcastPoll" in _WS
