"""Notebook routes — CRUD for notebooks + per-notebook source upload/removal."""

import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Body, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse

import src.notebook_audio as notebook_audio
import src.notebook_video as notebook_video
import src.notebook_covers as notebook_covers
from core.database import Document, SessionLocal, Notebook, NotebookArtifact, NotebookSource
from core.database import Session as DbSession
from src.auth_helpers import get_current_user
from src.notebook_artifacts import ARTIFACT_KINDS, generate_artifact
from src.notebook_report_layouts import (
    FIXED_TEMPLATES,
    get_recommended_layouts,
    notebook_has_sources,
)
from src.notebook_audio import (
    NOTEBOOK_AUDIO_HEADERS,
    NOTEBOOK_AUDIO_RE,
    get_job,
    resolve_notebook_audio_path,
    set_synthesizer,
    start_podcast_job,
)
from src.notebook_covers import (
    COVER_IMAGE_HEADERS,
    resolve_cover_image_path,
    start_cover_job,
)
from src.notebook_flashcards import generate_flashcards
from src.notebook_infographic import generate_infographic
from src.notebook_mindmap import generate_mindmap_viewer
from src.notebook_slides import generate_slide_deck
from src.notebook_ingest import ingest_notebook_file, ingest_notebook_url
from src.notebook_report import generate_notebook_artifact_report
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


def _unlink_video_file(video_path: Optional[str]) -> None:
    """Best-effort removal of a video artifact's mp4. Never raises.

    Same shape and reasoning as _unlink_podcast_audio above, against
    notebook_video's whitelist regex and directory attribute.
    """
    if not isinstance(video_path, str) or not notebook_video.NOTEBOOK_VIDEO_RE.fullmatch(video_path):
        return
    try:
        directory = Path(notebook_video.NOTEBOOK_VIDEO_DIR)
        root = directory.resolve()
        path = (directory / video_path).resolve()
        if os.path.commonpath([str(root), str(path)]) != str(root):
            return
        path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Could not remove notebook video %s: %s", video_path, exc)


