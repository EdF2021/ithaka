"""Veo video-generation routes: start a job, poll its status, serve the mp4.

Mirrors the notebook podcast/video routes in routes/notebook_routes.py (auth +
ownership + FileResponse for a generated media file) and the
`require_privilege(request, "can_generate_images")` gate on the image-gen
endpoints in routes/gallery/gallery_routes.py — this uses the analogous
`can_generate_videos` privilege.

Ownership for the serve route is delegated entirely to
`src.video_gen.get_job`, which is itself restart-proof (it falls back to an
on-disk `<job_id>.owner` sidecar when the in-memory job registry has been
lost to a restart) — the route never re-implements that check.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from src import video_gen
from src.auth_helpers import get_current_user, require_privilege
from src.settings import get_setting


def setup_video_routes() -> APIRouter:
    router = APIRouter(tags=["video"])

    # ---- POST /api/video/generate ----
    @router.post("/api/video/generate", status_code=202)
    async def generate_video(request: Request):
        user = require_privilege(request, "can_generate_videos")

        if not get_setting("video_gen_enabled", False):
            raise HTTPException(status_code=400, detail="Video generation is not enabled")

        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="Prompt mag niet leeg zijn")

        aspect_ratio = body.get("aspect_ratio")
        duration_seconds = body.get("duration_seconds")

        try:
            job_id = video_gen.start_video_job(
                prompt, user,
                aspect_ratio=aspect_ratio,
                duration_seconds=duration_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        job = video_gen.get_job(job_id, user)
        cost_estimate = job.get("cost_estimate") if job else None
        return {"job_id": job_id, "cost_estimate": cost_estimate}

    # ---- GET /api/video/jobs/{job_id} ----
    @router.get("/api/video/jobs/{job_id}")
    async def get_video_job(request: Request, job_id: str):
        user = get_current_user(request)
        job = video_gen.get_job(job_id, user)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    # ---- GET /api/video/{filename} ----
    @router.get("/api/video/{filename}")
    async def serve_video(request: Request, filename: str):
        user = get_current_user(request)
        # 400 on a malformed name, 404 when the file itself is absent.
        path = video_gen.resolve_video_path(filename)

        job_id = filename[: -len(".mp4")] if filename.endswith(".mp4") else filename
        job = video_gen.get_job(job_id, user)
        # get_job already proves ownership (in-memory entry, or the on-disk
        # .owner sidecar after a restart) — never confirm the file's
        # existence to anyone whose job/owner check doesn't match it.
        if job is None or job.get("status") != "done" or job.get("video_url") != f"/api/video/{filename}":
            raise HTTPException(status_code=404, detail="Video not found")

        # FileResponse serves Range/206 natively, so the <video> element can
        # seek without any extra work here.
        return FileResponse(str(path), media_type="video/mp4", headers=video_gen.VIDEO_HEADERS)

    return router
