---
name: ithaka-ops
description: Ithaka deploy/ops gotchas — Tailscale sidecar (MTU blackhole, containerboot auth-race, hosts entry), DavMail/Outlook bootstrap, Google CalDAV legacy endpoint, HF_TOKEN for gated repos, npx MCP cache. Use when touching docker-compose, the Tailscale/davmail services, email/calendar accounts, or debugging remote access.
---

# Ithaka ops gotchas & deployment specifics

Moved out of the root `CLAUDE.md` (2026-09-04) so it only loads when relevant.

## Gotchas & common pitfalls

- **Tailscale MTU blackhole** — packets > ~1200 B drop silently on the Windows↔WSL sidecar (health checks pass, pages/TLS hang). Fix is baked into `docker-compose.yml` (`TS_DEBUG_MTU=1130`); mirror it in the GPU compose variants. Verify with `tailscale ping --size`.
- **Trailing-dot host entry** — Windows Chrome doesn't resolve `*.ts.net` (DoH); add a hosts entry via admin PowerShell:
  ```powershell
  Add-Content C:\Windows\System32\drivers\etc\hosts "100.72.181.25 ithaka.tailb21d35.ts.net."
  ```
- **HF_TOKEN for gated HF repos** — the cookbook reads a stored token via `load_stored_hf_token()` (`src/tools/cookbook.py`), set in Cookbook → Settings or `.env`. Standalone scripts (`diffusion_server.py`, `add_hwfit_models.py`, …) only read the env var and miss the stored token.
- **npx MCP server "Connection closed"** — usually a corrupt `_npx` cache entry (directory without `bin/`) in `/app/.npm` from a killed install; remove it and reinstall with `HOME=/app`. Since #70 the npm cache persists in a volume and stdio MCP sessions run in dedicated tasks, so this should stay rare.
- **`.claude.local.md`** — personal overrides (keybindings, local shortcuts). Git-ignored, merged at runtime.

## Deployment specifics of this fork

- Remote access runs through a Tailscale sidecar (`tailscale` service in compose): bare `tailscaled --tun=userspace-networking`, node `ithaka.tailb21d35.ts.net`, app reachable on port 7000 within the tailnet (HTTPS via `tailscale serve` → https://ithaka.tailb21d35.ts.net). Do **not** switch it back to containerboot — without `TS_AUTHKEY` containerboot regenerates the nodekey in a loop and auth URLs expire instantly; the fix is bare `tailscaled` + a one-time `docker exec ithaka-tailscale-1 tailscale up --accept-dns=false`.
- `.env` uses `APP_BIND=0.0.0.0` (WSL NAT shields the LAN). The API-token prefix `ody_` is intentionally kept from upstream for existing tokens.
- Google Calendar as CalDAV account: use the **legacy** endpoint `https://www.google.com/calendar/dav/<email>/user/` with a Google app password — the modern `apidata.googleusercontent.com/caldav/v2/...` endpoint is OAuth-only and returns Unauthorized for app passwords (verified live 2026-08-31).
- Outlook.com / Microsoft 365 accounts go through the bundled `davmail` service (internal-only, hostname `davmail`) — Microsoft basic auth for IMAP/SMTP is dead, DavMail is the OAuth bridge. Personal accounts (outlook.com/hotmail/live) need `DAVMAIL_TENANT=consumers` + Graph mode and a one-time authorize-code bootstrap — device-code (`DAVMAIL_AUTHENTICATION=O365DeviceCode`) fails for them with a false "expired" and is only for org accounts. The refresh token then persists in the `davmail-config` volume. Full setup and troubleshooting: `docs/email-outlook.md`.
- Deploying without touching the main checkout's git (e.g. from a worktree at `origin/dev`): `docker compose -p ithaka --env-file /home/eddef/projects/ithaka/.env -f docker-compose.yml -f docker/gpu.nvidia.yml build ithaka` in the worktree, then `docker compose -p ithaka --project-directory /home/eddef/projects/ithaka -f <worktree>/docker-compose.yml -f <worktree>/docker/gpu.nvidia.yml up -d --no-build ithaka` — the run config (bind mounts `./data`, `./logs`) must stay the main checkout's. **Always pass the GPU overlay explicitly:** any `-f` makes compose ignore `COMPOSE_FILE` from `.env`, so a bare `-f docker-compose.yml` silently builds without `GPU_EXTRAS=1` (CPU onnxruntime) and starts the container without the NVIDIA device request — this regressed prod several times (last: 2026-09-05). After every deploy verify all three: `docker inspect ithaka-ithaka-1 --format '{{json .HostConfig.DeviceRequests}}'` is not `null`, `docker exec ithaka-ithaka-1 pip list | grep onnxruntime` shows `onnxruntime-gpu`, and `docker exec ithaka-ithaka-1 nvidia-smi -L` lists the GPU.