def _artifact_dict_with_title(artifact, document_title):
    """Enrich an artifact's to_dict() with the effective title: the
    artifact's own (renamable) title if set, else the linked Document's
    title. `document_title` may be None (see test_list_artifacts_title_none_
    safe_when_document_missing) — that stays a safe fallback to None, same
    as before this column existed."""
    d = artifact.to_dict()
    d["title"] = artifact.title or document_title
    return d


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
        notebook_video.set_synthesizer(tts_service.synthesize_voice)

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
        # Same body posture as create_artifact/rename_artifact: malformed JSON
        # and non-string field types are client errors (400), not 500s. Without
        # the isinstance guards, {"name": 123} raises AttributeError on .strip()
        # and a dict/list description reaches SQLite as an unbindable type.
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        name = body.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        description = body.get("description")
        if description is not None and not isinstance(description, str):
            raise HTTPException(status_code=400, detail="description must be a string")
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
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            if "name" in body:
                name = body.get("name")
                name = name.strip() if isinstance(name, str) else ""
                if not name:
                    raise HTTPException(status_code=400, detail="name cannot be empty")
                nb.name = name
            if "description" in body:
                description = body.get("description")
                if description is not None and not isinstance(description, str):
                    raise HTTPException(
                        status_code=400, detail="description must be a string"
                    )
                nb.description = description
            if "archived" in body:
                nb.archived = bool(body.get("archived"))
            if "cover_image" in body:
                cover = body.get("cover_image")
                if cover is not None and not isinstance(cover, str):
                    raise HTTPException(
                        status_code=400, detail="cover_image must be a string"
                    )
                nb.cover_image = cover
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
                db_session.query(NotebookArtifact.document_id, NotebookArtifact.audio_path,
                                 NotebookArtifact.video_path)
                .filter_by(notebook_id=nb.id).all()
            )
            artifact_doc_ids = [doc_id for (doc_id, _audio_path, _video_path) in artifact_rows]
            artifact_audio_paths = [audio_path for (_doc_id, audio_path, _video_path) in artifact_rows if audio_path]
            artifact_video_paths = [video_path for (_doc_id, _audio_path, video_path) in artifact_rows if video_path]
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
            for video_path in artifact_video_paths:
                _unlink_video_file(video_path)
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

    # ---- POST /api/notebooks/{id}/sources/url ----
    @router.post("/api/notebooks/{notebook_id}/sources/url")
    async def add_source_from_url(request: Request, notebook_id: str,
                                  body: dict = Body(...)):
        """Fetch one web page and ingest it as a source (fase 4d).

        One URL per call — the fetch+embed runs synchronously in this
        request (same cost profile as a file upload), so the frontend adds
        results one "Toevoegen" click at a time.
        """
        user = get_current_user(request)
        url = (body.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Ongeldige URL")
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            # The fetch (network I/O, seconds) must not block the event loop.
            src = await asyncio.to_thread(
                ingest_notebook_url, nb.id, user, url, rag_manager, db_session
            )
            return {"source": src.to_dict()}
        finally:
            db_session.close()

    # ---- POST /api/notebooks/{id}/source-search ----
    @router.post("/api/notebooks/{notebook_id}/source-search")
    async def search_web_sources(request: Request, notebook_id: str,
                                 body: dict = Body(...)):
        """Light web search (configured provider, no page fetches) so the
        sources panel can offer results to add as sources."""
        user = get_current_user(request)
        query = (body.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="Lege zoekopdracht")
        db_session = SessionLocal()
        try:
            _get_owned_notebook(db_session, notebook_id, user)
        finally:
            db_session.close()
        try:
            # Import from services.search (NOT the divergent src/search
            # duplicate — see the fase-4 design doc's verkenning).
            from services.search.core import searxng_search_results
            results = await asyncio.to_thread(searxng_search_results, query)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Zoeken mislukt: {exc}")
        slim = [
            {"title": r.get("title") or r.get("url"), "url": r.get("url"),
             "snippet": (r.get("snippet") or "")[:300]}
            for r in (results or []) if r.get("url")
        ][:10]
        return {"results": slim}

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
            artifacts = [_artifact_dict_with_title(artifact, title) for artifact, title in rows]
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
        focus = body.get("focus") if isinstance(body, dict) else None
        layout_instruction = body.get("layout_instruction") if isinstance(body, dict) else None
        if focus is not None and not isinstance(focus, str):
            raise HTTPException(status_code=400, detail="focus moet een string zijn")
        if layout_instruction is not None:
            if not isinstance(layout_instruction, str):
                raise HTTPException(status_code=400, detail="layout_instruction moet een string zijn")
            if len(layout_instruction) > 2000:
                raise HTTPException(status_code=400, detail="layout_instruction is te lang (max 2000 tekens)")
        # ARTIFACT_KINDS is a dict, so an unhashable `kind` (a list or dict
        # from the request body) raises TypeError on the membership test
        # below — a 500 where the client sent bad input. Same isinstance
        # posture as rename_artifact's title check.
        if not isinstance(kind, str):
            raise HTTPException(status_code=400, detail=f"Onbekend artifact-type: {kind!r}")
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
                artifact = await generate_artifact(
                    notebook_id, user, kind, db_session,
                    focus=focus, layout_instruction=layout_instruction,
                )
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

    # ---- GET /api/notebooks/{id}/report-layouts ----
    @router.get("/api/notebooks/{notebook_id}/report-layouts")
    async def get_report_layouts(request: Request, notebook_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            recommended = await get_recommended_layouts(nb, db_session, user)
            if recommended:
                recommended_status = "ok"
            elif notebook_has_sources(nb, db_session):
                recommended_status = "unavailable"
            else:
                recommended_status = "no_sources"
            return {
                "templates": FIXED_TEMPLATES,
                "recommended": recommended,
                "recommended_status": recommended_status,
            }
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
            video_path = artifact.video_path
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
            _unlink_video_file(video_path)
            return {"success": True}
        finally:
            db_session.close()

    # ---- PATCH /api/notebooks/{id}/artifacts/{artifact_id} (rename) ----
    @router.patch("/api/notebooks/{notebook_id}/artifacts/{artifact_id}")
    async def rename_artifact(request: Request, notebook_id: str, artifact_id: str):
        user = get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            body = None
        title = body.get("title") if isinstance(body, dict) else None
        if not isinstance(title, str):
            raise HTTPException(status_code=400, detail="title is verplicht")
        title = title.strip()
        if not title or len(title) > 200:
            raise HTTPException(
                status_code=400, detail="title moet 1-200 tekens zijn (na strip)"
            )
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            # Outerjoin (not the report endpoint's inner join): an artifact
            # whose Document was hard-deleted must still be renamable, it
            # just keeps falling back to None for the enriched title, same
            # as list_artifacts.
            row = (
                db_session.query(NotebookArtifact, Document.title)
                .outerjoin(Document, Document.id == NotebookArtifact.document_id)
                .filter(NotebookArtifact.id == artifact_id, NotebookArtifact.notebook_id == nb.id)
                .first()
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Artifact not found")
            artifact, document_title = row
            artifact.title = title
            db_session.commit()
            db_session.refresh(artifact)
            return _artifact_dict_with_title(artifact, document_title)
        finally:
            db_session.close()

    # ---- GET /api/notebooks/{id}/artifacts/{artifact_id}/report ----
    @router.get("/api/notebooks/{notebook_id}/artifacts/{artifact_id}/report")
    async def get_artifact_report(request: Request, notebook_id: str, artifact_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            # Inner join: an artifact whose Document has been hard-deleted
            # (data inconsistency, see test_list_artifacts_title_none_safe_
            # when_document_missing) has nothing to render, so it 404s the
            # same as an unknown artifact id rather than 500ing.
            row = (
                db_session.query(NotebookArtifact, Document)
                .join(Document, Document.id == NotebookArtifact.document_id)
                .filter(NotebookArtifact.id == artifact_id, NotebookArtifact.notebook_id == nb.id)
                .first()
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Artifact not found")
            artifact, document = row
            # Podcasts have no markdown (audio_path, not current_content) —
            # nothing for the visual-report template to render.
            if artifact.kind in ("podcast", "video"):
                # Media artifacts render through their player panel; their
                # readable script/transcript opens via the linked Document.
                raise HTTPException(status_code=404, detail="No visual report for media artifacts")
            # Infographic gets its own compact poster renderer instead of
            # the shared long-form editorial template — see
            # src/notebook_infographic.py's module docstring for why.
            if artifact.kind == "slide_deck":
                # Slides are an interaction (navigate, notes toggle) — own
                # viewer template, same reasoning as flashcards/infographic.
                html_content = generate_slide_deck(
                    title=artifact.title or document.title,
                    markdown=document.current_content,
                    notebook_name=nb.name,
                    generated_at=datetime.now(),
                )
            elif artifact.kind == "flashcards":
                # Flip cards are an interaction, not a long-form read — own
                # compact template, same reasoning as the infographic below.
                html_content = generate_flashcards(
                    title=artifact.title or document.title,
                    markdown=document.current_content,
                    notebook_name=nb.name,
                    generated_at=datetime.now(),
                )
            elif artifact.kind == "mindmap":
                # Interactive collapsible-tree viewer over the stored mermaid
                # markdown — own template, same reasoning as slides/flashcards.
                html_content = generate_mindmap_viewer(
                    title=artifact.title or document.title,
                    markdown=document.current_content,
                    notebook_name=nb.name,
                    generated_at=datetime.now(),
                )
            elif artifact.kind == "infographic":
                html_content = generate_infographic(
                    title=artifact.title or document.title,
                    markdown=document.current_content,
                    notebook_name=nb.name,
                    generated_at=datetime.now(),
                )
            else:
                html_content = generate_notebook_artifact_report(
                    notebook_name=nb.name,
                    kind=artifact.kind,
                    document_title=artifact.title or document.title,
                    document_content=document.current_content,
                )
            return HTMLResponse(content=html_content)
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

    # ---- POST /api/notebooks/{id}/video ----
    @router.post("/api/notebooks/{notebook_id}/video")
    async def create_video(request: Request, notebook_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            # Same validation order as create_podcast: owner-404, then the
            # user-facing TTS-400, ahead of the job start (which re-checks
            # owner/bronnen/synthesizer/ffmpeg itself).
            _get_owned_notebook(db_session, notebook_id, user)
            if _current_tts_provider() in ("disabled", "browser"):
                raise HTTPException(status_code=400, detail=_TTS_NOT_CONFIGURED_DETAIL)
        finally:
            db_session.close()

        try:
            job_id = notebook_video.start_video_job(notebook_id, user)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"job_id": job_id, "status": "running"}

    # ---- GET /api/notebooks/{id}/video/{job_id} ----
    @router.get("/api/notebooks/{notebook_id}/video/{job_id}")
    async def get_video_status(request: Request, notebook_id: str, job_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            _get_owned_notebook(db_session, notebook_id, user)
        finally:
            db_session.close()

        job = notebook_video.get_job(job_id, user)
        if job is None or job.get("notebook_id") != notebook_id:
            raise HTTPException(status_code=404, detail="Job not found")

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
            "script_attempt": job.get("script_attempt"),
            "error": error,
            "artifact": job.get("artifact"),
        }

    # ---- GET /api/notebook-video/{filename} ----
    @router.get("/api/notebook-video/{filename}")
    async def serve_notebook_video(request: Request, filename: str):
        user = get_current_user(request)
        path = notebook_video.resolve_notebook_video_path(filename)
        db_session = SessionLocal()
        try:
            row = (
                db_session.query(Notebook.owner)
                .join(NotebookArtifact, NotebookArtifact.notebook_id == Notebook.id)
                .filter(NotebookArtifact.video_path == filename)
                .first()
            )
            if row is None or row[0] != user:
                raise HTTPException(status_code=404, detail="Video not found")
        finally:
            db_session.close()

        # FileResponse serves Range/206 natively, so the <video> element can
        # seek without any extra work here.
        return FileResponse(
            str(path), media_type="video/mp4",
            headers=notebook_video.NOTEBOOK_VIDEO_HEADERS,
        )

    # ---- POST /api/notebooks/{id}/cover-image ----
    @router.post("/api/notebooks/{notebook_id}/cover-image")
    async def create_cover_image(request: Request, notebook_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            _get_owned_notebook(db_session, notebook_id, user)
        finally:
            db_session.close()

        try:
            job_id = start_cover_job(notebook_id, user)
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if "niet gevonden" in detail else 409
            raise HTTPException(status_code=status_code, detail=detail)
        return {"job_id": job_id, "status": "running"}

    # ---- GET /api/notebooks/{id}/cover-image/{job_id} ----
    @router.get("/api/notebooks/{notebook_id}/cover-image/{job_id}")
    async def get_cover_image_status(request: Request, notebook_id: str, job_id: str):
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            _get_owned_notebook(db_session, notebook_id, user)
        finally:
            db_session.close()

        job = notebook_covers.get_job(job_id, user)
        if job is None or job.get("notebook_id") != notebook_id:
            raise HTTPException(status_code=404, detail="Job not found")

        return {
            "status": job.get("status"),
            "error": job.get("error"),
            "cover_image": job.get("cover_image"),
        }

    # ---- GET /api/notebook-cover/{filename} ----
    @router.get("/api/notebook-cover/{filename}")
    async def serve_notebook_cover(request: Request, filename: str):
        user = get_current_user(request)
        path = resolve_cover_image_path(filename)
        db_session = SessionLocal()
        try:
            row = (
                db_session.query(Notebook.owner)
                .filter(Notebook.cover_image == filename)
                .first()
            )
            if row is None or row[0] != user:
                raise HTTPException(status_code=404, detail="Cover image not found")
        finally:
            db_session.close()

        ext = filename.rsplit(".", 1)[-1].lower()
        mime = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp",
        }.get(ext, "application/octet-stream")
        return FileResponse(str(path), media_type=mime, headers=COVER_IMAGE_HEADERS)

    return router
