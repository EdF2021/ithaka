"""generate_video must be reachable as a native function-calling tool (not
just via TOOL_HANDLERS/direct dispatch).

Cross-task finding (PR #149, intents agent): TOOL_TAGS gates two independent
paths — function_call_to_tool_block()'s tool_type allowlist and the fenced
```<tag> ...``` regex built from TOOL_TAGS in tool_parsing.py. Without
"generate_video" in TOOL_TAGS, a native model's function call to
generate_video would silently fail with "Unknown function call" and a
```generate_video``` fenced block would never match. src/agent_tools/__init__.py
already lists it there (mirroring generate_image); this test pins both paths
end to end, including the round trip back through GenerateVideoTool's own
content parsing.
"""
import json

import src.agent_tools  # noqa: F401 - resolves the tool_schemas <-> agent_tools circular import
from src.tool_schemas import function_call_to_tool_block
from src.tool_parsing import parse_tool_blocks
from src.agent_tools.video_tools import GenerateVideoTool


def test_native_function_call_produces_generate_video_block():
    block = function_call_to_tool_block(
        "generate_video", json.dumps({"prompt": "a cat", "duration_seconds": 4})
    )
    assert block is not None
    assert block.tool_type == "generate_video"
    payload = json.loads(block.content)
    assert payload["prompt"] == "a cat"
    assert payload["duration_seconds"] == 4


async def test_native_function_call_content_round_trips_through_the_tool(monkeypatch):
    import src.agent_tools.video_tools as video_tools_mod

    class FakeVG:
        def __init__(self):
            self.calls = []

        def start_video_job(self, prompt, owner, **kw):
            self.calls.append((prompt, owner, kw))
            return "b" * 32

        def estimate_cost_usd(self, model, duration, resolution="720p"):
            return 1.6

        VEO_MODELS = ("veo-3.1-generate-preview",)

    fake = FakeVG()
    monkeypatch.setattr(video_tools_mod, "video_gen", fake)
    monkeypatch.setattr(
        video_tools_mod,
        "get_setting",
        lambda k, d=None: {"video_gen_enabled": True}.get(k, d),
    )

    block = function_call_to_tool_block(
        "generate_video", json.dumps({"prompt": "a cat", "duration_seconds": 4})
    )
    result = await GenerateVideoTool().execute(block.content, {"owner": "ed"})

    assert result["video_job_id"] == "b" * 32
    assert fake.calls[0][0] == "a cat"
    assert fake.calls[0][2]["duration_seconds"] == 4


def test_fenced_generate_video_block_is_parsed():
    text = '```generate_video\n{"prompt": "a cat"}\n```'
    blocks = parse_tool_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "generate_video"
    assert json.loads(blocks[0].content) == {"prompt": "a cat"}


def test_generate_video_in_tool_tags():
    from src.agent_tools import TOOL_TAGS
    assert "generate_video" in TOOL_TAGS
