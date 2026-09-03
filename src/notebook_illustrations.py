"""Infographic v2 illustrations: one AI image per block, generated async.

Modelled on src/notebook_covers.py: an in-memory ``_active_jobs`` registry,
``asyncio.create_task``, a start call that returns immediately and a viewer
that polls (GET /api/notebooks/{id}/artifacts/{artifact_id}/illustrations).
Jobs do not survive a restart; the artifact stays valid either way (icons,
or whatever illustrations already landed).

Per block: build_illustration_prompt -> do_generate_image (quality "low",
hero 1536x1024, others 1024x1024) -> copy the PNG from GENERATED_IMAGES_DIR
into NOTEBOOK_INFOGRAPHICS_DIR as "<artifact_id>-<block_id>-<hex8>.png" ->
write {"illustrations": {block_id: filename}} back into the artifact's
Document JSON. Persisting after *each* image means a job that dies halfway
keeps what it already produced. Failures are per block: logged, skipped,
counted in ``errors``; the job still ends "done".

Spec: docs/superpowers/specs/2026-09-03-notebooks-infographic-v2-design.md (Deel B).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from core.database import Document, Notebook, NotebookArtifact, SessionLocal
from src.constants import GENERATED_IMAGES_DIR, NOTEBOOK_INFOGRAPHICS_DIR
from src.notebook_infographic import MAX_ILLUSTRATIONS, extract_infographic, iter_blocks

logger = logging.getLogger(__name__)

JOB_TIMEOUT_SECONDS = 300
_JOB_EVICT_AFTER_SECONDS = 1800

_STYLE_SUFFIX = (
    ", flat vector illustration, pastel palette, soft shapes, white background, "
    "no text, no letters"
)
_HERO_SIZE = "1536x1024"
_BLOCK_SIZE = "1024x1024"
_QUALITY = "low"  # deliberately not the admin's image_quality: predictable cost

# "<artifact uuid>-<block slug>-<hex8>.png"
ILLUSTRATION_FILE_RE = re.compile(
    r"^([0-9a-f-]{36})-([a-z0-9][a-z0-9_-]{0,39})-([0-9a-f]{8})\.png$"
)
_GENERATED_NAME_RE = re.compile(r"^[a-f0-9]{8,64}\.(png|jpg|jpeg|webp)$")

ILLUSTRATION_HEADERS = {
    "Cache-Control": "private, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
}

_active_jobs: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def build_illustration_prompt(prompt: str, *, hero: bool) -> str:
    """do_generate_image content: prompt line, blank, size, quality."""
    size = _HERO_SIZE if hero else _BLOCK_SIZE
    return f"{prompt.strip()}{_STYLE_SUFFIX}\n\n{size}\n{_QUALITY}"


def select_illustration_blocks(data: dict) -> list[dict]:
    """First MAX_ILLUSTRATIONS blocks (document order) that carry a prompt."""
    picked = [b for b in iter_blocks(data) if b.get("illustration_prompt")]
    return picked[:MAX_ILLUSTRATIONS]


def load_illustrations(content: str) -> dict[str, str]:
    """block_id -> filename from stored v2 content; {} for anything else."""
    try:
        return dict(extract_infographic(content).get("illustrations") or {})
    except ValueError:
        return {}


def artifact_id_from_filename(filename: str) -> str:
    m = ILLUSTRATION_FILE_RE.fullmatch(filename or "")
    if not m:
        raise ValueError("Invalid illustration filename")
    return m.group(1)


# ---------------------------------------------------------------------------
# Job registry
# ---------------------------------------------------------------------------

def _reap_stale_jobs(now: float) -> None:
    for job_id, entry in list(_active_jobs.items()):
        if entry.get("status") == "running":
            continue
        completed_at = entry.get("completed_at")
        if completed_at is not None and (now - completed_at) > _JOB_EVICT_AFTER_SECONDS:
            _active_jobs.pop(job_id, None)


def _find_job(artifact_id: str) -> Optional[dict]:
    """Newest registry entry for this artifact (running preferred)."""
    best = None
    for entry in _active_jobs.values():
        if entry.get("artifact_id") != artifact_id:
            continue
        if entry.get("status") == "running":
            return entry
        if best is None or (entry.get("started_at") or 0) > (best.get("started_at") or 0):
            best = entry
    return best


def get_artifact_job(artifact_id: str, owner: str) -> Optional[dict]:
    entry = _find_job(artifact_id)
    if entry is None or (entry.get("owner") or "") != (owner or ""):
        return None
    return {
        "status": entry.get("status"),
        "illustrations": dict(entry.get("illustrations") or {}),
        "errors": int(entry.get("errors") or 0),
    }


def _load_artifact_data(session, artifact_id: str, owner: str) -> Optional[tuple[dict, Document]]:
    row = (
        session.query(NotebookArtifact, Document)
        .join(Document, Document.id == NotebookArtifact.document_id)
        .join(Notebook, Notebook.id == NotebookArtifact.notebook_id)
        .filter(NotebookArtifact.id == artifact_id, Notebook.owner == owner)
        .first()
    )
    if row is None:
        return None
    artifact, document = row
    return extract_infographic(document.current_content), document


def start_illustration_job(notebook_id: str, artifact_id: str, owner: str,
                           db_session_factory=None) -> str:
    """Validate, register and schedule an illustration job; return its id.

    Raises ValueError when the artifact is unknown/foreign/not v2, or when a
    job for it is already running. A v2 artifact without any
    illustration_prompt registers an already-"done" job (no task).
    """
    factory = db_session_factory or SessionLocal
    now = time.time()
    _reap_stale_jobs(now)
    running = _find_job(artifact_id)
    if running is not None and running.get("status") == "running":
        raise ValueError("Er loopt al een illustratie-job voor dit artifact")

    session = factory()
    try:
        loaded = _load_artifact_data(session, artifact_id, owner)
        if loaded is None:
            raise ValueError("Artifact niet gevonden")
        data, _document = loaded
    except ValueError as exc:
        if "niet gevonden" in str(exc):
            raise
        raise ValueError(f"Artifact is geen geldige v2-infographic: {exc}") from exc
    finally:
        session.close()

    blocks = select_illustration_blocks(data)
    job_id = uuid.uuid4().hex
    entry = {
        "status": "running" if blocks else "done",
        "owner": owner or "",
        "notebook_id": notebook_id,
        "artifact_id": artifact_id,
        "illustrations": dict(data.get("illustrations") or {}),
        "errors": 0,
        "started_at": now,
        "completed_at": None if blocks else now,
        "task": None,
    }
    _active_jobs[job_id] = entry
    if blocks:
        entry["task"] = asyncio.create_task(_run_job(job_id, artifact_id, owner, factory, blocks))
    return job_id


async def _run_job(job_id: str, artifact_id: str, owner: str, factory, blocks: list[dict]) -> None:
    entry = _active_jobs.get(job_id)
    if entry is None:
        return
    try:
        await asyncio.wait_for(_generate(entry, artifact_id, owner, factory, blocks),
                               timeout=JOB_TIMEOUT_SECONDS)
        entry["status"] = "done"
    except asyncio.CancelledError:
        entry["status"] = "cancelled"
    except asyncio.TimeoutError:
        # Whatever landed before the time-out is already persisted.
        entry["status"] = "done"
        entry["errors"] = int(entry.get("errors") or 0) + 1
        logger.warning("Illustration job %s timed out after %ss", job_id, JOB_TIMEOUT_SECONDS)
    except Exception as exc:
        entry["status"] = "error"
        logger.warning("Illustration job %s failed: %s", job_id, exc, exc_info=True)
    finally:
        entry["completed_at"] = time.time()


async def _generate_image(content: str, owner: str) -> dict:
    """Seam over the image pipeline (monkeypatched in tests)."""
    from src.ai_interaction import do_generate_image
    return await do_generate_image(content, owner=owner)


def _copy_generated(result: dict, artifact_id: str, block_id: str) -> str:
    """Copy the pipeline's PNG into NOTEBOOK_INFOGRAPHICS_DIR; return the filename."""
    if not isinstance(result, dict) or result.get("error"):
        raise RuntimeError(result.get("error", "Onbekende fout") if isinstance(result, dict) else "Onbekende fout")
    source_name = (result.get("image_url") or "").rsplit("/", 1)[-1]
    if not _GENERATED_NAME_RE.fullmatch(source_name):
        raise RuntimeError("Image generation returned unexpected URL format")
    source = Path(GENERATED_IMAGES_DIR) / source_name
    if not source.exists():
        raise RuntimeError(f"Generated image not found: {source_name}")
    dest_dir = Path(NOTEBOOK_INFOGRAPHICS_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{artifact_id}-{block_id}-{uuid.uuid4().hex[:8]}.png"
    shutil.copy2(str(source), str(dest_dir / filename))
    return filename


def _persist(factory, artifact_id: str, owner: str, block_id: str, filename: str) -> bool:
    """Write illustrations[block_id] into the Document JSON. False when the
    artifact is gone (job should stop)."""
    session = factory()
    try:
        loaded = _load_artifact_data(session, artifact_id, owner)
        if loaded is None:
            return False
        data, document = loaded
        data.setdefault("illustrations", {})[block_id] = filename
        document.current_content = json.dumps(data, ensure_ascii=False, indent=2)
        session.commit()
        return True
    finally:
        session.close()


async def _generate(entry: dict, artifact_id: str, owner: str, factory, blocks: list[dict]) -> None:
    for block in blocks:
        block_id = block["id"]
        content = build_illustration_prompt(block["illustration_prompt"], hero=(block["type"] == "hero"))
        try:
            result = await _generate_image(content, owner)
            filename = _copy_generated(result, artifact_id, block_id)
        except Exception as exc:
            entry["errors"] = int(entry.get("errors") or 0) + 1
            logger.warning("Illustration for %s/%s failed: %s", artifact_id, block_id, exc)
            continue
        if not await asyncio.to_thread(_persist, factory, artifact_id, owner, block_id, filename):
            logger.info("Illustration job for %s stopped: artifact gone", artifact_id)
            try:
                (Path(NOTEBOOK_INFOGRAPHICS_DIR) / filename).unlink(missing_ok=True)
            except OSError:
                pass
            return
        entry["illustrations"][block_id] = filename
