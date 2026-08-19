# Sessielog 2026-08-19 — issues #12/#13 gemerged + Ollama-runtime-diagnose

## Geleverd

- **PR #19** (issue #12, squash-merged): `fix(models): detach sessions from deleted endpoint`.
  DELETE op een model-endpoint koppelt nu ook DB- en in-memory-sessies los (endpoint_url/model
  leeg, auth weg), tenzij een ander enabled endpoint dezelfde base-URL serveert. Smoke op :7001:
  endpoint toevoegen → chatten → verwijderen → verse chat toont schone no-model-flow i.p.v. de
  oude 400-loop ("Selected model endpoint was removed"). 151 tests groen; desktop + 360px.
- **PR #20** (issue #13, squash-merged): `fix(settings): mark admin-managed tabs`.
  Niet-admins zien Admin-badges op Add Models/AI Defaults/Search/Integrations/Reminders/Shortcuts;
  AI Defaults c.s. volledig disabled + banner; Integrations mark-only met vriendelijke
  "Only an admin can change this"-vertaling van 403 "Admin only". Admin-tegenproef zonder badges.
  6 tests + `node --check` groen; desktop + 360px.
- **Issue #17 gesloten** (was al gefixt door PR #18, volle suite 4856 passed).
- **CLAUDE.md opgeschoond** (463f8d2): test-taxonomie-paragraaf hersteld, nep-smoke-commando
  vervangen door echt chrome-devtools-MCP-recept, emoji + U+2011-hyphens eruit.
- Opruiming: 5 agent-worktrees + 11 stale branches verwijderd; zwerf-uvicorn :7004 gestopt.

## Ollama-runtime-incident (host, niet repo)

Ed meldde "lokale modellen onbereikbaar". Rootcause-keten:

1. Op 16 aug is een **snap-Ollama** geïnstalleerd naast de Docker-GPU-container `ollama`;
   de container verloor bij herstart de poort (Exited 128) en de snap nam 11434 over.
2. Snap bond alleen `127.0.0.1` → Docker-app kreeg "connection refused" via
   `host.docker.internal` terwijl host-curl werkte. Interim-fix: `sudo snap set ollama
   host=0.0.0.0:11434` — lokale modellen direct weer zichtbaar in Ithaka.
3. Snap-Ollama bleek bovendien **CPU-only** (`/api/ps` size_vram=0) — de RTX 5060 Ti stond
   sinds 17 aug ongebruikt. Besluit (Ed): terug naar de Docker-GPU-container.
4. Snap-store (54G, incl. sherlock-coder/devstral/deepseek-r1) gemerged naar het docker-volume
   `ollama` (nu 71G, 9 modellen). Let op: busybox `cp -an` en `tar -k` faalden **stil** —
   uiteindelijk per-bestand-loop met zichtbare fouten gebruikt.
5. Restpunt bij sessie-einde: `sudo snap remove ollama` (Ed) → `docker start ollama` →
   GPU-verificatie via `/api/ps` (size_vram > 0).

Diagnose-recept bij "modellen onbereikbaar vanuit container": `ss -tln | grep 11434`
(bind op `127.0.0.1` i.p.v. `*`?) en `docker ps -a | grep ollama`.

## Open

- #16 — CI draait nooit (GitHub Actions billing/spending-limit; alleen Ed kan dit fixen).
