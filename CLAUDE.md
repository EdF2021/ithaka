# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Ithaka is Ed's self-hosted AI workspace (chat/agents, cookbook model serving, deep research, documents, email, notes/tasks/calendar) built on FastAPI + a vanilla-JS frontend, at `EdF2021/ithaka` (public). It started as a fork but is fully detached — no upstream remote; origin and third-party credits live in `ACKNOWLEDGMENTS.md`. AGPL-3.0. Branch model: `dev` is the default working branch, `main` is the curated/stable branch — PRs target `dev`.

Session logs live in `docs/sessions/` — read the most recent one before starting substantial work. Operational guides (setup, backup/restore, security CI, PR-blocker audit) live in `docs/`; a deeper runtime/manager inventory lives in `specs/architecture-runtime-inventory.md`.

## Commands

The local virtualenv is **`.venv`** (repo docs say `./venv` — that's stale; use `.venv/bin/python`).

```bash
# Docker stack (canonical way to run): app + chromadb + searxng + ntfy + tailscale sidecar
docker compose up -d --build          # containers ithaka-*, app on http://localhost:7000
                                      # GPU variants: docker-compose.gpu-nvidia.yml / gpu-amd.yml
docker compose logs --tail=120 ithaka  # first admin password is printed here on a fresh data volume
docker compose config                  # validate after compose changes

# Native dev server
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 7000

# Isolated smoke instance alongside the Docker stack (fresh data dir, port 7001;
# create the first account via POST /api/auth/setup)
ITHAKA_DATA_DIR=<fresh-dir> .venv/bin/python -m uvicorn app:app --port 7001

# Tests
.venv/bin/python -m pytest                          # full suite
.venv/bin/python -m pytest tests/test_foo.py -k bar # single test
.venv/bin/python tests/run_focus.py --area security # focused slice (validates names)
.venv/bin/python tests/run_focus.py --area services --sub-area cookbook
.venv/bin/python tests/run_focus.py --last-failed
.venv/bin/python tests/run_focus.py --area routes --fast   # fast lane = "not slow"

# Quick syntax checks
.venv/bin/python -m py_compile app.py routes/*.py src/*.py
node --check static/js/<changed-file>.js
```

Tests are auto-tagged at collection by filename (`tests/_taxonomy.py`): `area_*` (security, routes, services, cli, js, helpers, unit, uncategorized) plus a finer `sub_*` marker, so `-m "area_services and sub_cookbook"` also works. Pytest runs with `asyncio_mode = "auto"` — async test functions need no marker. Testing rules live in `tests/TESTING_STANDARD.md` (policy) and `tests/README.md` (helper reference). There is no linter/formatter configured; the checks are pytest plus the syntax checks above. CI (`.github/workflows/ci.yml`) runs the same things on PRs — full pytest on Python 3.11 and `node --check` over changed JS — plus separate secret-scan/container-scan/dependency-review workflows (see `docs/security-ci.md`).

### UI smoke test (desktop & mobile)

Before merging anything that touches a page, navigation, session state, or file paths: drive the app in a real browser via the chrome-devtools MCP tools at `http://localhost:7000` (or the :7001 smoke instance) — desktop viewport **and** 360 px mobile. Check the flows the change touches plus the console for errors, and paste the full smoke output (commands + result, not just "green") into the PR chat before merging.

### Gotchas & common pitfalls

- **Tailscale MTU blackhole** — packets > ~1200 B drop silently on the Windows↔WSL sidecar (health checks pass, pages/TLS hang). Fix is baked into `docker-compose.yml` (`TS_DEBUG_MTU=1130`); mirror it in the GPU compose variants. Verify with `tailscale ping --size`.
- **Trailing-dot host entry** — Windows Chrome doesn't resolve `*.ts.net` (DoH); add a hosts entry via admin PowerShell:
  ```powershell
  Add-Content C:\Windows\System32\drivers\etc\hosts "100.72.181.25 ithaka.tailb21d35.ts.net."
  ```
- **HF_TOKEN for gated HF repos** — the cookbook reads a stored token via `load_stored_hf_token()` (`src/tools/cookbook.py`), set in Cookbook → Settings or `.env`. Standalone scripts (`diffusion_server.py`, `add_hwfit_models.py`, …) only read the env var and miss the stored token.
- **npx MCP server "Connection closed"** — usually a corrupt `_npx` cache entry (directory without `bin/`) in `/app/.npm` from a killed install; remove it and reinstall with `HOME=/app`. Since #70 the npm cache persists in a volume and stdio MCP sessions run in dedicated tasks, so this should stay rare.
- **`.claude.local.md`** — personal overrides (keybindings, local shortcuts). Git-ignored, merged at runtime.

## Architecture

- **`app.py`** — slim orchestrator (~1300 lines): loads `.env`, constructs managers/services, then wires ~48 routers via `app.include_router(setup_*_routes(deps...))` factory calls. Routers get their dependencies injected as arguments, not via globals.
- **`routes/`** — HTTP layer only, one module per feature (`chat_routes.py`, `cookbook_routes.py`, `email_routes.py`, …) plus `_validators.py` and per-feature helper modules.
- **`src/`** — the bulk of the logic: agent loop (`agent_loop.py`, `tool_execution.py`, `tool_policy.py`, `tool_schemas.py`, `tool_index.py` — RAG-based tool selection: tool descriptions live in a Chroma collection, top-K retrieved per message instead of all-in-prompt), chat pipeline (`chat_handler.py`, `chat_processor.py`, `llm_core.py`), RAG (`rag_manager.py`, `chroma_client.py`, `embeddings.py`), MCP (`mcp_manager.py`, `mcp_presets.py` — server-side catalog of connector presets incl. Google Calendar/Drive via in-app OAuth), task scheduler, security helpers (`prompt_security.py`, `url_safety.py`, `tool_security.py`).
- **`services/`** — subsystem packages: `research/`, `search/`, `shell/`, `stt/`, `tts/`, `memory/`, `hwfit/`, `docs/`, `faces/`, `youtube/`.
- **`core/`** — auth (`AuthManager`), database, middleware, session manager. `core/constants.py` only re-exports `src/constants.py` for backward compatibility.
- **`static/`** — vanilla JS ES modules, no framework, no build step. `static/index.html` loads the scripts; `static/js/` holds 80+ modules (`MODULE_SUMMARY.md` there is a partial, historical overview). Continuous voice mode is cross-cutting: `static/js/voiceMode.js` orchestrates the hands-free loop (mic arms → VAD detects end-of-speech and auto-stops → STT transcribes → auto-send → TTS auto-plays → mic re-arms), with end-of-speech detection in `voiceRecorder.js` (`createVoiceActivityDetector`, unit-tested in `tests/test_voice_mode_js.py`), hooks in `chat.js` (`onStreamStart`/`onResponseComplete` — also on the `!res.ok` error paths) and toggle/persistence in `app.js`. Requires STT enabled; degrades gracefully without TTS. STT is configured via the card in Settings → AI Defaults (`initSttSettings` in `settings.js`; restored in #71) or `POST /api/auth/settings` — `stt_language` is normalized server-side to ISO-639-1 (#75), and a standing `response_language` setting (empty by default) is injected into the system message each turn when set (`src/chat_processor.py`).
- **`scripts/`** — CLI entry points (`ithaka`, `ithaka-backup`, `ithaka-cookbook`, …) and maintenance scripts.
- **Notebooks** (NotebookLM-style, cross-cutting): `routes/notebook_routes.py` + `src/notebook_ingest.py` + `static/js/notebooks.js`. Sources are indexed per notebook via a `notebook_id` metadata filter in the RAG layer (`rag_manager.py` / `rag_vector.py`); notebook chat runs strictly grounded with paragraph-level `[n, ¶N]` citations (chunk metadata carries `paragraph_ref`/`section_hint`; multi-turn follow-ups are LLM-condensed to a standalone RAG query) and a server-side tool lockdown (all tools + MCP disabled). Text artifacts (study guide/briefing/FAQ/quiz/mindmap/flashcards/data table) live in `src/notebook_artifacts.py`, with a validator-retry seam: slide decks validate against a JSON schema, and infographic/flashcards/mindmap each have a format validator (`validate_*_markdown` in their module) that triggers regeneration on malformed output; renderers in `src/notebook_flashcards.py`, `src/notebook_slides.py` (slide-JSON schema + standalone viewer), `src/notebook_infographic.py`, `src/notebook_mindmap.py` (mermaid mindmap parser + interactive collapsible viewer in `notebookWorkspace.js`). The podcast pipeline (LLM dialogue script → per-turn TTS → streaming WAV concat, async job mirroring `research_handler.py`) lives in `src/notebook_audio.py`; the video pipeline (slide JSON + narration → Pillow PNG frames → per-slide TTS → ffmpeg mp4; ffmpeg + fonts-dejavu-core are in the Docker image) in `src/notebook_video.py`, served via `/api/notebook-video/{fn}`. Both host hourly janitors for orphaned media files (wired in `app.py`). Web sources: search bar in the sources panel → SearXNG → `POST .../sources/url` → `ingest_notebook_url` (fetch via `services/search/content.py`, never the divergent `src/search/` duplicate). Podcast and video status polls are on the passive list in `src/interactive_gate.py` (`_PASSIVE_PATTERNS`). All generated notebook output (chat, artifacts, podcast, video, question suggestions) is forced to Dutch: every generation prompt embeds `DUTCH_OUTPUT_RULE` from `src/notebook_language.py` — new generators must include it, and the rule is changed there, never inline. Datamodel incl. `NotebookArtifact` (with `audio_path`/`video_path`) and `NotebookSource.url` lives in `core/database.py`. **Gotcha:** a synchronous LLM call inside a tracked request self-deadlocks on two background gates (`wait_for_interactive_quiet` + the `_local_model_slot` workload gate) — pass `wait_for_quiet=False, workload="foreground"`, or better: use an async job like the podcast does. Regression test: `tests/test_notebooks_gate_seam.py`. Design docs: `docs/notebooklm-gap-analyse.md` (status header lists all phase session logs) and `docs/superpowers/specs/` (fase 2, 3 en 4).

### Constants rule (enforced in review)

`src/constants.py` is the single source of truth for paths and config. `DATA_DIR` is the **only** place `ITHAKA_DATA_DIR` is read. Never build writable paths from `Path(__file__)`, hardcode `/app/...`, or use relative `"data/..."` strings — import the named constant (`AUTH_FILE`, `SETTINGS_FILE`, `CHROMA_DIR`, …) or add one. For loopback/internal URLs use `internal_api_base()` from `src.constants`, never a hardcoded `http://localhost:7000`. The source tree is read-only in Docker; guard directory creation so unwritable paths degrade instead of crashing at import.

## Conventions

- **Commits:** Conventional Commits — `type(scope): summary` (e.g. `fix(search): …`, `feat(notes): …`).
- **Visual changes** (anything touching CSS/HTML/SVG or DOM-drawing JS): run the app and look at it in a browser — tests alone don't count; attach a screenshot (mobile too when relevant). Reuse existing CSS variables (`--red`, `--fg`, `--bg`, `--card`, `--border`, …) and existing button/input/card classes; don't introduce new color values, font sizes, or parallel components. **No Unicode emoji in UI or code** — inline monochrome SVG or plain text. Primary UI font is monospaced (`Fira Code`); dark theme is default and light-mode work goes through the existing theme system.
- Small, single-purpose PRs; no broad rewrites or formatting-only changes.
- **Dependency pin:** `mcp<2` in `requirements.txt` is deliberate — mcp 2.0 drops the `Server.list_tools()` decorator API that `mcp_servers/*` use. Don't bump it without porting those servers.

## Deployment specifics of this fork

- Remote access runs through a Tailscale sidecar (`tailscale` service in compose): bare `tailscaled --tun=userspace-networking`, node `ithaka.tailb21d35.ts.net`, app reachable on port 7000 within the tailnet (HTTPS via `tailscale serve` → https://ithaka.tailb21d35.ts.net). Do **not** switch it back to containerboot — without `TS_AUTHKEY` containerboot regenerates the nodekey in a loop and auth URLs expire instantly; the fix is bare `tailscaled` + a one-time `docker exec ithaka-tailscale-1 tailscale up --accept-dns=false`.
- `.env` uses `APP_BIND=0.0.0.0` (WSL NAT shields the LAN). The API-token prefix `ody_` is intentionally kept from upstream for existing tokens.
- Google Calendar as CalDAV account: use the **legacy** endpoint `https://www.google.com/calendar/dav/<email>/user/` with a Google app password — the modern `apidata.googleusercontent.com/caldav/v2/...` endpoint is OAuth-only and returns Unauthorized for app passwords (verified live 2026-08-31).
- Outlook.com / Microsoft 365 accounts go through the bundled `davmail` service (internal-only, hostname `davmail`) — Microsoft basic auth for IMAP/SMTP is dead, DavMail is the OAuth bridge. Personal accounts (outlook.com/hotmail/live) need `DAVMAIL_TENANT=consumers` + Graph mode and a one-time authorize-code bootstrap — device-code (`DAVMAIL_AUTHENTICATION=O365DeviceCode`) fails for them with a false "expired" and is only for org accounts. The refresh token then persists in the `davmail-config` volume. Full setup and troubleshooting: `docs/email-outlook.md`.
