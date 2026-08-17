"""Notebook podcast/audio routes: start, status, serving, delete-opruiming.

Pattern copied from tests/test_routes_notebook_artifacts.py — same
file-backed temp sqlite rationale. ``start_podcast_job`` / ``get_job`` are
monkeypatched at the route-module level (``routes.notebook_routes.*``) so
these tests never touch the real job runner / TTS chain; the serve-route
tests monkeypatch ``src.notebook_audio.NOTEBOOK_AUDIO_DIR`` (the module
``resolve_notebook_audio_path`` reads on every call) and write a real temp
file there.

``set_synthesizer`` is a process-global hook
(``src.notebook_audio._synthesizer``). Every test that touches it restores
it to None afterwards — the base commit this task builds on is literally
titled "testleak dicht" for exactly this class of cross-test leak.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-audio-routes")

import uuid

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import routes.notebook_routes as nbr
import src.notebook_audio as notebook_audio
from tests.helpers.sqlite_db import make_temp_sqlite


class _FakeRagManager:
    def __init__(self):
        self.removed = []
        self.vector_rag = self

    def add_document(self, text, metadata):
        return True

    def remove_notebook(self, notebook_id, document_id=None):
        self.removed.append((notebook_id, document_id))

    def _split_into_chunks(self, text):
        return [text[i:i + 1000] for i in range(0, len(text), 800)]


@pytest.fixture()
def ts(monkeypatch):
    """A shared, file-backed temp sqlite SessionLocal all clients in a test use."""
    test_session_local, engine, tmpfile = make_temp_sqlite(db.Base.metadata)
    monkeypatch.setattr(nbr, "SessionLocal", test_session_local)
    yield test_session_local
    tmpfile.close()


@pytest.fixture(autouse=True)
def _reset_synthesizer():
    """set_synthesizer() is process-global state; never leak it across tests."""
    notebook_audio.set_synthesizer(None)
    yield
    notebook_audio.set_synthesizer(None)


def _client(monkeypatch, user="ed", tts_service=None):
    monkeypatch.setattr(nbr, "get_current_user", lambda request: user)
    app = FastAPI()
    app.include_router(nbr.setup_notebook_routes(rag_manager=_FakeRagManager(), tts_service=tts_service))
    return TestClient(app, raise_server_exceptions=False)


def _make_notebook(c, name="NB"):
    return c.post("/api/notebooks", json={"name": name}).json()["id"]


def _add_source(c, nb_id):
    files = [("files", ("good.txt", b"plain text content " * 50, "text/plain"))]
    return c.post(f"/api/notebooks/{nb_id}/sources", files=files).json()["sources"][0]


def _make_podcast_artifact(ts, nb_id, owner="ed", audio_path=None):
    """Write a real Document + NotebookArtifact(kind="podcast") row."""
    audio_path = audio_path or (uuid.uuid4().hex + ".wav")
    s = ts()
    try:
        document_id = str(uuid.uuid4())
        s.add(db.Document(
            id=document_id, title="Podcast", owner=owner,
            language="markdown", current_content="# S1: hoi", session_id=None,
        ))
        artifact = db.NotebookArtifact(
            id=str(uuid.uuid4()), notebook_id=nb_id, document_id=document_id,
            kind="podcast", audio_path=audio_path,
        )
        s.add(artifact)
        s.commit()
        s.refresh(artifact)
        return artifact.to_dict()
    finally:
        s.close()


def _fake_get_synthesizer_not_none():
    return lambda text, voice: b"fake-wav-bytes"


# ---- POST /api/notebooks/{id}/podcast — validation order ----

def test_create_podcast_cross_owner_is_404(monkeypatch, ts):
    c_ed = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c_ed)
    c_eve = _client(monkeypatch, user="eve")

    r = c_eve.post(f"/api/notebooks/{nb_id}/podcast")
    assert r.status_code == 404


def test_create_podcast_no_sources_is_400(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/podcast")
    assert r.status_code == 400
    assert r.json()["detail"] == "Geen geïndexeerde bronnen"


def test_create_podcast_tts_not_configured_is_400_before_job_start(monkeypatch, ts):
    """Bronnen aanwezig, maar TTS niet geconfigureerd -> 400 met de exacte
    spec-tekst, en start_podcast_job wordt niet aangeroepen (validatievolgorde)."""
    called = []
    monkeypatch.setattr(nbr, "start_podcast_job", lambda *a, **k: called.append(1) or "job-x")
    monkeypatch.setattr(nbr, "_current_tts_provider", lambda: "disabled")
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    _add_source(c, nb_id)

    r = c.post(f"/api/notebooks/{nb_id}/podcast")
    assert r.status_code == 400
    assert r.json()["detail"] == "TTS is niet geconfigureerd (Settings → TTS)"
    assert called == []


def test_create_podcast_bronnen_checked_before_tts(monkeypatch, ts):
    """No sources AND TTS disabled -> the bronnen-400 wins (validatievolgorde)."""
    monkeypatch.setattr(nbr, "_current_tts_provider", lambda: "disabled")
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/podcast")
    assert r.status_code == 400
    assert r.json()["detail"] == "Geen geïndexeerde bronnen"


def test_create_podcast_starts_job_returns_running(monkeypatch, ts):
    monkeypatch.setattr(nbr, "_current_tts_provider", lambda: "endpoint:x")
    monkeypatch.setattr(nbr, "start_podcast_job", lambda notebook_id, owner: "job-123")
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    _add_source(c, nb_id)

    r = c.post(f"/api/notebooks/{nb_id}/podcast")
    assert r.status_code == 200
    assert r.json() == {"job_id": "job-123", "status": "running"}


def test_create_podcast_valueerror_from_job_start_is_400(monkeypatch, ts):
    def _raise(*a, **k):
        raise ValueError("Geen geïndexeerde bronnen")
    monkeypatch.setattr(nbr, "_current_tts_provider", lambda: "endpoint:x")
    monkeypatch.setattr(nbr, "start_podcast_job", _raise)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    _add_source(c, nb_id)

    r = c.post(f"/api/notebooks/{nb_id}/podcast")
    assert r.status_code == 400


def test_create_podcast_runtimeerror_from_job_start_is_400(monkeypatch, ts):
    def _raise(*a, **k):
        raise RuntimeError("TTS niet geconfigureerd")
    monkeypatch.setattr(nbr, "_current_tts_provider", lambda: "endpoint:x")
    monkeypatch.setattr(nbr, "start_podcast_job", _raise)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    _add_source(c, nb_id)

    r = c.post(f"/api/notebooks/{nb_id}/podcast")
    assert r.status_code == 400
    assert r.json()["detail"] == "TTS niet geconfigureerd"


# ---- setup-time synthesizer wiring ----

def test_setup_wires_synthesizer_when_provider_enabled(monkeypatch, ts):
    class _FakeTTS:
        def synthesize_voice(self, text, voice):
            return b"x"
    monkeypatch.setattr(nbr, "_current_tts_provider", lambda: "endpoint:x")
    _client(monkeypatch, tts_service=_FakeTTS())
    assert notebook_audio.get_synthesizer() is not None


def test_setup_leaves_synthesizer_unset_when_provider_disabled(monkeypatch, ts):
    class _FakeTTS:
        def synthesize_voice(self, text, voice):
            return b"x"
    monkeypatch.setattr(nbr, "_current_tts_provider", lambda: "disabled")
    _client(monkeypatch, tts_service=_FakeTTS())
    assert notebook_audio.get_synthesizer() is None


# ---- GET /api/notebooks/{id}/podcast/{job_id} ----

def test_get_podcast_status_unknown_job_is_404(monkeypatch, ts):
    monkeypatch.setattr(nbr, "get_job", lambda job_id, owner: None)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.get(f"/api/notebooks/{nb_id}/podcast/does-not-exist")
    assert r.status_code == 404


def test_get_podcast_status_cross_owner_notebook_is_404(monkeypatch, ts):
    monkeypatch.setattr(nbr, "get_job", lambda job_id, owner: {"status": "running"})
    c_ed = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c_ed)
    c_eve = _client(monkeypatch, user="eve")

    r = c_eve.get(f"/api/notebooks/{nb_id}/podcast/job-1")
    assert r.status_code == 404


def test_get_podcast_status_running_passthrough(monkeypatch, ts):
    monkeypatch.setattr(nbr, "get_job", lambda job_id, owner: {
        "status": "running", "phase": "tts", "segment": 3, "total": 10,
        "error": None, "artifact": None,
    })
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.get(f"/api/notebooks/{nb_id}/podcast/job-1")
    assert r.status_code == 200
    assert r.json() == {
        "status": "running", "phase": "tts", "segment": 3, "total": 10,
        "error": None, "artifact": None,
    }


def test_get_podcast_status_done_includes_artifact(monkeypatch, ts):
    artifact = {"id": "a1", "kind": "podcast", "audio_path": "x.wav"}
    monkeypatch.setattr(nbr, "get_job", lambda job_id, owner: {
        "status": "done", "phase": "done", "segment": 10, "total": 10,
        "error": None, "artifact": artifact,
    })
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.get(f"/api/notebooks/{nb_id}/podcast/job-1")
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    assert r.json()["artifact"] == artifact


def test_get_podcast_status_cancelled_is_mapped_to_error(monkeypatch, ts):
    monkeypatch.setattr(nbr, "get_job", lambda job_id, owner: {
        "status": "cancelled", "phase": "tts", "segment": 2, "total": 10,
        "error": "Generatie afgebroken", "artifact": None,
    })
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.get(f"/api/notebooks/{nb_id}/podcast/job-1")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert body["error"] == "Generatie afgebroken"


def test_get_podcast_status_cancelled_without_error_gets_default_message(monkeypatch, ts):
    monkeypatch.setattr(nbr, "get_job", lambda job_id, owner: {
        "status": "cancelled", "phase": "tts", "segment": 2, "total": 10,
        "error": None, "artifact": None,
    })
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.get(f"/api/notebooks/{nb_id}/podcast/job-1")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert body["error"] == "Generatie afgebroken"


# ---- GET /api/notebook-audio/{filename} ----

def test_serve_audio_invalid_filename_is_400(monkeypatch, ts):
    c = _client(monkeypatch)
    r = c.get("/api/notebook-audio/../../etc/passwd")
    assert r.status_code in (400, 404)  # path traversal never resolves to 200


def test_serve_audio_wrong_pattern_is_400(monkeypatch, ts):
    c = _client(monkeypatch)
    r = c.get("/api/notebook-audio/not-a-valid-name.wav")
    assert r.status_code == 400


def test_serve_audio_unknown_file_is_404(monkeypatch, tmp_path, ts):
    monkeypatch.setattr(notebook_audio, "NOTEBOOK_AUDIO_DIR", str(tmp_path))
    c = _client(monkeypatch)
    filename = uuid.uuid4().hex + ".wav"
    r = c.get(f"/api/notebook-audio/{filename}")
    assert r.status_code == 404


def test_serve_audio_no_artifact_row_is_404(monkeypatch, tmp_path, ts):
    """File exists on disk but no NotebookArtifact references it: 404, don't
    confirm its existence to anyone (matches src/notebook_audio.py's
    resolve_notebook_audio_path/ownership split)."""
    monkeypatch.setattr(notebook_audio, "NOTEBOOK_AUDIO_DIR", str(tmp_path))
    filename = uuid.uuid4().hex + ".wav"
    (tmp_path / filename).write_bytes(b"RIFF....WAVEfmt ")

    c = _client(monkeypatch)
    r = c.get(f"/api/notebook-audio/{filename}")
    assert r.status_code == 404


def test_serve_audio_cross_owner_is_404(monkeypatch, tmp_path, ts):
    monkeypatch.setattr(notebook_audio, "NOTEBOOK_AUDIO_DIR", str(tmp_path))
    filename = uuid.uuid4().hex + ".wav"
    (tmp_path / filename).write_bytes(b"RIFF....WAVEfmt ")

    c_ed = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c_ed)
    _make_podcast_artifact(ts, nb_id, owner="ed", audio_path=filename)

    c_eve = _client(monkeypatch, user="eve")
    r = c_eve.get(f"/api/notebook-audio/{filename}")
    assert r.status_code == 404


def test_serve_audio_owner_gets_file(monkeypatch, tmp_path, ts):
    monkeypatch.setattr(notebook_audio, "NOTEBOOK_AUDIO_DIR", str(tmp_path))
    filename = uuid.uuid4().hex + ".wav"
    content = b"RIFF....WAVEfmt some-fake-wav-bytes"
    (tmp_path / filename).write_bytes(content)

    c = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c)
    _make_podcast_artifact(ts, nb_id, owner="ed", audio_path=filename)

    r = c.get(f"/api/notebook-audio/{filename}")
    assert r.status_code == 200
    assert r.content == content
    assert r.headers["content-type"] == "audio/wav"
    assert r.headers["cache-control"] == notebook_audio.NOTEBOOK_AUDIO_HEADERS["Cache-Control"]


# ---- Delete-opruiming ----

def test_delete_artifact_unlinks_audio_file(monkeypatch, tmp_path, ts):
    monkeypatch.setattr(notebook_audio, "NOTEBOOK_AUDIO_DIR", str(tmp_path))
    filename = uuid.uuid4().hex + ".wav"
    (tmp_path / filename).write_bytes(b"fake")

    c = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c)
    artifact = _make_podcast_artifact(ts, nb_id, owner="ed", audio_path=filename)
    assert (tmp_path / filename).exists()

    r = c.delete(f"/api/notebooks/{nb_id}/artifacts/{artifact['id']}")
    assert r.status_code == 200
    assert not (tmp_path / filename).exists()


def test_delete_artifact_missing_audio_file_does_not_500(monkeypatch, tmp_path, ts):
    """audio_path points at a file that's already gone: unlink is best-effort."""
    monkeypatch.setattr(notebook_audio, "NOTEBOOK_AUDIO_DIR", str(tmp_path))
    filename = uuid.uuid4().hex + ".wav"

    c = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c)
    artifact = _make_podcast_artifact(ts, nb_id, owner="ed", audio_path=filename)

    r = c.delete(f"/api/notebooks/{nb_id}/artifacts/{artifact['id']}")
    assert r.status_code == 200


