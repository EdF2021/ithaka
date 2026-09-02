# Auto-routing beeld (gpt-image-1.5) en video (Veo 3.1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Een chatverzoek om een afbeelding levert automatisch een gpt-image-1.5-beeld; een verzoek om een video levert automatisch een Veo 3.1-clip, inline in de chat, in Chat- én Agent-modus.

**Architecture:** Beeld hergebruikt de bestaande `generate_image`-tool; we voegen alleen intent-detectie (Chat→Agent-escalatie) en tool-forcering toe. Video krijgt een eigen async-job-module (`src/video_gen.py`, spiegel van `src/notebook_audio.py`), een router (`routes/video_routes.py`), een ingebouwde tool `generate_video` (TOOL_HANDLERS), en een chat-bubble die de job pollt en een `<video>` rendert.

**Tech Stack:** FastAPI, httpx (async), vanilla JS, pytest (`asyncio_mode=auto`), node voor JS-tests. Veo via Gemini REST (`predictLongRunning`).

**Spec:** `docs/superpowers/specs/2026-09-02-image-video-autoroute-design.md`

## Global Constraints

- Paden alleen via `src/constants.py` (nieuwe `VIDEO_DIR = os.path.join(DATA_DIR, "videos")`, guarded mkdir zoals `NOTEBOOK_VIDEO_DIR`). Nooit `Path(__file__)`-schrijfpaden of hardcoded `/app/...`.
- Geen Unicode-emoji in UI/code; bestaande CSS-variabelen/klassen hergebruiken; UI-strings Engels (zoals de rest van de UI).
- Conventional Commits; commit-body en PR-body eindigen met exact `Ed de Feber, in nauwe samenwerking met Claude`, geen Co-Authored-By.
- Tests volgen `tests/TESTING_STANDARD.md`; `py_compile` + `node --check` op gewijzigde bestanden.
- `mcp<2` blijft; geen nieuwe Python-dependencies (httpx is er al).
- Veo-modelnamen: `veo-3.1-generate-preview` (default), `veo-3.1-fast-generate-preview`, `veo-3.1-lite-generate-preview`. Tarief 720p: 0.40 / 0.10 / 0.05 USD per seconde.

## Interface-contracten (alle taken bouwen hiertegen; parallel uitvoerbaar)

```python
# src/video_gen.py
VEO_MODELS = ("veo-3.1-generate-preview", "veo-3.1-fast-generate-preview", "veo-3.1-lite-generate-preview")
VEO_PRICE_PER_SECOND_720P = {"veo-3.1-generate-preview": 0.40, "veo-3.1-fast-generate-preview": 0.10, "veo-3.1-lite-generate-preview": 0.05}
VIDEO_FILENAME_RE = re.compile(r"^[a-f0-9]{32}\.mp4$")

def resolve_gemini_endpoint(db_session_factory=None) -> tuple[str, str]   # (base_url zonder /openai, api_key); RuntimeError("Geen Gemini-endpoint met API-key") als er geen is
async def start_generation(base_url, api_key, prompt, *, model, aspect_ratio="16:9", resolution="720p", duration_seconds=8, negative_prompt="", client=None) -> str   # operation name
async def poll_operation(base_url, api_key, operation_name, client=None) -> dict   # {"done": bool, "video_uri": str|None, "error": str|None, "blocked": bool}
async def download_video(api_key, uri, dest_path, client=None) -> int   # bytes geschreven
def estimate_cost_usd(model: str, duration_seconds: int, resolution: str = "720p") -> float
def resolve_video_path(filename: str) -> Path   # HTTPException 400/404 zoals notebook_audio.resolve_notebook_audio_path
def start_video_job(prompt: str, owner: str, *, model=None, aspect_ratio=None, duration_seconds=None, resolution=None, db_session_factory=None) -> str   # job_id (uuid4 hex); RuntimeError bij ontbrekend endpoint; ValueError bij ongeldige parameters
def get_job(job_id: str, owner: str) -> dict | None   # {"job_id","status":"running"|"done"|"error","prompt","model","error","video_url":"/api/video/<fn>"|None,"cost_estimate","started_at","completed_at"}; herstart-bestendig: onbekende job maar bestaand bestand VIDEO_DIR/<job_id>.mp4 → done (owner-check niet mogelijk → alleen als bestand bestaat én owner-bestand <job_id>.owner overeenkomt; schrijf daarom naast de mp4 een <job_id>.owner-tekstbestand)
def cleanup_orphaned_videos(*, max_age_seconds: int = 7*24*3600) -> tuple[int, int]   # (removed, kept)
```

