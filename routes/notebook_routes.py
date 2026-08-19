"""Notebook routes — CRUD for notebooks + per-notebook source upload/removal."""

import logging
import os
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse

import src.notebook_audio as notebook_audio
from core.database import Document, SessionLocal, Notebook, NotebookArtifact, NotebookSource
from core.database import Session as DbSession
from src.auth_helpers import get_current_user
from src.notebook_artifacts import ARTIFACT_KINDS, generate_artifact
from src.notebook_audio import (
    NOTEBOOK_AUDIO_HEADERS,
    NOTEBOOK_AUDIO_RE,
    get_job,
    resolve_notebook_audio_path,
    set_synthesizer,
    start_podcast_job,
)
from src.notebook_ingest import ingest_notebook_file
from src.notebook_suggest import suggest_questions
from src.settings import load_settings
from src.upload_limits import PERSONAL_UPLOAD_MAX_BYTES, format_byte_limit

logger = logging.getLogger(__name__)

# Exact wording from docs/superpowers/specs/2026-08-17-notebooks-fase3-audio-design.md
# §Fouten & randgevallen — the frontend surfaces this detail verbatim.
_TTS_NOT_CONFIGURED_DETAIL = "TTS is niet geconfigureerd (Settings → TTS)"


def _current_tts_provider() -> str:
    """Read tts_provider the same way TTSService._load_settings does."""
    try:
        settings = load_settings() or {}
    except Exception:
        settings = {}
    return str(settings.get("tts_provider") or "disabled")


def _unlink_podcast_audio(audio_path: Optional[str]) -> None:
    """Best-effort removal of a podcast's WAV file. Never raises.

    Deliberately does not call resolve_notebook_audio_path: that function
    raises HTTPException(404) when the file is already gone, which makes
    unlink(missing_ok=True) dead code and turns every cleanup of an
    already-removed file into a logged "Could not remove" warning. Validates
    the filename with the same whitelist regex + commonpath guard instead,
    reading NOTEBOOK_AUDIO_DIR off the notebook_audio module (not
    src.constants) at call time so tests that monkeypatch it there still
    apply here, exactly like resolve_notebook_audio_path itself does.
    """
    if not isinstance(audio_path, str) or not NOTEBOOK_AUDIO_RE.fullmatch(audio_path):
        return
    try:
        directory = Path(notebook_audio.NOTEBOOK_AUDIO_DIR)
        root = directory.resolve()
        path = (directory / audio_path).resolve()
        if os.path.commonpath([str(root), str(path)]) != str(root):
            return
        path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Could not remove podcast audio %s: %s", audio_path, exc)


def _get_owned_notebook(db_session, notebook_id, user):
    nb = db_session.query(Notebook).filter_by(id=notebook_id).first()
    if nb is None or nb.owner != user:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return nb


