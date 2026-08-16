"""notebook_id must scope RAG retrieval via the Chroma where filter."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import src.rag_vector as rag_vector


class _FakeLane:
    def count(self):
        return 1


def _make_rag():
    rag = rag_vector.VectorRAG.__new__(rag_vector.VectorRAG)  # skip Chroma connect
    rag._healthy = True
    rag._lanes = [_FakeLane()]
    return rag


def _capture_where(monkeypatch, rag):
    captured = {}

    def fake_query_lanes(*args, **kwargs):
        captured["where"] = kwargs.get("where")
        return []

    # patch at the use-site module: rag_vector calls query_lanes(...)
    monkeypatch.setattr(rag_vector, "query_lanes", fake_query_lanes)
    return captured


def test_notebook_filter_is_anded_with_owner(monkeypatch):
    rag = _make_rag()
    captured = _capture_where(monkeypatch, rag)
    rag.search("q", k=3, owner="ed", notebook_id="nb-1")
    assert captured["where"] == {"$and": [{"owner": "ed"}, {"notebook_id": "nb-1"}]}


def test_notebook_filter_without_owner(monkeypatch):
    rag = _make_rag()
    captured = _capture_where(monkeypatch, rag)
    rag.search("q", notebook_id="nb-1")
    assert captured["where"] == {"notebook_id": "nb-1"}


def test_no_notebook_keeps_legacy_owner_filter(monkeypatch):
    rag = _make_rag()
    captured = _capture_where(monkeypatch, rag)
    rag.search("q", owner="ed")
    assert captured["where"] == {"owner": "ed"}


# --------------------------------------------------------------------------- #
# VectorRAG.remove_notebook — targeted chunk cleanup on notebook/source delete
# --------------------------------------------------------------------------- #


class _FakeCollection:
    """Mirrors tests/test_rag_remove_directory_scope.py's fake-collection contract."""

    def __init__(self, rows):
        self._ids = [r[0] for r in rows]
        self._metas = [r[1] for r in rows]

    def get(self, include=None):
        return {"ids": list(self._ids), "metadatas": list(self._metas)}

    def delete(self, ids=None):
        drop = set(ids or [])
        kept = [(i, m) for i, m in zip(self._ids, self._metas) if i not in drop]
        self._ids = [i for i, _ in kept]
        self._metas = [m for _, m in kept]


def _make_vectorrag_with_collection(rows):
    rag = rag_vector.VectorRAG.__new__(rag_vector.VectorRAG)  # skip Chroma connect
    rag._collection = _FakeCollection(rows)
    rag._healthy = True
    return rag


def test_remove_notebook_deletes_only_that_notebooks_chunks():
    rows = [
        ("a", {"notebook_id": "nb-1", "document_id": "doc-1"}),
        ("b", {"notebook_id": "nb-1", "document_id": "doc-2"}),
        ("c", {"notebook_id": "nb-2", "document_id": "doc-3"}),
        ("d", {"filename": "no-notebook.md"}),
    ]
    rag = _make_vectorrag_with_collection(rows)
    res = rag.remove_notebook("nb-1")
    assert res["success"] is True
    assert res["removed_count"] == 2
    remaining = set(rag._collection.get()["ids"])
    assert remaining == {"c", "d"}, remaining


def test_remove_notebook_scoped_to_one_document():
    rows = [
        ("a", {"notebook_id": "nb-1", "document_id": "doc-1"}),
        ("b", {"notebook_id": "nb-1", "document_id": "doc-2"}),
    ]
    rag = _make_vectorrag_with_collection(rows)
    res = rag.remove_notebook("nb-1", document_id="doc-1")
    assert res["success"] is True
    assert res["removed_count"] == 1
    remaining = set(rag._collection.get()["ids"])
    assert remaining == {"b"}, remaining


def test_remove_notebook_no_match_is_noop():
    rag = _make_vectorrag_with_collection([("a", {"notebook_id": "nb-1"})])
    res = rag.remove_notebook("nb-nowhere")
    assert res["success"] is True
    assert res["removed_count"] == 0
    assert set(rag._collection.get()["ids"]) == {"a"}


# --------------------------------------------------------------------------- #
# search() error path — _keyword_search_fallback must stay notebook-scoped
# --------------------------------------------------------------------------- #


class _FakeFallbackCollection:
    def __init__(self, rows):
        self._ids = [r[0] for r in rows]
        self._docs = [r[1] for r in rows]
        self._metas = [r[2] for r in rows]

    def count(self):
        return len(self._ids)

    def get(self, include=None):
        return {"ids": list(self._ids), "documents": list(self._docs), "metadatas": list(self._metas)}


class _FakeFallbackLane:
    def __init__(self, name, collection):
        self.name = name
        self.collection = collection

    def count(self):
        return self.collection.count()


def test_keyword_fallback_respects_notebook_id(monkeypatch):
    rows = [
        ("a", "apple pie recipe", {"owner": "ed", "notebook_id": "nb-1"}),
        ("b", "apple pie recipe", {"owner": "ed", "notebook_id": "nb-2"}),
        ("c", "apple pie recipe", {"owner": "ed"}),
    ]
    collection = _FakeFallbackCollection(rows)
    rag = rag_vector.VectorRAG.__new__(rag_vector.VectorRAG)  # skip Chroma connect
    rag._healthy = True
    rag._lanes = [_FakeFallbackLane("fastembed", collection)]

    def raising_query_lanes(*args, **kwargs):
        raise RuntimeError("lane down")

    monkeypatch.setattr(rag_vector, "query_lanes", raising_query_lanes)

    results = rag.search("apple pie", k=5, owner="ed", notebook_id="nb-1")
    ids = {r["id"] for r in results}
    assert ids == {"a"}, ids
