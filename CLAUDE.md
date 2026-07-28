# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Ithaka is Ed's private fork of `pewdiepie-archdaemon/odysseus` (detached, no upstream remote): a self-hosted AI workspace (chat/agents, cookbook model serving, deep research, documents, email, notes/tasks/calendar) built on FastAPI + a vanilla-JS frontend. AGPL-3.0. Branch model: `dev` is the default working branch, `main` is the curated/stable branch — PRs target `dev`.

Session logs live in `docs/sessions/` — read the most recent one before starting substantial work.

## Commands

The local virtualenv is **`.venv`** (repo docs say `./venv` — that's stale; use `.venv/bin/python`).

```bash
# Docker stack (canonical way to run): app + chromadb + searxng + ntfy + tailscale sidecar
docker compose up -d --build          # containers ithaka-*, app on http://localhost:7000
docker compose logs --tail=120 ithaka
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

Tests are auto-tagged at collection by filename (`tests/_taxonomy.py`): `area_*` (security, routes, services, cli, js, helpers, unit, uncategorized) plus a finer `sub_*` marker, so `-m "area_services and sub_cookbook"` also works. Testing rules live in `tests/TESTING_STANDARD.md` (policy) and `tests/README.md` (helper reference).

## Architecture

- **`app.py`** — slim orchestrator (~1300 lines): loads `.env`, constructs managers/services, then wires ~40 routers via `app.include_router(setup_*_routes(deps...))` factory calls. Routers get their dependencies injected as arguments, not via globals.
- **`routes/`** — HTTP layer only, one module per feature (`chat_routes.py`, `cookbook_routes.py`, `email_routes.py`, …) plus `_validators.py` and per-feature helper modules.
- **`src/`** — the bulk of the logic: agent loop (`agent_loop.py`, `tool_execution.py`, `tool_policy.py`, `tool_schemas.py`), chat pipeline (`chat_handler.py`, `chat_processor.py`, `llm_core.py`), RAG (`rag_manager.py`, `chroma_client.py`, `embeddings.py`), MCP (`mcp_manager.py`), task scheduler, security helpers (`prompt_security.py`, `url_safety.py`, `tool_security.py`).
- **`services/`** — subsystem packages: `research/`, `search/`, `shell/`, `stt/`, `tts/`, `memory/`, `hwfit/`, `docs/`, `faces/`, `youtube/`.
- **`core/`** — auth (`AuthManager`), database, middleware, session manager. `core/constants.py` only re-exports `src/constants.py` for backward compatibility.
- **`static/`** — vanilla JS ES modules, no framework, no build step. `static/index.html` loads the scripts; `static/js/` holds 65+ modules (`MODULE_SUMMARY.md` there is a partial, historical overview).
- **`scripts/`** — CLI entry points (`ithaka`, `ithaka-backup`, `ithaka-cookbook`, …) and maintenance scripts.

### Constants rule (enforced in review)

`src/constants.py` is the single source of truth for paths and config. `DATA_DIR` is the **only** place `ITHAKA_DATA_DIR` is read. Never build writable paths from `Path(__file__)`, hardcode `/app/...`, or use relative `"data/..."` strings — import the named constant (`AUTH_FILE`, `SETTINGS_FILE`, `CHROMA_DIR`, …) or add one. For loopback/internal URLs use `internal_api_base()` from `src.constants`, never a hardcoded `http://localhost:7000`. The source tree is read-only in Docker; guard directory creation so unwritable paths degrade instead of crashing at import.

## Conventions

- **Commits:** Conventional Commits — `type(scope): summary` (e.g. `fix(search): …`, `feat(notes): …`).
- **Visual changes** (anything touching CSS/HTML/SVG or DOM-drawing JS): run the app and look at it in a browser — tests alone don't count; attach a screenshot (mobile too when relevant). Reuse existing CSS variables (`--red`, `--fg`, `--bg`, `--card`, `--border`, …) and existing button/input/card classes; don't introduce new color values, font sizes, or parallel components. **No Unicode emoji in UI or code** — inline monochrome SVG or plain text. Primary UI font is monospaced (`Fira Code`); dark theme is default and light-mode work goes through the existing theme system.
- Small, single-purpose PRs; no broad rewrites or formatting-only changes.

## Deployment specifics of this fork

- Remote access runs through a Tailscale sidecar (`tailscale` service in compose): bare `tailscaled --tun=userspace-networking`, node `ithaka.tailb21d35.ts.net`, app reachable on port 7000 within the tailnet. Do **not** switch it back to containerboot — without `TS_AUTHKEY` containerboot regenerates the nodekey in a loop and auth URLs expire instantly; the fix is bare `tailscaled` + a one-time `docker exec ithaka-tailscale-1 tailscale up --accept-dns=false`.
- `.env` uses `APP_BIND=0.0.0.0` (WSL NAT shields the LAN). The API-token prefix `ody_` is intentionally kept from upstream for existing tokens.
