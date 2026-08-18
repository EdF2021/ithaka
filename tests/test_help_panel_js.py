"""Coverage for the Help panel (static/js/helpPanel.js).

Driven through `node --input-type=module` against a minimal DOM mock, same
approach as test_tile_manager_snap_zones_js.py / test_composer_arrow_up_recall_js.py.
The module self-initializes on import when document.readyState is not
'loading' (mirrors tourAutoplay.js), so the mock sets readyState:'complete'
and init() runs as part of the import.

Locks in:
- open()/close() toggle the .hidden class on #help-panel.
- Each tour's click handler fills #message with the right `/tour[-x]`
  command and submits #chat-form (same setup-trigger-link mechanism).
- Each command chip's click handler fills #message but does NOT submit.
- On mobile (innerWidth <= 768) the Tours section is hidden and the
  desktop-only note is shown; on desktop it's the reverse.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "helpPanel.js"
_HAS_NODE = shutil.which("node") is not None


def _run_case():
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
          _isAdmin: true,
          addEventListener() {{}},
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

        const chipResults = mod.COMMANDS.map((c, i) => {{
          submitCount = 0;
          registry['message'].value = '';
          const row = registry['help-commands-list'].children[i];
          const chip = row.children[0];
          chip.dispatchEvent({{ type: 'click' }});
          return {{ cmd: c.cmd, value: registry['message'].value, submitted: submitCount > 0 }};
        }});

        window.innerWidth = 500;
        mod.open();
        const mobileState = {{
          toursDisplay: registry['help-tours-section'].style.display,
          noteDisplay: registry['help-tours-mobile-note'].style.display,
        }};

        window.innerWidth = 1200;
        mod.open();
        const desktopState = {{
          toursDisplay: registry['help-tours-section'].style.display,
          noteDisplay: registry['help-tours-mobile-note'].style.display,
        }};

        console.log(JSON.stringify({{
          hasOpenClose, hiddenInitially, hiddenAfterOpen, hiddenAfterClose,
          tourCommands, tourResults, chipResults, mobileState, desktopState,
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
    assert result["tourItemCount"] == 8  # 7 named tours + General tour
    assert result["tourCommands"] == [
        "/tour",
        "/tour-library",
        "/tour-cookbook",
        "/tour-research",
        "/tour-compare",
        "/tour-theme",
        "/tour-settings",
        "/tour-gallery",
    ]
    for expected_cmd, actual in zip(result["tourCommands"], result["tourResults"]):
        assert actual["value"] == expected_cmd
        assert actual["submitted"] is True


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_command_chips_fill_without_submitting():
    result = _run_case()
    assert result["chipItemCount"] == 5
    for chip in result["chipResults"]:
        assert chip["value"] == chip["cmd"]
        assert chip["submitted"] is False


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_mobile_width_hides_tours_section_and_shows_note():
    result = _run_case()
    assert result["mobileState"]["toursDisplay"] == "none"
    assert result["mobileState"]["noteDisplay"] != "none"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_desktop_width_shows_tours_section_and_hides_note():
    result = _run_case()
    assert result["desktopState"]["toursDisplay"] != "none"
    assert result["desktopState"]["noteDisplay"] == "none"
