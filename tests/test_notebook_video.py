"""Notebook video overview (Fase 4): slide-deck + narration -> mp4.

Mirrors tests/test_services_notebook_audio.py's audio-module block (job
store, script-retry, janitor) and tests/test_routes_notebook_audio.py /
tests/test_notebook_flashcards.py's route-fixture pattern (file-backed temp
sqlite via tests.helpers.sqlite_db, monkeypatched nbr.SessionLocal /
nbr.get_current_user).

Hermetic throughout — no LLM, no TTS, no ffmpeg is ever actually invoked:
`task_llm_call_async` and the injected synthesizer are fakes, `_run_ffmpeg`
is monkeypatched to a stand-in that just drops a file at the output path,
and all disk work happens under `tmp_path` with `NOTEBOOK_VIDEO_DIR`
pointed there for the duration of each test.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-video")

import asyncio
import io
import json
import re
import time
import uuid
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

import core.database as cdb
import routes.notebook_routes as nbr
import src.notebook_video as video
from src.notebook_language import DUTCH_OUTPUT_RULE
from src.notebook_slides import extract_slide_deck
from tests.helpers.sqlite_db import make_temp_sqlite

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


# ── VIDEO_PROMPT ─────────────────────────────────────────────────────────

def test_video_prompt_states_the_hard_requirements():
    prompt = video.VIDEO_PROMPT
    assert DUTCH_OUTPUT_RULE in prompt                    # always Dutch, not source language
    assert "Schrijf in de taal van de bronnen" not in prompt  # old source-language clause is gone
    assert "Nederlandse videotitel" in prompt       # schema title is Dutch, not source-language
    assert "{{" not in prompt                       # f-string brace-escaping didn't leak


# ── shared fixtures / helpers ────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_module_state():
    """`_synthesizer` and `_active_jobs` are process-global; never leak them
    across tests (same rationale as test_services_notebook_audio.py)."""
    previous_synthesizer = video.get_synthesizer()
    video._active_jobs.clear()
    yield
    video._active_jobs.clear()
    video.set_synthesizer(previous_synthesizer)


def _tiny_wav(n_frames=1):
    """A real, minimal WAV built with the stdlib: mono, 16-bit, 24000Hz."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * n_frames)
    return buf.getvalue()


def _age(path, seconds_ago=7200):
    """Backdate a file/dir's mtime (and atime) by `seconds_ago`."""
    stamp = time.time() - seconds_ago
    os.utime(path, (stamp, stamp))


def make_notebook(session, owner="own", name="Testboek"):
    nb = cdb.Notebook(id=str(uuid.uuid4()), owner=owner, name=name)
    session.add(nb)
    session.commit()
    return nb


def make_source(session, notebook, filename="a.txt", content="brontekst",
                status="indexed", owner="own"):
    doc = cdb.Document(id=str(uuid.uuid4()), title=filename, owner=owner,
                       current_content=content)
    session.add(doc)
    session.commit()
    src = cdb.NotebookSource(id=str(uuid.uuid4()), notebook_id=notebook.id,
                             document_id=doc.id, filename=filename,
                             status=status, chunk_count=1)
    session.add(src)
    session.commit()
    return src


def _seed_notebook(owner="own", name="Testboek", filename="a.txt",
                   content="brontekst", status="indexed", with_source=True):
    """Create a notebook (plus one source unless with_source=False); return
    the id (not the ORM object) — the session closes in the finally, and a
    detached Notebook cannot refresh its attributes."""
    session = _TS()
    try:
        notebook = make_notebook(session, owner=owner, name=name)
        if with_source:
            make_source(session, notebook, filename=filename, content=content,
                       status=status, owner=owner)
        return notebook.id
    finally:
        session.close()


def _script_json(title="Test Video", n_slides=2):
    """A valid ```json fence for extract_slide_deck(require_narration=True)."""
    slides = [
        {
            "title": f"Slide {i}",
            "bullets": [f"punt {i}"],
            "narration": f"Dit is de narratie voor slide {i}.",
        }
        for i in range(1, n_slides + 1)
    ]
    return "```json\n" + json.dumps({"title": title, "slides": slides}) + "\n```"


