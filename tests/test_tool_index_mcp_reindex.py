"""MCP-herindexering zonder koud-gat (issue #62).

De oude flow verwijderde eerst alle MCP-entries en deed daarna pas de (trage)
embedding-upsert; de 1.5s-cap in agent_loop kapte de caller mid-upsert af,
waardoor een gelijktijdige retrieval alle MCP-tools kwijt was. Nu: parse eerst,
upsert, prune daarna alleen verdwenen ids — en agent_loop wacht er niet meer op
(fire-and-forget, gededuped).
"""
import asyncio

import pytest

from tests.helpers.embedding_lanes import FakeChroma, FakeEmbedder, patch_chroma


class FakeMcpManager:
    def __init__(self, prompt_text, generation=1):
        self._generation = generation
        self._prompt_text = prompt_text
        self.calls = 0

    def get_tool_descriptions_for_prompt(self, disabled_map):
        self.calls += 1
        if isinstance(self._prompt_text, Exception):
            raise self._prompt_text
        return self._prompt_text


def _make_index(monkeypatch):
    fake = FakeChroma()
    patch_chroma(monkeypatch, fake)
    import src.embedding_lanes as lanes

    monkeypatch.setattr(lanes, "_build_custom_client", lambda: FakeEmbedder(768, "nomic", "http://embeddings/v1"))
    monkeypatch.setattr(lanes, "_build_fastembed_client", lambda: FakeEmbedder(384, "mini", "local://fastembed"))
    from src.tool_index import ToolIndex

    return ToolIndex(), fake


def _mcp_ids(fake):
    col = fake.collections["ithaka_tool_index_fastembed"]
    return set(col.get(where={"tool_type": "mcp"})["ids"])


PROMPT_V1 = "**drive:**\n- mcp__d1__search: Zoek bestanden in Drive\n- mcp__d1__upload: Upload een bestand\n"
PROMPT_V2 = "**drive:**\n- mcp__d1__search: Zoek bestanden in Drive\n- mcp__c1__listEvents: Agenda-events ophalen\n"


def test_reindex_upserts_then_prunes_only_stale_ids(monkeypatch):
    idx, fake = _make_index(monkeypatch)
    mgr = FakeMcpManager(PROMPT_V1, generation=1)
    idx.index_mcp_tools(mgr, {})
    assert _mcp_ids(fake) == {"mcp_mcp__d1__search", "mcp_mcp__d1__upload"}

    mgr._generation = 2
    mgr._prompt_text = PROMPT_V2
    idx.index_mcp_tools(mgr, {})
    assert _mcp_ids(fake) == {"mcp_mcp__d1__search", "mcp_mcp__c1__listEvents"}


def test_reindex_skips_when_generation_unchanged(monkeypatch):
    idx, fake = _make_index(monkeypatch)
    mgr = FakeMcpManager(PROMPT_V1, generation=1)
    idx.index_mcp_tools(mgr, {})
    idx.index_mcp_tools(mgr, {})
    assert mgr.calls == 1


def test_transient_failure_keeps_index_and_retries(monkeypatch):
    idx, fake = _make_index(monkeypatch)
    mgr = FakeMcpManager(PROMPT_V1, generation=1)
    idx.index_mcp_tools(mgr, {})

    mgr._generation = 2
    mgr._prompt_text = RuntimeError("mcp даун")
    idx.index_mcp_tools(mgr, {})
    # index onaangetast, generatie niet geconsumeerd
    assert _mcp_ids(fake) == {"mcp_mcp__d1__search", "mcp_mcp__d1__upload"}

    mgr._prompt_text = PROMPT_V2
    idx.index_mcp_tools(mgr, {})
    assert _mcp_ids(fake) == {"mcp_mcp__d1__search", "mcp_mcp__c1__listEvents"}


def test_empty_catalog_prunes_all_mcp_entries(monkeypatch):
    idx, fake = _make_index(monkeypatch)
    mgr = FakeMcpManager(PROMPT_V1, generation=1)
    idx.index_mcp_tools(mgr, {})

    mgr._generation = 2
    mgr._prompt_text = ""
    idx.index_mcp_tools(mgr, {})
    assert _mcp_ids(fake) == set()


class _SlowIndex:
    _mcp_generation = 0

    def __init__(self):
        self.calls = 0
        self.release = asyncio.Event()

    def index_mcp_tools(self, mcp_mgr, disabled_map):
        self.calls += 1


async def test_schedule_mcp_reindex_dedupes_and_checks_generation():
    from src.agent_loop import _schedule_mcp_reindex
    import src.agent_loop as al

    al._mcp_reindex_task = None
    idx = _SlowIndex()
    mgr = FakeMcpManager(PROMPT_V1, generation=1)

    t1 = _schedule_mcp_reindex(idx, mgr, {})
    assert t1 is not None
    # tweede call terwijl de eerste nog loopt -> gededuped
    assert _schedule_mcp_reindex(idx, mgr, {}) is None
    await t1
    assert idx.calls == 1

    # generatie gelijk -> geen nieuwe taak
    idx._mcp_generation = 1
    assert _schedule_mcp_reindex(idx, mgr, {}) is None

    # generatie verspringt -> wel een nieuwe taak
    mgr._generation = 2
    t2 = _schedule_mcp_reindex(idx, mgr, {})
    assert t2 is not None
    await t2
    assert idx.calls == 2
    al._mcp_reindex_task = None
