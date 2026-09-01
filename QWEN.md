# QWEN.md

Guidance for Qwen Code when working in the Ithaka repository. A companion to `CLAUDE.md` — read the most recent session log in `docs/sessions/` before starting substantial work.

## What this is

Ithaka is a self-hosted AI workspace (chat/agents, cookbook model serving, deep research, NotebookLM-style notebooks, documents, email, notes/tasks/calendar) built on FastAPI + a vanilla-JS frontend. Public repo: `EdF2021/ithaka`. AGPL-3.0-or-later. It started as a fork but is fully detached — no upstream remote; credits in `ACKNOWLEDGMENTS.md`.

**Branch model:** `dev` is the default working branch (all PRs target it); `main` is the curated/stable branch, fast-forwarded to a stable `dev` commit at each release.

## Architecture

- **`app.py`** — slim orchestrator (~1300 lines): loads `.env`, constructs managers/services, wires ~48 routers via `app.include_router(setup_*_routes(deps...))` factory calls. Dependencies are injected as arguments, not globals.
- **`routes/`** — HTTP layer only, one module per feature (`chat_routes.py`, `cookbook_routes.py`, `email_routes.py`, `notebook_routes.py`, …) plus `_validators.py` and per-feature helper modules.
- **`src/`** — bulk of the logic:
  - Agent loop: `agent_loop.py`, `tool_execution.py`, `tool_policy.py`, `tool_schemas.py`, `tool_index.py` — RAG-based tool selection (tool descriptions in a Chroma collection, top-K retrieved per message instead of all-in-prompt).
  - Chat pipeline: `chat_handler.py`, `chat_processor.py`, `llm_core.py`.
  - RAG: `rag_manager.py`, `chroma_client.py`, `embeddings.py`, `rag_vector.py`, `rag_singleton.py`.
  - MCP: `mcp_manager.py`, `mcp_presets.py` (server-side catalog of connector presets incl. Google Calendar/Drive via in-app OAuth).
  - Security: `prompt_security.py`, `url_safety.py`, `tool_security.py`, `url_security.py`.
  - Other: `task_scheduler.py`, `deep_research.py`, `research_handler.py`, `notebook_*.py`, `settings.py`, `constants.py`.
  - **`src/agent_tools/`** — tool implementations (`web_tools.py`, `admin_tools.py`, `cookbook.py`, etc.).
- **`services/`** — subsystem packages: `research/`, `search/`, `shell/`, `stt/`, `tts/`, `memory/`, `hwfit/`, `docs/`, `faces/`, `youtube/`, `plugins/`.
- **`core/`** — auth (`AuthManager`), database, middleware, session manager. `core/constants.py` re-exports `src/constants.py` for backward compatibility.
- **`static/`** — vanilla JS ES modules, no framework, no build step. `static/index.html` loads the scripts; `static/js/` holds 85+ modules. Continuous voice mode is cross-cutting (`voiceMode.js`, `voiceRecorder.js`, hooks in `chat.js`/`app.js`).
- **`mcp_servers/`** — in-repo MCP server implementations (`email_server.py`, `image_gen_server.py`, `memory_server.py`, `rag_server.py`).
- **`scripts/`** — CLI entry points (`ithaka`, `ithaka-backup`, `ithaka-cookbook`, `ithaka-mail`, …) and maintenance scripts.
- **`tests/`** — pytest suite with taxonomy markers (see below).
- **`docs/sessions/`** — session logs; read the most recent before starting substantial work.
- **`specs/architecture-runtime-inventory.md`** — deeper runtime/manager inventory.

## Building and Running

### Docker (canonical)

```bash
docker compose up -d --build          # ithaka-* containers, app on http://localhost:7000
                                      # GPU: docker-compose.gpu-nvidia.yml / gpu-amd.yml
docker compose logs --tail=120 ithaka  # first admin password printed here on fresh data volume
docker compose config                  # validate after compose changes
```

Stack: `ithaka` + `chromadb` + `searxng` + `ntfy` + `tailscale` sidecar. DavMail gateway for Outlook/Microsoft 365 (internal-only, no host ports).

**Code changes require a rebuild** — a plain restart reuses the old image:
```bash
docker compose up -d --build ithaka
```

### Native dev

```bash
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 7000
```

Isolated smoke instance (fresh data dir, port 7001):
```bash
ITHAKA_DATA_DIR=<fresh-dir> .venv/bin/python -m uvicorn app:app --port 7001
```

### Tests

```bash
.venv/bin/python -m pytest                              # full suite
.venv/bin/python -m pytest tests/test_foo.py -k bar     # single test
.venv/bin/python tests/run_focus.py --area security     # focused slice
.venv/bin/python tests/run_focus.py --area services --sub-area cookbook
.venv/bin/python tests/run_focus.py --last-failed
.venv/bin/python tests/run_focus.py --area routes --fast  # fast lane = "not slow"
```

Tests are auto-tagged at collection by filename (`tests/_taxonomy.py`): `area_*` (security, routes, services, cli, js, helpers, unit, uncategorized) plus finer `sub_*` markers. Pytest runs with `asyncio_mode = "auto"` — async test functions need no marker. Testing policy: `tests/TESTING_STANDARD.md`; helper reference: `tests/README.md`.

