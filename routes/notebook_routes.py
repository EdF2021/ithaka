"""Notebook routes — CRUD for notebooks + per-notebook source upload/removal."""

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, UploadFile, File

from core.database import Document, SessionLocal, Notebook, NotebookArtifact, NotebookSource
from core.database import Session as DbSession
from src.auth_helpers import get_current_user
from src.notebook_artifacts import ARTIFACT_KINDS, generate_artifact
from src.notebook_ingest import ingest_notebook_file
from src.upload_limits import PERSONAL_UPLOAD_MAX_BYTES, format_byte_limit

logger = logging.getLogger(__name__)


def _get_owned_notebook(db_session, notebook_id, user):
    nb = db_session.query(Notebook).filter_by(id=notebook_id).first()
    if nb is None or nb.owner != user:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return nb


def setup_notebook_routes(rag_manager) -> APIRouter:
    router = APIRouter(tags=["notebooks"])

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
            artifact_doc_ids = [
                doc_id for (doc_id,) in
                db_session.query(NotebookArtifact.document_id).filter_by(notebook_id=nb.id).all()
            ]
            if artifact_doc_ids:
                (db_session.query(NotebookArtifact)
                 .filter_by(notebook_id=nb.id)
                 .delete(synchronize_session=False))
                (db_session.query(Document)
                 .filter(Document.id.in_(artifact_doc_ids))
                 .delete(synchronize_session=False))
            db_session.delete(nb)
            db_session.commit()
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
        body = await request.json()
        kind = body.get("kind")
        if kind not in ARTIFACT_KINDS:
            raise HTTPException(status_code=400, detail=f"Onbekend artifact-type: {kind}")
        db_session = SessionLocal()
        try:
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
            db_session.delete(artifact)
            db_session.flush()
            if doc is not None:
                db_session.delete(doc)
            db_session.commit()
            return {"success": True}
        finally:
            db_session.close()

    return router
