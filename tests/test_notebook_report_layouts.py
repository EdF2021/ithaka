"""Tests for src/notebook_report_layouts.py.

The LLM call is always monkeypatched (same convention as
tests/test_services_notebook_artifacts.py — hermetic, never reaches a real
endpoint).
"""
import uuid

import pytest

import core.database as db
import src.notebook_report_layouts as report_layouts
from tests.helpers.sqlite_db import make_temp_sqlite

_TS, _ENGINE, _TMPDB = make_temp_sqlite(db.Base.metadata)


def make_notebook(session, owner="ed", name="Thesis"):
    nb = db.Notebook(id=str(uuid.uuid4()), owner=owner, name=name)
    session.add(nb)
    session.commit()
    return nb


def make_document(session, title="Bron", owner="ed", content="inhoud"):
    doc = db.Document(id=str(uuid.uuid4()), title=title, owner=owner,
                      current_content=content)
    session.add(doc)
    session.commit()
    return doc


def make_source(session, notebook, filename="a.txt", content="inhoud",
                status="indexed", owner="ed"):
    doc = make_document(session, title=filename, owner=owner, content=content)
    src = db.NotebookSource(id=str(uuid.uuid4()), notebook_id=notebook.id,
                            document_id=doc.id, filename=filename,
                            status=status, chunk_count=1)
    session.add(src)
    session.commit()
    return src


def test_fixed_templates_have_three_entries_with_required_fields():
    assert len(report_layouts.FIXED_TEMPLATES) == 3
    keys = {t["key"] for t in report_layouts.FIXED_TEMPLATES}
    assert keys == {"overview", "study_material", "blogpost"}
    for t in report_layouts.FIXED_TEMPLATES:
        for field in ("key", "title", "description", "instruction"):
            assert isinstance(t[field], str) and t[field].strip(), (
                f'template {t.get("key")!r}: field "{field}" is missing or empty'
            )


def test_fingerprint_stable_for_same_entries_regardless_of_order():
    a = report_layouts._fingerprint_sources([("b.txt", "y"), ("a.txt", "x")])
    b = report_layouts._fingerprint_sources([("a.txt", "x"), ("b.txt", "y")])
    assert a == b


def test_fingerprint_changes_when_content_changes():
    a = report_layouts._fingerprint_sources([("a.txt", "x")])
    b = report_layouts._fingerprint_sources([("a.txt", "y")])
    assert a != b


def test_parse_layout_suggestions_valid_json():
    content = '''```json
[
  {"title": "T1", "description": "D1", "instruction": "I1"},
  {"title": "T2", "description": "D2", "instruction": "I2"}
]
```'''
    result = report_layouts._parse_layout_suggestions(content)
    assert result == [
        {"title": "T1", "description": "D1", "instruction": "I1"},
        {"title": "T2", "description": "D2", "instruction": "I2"},
    ]


def test_parse_layout_suggestions_no_json_raises():
    with pytest.raises(ValueError, match="geen JSON"):
        report_layouts._parse_layout_suggestions("gewoon tekst, geen json")


def test_parse_layout_suggestions_missing_field_raises():
    content = '```json\n[{"title": "T1", "description": "D1"}]\n```'
    with pytest.raises(ValueError, match="instruction"):
        report_layouts._parse_layout_suggestions(content)


def test_parse_layout_suggestions_caps_at_four():
    items = [{"title": f"T{i}", "description": "D", "instruction": "I"} for i in range(6)]
    import json
    content = "```json\n" + json.dumps(items) + "\n```"
    result = report_layouts._parse_layout_suggestions(content)
    assert len(result) == 4


def test_parse_layout_suggestions_single_line_fence_without_newline():
    """_JSON_FENCE_RE previously required a newline right after the opening
    ```json marker (\\s*\\n), so a fence the model puts entirely on one line
    — no newline between ```json and the array — didn't match at all."""
    content = '```json [{"title": "T1", "description": "D1", "instruction": "I1"}]```'
    result = report_layouts._parse_layout_suggestions(content)
    assert result == [{"title": "T1", "description": "D1", "instruction": "I1"}]


async def test_get_recommended_layouts_no_sources_returns_empty_no_llm_call(monkeypatch):
    calls = []

    async def _fake_llm(messages, **kwargs):
        calls.append(messages)
        return "should not be called"

    monkeypatch.setattr(report_layouts, "task_llm_call_async", _fake_llm)
    s = _TS()
    try:
        nb = make_notebook(s)
        result = await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert result == []
        assert calls == []
    finally:
        s.close()


async def test_get_recommended_layouts_generates_and_caches(monkeypatch):
    content = '''```json
[
  {"title": "T1", "description": "D1", "instruction": "I1"},
  {"title": "T2", "description": "D2", "instruction": "I2"},
  {"title": "T3", "description": "D3", "instruction": "I3"},
  {"title": "T4", "description": "D4", "instruction": "I4"}
]
```'''
    calls = []

    async def _fake_llm(messages, **kwargs):
        calls.append(messages)
        return content

    monkeypatch.setattr(report_layouts, "task_llm_call_async", _fake_llm)
    s = _TS()
    try:
        nb = make_notebook(s)
        make_source(s, nb, filename="a.txt", content="inhoud over AI-geletterdheid")

        result = await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert len(result) == 4
        assert result[0]["title"] == "T1"
        assert len(calls) == 1

        # Cached on the Notebook row.
        s.refresh(nb)
        assert nb.report_layouts_fingerprint is not None
        assert nb.report_layouts_json is not None

        # Second call with unchanged sources must not call the LLM again.
        result2 = await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert result2 == result
        assert len(calls) == 1
    finally:
        s.close()


