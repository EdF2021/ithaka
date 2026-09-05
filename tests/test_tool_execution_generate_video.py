"""generate_video's dispatch through execute_tool_block (src/tool_execution.py).

The generic `elif tool in dynamic_handlers:` fallback in
_execute_tool_block_impl calls `_direct_fallback(tool, content,
progress_cb=progress_cb)` WITHOUT owner, so any TOOL_HANDLERS-registered tool
without its own explicit dispatch branch silently gets owner=None. That is
fine for owner-agnostic tools, but generate_video's owner flows straight into
video_gen.start_video_job(prompt, owner, ...) for job ownership / the
GET /api/video/jobs/{id} owner-check — an unowned job would be a real gap. So
generate_video has its own explicit branch (mirroring manage_endpoints) that
threads owner=owner through. This test exercises that dispatch path directly
(not just GenerateVideoTool.execute in isolation) so a future refactor that
drops the branch fails here.
"""
import json

from src.agent_tools import ToolBlock


class FakeVG:
    def __init__(self):
        self.calls = []

    def start_video_job(self, prompt, owner, **kw):
        self.calls.append((prompt, owner, kw))
        return "c" * 32

    def estimate_cost_usd(self, model, duration, resolution="720p"):
        return 2.4

    VEO_MODELS = ("veo-3.1-generate-preview",)


def _patch_backend(monkeypatch, fake, enabled=True):
    import src.agent_tools.video_tools as video_tools_mod
    monkeypatch.setattr(video_tools_mod, "video_gen", fake)
    monkeypatch.setattr(
        video_tools_mod, "get_user_setting",
        lambda k, owner="", d=None: {"video_gen_enabled": enabled}.get(k, d),
    )


async def test_execute_tool_block_threads_owner_into_start_video_job(monkeypatch):
    from src.tool_execution import execute_tool_block

    fake = FakeVG()
    _patch_backend(monkeypatch, fake)

    block = ToolBlock("generate_video", json.dumps({"prompt": "a cat"}))
    desc, result = await execute_tool_block(block, owner="ed")

    assert result["video_job_id"] == "c" * 32
    assert fake.calls, "start_video_job was never called"
    assert fake.calls[0][1] == "ed", "owner must reach video_gen.start_video_job"


async def test_execute_tool_block_respects_disabled_tools(monkeypatch):
    from src.tool_execution import execute_tool_block

    fake = FakeVG()
    _patch_backend(monkeypatch, fake)

    block = ToolBlock("generate_video", json.dumps({"prompt": "a cat"}))
    desc, result = await execute_tool_block(block, owner="ed", disabled_tools={"generate_video"})

    assert "error" in result
    assert result.get("exit_code") == 1
    assert not fake.calls, "start_video_job must not run when the tool is disabled"
