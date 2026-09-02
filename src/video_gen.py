"""Veo 3.1 video generation: async jobs that poll a Gemini long-running operation.

Mirrors src/notebook_audio.py (in-memory `_active_jobs` + `asyncio.create_task`,
tempfile + `os.replace` publish) and src/notebook_video.py (path-safe file
serving via a strict filename whitelist + commonpath guard, hourly janitor).

Unlike the notebook jobs, a video job is not tied to a notebook and has no DB
row of its own — the only durable trace of a job after a restart is the
published `<job_id>.mp4` plus a sidecar `<job_id>.owner` text file written
right after publish. `get_job` uses that sidecar to stay restart-proof without
ever trusting an unauthenticated caller: a job unknown to `_active_jobs` is
only ever reported as done when the on-disk owner file matches the caller.

Design docs: docs/superpowers/specs/2026-09-02-image-video-autoroute-design.md
and docs/superpowers/plans/2026-09-02-image-video-autoroute.md (Task 2).

The Gemini API key is never logged and never appears in an error message —
error text is at most "HTTP <status>: <first 200 chars of the response body>".
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

import httpx
from fastapi import HTTPException

from src.constants import VIDEO_DIR

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Models, pricing, filenames
# --------------------------------------------------------------------------

VEO_MODELS = (
    "veo-3.1-generate-preview",
    "veo-3.1-fast-generate-preview",
    "veo-3.1-lite-generate-preview",
)

# USD per second of rendered 720p video. Informational only — used to show a
# cost estimate before/while a job runs; never used to bill or throttle.
VEO_PRICE_PER_SECOND_720P = {
    "veo-3.1-generate-preview": 0.40,
    "veo-3.1-fast-generate-preview": 0.10,
    "veo-3.1-lite-generate-preview": 0.05,
}

VIDEO_FILENAME_RE = re.compile(r"^[a-f0-9]{32}\.mp4$")
VIDEO_HEADERS = {
    # Every generation gets a fresh uuid4 filename, so immutable is safe.
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
}

# Poll cadence and wall-clock cap for a single job (spec B1).
VIDEO_POLL_INTERVAL_SECONDS = 10
VIDEO_POLL_MAX_SECONDS = 600

_ERROR_BODY_SNIPPET_CHARS = 200


# --------------------------------------------------------------------------
# HTTP client + error text
# --------------------------------------------------------------------------

def _make_client() -> httpx.AsyncClient:
    """Default client for Veo calls. Module-level seam so tests + the job
    runner can share one client per job instead of opening one per call."""
    return httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)


def _error_text(resp: httpx.Response) -> str:
    """Status code + first 200 chars of the body. Never the API key — the key
    only ever appears in a request header, which this never reads."""
    try:
        body = resp.text
    except Exception:
        body = ""
    return f"HTTP {resp.status_code}: {body[:_ERROR_BODY_SNIPPET_CHARS]}"


# --------------------------------------------------------------------------
# Gemini endpoint resolution
# --------------------------------------------------------------------------

def resolve_gemini_endpoint(db_session_factory=None) -> tuple[str, str]:
    """First enabled ModelEndpoint pointing at the Gemini API.

    Returns (base_url without a trailing /openai suffix, api_key).
    Raises RuntimeError("Geen Gemini-endpoint met API-key") when none is
    configured or none of them carries a key.
    """
    from core.database import ModelEndpoint, SessionLocal

    factory = db_session_factory or SessionLocal
    db = factory()
    try:
        rows = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all()  # noqa: E712
        for ep in rows:
            base = (getattr(ep, "base_url", "") or "").rstrip("/")
            api_key = getattr(ep, "api_key", None)
            if "generativelanguage.googleapis.com" not in base or not api_key:
                continue
            if base.endswith("/openai"):
                base = base[: -len("/openai")]
            return base, api_key
    finally:
        db.close()
    raise RuntimeError("Geen Gemini-endpoint met API-key")


# --------------------------------------------------------------------------
# Veo REST calls
# --------------------------------------------------------------------------

async def start_generation(
    base_url: str,
    api_key: str,
    prompt: str,
    *,
    model: str,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
    duration_seconds: int = 8,
    negative_prompt: str = "",
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """POST {base}/models/{model}:predictLongRunning. Returns the operation name.

    Raises RuntimeError on a non-2xx response (see `_error_text`) or when the
    response carries no operation name.
    """
    parameters = {
        "aspectRatio": aspect_ratio,
        "resolution": resolution,
        "durationSeconds": str(duration_seconds),
        "numberOfVideos": 1,
    }
    if negative_prompt:
        parameters["negativePrompt"] = negative_prompt
    body = {"instances": [{"prompt": prompt}], "parameters": parameters}

    owns_client = client is None
    c = client or _make_client()
    try:
        resp = await c.post(
            f"{base_url}/models/{model}:predictLongRunning",
            headers={"x-goog-api-key": api_key},
            json=body,
        )
    finally:
        if owns_client:
            await c.aclose()

    if resp.status_code >= 400:
        raise RuntimeError(_error_text(resp))
    data = resp.json()
    name = data.get("name")
    if not name:
        raise RuntimeError("Veo gaf geen operation-name terug")
    return name


async def poll_operation(
    base_url: str,
    api_key: str,
    operation_name: str,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """GET {base}/{operation_name}.

    Returns {"done": bool, "video_uri": str|None, "error": str|None,
    "blocked": bool}. `blocked` is True when Google reports the operation
    done with no generated sample (safety-filtered, not billed per Google's
    docs). Raises RuntimeError on a non-2xx response.
    """
    owns_client = client is None
    c = client or _make_client()
    try:
        resp = await c.get(f"{base_url}/{operation_name}", headers={"x-goog-api-key": api_key})
    finally:
        if owns_client:
            await c.aclose()

    if resp.status_code >= 400:
        raise RuntimeError(_error_text(resp))
    data = resp.json()

    if not data.get("done"):
        return {"done": False, "video_uri": None, "error": None, "blocked": False}

    err = data.get("error")
    if err:
        message = (err or {}).get("message") or "Onbekende Veo-fout"
        return {"done": True, "video_uri": None, "error": message, "blocked": False}

    samples = (
        ((data.get("response") or {}).get("generateVideoResponse") or {})
        .get("generatedSamples", [])
    )
    if not samples:
        return {"done": True, "video_uri": None, "error": None, "blocked": True}

    uri = (samples[0].get("video") or {}).get("uri")
    return {"done": True, "video_uri": uri, "error": None, "blocked": False}


async def download_video(
    api_key: str,
    uri: str,
    dest_path,
    client: Optional[httpx.AsyncClient] = None,
) -> int:
    """Stream `uri` to `dest_path`, following redirects. Returns bytes written.

    Raises RuntimeError on a non-2xx response.
    """
    owns_client = client is None
    c = client or _make_client()
    try:
        written = 0
        async with c.stream("GET", uri, headers={"x-goog-api-key": api_key}) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                snippet = body[:_ERROR_BODY_SNIPPET_CHARS].decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {resp.status_code}: {snippet}")
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
                    written += len(chunk)
        return written
    finally:
        if owns_client:
            await c.aclose()


def estimate_cost_usd(model: str, duration_seconds: int, resolution: str = "720p") -> float:
    """duration x per-second-720p rate for `model`; falls back to the default
    model's rate for an unknown model name rather than raising — this is an
    informational estimate, never a billing gate."""
    rate = VEO_PRICE_PER_SECOND_720P.get(model, VEO_PRICE_PER_SECOND_720P["veo-3.1-generate-preview"])
    return round(rate * float(duration_seconds), 2)


# --------------------------------------------------------------------------
# File serving
# --------------------------------------------------------------------------

def resolve_video_path(filename: str) -> Path:
    """Map a video filename to its on-disk path, or raise HTTPException.

    Mirror of notebook_video.resolve_notebook_video_path: strict whitelist
    regex plus a commonpath guard; VIDEO_DIR read from the module attribute
    on every call (never bound at import) so tests can point it elsewhere.
    Ownership is not checked here — the caller (the route) does that via
    `get_job`.
    """
    if not isinstance(filename, str) or not VIDEO_FILENAME_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    directory = Path(VIDEO_DIR)
    root = directory.resolve()
    path = (directory / filename).resolve()
    try:
        if os.path.commonpath([str(root), str(path)]) != str(root):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    return path


# --------------------------------------------------------------------------
# Job registry (in-memory, mirrors notebook_audio._active_jobs)
# --------------------------------------------------------------------------

_active_jobs: dict[str, dict] = {}

# Terminal entries older than this are dropped the next time a job starts —
# _active_jobs is unbounded otherwise. Same value/reasoning as notebook_audio.
_JOB_EVICT_AFTER_SECONDS = 1800

_PUBLIC_JOB_FIELDS = (
    "status", "prompt", "model", "error", "video_url",
    "cost_estimate", "started_at", "completed_at",
)


def _reap_stale_jobs(now: float) -> None:
    for job_id, entry in list(_active_jobs.items()):
        if entry.get("status") == "running":
            continue
        completed_at = entry.get("completed_at")
        if completed_at is not None and (now - completed_at) > _JOB_EVICT_AFTER_SECONDS:
            _active_jobs.pop(job_id, None)


def _owner_file_path(job_id: str) -> Path:
    return Path(VIDEO_DIR) / f"{job_id}.owner"


def get_job(job_id: str, owner: str) -> Optional[dict]:
    """Return a snapshot of a job, or None for unknown id / wrong owner.

    Restart-proof: when `job_id` is not in `_active_jobs` (process restart
    lost the in-memory entry) but VIDEO_DIR/<job_id>.mp4 exists on disk with a
    matching `<job_id>.owner` sidecar, this reports it as done rather than
    unknown.
    """
    entry = _active_jobs.get(job_id)
    if entry is not None:
        if (entry.get("owner") or "") != (owner or ""):
            return None
        snapshot = {field: entry.get(field) for field in _PUBLIC_JOB_FIELDS}
        snapshot["job_id"] = job_id
        return snapshot

    # Not tracked in memory — only a well-formed job id can map to a file at
    # all, so reject anything else before touching the filesystem.
    if not VIDEO_FILENAME_RE.fullmatch(f"{job_id}.mp4"):
        return None
    video_path = Path(VIDEO_DIR) / f"{job_id}.mp4"
    owner_path = _owner_file_path(job_id)
    if not video_path.is_file() or not owner_path.is_file():
        return None
    try:
        stored_owner = owner_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if stored_owner != (owner or ""):
        return None
    try:
        mtime = video_path.stat().st_mtime
    except OSError:
        mtime = None
    return {
        "job_id": job_id,
        "status": "done",
        "prompt": None,
        "model": None,
        "error": None,
        "video_url": f"/api/video/{job_id}.mp4",
        "cost_estimate": None,
        "started_at": mtime,
        "completed_at": mtime,
    }


# --------------------------------------------------------------------------
# Job runner
# --------------------------------------------------------------------------

def start_video_job(
    prompt: str,
    owner: str,
    *,
    model: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    resolution: Optional[str] = None,
    db_session_factory=None,
) -> str:
    """Validate, register and schedule a Veo video job; return its job id.

    Defaults for any omitted parameter come from src.settings. Raises
    ValueError for bad parameters (empty prompt, unknown model/aspect
    ratio/duration) and RuntimeError when no Gemini endpoint is configured
    (checked eagerly, before the job is registered, so a misconfigured
    install fails fast instead of after minutes of polling).
    """
    from src.settings import get_setting

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Prompt mag niet leeg zijn")

    model = model or get_setting("video_model", "veo-3.1-generate-preview")
    if model not in VEO_MODELS:
        raise ValueError(f"Onbekend video-model: {model}")

    aspect_ratio = aspect_ratio or get_setting("video_aspect_ratio", "16:9")
    if aspect_ratio not in ("16:9", "9:16"):
        raise ValueError(f"Ongeldige aspect ratio: {aspect_ratio}")

    resolution = resolution or get_setting("video_resolution", "720p")

    if duration_seconds is None:
        duration_seconds = get_setting("video_duration_seconds", 8)
    try:
        duration_seconds = int(duration_seconds)
    except (TypeError, ValueError):
        raise ValueError(f"Ongeldige duur: {duration_seconds}")
    if duration_seconds not in (4, 6, 8):
        raise ValueError(f"Ongeldige duur: {duration_seconds}")

    # Fail fast: no endpoint configured raises RuntimeError before the job is
    # even registered, rather than surfacing minutes later as a job error.
    resolve_gemini_endpoint(db_session_factory)

    now = time.time()
    _reap_stale_jobs(now)

    job_id = uuid.uuid4().hex
    entry = {
        "status": "running",
        "prompt": prompt,
        "model": model,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "duration_seconds": duration_seconds,
        "error": None,
        "video_url": None,
        "cost_estimate": estimate_cost_usd(model, duration_seconds, resolution),
        # SECURITY: ownership is tracked so every read can filter by user.
        "owner": owner or "",
        "started_at": now,
        "completed_at": None,
        "task": None,
    }
    _active_jobs[job_id] = entry
    task = asyncio.create_task(_run_job(job_id, db_session_factory))
    # Hold the reference: a bare create_task result can be garbage-collected
    # while still running.
    entry["task"] = task
    return job_id


async def _download_with_retry(api_key: str, uri: str, dest_path, client: httpx.AsyncClient) -> int:
    """download_video with one retry, per spec ('Download faalt -> retry 1x')."""
    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            return await download_video(api_key, uri, dest_path, client=client)
        except Exception as exc:  # noqa: BLE001 - genuinely any transport/IO failure retries once
            last_exc = exc
            logger.warning("Video download attempt %d/2 failed: %s", attempt + 1, exc)
    raise RuntimeError(f"Download van de video is mislukt: {last_exc}")


async def _run_job(job_id: str, db_session_factory) -> None:
    """Job wrapper: the single place a job's terminal status is recorded."""
    entry = _active_jobs.get(job_id)
    if entry is None:
        return
    try:
        await _generate(job_id, entry, db_session_factory)
    except asyncio.CancelledError:
        entry["status"] = "error"
        entry["error"] = "Generatie afgebroken"
        raise
    except Exception as exc:
        logger.error("Video job %s failed: %s", job_id, exc, exc_info=True)
        entry["status"] = "error"
        entry["error"] = str(exc) or exc.__class__.__name__
    finally:
        entry["completed_at"] = time.time()


