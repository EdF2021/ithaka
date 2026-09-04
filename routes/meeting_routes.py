"""Meeting-recorder REST routes: create/record/finish/list/detail/audio/delete.

Task 4 of the meeting-recorder feature: the HTTP layer only, wiring the pure
functions/job-runner from ``src/meeting_minutes.py`` (Tasks 2+3) and the
``Meeting`` model (Task 1) into routes the frontend (``static/js/meetings.js``,
Task 5) already codes against.

Dependency style deliberately differs from most other route modules in this
codebase (which import ``get_current_user``/``SessionLocal`` at module scope):
per the task-4 brief, ``setup_meeting_routes`` takes both as arguments so
tests can hand it a fake auth function and a file-backed temp-sqlite session
factory without any module-level monkeypatching.

Spec: docs/superpowers/specs/2026-09-04-meeting-recorder-design.md
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from core.database import Meeting
from src.constants import MEETING_AUDIO_DIR
from src.meeting_minutes import (
    get_job_for_meeting,
    resolve_meeting_audio_path,
    start_processing_job,
)
from src.upload_handler import secure_filename
from src.upload_limits import (
    MEETING_AUDIO_MAX_BYTES,
    MEETING_CHUNK_MAX_BYTES,
    read_upload_limited,
)

TITLE_MAX_CHARS = 200
TEXT_MAX_CHARS = 20000

# In-memory expected-chunk-seq counter, keyed by meeting id. Reset on
# POST /api/meetings (create) and on a successful finish (both fresh and
# reprocess), popped on delete. Lost on a process restart — a chunk upload
# mid-recording then gets a 409 with the counter restarted at 0, which the
# client surfaces as a visible error rather than silently dropping audio.
# Ruling: this stays a plain module-level dict, not a DB column — see
# task-4-brief.md.
_next_seq: dict[str, int] = {}

# Per-meeting lock guarding the seq-check -> read -> size-check -> append ->
# counter-update sequence in upload_meeting_chunk. The client uploader is
# strictly one-request-at-a-time, so the only way two requests for the same
# meeting run concurrently is a dropped connection: the client's fetch
# rejects and retries the same seq while the original request is still
# parked inside `await read_upload_limited(...)`. Without the lock both
# requests observe the same `expected` seq, both pass the check, and both
# append — silently duplicating audio. Popped alongside `_next_seq` on
# finish/delete so this dict cannot grow without bound either.
_chunk_locks: dict[str, asyncio.Lock] = {}


def _lock_for(meeting_id: str) -> asyncio.Lock:
    lock = _chunk_locks.get(meeting_id)
    if lock is None:
        lock = asyncio.Lock()
        _chunk_locks[meeting_id] = lock
    return lock


def _serialize(row: Meeting, job: dict | None) -> dict:
    """Shape a Meeting row (+ optional live job snapshot) for the frontend.

    `phase`/`segment`/`total`/`depth` come from `job` when one is running
    for this meeting (get_job_for_meeting already filters to status ==
    "running"); segment/total/depth have no column on Meeting so they are
    None whenever no job is live.
    """
    data = {
        "id": row.id,
        "title": row.title,
        "agenda": row.agenda,
        "key_terms": row.key_terms,
        "status": row.status,
        "phase": row.phase,
        "error": row.error,
        "bytes_total": row.bytes_total,
        "duration_seconds": row.duration_seconds,
        "document_id": row.document_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "segment": None,
        "total": None,
        "depth": None,
    }
    if job:
        data["phase"] = job.get("phase")
        data["segment"] = job.get("segment")
        data["total"] = job.get("total")
        data["depth"] = job.get("depth")
    return data


def setup_meeting_routes(get_current_user, SessionLocal) -> APIRouter:
    router = APIRouter(tags=["meetings"])

    def _present(row: Meeting, user: str) -> dict:
        """_serialize() + the interrupted-processing presentation rule: a
        row stuck on status="processing" with no live job (the process
        restarted mid-job) is presented as an error telling the user to
        reprocess, without mutating the row itself (a restart might still
        resume; the job runner, not this route, owns that decision)."""
        job = get_job_for_meeting(row.id, user)
        data = _serialize(row, job)
        if row.status == "processing" and job is None:
            data["status"] = "error"
            data["error"] = "Verwerking onderbroken (herstart) — gebruik Reprocess"
        return data

    def _get_owned_meeting(db_session, meeting_id: str, user: str) -> Meeting:
        row = (
            db_session.query(Meeting)
            .filter(Meeting.id == meeting_id, Meeting.owner == user)
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Vergadering niet gevonden")
        return row

    # ---- POST /api/meetings ----
    @router.post("/api/meetings")
    async def create_meeting(request: Request):
        user = get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            body = {}

        raw_title = body.get("title")
        title = raw_title.strip() if isinstance(raw_title, str) else ""
        if not title:
            raise HTTPException(status_code=400, detail="Titel is verplicht")
        if len(title) > TITLE_MAX_CHARS:
            raise HTTPException(status_code=400, detail="Titel te lang")

        agenda = body.get("agenda")
        key_terms = body.get("key_terms")
        if isinstance(agenda, str) and len(agenda) > TEXT_MAX_CHARS:
            raise HTTPException(status_code=413, detail="Tekst te lang")
        if isinstance(key_terms, str) and len(key_terms) > TEXT_MAX_CHARS:
            raise HTTPException(status_code=413, detail="Tekst te lang")

        meeting_id = str(uuid.uuid4())
        row = Meeting(
            id=meeting_id,
            owner=user,
            title=title,
            agenda=agenda,
            key_terms=key_terms,
            status="recording",
            audio_path=f"{meeting_id}.webm",
            bytes_total=0,
        )
        db_session = SessionLocal()
        try:
            db_session.add(row)
            db_session.commit()
            db_session.refresh(row)
            _next_seq[meeting_id] = 0
            return _serialize(row, None)
        finally:
            db_session.close()

    # ---- POST /api/meetings/{meeting_id}/chunks ----
    @router.post("/api/meetings/{meeting_id}/chunks")
    async def upload_meeting_chunk(
        request: Request,
        meeting_id: str,
        seq: int = Query(...),
        file: UploadFile = File(...),
    ):
        user = get_current_user(request)
        # Held across the whole seq-check -> read -> size-check -> append ->
        # counter-update sequence so a retried chunk (client fetch rejects,
        # _pump retries the same seq while the original request is still
        # parked inside read_upload_limited) cannot be appended twice — see
        # _chunk_locks above.
        async with _lock_for(meeting_id):
            db_session = SessionLocal()
            try:
                row = _get_owned_meeting(db_session, meeting_id, user)
                if row.status != "recording":
                    raise HTTPException(status_code=400, detail="Opname is al afgesloten")

                expected = _next_seq.get(meeting_id, 0)
                if seq != expected:
                    # Flat body {"detail": ..., "expected": n} per spec — NOT
                    # HTTPException(detail={...}), which FastAPI would wrap in
                    # an extra {"detail": {...}} layer.
                    return JSONResponse(
                        status_code=409,
                        content={"detail": "Onverwacht chunknummer", "expected": expected},
                    )

                data = await read_upload_limited(file, MEETING_CHUNK_MAX_BYTES, "Audio chunk")
                if not data:
                    raise HTTPException(status_code=400, detail="Leeg audiofragment")
                if row.bytes_total + len(data) > MEETING_AUDIO_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="Opname te groot")

                path = Path(MEETING_AUDIO_DIR) / row.audio_path
                with open(path, "ab") as fh:
                    fh.write(data)

                row.bytes_total += len(data)
                _next_seq[meeting_id] = seq + 1
                db_session.commit()
                return {"seq": seq, "bytes_total": row.bytes_total}
            finally:
                db_session.close()

    # ---- POST /api/meetings/{meeting_id}/finish ----
    @router.post("/api/meetings/{meeting_id}/finish")
    async def finish_meeting(request: Request, meeting_id: str):
        user = get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            body = {}

        db_session = SessionLocal()
        try:
            row = _get_owned_meeting(db_session, meeting_id, user)
            if row.status not in ("recording", "error", "done"):
                # A "processing" row usually means a job is genuinely
                # running (start_processing_job's own check below would
                # raise the same ValueError("Verwerking loopt al") anyway
                # — checked here first only to avoid writing
                # duration_seconds into a row mid-job). But the detail
                # route presents a "processing" row with no live job as an
                # interrupted-by-restart error that tells the user to
                # Reprocess (POSTs right back here) — that path must not
                # dead-end on this same 400.
                if get_job_for_meeting(meeting_id, user) is not None:
                    raise HTTPException(status_code=400, detail="Verwerking loopt al")

            duration = body.get("duration_seconds")
            if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0:
                row.duration_seconds = duration
                db_session.commit()

            try:
                job_id = start_processing_job(meeting_id, user, SessionLocal)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except RuntimeError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

            _next_seq.pop(meeting_id, None)
            _chunk_locks.pop(meeting_id, None)
            return {"job_id": job_id, "status": "processing"}
        finally:
            db_session.close()

    # ---- GET /api/meetings ----
    @router.get("/api/meetings")
    async def list_meetings(request: Request):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            rows = (
                db_session.query(Meeting)
                .filter(Meeting.owner == user)
                .order_by(Meeting.created_at.desc())
                .limit(200)
                .all()
            )
            return {"meetings": [_present(row, user) for row in rows]}
        finally:
            db_session.close()

    # ---- GET /api/meetings/{meeting_id} ----
    @router.get("/api/meetings/{meeting_id}")
    async def get_meeting(request: Request, meeting_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            row = _get_owned_meeting(db_session, meeting_id, user)
            return _present(row, user)
        finally:
            db_session.close()

    # ---- GET /api/meetings/{meeting_id}/audio ----
    @router.get("/api/meetings/{meeting_id}/audio")
    async def get_meeting_audio(request: Request, meeting_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            row = _get_owned_meeting(db_session, meeting_id, user)
        finally:
            db_session.close()

        path = resolve_meeting_audio_path(row.audio_path)
        if path is None:
            raise HTTPException(status_code=404, detail="Audio niet gevonden")

        safe_title = secure_filename(row.title or "meeting")
        return FileResponse(
            str(path), media_type="audio/webm", filename=f"{safe_title}.webm"
        )

    # ---- DELETE /api/meetings/{meeting_id} ----
    @router.delete("/api/meetings/{meeting_id}")
    async def delete_meeting(request: Request, meeting_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            row = _get_owned_meeting(db_session, meeting_id, user)
            if get_job_for_meeting(meeting_id, user) is not None:
                raise HTTPException(status_code=409, detail="Verwerking loopt nog")

            if row.audio_path:
                (Path(MEETING_AUDIO_DIR) / row.audio_path).unlink(missing_ok=True)

            db_session.delete(row)
            db_session.commit()
            _next_seq.pop(meeting_id, None)
            _chunk_locks.pop(meeting_id, None)
            return {"ok": True}
        finally:
            db_session.close()

    return router
