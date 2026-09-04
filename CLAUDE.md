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
                                      # first admin password is printed in `docker compose logs ithaka` on a fresh data volume

# Native dev server
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 7000

# Isolated smoke instance alongside the Docker stack (fresh data dir, port 7001;
# create the first account via POST /api/auth/setup)
ITHAKA_DATA_DIR=<fresh-dir> .venv/bin/python -m uvicorn app:app --port 7001

# Focused test slices (pytest itself is standard)
.venv/bin/python tests/run_focus.py --area security # focused slice (validates names)
.venv/bin/python tests/run_focus.py --area services --sub-area cookbook
.venv/bin/python tests/run_focus.py --last-failed
.venv/bin/python tests/run_focus.py --area routes --fast   # fast lane = "not slow"
```

Tests are auto-tagged at collection by filename (`tests/_taxonomy.py`): `area_*` (security, routes, services, cli, js, helpers, unit, uncategorized) plus a finer `sub_*` marker, so `-m "area_services and sub_cookbook"` also works. Pytest runs with `asyncio_mode = "auto"` — async test functions need no marker. Testing rules live in `tests/TESTING_STANDARD.md` (policy) and `tests/README.md` (helper reference). There is no linter/formatter configured; the checks are pytest plus `py_compile` / `node --check`. CI (`.github/workflows/ci.yml`) runs the same things on PRs — full pytest on Python 3.11 and `node --check` over changed JS — plus separate secret-scan/container-scan/dependency-review workflows (see `docs/security-ci.md`).

### UI smoke test (desktop & mobile)

Before merging anything that touches a page, navigation, session state, or file paths: drive the app in a real browser via the chrome-devtools MCP tools at `http://localhost:7000` (or the :7001 smoke instance) — desktop viewport **and** 360 px mobile. Check the flows the change touches plus the console for errors, and paste the full smoke output (commands + result, not just "green") into the PR chat before merging.

### Gotchas & deployment specifics

Tailscale sidecar (MTU blackhole, containerboot auth-race, hosts entry), DavMail/Outlook bootstrap, Google CalDAV legacy endpoint, HF_TOKEN for gated repos, npx MCP cache, and the worktree-based deploy recipe live in the `ithaka-ops` skill (`.claude/skills/ithaka-ops/SKILL.md`) — invoke it when touching compose, the Tailscale/davmail services, email/calendar accounts, or remote access.

## Architecture