async def _generate(job_id: str, entry: dict, db_session_factory) -> None:
    """start -> poll every VIDEO_POLL_INTERVAL_SECONDS up to VIDEO_POLL_MAX_SECONDS
    -> download -> publish. Raises on any failure; the caller (_run_job)
    records it as the job's terminal error."""
    base_url, api_key = resolve_gemini_endpoint(db_session_factory)
    client = _make_client()
    try:
        operation_name = await start_generation(
            base_url, api_key, entry["prompt"],
            model=entry["model"],
            aspect_ratio=entry["aspect_ratio"],
            resolution=entry["resolution"],
            duration_seconds=entry["duration_seconds"],
            client=client,
        )

        deadline = time.time() + VIDEO_POLL_MAX_SECONDS
        result = await poll_operation(base_url, api_key, operation_name, client=client)
        while not result["done"]:
            if time.time() >= deadline:
                raise RuntimeError("Time-out")
            await asyncio.sleep(VIDEO_POLL_INTERVAL_SECONDS)
            result = await poll_operation(base_url, api_key, operation_name, client=client)

        if result.get("blocked"):
            raise RuntimeError("Geblokkeerd door Veo safety-filter — niet gefactureerd")
        if result.get("error"):
            raise RuntimeError(result["error"])
        uri = result.get("video_uri")
        if not uri:
            raise RuntimeError("Veo gaf geen video-URI terug")

        directory = Path(VIDEO_DIR)
        final_path = directory / f"{job_id}.mp4"
        temp_path = directory / f".video-{job_id}.tmp"
        try:
            written = await _download_with_retry(api_key, uri, temp_path, client)
            if written <= 0:
                raise RuntimeError("Download leverde geen data op")
            # Atomic publish: no reader ever sees a half-written file.
            os.replace(temp_path, final_path)
        except OSError as exc:
            raise RuntimeError(f"Kon de video niet opslaan in {directory} ({exc})") from exc
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

        # Written right after publish so a restart-proof get_job can attribute
        # the file without ever trusting an unauthenticated caller.
        _owner_file_path(job_id).write_text(entry.get("owner") or "", encoding="utf-8")

        entry["video_url"] = f"/api/video/{job_id}.mp4"
        entry["status"] = "done"
    finally:
        await client.aclose()


