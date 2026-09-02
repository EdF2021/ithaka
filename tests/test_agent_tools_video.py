"""generate_video builtin tool (src/agent_tools/video_tools.py).

Mirrors the generate_image contract: a native TOOL_HANDLERS entry that parses
JSON or plain-text content, resolves the owner from ctx like the other
agent_tools (grep ctx.get("owner")), and starts an async Veo job through the
src.video_gen backend (built in parallel — see docs/superpowers/plans/
2026-09-02-image-video-autoroute.md, Task 3). The backend module is imported
defensively so this module (and TOOL_HANDLERS registration) still imports
cleanly before that PR lands.
"""
import json

import src.agent_tools.video_tools as video_tools_mod
from src.agent_tools.video_tools import GenerateVideoTool


class FakeVG:
    def __init__(self):
        self.calls = []

    def start_video_job(self, prompt, owner, **kw):
        self.calls.append((prompt, owner, kw))
        return "a" * 32

    def estimate_cost_usd(self, model, duration, resolution="720p"):
        return 3.2

    VEO_MODELS = ("veo-3.1-generate-preview",)


class RaisingVG(FakeVG):
    def __init__(self, exc):
        super().__init__()
        self._exc = exc

    def start_video_job(self, prompt, owner, **kw):
        self.calls.append((prompt, owner, kw))
        raise self._exc


def _settings(overrides=None):
    base = {"video_gen_enabled": True, "video_model": "veo-3.1-generate-preview",
            "video_resolution": "720p", "video_aspect_ratio": "16:9",
            "video_duration_seconds": 8}
    if overrides:
        base.update(overrides)
    return lambda k, d=None: base.get(k, d)


async def test_json_args_start_job(monkeypatch):
    fake = FakeVG()
    monkeypatch.setattr(video_tools_mod, "video_gen", fake)
    monkeypatch.setattr(video_tools_mod, "get_setting", _settings())
    r = await GenerateVideoTool().execute(
        json.dumps({"prompt": "a cat", "aspect_ratio": "9:16", "duration_seconds": 4}),
        {"owner": "ed"},
    )
    assert r["video_job_id"] == "a" * 32
    assert r["video_status"] == "running"
    assert r["video_url"] is None
    assert r["video_model"] == "veo-3.1-generate-preview"
    assert isinstance(r["video_cost_estimate"], float)
    assert fake.calls[0][0] == "a cat"
    assert fake.calls[0][1] == "ed"
    assert fake.calls[0][2]["aspect_ratio"] == "9:16"
    assert fake.calls[0][2]["duration_seconds"] == 4
    assert "Video generation started" in r["output"]


async def test_plain_text_prompt(monkeypatch):
    fake = FakeVG()
    monkeypatch.setattr(video_tools_mod, "video_gen", fake)
    monkeypatch.setattr(video_tools_mod, "get_setting", _settings())
    r = await GenerateVideoTool().execute("a dog surfing", {"owner": "ed"})
    assert r["video_job_id"] == "a" * 32
    assert fake.calls[0][0] == "a dog surfing"
    # No aspect_ratio/duration in plain text -> falls back to settings defaults.
    assert fake.calls[0][2]["aspect_ratio"] == "16:9"
    assert fake.calls[0][2]["duration_seconds"] == 8


async def test_settings_duration_as_string_is_coerced_to_int(monkeypatch):
    """video_duration_seconds may round-trip as a string through the settings
    <select> (POST /api/auth/settings) — the backend documents ValueError on
    invalid params, so a bare passthrough would risk sending "8" instead of 8."""
    fake = FakeVG()
    monkeypatch.setattr(video_tools_mod, "video_gen", fake)
    monkeypatch.setattr(video_tools_mod, "get_setting", _settings({"video_duration_seconds": "6"}))
    r = await GenerateVideoTool().execute("a dog surfing", {"owner": "ed"})
    assert r["video_job_id"] == "a" * 32
    assert fake.calls[0][2]["duration_seconds"] == 6
    assert isinstance(fake.calls[0][2]["duration_seconds"], int)


async def test_settings_duration_unparseable_falls_back_to_default(monkeypatch):
    fake = FakeVG()
    monkeypatch.setattr(video_tools_mod, "video_gen", fake)
    monkeypatch.setattr(video_tools_mod, "get_setting", _settings({"video_duration_seconds": "not-a-number"}))
    r = await GenerateVideoTool().execute("a dog surfing", {"owner": "ed"})
    assert fake.calls[0][2]["duration_seconds"] == 8


async def test_disabled_setting(monkeypatch):
    fake = FakeVG()
    monkeypatch.setattr(video_tools_mod, "video_gen", fake)
    monkeypatch.setattr(
        video_tools_mod, "get_setting",
        _settings({"video_gen_enabled": False}),
    )
    r = await GenerateVideoTool().execute("a dog surfing", {"owner": "ed"})
    assert "error" in r and r["exit_code"] == 1
    assert not fake.calls  # no job started


async def test_missing_endpoint(monkeypatch):
    fake = RaisingVG(RuntimeError("Geen Gemini-endpoint met API-key"))
    monkeypatch.setattr(video_tools_mod, "video_gen", fake)
    monkeypatch.setattr(video_tools_mod, "get_setting", _settings())
    r = await GenerateVideoTool().execute("a dog surfing", {"owner": "ed"})
    assert r["error"] == "Geen Gemini-endpoint met API-key"
    assert r["exit_code"] == 1
    assert "video_job_id" not in r


async def test_invalid_params_value_error(monkeypatch):
    fake = RaisingVG(ValueError("duration_seconds must be one of 4, 6, 8"))
    monkeypatch.setattr(video_tools_mod, "video_gen", fake)
    monkeypatch.setattr(video_tools_mod, "get_setting", _settings())
    r = await GenerateVideoTool().execute(
        json.dumps({"prompt": "a cat", "duration_seconds": 99}), {"owner": "ed"}
    )
    assert "error" in r and r["exit_code"] == 1


async def test_empty_prompt_is_rejected(monkeypatch):
    fake = FakeVG()
    monkeypatch.setattr(video_tools_mod, "video_gen", fake)
    monkeypatch.setattr(video_tools_mod, "get_setting", _settings())
    r = await GenerateVideoTool().execute("   ", {"owner": "ed"})
    assert "error" in r and r["exit_code"] == 1
    assert not fake.calls


async def test_backend_not_available(monkeypatch):
    monkeypatch.setattr(video_tools_mod, "video_gen", None)
    monkeypatch.setattr(video_tools_mod, "get_setting", _settings())
    r = await GenerateVideoTool().execute("a dog surfing", {"owner": "ed"})
    assert "error" in r and r["exit_code"] == 1


def test_registered_in_tool_handlers():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    assert "generate_video" in TOOL_HANDLERS
    assert "generate_video" in TOOL_TAGS
