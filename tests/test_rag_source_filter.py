"""source_ids retrieval filter threaded through VectorRAG.search / fallback.

A notebook restricts chat to a bounded source set; the frontend lets the user
check/uncheck individual sources within a notebook. These tests pin the
Chroma where-filter shape (an extra ``$and`` condition alongside notebook_id)
and the keyword-fallback's Python-side equivalent, plus the ChatRequest field
that carries source_ids in from the API.
"""
import src.rag_vector as rag_vector
from src.rag_vector import VectorRAG
from src.request_models import ChatRequest


class _FakeLane:
    """Stand-in embedding lane: just enough for lane_count() to see stock."""

    def count(self):
        return 1


def _store():
    store = VectorRAG.__new__(VectorRAG)
    store._lanes = [_FakeLane()]
    store._healthy = True
    return store


def _capture_where(monkeypatch):
    captured = {}

    def _fake_query_lanes(lanes, query, n_results, include, where=None, raise_if_all_failed=False):
        captured["where"] = where
        return []

    monkeypatch.setattr(rag_vector, "query_lanes", _fake_query_lanes)
    return captured


def test_where_filter_combines_notebook_and_source_ids(monkeypatch):
    store = _store()
    captured = _capture_where(monkeypatch)

    store.search("query", notebook_id="nb1", source_ids=["d1", "d2"])

    assert captured["where"] == {
        "$and": [
            {"notebook_id": "nb1"},
            {"document_id": {"$in": ["d1", "d2"]}},
        ]
    }


def test_source_ids_none_or_empty_means_no_document_filter(monkeypatch):
    store = _store()
    captured = _capture_where(monkeypatch)

    store.search("query", notebook_id="nb1", source_ids=None)
    assert captured["where"] == {"notebook_id": "nb1"}

    store.search("query", notebook_id="nb1", source_ids=[])
    assert captured["where"] == {"notebook_id": "nb1"}


def test_keyword_fallback_respects_source_ids():
    store = VectorRAG.__new__(VectorRAG)

    class _FakeCollection:
        def count(self):
            return 2

        def get(self, include=None):
            return {
                "ids": ["c1", "c2"],
                "documents": ["match one text", "match two text"],
                "metadatas": [
                    {"document_id": "d1"},
                    {"document_id": "d3"},
                ],
            }

    store._active_collections = lambda: [("fastembed", _FakeCollection())]

    results = store._keyword_search_fallback("match", k=10, source_ids=["d1"])

    ids = {r["id"] for r in results}
    assert ids == {"c1"}
    assert "c2" not in ids


def test_chat_request_accepts_source_ids():
    req = ChatRequest(message="x", session="s", source_ids=["a"])
    assert req.source_ids == ["a"]

    default_req = ChatRequest(message="x", session="s")
    assert default_req.source_ids is None
