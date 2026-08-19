"""Follow-up question suggestions: parse_questions parser + suggest_questions route.

Route tests mirror tests/test_routes_notebook_artifacts.py (file-backed temp
sqlite via make_temp_sqlite, monkeypatched nbr.SessionLocal +
nbr.get_current_user, and the LLM chain replaced by monkeypatching
``routes.notebook_routes.suggest_questions`` at module level).
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-suggest")

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import routes.notebook_routes as nbr
from src.notebook_suggest import parse_questions
from tests.helpers.sqlite_db import make_temp_sqlite


# ---- parse_questions (pure function) ----

def test_parse_json_array():
    assert parse_questions('["Waarom?", "Hoe?", "Wat?"]') == ["Waarom?", "Hoe?", "Wat?"]


def test_parse_json_in_prose_and_fences():
    txt = 'Hier zijn ze:\n```json\n["A?", "B?"]\n```'
    assert parse_questions(txt) == ["A?", "B?"]


def test_parse_garbage_returns_empty():
    assert parse_questions("geen json hier") == []
    assert parse_questions('{"vraag": "x"}') == []  # geen array van strings
    assert parse_questions("") == []
    assert parse_questions(None) == []


def test_parse_caps_at_three_and_strips():
    assert parse_questions('[" A? ", "B?", "C?", "D?"]') == ["A?", "B?", "C?"]


def test_parse_skips_non_string_items():
    assert parse_questions('[1, "A?", null, "B?"]') == ["A?", "B?"]


# ---- route ----

class _FakeRagManager:
    def __init__(self):
        self.vector_rag = self

    def add_document(self, text, metadata):
        return True

    def remove_notebook(self, notebook_id, document_id=None):
        pass

    def _split_into_chunks(self, text):
        return [text]


@pytest.fixture()
def ts(monkeypatch):
    test_session_local, engine, tmpfile = make_temp_sqlite(db.Base.metadata)
    monkeypatch.setattr(nbr, "SessionLocal", test_session_local)
    yield test_session_local
    tmpfile.close()


def _client(monkeypatch, user="ed"):
    monkeypatch.setattr(nbr, "get_current_user", lambda request: user)
    app = FastAPI()
    app.include_router(nbr.setup_notebook_routes(rag_manager=_FakeRagManager()))
    return TestClient(app, raise_server_exceptions=False)


def _make_notebook(c, name="NB"):
    return c.post("/api/notebooks", json={"name": name}).json()["id"]


async def _fake_suggest_ok(question, answer, owner):
    return ["A?", "B?", "C?"]


async def _fake_suggest_boom(question, answer, owner):
    raise RuntimeError("LLM down")


def test_suggest_questions_ok(monkeypatch, ts):
    monkeypatch.setattr(nbr, "suggest_questions", _fake_suggest_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/suggest_questions",
               json={"question": "Q?", "answer": "A."})
    assert r.status_code == 200
    assert r.json() == {"questions": ["A?", "B?", "C?"]}


def test_suggest_questions_unknown_notebook_404(monkeypatch, ts):
    monkeypatch.setattr(nbr, "suggest_questions", _fake_suggest_ok)
    c = _client(monkeypatch)

    r = c.post("/api/notebooks/nope/suggest_questions",
               json={"question": "Q?", "answer": "A."})
    assert r.status_code == 404


def test_suggest_questions_foreign_notebook_404(monkeypatch, ts):
    monkeypatch.setattr(nbr, "suggest_questions", _fake_suggest_ok)
    c = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c)

    other = _client(monkeypatch, user="mallory")
    r = other.post(f"/api/notebooks/{nb_id}/suggest_questions",
                   json={"question": "Q?", "answer": "A."})
    assert r.status_code == 404


def test_suggest_questions_llm_failure_is_empty_200(monkeypatch, ts):
    monkeypatch.setattr(nbr, "suggest_questions", _fake_suggest_boom)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/suggest_questions",
               json={"question": "Q?", "answer": "A."})
    assert r.status_code == 200
    assert r.json() == {"questions": []}


@pytest.mark.parametrize("body", [
    {},
    {"question": "Q?"},
    {"answer": "A."},
    {"question": "", "answer": "A."},
    [1, 2],
])
def test_suggest_questions_bad_body_400(monkeypatch, ts, body):
    monkeypatch.setattr(nbr, "suggest_questions", _fake_suggest_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/suggest_questions", json=body)
    assert r.status_code == 400


def test_suggest_questions_invalid_json_400(monkeypatch, ts):
    monkeypatch.setattr(nbr, "suggest_questions", _fake_suggest_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/suggest_questions",
               content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
