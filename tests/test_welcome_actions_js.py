"""Coverage for the welcome-screen first-run action row visibility logic.

Extracted into static/js/welcomeActions.js (imported and called from
models.js, and from helpPanel.js for the Help panel's Model section) rather
than tested through models.js directly: models.js pulls in
ui.js/sessions.js/dragSort.js/chatRenderer.js, which reference browser-only
globals (e.g. HTMLInputElement) at module scope and fail to import under
plain `node --input-type=module`. The extracted helper has no such imports.

Semantics (post review-round #15 redesign, see docs/sessions for the PR):
- #welcome-actions row itself is ALWAYS display:flex — #welcome-screen (not
  this row) is what controls whether the first-run area shows at all.
- Connect a model: visible iff there's no usable model yet AND the user is
  an admin (only admins can add endpoints).
- Take the tour: visible iff there IS a usable model (the tour drives real
  chat turns and refuses to run without one) — deliberately NOT admin-gated,
  so a non-admin first run isn't a dead end with only Help.
- Help: always visible, but lives outside this row's admin gate entirely
  (managed by helpPanel.js, not this function).
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

        // (hasUsableModel, isAdmin) matrix.
        mod._setWelcomeFirstRun(false, true);
        const noModelAdmin = snapshot();

        mod._setWelcomeFirstRun(false, false);
        const noModelNonAdmin = snapshot();

        mod._setWelcomeFirstRun(true, true);
        const usableModelAdmin = snapshot();

        mod._setWelcomeFirstRun(true, false);
        const usableModelNonAdmin = snapshot();

        const usableModelResults = {{
          empty: mod.hasUsableModel([]),
          onlyOffline: mod.hasUsableModel([{{ offline: true, models: ['a'] }}]),
          onlineNoModels: mod.hasUsableModel([{{ offline: false, models: [], models_extra: [] }}]),
          onlineWithModels: mod.hasUsableModel([{{ offline: false, models: ['a'] }}]),
          onlineWithExtraOnly: mod.hasUsableModel([{{ offline: false, models: [], models_extra: ['b'] }}]),
          mixedOfflineThenOnline: mod.hasUsableModel([
            {{ offline: true, models: ['a'] }},
            {{ offline: false, models: ['b'] }},
          ]),
        }};

        console.log(JSON.stringify({{
          noModelAdmin, noModelNonAdmin, usableModelAdmin, usableModelNonAdmin,
          usableModelResults,
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
def test_no_usable_model_admin_shows_connect_only():
    result = _run_case()
    assert result["noModelAdmin"] == {
        "actionsDisplay": "flex",
        "connectDisplay": "",
        "tourDisplay": "none",
    }


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_no_usable_model_non_admin_shows_neither_connect_nor_tour():
    result = _run_case()
    assert result["noModelNonAdmin"] == {
        "actionsDisplay": "flex",
        "connectDisplay": "none",
        "tourDisplay": "none",
    }


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_usable_model_admin_shows_tour_not_connect():
    result = _run_case()
    assert result["usableModelAdmin"] == {
        "actionsDisplay": "flex",
        "connectDisplay": "none",
        "tourDisplay": "",
    }


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_usable_model_non_admin_still_shows_tour():
    result = _run_case()
    assert result["usableModelNonAdmin"] == {
        "actionsDisplay": "flex",
        "connectDisplay": "none",
        "tourDisplay": "",
    }


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_actions_row_is_always_flex():
    result = _run_case()
    for state in ("noModelAdmin", "noModelNonAdmin", "usableModelAdmin", "usableModelNonAdmin"):
        assert result[state]["actionsDisplay"] == "flex"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_has_usable_model_predicate():
    result = _run_case()["usableModelResults"]
    assert result["empty"] is False
    assert result["onlyOffline"] is False
    assert result["onlineNoModels"] is False
    assert result["onlineWithModels"] is True
    assert result["onlineWithExtraOnly"] is True
    assert result["mixedOfflineThenOnline"] is True
