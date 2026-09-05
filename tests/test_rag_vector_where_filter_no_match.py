"""Regression test for issue #112's root-cause mechanism.

Root cause (demonstrated against an isolated instance during the #112
investigation): the production "Hybrid search for '...': 0 results"
turn was NOT retrieval being broken or the collection being empty — it was
the Chroma `where` filter (owner and/or notebook_id) matching nothing in an
otherwise non-empty collection. `lane.count()` reports the collection's
TOTAL size regardless of the filter, so the early `lane_count(...) == 0`
guard never trips; `query_lanes` then calls `collection.query(where=...)`,
which Chroma answers with `ids=[[]]` — an empty result with no exception,
so `VectorRAG.search` falls straight through to a legitimate-looking
`return []` rather than the `search failed` / keyword-fallback path.

This test pins that mechanism at the `VectorRAG.search` boundary: a
mismatched `where` filter must return `[]` cleanly (not raise, not fall
back to keyword search) while the SAME collection with the correct filter
values returns real hits.
"""
from src.rag_vector import VectorRAG
from src.embedding_lanes import LANE_FASTEMBED


def _matches_where(where, metadata):
    if where is None:
        return True
    if "$and" in where:
        return all(_matches_where(cond, metadata) for cond in where["$and"])
    for key, value in where.items():
        if isinstance(value, dict) and "$in" in value:
            if metadata.get(key) not in value["$in"]:
                return False
        elif metadata.get(key) != value:
            return False
    return True


class _FakeCollection:
    """Mirrors real Chroma: count() is the collection TOTAL, unaffected by
    a query's `where` filter — that's the crux of the reproduced bug."""

    def __init__(self, docs):
        self._docs = docs  # list of (id, document, metadata)

    def count(self):
        return len(self._docs)

    def query(self, query_embeddings, n_results, where, include):
        matched = [d for d in self._docs if _matches_where(where, d[2])][:n_results]
        return {
            "ids": [[d[0] for d in matched]],
            "documents": [[d[1] for d in matched]],
            "metadatas": [[d[2] for d in matched]],
            "distances": [[0.1 for _ in matched]],
        }


class _FakeLane:
    name = LANE_FASTEMBED

    def __init__(self, collection):
        self.collection = collection
        self.collection_name = "ithaka_rag_fastembed"

    def count(self):
        return self.collection.count()

    def encode(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


def _store(collection):
    store = VectorRAG.__new__(VectorRAG)
    store._lanes = [_FakeLane(collection)]
    store._healthy = True
    return store


def _make_collection():
    return _FakeCollection([
        ("chunk-1", "conclusies en aanbevelingen thesis tekst", {
            "owner": "admin", "notebook_id": "real-nb-id", "document_id": "doc-1",
            "filename": "conclusies.txt",
        }),
        ("chunk-2", "methodologie tekst", {
            "owner": "admin", "notebook_id": "real-nb-id", "document_id": "doc-2",
            "filename": "methodologie.txt",
        }),
    ])


def test_search_returns_empty_not_error_when_where_filter_matches_nothing():
    """A non-empty collection + a `where` filter that matches nothing (wrong
    notebook_id) must come back as a clean [] — the exact shape of the prod
    '0 results' log line — not an exception / keyword-search fallback."""
    store = _store(_make_collection())

    results = store.search(
        "conclusies en aanbevelingen", k=8, owner="admin", notebook_id="WRONG-nb-id",
    )

    assert results == []


def test_search_returns_empty_not_error_when_owner_filter_matches_nothing():
    store = _store(_make_collection())

    results = store.search(
        "conclusies en aanbevelingen", k=8, owner="wrong-owner", notebook_id="real-nb-id",
    )

    assert results == []


def test_search_returns_hits_when_where_filter_matches():
    """Same collection, correct filter values: retrieval is not broken —
    confirms the mismatch above is a filter-value problem, not a lane/
    collection problem."""
    store = _store(_make_collection())

    results = store.search(
        "conclusies en aanbevelingen", k=8, owner="admin", notebook_id="real-nb-id",
    )

    assert len(results) == 2
    assert {r["metadata"]["document_id"] for r in results} == {"doc-1", "doc-2"}
