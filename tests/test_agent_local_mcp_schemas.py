"""Local-model MCP schema selection (_local_mcp_schemas).

Local models used to receive MCP schemas only when the message contained a
hardcoded keyword ("mcp", "browse", ...) — and then the FULL catalog. With a
big server connected (Google Drive alone exposes 116 tools) that meant either
no Drive tools at all ("zoek in mijn Google Drive" matches no keyword) or an
overwhelming 200+ schema payload. The tool-RAG selection now drives the
subset; the keyword gate remains as fallback.
"""
from src.agent_loop import _local_mcp_schemas


def _schema(name):
    return {"type": "function", "function": {"name": name}}


MCP = [_schema("mcp__abc__search"), _schema("mcp__abc__uploadFile"), _schema("mcp__xyz__listEvents")]


def test_rag_selected_subset_is_sent_without_keyword():
    out = _local_mcp_schemas(
        "Zoek in mijn Google Drive naar het projectplan",
        MCP,
        {"mcp__abc__search", "web_search"},
        set(),
    )
    assert [s["function"]["name"] for s in out] == ["mcp__abc__search"]


def test_keyword_fallback_sends_catalog_when_selection_has_no_mcp():
    out = _local_mcp_schemas("open de mcp tools eens", MCP, {"web_search"}, set())
    assert len(out) == len(MCP)


def test_no_keyword_and_no_selection_sends_nothing():
    out = _local_mcp_schemas("hoe laat is het?", MCP, set(), set())
    assert out == []


def test_disabled_tools_are_filtered_in_both_paths():
    out = _local_mcp_schemas(
        "Zoek in mijn Google Drive",
        MCP,
        {"mcp__abc__search", "mcp__abc__uploadFile"},
        {"mcp__abc__uploadFile"},
    )
    assert [s["function"]["name"] for s in out] == ["mcp__abc__search"]
    out2 = _local_mcp_schemas("gebruik mcp", MCP, set(), {"mcp__xyz__listEvents"})
    assert [s["function"]["name"] for s in out2] == [
        "mcp__abc__search",
        "mcp__abc__uploadFile",
    ]