```text
# Tool-resultaat van generate_video (dict, wordt door agent_loop in tool_output-SSE en tool_events gezet):
{"output": "Video generation started (veo-3.1-generate-preview, 8s, ~$3.20). It renders inline when ready.",
 "video_job_id": "<hex>", "video_model": "...", "video_status": "running", "video_cost_estimate": 3.2, "video_url": None}
# Fout: {"error": "<reden>", "exit_code": 1}
# Frontend-contract: tool_output/tool_event met `video_job_id` → statusbubble + poll GET /api/video/jobs/{id};
# job.status=="done" → <video src=job.video_url>; "error" → rode regel job.error.
```

```text
# Routes (routes/video_routes.py, factory setup_video_routes() zonder args)
POST /api/video/generate  {prompt, aspect_ratio?, duration_seconds?} → 202 {"job_id","cost_estimate"}; 403 zonder privilege can_generate_videos; 400 als video_gen_enabled False of prompt leeg; 503 RuntimeError (geen endpoint)
GET  /api/video/jobs/{job_id} → get_job(...) of 404
GET  /api/video/{filename}   → FileResponse video/mp4 (alleen eigenaar; 404 anders)
```

```text
# Settings-keys (src/settings.py DEFAULTS + _PER_USER_KEYS):
video_gen_enabled: False, video_model: "veo-3.1-generate-preview", video_resolution: "720p", video_aspect_ratio: "16:9", video_duration_seconds: 8
# Privilege: can_generate_videos (core/auth.py DEFAULT_PRIVILEGES, admin.js label "Video generation", chat_routes disabled_tools → "generate_video")
# Intent-categorieën in src/action_intents.py: "image" en "video" (ToolIntent(True, category=...))
```

---

### Task 1 (agent A): intent-categorieën `image`/`video` + tool-forcering + schema

**Files:**
- Modify: `src/action_intents.py` (`_ROUTING_PATTERNS`)
- Modify: `src/tool_index.py` (keyword→tool-map rond regel 343-375: image-set uitbreiden, video-set toevoegen; `TOOL_DESCRIPTIONS`-achtige dict rond regel 85: beschrijving `generate_video`)
- Modify: `src/tool_schemas.py` (schema `generate_video` direct na `generate_image`, ~regel 1035)
- Test: `tests/test_action_intents_media.py`, `tests/test_tool_index_media_force.py`

**Interfaces:** Produces `classify_tool_intent("maak een video van een kat").category == "video"`; tool-index-map levert `{"generate_video"}` voor die tekst; schema-naam `generate_video` met properties `prompt` (required), `aspect_ratio` enum `["16:9","9:16"]`, `duration_seconds` enum `[4,6,8]`.

- [ ] **Step 1: failing tests**

