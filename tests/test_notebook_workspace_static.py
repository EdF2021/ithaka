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
    # The workspace must never look up or reparent #chat-container itself
    # (per the module-header note): CSS drives the layout, this module only
    # ever appends/removes chrome of its own (badge, in-panel artifact
    # viewer) — a bare "appendChild" substring check is no longer a valid
    # proxy for that now that the in-panel artifact viewer legitimately
    # appends its own iframe (86d8084), so assert the real invariant.
    assert "getElementById('chat-container')" not in _WS
    assert 'getElementById("chat-container")' not in _WS
    assert "querySelector('#chat-container')" not in _WS
    assert 'querySelector("#chat-container")' not in _WS


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


def test_podcast_poll_stops_when_switching_to_a_different_notebook():
    """The notebooks picker switches workspaces by calling open() again, never
    close(), so the close hook doesn't fire on that path. Without an explicit
    stop in _openImpl the poll keeps painting notebook A's pending row — and
    keeps A's generate button disabled — inside notebook B's studio panel,
    which is a wired-once singleton shared across notebooks."""
    open_impl = _between(_WS, "async function _openImpl", "\n}\n")
    assert "_stopPodcastPoll()" in open_impl
    # Guarded on an actual id change: re-opening the SAME notebook must keep
    # the poll alive so in-flight progress survives.
    assert "_state.notebook.id !== nb.id" in open_impl


def test_notebook_scoped_handlers_guard_ui_writes_on_open_epoch():
    """Every handler that writes into the shared, persistent panel chrome after
    an await must check _openEpoch first — otherwise a delete/upload/generate
    that resolves after a notebook switch reports the old notebook's result
    into the new notebook's UI (same bug class as the sessions.js issue #22
    fix: state captured at moment A, used at moment B)."""
    for fn in ("_deleteSource", "_uploadSources", "_deleteArtifact", "_generateArtifact"):
        body = _between(_WS, f"async function {fn}", "\n}\n")
        assert "const epoch = _openEpoch;" in body, f"{fn} does not capture _openEpoch"
        # Require the re-check form itself, not just the identifier: a bare
        # `_openEpoch` anywhere after the first await would also match the
        # capture line or a comment, so the assertion would pass with no guard
        # on the write path at all.
        assert ("epoch !== _openEpoch" in body or "epoch === _openEpoch" in body), (
            f"{fn} captures _openEpoch but never compares it back"
        )


def test_shared_idle_chrome_resets_unconditionally():
    """The counterpart to the guard above: controls that must return to an idle
    state live in the wired-once skeleton and are never re-rendered on open, so
    guarding THEIR reset would strand 'Uploading…' / a disabled 'Generating…'
    button in whichever notebook is on screen next."""
    upload = _between(_WS, "async function _uploadSources", "\n}\n")
    assert "if (zone) zone.textContent = _ZONE_IDLE;" in upload.split("finally", 1)[1]
    gen = _between(_WS, "async function _generateArtifact", "\n}\n")
    finally_block = gen.split("finally", 1)[1]
    assert "if (btn) btn.disabled = false;" in finally_block
    assert "label.textContent = original;" in finally_block


# ── Task 7: mobile (<=700px) tabs ────────────────────────────────────────────

def test_mobile_tabbar_exists():
    assert 'data-nbws-tab="chat"' in _HTML or 'data-nbws-tab="chat"' in _WS
    assert "max-width: 700px" in _between(_CSS, "#nbws-root", "/* nbws-end */")


# ── Task B: studio panel redesign — Generate/Files sections ────────────────

def test_studio_panel_has_generate_and_files_section_headers():
    assert 'nbws-studio-section-head">Generate<' in _WS
    assert 'nbws-studio-section-head">Files<' in _WS


def test_open_artifact_report_opens_in_panel_iframe_viewer():
    # 86d8084 replaced the new-tab window.open with an in-panel iframe
    # viewer (relative URL, same origin) so the report opens inside the
    # studio panel instead of a separate browser tab. Scoped to
    # _openArtifactReport's own body so this fails if the call is moved,
    # dropped, or reverted back to window.open.
    fn = _between(_WS, "function _openArtifactReport", "\n}\n")
    assert "window.open(" not in fn
    assert "/report`" in fn
    assert "_showArtifactViewer(url, row.dataset.kind);" in fn