class _ScriptedLLM:
    """Async task_llm_call_async stand-in: returns scripts[i] per call,
    repeating the last entry once exhausted; records the last call's args."""

    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = 0
        self.messages = None
        self.kwargs = None

    async def __call__(self, messages, **kwargs):
        self.messages = messages
        self.kwargs = kwargs
        i = self.calls
        self.calls += 1
        return self.scripts[i] if i < len(self.scripts) else self.scripts[-1]


async def _fake_run_ffmpeg(cmd):
    """Stand-in for video._run_ffmpeg: drop a small file at the output path
    (the last argv element in both segment_command and concat_command)."""
    Path(cmd[-1]).write_bytes(b"fake-mp4-bytes")


def _prepare(monkeypatch, tmp_path, llm=None, synth=None, ffmpeg_ok=True):
    """Wire the module for a hermetic job run; returns (llm, synth)."""
    llm = llm if llm is not None else _ScriptedLLM([_script_json()])
    synth = synth if synth is not None else (lambda text, voice: _tiny_wav())
    monkeypatch.setattr(video, "task_llm_call_async", llm)
    monkeypatch.setattr(video, "fire_event", lambda *a, **k: None)
    monkeypatch.setattr(video, "NOTEBOOK_VIDEO_DIR", str(tmp_path))
    monkeypatch.setattr(video, "resolve_voices", lambda: ("voice-a", "voice-b"))
    monkeypatch.setattr(video, "_run_ffmpeg", _fake_run_ffmpeg)
    monkeypatch.setattr(video, "ffmpeg_available", lambda: ffmpeg_ok)
    video.set_synthesizer(synth)
    return llm, synth


# ==========================================================================
# 1. render_slide_png (Pillow)
# ==========================================================================

def test_render_slide_png_returns_valid_png_bytes():
    data = video.render_slide_png({"title": "Titel", "bullets": ["een", "twee"]}, 1, 3)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_slide_png_handles_empty_bullets_without_raising():
    data = video.render_slide_png({"title": "Titel", "bullets": []}, 1, 1)
    assert data[:4] == b"\x89PNG"


def test_render_slide_png_handles_very_long_title_without_raising():
    data = video.render_slide_png(
        {"title": "Een extreem lange titel die niet in één regel past " * 10,
         "bullets": ["punt"]},
        1, 1,
    )
    assert data[:4] == b"\x89PNG"


def test_render_slide_png_is_1280x720():
    from PIL import Image
    data = video.render_slide_png({"title": "T", "bullets": ["a", "b"]}, 2, 5)
    img = Image.open(io.BytesIO(data))
    assert img.size == (1280, 720)


# ==========================================================================
# 2. ffmpeg argv builders
# ==========================================================================

def test_segment_command_contains_paths_and_encode_flags():
    cmd = video.segment_command("slide.png", "audio.wav", "out.mp4")
    assert "slide.png" in cmd
    assert "audio.wav" in cmd
    assert cmd[-1] == "out.mp4"
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert "-shortest" in cmd


def test_concat_command_uses_concat_demuxer_and_stream_copy():
    cmd = video.concat_command("list.txt", "out.mp4")
    assert "list.txt" in cmd
    assert cmd[-1] == "out.mp4"
    assert cmd[cmd.index("-f") + 1] == "concat"
    assert cmd[cmd.index("-c") + 1] == "copy"


# ==========================================================================
# 3. resolve_notebook_video_path
# ==========================================================================

