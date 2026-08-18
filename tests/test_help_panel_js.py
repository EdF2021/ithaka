"""Coverage for the Help panel (static/js/helpPanel.js).

Driven through `node --input-type=module` against a minimal DOM mock, same
approach as test_tile_manager_snap_zones_js.py / test_composer_arrow_up_recall_js.py.
The module self-initializes on import when document.readyState is not
'loading' (mirrors tourAutoplay.js), so the mock sets readyState:'complete'
and init() runs as part of the import.

Locks in:
- open()/close() toggle the .hidden class on #help-panel.
- Each tour's click handler fills #message with the right `/tour[-x]`
  command and submits #chat-form (same setup-trigger-link mechanism), unless
  a stream is currently active (window.chatModule.hasActiveStream()), in
  which case only the fill happens — dispatching submit would hit chat.js's
  stop-while-streaming branch and abort the user's in-flight stream.
- Each command chip's click handler closes the panel and fills #message,
  but does NOT submit.
- TOURS stays in sync with the `tour-*` commands registered in
  slashCommands.js (drift-guard, regex-based — doesn't import the heavy
  module).

The mobile/desktop split for the Tours section (hide tours, show a note
below 768px) moved to pure CSS in style.css — see test_help_panel_js.py's
sibling coverage requirement note below; there's no JS-side behavior left to
test here for that responsive switch (by construction it's always correct
on resize, unlike the old JS recompute-on-open approach).
"""
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "helpPanel.js"
_SLASH_COMMANDS = _REPO / "static" / "js" / "slashCommands.js"
_HAS_NODE = shutil.which("node") is not None