```python
# tests/test_action_intents_media.py
import pytest
from src.action_intents import classify_tool_intent

@pytest.mark.parametrize("text", [
    "maak een afbeelding van een kat op een fiets",
    "Genereer een plaatje van de Eiffeltoren bij nacht",
    "teken een logo voor mijn bakkerij",
    "kun je een illustratie maken van een draak",
    "create an image of a red bicycle",
    "draw a picture of a lighthouse",
    "make me a poster for the school party",
])
def test_image_requests_route_to_image(text):
    intent = classify_tool_intent(text)
    assert intent.needs_tools and intent.category == "image"

@pytest.mark.parametrize("text", [
    "maak een video van een surfende hond",
    "genereer een filmpje over de zee",
    "create a short clip of a rocket launch",
    "kun je een animatie maken van een dansende robot",
    "make a video of waves at sunset",
])
def test_video_requests_route_to_video(text):
    intent = classify_tool_intent(text)
    assert intent.needs_tools and intent.category == "video"

@pytest.mark.parametrize("text", [
    "zoek een afbeelding van de Eiffeltoren",
    "beschrijf deze afbeelding",
    "wat zie je op deze foto",
    "find a video about python decorators",
    "vat deze youtube video samen",
    "how do I generate an image here?",
    "upload een afbeelding",
])
def test_media_negatives_do_not_route(text):
    intent = classify_tool_intent(text)
    assert intent.category not in ("image", "video")

def test_existing_calendar_still_calendar():
    assert classify_tool_intent("add lunch to my calendar tomorrow").category == "calendar"
```

```python
# tests/test_tool_index_media_force.py
from src import tool_index
from src.tool_schemas import get_all_openai_schemas  # pas aan naar de echte naam (grep "def get_all_openai_schemas")

def _forced(text):
    # gebruik de bestaande helper die de keyword-map toepast (grep in tool_index.py naar de functie
    # die het dict met frozenset-keys itereert, bv. `_keyword_forced_tools` / binnen select_relevant_tools);
    # roep die aan met de tekst en geef de set terug.
    ...

def test_video_phrases_force_generate_video():
    for t in ("maak een video van een kat", "make a video of a cat", "genereer een filmpje", "create a short clip"):
        assert "generate_video" in _forced(t)

def test_image_phrases_force_generate_image():
    for t in ("maak een afbeelding van een kat", "generate an image of a cat", "teken een logo"):
        assert "generate_image" in _forced(t)

def test_generate_video_schema_present():
    names = {s["function"]["name"] for s in get_all_openai_schemas() if s.get("type") == "function"}
    assert "generate_video" in names
```

- [ ] **Step 2: run, verify FAIL** — `/home/eddef/projects/ithaka/.venv/bin/python -m pytest tests/test_action_intents_media.py tests/test_tool_index_media_force.py -q`
- [ ] **Step 3: implement** — in `_ROUTING_PATTERNS` twee blokken toevoegen (vóór de generieke "web"-patronen zodat "maak een afbeelding" niet als search matcht):

```python
_MEDIA_MAKE = r"(?:maak|maken|genereer|genereren|teken|tekenen|creëer|creeer|ontwerp|render|schets|make|making|generate|draw|create|design|render|paint)"
_IMAGE_THING = r"(?:afbeelding|plaatje|foto|illustratie|logo|poster|tekening|icoon|banner|image|picture|photo|illustration|drawing|icon)"
_VIDEO_THING = r"(?:video|filmpje|clip|animatie|animation|reel)"
_MEDIA_NEG = re.compile(r"\b(?:zoek|vind|search|find|bekijk|beschrijf|analyseer|describe|analy[sz]e|upload|transcribeer|transcribe|vat\s+samen|summari[sz]e|youtube)\b", re.I)
...
        ("image", "image generation request", rf"\b{_MEDIA_MAKE}\b(?:\s+\S+){{0,6}}?\s+(?:een\s+|a\s+|an\s+|me\s+(?:a|an|een)\s+)?(?:\S+\s+){{0,3}}?{_IMAGE_THING}\b"),
        ("video", "video generation request", rf"\b{_MEDIA_MAKE}\b(?:\s+\S+){{0,6}}?\s+(?:een\s+|a\s+|an\s+|me\s+(?:a|an|een)\s+)?(?:\S+\s+){{0,3}}?{_VIDEO_THING}\b"),
```

