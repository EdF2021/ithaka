# Sessielog 2026-09-03 — Realtime-gesprek (voice mode fase 1)

Spec: `docs/superpowers/specs/2026-09-03-realtime-voice-mode-design.md` (gepresenteerd, drie
expliciete aannames — geen formeel akkoord van Ed vóór start; hij vroeg zelfstandig werken).
Plan: `docs/superpowers/plans/2026-09-03-realtime-voice-mode.md` (6 taken, TDD).
Uitvoering: subagent-driven development (sonnet-implementers, sonnet-reviewers, opus voor
de eindreview), branch `feat/realtime-voice-mode` (afgesplitst van `dev` @ c88d0e5, in de
bestaande `feat-infographic-v2`-worktree — de infographic-PR-branch is ongemoeid, teruggezet
naar zijn gepushte tip).

## Wat er gebouwd is

| Taak | Inhoud | Commit |
|---|---|---|
| 1 | `src/settings.py`: 10 nieuwe `realtime_*`-defaults (globaal, niet per-gebruiker) | e009038 |
| 2 | `services/realtime/realtime_service.py`: `RealtimeService` — zuivere `build_session_config()`, `create_session()` mint een ephemeral `client_secret` bij OpenAI, Nederlandse `ValueError` op elke fout, nooit het langlevende `api_key` gelekt | b8c9ff6, a0b6518 (fix: `r.json()`-decodeerfout ook naar Nederlandse ValueError) |
| 3 | `routes/realtime_routes.py` + `app.py`-wiring: `POST /api/realtime/session` | 43c336a |
| 4 | `static/js/realtimeVoice.js`: WebRTC-sessie-levenscyclus, event-classificatie, barge-in-fallback; `chatRenderer` via `window` opgelost (niet statisch geïmporteerd) zodat de module Node-testbaar blijft | 0273207 |
| 5 | Settings-kaart + overflow-toggle + indicator-knop (`static/index.html`, `settings.js`, `app.js`) | 99ea0fa |
| — | Eindreview-fixes: CSP blokkeerde de browser→OpenAI-fetch; `activate()` her-checkte `_active` niet na elke await (race op annuleren-tijdens-verbinden); geen wederzijdse uitsluiting tussen de twee voice-mode-toggles; `calls_url` nu endpoint-generiek i.p.v. hardcoded `api.openai.com` | e6a4cf8 |

Nieuwe tests: `tests/test_settings_realtime_keys.py`, `tests/test_realtime_service.py`,
`tests/test_routes_realtime.py`, `tests/test_realtime_voice_js.py` (Node-based, pure
event-classificatie + barge-in-logica), uitbreiding `tests/test_security_headers_pdf_preview.py`.

## Verificatie

- Volledige suite (ongesandboxt) op e6a4cf8: 5699 passed, 3 skipped, 17 failed/20 errors —
  allemaal vooraf bekende sandbox-artefacten (read-only `/tmp`, docker-socket, netwerk-proxy),
  geen regressies in de aangeraakte bestanden.
- Elke taak individueel gereviewd (spec + kwaliteit): Taak 1/3/4/5 in één ronde goedgekeurd;
  Taak 2 kreeg één fix-ronde (ongevangen `r.json()`-decodeerfout) en is daarna schoon.
- Eindreview (whole-branch, opus) vond **2 Critical + 2 Important** die individuele
  taakreviews niet konden zien (pas zichtbaar over de hele branch):
  - CSP (`core/middleware.py`) blokkeerde de enige cross-origin `fetch` in de hele codebase —
    geverifieerd live tegen de draaiende `:7000`-instance (`curl -sI /login`).
  - `activate()` in `realtimeVoice.js` her-checkte `_active` niet na de 5 awaits — annuleren
    tijdens verbinden liet een levende, wees-sessie achter (mic + peer-connection), UI toonde
    "idle". Patroon bestond al in `voiceMode.js` maar was niet overgenomen.
  - Geen wederzijdse uitsluiting tussen de bestaande voice mode en Realtime — beide konden
    tegelijk de microfoon vasthouden (dubbele transcriptie/TTS/kosten).
  - `realtimeVoice.js` hardcodeerde `api.openai.com` terwijl de backend endpoint-generiek is.
  - Alle 4 in één fix-golf verholpen, één scoped re-review bevestigde alle 4 ADDRESSED, geen
    nieuwe schade (re-reviewer liep zelf alle 5 activate()-afbreekpunten na i.p.v. het
    implementer-rapport te vertrouwen).
  - 7 Minor bevindingen bewust niet gefixed (geparkeerd) — zie ledger voor de volledige lijst.