### Syntax checks (no linter configured)

```bash
.venv/bin/python -m py_compile app.py routes/*.py src/*.py
node --check static/js/<changed-file>.js
```

### CI (`.github/workflows/ci.yml`)

Runs full pytest on Python 3.11 and `node --check` over changed JS, plus separate secret-scan/container-scan/dependency-review workflows (see `docs/security-ci.md`). pytest job is currently `continue-on-error: true` (known flaky/environment-dependent failures tracked in ROADMAP).

## Key Conventions

### Constants rule (enforced in review)

`src/constants.py` is the single source of truth for paths and config. `DATA_DIR` is the **only** place `ITHAKA_DATA_DIR` is read. Never build writable paths from `Path(__file__)`, hardcode `/app/...`, or use relative `"data/..."` strings — import the named constant (`AUTH_FILE`, `SETTINGS_FILE`, `CHROMA_DIR`, …) or add one. For loopback/internal URLs use `internal_api_base()` from `src.constants`, never a hardcoded `http://localhost:7000`. The source tree is read-only in Docker; guard directory creation so unwritable paths degrade instead of crashing at import.

### Commits

Conventional Commits — `type(scope): summary` (e.g. `fix(search): …`, `feat(notes): …`, `docs(contributing): …`). Common types: `fix`, `feat`, `refactor`, `docs`, `test`, `chore`, `ci`. Keep the subject short and imperative; put the "why" in the body when it isn't obvious.

### Visual / UI changes

Anything touching CSS/HTML/SVG or DOM-drawing JS: run the app and look at it in a browser — tests alone don't count; attach a screenshot (mobile too when relevant). Reuse existing CSS variables (`--red`, `--fg`, `--bg`, `--card`, `--border`, …) and existing button/input/card classes. Don't introduce new color values, font sizes, or parallel components. **No Unicode emoji in UI or code** — inline monochrome SVG or plain text. Primary UI font is monospaced (`Fira Code`); dark theme is default; light-mode work goes through the existing theme system.

### PRs

Small, single-purpose; no broad rewrites or formatting-only changes. Open PRs against `dev`, not `main`. One kind of change per PR — don't mix file moves with assertion changes, helper extraction with logic changes, etc.

### Dependencies

`mcp<2` in `requirements.txt` is deliberate — mcp 2.0 drops the `Server.list_tools()` decorator API that `mcp_servers/*` use. Don't bump it without porting those servers.

## Deployment specifics of this fork

- Remote access runs through a Tailscale sidecar (`tailscale` service in compose): bare `tailscaled --tun=userspace-networking`, node `ithaka.tailb21d35.ts.net`, app reachable on port 7000 within the tailnet (HTTPS via `tailscale serve` → `https://ithaka.tailb21d35.ts.net`).
- `.env` uses `APP_BIND=0.0.0.0` (WSL NAT shields the LAN). API-token prefix `ody_` is intentionally kept from upstream for existing tokens.
- To deploy code changes live: rebuild container, then verify via `docker exec ithaka-ithaka-1` that new files exist, and confirm the Tailscale serve proxy returns HTTP 302/200.
- Outlook.com / Microsoft 365 accounts go through the bundled `davmail` service (internal-only, hostname `davmail`). Personal accounts (outlook.com/hotmail/live) need `DAVMAIL_TENANT=consumers` + Graph mode (`enableGraph=true`, `enableOidc=true`) and a one-time authorize-code bootstrap — device-code fails for personal MSA accounts. Full setup: `docs/email-outlook.md`.

## Gotchas & common pitfalls

- **Tailscale MTU blackhole** — packets > ~1200 B drop silently on Windows↔WSL sidecar. Fix baked into `docker-compose.yml` (`TS_DEBUG_MTU=1130`); mirror in GPU compose variants. Verify with `tailscale ping --size`.
- **Trailing-dot host entry** — Windows Chrome doesn't resolve `*.ts.net` (DoH); add a hosts entry via admin PowerShell: `Add-Content C:\Windows\System32\drivers\etc\hosts "100.72.181.25 ithaka.tailb21d35.ts.net."`.
- **HF_TOKEN for gated HF repos** — cookbook reads a stored token via `load_stored_hf_token()` (`src/tools/cookbook.py`), set in Cookbook → Settings or `.env`. Standalone scripts only read the env var and miss the stored token.
- **npx MCP server "Connection closed"** — usually a corrupt `_npx` cache entry in `/app/.npm` from a killed install; remove it and reinstall with `HOME=/app`.
- **Local venv is `.venv`** (repo docs say `venv` — that's stale; use `.venv/bin/python`).

## UI smoke test (desktop & mobile)

Before merging anything that touches a page, navigation, session state, or file paths: drive the app in a real browser at `http://localhost:7000` (or :7001 smoke instance) — desktop viewport and 360 px mobile. Check the flows the change touches plus the console for errors, and paste the full smoke output into the PR before merging.