# --------------------------------------------------------------------------
# Janitor
#
# Unlike the notebook audio/video janitors, a Veo job has no DB row to
# cross-reference against (job records live only in `_active_jobs`, which is
# itself in-memory and reaped long before this runs) — age is the only
# signal. Safe to call any time: a job mid-download or just-published is
# younger than any realistic max_age_seconds.
# --------------------------------------------------------------------------

def cleanup_orphaned_videos(*, max_age_seconds: int = 7 * 24 * 3600) -> tuple[int, int]:
    """Delete stale `.video-*.tmp`, `<hex>.mp4` and `<hex>.owner` files from
    VIDEO_DIR older than `max_age_seconds`. Returns (removed, kept)."""
    directory = Path(VIDEO_DIR)
    if not directory.is_dir():
        return (0, 0)

    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        logger.debug("Video-janitor: kon %s niet lezen (%s)", directory, exc)
        return (0, 0)

    now = time.time()
    removed = 0
    kept = 0
    for path in entries:
        if not path.is_file():
            continue
        name = path.name
        is_tmp = name.startswith(".video-") and name.endswith(".tmp")
        is_mp4 = bool(VIDEO_FILENAME_RE.fullmatch(name))
        is_owner = name.endswith(".owner")
        if not (is_tmp or is_mp4 or is_owner):
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age <= max_age_seconds:
            kept += 1
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.debug("Video-janitor: kon %s niet verwijderen (%s)", name, exc)

    if removed:
        logger.info("Video-janitor: %d verweesd bestand(en) opgeruimd", removed)
    else:
        logger.debug("Video-janitor: niets om op te ruimen")

    return removed, kept