en in `classify_tool_intent` vóór de loop: `if _MEDIA_NEG.search(text): ...` mag alleen de image/video-categorieën blokkeren — implementeer als: bij match op category in ("image","video") en `_MEDIA_NEG.search(text)` → doorgaan naar het volgende patroon. Tool-index: voeg aan de bestaande image-frozenset toe `"maak een logo", "maak een poster", "generate a logo", "create a logo", "make a poster", "teken een"`; nieuwe entry `frozenset({"maak een video", "maak een filmpje", "genereer een video", "genereer een filmpje", "maak een animatie", "make a video", "generate a video", "create a video", "create a clip", "make a clip", "create an animation", "make an animation", "video van", "video of"}): {"generate_video"}`. Beschrijving: `"generate_video": "Generate a short AI video clip (Veo) from a text prompt. Use when the user asks to make/generate/create a video, clip or animation. Renders inline when ready."`. Schema:

```python
    {
        "type": "function",
        "function": {
            "name": "generate_video",
            "description": "Generate a short AI video clip from a text prompt (Veo). Use this whenever the user asks you to make, create, or generate a video, clip, or animation. The job runs in the background and the video renders inline in the chat when ready.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What the video should show; describe scene, motion, camera, mood."},
                    "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16"], "description": "Default 16:9."},
                    "duration_seconds": {"type": "integer", "enum": [4, 6, 8], "description": "Default 8."},
                },
                "required": ["prompt"]
            }
        }
    },
```

- [ ] **Step 4: run, verify PASS**, plus `pytest tests/test_action_intents*.py tests/test_tool_index*.py tests/test_tool_schemas*.py -q` (bestaande tests groen).
- [ ] **Step 5: commit** `feat(intents): route image/video requests to generate_image/generate_video`

### Task 2 (agent B): `src/video_gen.py` + constant + routes + app-wiring + janitor + passive poll

**Files:**
- Create: `src/video_gen.py`, `routes/video_routes.py`
- Modify: `src/constants.py` (VIDEO_DIR naast NOTEBOOK_VIDEO_DIR, zelfde guarded mkdir), `app.py` (include_router na `setup_stt_routes`; janitor-loop naast `_notebook_video_janitor_loop`), `src/interactive_gate.py` (`_PASSIVE_PATTERNS` + `re.compile(r"^/api/video/jobs/[^/]+$")`)
- Test: `tests/test_video_gen.py`, `tests/test_routes_video.py`

**Interfaces:** zie contracten hierboven; `get_current_user`, `require_privilege` uit `src.auth_helpers`; `get_setting` uit `src.settings`; `ModelEndpoint`, `SessionLocal` uit `core.database`.

- [ ] **Step 1: failing tests** (httpx gemockt via `httpx.MockTransport`; `asyncio.sleep` gepatcht):