def _run_case(active_stream=False, model_items=None, is_admin=True):
    model_items_json = json.dumps(model_items if model_items is not None else [])
    active_stream_json = json.dumps(active_stream)
    is_admin_json = json.dumps(is_admin)
    script = textwrap.dedent(
        f"""
        const registryIds = [
          'help-panel', 'help-tours-section', 'help-tours-mobile-note',
          'help-tours-list', 'help-commands-list', 'help-model-section',
          'help-panel-close-btn', 'help-connect-btn', 'user-bar-help',
          'welcome-help-btn', 'welcome-connect-btn', 'welcome-tour-btn',
          'message', 'chat-form',
        ];

        function makeEl(id) {{
          const el = {{
            id: id || '',
            style: {{}},
            dataset: {{}},
            children: [],
            _listeners: {{}},
            className: '',
            textContent: '',
            title: '',
            value: '',
            classList: {{
              _set: new Set(),
              add(...c) {{ c.forEach((x) => this._set.add(x)); }},
              remove(...c) {{ c.forEach((x) => this._set.delete(x)); }},
              contains(c) {{ return this._set.has(c); }},
            }},
            addEventListener(type, fn) {{
              (this._listeners[type] = this._listeners[type] || []).push(fn);
            }},
            dispatchEvent(evt) {{
              (this._listeners[evt.type] || []).forEach((fn) => fn(evt));
              return true;
            }},
            appendChild(child) {{ this.children.push(child); return child; }},
            focus() {{}},
            set innerHTML(v) {{ this.children = []; }},
          }};
          return el;
        }}

        const registry = {{}};
        registryIds.forEach((id) => {{ registry[id] = makeEl(id); }});
        registry['help-panel'].classList.add('hidden');

        globalThis.window = {{
          innerWidth: 1200,
          _isAdmin: {is_admin_json},
          addEventListener() {{}},
          sessionModule: {{ getCurrentSessionId: () => 'sid-1' }},
          // Only reports active for the *current* session id, so the test
          // fails if _fillAndSubmit ever stops passing that id through.
          chatModule: {{ hasActiveStream: (sid) => {active_stream_json} && sid === 'sid-1' }},
          modelsModule: {{ getCachedItems: () => ({model_items_json}) }},
        }};
        globalThis.document = {{
          readyState: 'complete',
          getElementById(id) {{ return registry[id] || null; }},
          createElement() {{ return makeEl(); }},
          addEventListener() {{}},
        }};

        const mod = await import('{_HELPER.as_posix()}');

        let submitCount = 0;
        registry['chat-form'].addEventListener('submit', () => {{ submitCount++; }});

        const hasOpenClose = typeof mod.open === 'function' && typeof mod.close === 'function';

        const hiddenInitially = registry['help-panel'].classList.contains('hidden');
        mod.open();
        const hiddenAfterOpen = registry['help-panel'].classList.contains('hidden');
        const modelSectionDisplay = registry['help-model-section'].style.display;
        mod.close();
        const hiddenAfterClose = registry['help-panel'].classList.contains('hidden');

        const tourCommands = mod.TOURS.map((t) => mod._tourCommandFor(t.key));

        mod.open();
        const tourResults = mod.TOURS.map((t, i) => {{
          submitCount = 0;
          registry['message'].value = '';
          registry['help-tours-list'].children[i].dispatchEvent({{ type: 'click' }});
          return {{ key: t.key, value: registry['message'].value, submitted: submitCount > 0 }};
        }});

        // Each chip click calls close() (that's what we're asserting), so
        // reopen before every click.
        const chipResults = mod.COMMANDS.map((c, i) => {{
          mod.open();
          submitCount = 0;
          registry['message'].value = '';
          const row = registry['help-commands-list'].children[i];
          const chip = row.children[0];
          chip.dispatchEvent({{ type: 'click' }});
          return {{
            cmd: c.cmd,
            value: registry['message'].value,
            submitted: submitCount > 0,
            panelHiddenAfterClick: registry['help-panel'].classList.contains('hidden'),
          }};
        }});

        console.log(JSON.stringify({{
          hasOpenClose, hiddenInitially, hiddenAfterOpen, hiddenAfterClose,
          modelSectionDisplay,
          tourCommands, tourResults, chipResults,
          tourItemCount: registry['help-tours-list'].children.length,
          chipItemCount: registry['help-commands-list'].children.length,
        }}));
        """
    )
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_help_panel_exposes_open_and_close():
    result = _run_case()
    assert result["hasOpenClose"] is True


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_open_unhides_and_close_hides_the_panel():
    result = _run_case()
    assert result["hiddenInitially"] is True
    assert result["hiddenAfterOpen"] is False
    assert result["hiddenAfterClose"] is True


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_each_tour_click_fills_and_submits_its_command():
    result = _run_case()
    assert result["tourItemCount"] == 11  # 10 named tours + General tour
    assert result["tourCommands"] == [
        "/tour",
        "/tour-library",
        "/tour-cookbook",
        "/tour-research",
        "/tour-compare",
        "/tour-theme",
        "/tour-settings",
        "/tour-gallery",
        "/tour-brain",
        "/tour-task-1",
        "/tour-task-2",
    ]
    for expected_cmd, actual in zip(result["tourCommands"], result["tourResults"]):
        assert actual["value"] == expected_cmd
        assert actual["submitted"] is True


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_tour_click_fills_without_submitting_during_an_active_stream():
    # window.chatModule.hasActiveStream() => true: fill only, don't abort
    # the user's in-flight stream by dispatching a submit.
    result = _run_case(active_stream=True)
    for actual in result["tourResults"]:
        assert actual["value"]  # filled
        assert actual["submitted"] is False


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_command_chips_fill_without_submitting_and_close_the_panel():
    result = _run_case()
    assert result["chipItemCount"] == 5
    for chip in result["chipResults"]:
        assert chip["value"] == chip["cmd"]
        assert chip["submitted"] is False
        assert chip["panelHiddenAfterClick"] is True


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_tours_registry_matches_slash_commands_tour_registrations():
    """Drift guard: every `tour-*` command registered in slashCommands.js
    must have a matching entry in helpPanel.js's TOURS (and vice versa),
    minus the general `/tour` alias (registered there as `demo`/alias
    `tour`, not a `tour-*` key). Regex-based so it doesn't need to import
    slashCommands.js's heavy module graph."""
    result = _run_case()
    tours_cmds = {c.lstrip("/") for c in result["tourCommands"] if c != "/tour"}

    src = _SLASH_COMMANDS.read_text()
    registry_keys = set(re.findall(r"^\s*'(tour-[a-z0-9-]+)':\s*\{", src, re.MULTILINE))

    assert registry_keys, "regex found no tour-* commands — has slashCommands.js's format changed?"
    assert tours_cmds == registry_keys


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_model_section_shown_only_for_admin_without_a_usable_model():
    result = _run_case(is_admin=True, model_items=[])
    assert result["modelSectionDisplay"] == ""


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_model_section_hidden_for_non_admin_even_without_a_usable_model():
    result = _run_case(is_admin=False, model_items=[])
    assert result["modelSectionDisplay"] == "none"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_model_section_hidden_for_admin_once_a_model_is_usable():
    result = _run_case(is_admin=True, model_items=[{"offline": False, "models": ["gpt"]}])
    assert result["modelSectionDisplay"] == "none"
