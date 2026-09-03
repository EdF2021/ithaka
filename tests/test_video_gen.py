"""src/video_gen.py: Veo REST client, job registry, path resolver, janitor.

httpx calls are mocked via httpx.MockTransport (never a real network call);
asyncio.sleep is patched in the job-lifecycle tests so they run instantly.
VIDEO_DIR is monkeypatched to a tmp_path per test — video_gen reads it as a
module attribute on every call (never binds it at import), mirroring
src/notebook_audio.py's NOTEBOOK_AUDIO_DIR pattern.
"""
import asyncio
import json
import time

import httpx
import pytest
from fastapi import HTTPException

from src import video_gen


def _transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------
# start_generation
# --------------------------------------------------------------------------

async def test_start_generation_posts_predict_long_running():
    seen = {}

    def h(req):
        seen["url"] = str(req.url)
        seen["key"] = req.headers.get("x-goog-api-key")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"name": "models/veo-3.1-generate-preview/operations/abc"})

    async with _transport(h) as c:
        op = await video_gen.start_generation(
            "https://generativelanguage.googleapis.com/v1beta", "KEY", "a cat",
            model="veo-3.1-generate-preview", client=c,
        )
    assert op == "models/veo-3.1-generate-preview/operations/abc"
    assert seen["url"].endswith("/v1beta/models/veo-3.1-generate-preview:predictLongRunning")
    assert seen["key"] == "KEY"
    assert seen["body"]["instances"][0]["prompt"] == "a cat"
    assert seen["body"]["parameters"]["durationSeconds"] == 8
    assert seen["body"]["parameters"]["aspectRatio"] == "16:9"
    assert "negativePrompt" not in seen["body"]["parameters"]


async def test_start_generation_includes_negative_prompt_when_set():
    seen = {}

    def h(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"name": "op/1"})

    async with _transport(h) as c:
        await video_gen.start_generation(
            "https://g/v1beta", "K", "a cat", model="m", negative_prompt="blurry", client=c,
        )
    assert seen["body"]["parameters"]["negativePrompt"] == "blurry"


async def test_start_generation_error_status_raises_without_leaking_key():
    def h(req):
        return httpx.Response(403, text="permission denied for key abc123")

    async with _transport(h) as c:
        with pytest.raises(RuntimeError) as exc_info:
            await video_gen.start_generation("https://g/v1beta", "SECRET-KEY", "x", model="m", client=c)
    message = str(exc_info.value)
    assert "403" in message
    assert "SECRET-KEY" not in message


async def test_start_generation_missing_name_raises():
    def h(req):
        return httpx.Response(200, json={})

    async with _transport(h) as c:
        with pytest.raises(RuntimeError):
            await video_gen.start_generation("https://g/v1beta", "K", "x", model="m", client=c)


# --------------------------------------------------------------------------
# poll_operation
# --------------------------------------------------------------------------

async def test_poll_operation_running():
    def h(req):
        return httpx.Response(200, json={"done": False})

    async with _transport(h) as c:
        r = await video_gen.poll_operation("https://g/v1beta", "K", "op", client=c)
    assert r == {"done": False, "video_uri": None, "error": None, "blocked": False, "blocked_reason": None}


async def test_poll_operation_done_with_uri():
    def h(req):
        return httpx.Response(200, json={
            "done": True,
            "response": {"generateVideoResponse": {"generatedSamples": [{"video": {"uri": "https://x/v.mp4"}}]}},
        })

    async with _transport(h) as c:
        r = await video_gen.poll_operation("https://g/v1beta", "K", "models/m/operations/1", client=c)
    assert r == {"done": True, "video_uri": "https://x/v.mp4", "error": None, "blocked": False, "blocked_reason": None}


async def test_poll_operation_done_without_sample_is_blocked():
    def h(req):
        return httpx.Response(200, json={"done": True, "response": {"generateVideoResponse": {}}})

    async with _transport(h) as c:
        r = await video_gen.poll_operation("https://g/v1beta", "K", "op", client=c)
    assert r["done"] and r["blocked"] and r["video_uri"] is None
    assert r["blocked_reason"] is None