```python
# tests/test_video_gen.py (kern)
import json, httpx, pytest
from src import video_gen

def _transport(handler): return httpx.AsyncClient(transport=httpx.MockTransport(handler))

async def test_start_generation_posts_predict_long_running():
    seen = {}
    def h(req):
        seen["url"] = str(req.url); seen["key"] = req.headers.get("x-goog-api-key"); seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"name": "models/veo-3.1-generate-preview/operations/abc"})
    async with _transport(h) as c:
        op = await video_gen.start_generation("https://generativelanguage.googleapis.com/v1beta", "KEY", "a cat", model="veo-3.1-generate-preview", client=c)
    assert op == "models/veo-3.1-generate-preview/operations/abc"
    assert seen["url"].endswith("/v1beta/models/veo-3.1-generate-preview:predictLongRunning")
    assert seen["key"] == "KEY"
    assert seen["body"]["instances"][0]["prompt"] == "a cat"
    assert seen["body"]["parameters"]["durationSeconds"] == "8"
    assert seen["body"]["parameters"]["aspectRatio"] == "16:9"

async def test_poll_operation_done_with_uri():
    def h(req): return httpx.Response(200, json={"done": True, "response": {"generateVideoResponse": {"generatedSamples": [{"video": {"uri": "https://x/v.mp4"}}]}}})
    async with _transport(h) as c:
        r = await video_gen.poll_operation("https://g/v1beta", "K", "models/m/operations/1", client=c)
    assert r == {"done": True, "video_uri": "https://x/v.mp4", "error": None, "blocked": False}

async def test_poll_operation_done_without_sample_is_blocked():
    def h(req): return httpx.Response(200, json={"done": True, "response": {"generateVideoResponse": {}}})
    async with _transport(h) as c:
        r = await video_gen.poll_operation("https://g/v1beta", "K", "op", client=c)
    assert r["done"] and r["blocked"] and r["video_uri"] is None

async def test_poll_operation_error():
    def h(req): return httpx.Response(200, json={"done": True, "error": {"code": 3, "message": "bad prompt"}})
    async with _transport(h) as c:
        r = await video_gen.poll_operation("https://g/v1beta", "K", "op", client=c)
    assert r["done"] and r["error"] == "bad prompt"

def test_resolve_gemini_endpoint_strips_openai_suffix(monkeypatch):  # fake db-factory met één ModelEndpoint-achtig object
    class EP: base_url = "https://generativelanguage.googleapis.com/v1beta/openai"; api_key = "K"; is_enabled = True
    class Q:
        def filter(self, *a): return self
        def all(self): return [EP()]
    class S:
        def query(self, *a): return Q()
        def close(self): pass
    base, key = video_gen.resolve_gemini_endpoint(db_session_factory=lambda: S())
    assert base == "https://generativelanguage.googleapis.com/v1beta" and key == "K"

def test_estimate_cost():
    assert video_gen.estimate_cost_usd("veo-3.1-generate-preview", 8) == pytest.approx(3.2)
    assert video_gen.estimate_cost_usd("veo-3.1-fast-generate-preview", 4) == pytest.approx(0.4)

def test_resolve_video_path_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    from fastapi import HTTPException
    with pytest.raises(HTTPException): video_gen.resolve_video_path("../etc/passwd")
    with pytest.raises(HTTPException): video_gen.resolve_video_path("nope.mp4")  # 404

async def test_job_lifecycle_done(tmp_path, monkeypatch):
    monkeypatch.setattr(video_gen, "VIDEO_DIR", str(tmp_path))
    monkeypatch.setattr(video_gen, "resolve_gemini_endpoint", lambda db_session_factory=None: ("https://g/v1beta", "K"))
    calls = {"poll": 0}
    def h(req):
        u = str(req.url)
        if u.endswith(":predictLongRunning"): return httpx.Response(200, json={"name": "models/m/operations/1"})
        if "/operations/" in u:
            calls["poll"] += 1
            if calls["poll"] < 2: return httpx.Response(200, json={"done": False})
            return httpx.Response(200, json={"done": True, "response": {"generateVideoResponse": {"generatedSamples": [{"video": {"uri": "https://g/file.mp4"}}]}}})
        return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(video_gen, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(h)))
    async def fast_sleep(_): pass
    monkeypatch.setattr(video_gen.asyncio, "sleep", fast_sleep)
    job_id = video_gen.start_video_job("a cat", "ed")
    for _ in range(50):
        await asyncio.sleep(0)  # let the task run
        if video_gen.get_job(job_id, "ed")["status"] != "running": break
    job = video_gen.get_job(job_id, "ed")
    assert job["status"] == "done" and job["video_url"] == f"/api/video/{job_id}.mp4"
    assert (tmp_path / f"{job_id}.mp4").exists() and (tmp_path / f"{job_id}.owner").read_text() == "ed"
    assert video_gen.get_job(job_id, "someone-else") is None

async def test_job_timeout(tmp_path, monkeypatch): ...  # poll altijd done=False, VIDEO_POLL_MAX_SECONDS=0 → status error met "Time-out"
def test_get_job_recovers_from_disk(tmp_path, monkeypatch): ...  # bestand + .owner aanwezig, niet in _active_jobs → done
```

