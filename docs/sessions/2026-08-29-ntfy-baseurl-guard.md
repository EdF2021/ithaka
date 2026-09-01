# 2026-08-29 — ntfy base_url-guard (issue #85, PR #86)

## Aanleiding

Op 2026-08-28 brak de reminder-delivery twee keer via de Integrations-UI, zonder
waarschuwing bij het opslaan:

1. base_url → `https://ithaka.tailb21d35.ts.net:8443` (tailnet-serve-URL). Die is
   voor telefoon-abonnementen; de app-container heeft geen MagicDNS en geen route
   naar 100.x (`curl: (6) Could not resolve host`).
2. base_url → `https://ntfy.sh/ithaka` (topic in de base-URL geplakt). De test-knop
   stript het pad (misleidend groen), maar `dispatch_reminder()` niet → zou naar
   `/ithaka/ithaka` posten (404).

Beide saves kwamen uit de browser-UI (PUT `/api/auth/integrations/ee36ad6fb9ce`),
niet van de in-app agent. Handmatig hersteld naar `http://ntfy` + testbericht
(id `remlNCq41RCC`) bevestigd.

## Fix (TDD, 9 nieuwe tests)

- `src/integrations.py`: `validate_ntfy_base_url()` — normaliseert en weigert een
  pad in de base_url voor preset ntfy (topic hoort in Settings → Reminders);
  toegepast in `add_integration`/`update_integration`. Async
  `check_ntfy_reachable()` — probe `GET {base}/v1/health` (4s timeout).
- `routes/auth_routes.py`: create/update weigeren een ntfy-save met 400 wanneer de
  genormaliseerde URL een pad heeft of de probe faalt (foutmelding verwijst naar
  `http://ntfy` voor de gebundelde container).
- `static/js/settings.js`: het unified formulier slikte elke save-fout in tot kaal
  "Failed"; toont nu de server-detail (zoals het legacy-formulier al deed).
- Tests: `tests/test_integrations_ntfy_guard.py` (store-laag, probe-helper tegen
  echte sockets, route-wiring met gemockte probe). Niet-ntfy-presets houden
  pad-support (Discord-webhook-URL bevat legitiem een pad).

## Verificatie

- Suite: 9 nieuwe + 43 buur- + 471 routes-fast groen; CI vol pytest groen.
- UI-smoke op code-identieke :7001-instance (verse data-dir, `CHROMADB_PORT` naar
  dode poort — les 2026-08-28): desktop + 360px mobiel; pad-fout en
  unreachable-fout zichtbaar in rood, geldige URL "Saved", geweigerde URLs niet
  gepersisteerd, console schoon.
- Prod herbouwd op dev-HEAD; guard in container bevestigd (pad-check actief,
  probe `http://ntfy` → bereikbaar), `integrations.json` staat op `http://ntfy`.

## Twee URLs, twee rollen (operationeel)

- App → ntfy (integratie-base_url): `http://ntfy` (intern compose-DNS).
- Telefoon → ntfy (abonnement): `https://ithaka.tailb21d35.ts.net:8443`
  (tailscale serve → 127.0.0.1:8091). Topic: `ithaka`.

Ed de Feber, in nauwe samenwerking met Claude