async def test_poll_operation_blocked_carries_rai_reason():
    # Real shape observed on prod 2026-09-03: done, no samples, reasons list.
    def h(req):
        return httpx.Response(200, json={"done": True, "response": {"generateVideoResponse": {
            "raiMediaFilteredCount": 1,
            "raiMediaFilteredReasons": ["Sorry, we can't create videos with real people's names or likenesses."],
        }}})

    async with _transport(h) as c:
        r = await video_gen.poll_operation("https://g/v1beta", "K", "op", client=c)
    assert r["done"] and r["blocked"] and r["video_uri"] is None
    assert r["blocked_reason"] == "Sorry, we can't create videos with real people's names or likenesses."


async def test_poll_operation_error():
    def h(req):
        return httpx.Response(200, json={"done": True, "error": {"code": 3, "message": "bad prompt"}})

    async with _transport(h) as c:
        r = await video_gen.poll_operation("https://g/v1beta", "K", "op", client=c)
    assert r["done"] and r["error"] == "bad prompt"


async def test_poll_operation_http_error_raises():
    def h(req):
        return httpx.Response(500, text="server error")

    async with _transport(h) as c:
        with pytest.raises(RuntimeError):
            await video_gen.poll_operation("https://g/v1beta", "K", "op", client=c)


# --------------------------------------------------------------------------
# download_video
# --------------------------------------------------------------------------

async def test_download_video_streams_to_disk(tmp_path):
    payload = b"\x00\x00\x00\x18ftypmp42" + b"x" * 1000

    def h(req):
        return httpx.Response(200, content=payload)

    dest = tmp_path / "out.mp4"
    async with _transport(h) as c:
        written = await video_gen.download_video("K", "https://x/v.mp4", dest, client=c)
    assert written == len(payload)
    assert dest.read_bytes() == payload


async def test_download_video_error_status_raises():
    def h(req):
        return httpx.Response(404, text="not found")

    async with _transport(h) as c:
        with pytest.raises(RuntimeError):
            await video_gen.download_video("K", "https://x/v.mp4", "/tmp/whatever.mp4", client=c)


# --------------------------------------------------------------------------
# resolve_gemini_endpoint
# --------------------------------------------------------------------------

_GEMINI_BASE_SHAPES = [
    "https://generativelanguage.googleapis.com",
    "https://generativelanguage.googleapis.com/v1",
    "https://generativelanguage.googleapis.com/v1beta",
    "https://generativelanguage.googleapis.com/v1beta/",
    "https://generativelanguage.googleapis.com/v1beta/openai",
    "https://generativelanguage.googleapis.com/v1beta/openai/",
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
]


@pytest.mark.parametrize("raw_base", _GEMINI_BASE_SHAPES)
def test_normalize_gemini_base_always_yields_v1beta(raw_base):
    assert video_gen._normalize_gemini_base(raw_base) == "https://generativelanguage.googleapis.com/v1beta"


@pytest.mark.parametrize("raw_base", _GEMINI_BASE_SHAPES)
def test_resolve_gemini_endpoint_normalizes_every_stored_shape(raw_base):
    """Whatever shape an admin saved base_url in (bare host, /v1, /v1beta,
    the OpenAI-compat proxy path with or without /chat/completions), the
    Veo REST base returned is always the same canonical .../v1beta."""
    class EP:
        base_url = raw_base
        api_key = "K"
        is_enabled = True

    class Q:
        def filter(self, *a):
            return self

        def all(self):
            return [EP()]

    class S:
        def query(self, *a):
            return Q()

        def close(self):
            pass

    base, key = video_gen.resolve_gemini_endpoint(db_session_factory=lambda: S())
    assert base == "https://generativelanguage.googleapis.com/v1beta"
    assert key == "K"


def test_resolve_gemini_endpoint_strips_openai_suffix():
    class EP:
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
        api_key = "K"
        is_enabled = True

    class Q:
        def filter(self, *a):
            return self

        def all(self):
            return [EP()]

    class S:
        def query(self, *a):
            return Q()

        def close(self):
            pass

    base, key = video_gen.resolve_gemini_endpoint(db_session_factory=lambda: S())
    assert base == "https://generativelanguage.googleapis.com/v1beta"
    assert key == "K"