def test_show_and_close_artifact_viewer_manage_in_panel_iframe():
    show = _between(_WS, "function _showArtifactViewer", "\n}\n")
    assert "id = 'nbws-artifact-viewer'" in show
    assert "<iframe" in show
    assert "body.appendChild(viewer);" in show
    assert "nbws-artifact-viewer-active" in show

    close = _between(_WS, "function _closeArtifactViewer", "\n}\n")
    assert "if (viewer) viewer.remove();" in close
    assert "nbws-artifact-viewer-active" in close

    # The viewer is torn down on workspace close too, so no stale iframe
    # survives across a close/reopen.
    assert "registerCloseHook(_closeArtifactViewer);" in _WS


def test_studio_widens_to_60vw_while_artifact_viewer_is_active():
    assert "width: 60vw;" in _CSS
    assert "nbws-artifact-viewer-active" in _CSS


def test_artifact_click_podcast_toggles_others_open_report():
    """Regression for the row-click behavior: podcast rows must never open
    the report tab (Task A 404s that route for podcast). Since the
    interactive mindmap viewer, mindmap rows open the report like every
    other text kind — the old `_openArtifact` preview guard must stay gone
    (the raw mermaid document remains reachable via the secondary
    open-source-document button). The ordering assertion catches a dropped
    'return' or a guard moved after the fallthrough."""
    handler = _between(
        _WS,
        "row.addEventListener('click', (e) => {",
        "\n  });\n  box.querySelectorAll('.notebook-artifact-del')",
    )
    assert "if (kind === 'podcast') { _togglePodcastPanel(row); return; }" in handler
    assert "if (kind === 'mindmap') { _openArtifact(row); return; }" not in handler
    assert "_openArtifactReport(row);" in handler
    podcast_idx = handler.index("kind === 'podcast'")
    report_idx = handler.index("_openArtifactReport(row);")
    assert podcast_idx < report_idx


def test_studio_panel_labels_are_english_not_dutch():
    assert "Study guide" in _WS
    assert "Studiegids" not in _WS


# ── Task: NotebookArtifact title-kolom + hernoemen ──────────────────────────

def test_artifact_row_has_rename_button_in_same_group_as_opendoc_button():
    # Scoped to _artifactRow's own body: the rename button must live inside
    # the row markup, next to the existing "open source document" button
    # (same $-openSrcBtn/${renameBtn} pair), not bolted on somewhere else.
    fn = _between(_WS, "function _artifactRow", "\n}\n")
    assert "notebook-artifact-rename" in fn
    assert "${openSrcBtn}" in fn
    assert "${renameBtn}" in fn
    assert fn.index("${openSrcBtn}") < fn.index("${renameBtn}")


def test_row_click_guards_rename_input_before_kind_dispatch():
    # Same precedent as test_artifact_click_podcast_toggles_mindmap_previews_
    # others_open_report above: assert the exact guard clause AND that it
    # runs before the podcast/mindmap/report dispatch, not just that the
    # string exists somewhere in the file.
    handler = _between(
        _WS,
        "row.addEventListener('click', (e) => {",
        "\n  });\n  box.querySelectorAll('.notebook-artifact-del')",
    )
    assert "if (e.target.closest('.notebook-artifact-rename-input')) return;" in handler
    guard_idx = handler.index("notebook-artifact-rename-input")
    podcast_idx = handler.index("kind === 'podcast'")
    assert guard_idx < podcast_idx


def test_rename_button_click_handler_stops_propagation_and_starts_rename():
    fn = _between(
        _WS,
        "box.querySelectorAll('.notebook-artifact-rename').forEach(btn => {",
        "\n  });\n  // \"Open transcript\"",
    )
    assert "e.stopPropagation();" in fn
    assert "_startArtifactRename(row);" in fn


def test_start_artifact_rename_enter_saves_via_patch_escape_and_blur_cancel():
    fn = _between(_WS, "function _startArtifactRename", "\n}\n")
    assert "if (!_state.notebook) return;" in fn
    assert "method: 'PATCH'" in fn
    assert "JSON.stringify({ title: newTitle })" in fn
    assert "e.key === 'Enter'" in fn
    assert "commit();" in fn
    assert "e.key === 'Escape'" in fn
    assert "restore();" in fn
    assert "input.addEventListener('blur', restore);" in fn
    # The input itself must stop its own clicks from bubbling to the row's
    # click handler (which would otherwise open the report/preview).
    assert "input.addEventListener('click', (e) => e.stopPropagation());" in fn
    enter_idx = fn.index("e.key === 'Enter'")
    escape_idx = fn.index("e.key === 'Escape'")
    assert enter_idx < escape_idx


