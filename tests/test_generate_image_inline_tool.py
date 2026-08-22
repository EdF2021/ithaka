"""Inline image generation in chat: generate_image must be a native tool.

The image_gen backend has always worked (do_generate_image → OpenAI
gpt-image-1, verified live), and the XML/fenced tool path could reach it. But
generate_image is served by a builtin Python MCP server, which
get_all_openai_schemas deliberately skips, and it was not in
FUNCTION_TOOL_SCHEMAS either. So a native function-calling model (GPT/Claude/
Gemini — the models actually used) was never offered the tool and could not
generate an image inline in a normal chat.

These tests pin the two seams that make the native path work:
  1. generate_image is in FUNCTION_TOOL_SCHEMAS with `prompt` as the required
     property (the name matters — see below).
  2. a native tool_call round-trips through function_call_to_tool_block and
     _build_mcp_args with the prompt intact. The property MUST be `prompt`:
     function_call_to_tool_block json.dumps the args and _build_mcp_args only
     decodes them because `prompt` is in _MCP_JSON_PRIMARY_KEYS; any other name
     falls through to the line parser, which treats the whole JSON blob as the
     prompt (an image of a JSON string).
"""
import json

# agent_tools must import first to resolve the tool_schemas <-> agent_tools
# circular import (agent_tools/__init__ imports names from tool_schemas).
import src.agent_tools  # noqa: F401
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
from src.tool_execution import _build_mcp_args, _MCP_JSON_PRIMARY_KEYS


def _schema():
    matches = [
        s for s in FUNCTION_TOOL_SCHEMAS
        if s.get("function", {}).get("name") == "generate_image"
    ]
    assert len(matches) == 1, f"expected exactly one generate_image schema, got {len(matches)}"
    return matches[0]["function"]


def test_generate_image_is_a_native_function_tool():
    fn = _schema()
    assert fn["parameters"]["required"] == ["prompt"]
    assert "prompt" in fn["parameters"]["properties"]


def test_required_property_is_a_primary_mcp_key():
    """The schema's required property must be a key _build_mcp_args treats as a
    JSON primary, or the round trip silently degrades to a line parse."""
    fn = _schema()
    (required,) = fn["parameters"]["required"]
    assert required in _MCP_JSON_PRIMARY_KEYS["generate_image"]


def test_native_call_round_trips_with_prompt_intact():
    args = json.dumps({
        "prompt": "een rode zeilboot bij zonsondergang",
        "model": "gpt-image-1",
        "size": "1536x1024",
        "quality": "high",
    })
    block = function_call_to_tool_block("generate_image", args)
    assert block is not None and block.tool_type == "generate_image"

    mcp_args = _build_mcp_args("generate_image", block.content)
    assert mcp_args["prompt"] == "een rode zeilboot bij zonsondergang"
    assert mcp_args["model"] == "gpt-image-1"
    assert mcp_args["size"] == "1536x1024"
    assert mcp_args["quality"] == "high"


def test_minimal_native_call_only_prompt():
    block = function_call_to_tool_block("generate_image", json.dumps({"prompt": "een kat"}))
    assert block is not None
    assert _build_mcp_args("generate_image", block.content)["prompt"] == "een kat"
