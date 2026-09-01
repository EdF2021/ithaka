"""Generate AI cover images for notebook cards.

Pipeline: build a prompt from the notebook name/description → call the
existing image generation pipeline (src.ai_interaction.do_generate_image)
→ save the image to NOTEBOOK_COVERS_DIR → store the filename on the
Notebook.cover_image column.

Mirrors the podcast job pattern (src/notebook_audio.py): an in-memory
``_active_jobs`` dict, ``asyncio.create_task``, a POST that returns
immediately and a UI that polls. Jobs do not survive a restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from core.database import Notebook, SessionLocal
from src.constants import NOTEBOOK_COVERS_DIR

logger = logging.getLogger(__name__)

JOB_TIMEOUT_SECONDS = 300

_active_jobs: dict[str, dict] = {}

_JOB_EVICT_AFTER_SECONDS = 1800

_PUBLIC_JOB_FIELDS = (
    "status", "phase", "error", "cover_image", "notebook_id", "started_at",
)

COVER_IMAGE_RE = re.compile(
    r"^[a-f0-9]{8,64}\.(png|jpg|jpeg|webp)$"
)

COVER_IMAGE_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
}


def _reap_stale_jobs(now: float) -> None:
    for job_id, entry in list(_active_jobs.items()):
        if entry.get("status") == "running":
            continue
        completed_at = entry.get("completed_at")
        if completed_at is not None and (now - completed_at) > _JOB_EVICT_AFTER_SECONDS:
            _active_jobs.pop(job_id, None)


def get_job(job_id: str, owner: str) -> Optional[dict]:
    entry = _active_jobs.get(job_id)
    if entry is None:
        return None
    if (entry.get("owner") or "") != (owner or ""):
        return None
    return {field: entry.get(field) for field in _PUBLIC_JOB_FIELDS}


def _build_prompt(name: str, description: str) -> str:
    """Build an image-gen prompt from the notebook name and description.

    The prompt asks for a photorealistic, atmospheric image that captures the
    topic of the notebook — the same style as Google NotebookLM's cover photos.
    """
    parts = [f"Create a photorealistic cover image for a notebook titled \"{name}\"."]
    if description and description.strip():
        parts.append(f"The notebook is about: {description.strip()}.")
    parts.append(
        "Style: atmospheric, professional, high-quality photograph. "
        "No text, no letters, no words, no watermarks. "
        "The image should evoke the subject matter through visual metaphor "
        "or a literal scene — objects, landscapes, textures, or abstract "
        "composition. Wide aspect ratio, suitable for a card banner."
    )
    return " ".join(parts)


def start_cover_job(notebook_id: str, owner: str, db_session_factory=None) -> str:
    """Validate, register and schedule a cover-image job; return its job id.

    Raises ValueError for notebook-not-found or already-running.
    """
    factory = db_session_factory or SessionLocal

    now = time.time()
    _reap_stale_jobs(now)
    for entry in _active_jobs.values():
        if (entry.get("status") == "running"
                and entry.get("owner") == owner
                and entry.get("notebook_id") == notebook_id):
            raise ValueError("Er loopt al een cover-generatie voor dit notebook")

    session = factory()
    try:
        notebook = (
            session.query(Notebook)
            .filter(Notebook.id == notebook_id, Notebook.owner == owner)
            .first()
        )
        if notebook is None:
            raise ValueError("Notebook niet gevonden")
        name = notebook.name
        description = notebook.description or ""
    finally:
        session.close()

    job_id = uuid.uuid4().hex
    entry = {
        "status": "running",
        "phase": "generating",
        "error": None,
        "cover_image": None,
        "owner": owner or "",
        "notebook_id": notebook_id,
        "started_at": now,
        "completed_at": None,
        "task": None,
        # Copied out before the session closes (same pattern as podcast).
        "_name": name,
        "_description": description,
    }
    _active_jobs[job_id] = entry
    task = asyncio.create_task(_run_job(job_id, notebook_id, owner, factory, name, description))
    entry["task"] = task
    return job_id


async def _run_job(job_id: str, notebook_id: str, owner: str, factory,
                   name: str, description: str) -> None:
    entry = _active_jobs.get(job_id)
    if entry is None:
        return
    try:
        await asyncio.wait_for(
            _generate(job_id, notebook_id, owner, factory, name, description),
            timeout=JOB_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        entry["status"] = "cancelled"
        entry["completed_at"] = time.time()
    except asyncio.TimeoutError:
        entry["status"] = "error"
        entry["error"] = "Time-out bij genereren cover-image"
        entry["completed_at"] = time.time()
    except Exception as exc:
        entry["status"] = "error"
        entry["error"] = str(exc)[:500]
        entry["completed_at"] = time.time()
        logger.warning("Cover-image job %s failed: %s", job_id, exc, exc_info=True)


async def _generate(job_id: str, notebook_id: str, owner: str, factory,
                    name: str, description: str) -> None:
    entry = _active_jobs.get(job_id)
    if entry is None:
        return

    prompt = _build_prompt(name, description)

    # Use the existing image generation pipeline — same code path as the
    # generate_image agent tool. do_generate_image reads admin settings for
    # model/quality defaults and auto-detects the best available image model.
    from src.ai_interaction import do_generate_image
    content = f"{prompt}\n\n1024x1024\nlow"
    result = await do_generate_image(content, owner=owner)

    if not isinstance(result, dict) or result.get("error"):
        error = result.get("error", "Onbekende fout") if isinstance(result, dict) else "Onbekende fout"
        raise RuntimeError(error)

    image_url = result.get("image_url", "")
    # image_url is "/api/generated-image/<filename>" — extract the filename
    # and copy it into NOTEBOOK_COVERS_DIR so it's independent of the gallery.
    source_filename = image_url.rsplit("/", 1)[-1] if image_url else ""
    if not source_filename or not COVER_IMAGE_RE.fullmatch(source_filename):
        raise RuntimeError("Image generation returned unexpected URL format")

    from src.constants import GENERATED_IMAGES_DIR
    source_path = Path(GENERATED_IMAGES_DIR) / source_filename
    if not source_path.exists():
        raise RuntimeError(f"Generated image not found: {source_filename}")

    ext = source_filename.rsplit(".", 1)[-1].lower()
    cover_filename = f"{uuid.uuid4().hex[:12]}.{ext}"
    covers_dir = Path(NOTEBOOK_COVERS_DIR)
    covers_dir.mkdir(parents=True, exist_ok=True)
    dest_path = covers_dir / cover_filename

    # Copy (not move) — the original stays in the gallery for the agent tool.
    import shutil
    shutil.copy2(str(source_path), str(dest_path))

    # Persist the filename on the notebook row.
    session = factory()
    try:
        notebook = (
            session.query(Notebook)
            .filter(Notebook.id == notebook_id, Notebook.owner == owner)
            .first()
        )
        if notebook is None:
            raise RuntimeError("Notebook niet gevonden tijdens opslaan")
        # Best-effort cleanup of the previous cover file.
        old_cover = notebook.cover_image
        notebook.cover_image = cover_filename
        session.commit()
    finally:
        session.close()

    # Clean up the old cover file (if any) after the commit succeeds.
    if old_cover and COVER_IMAGE_RE.fullmatch(old_cover):
        try:
            old_path = covers_dir / old_cover
            old_path.unlink(missing_ok=True)
        except Exception:
            pass

    entry["status"] = "done"
    entry["cover_image"] = cover_filename
    entry["completed_at"] = time.time()


def resolve_cover_image_path(filename: str) -> Path:
    """Resolve and validate a cover-image filename. Raises HTTPException(400/404)."""
    from fastapi import HTTPException
    if not isinstance(filename, str) or not COVER_IMAGE_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    root = Path(NOTEBOOK_COVERS_DIR).resolve()
    path = (Path(NOTEBOOK_COVERS_DIR) / filename).resolve()
    try:
        if os.path.commonpath([str(root), str(path)]) != str(root):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Cover image not found")
    return path