def setup_notebook_routes(rag_manager, tts_service=None) -> APIRouter:
    router = APIRouter(tags=["notebooks"])

    # Install the synthesizer hook once, at wiring time, mirroring how every
    # other route factory in this codebase gets its dependencies injected as
    # arguments rather than reading globals. Unconditional on the provider:
    # TTSService.synthesize_voice re-reads settings live on every call and
    # raises its own RuntimeError when the provider is disabled/browser, so
    # gating this on the boot-time provider would only cause a stale gap —
    # TTS enabled in Settings after boot would stay unwired until a restart,
    # even though the POST route's own live provider-check would let the
    # request through. The route's pre-check below is what actually produces
    # the spec's 400 for a disabled/browser provider; this line only needs to
    # know whether a TTSService exists at all.
    if tts_service is not None:
        set_synthesizer(tts_service.synthesize_voice)

    def _remove_notebook_chunks(notebook_id, document_id=None):
        try:
            rag_manager.remove_notebook(notebook_id, document_id=document_id)
        except Exception as exc:
            logger.warning("remove_notebook failed for %s (document_id=%s): %s",
                          notebook_id, document_id, exc)

    # ---- GET /api/notebooks ----
    @router.get("/api/notebooks")
    async def list_notebooks(request: Request):
        user = get_current_user(request)
        include_archived = request.query_params.get("archived") == "1"
        db_session = SessionLocal()
        try:
            q = db_session.query(Notebook).filter_by(owner=user)
            if not include_archived:
                q = q.filter_by(archived=False)
            notebooks = q.order_by(Notebook.archived.asc(), Notebook.created_at.desc()).all()
            return {"notebooks": [n.to_dict() for n in notebooks]}
        finally:
            db_session.close()

    # ---- POST /api/notebooks ----
    @router.post("/api/notebooks")
    async def create_notebook(request: Request):
        user = get_current_user(request)
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        description = body.get("description")
        db_session = SessionLocal()
        try:
            nb = Notebook(id=str(uuid.uuid4()), owner=user, name=name, description=description)
            db_session.add(nb)
            db_session.commit()
            db_session.refresh(nb)
            return nb.to_dict()
        finally:
            db_session.close()

    # ---- PATCH /api/notebooks/{id} ----
    @router.patch("/api/notebooks/{notebook_id}")
    async def update_notebook(request: Request, notebook_id: str):
        user = get_current_user(request)
        body = await request.json()
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            if "name" in body:
                name = (body.get("name") or "").strip()
                if not name:
                    raise HTTPException(status_code=400, detail="name cannot be empty")
                nb.name = name
            if "description" in body:
                nb.description = body.get("description")
            if "archived" in body:
                nb.archived = bool(body.get("archived"))
            db_session.commit()
            db_session.refresh(nb)
            return nb.to_dict()
        finally:
            db_session.close()

    # ---- DELETE /api/notebooks/{id} ----
    @router.delete("/api/notebooks/{notebook_id}")
    async def delete_notebook(request: Request, notebook_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            _remove_notebook_chunks(nb.id)
            db_session.query(DbSession).filter_by(notebook_id=nb.id).update({"notebook_id": None})
            # Artifact Documents are generated output owned by the notebook,
            # so they go with it. Source Documents stay (Fase 1 behaviour):
            # a source's Document lives in the Library independent of the
            # notebook and is only unlinked via NotebookSource's own
            # ondelete=SET NULL when the source row disappears.
            #
            # Delete the notebook_artifacts rows via a bulk statement before
            # touching their Documents: document_id is ondelete=CASCADE, so
            # deleting the Documents first (or relying on
            # db_session.delete(nb)'s relationship cascade, whose timing vs.
            # a raw bulk delete isn't guaranteed) risks SQLite's cascade
            # removing a notebook_artifacts row the ORM still expects to
            # delete itself, which trips SQLAlchemy's confirm_deleted_rows
            # check. Deleting the artifact rows here first removes that race
            # entirely; the relationship cascade below then finds nothing
            # left to do.
            artifact_rows = (
                db_session.query(NotebookArtifact.document_id, NotebookArtifact.audio_path)
                .filter_by(notebook_id=nb.id).all()
            )
            artifact_doc_ids = [doc_id for (doc_id, _audio_path) in artifact_rows]
            artifact_audio_paths = [audio_path for (_doc_id, audio_path) in artifact_rows if audio_path]
            if artifact_doc_ids:
                (db_session.query(NotebookArtifact)
                 .filter_by(notebook_id=nb.id)
                 .delete(synchronize_session=False))
                (db_session.query(Document)
                 .filter(Document.id.in_(artifact_doc_ids))
                 .delete(synchronize_session=False))
            db_session.delete(nb)
            db_session.commit()
            # Best-effort, same as delete_artifact above: drop any in-memory
            # active-doc pointers for the artifact Documents just hard-deleted.
            try:
                from src.agent_tools.document_tools import clear_active_document
                for doc_id in artifact_doc_ids:
                    clear_active_document(doc_id)
            except Exception:
                pass
            for audio_path in artifact_audio_paths:
                _unlink_podcast_audio(audio_path)
            return {"success": True}
        finally:
            db_session.close()

    # ---- GET /api/notebooks/{id}/sources ----
    @router.get("/api/notebooks/{notebook_id}/sources")
    async def list_sources(request: Request, notebook_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            sources = (db_session.query(NotebookSource)
                       .filter_by(notebook_id=nb.id)
                       .order_by(NotebookSource.created_at.desc())
                       .all())
            return {"sources": [s.to_dict() for s in sources]}
        finally:
            db_session.close()

    # ---- POST /api/notebooks/{id}/sources ----
    @router.post("/api/notebooks/{notebook_id}/sources")
    async def upload_sources(request: Request, notebook_id: str,
                             files: List[UploadFile] = File(...)):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            results = []
            failed = 0
            for f in files:
                content = await f.read(PERSONAL_UPLOAD_MAX_BYTES + 1)
                if len(content) > PERSONAL_UPLOAD_MAX_BYTES:
                    src = NotebookSource(
                        id=str(uuid.uuid4()), notebook_id=nb.id, filename=f.filename,
                        status="failed",
                        error=f"file exceeds {format_byte_limit(PERSONAL_UPLOAD_MAX_BYTES)} limit")
                    db_session.add(src)
                    db_session.commit()
                    results.append(src)
                    failed += 1
                    continue
                src = ingest_notebook_file(nb.id, user, f.filename, content,
                                           rag_manager, db_session)
                results.append(src)
                if src.status != "indexed":
                    failed += 1
            return {"sources": [s.to_dict() for s in results], "failed": failed}
        finally:
            db_session.close()

    # ---- DELETE /api/notebooks/{id}/sources/{source_id} ----
    @router.delete("/api/notebooks/{notebook_id}/sources/{source_id}")
    async def delete_source(request: Request, notebook_id: str, source_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            src = (db_session.query(NotebookSource)
                  .filter_by(id=source_id, notebook_id=nb.id).first())
            if src is None:
                raise HTTPException(status_code=404, detail="Source not found")
            # Only chunks embedded for this document belong to this source; a
            # "failed" source has document_id=None, so skip the RAG call
            # entirely rather than passing document_id=None (which would
            # match — and delete — every chunk in the whole notebook).
            if src.document_id:
                _remove_notebook_chunks(nb.id, document_id=src.document_id)
            db_session.delete(src)
            db_session.commit()
            return {"success": True}
        finally:
            db_session.close()

    # ---- GET /api/notebooks/{id}/artifacts ----
    @router.get("/api/notebooks/{notebook_id}/artifacts")
    async def list_artifacts(request: Request, notebook_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            rows = (
                db_session.query(NotebookArtifact, Document.title)
                .outerjoin(Document, Document.id == NotebookArtifact.document_id)
                .filter(NotebookArtifact.notebook_id == nb.id)
                .order_by(NotebookArtifact.created_at.desc())
                .all()
            )
            artifacts = []
            for artifact, title in rows:
                d = artifact.to_dict()
                d["title"] = title
                artifacts.append(d)
            return {"artifacts": artifacts}
        finally:
            db_session.close()

    # ---- POST /api/notebooks/{id}/artifacts ----
    @router.post("/api/notebooks/{notebook_id}/artifacts")
    async def create_artifact(request: Request, notebook_id: str):
        user = get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            body = None
        kind = body.get("kind") if isinstance(body, dict) else None
        if kind not in ARTIFACT_KINDS:
            raise HTTPException(status_code=400, detail=f"Onbekend artifact-type: {kind}")
        db_session = SessionLocal()
        try:
            # _get_owned_notebook here + generate_artifact's own owner filter
            # below is intentional defence-in-depth, not redundancy: this
            # call gives a clean 404 before any work starts, while
            # generate_artifact's own check keeps it safe to call directly
            # (e.g. from a future non-route caller) without relying on the
            # route to have checked ownership first.
            _get_owned_notebook(db_session, notebook_id, user)
            try:
                artifact = await generate_artifact(notebook_id, user, kind, db_session)
            except HTTPException:
                # Not raised by generate_artifact today, but this keeps a
                # future refactor from having HTTPException fall through
                # into the catch-all below and get remapped to a 502.
                raise
            except ValueError as exc:
                # kind and notebook ownership are already validated above, so
                # the only ValueError generate_artifact can still raise here
                # is "geen geïndexeerde bronnen".
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                # LLM/endpoint failure (RuntimeError on an empty answer, or
                # any error from the endpoint call chain).
                logger.exception(
                    "Artifact generation failed for notebook %s (kind=%s)", notebook_id, kind
                )
                raise HTTPException(status_code=502, detail=str(exc))
            return artifact.to_dict()
        finally:
            db_session.close()

    # ---- DELETE /api/notebooks/{id}/artifacts/{artifact_id} ----
    @router.delete("/api/notebooks/{notebook_id}/artifacts/{artifact_id}")
    async def delete_artifact(request: Request, notebook_id: str, artifact_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            artifact = (db_session.query(NotebookArtifact)
                       .filter_by(id=artifact_id, notebook_id=nb.id).first())
            if artifact is None:
                raise HTTPException(status_code=404, detail="Artifact not found")
            # document_id has ondelete=CASCADE, so if the Document delete
            # flushes before the artifact's own DELETE, SQLite's cascade
            # takes the notebook_artifacts row with it and the ORM's
            # subsequent DELETE for that row matches 0. Flushing the
            # artifact delete first (no ORM relationship links the two
            # mappers, so unit-of-work has no dependency to order this by
            # itself) avoids that race.
            doc = db_session.get(Document, artifact.document_id)
            # Copied out before the DELETE: once the row is gone the ORM
            # instance is expired, and the post-commit .document_id access
            # below only still works by relying on SQLAlchemy's identity-map
            # cache — audio_path deserves its own explicit local, not that.
            document_id = artifact.document_id
            audio_path = artifact.audio_path
            db_session.delete(artifact)
            db_session.flush()
            if doc is not None:
                db_session.delete(doc)
            db_session.commit()
            # Best-effort: drop the in-memory active-doc pointer so a hard-deleted
            # artifact Document isn't re-injected into a later chat (#1160), same
            # as routes/document_routes.py's delete_document.
            try:
                from src.agent_tools.document_tools import clear_active_document
                clear_active_document(document_id)
            except Exception:
                pass
            _unlink_podcast_audio(audio_path)
            return {"success": True}
        finally:
            db_session.close()

    # ---- POST /api/notebooks/{id}/suggest_questions ----
    @router.post("/api/notebooks/{notebook_id}/suggest_questions")
    async def suggest_notebook_questions(request: Request, notebook_id: str):
        user = get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            body = None
        question = body.get("question") if isinstance(body, dict) else None
        answer = body.get("answer") if isinstance(body, dict) else None
        if not question or not answer:
            raise HTTPException(status_code=400, detail="question en answer zijn verplicht")
        db_session = SessionLocal()
        try:
            _get_owned_notebook(db_session, notebook_id, user)
        finally:
            db_session.close()
        try:
            questions = await suggest_questions(question, answer, user)
        except Exception:
            # Best-effort: suggesties zijn nice-to-have, nooit een 5xx
            # richting de chat-flow.
            logger.info("suggest_questions failed for notebook %s", notebook_id, exc_info=True)
            questions = []
        return {"questions": questions}

    # ---- POST /api/notebooks/{id}/podcast ----
    @router.post("/api/notebooks/{notebook_id}/podcast")
    async def create_podcast(request: Request, notebook_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            # Validatievolgorde: owner-404, dan TTS-400 — in die volgorde,
            # vóór jobstart. start_podcast_job below re-checks owner/bronnen/
            # synthesizer itself (it can be called without a route in front
            # of it) and is the sole source of the bronnen-400 now (no
            # route-level gather_source_text duplicate): its ValueError
            # "Geen geïndexeerde bronnen" is mapped to 400 below with the
            # same text. Its TTS RuntimeError text ("TTS niet geconfigureerd")
            # is not the spec's user-facing string though, so that one check
            # still has to happen here, ahead of the job start.
            _get_owned_notebook(db_session, notebook_id, user)
            if _current_tts_provider() in ("disabled", "browser"):
                raise HTTPException(status_code=400, detail=_TTS_NOT_CONFIGURED_DETAIL)
        finally:
            db_session.close()

        try:
            job_id = start_podcast_job(notebook_id, user)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"job_id": job_id, "status": "running"}

    # ---- GET /api/notebooks/{id}/podcast/{job_id} ----
    @router.get("/api/notebooks/{notebook_id}/podcast/{job_id}")
    async def get_podcast_status(request: Request, notebook_id: str, job_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            _get_owned_notebook(db_session, notebook_id, user)
        finally:
            db_session.close()

        job = get_job(job_id, user)
        # A job that exists but belongs to a different notebook (same owner,
        # e.g. re-used across two of the user's own notebooks) is unknown
        # from this route's point of view: same 404 as an unknown job id.
        if job is None or job.get("notebook_id") != notebook_id:
            raise HTTPException(status_code=404, detail="Job not found")

        # get_job's owner-check already keeps this cross-owner-safe; "cancelled"
        # (set when the job's asyncio.Task is cancelled, e.g. a shutdown mid-job)
        # is a third terminal status the frontend does not know about, so it is
        # folded into "error" here rather than adding a poller branch there.
        status = job.get("status")
        error = job.get("error")
        if status == "cancelled":
            status = "error"
            error = error or "Generatie afgebroken"
        return {
            "status": status,
            "phase": job.get("phase"),
            "segment": job.get("segment"),
            "total": job.get("total"),
            "error": error,
            "artifact": job.get("artifact"),
        }

    # ---- GET /api/notebook-audio/{filename} ----
    @router.get("/api/notebook-audio/{filename}")
    async def serve_notebook_audio(request: Request, filename: str):
        user = get_current_user(request)
        # 400 on a malformed name, 404 when the file itself is absent.
        path = resolve_notebook_audio_path(filename)
        db_session = SessionLocal()
        try:
            row = (
                db_session.query(Notebook.owner)
                .join(NotebookArtifact, NotebookArtifact.notebook_id == Notebook.id)
                .filter(NotebookArtifact.audio_path == filename)
                .first()
            )
            # No artifact row for this filename, or it belongs to another
            # owner: 404 either way, never confirm existence to a non-owner.
            if row is None or row[0] != user:
                raise HTTPException(status_code=404, detail="Audio not found")
        finally:
            db_session.close()

        return FileResponse(str(path), media_type="audio/wav", headers=NOTEBOOK_AUDIO_HEADERS)

    return router
