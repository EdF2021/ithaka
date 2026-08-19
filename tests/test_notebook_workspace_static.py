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


# ── Task 7: mobile (<=700px) tabs ────────────────────────────────────────────

def test_mobile_tabbar_exists():
    assert 'data-nbws-tab="chat"' in _HTML or 'data-nbws-tab="chat"' in _WS
    assert "max-width: 700px" in _between(_CSS, "#nbws-root", "/* nbws-end */")


# ── Task B: studio panel redesign — Generate/Files sections ────────────────

def test_studio_panel_has_generate_and_files_section_headers():
    assert 'nbws-studio-section-head">Generate<' in _WS
    assert 'nbws-studio-section-head">Files<' in _WS


def test_open_artifact_report_calls_window_open_with_report_endpoint():
    # Scoped to _openArtifactReport's own body (not a bare "somewhere in the
    # file" substring check) so this fails if the report-tab call is moved,
    # dropped, or its target/tab-behavior changed.
    fn = _between(_WS, "function _openArtifactReport", "\n}\n")
    assert "window.open(" in fn
    assert "/report`" in fn
    assert "'_blank'" in fn


def test_artifact_click_podcast_toggles_mindmap_previews_others_open_report():
    """Regression for the two behaviors the design brief calls out most
    specifically: podcast rows must never open the report tab (Task A 404s
    that route for podcast) and mindmap rows must keep their existing
    _openArtifact preview. A bare '"/report" in _WS' substring check would
    still pass even if the podcast/mindmap guards were dropped or reordered
    after the report fallthrough — so this asserts the exact guard clauses
    AND their relative order inside the row click handler. """
    handler = _between(
        _WS,
        "row.addEventListener('click', (e) => {",
        "\n  });\n  box.querySelectorAll('.notebook-artifact-del')",
    )
    assert "if (kind === 'podcast') { _togglePodcastPanel(row); return; }" in handler
    assert "if (kind === 'mindmap') { _openArtifact(row); return; }" in handler
    assert "_openArtifactReport(row);" in handler
    podcast_idx = handler.index("kind === 'podcast'")
    mindmap_idx = handler.index("kind === 'mindmap'")
    report_idx = handler.index("_openArtifactReport(row);")
    # Both guards must return before the fallthrough is ever reached —
    # if either 'return' were dropped, or a guard moved after the
    # fallthrough call, this ordering assertion catches it even though every
    # substring above would still individually be present in the file.
    assert podcast_idx < mindmap_idx < report_idx


def test_studio_panel_labels_are_english_not_dutch():
    assert "Study guide" in _WS
    assert "Studiegids" not in _WS


def test_artifact_error_slot_lives_in_files_section_not_generate():
    # Review fix-round 1: of _showArtifactError's 6 call sites, 5 are
    # Files-section concerns (load/delete/podcast/open-artifact failures) —
    # only _generateArtifact's own failure is a Generate-section concern.
    # The error div must render inside .nbws-studio-files, not
    # .nbws-studio-generate, so it doesn't visually misattribute those
    # failures to the generate buttons.
    files_section = _between(_WS, 'nbws-studio-section-head">Files<', "</div>\n    </div>`;")
    assert 'id="nbws-artifact-error"' in files_section
    generate_section = _between(
        _WS, 'nbws-studio-section-head">Generate<', 'nbws-studio-section-head">Files<'
    )
    assert 'id="nbws-artifact-error"' not in generate_section