def test_rename_input_keeps_grow_so_row_does_not_wrap():
    # The title span it replaces carries `.grow` (flex:1) — dropping it on
    # the input would push the date + action buttons onto a second line in
    # this wrapping flex row (.notebook-artifact-item is flex-wrap:wrap).
    fn = _between(_WS, "function _startArtifactRename", "\n}\n")
    assert "input.className = 'grow session-rename-input notebook-artifact-rename-input';" in fn


# ── Task: infographic artifact ───────────────────────────────────────────

def test_infographic_generate_button_exists_with_english_label():
    kinds = _between(_WS, "const ARTIFACT_KINDS = [", "];")
    assert "'infographic'" in kinds
    labels = _between(_WS, "const KIND_LABELS = {", "\n};")
    assert "infographic: 'Infographic'," in labels


# ── Task: fase-4a studio tiles + flashcards/data_table ───────────────────

def test_web_source_search_zone_registered_client_side():
    skeleton = _between(_WS, "function _sourcesPanelSkeleton", "\n}\n")
    assert 'id="nbws-web-search-input"' in skeleton
    assert 'id="nbws-web-search-btn"' in skeleton
    assert 'id="nbws-web-search-results"' in skeleton
    # Search wiring happens once, alongside the upload zone.
    wire = _between(_WS, "function _wireSourcesPanel", "\n}\n")
    assert "_setupWebSearch();" in wire
    # Add-flow posts to the URL-ingest endpoint and refreshes the list.
    add_fn = _between(_WS, "async function _addWebSource", "\n}\n")
    assert "/sources/url" in add_fn
    assert "_loadSources()" in add_fn


def test_video_tile_and_job_flow_registered_client_side():
    labels = _between(_WS, "const KIND_LABELS = {", "\n};")
    assert "video: 'Video'," in labels
    skeleton = _between(_WS, "function _studioPanelSkeleton", "\n}\n")
    assert 'class="nbws-tile notebook-video-gen-btn nbws-tile--video"' in skeleton
    # Video rows toggle their player panel, never the report path; the poll
    # must have its own close-hook so no stale loop outlives the workspace.
    handler = _between(_WS, "row.addEventListener('click'", "\n    });")
    assert "_toggleVideoPanel(row)" in handler
    assert "registerCloseHook(_stopVideoPoll);" in _WS
    assert "notebook-video-gen-btn" in _WS
    icons = _between(_WS, "const _KIND_ICONS = {", "\n};")
    assert "video: '<svg" in icons


def test_report_tile_and_modal_registered_client_side():
    # KIND_LABELS.report is a deliberate, already-reviewed distinction: the
    # tile shows the Dutch literal "Rapporten", but KIND_LABELS.report
    # itself must stay English 'Report' since it also feeds the
    # Files-list/viewer kind pills (see the comment above KIND_LABELS).
    labels = _between(_WS, "const KIND_LABELS = {", "\n};")
    assert "report: 'Report'," in labels
    assert "report: 'Rapporten'," not in labels
    icons = _between(_WS, "const _KIND_ICONS = {", "\n};")
    assert "report: '<svg" in icons
    skeleton = _between(_WS, "function _studioPanelSkeleton", "\n}\n")
    assert 'class="nbws-tile notebook-report-open-btn nbws-tile--report"' in skeleton
    # The tile's own label is a hardcoded Dutch literal, mirroring the
    # podcast tile's literal "Audio" label — not ${_esc(KIND_LABELS.report)}.
    assert '<span class="nbws-tile-label">Rapporten</span>' in skeleton
    assert '${_esc(KIND_LABELS.report)}' not in skeleton
    # Defect Task 6's own review round already caught once: the report
    # modal's Escape-key listener and DOM node must be torn down on
    # workspace close.
    assert "registerCloseHook(_closeReportModal);" in _WS
    # Row-click dispatch must NOT special-case 'report' the way
    # 'podcast'/'video' are special-cased — it falls through to the same
    # _openArtifactReport(row) path every other plain-text kind uses.
    handler = _between(
        _WS,
        "row.addEventListener('click', (e) => {",
        "\n  });\n  box.querySelectorAll('.notebook-artifact-del')",
    )
    assert "kind === 'report'" not in handler


