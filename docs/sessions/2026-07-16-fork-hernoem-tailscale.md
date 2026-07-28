# Sessie 15–16 juli 2026 — fork, hernoeming naar Ithaka, remote toegang

## Wat er is gebeurd

1. **Fork losgekoppeld.** Kloon van `pewdiepie-archdaemon/odysseus` losgemaakt van upstream
   en als eigen privérepo verder: nu `EdF2021/ithaka`, default branch `dev` (ook `main`
   gepusht). Geen upstream-remote bewaard.
2. **Bugfix meegenomen** (`032101c`): `_explicit_web_intent` werd in `routes/chat_routes.py`
   op drie plekken gebruikt maar nergens gedefinieerd — gegarandeerde `NameError` op het
   web-search-pad van `/api/chat_stream`. End-to-end geverifieerd met een echte
   chat-round-trip via lokale Ollama (`qwen3.5:latest`).
3. **App hernoemd: Odysseus → Ithaka** (`6ce477b`, `c118ae8`): 2116 vermeldingen in 312
   bestanden, 34 bestandsnamen/mappen, env vars nu `ITHAKA_*`, Chroma-collecties `ithaka_*`.
   Drie verificatie-subagents (code, infra, docs); vangsten: `ODY_USER` in
   `docker/entrypoint.sh`, ~30 kapotte upstream-URLs, apostrof-artefact. README-screenshot
   vervangen door verse capture van de draaiende Ithaka-UI; upstream gecrediteerd in
   ACKNOWLEDGMENTS.md. Bewust behouden: API-token-prefix `ody_` (bestaande tokens).
4. **Alles hernoemd naar buiten toe:** GitHub-repo (`EdF2021/ithaka`), lokale map
   (`~/projects/ithaka`), compose-project → containers `ithaka-*`; named volumes
   gemigreerd van `odysseus_*` naar `ithaka_*` (oude volumes staan er nog als backup:
   `docker volume rm odysseus_chromadb-data odysseus_ntfy-cache odysseus_searxng-data`
   zodra alles een tijdje goed draait).
5. **Remote toegang via Tailscale** (`fc0995e`, `181384a`, `52d8aeb`): sidecar-service in
   compose, kale `tailscaled --tun=userspace-networking` op het host-netwerk. Node
   **ithaka** = `100.72.181.25` / `ithaka.tailb21d35.ts.net`; app bereikbaar op poort 7000
   via het tailnet. **End-to-end geverifieerd vanaf Ed's telefoon.**

## Belangrijkste valkuil (voor herhaling elders)

Het officiële `tailscale/tailscale`-image draait containerboot, dat zonder `TS_AUTHKEY` de
interactieve login elke paar minuten opnieuw start en daarbij de nodekey regenereert —
auth-URLs verlopen daardoor sneller dan een mens kan klikken; `TS_AUTH_ONCE=true` helpt
niet. Oplossing: entrypoint overriden naar kale `tailscaled` en eenmalig
`docker exec ithaka-tailscale-1 tailscale up --accept-dns=false` draaien; de URL blijft dan
geldig tot gebruik. State staat op het `tailscale-state`-volume, dus de identiteit
overleeft reboots.

## Stand van zaken / hoe verder

- Stack: `docker compose up -d --build` in `~/projects/ithaka`; app op `localhost:7000`,
  onderweg `http://ithaka.tailb21d35.ts.net:7000` (apparaat moet zelf in het tailnet
  zitten; Windows-host zit er níét in — daar gewoon localhost).
- `.env`: `APP_BIND=0.0.0.0` (WSL-NAT schermt het LAN af; geen Windows-portproxy gezet).
- Native smoke-instantie naast de stack: `ITHAKA_DATA_DIR=<verse map> .venv/bin/python -m
  uvicorn app:app --port 7001`; eerste account via POST `/api/auth/setup`.
- Open punten: geen. Optioneel ooit: oude `odysseus_*`-volumes opruimen, LAN-route
  (Windows-portproxy) als dat ooit nodig is.

Ed de Feber, in nauwe samenwerking met Claude