def test_resolve_gemini_endpoint_no_endpoint_raises():
    class Q:
        def filter(self, *a):
            return self

        def all(self):
            return []

    class S:
        def query(self, *a):
            return Q()

        def close(self):
            pass

    with pytest.raises(RuntimeError, match="Geen Gemini-endpoint met API-key"):
        video_gen.resolve_gemini_endpoint(db_session_factory=lambda: S())


def test_resolve_gemini_endpoint_ignores_non_gemini_and_keyless():
    class EPOther:
        base_url = "https://api.openai.com/v1"
        api_key = "K"
        is_enabled = True

    class EPNoKey:
        base_url = "https://generativelanguage.googleapis.com/v1beta"
        api_key = None
        is_enabled = True

    class EPGood:
        base_url = "https://generativelanguage.googleapis.com/v1beta"
        api_key = "GOODKEY"
        is_enabled = True

    class Q:
        def filter(self, *a):
            return self

        def all(self):
            return [EPOther(), EPNoKey(), EPGood()]

    class S:
        def query(self, *a):
            return Q()

        def close(self):
            pass

    base, key = video_gen.resolve_gemini_endpoint(db_session_factory=lambda: S())
    assert key == "GOODKEY"


# --------------------------------------------------------------------------
# estimate_cost_usd
# --------------------------------------------------------------------------

def test_estimate_cost():
    assert video_gen.estimate_cost_usd("veo-3.1-generate-preview", 8) == pytest.approx(3.2)
    assert video_gen.estimate_cost_usd("veo-3.1-fast-generate-preview", 4) == pytest.approx(0.4)


def test_estimate_cost_unknown_model_falls_back_to_default_rate():
    assert video_gen.estimate_cost_usd("not-a-real-model", 8) == pytest.approx(3.2)


# --------------------------------------------------------------------------
# resolve_video_path
# --------------------------------------------------------------------------

def test_resolve_video_path_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    with pytest.raises(HTTPException):
        video_gen.resolve_video_path("../etc/passwd")
    with pytest.raises(HTTPException):
        video_gen.resolve_video_path("nope.mp4")  # well-formed-looking, but absent -> 404-shaped


def test_resolve_video_path_rejects_malformed_name(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as exc_info:
        video_gen.resolve_video_path("not-hex.mp4")
    assert exc_info.value.status_code == 400


def test_resolve_video_path_returns_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    name = "a" * 32 + ".mp4"
    (tmp_path / name).write_bytes(b"data")
    path = video_gen.resolve_video_path(name)
    assert path == (tmp_path / name)


# --------------------------------------------------------------------------
# Job lifecycle
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_active_jobs():
    """_active_jobs is process-global; never leak entries across tests."""
    video_gen._active_jobs.clear()
    yield
    video_gen._active_jobs.clear()


async def test_job_lifecycle_done(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    monkeypatch.setattr(video_gen, "resolve_gemini_endpoint", lambda db_session_factory=None: ("https://g/v1beta", "K"))
    calls = {"poll": 0}

    def h(req):
        u = str(req.url)
        if u.endswith(":predictLongRunning"):
            return httpx.Response(200, json={"name": "models/m/operations/1"})
        if "/operations/" in u:
            calls["poll"] += 1
            if calls["poll"] < 2:
                return httpx.Response(200, json={"done": False})
            return httpx.Response(200, json={
                "done": True,
                "response": {"generateVideoResponse": {"generatedSamples": [{"video": {"uri": "https://g/file.mp4"}}]}},
            })
        return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42")

    monkeypatch.setattr(video_gen, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(h)))

    async def fast_sleep(_):
        pass

    monkeypatch.setattr(video_gen.asyncio, "sleep", fast_sleep)

    job_id = video_gen.start_video_job("a cat", "ed")
    await video_gen._active_jobs[job_id]["task"]  # deterministic: wait for the job to finish
    job = video_gen.get_job(job_id, "ed")
    assert job["status"] == "done"
    assert job["video_url"] == f"/api/video/{job_id}.mp4"
    assert (tmp_path / f"{job_id}.mp4").exists()
    assert (tmp_path / f"{job_id}.owner").read_text() == "ed"
    assert video_gen.get_job(job_id, "someone-else") is None


async def test_job_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    monkeypatch.setattr(video_gen, "resolve_gemini_endpoint", lambda db_session_factory=None: ("https://g/v1beta", "K"))
    monkeypatch.setattr(video_gen, "VIDEO_POLL_MAX_SECONDS", 0)

    def h(req):
        u = str(req.url)
        if u.endswith(":predictLongRunning"):
            return httpx.Response(200, json={"name": "models/m/operations/1"})
        return httpx.Response(200, json={"done": False})  # never completes

    monkeypatch.setattr(video_gen, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(h)))

    slept = []

    async def fast_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr(video_gen.asyncio, "sleep", fast_sleep)

    job_id = video_gen.start_video_job("a dog", "ed")
    await video_gen._active_jobs[job_id]["task"]
    job = video_gen.get_job(job_id, "ed")
    assert job["status"] == "error"
    assert job["error"] == "Time-out"
    assert not (tmp_path / f"{job_id}.mp4").exists()


