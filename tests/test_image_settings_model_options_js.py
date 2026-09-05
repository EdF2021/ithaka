"""Pin `_buildImageModelOptions` in static/js/settings.js (issue #153).

The Image Generation card's `set-imgModelSelect` dropdown used to only ever
offer inpaint-capable models plus two hardcoded fallbacks. When the saved
`image_model` setting was an OpenAI model (e.g. `gpt-image-1.5`, the prod
default used by `do_generate_image` for chat image generation — see
src/ai_interaction.py), the select couldn't represent the current value and
silently reverted to "Auto-detect", making the setting look blank.

Fix: also treat OpenAI image models (gpt-image-*, dall-e-*) as selectable
options, and always ensure the currently-saved value is present as an option
even if it isn't in the model cache at all, so the dropdown never looks empty.

Driven through `node --input-type=module` against the real function
(extracted from source), same technique as test_local_endpoint_js.py, since
settings.js pulls in browser-only modules and can't be imported standalone.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "static" / "js" / "settings.js"
_HAS_NODE = shutil.which("node") is not None


def _build_options(all_model_ids, current_value):
    src = _SRC.read_text(encoding="utf-8")
    m = re.search(r"function _buildImageModelOptions\(.*?\n\}", src, re.DOTALL)
    assert m, "_buildImageModelOptions not found in settings.js"
    fn = m.group(0)
    js = (
        "import { sortModelIds } from './static/js/modelSort.js';\n"
        + fn
        + "\nconsole.log(JSON.stringify(_buildImageModelOptions("
        + json.dumps(all_model_ids) + ", " + json.dumps(current_value) + ")));"
    )
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=js, capture_output=True, text=True, cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_prod_default_gpt_image_1_5_selectable_when_in_cache():
    options = _build_options(
        ["gpt-image-1.5", "gpt-image-1", "dall-e-3", "stable-diffusion-inpainting"],
        "gpt-image-1.5",
    )
    values = [o["value"] for o in options]
    assert "gpt-image-1.5" in values
    assert "gpt-image-1" in values
    assert "dall-e-3" in values


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_current_value_always_present_even_when_not_in_cache():
    # Reproduces the exact #153 symptom: image_model=gpt-image-1.5 but the
    # model cache doesn't (yet) contain it — the select must not go blank.
    options = _build_options(["stable-diffusion-inpainting"], "gpt-image-1.5")
    values = [o["value"] for o in options]
    assert "gpt-image-1.5" in values


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_current_value_not_duplicated_when_already_detected():
    options = _build_options(["gpt-image-1.5"], "gpt-image-1.5")
    values = [o["value"] for o in options]
    assert values.count("gpt-image-1.5") == 1


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_inpaint_models_still_detected():
    options = _build_options(
        ["some-endpoint/stable-diffusion-3.5-medium", "unrelated-llm-7b"],
        "",
    )
    values = [o["value"] for o in options]
    assert "some-endpoint/stable-diffusion-3.5-medium" in values
    assert "unrelated-llm-7b" not in values


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_undetected_inpaint_fallbacks_marked_not_detected():
    options = _build_options([], "")
    by_value = {o["value"]: o["label"] for o in options}
    assert by_value["stable-diffusion-3.5-medium"] == "stable-diffusion-3.5-medium (not detected)"
    assert by_value["stable-diffusion-inpainting"] == "stable-diffusion-inpainting (not detected)"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_no_current_value_yields_no_extra_blank_option():
    options = _build_options(["gpt-image-1.5"], "")
    assert all(o["value"] for o in options)


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_settings_js_syntax_is_valid():
    subprocess.run(
        ["node", "--check", str(_SRC)],
        check=True,
    )
