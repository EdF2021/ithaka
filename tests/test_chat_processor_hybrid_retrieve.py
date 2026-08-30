"""Tests for ChatProcessor._hybrid_retrieve.

_hybrid_retrieve does hybrid memory retrieval: BM25-style keyword scoring
over the memory corpus, optionally blended with vector similarity scores
from ``memory_vector`` when it is present and healthy, with recency used
only as a small tiebreaker. Results below a relevance gate are dropped and
the survivors are sorted best-first and truncated to ``k``.

These tests call ``_hybrid_retrieve`` directly with plain memory-entry
dicts, bypassing MemoryManager/MemoryVectorStore entirely, so no real
Chroma/network access happens (see the sandbox note in the task: heavy
mocking, no real backends).
"""
import time

from src.chat_processor import ChatProcessor


class _FakeVectorStore:
    """Stand-in for MemoryVectorStore: configurable health, results, or a
    raised exception from .search()."""

    def __init__(self, healthy=True, results=None, raise_exc=None):
        self.healthy = healthy
        self._results = results or []
        self._raise = raise_exc

    def search(self, query, k=10):
        if self._raise is not None:
            raise self._raise
        return self._results


def _mem(mem_id, text, category="fact", ts=None):
    return {
        "id": mem_id,
        "text": text,
        "category": category,
        "timestamp": ts if ts is not None else time.time(),
    }


def _processor(memory_vector=None):
    return ChatProcessor(memory_manager=None, personal_docs_manager=None, memory_vector=memory_vector)


# ── empty / degenerate inputs ──

def test_empty_mem_entries_returns_empty_list():
    processor = _processor()
    assert processor._hybrid_retrieve("hello world", [], k=5) == []


def test_blank_message_returns_empty_list_even_with_entries():
    processor = _processor()
    mems = [_mem("A", "alpha bravo charlie")]
    assert processor._hybrid_retrieve("   ", mems, k=5) == []


def test_message_with_only_stopwords_and_no_vector_returns_empty_list():
    """No content tokens survive tokenization, and there's no vector
    backend to fall back to -> keyword retrieval can't run at all."""
    processor = _processor()
    mems = [_mem("A", "alpha bravo charlie delta echo")]
    assert processor._hybrid_retrieve("the is a", mems, k=5) == []


def test_message_with_only_stopwords_falls_back_to_vector_only():
    """No content tokens, but a healthy vector store is present: keyword
    scoring contributes nothing, so only entries with a strong-enough
    vector score survive the relevance gate (vs >= 0.20)."""
    fv = _FakeVectorStore(healthy=True, results=[
        {"memory_id": "A", "score": 0.9},
        {"memory_id": "B", "score": 0.1},  # below the 0.20 vector gate
    ])
    processor = _processor(memory_vector=fv)
    mems = [
        _mem("A", "alpha bravo charlie delta echo fillA"),
        _mem("B", "alpha bravo fillF fillG"),
    ]
    result = processor._hybrid_retrieve("the is a", mems, k=5)
    assert [m["id"] for m in result] == ["A"]


# ── keyword-only ranking (no vector backend) ──

def test_keyword_ranking_orders_by_relevance_and_filters_weak_matches():
    """More overlapping content tokens with the query yields a higher
    BM25-based score; an entry with no overlapping tokens is dropped
    entirely by the relevance gate."""
    processor = _processor()
    mems = [
        _mem("A", "alpha bravo charlie delta echo fillA"),
        _mem("B", "alpha bravo charlie fillB"),
        _mem("E", "alpha bravo fillF fillG"),
        _mem("D", "fillD fillE"),  # no overlap with the query at all
    ]
    result = processor._hybrid_retrieve("alpha bravo charlie delta echo", mems, k=10)
    assert [m["id"] for m in result] == ["A", "B", "E"]


