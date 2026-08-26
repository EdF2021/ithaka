# Sessie 2026-08-18: knop-gedreven onboarding + Help-paneel (issue #11)

## Aanleiding
Issue #11 (UX-review hoog #1): onboarding was command-line-gedreven, geen
Help-ingang. Autonoom uitgevoerd met subagents (sonnet voor implementatie/fixes).

## Gedaan
- **PR #15** (squash → dev, `d60cb31`): first-run-knoppen op het welcome-screen
  (Connect a model / Take the tour / Help), Help-paneel (`static/js/helpPanel.js`:
  11 tours, commando-chips die alleen invullen, Connect-sectie), permanente
  Help-knop in de sidebar-user-bar. Zichtbaarheidslogica in `welcomeActions.js`
  met gedeeld `hasUsableModel`-predicaat (spiegelt `_syncWelcomeModelHint`).
- **Review-ronde** (code-review low, 10 bevindingen, alle verwerkt): stream-guard
  op synthetic submit (`hasActiveStream(sessionId)` — bare call is altijd false!),
  dead-end tour-knop opgelost (tour toont alleen mét bruikbaar model), phantom-taps
  gedicht (pointer-events-companions voor alle opacity-0-states van
  #welcome-screen), chips sluiten het paneel, mobiele sheet scrollt
  (`.help-panel-body { overflow-y:auto }`), TOURS-drift-guard-test, responsive
  Tours-sectie via CSS-media-query i.p.v. JS.
- **Deploy + live-verificatie** op :7000 (rebuild), desktop 1600px + mobiel 360px,
  via tijdelijk testaccount (na afloop verwijderd). Non-admin met model:
  alleen Tour + Help; Model-sectie in paneel verborgen — klopt.
- **Issue #16**: ontdekt dat CI nooit gedraaid heeft (nul Actions-runs sinds
  repo-creatie; geen fork, triggers goed; vermoedelijk billing — Ed moet ingelogd
  Actions-tab/billing checken). Lokale volledige suite als vangnet gebruikt.
- **Issue #17 → PR #18** (gemerged, `190f9bc`): 5 pre-existing failures op dev
  gefixt — GPU-compose-varianten misten de MTU-fix uit `docker-compose.yml`
  (gespiegeld), en een 401-copy-assertie liep achter op PR #14 (bijgewerkt).
  Volledige suite daarna 4856 passed — dev weer helemaal groen.

## Verificatie
15 feature-tests (TDD), 203 js-area, volledige suite 4845 passed (5 = #17).
Browser-smoke op verse instance :7001 én live op :7000, beide viewports,
screenshots in de chat. Geen console-errors.

## Open / lessen
- Issues #12, #13, #16 open; roadmap-items in het UX-rapport.
- Les: `chatModule.hasActiveStream` vereist expliciet sessie-id.
- Les: extern gemuteerd auth.json (docker exec) vergt container-restart;
  tijdelijk account netjes opgeruimd met `delete_user`.
- Pre-existing (niet aangeraakt): welcome-enter-animatie pint opacity op 1
  waardoor opacity-0-states alleen pointer-events effectief togglen.
- Account `aransafon` bestaat al — vriendin-toegang (oude handoff) is geregeld.