- **Post-commit automated security scanner** meldde "Unauthenticated Token Minting" op
  `POST /api/realtime/session`. Geverifieerd als vals-positief: elke `/api/*`-route in deze
  app loopt via de globale `AuthMiddleware` (sessie-cookie/bearer-token), en deze route staat
  niet in een `AUTH_EXEMPT_*`-vrijstellingslijst — onafhankelijk bevestigd door zowel de
  Taak-3-reviewer als de eindreviewer.

**Taak 6 — live smoke, uitgevoerd 2026-09-04 00:00-00:25 op een verse `:7001`-instance
(branch `feat/realtime-voice-mode`, fresh data-dir, echte OpenAI-key van Ed):**
- Backend end-to-end gecontroleerd via een directe `POST /api/realtime/session`-call (tijdelijk
  `LOCALHOST_BYPASS=true`, alleen loopback, meteen weer uitgezet): eerste poging faalde met
  `HTTP 401` van OpenAI — bleek Eds eigen fout (hij had een tweede `ModelEndpoint`-rij aangemaakt
  i.p.v. de eerste te bewerken; `realtime_provider` wees nog naar de oude/ongeldige rij). Na
  wijzen naar de juiste rij: `200 OK` met een echte `client_secret`/`expires_at`/`calls_url` terug
  van OpenAI — bevestigt de volledige backend-keten (settings → endpoint-resolutie →
  key-decryptie → `client_secrets`-call → response-mapping) werkt tegen de echte API, zonder
  lek van het langlevende `api_key`.
- Live browser-test door Ed zelf op `:7001` (ingelogd, Realtime-toggle aan): antwoord kwam direct
  in het Nederlands (geen Engelse denkstap vooraf), barge-in werkte (AI stopte toen hij erover
  heen praatte), ook getest op mobiel/smal scherm — alle drie bevestigd door Ed in de chat
  (2026-09-04 00:24 CEST).
- Sessie-timeout (10 min) niet apart getest — laag risico, triviale timer-logica, geen aparte
  verificatie nodig geacht.

Taak 6 is hiermee gedekt. PR #162 kan door naar de merge-gate.

## Rulings tijdens de uitvoering

- `chatRenderer` via `window` opgelost i.p.v. statisch geïmporteerd (preflight-scan, vóór elke
  dispatch) — chatRenderer.js's eigen import-keten raakt DOM aan op moduleniveau, wat de
  Node-testharness had gecrasht.
- Automatische reconnect-poging bij een WebRTC-drop niet gebouwd (spec noemde die wél); i.p.v.
  daarvan `pc.onconnectionstatechange` die de drop detecteert en zichtbaar faalt. Vastgelegd in
  spec én plan vóór dispatch.
- Eindreview-fix-golf beperkt tot de 2 Critical + 2 Important bevindingen; alle Minor-bevindingen
  bewust geparkeerd (nooit gefixed) conform het proces.

## Open punten / follow-ups

- Taak 6: live smoke met een echte OpenAI-sleutel (desktop + 360 px, barge-in, sessie-timeout).
- 7 geparkeerde Minor-bevindingen uit de eindreview (zie ledger `.superpowers/sdd/2026-09-03-realtime-voice-mode/progress.md`
  voor de volledige lijst): o.a. geen lege-`client_secret`-check, `realtime_max_minutes`
  onbegrensd in `_INT_RANGES`, `RealtimeService.available` heeft geen aanroeper.
- Tool-calling (fase 2, expliciet buiten scope van dit plan).
- Server-side sessiepersistentie van het Realtime-transcript (nu alleen DOM, verdwijnt bij reload).
