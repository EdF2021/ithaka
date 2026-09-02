"""source_ids retrieval filter threaded through VectorRAG.search / fallback.

A notebook restricts chat to a bounded source set; the frontend lets the user
check/uncheck individual sources within a notebook. These tests pin the
Chroma where-filter shape (an extra ``$and`` condition alongside notebook_id)
and the keyword-fallback's Python-side equivalent, plus the ChatRequest field
that carries source_ids in from the API.
"""
import ast
import json
from pathlib import Path

import src.rag_vector as rag_vector
from src.rag_vector import VectorRAG
from src.request_models import ChatRequest

_CHAT_ROUTES_SRC = (
    Path(__file__).resolve().parent.parent / "routes" / "chat_routes.py"
).read_text(encoding="utf-8")


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


# --- /api/chat_stream form-data fallback (Task 4) ---------------------------
#
# chat.js's actual browser send is FormData, not a JSON body — `body` in
# chat_stream is only ever populated for `application/json` callers, so the
# JSON-body-only read below silently dropped source_ids for every real UI
# send. Task 4 added a form-field fallback (source_ids as a JSON-encoded
# string, exactly like the pre-existing `attachments` field) so the frontend
# checkbox panel actually reaches retrieval. Pinned two ways: a static check
# that the fallback code exists (mirrors test_chat_route_tool_policy.py's
# body-fallback tests for allow_bash/allow_web_search), and a functional test
# of the parsing logic itself.


def test_chat_stream_source_ids_falls_back_to_form_data():
    tree = ast.parse(_CHAT_ROUTES_SRC)
    chat_stream_func = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "chat_stream"),
        None,
    )
    assert chat_stream_func is not None, "chat_stream function not found"

    found = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "form_data"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "source_ids"
        for n in ast.walk(chat_stream_func)
    )
    assert found, "chat_stream must read source_ids from form_data as a fallback"


def _parse_source_ids_form_fallback(raw_body_value, raw_form_value):
    """Replicates chat_routes.py's source_ids parsing for a functional test
    without needing a full HTTP request/session fixture."""
    source_ids = None
    if isinstance(raw_body_value, list) and all(isinstance(x, str) for x in raw_body_value):
        source_ids = raw_body_value
    else:
        if raw_form_value:
            try:
                parsed = json.loads(raw_form_value)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                source_ids = parsed
    return source_ids


def test_form_fallback_parses_json_encoded_list():
    assert _parse_source_ids_form_fallback(None, json.dumps(["d1", "d2"])) == ["d1", "d2"]


def test_form_fallback_ignores_malformed_json():
    assert _parse_source_ids_form_fallback(None, "not json") is None


def test_form_fallback_ignores_non_list_or_non_string_items():
    assert _parse_source_ids_form_fallback(None, json.dumps({"a": 1})) is None
    assert _parse_source_ids_form_fallback(None, json.dumps(["a", 1])) is None


def test_form_fallback_ignores_missing_field():
    assert _parse_source_ids_form_fallback(None, None) is None
    assert _parse_source_ids_form_fallback(None, "") is None


def test_json_body_still_wins_over_form_field():
    """A JSON API caller's body list is used as-is, form_data isn't consulted."""
    assert _parse_source_ids_form_fallback(["b1"], json.dumps(["f1"])) == ["b1"]


# --- search_hint (#112) -------------------------------------------------
#
# A mindmap-node-click chat message carries the bare clicked label as
# `search_hint`, a best-effort anchor for the notebook RAG-condensation
# fallback in ChatProcessor._rag_preface — used only when condensation fails
# or comes back empty. It's best-effort by design, so an out-of-contract
# value (wrong type, blank, over 300 chars) must be silently ignored rather
# than 400ing the whole chat request. Pinned two ways, mirroring the
# source_ids tests above: a static check that chat_stream actually reads the
# field from form_data, and a functional test of the parsing/validation
# logic itself.


def test_chat_stream_reads_search_hint_from_form_data():
    tree = ast.parse(_CHAT_ROUTES_SRC)
    chat_stream_func = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "chat_stream"),
        None,
    )
    assert chat_stream_func is not None, "chat_stream function not found"

    found = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "form_data"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "search_hint"
        for n in ast.walk(chat_stream_func)
    )
    assert found, "chat_stream must read search_hint from form_data"


def _parse_search_hint(raw_form_value, raw_body_value=None):
    """Replicates chat_routes.py's search_hint validation for a functional
    test without needing a full HTTP request/session fixture: string, non-
    blank after stripping, max 300 chars — anything else is ignored (None),
    never a 400."""
    raw = raw_form_value
    if raw is None:
        raw = raw_body_value
    search_hint = None
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped and len(stripped) <= 300:
            search_hint = stripped
    return search_hint


def test_search_hint_accepts_a_plain_label():
    assert _parse_search_hint("Skills/integraties") == "Skills/integraties"


def test_search_hint_strips_surrounding_whitespace():
    assert _parse_search_hint("  Resultaten  ") == "Resultaten"


def test_search_hint_ignores_blank_or_whitespace_only():
    assert _parse_search_hint("") is None
    assert _parse_search_hint("   ") is None


def test_search_hint_ignores_missing_field():
    assert _parse_search_hint(None) is None


def test_search_hint_ignores_non_string():
    assert _parse_search_hint(None, raw_body_value=123) is None
    assert _parse_search_hint(None, raw_body_value=["x"]) is None


def test_search_hint_accepts_exactly_300_chars():
    hint = "x" * 300
    assert _parse_search_hint(hint) == hint


def test_search_hint_ignores_over_300_chars():
    assert _parse_search_hint("x" * 301) is None


def test_search_hint_form_field_wins_over_json_body():
    """Mirrors the actual precedence in chat_routes.py: form_data.get(...)
    is read first (the real browser send is always FormData), body is only
    consulted when the form field is absent."""
    assert _parse_search_hint("from-form", raw_body_value="from-body") == "from-form"
    assert _parse_search_hint(None, raw_body_value="from-body") == "from-body"


def test_chat_request_accepts_search_hint():
    req = ChatRequest(message="x", session="s", search_hint="Skills/integraties")
    assert req.search_hint == "Skills/integraties"

    default_req = ChatRequest(message="x", session="s")
    assert default_req.search_hint is None
