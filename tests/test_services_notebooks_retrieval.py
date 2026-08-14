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
