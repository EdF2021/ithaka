"""generate_video — builtin tool that starts an async Veo video-generation job.

Mirrors the generate_image flow (Deel A of the auto-routing design), but
video generation is genuinely long-running (a Veo job takes 1-3 minutes), so
this tool never waits for it: it starts the job via ``src.video_gen`` and
returns immediately with a job id the frontend polls
(``GET /api/video/jobs/{job_id}``).

``src.video_gen`` is built in parallel (see docs/superpowers/plans/
2026-09-02-image-video-autoroute.md, Task 2/B) and may not exist yet when
this module is imported — e.g. while these tasks are still landing
independently — so the import is defensive and ``execute`` degrades to a
clear error dict instead of raising ImportError at module load time.
"""
import json
from typing import Any, Dict

from src.settings import get_user_setting

try:
    from src import video_gen
except ImportError:  # pragma: no cover - backend PR not landed yet
    video_gen = None

DEFAULT_VIDEO_MODEL = "veo-3.1-generate-preview"
DEFAULT_VIDEO_ASPECT_RATIO = "16:9"
DEFAULT_VIDEO_RESOLUTION = "720p"
DEFAULT_VIDEO_DURATION_SECONDS = 8


class GenerateVideoTool:
    async def execute(self, content: str, ctx: dict) -> Dict[str, Any]:
        owner = ctx.get("owner") if isinstance(ctx, dict) else None

        raw = (content or "").strip()
        prompt = ""
        aspect_ratio = None
        duration_seconds = None
        parsed_json_object = False
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    parsed_json_object = True
                    prompt = str(parsed.get("prompt") or "").strip()
                    ar = parsed.get("aspect_ratio")
                    if isinstance(ar, str) and ar.strip():
                        aspect_ratio = ar.strip()
                    ds = parsed.get("duration_seconds")
                    if isinstance(ds, (int, float)) and not isinstance(ds, bool) and ds > 0:
                        duration_seconds = int(ds)
            except json.JSONDecodeError:
                prompt = ""
        if not prompt and not parsed_json_object:
            # Not a JSON object at all (or not valid JSON) — the whole text is
            # the prompt, same fallback as WebFetchTool/WebSearchTool. But a
            # JSON object that DID parse with an empty/missing "prompt" must
            # not fall through here — sending the raw JSON blob as the prompt
            # would silently generate a video of a JSON string instead of
            # rejecting it (the class of bug generate_image's native-tool test
            # suite warns about, see tests/test_generate_image_inline_tool.py).
            prompt = raw

        prompt = prompt.strip()
        if not prompt:
            return {
                "error": "generate_video: provide a prompt describing the video",
                "exit_code": 1,
            }

        if not get_user_setting("video_gen_enabled", owner or "", False):
            return {
                "error": "Video generation is disabled by the administrator.",
                "exit_code": 1,
            }

        if video_gen is None:
            return {
                "error": "Video generation backend is not available.",
                "exit_code": 1,
            }

        model = get_user_setting("video_model", owner or "", DEFAULT_VIDEO_MODEL)
        resolution = get_user_setting("video_resolution", owner or "", DEFAULT_VIDEO_RESOLUTION)
        effective_aspect_ratio = aspect_ratio or get_user_setting("video_aspect_ratio", owner or "", DEFAULT_VIDEO_ASPECT_RATIO)
        if duration_seconds is not None:
            effective_duration = duration_seconds
        else:
            # Settings are admin/user-editable (Task 4's <select> posts through
            # POST /api/auth/settings) and may round-trip as a string; the
            # backend documents ValueError on invalid params, so normalize here
            # rather than pass an unvalidated str/other type through.
            _setting_duration = get_user_setting("video_duration_seconds", owner or "", DEFAULT_VIDEO_DURATION_SECONDS)
            try:
                effective_duration = int(_setting_duration)
            except (TypeError, ValueError):
                effective_duration = DEFAULT_VIDEO_DURATION_SECONDS

        try:
            job_id = video_gen.start_video_job(
                prompt,
                owner,
                model=model,
                aspect_ratio=effective_aspect_ratio,
                duration_seconds=effective_duration,
                resolution=resolution,
            )
        except RuntimeError as e:
            return {"error": str(e), "exit_code": 1}
        except ValueError as e:
            return {"error": f"generate_video: invalid parameters: {e}", "exit_code": 1}
        except Exception as e:  # pragma: no cover - defensive, mirrors other tools
            return {"error": f"generate_video: {type(e).__name__}: {e}", "exit_code": 1}

        try:
            cost_estimate = video_gen.estimate_cost_usd(model, effective_duration, resolution)
        except Exception:
            cost_estimate = 0.0

        return {
            "output": (
                f"Video generation started ({model}, {effective_duration}s, "
                f"~${cost_estimate:.2f}). It renders inline when ready."
            ),
            "video_job_id": job_id,
            "video_model": model,
            "video_status": "running",
            "video_cost_estimate": cost_estimate,
            "video_url": None,
            "exit_code": 0,
        }
