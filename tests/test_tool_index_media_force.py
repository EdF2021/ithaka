"""Media generation keyword-force: image/video phrases must force-include
generate_image/generate_video in the tools offered for a query, and the
generate_video function schema must exist.

Mirrors the keyword-loop isolation pattern in
tests/test_tool_index_keyword_boundaries.py: `retrieve` (chroma-backed) is
stubbed out so these tests exercise only the keyword-hint loop, not real
embedding retrieval.
"""
# agent_tools must import first to resolve the tool_schemas <-> agent_tools
# circular import (agent_tools/__init__ imports names from tool_schemas).
import src.agent_tools  # noqa: F401
from src.tool_index import ToolIndex
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS


def _index():
    ti = ToolIndex.__new__(ToolIndex)
    ti.retrieve = lambda query, k=8: []  # no chroma; isolate the keyword loop
    return ti


def _forced(text):
    return _index().get_tools_for_query(text)


def test_video_phrases_force_generate_video():
    for t in ("maak een video van een kat", "make a video of a cat", "genereer een filmpje", "create a short clip"):
        assert "generate_video" in _forced(t), t


def test_image_phrases_force_generate_image():
    for t in ("maak een afbeelding van een kat", "generate an image of a cat", "teken een logo"):
        assert "generate_image" in _forced(t), t


def test_generate_video_schema_present():
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS if s.get("type") == "function"}
    assert "generate_video" in names
