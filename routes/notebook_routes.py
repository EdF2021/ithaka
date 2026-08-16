"""Notebook routes — CRUD for notebooks + per-notebook source upload/removal."""

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, UploadFile, File

from core.database import SessionLocal, Notebook, NotebookSource
from core.database import Session as DbSession
from src.auth_helpers import get_current_user
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

    return router