```python
# tests/test_routes_video.py (FastAPI TestClient met app-factory; mirror tests/test_routes_notebook_audio.py voor auth-mocking)
# - POST /api/video/generate zonder privilege → 403; met video_gen_enabled False → 400; ok → 202 met job_id (start_video_job gemockt)
# - GET /api/video/jobs/{id} andere owner → 404
# - GET /api/video/..%2Fetc → 400; onbekend → 404
# - interactive_gate: is_interactive_request("GET", "/api/video/jobs/abc") is False (grep de echte functienaam in src/interactive_gate.py)
```

- [ ] **Step 2: run, verify FAIL**
- [ ] **Step 3: implement `src/video_gen.py`** — structuur:

```python
"""Veo video generation: async jobs that poll the Gemini long-running operation.
Mirrors src/notebook_audio.py (in-memory _active_jobs + asyncio.create_task)."""
import asyncio, logging, os, re, time, uuid
from pathlib import Path
import httpx
from fastapi import HTTPException
from src.constants import VIDEO_DIR

VEO_MODELS = (...); VEO_PRICE_PER_SECOND_720P = {...}
VIDEO_FILENAME_RE = re.compile(r"^[a-f0-9]{32}\.mp4$")
VIDEO_POLL_INTERVAL_SECONDS = 10
VIDEO_POLL_MAX_SECONDS = 600
_active_jobs: dict[str, dict] = {}

def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)

def resolve_gemini_endpoint(db_session_factory=None):
    from core.database import SessionLocal, ModelEndpoint
    db = (db_session_factory or SessionLocal)()
    try:
        for ep in db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all():
            base = (ep.base_url or "").rstrip("/")
            if "generativelanguage.googleapis.com" in base and getattr(ep, "api_key", None):
                if base.endswith("/openai"): base = base[: -len("/openai")]
                if not base.endswith("/v1beta"): base = base.rsplit("/v1", 1)[0].rstrip("/") + "/v1beta"
                return base, ep.api_key
    finally:
        db.close()
    raise RuntimeError("Geen Gemini-endpoint met API-key geconfigureerd")
# start_generation / poll_operation / download_video / estimate_cost_usd / resolve_video_path /
# start_video_job (validatie: model in VEO_MODELS, aspect in {"16:9","9:16"}, duration in {4,6,8}; defaults uit src.settings.get_setting)
# _run_job: start → loop poll tot done of VIDEO_POLL_MAX_SECONDS → blocked → error "Geblokkeerd door Veo safety-filter (niet gefactureerd)"
#           → download naar tmp + os.replace → schrijf <job_id>.owner → status done, video_url=f"/api/video/{job_id}.mp4"
# get_job: snapshot; fallback op schijf via .owner-bestand
# cleanup_orphaned_videos: verwijder *.mp4/*.owner/*.tmp ouder dan max_age
```

API-key nooit loggen; foutteksten bevatten hooguit statuscode + eerste 200 tekens van de body.

- [ ] **Step 4: implement `routes/video_routes.py`** (factory `setup_video_routes()`), `src/constants.py`, `app.py`-wiring (router + janitor), `src/interactive_gate.py`.
- [ ] **Step 5: run tests PASS**, `py_compile`, `pytest tests/ -k "video or interactive_gate or constants" -q`.
- [ ] **Step 6: commit** `feat(video): Veo 3.1 video-generation jobs, routes and janitor`

### Task 3 (agent C): tool `generate_video` + privilege + SSE-doorgifte