- **`app.py`** — slim orchestrator (~1300 lines): loads `.env`, constructs managers/services, then wires ~48 routers via `app.include_router(setup_*_routes(deps...))` factory calls. Routers get their dependencies injected as arguments, not via globals.
- **`routes/`** — HTTP layer only, one module per feature (`chat_routes.py`, `cookbook_routes.py`, `email_routes.py`, …) plus `_validators.py` and per-feature helper modules.
- **`src/`** — the bulk of the logic: agent loop (`agent_loop.py`, `tool_execution.py`, `tool_policy.py`, `tool_schemas.py`, `tool_index.py` — RAG-based tool selection: tool descriptions live in a Chroma collection, top-K retrieved per message instead of all-in-prompt), chat pipeline (`chat_handler.py`, `chat_processor.py`, `llm_core.py`), RAG (`rag_manager.py`, `chroma_client.py`, `embeddings.py`), MCP (`mcp_manager.py`, `mcp_presets.py` — server-side catalog of connector presets incl. Google Calendar/Drive via in-app OAuth), task scheduler, security helpers (`prompt_security.py`, `url_safety.py`, `tool_security.py`).
- **`services/`** — subsystem packages: `research/`, `search/`, `shell/`, `stt/`, `tts/`, `memory/`, `hwfit/`, `docs/`, `faces/`, `youtube/`.
- **`core/`** — auth (`AuthManager`), database, middleware, session manager. `core/constants.py` only re-exports `src/constants.py` for backward compatibility.
- **`static/`** — vanilla JS ES modules, no framework, no build step. `static/index.html` loads the scripts; `static/js/` holds 80+ modules (`MODULE_SUMMARY.md` there is a partial, historical overview). Continuous voice mode is cross-cutting: `static/js/voiceMode.js` orchestrates the hands-free loop (mic arms → VAD detects end-of-speech and auto-stops → STT transcribes → auto-send → TTS auto-plays → mic re-arms), with end-of-speech detection in `voiceRecorder.js` (`createVoiceActivityDetector`, unit-tested in `tests/test_voice_mode_js.py`), hooks in `chat.js` (`onStreamStart`/`onResponseComplete` — also on the `!res.ok` error paths) and toggle/persistence in `app.js`. Requires STT enabled; degrades gracefully without TTS. STT is configured via the card in Settings → AI Defaults (`initSttSettings` in `settings.js`; restored in #71) or `POST /api/auth/settings` — `stt_language` is normalized server-side to ISO-639-1 (#75), and a standing `response_language` setting (empty by default) is injected into the system message each turn when set (`src/chat_processor.py`). A second, independent toggle "Realtime Gesprek" (`static/js/realtimeVoice.js`, `services/realtime/`, `POST /api/realtime/session`) talks to the OpenAI Realtime API directly over WebRTC (fase 1, #162); fase 2 gives that session one function tool `ask_ithaka` — the browser forwards the call to `POST /api/realtime/ask`, which runs the question one-shot through the agent loop on the task/utility model chain (`services/realtime/realtime_ask.py`, never `workload="background"`); toggle `realtime_tools_enabled` (global). The session's own input transcription uses `realtime_transcription_model` (default `gpt-realtime-whisper`, Realtime-only model) — deliberately separate from `stt_model`, which serves voice mode and meeting minutes via `/audio/transcriptions` (`gpt-transcribe` / `gpt-4o-mini-transcribe` / `whisper-1`). Specs: `docs/superpowers/specs/2026-09-03-realtime-voice-mode-design.md`, `…/2026-09-04-realtime-voice-tools-design.md`.
- **`scripts/`** — CLI entry points (`ithaka`, `ithaka-backup`, `ithaka-cookbook`, …) and maintenance scripts.
- **Notebooks** (NotebookLM-style, cross-cutting: `routes/notebook_routes.py`, `src/notebook_*.py`, `static/js/notebook*.js`): full map, pipelines and conventions load automatically from `.claude/rules/notebooks.md` when you work in those files. Two rules that always hold: every notebook generation prompt embeds `DUTCH_OUTPUT_RULE` from `src/notebook_language.py` (change it there, never inline), and **a synchronous LLM call inside a tracked request self-deadlocks** on `wait_for_interactive_quiet` + the `_local_model_slot` workload gate — pass `wait_for_quiet=False, workload="foreground"`, or use an async job (regression test: `tests/test_notebooks_gate_seam.py`). This also bites any one-shot `stream_agent_loop` caller (see `services/realtime/realtime_ask.py`).
- **Meetings** (meeting recorder → notulen, #175): `static/js/meetings.js` records the mic with a 30 s `MediaRecorder` timeslice and uploads chunks sequentially to `routes/meeting_routes.py` (append to `MEETING_AUDIO_DIR/<id>.webm`); `finish` starts an async job in `src/meeting_minutes.py` (podcast-job shape: ffmpeg split into 10-min ogg/opus → parallel `STTService.transcribe(prompt=key terms)` → correction pass → Ed's recursive head/tail condensation → strict Markdown minutes template with validator → Library `Document`). Status poll `GET /api/meetings/{id}` is passive; hourly janitor; spec `docs/superpowers/specs/2026-09-04-meeting-recorder-design.md`.

### Constants rule (enforced in review)

`src/constants.py` is the single source of truth for paths and config. `DATA_DIR` is the **only** place `ITHAKA_DATA_DIR` is read. Never build writable paths from `Path(__file__)`, hardcode `/app/...`, or use relative `"data/..."` strings — import the named constant (`AUTH_FILE`, `SETTINGS_FILE`, `CHROMA_DIR`, …) or add one. For loopback/internal URLs use `internal_api_base()` from `src.constants`, never a hardcoded `http://localhost:7000`. The source tree is read-only in Docker; guard directory creation so unwritable paths degrade instead of crashing at import.

## Conventions

- **Commits:** Conventional Commits — `type(scope): summary` (e.g. `fix(search): …`, `feat(notes): …`).
- **Visual changes** (anything touching CSS/HTML/SVG or DOM-drawing JS): run the app and look at it in a browser — tests alone don't count; attach a screenshot (mobile too when relevant). Reuse existing CSS variables (`--red`, `--fg`, `--bg`, `--card`, `--border`, …) and existing button/input/card classes; don't introduce new color values, font sizes, or parallel components. **No Unicode emoji in UI or code** — inline monochrome SVG or plain text. Primary UI font is monospaced (`Fira Code`); dark theme is default and light-mode work goes through the existing theme system.
- Small, single-purpose PRs; no broad rewrites or formatting-only changes.
- **Dependency pin:** `mcp<2` in `requirements.txt` is deliberate — mcp 2.0 drops the `Server.list_tools()` decorator API that `mcp_servers/*` use. Don't bump it without porting those servers.

## Deployment

Tailscale sidecar, DavMail/Outlook and Google CalDAV specifics: see the `ithaka-ops` skill (`.claude/skills/ithaka-ops/SKILL.md`). Never switch the Tailscale sidecar back to containerboot (auth-URL race — details in the skill).