def test_k_limits_number_of_results_to_the_best_scoring_entries():
    processor = _processor()
    mems = [
        _mem("A", "alpha bravo charlie delta echo fillA"),
        _mem("B", "alpha bravo charlie fillB"),
        _mem("C", "alpha charlie fillC"),
        _mem("D", "fillD fillE"),  # no overlap with the query at all
        _mem("E", "alpha bravo fillF fillG"),
    ]
    # Without a k limit, four entries clear the relevance gate.
    unlimited = processor._hybrid_retrieve("alpha bravo charlie delta echo", mems, k=10)
    assert [m["id"] for m in unlimited] == ["A", "B", "C", "E"]

    # k=3 truncates to the top three, in the same best-first order.
    limited = processor._hybrid_retrieve("alpha bravo charlie delta echo", mems, k=3)
    assert [m["id"] for m in limited] == ["A", "B", "C"]


def test_category_boost_favors_identity_match_for_name_queries():
    processor = _processor()
    mems = [
        _mem("nameentry", "User's name is Frank Smith.", category="identity"),
        _mem("other", "User enjoys biking on weekends.", category="fact"),
    ]
    result = processor._hybrid_retrieve("what is my name", mems, k=5)
    assert [m["id"] for m in result] == ["nameentry"]


# ── blended keyword + vector ranking ──

def test_vector_and_keyword_scores_are_blended_and_can_reorder_results():
    """A strong vector score can outrank a purely keyword-driven match,
    demonstrating the two signals are actually combined rather than one
    silently winning."""
    fv = _FakeVectorStore(healthy=True, results=[
        {"memory_id": "E", "score": 0.9},
        {"memory_id": "C", "score": 0.05},
    ])
    processor = _processor(memory_vector=fv)
    mems = [
        _mem("A", "alpha bravo charlie delta echo fillA"),
        _mem("B", "alpha bravo charlie fillB"),
        _mem("C", "alpha fillC"),
        _mem("E", "alpha bravo fillF fillG"),
    ]
    result = processor._hybrid_retrieve("alpha bravo charlie delta echo", mems, k=5)
    # "E" jumps to the top on the strength of its vector score even though
    # it has fewer keyword matches than "A" or "B".
    assert [m["id"] for m in result] == ["E", "A", "B"]


def test_unhealthy_vector_store_is_ignored_falls_back_to_keyword_only():
    """memory_vector.healthy is False -> the vector backend must not even
    be queried; behavior matches the no-vector keyword-only path exactly."""
    fv = _FakeVectorStore(healthy=False, raise_exc=RuntimeError("must not be called"))
    processor = _processor(memory_vector=fv)
    mems = [
        _mem("A", "alpha bravo charlie delta echo fillA"),
        _mem("B", "alpha bravo charlie fillB"),
        _mem("C", "alpha charlie fillC"),
        _mem("D", "fillD fillE"),
        _mem("E", "alpha bravo fillF fillG"),
    ]
    result = processor._hybrid_retrieve("alpha bravo charlie delta echo", mems, k=3)
    assert [m["id"] for m in result] == ["A", "B", "C"]


# ── degraded / failure path ──

def test_vector_search_exception_propagates_uncaught():
    """_hybrid_retrieve has no try/except around memory_vector.search(): a
    raising vector backend is a hard failure, not a silent degrade. This
    pins the current contract so a future change to swallow-and-fall-back
    is a deliberate decision, not an accident."""
    fv = _FakeVectorStore(healthy=True, raise_exc=RuntimeError("chroma down"))
    processor = _processor(memory_vector=fv)
    mems = [_mem("A", "alpha bravo charlie delta echo fillA")]

    try:
        processor._hybrid_retrieve("alpha bravo", mems, k=5)
        assert False, "expected RuntimeError to propagate"
    except RuntimeError as exc:
        assert "chroma down" in str(exc)


# ── no built-in deduplication ──

def test_duplicate_memory_entries_are_not_deduplicated():
    """_hybrid_retrieve scores/returns whatever is in mem_entries as given;
    it does not dedupe by id. If a caller hands in the same memory twice,
    it comes back twice. Pinned this as documented, actual behavior."""
    processor = _processor()
    dup = _mem("A", "alpha bravo charlie delta echo fillA")
    result = processor._hybrid_retrieve("alpha bravo charlie delta echo", [dup, dup], k=5)
    assert [m["id"] for m in result] == ["A", "A"]