@pytest.mark.parametrize("bad", [
    "../x.mp4",
    "x.wav",
    "ABCDEF0123456789abcdef0123456789.mp4",  # uppercase hex not allowed
    "deadbeef.mp4",                           # too short
    "",
])
def test_resolve_notebook_video_path_rejects_bad_filenames(monkeypatch, tmp_path, bad):
    monkeypatch.setattr(video, "NOTEBOOK_VIDEO_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        video.resolve_notebook_video_path(bad)
    assert exc.value.status_code == 400


def test_resolve_notebook_video_path_404_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(video, "NOTEBOOK_VIDEO_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        video.resolve_notebook_video_path(uuid.uuid4().hex + ".mp4")
    assert exc.value.status_code == 404


def test_resolve_notebook_video_path_returns_existing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(video, "NOTEBOOK_VIDEO_DIR", str(tmp_path))
    name = uuid.uuid4().hex + ".mp4"
    (tmp_path / name).write_bytes(b"fake-mp4")
    assert video.resolve_notebook_video_path(name) == (tmp_path / name).resolve()


# ==========================================================================
# 4. extract_slide_deck(require_narration=True)  (src/notebook_slides.py)
# ==========================================================================

def test_extract_slide_deck_requires_narration_raises_when_field_missing():
    content = "```json\n" + json.dumps({
        "title": "T", "slides": [{"title": "S1", "bullets": ["a"]}]
    }) + "\n```"
    with pytest.raises(ValueError):
        extract_slide_deck(content, require_narration=True)


def test_extract_slide_deck_requires_narration_raises_when_field_empty():
    content = "```json\n" + json.dumps({
        "title": "T",
        "slides": [{"title": "S1", "bullets": ["a"], "narration": "   "}],
    }) + "\n```"
    with pytest.raises(ValueError):
        extract_slide_deck(content, require_narration=True)


def test_extract_slide_deck_accepts_present_narration():
    deck = extract_slide_deck(_script_json(n_slides=2), require_narration=True)
    assert deck["title"] == "Test Video"
    assert len(deck["slides"]) == 2
    assert all(s["narration"] for s in deck["slides"])


# ==========================================================================
# 5. Job store
# ==========================================================================

def test_get_job_unknown_id_returns_none():
    assert video.get_job("does-not-exist", "own") is None


def test_get_job_wrong_owner_returns_none():
    video._active_jobs["j1"] = {
        "owner": "own", "status": "done", "phase": "done", "segment": 1,
        "total": 1, "error": None, "artifact": None, "notebook_id": "nb",
        "started_at": time.time(), "script_attempt": 1,
    }
    try:
        assert video.get_job("j1", "iemand-anders") is None
    finally:
        video._active_jobs.pop("j1", None)


def test_public_job_fields_excludes_task_and_owner():
    assert "task" not in video._PUBLIC_JOB_FIELDS
    assert "owner" not in video._PUBLIC_JOB_FIELDS


# ==========================================================================
# 6. start_video_job: validation
# ==========================================================================

async def test_start_video_job_rejects_unknown_notebook(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="Notebook niet gevonden"):
        video.start_video_job("does-not-exist", "own", _TS)


async def test_start_video_job_rejects_notebook_without_sources(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    nb_id = _seed_notebook(with_source=False)
    with pytest.raises(ValueError, match="Geen geïndexeerde bronnen"):
        video.start_video_job(nb_id, "own", _TS)


async def test_start_video_job_rejects_when_no_synthesizer_configured(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    video.set_synthesizer(None)
    nb_id = _seed_notebook()
    with pytest.raises(RuntimeError, match="TTS niet geconfigureerd"):
        video.start_video_job(nb_id, "own", _TS)


async def test_start_video_job_rejects_when_ffmpeg_missing(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path, ffmpeg_ok=False)
    nb_id = _seed_notebook()
    with pytest.raises(RuntimeError, match="ffmpeg"):
        video.start_video_job(nb_id, "own", _TS)


# ==========================================================================
# 7. Full happy path: start_video_job -> _generate end to end
# ==========================================================================

async def test_full_happy_path_produces_artifact_document_and_mp4(monkeypatch, tmp_path):
    llm = _ScriptedLLM([_script_json(title="Mijn Video", n_slides=2)])
    _prepare(monkeypatch, tmp_path, llm=llm)
    nb_id = _seed_notebook(name="Testboek", content="brontekst over vogels")

    job_id = video.start_video_job(nb_id, "own", _TS)
    assert re.fullmatch(r"[a-f0-9]{32}", job_id)

    await video._active_jobs[job_id]["task"]

    job = video.get_job(job_id, "own")
    assert job["status"] == "done", job.get("error")
    assert job["phase"] == "done"

    mp4_files = [p for p in tmp_path.iterdir() if p.suffix == ".mp4"]
    assert len(mp4_files) == 1
    assert re.fullmatch(r"[a-f0-9]{32}\.mp4", mp4_files[0].name)

    s = _TS()
    try:
        art = s.query(cdb.NotebookArtifact).filter_by(notebook_id=nb_id).one()
        assert art.kind == "video"
        assert art.video_path == mp4_files[0].name
        doc = s.get(cdb.Document, art.document_id)
        assert doc is not None
        assert "Dit is de narratie voor slide 1." in doc.current_content
    finally:
        s.close()

    assert job["artifact"]["video_path"] == mp4_files[0].name


# ==========================================================================
# 8/9. Script-format retry
# ==========================================================================

async def test_script_retry_recovers_after_one_format_miss(monkeypatch, tmp_path):
    llm = _ScriptedLLM(["Sorry, ik kan dit niet in JSON gieten.", _script_json()])
    _prepare(monkeypatch, tmp_path, llm=llm)
    nb_id = _seed_notebook()

    job_id = video.start_video_job(nb_id, "own", _TS)
    await video._active_jobs[job_id]["task"]

    job = video.get_job(job_id, "own")
    assert job["status"] == "done", job.get("error")
    assert job["script_attempt"] == 2
    assert llm.calls == 2


async def test_script_fails_all_attempts_leaves_error_and_no_traces(monkeypatch, tmp_path):
    llm = _ScriptedLLM(["Sorry, ik kan dit niet in JSON gieten."])
    _prepare(monkeypatch, tmp_path, llm=llm)
    nb_id = _seed_notebook()

    job_id = video.start_video_job(nb_id, "own", _TS)
    await video._active_jobs[job_id]["task"]

    job = video.get_job(job_id, "own")
    assert job["status"] == "error"
    assert llm.calls == video._SCRIPT_FORMAT_ATTEMPTS == 3

    s = _TS()
    try:
        assert s.query(cdb.NotebookArtifact).filter_by(notebook_id=nb_id).count() == 0
    finally:
        s.close()
    # Script phase fails before any workdir/mp4 is ever created.
    assert list(tmp_path.iterdir()) == []


# ==========================================================================
# 10. cleanup_orphaned_video (janitor)
# ==========================================================================

def test_cleanup_orphaned_video_removes_stale_keeps_referenced_and_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(video, "NOTEBOOK_VIDEO_DIR", str(tmp_path))

    # (a) old .videojob-* workdir -> removed.
    stale_workdir = tmp_path / ".videojob-abc123"
    stale_workdir.mkdir()
    (stale_workdir / "slide001.png").write_bytes(b"x")
    _age(stale_workdir, 7200)

    # (b) old orphan mp4, no artifact row -> removed.
    orphan = tmp_path / (uuid.uuid4().hex + ".mp4")
    orphan.write_bytes(b"orphan-bytes")
    _age(orphan, 7200)

    # (c) old mp4 referenced by a NotebookArtifact row -> kept.
    referenced_name = uuid.uuid4().hex + ".mp4"
    s = _TS()
    try:
        nb = make_notebook(s)
        doc = cdb.Document(id=str(uuid.uuid4()), title="Video", owner="own",
                           current_content="script")
        s.add(doc)
        s.commit()
        art = cdb.NotebookArtifact(id=str(uuid.uuid4()), notebook_id=nb.id,
                                   document_id=doc.id, kind="video",
                                   video_path=referenced_name)
        s.add(art)
        s.commit()
    finally:
        s.close()
    referenced = tmp_path / referenced_name
    referenced.write_bytes(b"referenced-bytes")
    _age(referenced, 7200)

    # (d) fresh orphan mp4, no artifact row, but too young -> kept.
    fresh_orphan = tmp_path / (uuid.uuid4().hex + ".mp4")
    fresh_orphan.write_bytes(b"fresh-bytes")

    tmp_removed, orphans_removed = video.cleanup_orphaned_video(_TS, max_age_seconds=1)

    assert not stale_workdir.exists()
    assert not orphan.exists()
    assert referenced.exists()
    assert fresh_orphan.exists()
    assert tmp_removed == 1
    assert orphans_removed == 1


# ==========================================================================
# 11. Migration: notebook_artifacts.video_path column
# ==========================================================================

def test_migrate_add_notebook_artifact_video_path_column(tmp_path, monkeypatch):
    """Mirrors test_migrate_add_notebook_artifact_title_column in
    tests/test_services_notebook_artifacts.py — same tmp_path + raw sqlite3
    table_info convention."""
    import sqlite3

    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE notebook_artifacts (
            id TEXT PRIMARY KEY,
            notebook_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            audio_path VARCHAR,
            created_at DATETIME,
            updated_at DATETIME
        );
        INSERT INTO notebook_artifacts(id, notebook_id, document_id, kind)
        VALUES ('a1', 'n1', 'd1', 'video');
        """
    )
    conn.close()

    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{db_path}")

    conn = sqlite3.connect(db_path)
    try:
        columns_before = [row[1] for row in conn.execute("PRAGMA table_info(notebook_artifacts)")]
    finally:
        conn.close()
    assert "video_path" not in columns_before

    cdb._migrate_add_notebook_artifact_video_path_column()

    conn = sqlite3.connect(db_path)
    try:
        columns_after = [row[1] for row in conn.execute("PRAGMA table_info(notebook_artifacts)")]
        assert "video_path" in columns_after
        row = conn.execute("SELECT video_path FROM notebook_artifacts WHERE id = 'a1'").fetchone()
        assert row == (None,)
    finally:
        conn.close()

    # Idempotent: running it again on an already-migrated DB must not raise.
    cdb._migrate_add_notebook_artifact_video_path_column()


def test_migrate_add_notebook_artifact_video_path_column_missing_db_is_noop(tmp_path, monkeypatch):
    missing_path = tmp_path / "does-not-exist.db"
    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{missing_path}")

    # No DB file at all yet (fresh install) — must not raise.
    cdb._migrate_add_notebook_artifact_video_path_column()


# ==========================================================================
# 12. Routes (routes/notebook_routes.py video endpoints)
# ==========================================================================

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
    test_session_local, engine, tmpfile = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(nbr, "SessionLocal", test_session_local)
    yield test_session_local
    tmpfile.close()


def _client(monkeypatch, user="ed"):
    monkeypatch.setattr(nbr, "get_current_user", lambda request: user)
    app = FastAPI()
    app.include_router(nbr.setup_notebook_routes(rag_manager=_FakeRagManager()))
    return TestClient(app, raise_server_exceptions=False)


def _make_notebook_route(c, name="NB"):
    return c.post("/api/notebooks", json={"name": name}).json()["id"]


def _make_video_artifact(ts, nb_id, owner="ed", video_path=None):
    """Write a real Document + NotebookArtifact(kind="video") row."""
    video_path = video_path or (uuid.uuid4().hex + ".mp4")
    s = ts()
    try:
        document_id = str(uuid.uuid4())
        s.add(cdb.Document(
            id=document_id, title="Video", owner=owner,
            language="markdown", current_content="# script", session_id=None,
        ))
        artifact = cdb.NotebookArtifact(
            id=str(uuid.uuid4()), notebook_id=nb_id, document_id=document_id,
            kind="video", video_path=video_path,
        )
        s.add(artifact)
        s.commit()
        s.refresh(artifact)
        return artifact.to_dict()
    finally:
        s.close()


def test_route_create_video_tts_disabled_is_400(monkeypatch, ts):
    monkeypatch.setattr(nbr, "_current_tts_provider", lambda: "disabled")
    c = _client(monkeypatch)
    nb_id = _make_notebook_route(c)

    r = c.post(f"/api/notebooks/{nb_id}/video")
    assert r.status_code == 400
    assert r.json()["detail"] == "TTS is niet geconfigureerd (Settings → TTS)"


def test_route_get_video_status_unknown_job_is_404(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook_route(c)

    r = c.get(f"/api/notebooks/{nb_id}/video/does-not-exist")
    assert r.status_code == 404


def test_route_serve_video_cross_owner_is_404(monkeypatch, tmp_path, ts):
    monkeypatch.setattr(video, "NOTEBOOK_VIDEO_DIR", str(tmp_path))
    filename = uuid.uuid4().hex + ".mp4"
    (tmp_path / filename).write_bytes(b"fake-mp4-bytes")

    c_ed = _client(monkeypatch, user="ed")
    nb_id = _make_notebook_route(c_ed)
    _make_video_artifact(ts, nb_id, owner="ed", video_path=filename)

    c_eve = _client(monkeypatch, user="eve")
    r = c_eve.get(f"/api/notebook-video/{filename}")
    assert r.status_code == 404


def test_route_serve_video_owner_gets_file(monkeypatch, tmp_path, ts):
    monkeypatch.setattr(video, "NOTEBOOK_VIDEO_DIR", str(tmp_path))
    filename = uuid.uuid4().hex + ".mp4"
    content = b"fake-mp4-bytes-for-owner"
    (tmp_path / filename).write_bytes(content)

    c = _client(monkeypatch, user="ed")
    nb_id = _make_notebook_route(c)
    _make_video_artifact(ts, nb_id, owner="ed", video_path=filename)

    r = c.get(f"/api/notebook-video/{filename}")
    assert r.status_code == 200
    assert r.content == content
    assert r.headers["content-type"] == "video/mp4"
