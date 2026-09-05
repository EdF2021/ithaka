"""routes/video_routes.py: privilege/enabled gates, owner checks, path safety.

Pattern mirrors tests/test_routes_notebook_audio.py: the route module's
imported names (get_current_user, require_privilege, get_user_setting,
video_gen.*) are monkeypatched directly rather than wiring a real
AuthManager/settings file, so these tests never touch real auth or the real
Veo job runner.
"""
import routes.video_routes as vr
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from src.interactive_gate import should_track_interactive_request


def _client(
    monkeypatch,
    user="ed",
    privilege_denied=False,
    video_gen_enabled=True,
    start_job=None,
    get_job=None,
):
    def _current_user(request):
        return user
    monkeypatch.setattr(vr, "get_current_user", _current_user)

    def _require_privilege(request, key):
        assert key == "can_generate_videos"
        if privilege_denied:
            raise HTTPException(status_code=403, detail="Your account is not allowed to can generate videos.")
        return user
    monkeypatch.setattr(vr, "require_privilege", _require_privilege)

    def _get_user_setting(key, owner="", default=None):
        if key == "video_gen_enabled":
            return video_gen_enabled
        return default
    monkeypatch.setattr(vr, "get_user_setting", _get_user_setting)

    if start_job is not None:
        monkeypatch.setattr(vr.video_gen, "start_video_job", start_job)
    if get_job is not None:
        monkeypatch.setattr(vr.video_gen, "get_job", get_job)

    app = FastAPI()
    app.include_router(vr.setup_video_routes())
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------
# POST /api/video/generate
# --------------------------------------------------------------------------

def test_generate_without_privilege_is_403(monkeypatch):
    c = _client(monkeypatch, privilege_denied=True)
    r = c.post("/api/video/generate", json={"prompt": "a cat"})
    assert r.status_code == 403


def test_generate_disabled_is_400(monkeypatch):
    c = _client(monkeypatch, video_gen_enabled=False)
    r = c.post("/api/video/generate", json={"prompt": "a cat"})
    assert r.status_code == 400


def test_generate_per_user_pref_overrides_global_disabled(monkeypatch):
    """video_gen_enabled is in _PER_USER_KEYS: the real get_user_setting
    (not the test-fixture stub) reads routes.prefs_routes._load_for_user
    first. A global default-off admin setting must not block a user who
    explicitly enabled it for themselves, and must still block a different
    user with no such pref."""
    monkeypatch.setattr(vr, "get_current_user", lambda request: "ed")
    monkeypatch.setattr(vr, "require_privilege", lambda request, key: "ed")
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: False if key == "video_gen_enabled" else default)

    def fake_load_for_user(owner=None):
        return {"video_gen_enabled": True} if owner == "ed" else {}
    monkeypatch.setattr("routes.prefs_routes._load_for_user", fake_load_for_user)

    monkeypatch.setattr(vr.video_gen, "start_video_job", lambda prompt, owner, **kw: "job-x")
    monkeypatch.setattr(vr.video_gen, "get_job", lambda job_id, owner: {"cost_estimate": 1.0})

    app_client = _real_client()
    r = app_client.post("/api/video/generate", json={"prompt": "a cat"})
    assert r.status_code == 202

    # A different user with no per-user pref falls back to the disabled global.
    monkeypatch.setattr(vr, "get_current_user", lambda request: "other")
    monkeypatch.setattr(vr, "require_privilege", lambda request, key: "other")
    app_client2 = _real_client()
    r2 = app_client2.post("/api/video/generate", json={"prompt": "a cat"})
    assert r2.status_code == 400


def _real_client():
    from fastapi import FastAPI as _FastAPI
    from starlette.testclient import TestClient as _TestClient
    app = _FastAPI()
    app.include_router(vr.setup_video_routes())
    return _TestClient(app, raise_server_exceptions=False)


