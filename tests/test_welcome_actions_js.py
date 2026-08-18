"""Coverage for the welcome-screen first-run action row visibility logic.

Extracted into static/js/welcomeActions.js (imported and called from
models.js) rather than tested through models.js directly: models.js pulls in
ui.js/sessions.js/dragSort.js/chatRenderer.js, which reference browser-only
globals (e.g. HTMLInputElement) at module scope and fail to import under
plain `node --input-type=module`. The extracted helper has no such imports.

Locks in: first-run + admin shows the actions row with Connect/Tour buttons
visible; first-run + non-admin shows the row but hides Connect/Tour (only
Help remains, which lives outside this row's admin-gated buttons); configured
(has endpoints) hides the row entirely regardless of admin.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "welcomeActions.js"
_HAS_NODE = shutil.which("node") is not None


def _run_case():
    script = textwrap.dedent(
        f"""
        function makeEl(id) {{
          return {{
            id: id || '',
            style: {{}},
          }};
        }}

        const registry = {{
          'welcome-actions': makeEl('welcome-actions'),
          'welcome-connect-btn': makeEl('welcome-connect-btn'),
          'welcome-tour-btn': makeEl('welcome-tour-btn'),
        }};

        globalThis.document = {{
          getElementById(id) {{ return registry[id] || null; }},
        }};

        const mod = await import('{_HELPER.as_posix()}');

        function snapshot() {{
          return {{
            actionsDisplay: registry['welcome-actions'].style.display,
            connectDisplay: registry['welcome-connect-btn'].style.display,
            tourDisplay: registry['welcome-tour-btn'].style.display,
          }};
        }}

        mod._setWelcomeFirstRun(false, true);
        const firstRunAdmin = snapshot();

        mod._setWelcomeFirstRun(false, false);
        const firstRunNonAdmin = snapshot();

        mod._setWelcomeFirstRun(true, true);
        const configuredAdmin = snapshot();

        mod._setWelcomeFirstRun(true, false);
        const configuredNonAdmin = snapshot();

        console.log(JSON.stringify({{
          firstRunAdmin, firstRunNonAdmin, configuredAdmin, configuredNonAdmin,
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
def test_first_run_admin_shows_connect_and_tour():
    result = _run_case()
    assert result["firstRunAdmin"] == {
        "actionsDisplay": "flex",
        "connectDisplay": "",
        "tourDisplay": "",
    }


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_first_run_non_admin_hides_connect_and_tour():
    result = _run_case()
    assert result["firstRunNonAdmin"] == {
        "actionsDisplay": "flex",
        "connectDisplay": "none",
        "tourDisplay": "none",
    }


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_configured_hides_actions_row_regardless_of_admin():
    result = _run_case()
    assert result["configuredAdmin"]["actionsDisplay"] == "none"
    assert result["configuredNonAdmin"]["actionsDisplay"] == "none"