def test_slide_deck_kind_registered_client_side_as_first_tile():
    kinds = _between(_WS, "const ARTIFACT_KINDS = [", "];")
    assert kinds.split("[", 1)[-1].strip().startswith("'slide_deck'")
    labels = _between(_WS, "const KIND_LABELS = {", "\n};")
    assert "slide_deck: 'Slides'," in labels


def test_flashcards_and_data_table_kinds_registered_client_side():
    kinds = _between(_WS, "const ARTIFACT_KINDS = [", "];")
    assert "'flashcards'" in kinds
    assert "'data_table'" in kinds
    labels = _between(_WS, "const KIND_LABELS = {", "\n};")
    assert "flashcards: 'Flashcards'," in labels
    assert "data_table: 'Data table'," in labels


def test_studio_tiles_have_per_kind_icon_and_accent_class():
    # Every generate tile renders as .nbws-tile with a per-kind modifier
    # class (accent color) and an icon span; the podcast tile is part of
    # the same grid, listed first, labeled "Audio".
    skeleton = _between(_WS, "function _studioPanelSkeleton", "\n}\n")
    assert 'class="nbws-tile notebook-podcast-gen-btn nbws-tile--podcast"' in skeleton
    assert '<span class="nbws-tile-label">Audio</span>' in skeleton
    assert 'nbws-tile notebook-artifact-gen-btn nbws-tile--${_esc(kind)}' in skeleton
    assert '_KIND_ICONS[kind]' in skeleton
    icons = _between(_WS, "const _KIND_ICONS = {", "\n};")
    for kind in ("podcast", "slide_deck", "mindmap", "briefing", "flashcards", "quiz",
                 "infographic", "data_table", "study_guide", "faq"):
        assert f"{kind}: '<svg" in icons, kind


def test_flashcards_and_data_table_are_plain_report_kinds():
    # Row-click dispatch must not special-case the new kinds: they open via
    # the same _openArtifactReport(row) path as study_guide/briefing/faq.
    handler = _between(_WS, "row.addEventListener('click'", "\n    });")
    assert "flashcards" not in handler
    assert "data_table" not in handler


def test_infographic_is_a_plain_text_artifact_not_a_special_row_kind():
    # Row-click dispatch only special-cases podcast (toggle player) and
    # mindmap (preview); infographic must fall through to the same
    # _openArtifactReport(row) path as study_guide/briefing/faq/quiz — no
    # extra branch should exist for it.
    handler = _between(
        _WS,
        "row.addEventListener('click', (e) => {",
        "\n  });\n  box.querySelectorAll('.notebook-artifact-del')",
    )
    assert "kind === 'infographic'" not in handler


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


def test_podcast_phase_text_surfaces_a_script_retry():
    """A retried script phase must read as progress, not a hang: the phase-text
    helper distinguishes script_attempt > 1 from the first attempt."""
    body = _between(_WS, "function _podcastPhaseText", "\n}\n")
    assert "script_attempt > 1" in body
    assert "attempt" in body.lower()


def test_mindmap_node_click_submits_before_closing_workspace():
    """Regression for #112: closeNotebookWorkspace() must run AFTER the
    submit dispatch, not before. chat.js's handleChatSubmit source_ids gate
    reads isNotebookWorkspaceOpen() synchronously, before any await, while
    handling the submit event — if the workspace is closed first, that gate
    always reads "closed" and source_ids silently drops for this entry point
    regardless of the user's checked sources. This assertion fails on the
    old (broken) ordering. (Note: the separate issue-#22 fail-closed guard
    sits past an await elsewhere in handleChatSubmit and is NOT restored by
    this reorder — that remains a known gap for this entry point.)"""
    fn = _between(_WS, "function _handleMindmapNodeClick", "\n}\n")
    assert "form.dispatchEvent(new Event('submit'" in fn
    assert "closeNotebookWorkspace();" in fn
    submit_idx = fn.index("form.dispatchEvent(new Event('submit'")
    close_idx = fn.index("closeNotebookWorkspace();")
    assert submit_idx < close_idx