def test_delete_artifact_text_kind_has_no_audio_to_unlink(monkeypatch, ts):
    """Regression guard: deleting a text artifact (audio_path=None) must not
    touch the filesystem-unlink path at all."""
    c = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c)
    s = ts()
    try:
        document_id = str(uuid.uuid4())
        s.add(db.Document(
            id=document_id, title="FAQ", owner="ed",
            language="markdown", current_content="# faq", session_id=None,
        ))
        artifact = db.NotebookArtifact(
            id=str(uuid.uuid4()), notebook_id=nb_id, document_id=document_id, kind="faq",
        )
        s.add(artifact)
        s.commit()
        artifact_id = artifact.id
    finally:
        s.close()

    r = c.delete(f"/api/notebooks/{nb_id}/artifacts/{artifact_id}")
    assert r.status_code == 200


def test_notebook_delete_unlinks_podcast_audio(monkeypatch, tmp_path, ts):
    monkeypatch.setattr(notebook_audio, "NOTEBOOK_AUDIO_DIR", str(tmp_path))
    filename = uuid.uuid4().hex + ".wav"
    (tmp_path / filename).write_bytes(b"fake")

    c = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c)
    _make_podcast_artifact(ts, nb_id, owner="ed", audio_path=filename)
    assert (tmp_path / filename).exists()

    r = c.delete(f"/api/notebooks/{nb_id}")
    assert r.status_code == 200
    assert not (tmp_path / filename).exists()


# ---- GET-artifacts: audio_path comes through to_dict ----

def test_list_artifacts_includes_audio_path(monkeypatch, ts):
    c = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c)
    _make_podcast_artifact(ts, nb_id, owner="ed", audio_path="abc.wav")

    r = c.get(f"/api/notebooks/{nb_id}/artifacts")
    assert r.status_code == 200
    artifacts = r.json()["artifacts"]
    assert artifacts[0]["audio_path"] == "abc.wav"
    assert artifacts[0]["kind"] == "podcast"