**Files:**
- Create: `src/agent_tools/video_tools.py`
- Modify: `src/agent_tools/__init__.py` (TOOL_HANDLERS["generate_video"]), `src/agent_loop.py` (twee doorgifte-plekken ~3597 en ~3698: naast de image-keys ook `("video_job_id","video_model","video_status","video_cost_estimate","video_url")`), `routes/chat_routes.py` (~1125: `if not _privs.get("can_generate_videos", True): disabled_tools.add("generate_video")`; en naast de `image_gen_enabled`-check voor globale disable: `if not get_setting("video_gen_enabled", False): disabled_tools.add("generate_video")` — grep hoe `image_gen_enabled` daar gebruikt wordt en spiegel het), `core/auth.py` (`"can_generate_videos": True`), `static/js/admin.js` (`can_generate_videos: 'Video generation'`)
- Test: `tests/test_agent_tools_video.py`, `tests/test_agent_loop_video_forward.py`

**Interfaces:** Consumes `src.video_gen.start_video_job(prompt, owner, model=None, aspect_ratio=None, duration_seconds=None)` en `estimate_cost_usd` (in tests gemockt via `monkeypatch.setattr("src.agent_tools.video_tools.video_gen", fake)`). Produces tool-result-dict zoals in het contract.

- [ ] **Step 1: failing tests**

```python
# tests/test_agent_tools_video.py
import json, pytest
from src.agent_tools.video_tools import GenerateVideoTool

class FakeVG:
    def __init__(self): self.calls = []
    def start_video_job(self, prompt, owner, **kw): self.calls.append((prompt, owner, kw)); return "a"*32
    def estimate_cost_usd(self, model, duration, resolution="720p"): return 3.2
    VEO_MODELS = ("veo-3.1-generate-preview",)

async def test_json_args_start_job(monkeypatch):
    fake = FakeVG(); monkeypatch.setattr("src.agent_tools.video_tools.video_gen", fake)
    monkeypatch.setattr("src.agent_tools.video_tools.get_setting", lambda k, d=None: {"video_gen_enabled": True, "video_model": "veo-3.1-generate-preview"}.get(k, d))
    r = await GenerateVideoTool().execute(json.dumps({"prompt": "a cat", "aspect_ratio": "9:16", "duration_seconds": 4}), {"owner": "ed"})
    assert r["video_job_id"] == "a"*32 and r["video_status"] == "running" and r["video_url"] is None
    assert fake.calls[0][0] == "a cat" and fake.calls[0][2]["aspect_ratio"] == "9:16"
    assert "Video generation started" in r["output"]

async def test_plain_text_prompt(monkeypatch): ...  # content "a dog surfing" (geen JSON) → prompt = hele tekst
async def test_disabled_setting(monkeypatch): ...   # video_gen_enabled False → {"error": ..., "exit_code": 1}, geen job
async def test_missing_endpoint(monkeypatch): ...   # start_video_job raises RuntimeError → error-dict met de melding
```

Voor de doorgifte-test: zoek de bestaande test die controleert dat `image_url` uit een tool-result in `tool_output` belandt (grep `image_url` in tests/) en spiegel die voor `video_job_id`. Bepaal `owner` in de handler zoals andere tools dat doen (grep `ctx.get("owner")` / `ctx.get("user")` in `src/agent_tools/`).

- [ ] **Step 2: FAIL** → **Step 3: implement** (`GenerateVideoTool.execute(content, ctx)`: JSON-parse zoals `WebFetchTool`, anders hele tekst = prompt; validatie; `video_gen.start_video_job(...)`; return-dict) → **Step 4: PASS** + `pytest tests/ -k "agent_tools or agent_loop or chat_routes" -q` → **Step 5: commit** `feat(tools): generate_video tool with privilege gate and SSE forwarding`

### Task 4 (agent D): settings — backend keys + kaart "Video Generation"