async def test_job_safety_block_is_error(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    monkeypatch.setattr(video_gen, "resolve_gemini_endpoint", lambda db_session_factory=None: ("https://g/v1beta", "K"))

    def h(req):
        u = str(req.url)
        if u.endswith(":predictLongRunning"):
            return httpx.Response(200, json={"name": "models/m/operations/1"})
        return httpx.Response(200, json={"done": True, "response": {"generateVideoResponse": {}}})

    monkeypatch.setattr(video_gen, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(h)))

    job_id = video_gen.start_video_job("a horse", "ed")
    await video_gen._active_jobs[job_id]["task"]
    job = video_gen.get_job(job_id, "ed")
    assert job["status"] == "error"
    assert "safety" in job["error"].lower()
    assert not (tmp_path / f"{job_id}.mp4").exists()


async def test_job_safety_block_error_includes_google_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    monkeypatch.setattr(video_gen, "resolve_gemini_endpoint", lambda db_session_factory=None: ("https://g/v1beta", "K"))

    def h(req):
        u = str(req.url)
        if u.endswith(":predictLongRunning"):
            return httpx.Response(200, json={"name": "models/m/operations/1"})
        return httpx.Response(200, json={"done": True, "response": {"generateVideoResponse": {
            "raiMediaFilteredCount": 1,
            "raiMediaFilteredReasons": ["Please remove the celebrity reference and try again."],
        }}})

    monkeypatch.setattr(video_gen, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(h)))

    job_id = video_gen.start_video_job("a famous actor", "ed")
    await video_gen._active_jobs[job_id]["task"]
    job = video_gen.get_job(job_id, "ed")
    assert job["status"] == "error"
    assert job["error"].startswith("Geblokkeerd door Veo safety-filter")
    assert "celebrity reference" in job["error"]


async def test_run_job_cancelled_sets_error_status(monkeypatch):
    """A cancelled asyncio.Task (e.g. app shutdown mid-job) must still leave
    the job registry in a terminal, reportable state — not stuck 'running'
    forever — and re-raise so the task's cancellation itself isn't swallowed."""
    job_id = "f" * 32
    entry = {
        "status": "running", "prompt": "x", "model": "veo-3.1-generate-preview",
        "error": None, "video_url": None, "cost_estimate": 1.0,
        "owner": "ed", "started_at": time.time(), "completed_at": None, "task": None,
    }
    video_gen._active_jobs[job_id] = entry

    async def _raise_cancelled(*a, **k):
        raise asyncio.CancelledError()
    monkeypatch.setattr(video_gen, "_generate", _raise_cancelled)

    with pytest.raises(asyncio.CancelledError):
        await video_gen._run_job(job_id, None)

    assert entry["status"] == "error"
    assert entry["error"] == "Generatie afgebroken"
    assert entry["completed_at"] is not None


def test_get_job_recovers_from_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    job_id = "b" * 32
    (tmp_path / f"{job_id}.mp4").write_bytes(b"data")
    (tmp_path / f"{job_id}.owner").write_text("ed", encoding="utf-8")

    job = video_gen.get_job(job_id, "ed")
    assert job is not None
    assert job["status"] == "done"
    assert job["video_url"] == f"/api/video/{job_id}.mp4"

    # Wrong owner never sees it, and neither does an unauthenticated caller.
    assert video_gen.get_job(job_id, "someone-else") is None
    assert video_gen.get_job(job_id, "") is None