async def test_get_recommended_layouts_regenerates_when_sources_change(monkeypatch):
    content = '```json\n[{"title": "T1", "description": "D1", "instruction": "I1"}]\n```'
    calls = []

    async def _fake_llm(messages, **kwargs):
        calls.append(messages)
        return content

    monkeypatch.setattr(report_layouts, "task_llm_call_async", _fake_llm)
    s = _TS()
    try:
        nb = make_notebook(s)
        make_source(s, nb, filename="a.txt", content="eerste versie")
        await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert len(calls) == 1

        make_source(s, nb, filename="b.txt", content="tweede bron")
        await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert len(calls) == 2
    finally:
        s.close()


async def test_get_recommended_layouts_bad_json_after_retries_returns_empty(monkeypatch):
    async def _fake_llm(messages, **kwargs):
        return "geen geldige json, ooit"

    monkeypatch.setattr(report_layouts, "task_llm_call_async", _fake_llm)
    s = _TS()
    try:
        nb = make_notebook(s)
        make_source(s, nb)
        result = await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert result == []
    finally:
        s.close()


def _system_content(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "system")


def _user_content(messages):
    return "\n".join(m["content"] for m in messages if m["role"] == "user")


async def test_source_text_never_in_system_role(monkeypatch):
    content = '```json\n[{"title": "T1", "description": "D1", "instruction": "I1"}]\n```'
    captured = {}

    async def _fake_llm(messages, **kwargs):
        captured["messages"] = messages
        return content

    monkeypatch.setattr(report_layouts, "task_llm_call_async", _fake_llm)
    s = _TS()
    try:
        nb = make_notebook(s)
        make_source(s, nb, filename="geheim.txt", content="STRIKT_VERTROUWELIJKE_MARKER")
        await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert "STRIKT_VERTROUWELIJKE_MARKER" not in _system_content(captured["messages"])
        assert "STRIKT_VERTROUWELIJKE_MARKER" in _user_content(captured["messages"])
    finally:
        s.close()


def test_dutch_output_rule_in_suggestion_prompt():
    from src.notebook_language import DUTCH_OUTPUT_RULE
    assert DUTCH_OUTPUT_RULE in report_layouts._LAYOUT_SUGGESTION_PROMPT


def test_gather_excerpt_text_caps_per_source():
    s = _TS()
    try:
        nb = make_notebook(s)
        long_text = "X" * 5000
        make_source(s, nb, filename="lang.txt", content=long_text)
        excerpt = report_layouts._gather_excerpt_text(nb, s, max_chars_per_source=2000)
        assert "(bron ingekort)" in excerpt
        assert len(excerpt) < len(long_text)
        # The excerpt content itself (before the truncation marker) must not
        # exceed the cap.
        body = excerpt.split("\n", 1)[1].replace("\n(bron ingekort)", "")
        assert len(body) <= 2000
    finally:
        s.close()


async def test_get_recommended_layouts_sends_excerpt_not_full_source_text(monkeypatch):
    """The suggestion call must use the small per-source excerpt, not
    gather_source_text's full-context payload — a notebook with a lot of
    source material must not blow the 45s request timeout on GET
    /report-layouts (which has no timeout exemption, unlike POST
    /artifacts)."""
    content = '```json\n[{"title": "T1", "description": "D1", "instruction": "I1"}]\n```'
    captured = {}

    async def _fake_llm(messages, **kwargs):
        captured["messages"] = messages
        return content

    monkeypatch.setattr(report_layouts, "task_llm_call_async", _fake_llm)
    s = _TS()
    try:
        nb = make_notebook(s)
        long_text = "Y" * 10_000
        make_source(s, nb, filename="groot.txt", content=long_text)
        await report_layouts.get_recommended_layouts(nb, s, "ed")
        user_content = _user_content(captured["messages"])
        # The full 10,000-char source text must not have been sent whole —
        # only a <=2000-char excerpt plus the truncation marker.
        assert long_text not in user_content
        assert "(bron ingekort)" in user_content
        assert len(user_content) < len(long_text)
    finally:
        s.close()


async def test_get_recommended_layouts_passes_wait_for_quiet_and_workload(monkeypatch):
    """Same self-deadlock gotcha generate_artifact avoids (see the comment
    at its task_llm_call_async call site in src/notebook_artifacts.py):
    the suggestion call also runs inside a tracked foreground request, so
    it must skip the interactive-quiet gate and use the foreground
    workload."""
    content = '```json\n[{"title": "T1", "description": "D1", "instruction": "I1"}]\n```'
    captured = {}

    async def _fake_llm(messages, **kwargs):
        captured["kwargs"] = kwargs
        return content

    monkeypatch.setattr(report_layouts, "task_llm_call_async", _fake_llm)
    s = _TS()
    try:
        nb = make_notebook(s)
        make_source(s, nb)
        await report_layouts.get_recommended_layouts(nb, s, "ed")
        assert captured["kwargs"].get("wait_for_quiet") is False
        assert captured["kwargs"].get("workload") == "foreground"
    finally:
        s.close()