**Files:**
- Modify: `src/settings.py` (defaults + `_PER_USER_KEYS`), `static/index.html` (kaart direct onder de Image Generation-kaart, zelfde markup: toggle `set-videoEnabledToggle`, `set-videoModelSelect` met de 3 Veo-modellen, `set-videoResolutionSelect` 720p/1080p, `set-videoAspectSelect` 16:9/9:16, `set-videoDurationSelect` 4/6/8, `set-videoCostLine`, `set-videoSettingsMsg`; monochroom inline SVG-icoon, geen emoji), `static/js/settings.js` (`initVideoSettings()` naast `initImageSettings()`, aangeroepen op dezelfde plek; kostenregel `Estimated cost per clip: $X.XX` uit tarieftabel × duur; opslaan via `POST /api/auth/settings`, `res.ok` controleren zoals `saveSTT` sinds #146)
- Test: `tests/test_settings_video_keys.py` (defaults aanwezig, per-user key, `get_setting("video_model")`), `tests/test_settings_video_js.py` (node: `settings.js` bevat `initVideoSettings` en de ids; `node --check`)

- [ ] Steps: failing tests → implement → PASS → `node --check static/js/settings.js` → commit `feat(settings): video generation card (Veo model, resolution, aspect, duration)`

### Task 5 (agent E): chat-frontend — statusbubble, poll, `<video>`-render, history-replay

**Files:**
- Modify: `static/js/chatRenderer.js` (nieuwe export `buildVideoBubble(job)` en `buildVideoPendingBubble(jobId, model, costEstimate)`; history-replay bij ~2494: `if (ev.video_job_id) { ... }` — mét `ev.video_url` → direct video, anders pending + poll), `static/js/chat.js` (~2810: naast `json.image_url` een blok `if (json.video_job_id) {...}` dat de pending-bubble toevoegt en `startVideoJobPoll(jobId, bubbleEl)` start: `GET /api/video/jobs/{id}` elke 5 s, max 12 min; `done` → vervang inhoud door `<video controls preload="metadata" src=job.video_url>`; `error` → rode regel `job.error`; 404 → "Video job no longer known"). Toon in de pending-bubble: `Generating video with {model} (usually 1-3 min, ~$X.XX)...` met de bestaande spinner-klasse (grep `agent-thread-status` / `spinner` in chat.css).
- Test: `tests/test_chat_video_js.py` (node-harness zoals `tests/test_voice_mode_js.py`: laad `chatRenderer.js` in een jsdom-loze mini-DOM of test pure functies; controleer dat `buildVideoBubble({video_url:"/api/video/x.mp4"})` een `<video>` met die src oplevert en dat pending-bubble de kosten toont; `node --check` op beide bestanden)

- [ ] Steps: failing test → implement → PASS → `node --check` → commit `feat(chat): inline video bubble with job polling for generate_video`

### Task 6 (orkestrator): integratie, prod-config, smoke, deploy

- [ ] Merge-volgorde: B (#video_gen) → C (tool) → A (intents) → D (settings) → E (frontend); rebase bij conflicten.
- [ ] Prod: `image_model = "gpt-image-1.5"`, `video_gen_enabled = True` via `src.settings` in de container.
- [ ] Smoke op :7001 (verse datadir, Ollama gpt-oss:latest als chatmodel, echte OpenAI- en Gemini-endpoints met keys uit prod-DB gekopieerd via de UI): "maak een afbeelding van een vuurtoren bij nacht" in Chat-modus → escalatie-log `chat→agent auto-escalation: category=image` → inline beeld `image_model=gpt-image-1.5`; "maak een video van golven bij zonsondergang, 4 seconden" → pending-bubble → applog poll → `<video>` afspeelbaar (één echte Veo-run); reload → video blijft; 360px-check; settings-kaart opslaan; console zonder fouten.
- [ ] Deploy `docker compose up -d --build`, live-check, sessielog, memory.