def test_get_job_disk_recovery_requires_owner_file(tmp_path, monkeypatch):
    """A published mp4 with no .owner sidecar (e.g. from before this feature,
    or a partial publish) must never be handed to anyone."""
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    job_id = "c" * 32
    (tmp_path / f"{job_id}.mp4").write_bytes(b"data")

    assert video_gen.get_job(job_id, "ed") is None


def test_get_job_unknown_id_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    assert video_gen.get_job("does-not-exist", "ed") is None


def test_get_job_malformed_id_never_touches_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    assert video_gen.get_job("../../etc/passwd", "ed") is None


# --------------------------------------------------------------------------
# start_video_job validation
# --------------------------------------------------------------------------

def test_start_video_job_rejects_empty_prompt(monkeypatch):
    monkeypatch.setattr(video_gen, "resolve_gemini_endpoint", lambda db_session_factory=None: ("https://g/v1beta", "K"))
    with pytest.raises(ValueError):
        video_gen.start_video_job("   ", "ed")


def test_start_video_job_rejects_unknown_model(monkeypatch):
    monkeypatch.setattr(video_gen, "resolve_gemini_endpoint", lambda db_session_factory=None: ("https://g/v1beta", "K"))
    with pytest.raises(ValueError):
        video_gen.start_video_job("a cat", "ed", model="not-a-veo-model")


def test_start_video_job_rejects_bad_aspect_ratio(monkeypatch):
    monkeypatch.setattr(video_gen, "resolve_gemini_endpoint", lambda db_session_factory=None: ("https://g/v1beta", "K"))
    with pytest.raises(ValueError):
        video_gen.start_video_job("a cat", "ed", model="veo-3.1-generate-preview", aspect_ratio="4:3")


def test_start_video_job_rejects_bad_duration(monkeypatch):
    monkeypatch.setattr(video_gen, "resolve_gemini_endpoint", lambda db_session_factory=None: ("https://g/v1beta", "K"))
    with pytest.raises(ValueError):
        video_gen.start_video_job("a cat", "ed", model="veo-3.1-generate-preview", duration_seconds=99)


def test_start_video_job_propagates_missing_endpoint(monkeypatch):
    def _raise(db_session_factory=None):
        raise RuntimeError("Geen Gemini-endpoint met API-key")

    monkeypatch.setattr(video_gen, "resolve_gemini_endpoint", _raise)
    with pytest.raises(RuntimeError, match="Geen Gemini-endpoint met API-key"):
        video_gen.start_video_job("a cat", "ed")


# --------------------------------------------------------------------------
# cleanup_orphaned_videos
# --------------------------------------------------------------------------

def test_cleanup_orphaned_videos_removes_only_old_matching_files(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    import os as _os
    import time as _time

    old_mp4 = tmp_path / (("d" * 32) + ".mp4")
    old_mp4.write_bytes(b"x")
    old_owner = tmp_path / (("d" * 32) + ".owner")
    old_owner.write_text("ed")
    old_tmp = tmp_path / ".video-abc.tmp"
    old_tmp.write_bytes(b"x")
    unrelated = tmp_path / "keepme.txt"
    unrelated.write_text("keep")

    old_time = _time.time() - (8 * 24 * 3600)
    for p in (old_mp4, old_owner, old_tmp, unrelated):
        _os.utime(p, (old_time, old_time))

    fresh_mp4 = tmp_path / (("e" * 32) + ".mp4")
    fresh_mp4.write_bytes(b"y")

    removed, kept = video_gen.cleanup_orphaned_videos(max_age_seconds=7 * 24 * 3600)
    assert removed == 3  # old_mp4, old_owner, old_tmp
    assert kept == 1  # fresh_mp4
    assert not old_mp4.exists()
    assert not old_owner.exists()
    assert not old_tmp.exists()
    assert fresh_mp4.exists()
    assert unrelated.exists()  # never touched: not one of the three shapes


def test_cleanup_orphaned_videos_missing_dir_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path / "does-not-exist"))
    assert video_gen.cleanup_orphaned_videos() == (0, 0)