def test_generate_empty_prompt_is_400(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/video/generate", json={"prompt": "   "})
    assert r.status_code == 400


def test_generate_starts_job_returns_202(monkeypatch):
    def _start(prompt, owner, **kw):
        assert prompt == "a cat"
        assert owner == "ed"
        return "job-123"
    monkeypatch.setattr(vr.video_gen, "start_video_job", _start)
    monkeypatch.setattr(vr.video_gen, "get_job", lambda job_id, owner: {"cost_estimate": 3.2})
    c = _client(monkeypatch)

    r = c.post("/api/video/generate", json={"prompt": "a cat"})
    assert r.status_code == 202
    body = r.json()
    assert body["job_id"] == "job-123"
    assert body["cost_estimate"] == 3.2


def test_generate_passes_optional_params(monkeypatch):
    seen = {}

    def _start(prompt, owner, **kw):
        seen.update(kw)
        return "job-1"
    monkeypatch.setattr(vr.video_gen, "start_video_job", _start)
    monkeypatch.setattr(vr.video_gen, "get_job", lambda job_id, owner: {"cost_estimate": 1.0})
    c = _client(monkeypatch)

    r = c.post("/api/video/generate", json={"prompt": "a cat", "aspect_ratio": "9:16", "duration_seconds": 4})
    assert r.status_code == 202
    assert seen["aspect_ratio"] == "9:16"
    assert seen["duration_seconds"] == 4


def test_generate_valueerror_from_job_start_is_400(monkeypatch):
    def _raise(prompt, owner, **kw):
        raise ValueError("Onbekend video-model: x")
    monkeypatch.setattr(vr.video_gen, "start_video_job", _raise)
    c = _client(monkeypatch)

    r = c.post("/api/video/generate", json={"prompt": "a cat"})
    assert r.status_code == 400
    assert r.json()["detail"] == "Onbekend video-model: x"


def test_generate_runtimeerror_from_job_start_is_503(monkeypatch):
    def _raise(prompt, owner, **kw):
        raise RuntimeError("Geen Gemini-endpoint met API-key")
    monkeypatch.setattr(vr.video_gen, "start_video_job", _raise)
    c = _client(monkeypatch)

    r = c.post("/api/video/generate", json={"prompt": "a cat"})
    assert r.status_code == 503
    assert r.json()["detail"] == "Geen Gemini-endpoint met API-key"


# --------------------------------------------------------------------------
# GET /api/video/jobs/{job_id}
# --------------------------------------------------------------------------

def test_get_job_unknown_is_404(monkeypatch):
    monkeypatch.setattr(vr.video_gen, "get_job", lambda job_id, owner: None)
    c = _client(monkeypatch)
    r = c.get("/api/video/jobs/does-not-exist")
    assert r.status_code == 404


def test_get_job_wrong_owner_is_404(monkeypatch):
    """get_job itself returns None for a mismatched owner (contract) — the
    route has nothing extra to check, but this proves the route trusts that
    return value rather than re-deriving ownership."""
    monkeypatch.setattr(vr.video_gen, "get_job", lambda job_id, owner: None if owner != "ed" else {"status": "running"})
    c = _client(monkeypatch, user="eve")
    r = c.get("/api/video/jobs/some-job")
    assert r.status_code == 404


def test_get_job_running_passthrough(monkeypatch):
    job = {
        "job_id": "j1", "status": "running", "prompt": "a cat", "model": "veo-3.1-generate-preview",
        "error": None, "video_url": None, "cost_estimate": 3.2, "started_at": 100.0, "completed_at": None,
    }
    monkeypatch.setattr(vr.video_gen, "get_job", lambda job_id, owner: job)
    c = _client(monkeypatch)
    r = c.get("/api/video/jobs/j1")
    assert r.status_code == 200
    assert r.json() == job


# --------------------------------------------------------------------------
# GET /api/video/{filename}
# --------------------------------------------------------------------------

def test_serve_video_malformed_filename_is_400(monkeypatch, tmp_path):
    monkeypatch.setattr(vr.video_gen, "VIDEO_DIR", str(tmp_path))
    c = _client(monkeypatch)
    r = c.get("/api/video/not-a-valid-name.mp4")
    assert r.status_code == 400


def test_serve_video_traversal_never_reaches_200(monkeypatch, tmp_path):
    monkeypatch.setattr(vr.video_gen, "VIDEO_DIR", str(tmp_path))
    c = _client(monkeypatch)
    r = c.get("/api/video/..%2Fetc%2Fpasswd")
    assert r.status_code in (400, 404)


def test_serve_video_unknown_file_is_404(monkeypatch, tmp_path):
    monkeypatch.setattr(vr.video_gen, "VIDEO_DIR", str(tmp_path))
    c = _client(monkeypatch)
    filename = "a" * 32 + ".mp4"
    r = c.get(f"/api/video/{filename}")
    assert r.status_code == 404


def test_serve_video_ownership_mismatch_is_404(monkeypatch, tmp_path):
    """File exists on disk but get_job (owner-checked) doesn't recognize it
    for this caller: 404, never confirm existence to a non-owner."""
    monkeypatch.setattr(vr.video_gen, "VIDEO_DIR", str(tmp_path))
    filename = "b" * 32 + ".mp4"
    (tmp_path / filename).write_bytes(b"fake-mp4")
    monkeypatch.setattr(vr.video_gen, "get_job", lambda job_id, owner: None)
    c = _client(monkeypatch, user="eve")

    r = c.get(f"/api/video/{filename}")
    assert r.status_code == 404


def test_serve_video_owner_gets_file(monkeypatch, tmp_path):
    monkeypatch.setattr(vr.video_gen, "VIDEO_DIR", str(tmp_path))
    filename = "c" * 32 + ".mp4"
    content = b"fake-mp4-bytes"
    (tmp_path / filename).write_bytes(content)
    monkeypatch.setattr(vr.video_gen, "get_job", lambda job_id, owner: {
        "status": "done", "video_url": f"/api/video/{filename}",
    })
    c = _client(monkeypatch, user="ed")

    r = c.get(f"/api/video/{filename}")
    assert r.status_code == 200
    assert r.content == content
    assert r.headers["content-type"] == "video/mp4"
    # private (not public): an owner-gated resource must never be stored or
    # replayed by a shared/intermediate cache for another caller.
    assert r.headers["cache-control"].startswith("private,")


def test_serve_video_not_done_status_is_404(monkeypatch, tmp_path):
    monkeypatch.setattr(vr.video_gen, "VIDEO_DIR", str(tmp_path))
    filename = "d" * 32 + ".mp4"
    (tmp_path / filename).write_bytes(b"fake")
    monkeypatch.setattr(vr.video_gen, "get_job", lambda job_id, owner: {
        "status": "running", "video_url": None,
    })
    c = _client(monkeypatch)

    r = c.get(f"/api/video/{filename}")
    assert r.status_code == 404


# --------------------------------------------------------------------------
# interactive_gate passive-pattern registration
# --------------------------------------------------------------------------

def test_video_job_poll_is_passive():
    assert should_track_interactive_request("/api/video/jobs/abc123", "GET") is False


def test_video_generate_post_is_not_passive():
    """Only the GET status poll is passive — the POST that starts the job
    must still count as foreground activity."""
    assert should_track_interactive_request("/api/video/generate", "POST") is True


def test_video_jobs_prefix_alone_is_not_passive():
    assert should_track_interactive_request("/api/video/jobs/", "GET") is True
