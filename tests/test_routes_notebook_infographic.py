"""Infographic v2 routes: job start on POST, status endpoint, serving, report.

Fixture pattern copied from tests/test_routes_notebook_artifacts.py.
`generate_artifact` and the illustration-job functions are monkeypatched on
the route module; rows are real (file-backed temp sqlite).
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-infographic-routes")

import json
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import routes.notebook_routes as nbr
import src.notebook_illustrations as ill
from tests.helpers.sqlite_db import make_temp_sqlite

_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _v2_json(illustrations=None):
    d = {
        "title": "T", "takeaway": "one sentence",
        "blocks": [
            {"id": "hero", "type": "hero", "heading": "Hero", "text": "t", "illustration_prompt": "a hub"},
            {"id": "col", "type": "column", "heading": "Col", "subheading": "s", "children": [
                {"id": "c1", "type": "icon_card", "heading": "C1", "text": "t"},
                {"id": "c2", "type": "icon_card", "heading": "C2", "text": "t"},
            ]},
            {"id": "k1", "type": "icon_card", "heading": "K1", "text": "t"},
            {"id": "k2", "type": "icon_card", "heading": "K2", "text": "t"},
            {"id": "k3", "type": "icon_card", "heading": "K3", "text": "t"},
        ],
    }
    if illustrations:
        d["illustrations"] = illustrations
    return json.dumps(d)


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
    ill._active_jobs.clear()
    yield test_session_local
    ill._active_jobs.clear()
    tmpfile.close()


def _client(monkeypatch, user="ed"):
    monkeypatch.setattr(nbr, "get_current_user", lambda request: user)
    app = FastAPI()
    app.include_router(nbr.setup_notebook_routes(rag_manager=_FakeRagManager()))
    return TestClient(app, raise_server_exceptions=False)


def _rows(ts, content, owner="ed", kind="infographic"):
    s = ts()
    try:
        nb = db.Notebook(id=str(uuid.uuid4()), name="NB", owner=owner)
        s.add(nb)
        doc = db.Document(id=str(uuid.uuid4()), title="Doc", owner=owner,
                          language="markdown", current_content=content)
        s.add(doc)
        s.commit()
        art = db.NotebookArtifact(id=str(uuid.uuid4()), notebook_id=nb.id,
                                  document_id=doc.id, kind=kind, title="T")
        s.add(art)
        s.commit()
        return nb.id, art.id
    finally:
        s.close()


def _fake_generate(content):
    async def fake(notebook_id, owner, kind, db_session, focus=None, layout_instruction=None):
        document_id = str(uuid.uuid4())
        db_session.add(db.Document(id=document_id, title="Gen", owner=owner,
                                   language="markdown", current_content=content, session_id=None))
        artifact = db.NotebookArtifact(id=str(uuid.uuid4()), notebook_id=notebook_id,
                                       document_id=document_id, kind=kind, title="Gen")
        db_session.add(artifact)
        db_session.commit()
        db_session.refresh(artifact)
        return artifact
    return fake


# ---- POST /artifacts starts the job ---------------------------------------

def test_post_infographic_starts_job_when_image_gen_enabled(ts, monkeypatch):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate(_v2_json()))
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True if key == "image_gen_enabled" else default)
    started = []
    monkeypatch.setattr(nbr, "start_illustration_job",
                        lambda notebook_id, artifact_id, owner: started.append((notebook_id, artifact_id, owner)) or "job1")
    client = _client(monkeypatch)
    nb_id, _ = _rows(ts, "# whatever")
    r = client.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "infographic"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "infographic"
    assert started == [(nb_id, r.json()["id"], "ed")]


def test_post_infographic_skips_job_when_image_gen_disabled(ts, monkeypatch):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate(_v2_json()))
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(nbr, "start_illustration_job", lambda *a, **k: pytest.fail("must not start"))
    client = _client(monkeypatch)
    nb_id, _ = _rows(ts, "# whatever")
    r = client.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "infographic"})
    assert r.status_code == 200


def test_post_infographic_job_start_failure_does_not_fail_request(ts, monkeypatch):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate(_v2_json()))
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)

    def boom(*a, **k):
        raise ValueError("nope")
    monkeypatch.setattr(nbr, "start_illustration_job", boom)
    client = _client(monkeypatch)
    nb_id, _ = _rows(ts, "# whatever")
    r = client.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "infographic"})
    assert r.status_code == 200


def test_post_other_kind_never_starts_job(ts, monkeypatch):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate("# faq"))
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)
    monkeypatch.setattr(nbr, "start_illustration_job", lambda *a, **k: pytest.fail("must not start"))
    client = _client(monkeypatch)
    nb_id, _ = _rows(ts, "# whatever")
    assert client.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).status_code == 200


# ---- status endpoint --------------------------------------------------------

def test_status_none_when_image_gen_disabled(ts, monkeypatch):
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: default)
    client = _client(monkeypatch)
    nb_id, art_id = _rows(ts, _v2_json({"hero": f"{_UUID}-hero-0123abcd.png"}))
    r = client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/illustrations")
    assert r.status_code == 200
    assert r.json() == {"status": "none", "illustrations": {}}


def test_status_none_without_job_but_returns_stored_map(ts, monkeypatch):
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)
    client = _client(monkeypatch)
    nb_id, art_id = _rows(ts, _v2_json())
    # store a map on the doc for this artifact id
    s = ts()
    try:
        art = s.query(db.NotebookArtifact).get(art_id)
        doc = s.query(db.Document).get(art.document_id)
        doc.current_content = _v2_json({"hero": f"{art_id}-hero-0123abcd.png"})
        s.commit()
    finally:
        s.close()
    r = client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/illustrations")
    assert r.json() == {"status": "none",
                        "illustrations": {"hero": f"/api/notebook-illustration/{art_id}-hero-0123abcd.png"}}


def test_status_running_and_done_follow_job_registry(ts, monkeypatch):
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)
    client = _client(monkeypatch)
    nb_id, art_id = _rows(ts, _v2_json())
    ill._active_jobs["j"] = {"status": "running", "owner": "ed", "artifact_id": art_id,
                             "illustrations": {"hero": f"{art_id}-hero-0123abcd.png"}, "errors": 0,
                             "started_at": 1.0, "completed_at": None}
    r = client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/illustrations").json()
    assert r["status"] == "running"
    assert r["illustrations"] == {"hero": f"/api/notebook-illustration/{art_id}-hero-0123abcd.png"}
    ill._active_jobs["j"]["status"] = "done"
    assert client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/illustrations").json()["status"] == "done"


def test_status_404_for_foreign_notebook_or_unknown_artifact(ts, monkeypatch):
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)
    nb_id, art_id = _rows(ts, _v2_json(), owner="someone-else")
    client = _client(monkeypatch, user="ed")
    assert client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/illustrations").status_code == 404
    nb2, _ = _rows(ts, _v2_json(), owner="ed")
    assert client.get(f"/api/notebooks/{nb2}/artifacts/nope/illustrations").status_code == 404


# ---- report route -------------------------------------------------------------

def test_report_renders_v2_with_poll_url_while_running(ts, monkeypatch):
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)
    client = _client(monkeypatch)
    nb_id, art_id = _rows(ts, _v2_json())
    ill._active_jobs["j"] = {"status": "running", "owner": "ed", "artifact_id": art_id,
                             "illustrations": {}, "errors": 0, "started_at": 1.0, "completed_at": None}
    r = client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert 'class="ig2-wrap"' in r.text
    assert f'data-poll-url="/api/notebooks/{nb_id}/artifacts/{art_id}/illustrations"' in r.text


def test_report_renders_v2_without_poll_when_no_job(ts, monkeypatch):
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)
    client = _client(monkeypatch)
    nb_id, art_id = _rows(ts, _v2_json())
    r = client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert 'class="ig2-wrap"' in r.text
    assert "data-illustrations" not in r.text


def test_report_still_renders_legacy_markdown(ts, monkeypatch):
    client = _client(monkeypatch)
    nb_id, art_id = _rows(ts, "# Oud\n\n## Key numbers\n- **3** — x\n\n## S\n- f\n\n> t\n")
    r = client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert 'class="ig-grid"' in r.text


# ---- serving route --------------------------------------------------------------

def test_serve_illustration_owner_scoped(ts, monkeypatch, tmp_path):
    monkeypatch.setattr(ill, "NOTEBOOK_INFOGRAPHICS_DIR", str(tmp_path))
    nb_id, art_id = _rows(ts, _v2_json(), owner="ed")
    name = f"{art_id}-hero-0123abcd.png"
    (tmp_path / name).write_bytes(b"\x89PNG")
    r = _client(monkeypatch, user="ed").get(f"/api/notebook-illustration/{name}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert _client(monkeypatch, user="other").get(f"/api/notebook-illustration/{name}").status_code == 404


def test_serve_illustration_rejects_bad_names(ts, monkeypatch, tmp_path):
    monkeypatch.setattr(ill, "NOTEBOOK_INFOGRAPHICS_DIR", str(tmp_path))
    client = _client(monkeypatch)
    assert client.get("/api/notebook-illustration/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)
    assert client.get("/api/notebook-illustration/x.png").status_code == 400
    assert client.get(f"/api/notebook-illustration/{_UUID}-hero-0123abcd.png").status_code == 404
