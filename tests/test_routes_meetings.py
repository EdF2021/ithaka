"""Meeting-recorder REST routes: create/chunks/finish/list/detail/audio/delete.

Pattern copied from tests/test_routes_notebook_audio.py — file-backed temp
sqlite via tests/helpers/sqlite_db.make_temp_sqlite, FastAPI() +
include_router, TestClient. Unlike notebook_routes, setup_meeting_routes
takes get_current_user and SessionLocal as explicit arguments (task-4
brief), so auth/db are wired per-client instead of module-patched;
start_processing_job / get_job_for_meeting / MEETING_AUDIO_DIR are still
monkeypatched at the routes.meeting_routes module level since those aren't
constructor arguments.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-meeting-routes")

import asyncio
import threading
import time

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import routes.meeting_routes as mr
import src.meeting_minutes as meeting_minutes
from tests.helpers.sqlite_db import make_temp_sqlite


@pytest.fixture()
def ts():
    """A shared, file-backed temp sqlite SessionLocal all clients in a test use."""
    test_session_local, engine, tmpfile = make_temp_sqlite(db.Base.metadata)
    yield test_session_local
    tmpfile.close()


@pytest.fixture(autouse=True)
def _audio_dir(tmp_path, monkeypatch):
    """Point both the chunks-writer (routes.meeting_routes.MEETING_AUDIO_DIR)
    and resolve_meeting_audio_path's own module attribute
    (src.meeting_minutes.MEETING_AUDIO_DIR) at the same temp dir, so a chunk
    uploaded through the route can be read back through the audio-serve
    route."""
    monkeypatch.setattr(mr, "MEETING_AUDIO_DIR", str(tmp_path))
    monkeypatch.setattr(meeting_minutes, "MEETING_AUDIO_DIR", str(tmp_path))
    return tmp_path


def _client(ts, user="ed"):
    app = FastAPI()
    app.include_router(mr.setup_meeting_routes(lambda request: user, ts))
    return TestClient(app, raise_server_exceptions=False)


def _create(c, title="Sprint review", agenda=None, key_terms=None):
    body = {"title": title}
    if agenda is not None:
        body["agenda"] = agenda
    if key_terms is not None:
        body["key_terms"] = key_terms
    return c.post("/api/meetings", json=body)


# ---- POST /api/meetings ----

def test_create_meeting_returns_serialized_row(ts):
    c = _client(ts)
    r = _create(c)
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Sprint review"
    assert body["status"] == "recording"
    assert body["bytes_total"] == 0
    assert body["document_id"] is None
    assert body["finished_at"] is None
    assert body["created_at"] is not None
    assert body["segment"] is None and body["total"] is None and body["depth"] is None


def test_create_meeting_empty_title_is_400(ts):
    c = _client(ts)
    r = _create(c, title="   ")
    assert r.status_code == 400
    assert r.json()["detail"] == "Titel is verplicht"


def test_create_meeting_missing_title_is_400(ts):
    c = _client(ts)
    r = c.post("/api/meetings", json={})
    assert r.status_code == 400
    assert r.json()["detail"] == "Titel is verplicht"


def test_create_meeting_title_too_long_is_400(ts):
    c = _client(ts)
    r = _create(c, title="x" * 201)
    assert r.status_code == 400
    assert r.json()["detail"] == "Titel te lang"


def test_create_meeting_agenda_too_long_is_413(ts):
    c = _client(ts)
    r = _create(c, agenda="x" * 20001)
    assert r.status_code == 413
    assert r.json()["detail"] == "Tekst te lang"


def test_create_meeting_key_terms_too_long_is_413(ts):
    c = _client(ts)
    r = _create(c, key_terms="x" * 20001)
    assert r.status_code == 413
    assert r.json()["detail"] == "Tekst te lang"


# ---- POST /api/meetings/{id}/chunks ----

def test_chunks_in_order_grow_bytes_total_and_concatenate(ts, tmp_path):
    c = _client(ts)
    meeting_id = _create(c).json()["id"]

    r1 = c.post(
        f"/api/meetings/{meeting_id}/chunks?seq=0",
        files={"file": ("c0.webm", b"AAAA", "audio/webm")},
    )
    assert r1.status_code == 200
    assert r1.json() == {"seq": 0, "bytes_total": 4}

    r2 = c.post(
        f"/api/meetings/{meeting_id}/chunks?seq=1",
        files={"file": ("c1.webm", b"BBB", "audio/webm")},
    )
    assert r2.status_code == 200
    assert r2.json() == {"seq": 1, "bytes_total": 7}

    detail = c.get(f"/api/meetings/{meeting_id}").json()
    assert detail["bytes_total"] == 7

    # File content is the raw concatenation of the two chunks, in order.
    on_disk = (tmp_path / (detail["id"] + ".webm")).read_bytes()
    assert on_disk == b"AAAABBB"


def test_chunk_toctou_lock_prevents_double_append(ts, tmp_path, monkeypatch):
    # Fix-wave-2 item 3 (final-review.md [I]): a retried chunk (client
    # fetch rejects, _pump retries the same seq while the original request
    # is still parked mid-read) must not be appended twice. Simulate the
    # race by parking the FIRST request inside read_upload_limited (holding
    # the per-meeting lock) with a threading.Event, firing a second request
    # for the same seq from another thread while the first is parked, then
    # releasing — the lock must serialize them so exactly one gets 200 and
    # one gets 409, and the file holds the chunk only once.
    c = _client(ts)
    meeting_id = _create(c).json()["id"]

    parked = threading.Event()
    release = threading.Event()
    real_read = mr.read_upload_limited
    call_count = {"n": 0}

    async def _blocking_read(upload, limit, label="Upload"):
        call_count["n"] += 1
        if call_count["n"] == 1:
            parked.set()
            await asyncio.get_event_loop().run_in_executor(None, release.wait)
        return await real_read(upload, limit, label)

    monkeypatch.setattr(mr, "read_upload_limited", _blocking_read)

    results = {}

    def _post_first():
        results["first"] = c.post(
            f"/api/meetings/{meeting_id}/chunks?seq=0",
            files={"file": ("a.webm", b"AAAA", "audio/webm")},
        )

    def _post_second():
        results["second"] = c.post(
            f"/api/meetings/{meeting_id}/chunks?seq=0",
            files={"file": ("b.webm", b"BBBB", "audio/webm")},
        )

    with c:
        t1 = threading.Thread(target=_post_first)
        t1.start()
        assert parked.wait(timeout=5), "first request never parked inside read_upload_limited"

        t2 = threading.Thread(target=_post_second)
        t2.start()
        # Give the second request a moment to actually reach and block on
        # the per-meeting lock before releasing the first.
        time.sleep(0.2)
        release.set()

        t1.join(timeout=5)
        t2.join(timeout=5)

    statuses = sorted([results["first"].status_code, results["second"].status_code])
    assert statuses == [200, 409]

    detail = c.get(f"/api/meetings/{meeting_id}").json()
    assert detail["bytes_total"] == 4  # exactly one 4-byte chunk, not 8

    on_disk = (tmp_path / (meeting_id + ".webm")).read_bytes()
    assert on_disk == b"AAAA"


def test_chunk_seq_skip_is_409_with_expected(ts):
    c = _client(ts)
    meeting_id = _create(c).json()["id"]

    r = c.post(
        f"/api/meetings/{meeting_id}/chunks?seq=5",
        files={"file": ("c.webm", b"AAAA", "audio/webm")},
    )
    assert r.status_code == 409
    assert r.json() == {"detail": "Onverwacht chunknummer", "expected": 0}


def test_chunk_over_per_chunk_limit_is_413(ts, monkeypatch):
    monkeypatch.setattr(mr, "MEETING_CHUNK_MAX_BYTES", 10)
    c = _client(ts)
    meeting_id = _create(c).json()["id"]

    r = c.post(
        f"/api/meetings/{meeting_id}/chunks?seq=0",
        files={"file": ("c.webm", b"x" * 20, "audio/webm")},
    )
    assert r.status_code == 413


def test_chunk_over_total_limit_is_413(ts, monkeypatch):
    monkeypatch.setattr(mr, "MEETING_AUDIO_MAX_BYTES", 5)
    c = _client(ts)
    meeting_id = _create(c).json()["id"]

    r = c.post(
        f"/api/meetings/{meeting_id}/chunks?seq=0",
        files={"file": ("c.webm", b"x" * 10, "audio/webm")},
    )
    assert r.status_code == 413
    assert r.json()["detail"] == "Opname te groot"


def test_chunk_cross_owner_is_404(ts):
    c_ed = _client(ts, user="ed")
    meeting_id = _create(c_ed).json()["id"]
    c_bob = _client(ts, user="bob")

    r = c_bob.post(
        f"/api/meetings/{meeting_id}/chunks?seq=0",
        files={"file": ("c.webm", b"AAAA", "audio/webm")},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "Vergadering niet gevonden"


def test_chunk_empty_body_is_400_leeg_audiofragment(ts):
    # Fix-wave-2 item 8: read_upload_limited succeeding with zero bytes must
    # be rejected with the route's own "Leeg audiofragment" detail (the
    # empty-file case final-review.md flagged as implemented but untested).
    c = _client(ts)
    meeting_id = _create(c).json()["id"]

    r = c.post(
        f"/api/meetings/{meeting_id}/chunks?seq=0",
        files={"file": ("c.webm", b"", "audio/webm")},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "Leeg audiofragment"


def test_chunk_after_finish_is_400(ts):
    """Once the row is no longer "recording" (start_processing_job's real
    side effect on finish), a further chunk upload is rejected. The route
    itself only reads row.status - simulate the post-finish state directly
    rather than depending on the (separately monkeypatched, in other
    tests) start_processing_job's DB write."""
    c = _client(ts)
    meeting_id = _create(c).json()["id"]
    c.post(f"/api/meetings/{meeting_id}/chunks?seq=0", files={"file": ("c.webm", b"AAAA", "audio/webm")})

    s = ts()
    try:
        row = s.query(db.Meeting).filter(db.Meeting.id == meeting_id).first()
        row.status = "processing"
        s.commit()
    finally:
        s.close()

    r = c.post(
        f"/api/meetings/{meeting_id}/chunks?seq=1",
        files={"file": ("c.webm", b"AAAA", "audio/webm")},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "Opname is al afgesloten"


# ---- POST /api/meetings/{id}/finish ----

def test_finish_without_audio_is_400_geen_audio_ontvangen(ts):
    """Real start_processing_job path (not monkeypatched): a freshly
    created meeting has an audio_path but no chunks were ever uploaded, so
    no file exists on disk -> ValueError("Geen audio ontvangen") from
    src.meeting_minutes.start_processing_job, mapped to 400 by the route."""
    c = _client(ts)
    meeting_id = _create(c).json()["id"]

    r = c.post(f"/api/meetings/{meeting_id}/finish", json={})
    assert r.status_code == 400
    assert r.json()["detail"] == "Geen audio ontvangen"


def test_finish_happy_path_returns_processing(ts, monkeypatch):
    monkeypatch.setattr(mr, "start_processing_job", lambda *a, **k: "job-42")
    c = _client(ts)
    meeting_id = _create(c).json()["id"]

    r = c.post(f"/api/meetings/{meeting_id}/finish", json={"duration_seconds": 120})
    assert r.status_code == 200
    assert r.json() == {"job_id": "job-42", "status": "processing"}

    detail = c.get(f"/api/meetings/{meeting_id}").json()
    assert detail["duration_seconds"] == 120


def test_finish_valueerror_from_job_start_is_400(ts, monkeypatch):
    def _raise(*a, **k):
        raise ValueError("Verwerking loopt al")
    monkeypatch.setattr(mr, "start_processing_job", _raise)
    c = _client(ts)
    meeting_id = _create(c).json()["id"]

    r = c.post(f"/api/meetings/{meeting_id}/finish", json={})
    assert r.status_code == 400
    assert r.json()["detail"] == "Verwerking loopt al"


def test_finish_runtimeerror_from_job_start_is_400(ts, monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("STT niet geconfigureerd")
    monkeypatch.setattr(mr, "start_processing_job", _raise)
    c = _client(ts)
    meeting_id = _create(c).json()["id"]

    r = c.post(f"/api/meetings/{meeting_id}/finish", json={})
    assert r.status_code == 400
    assert r.json()["detail"] == "STT niet geconfigureerd"


def test_finish_cross_owner_is_404(ts):
    c_ed = _client(ts, user="ed")
    meeting_id = _create(c_ed).json()["id"]
    c_bob = _client(ts, user="bob")

    r = c_bob.post(f"/api/meetings/{meeting_id}/finish", json={})
    assert r.status_code == 404


def test_finish_while_job_actually_running_is_400(ts, monkeypatch):
    """status="processing" AND a live job (get_job_for_meeting returns a
    running entry) -> blocked before start_processing_job is even called,
    to avoid writing duration_seconds into a row mid-job."""
    called = []
    monkeypatch.setattr(mr, "start_processing_job", lambda *a, **k: called.append(1) or "job-1")
    monkeypatch.setattr(
        mr, "get_job_for_meeting",
        lambda mid, user: {"status": "running", "meeting_id": mid},
    )
    c = _client(ts)
    meeting_id = _create(c).json()["id"]
    s = ts()
    try:
        row = s.query(db.Meeting).filter(db.Meeting.id == meeting_id).first()
        row.status = "processing"
        s.commit()
    finally:
        s.close()

    r = c.post(f"/api/meetings/{meeting_id}/finish", json={})
    assert r.status_code == 400
    assert r.json()["detail"] == "Verwerking loopt al"
    assert called == []


def test_finish_while_processing_with_no_live_job_reprocesses(ts, monkeypatch):
    """status="processing" but NO live job (server restarted mid-job) is
    exactly the state the detail route presents as an error telling the
    user to "gebruik Reprocess" - and Reprocess POSTs right back to this
    route. It must not dead-end on the same "Verwerking loopt al" 400;
    finish must fall through to (a real or, here, patched)
    start_processing_job and succeed."""
    monkeypatch.setattr(mr, "start_processing_job", lambda *a, **k: "job-2")
    monkeypatch.setattr(mr, "get_job_for_meeting", lambda mid, user: None)
    c = _client(ts)
    meeting_id = _create(c).json()["id"]
    s = ts()
    try:
        row = s.query(db.Meeting).filter(db.Meeting.id == meeting_id).first()
        row.status = "processing"
        s.commit()
    finally:
        s.close()

    r = c.post(f"/api/meetings/{meeting_id}/finish", json={})
    assert r.status_code == 200
    assert r.json() == {"job_id": "job-2", "status": "processing"}


# ---- GET /api/meetings, GET /api/meetings/{id} ----

def test_list_and_detail_shapes(ts):
    c = _client(ts)
    meeting_id = _create(c, title="Weekly").json()["id"]

    listing = c.get("/api/meetings").json()
    assert len(listing["meetings"]) == 1
    assert listing["meetings"][0]["id"] == meeting_id
    assert listing["meetings"][0]["title"] == "Weekly"

    detail = c.get(f"/api/meetings/{meeting_id}").json()
    assert detail["id"] == meeting_id
    assert set(detail.keys()) == {
        "id", "title", "agenda", "key_terms", "status", "phase", "error",
        "bytes_total", "duration_seconds", "document_id", "created_at",
        "finished_at", "segment", "total", "depth",
    }


def test_detail_unknown_id_is_404(ts):
    c = _client(ts)
    r = c.get("/api/meetings/does-not-exist")
    assert r.status_code == 404


def test_detail_cross_owner_is_404(ts):
    c_ed = _client(ts, user="ed")
    meeting_id = _create(c_ed).json()["id"]
    c_bob = _client(ts, user="bob")

    r = c_bob.get(f"/api/meetings/{meeting_id}")
    assert r.status_code == 404


def test_list_orders_newest_first(ts):
    # Fix-wave-2 item 8: test_list_and_detail_shapes only ever asserted a
    # single row, so order_by(created_at.desc()) was unverified. Set
    # created_at explicitly (both rows are created in the same test process
    # tick, so relying on wall-clock ordering alone would be flaky).
    import datetime

    c = _client(ts)
    older_id = _create(c, title="Older").json()["id"]
    newer_id = _create(c, title="Newer").json()["id"]

    s = ts()
    try:
        older = s.query(db.Meeting).filter(db.Meeting.id == older_id).first()
        newer = s.query(db.Meeting).filter(db.Meeting.id == newer_id).first()
        older.created_at = datetime.datetime(2020, 1, 1)
        newer.created_at = datetime.datetime(2020, 1, 2)
        s.commit()
    finally:
        s.close()

    listing = c.get("/api/meetings").json()["meetings"]
    assert [m["id"] for m in listing] == [newer_id, older_id]


def test_list_only_returns_own_meetings(ts):
    c_ed = _client(ts, user="ed")
    _create(c_ed, title="Ed's")
    c_bob = _client(ts, user="bob")
    _create(c_bob, title="Bob's")

    listing = c_ed.get("/api/meetings").json()
    assert [m["title"] for m in listing["meetings"]] == ["Ed's"]


def test_detail_running_job_overrides_phase_segment_total_depth(ts, monkeypatch):
    c = _client(ts)
    meeting_id = _create(c).json()["id"]

    monkeypatch.setattr(
        mr, "get_job_for_meeting",
        lambda mid, user: {
            "status": "running", "phase": "transcribing", "segment": 2,
            "total": 5, "depth": 0, "error": None, "document_id": None,
            "meeting_id": mid, "started_at": 0,
        },
    )
    detail = c.get(f"/api/meetings/{meeting_id}").json()
    assert detail["phase"] == "transcribing"
    assert detail["segment"] == 2
    assert detail["total"] == 5
    assert detail["depth"] == 0


def test_detail_interrupted_processing_is_presented_as_error(ts, monkeypatch):
    """A row stuck on status="processing" with no live job (server restart
    mid-job) is presented as an error telling the user to reprocess -
    without mutating the row (status stays "processing" in the DB)."""
    c = _client(ts)
    meeting_id = _create(c).json()["id"]

    s = ts()
    try:
        row = s.query(db.Meeting).filter(db.Meeting.id == meeting_id).first()
        row.status = "processing"
        s.commit()
    finally:
        s.close()

    monkeypatch.setattr(mr, "get_job_for_meeting", lambda mid, user: None)
    detail = c.get(f"/api/meetings/{meeting_id}").json()
    assert detail["status"] == "error"
    assert detail["error"] == "Verwerking onderbroken (herstart) — gebruik Reprocess"

    s = ts()
    try:
        row = s.query(db.Meeting).filter(db.Meeting.id == meeting_id).first()
        assert row.status == "processing"
    finally:
        s.close()

    listing = c.get("/api/meetings").json()["meetings"]
    assert listing[0]["status"] == "error"


# ---- GET /api/meetings/{id}/audio ----

def test_audio_serve_roundtrip(ts):
    c = _client(ts)
    meeting_id = _create(c).json()["id"]
    c.post(
        f"/api/meetings/{meeting_id}/chunks?seq=0",
        files={"file": ("c.webm", b"webm-bytes", "audio/webm")},
    )

    r = c.get(f"/api/meetings/{meeting_id}/audio")
    assert r.status_code == 200
    assert r.content == b"webm-bytes"
    assert r.headers["content-type"] == "audio/webm"


def test_audio_serve_unknown_id_is_404(ts):
    c = _client(ts)
    r = c.get("/api/meetings/does-not-exist/audio")
    assert r.status_code == 404


def test_audio_serve_no_file_yet_is_404(ts):
    c = _client(ts)
    meeting_id = _create(c).json()["id"]
    r = c.get(f"/api/meetings/{meeting_id}/audio")
    assert r.status_code == 404


@pytest.mark.parametrize("bad_id", ["../x", "..%2Fx", "x.webm"])
def test_audio_serve_traversal_shaped_id_is_404(ts, bad_id):
    # Fix-wave-2 item 8: a route-level contract test — resolve_meeting_audio_
    # path's traversal-rejection is unit-tested directly in
    # test_meeting_minutes_job.py, but the route itself must also 404 (never
    # 500, never leak an arbitrary file) for a traversal-shaped meeting_id
    # path segment, whether that comes from URL-normalization collapsing it
    # into an unmatched route or from _get_owned_meeting finding no such row.
    c = _client(ts)
    r = c.get(f"/api/meetings/{bad_id}/audio")
    assert r.status_code == 404


def test_audio_serve_cross_owner_is_404(ts):
    c_ed = _client(ts, user="ed")
    meeting_id = _create(c_ed).json()["id"]
    c_ed.post(
        f"/api/meetings/{meeting_id}/chunks?seq=0",
        files={"file": ("c.webm", b"x", "audio/webm")},
    )
    c_bob = _client(ts, user="bob")

    r = c_bob.get(f"/api/meetings/{meeting_id}/audio")
    assert r.status_code == 404


# ---- DELETE /api/meetings/{id} ----

def test_delete_while_job_running_is_409(ts, monkeypatch):
    c = _client(ts)
    meeting_id = _create(c).json()["id"]
    monkeypatch.setattr(
        mr, "get_job_for_meeting",
        lambda mid, user: {"status": "running", "meeting_id": mid},
    )

    r = c.delete(f"/api/meetings/{meeting_id}")
    assert r.status_code == 409
    assert r.json()["detail"] == "Verwerking loopt nog"


def test_delete_removes_row_and_audio_file(ts):
    c = _client(ts)
    meeting_id = _create(c).json()["id"]
    c.post(
        f"/api/meetings/{meeting_id}/chunks?seq=0",
        files={"file": ("c.webm", b"x", "audio/webm")},
    )

    r = c.delete(f"/api/meetings/{meeting_id}")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    assert c.get(f"/api/meetings/{meeting_id}").status_code == 404

    s = ts()
    try:
        assert s.query(db.Meeting).filter(db.Meeting.id == meeting_id).first() is None
    finally:
        s.close()


def test_delete_cross_owner_is_404(ts):
    c_ed = _client(ts, user="ed")
    meeting_id = _create(c_ed).json()["id"]
    c_bob = _client(ts, user="bob")

    r = c_bob.delete(f"/api/meetings/{meeting_id}")
    assert r.status_code == 404


# ---- GET /api/meetings/{id}/minutes (styled visual-report view) ----

def _attach_document(ts, meeting_id, content="# Notulen: Weekly\n\n## Samenvatting\n\nKort overleg.\n"):
    s = ts()
    try:
        doc_id = "doc-" + meeting_id
        s.add(db.Document(id=doc_id, owner="ed", title="Notulen – Weekly", language="markdown",
                          current_content=content, session_id=None))
        row = s.query(db.Meeting).filter(db.Meeting.id == meeting_id).one()
        row.document_id = doc_id
        row.status = "done"
        row.duration_seconds = 125
        s.commit()
        return doc_id
    finally:
        s.close()


def test_minutes_view_renders_visual_report_html(ts):
    c = _client(ts)
    meeting_id = _create(c, title="Weekly").json()["id"]
    _attach_document(ts, meeting_id)
    r = c.get(f"/api/meetings/{meeting_id}/minutes")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "Notulen: Weekly" in body
    assert "Samenvatting" in body
    assert "Kort overleg." in body
    # the visual-report chrome carries the stats we pass (duration label)
    assert "2 min" in body
    assert "Ithaka Meetings" in body


def test_minutes_view_without_document_is_404(ts):
    c = _client(ts)
    meeting_id = _create(c, title="Weekly").json()["id"]
    r = c.get(f"/api/meetings/{meeting_id}/minutes")
    assert r.status_code == 404
    assert r.json()["detail"] == "Nog geen notulen"


def test_minutes_view_cross_owner_is_404(ts):
    c = _client(ts)
    meeting_id = _create(c, title="Weekly").json()["id"]
    _attach_document(ts, meeting_id)
    other = _client(ts, user="bob")
    assert other.get(f"/api/meetings/{meeting_id}/minutes").status_code == 404
