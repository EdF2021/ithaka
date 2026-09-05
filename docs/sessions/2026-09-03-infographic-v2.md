# Sessielog 2026-09-03 — Notebook-infographic v2 (hybride HTML-layout + AI-illustraties)

Spec: `docs/superpowers/specs/2026-09-03-notebooks-infographic-v2-design.md` (goedgekeurd door Ed 13:43).
Plan: `docs/superpowers/plans/2026-09-03-notebooks-infographic-v2.md` (8 taken, TDD).
Uitvoering: subagent-driven development (sonnet-implementers, sonnet/opus-reviewers, regie in de hoofdsessie), worktree `feat-infographic-v2`, branch `worktree-feat-infographic-v2`.

## Wat er gebouwd is

| Taak | Inhoud | Commit |
|---|---|---|
| 1 | JSON-schema-extractor `extract_infographic`, `is_infographic_v2`, `iter_blocks` (Nederlandse validatiefouten, tekst-in-beeld-filter voor `illustration_prompt`) | 0861760 |
| 2 | Nieuwe infographic-prompt (JSON-schema), validator-registratie, geldige icon-sleutel in het voorbeeld | dfc1bb7, 76283ef |
| 3 | v2-renderer `render_infographic_v2` + `_TEMPLATE_V2` (grid, steps, comparison-balken, stat-grid, hero, takeaway), inline poll-script (3 s / 120 s, retry op non-2xx), dispatch in `generate_infographic(..., poll_url=)`; legacy markdown blijft via de oude parser renderen | 15b01c3, 4b6afd8 |
| 4 | `src/notebook_illustrations.py`: async job naar covers-patroon, max 5 beelden, kwaliteit `low`, hero 1536x1024, incrementele persistentie in het artifact-JSON, per-blok-fouten, owner-scoped registry | 9b389aa, 9118c86 |
| 5 | Bestandsnaam-whitelist `<artifact_id>-<block_id>-<hex8>.png`, `resolve_illustration_path`, janitor `cleanup_orphaned_illustrations` | 2172cc9 |
| 6 | Routes: job-start in `POST …/artifacts` (alleen bij `image_gen_enabled`), `GET …/artifacts/{id}/illustrations`, `GET /api/notebook-illustration/{fn}`, `poll_url` in de report-route; `_PASSIVE_PATTERNS`; uurlijkse janitor in `app.py` | 6f00f6e |
| 7 | Tegel-tooltip, CLAUDE.md-paragraaf, gap-analyse-header | 4f2aa5a |
| 8a | Validator degradeert een derde `column` naar losse blokken i.p.v. afwijzen; prompt: "Maximaal TWEE column-blokken" | d35ac15 |
| 8b | Desktop-breakpoint 960 → 880 px zodat de studio-paneel-iframe (≈926 px) het 3-koloms grid toont | 52aec1e |
| 8c | Eindreview-fixes: illustratie-job schrijft het rúwe JSON terug (alleen `illustrations` toegevoegd; degradatie blijft render-tijd, anders >8 blokken na persist → foutkaart); whitespace/newlines in `illustration_prompt` platgeslagen (regel-injectie in `do_generate_image`-content) | 33c32ea |

Nieuwe constante `NOTEBOOK_INFOGRAPHICS_DIR` in `src/constants.py`. Nieuwe tests: `tests/test_notebook_infographic_v2.py`, `tests/test_notebook_illustrations.py`, `tests/test_routes_notebook_infographic.py`, uitbreiding `tests/test_interactive_gate_passive.py`.

## Verificatie

- Full suite zonder sandbox op 4f2aa5a: 5801 passed; op 33c32ea (eind): **5804 passed, 3 skipped, 0 failed**.
- Eindreview (whole-branch, sonnet; opus 2× overbelast) vond de twee punten van taak 8c; scoped re-review: beide verholpen, geen nieuwe schade. (Gesandboxte runs geven een vaste set omgevingsfouten: EROFS `/tmp`, netwerk-proxy, docker-socket — geen regressies.)
- Smoke op :7001 (verse data-dir, lokale Ollama Qwen3-14B als LLM, `image_gen_enabled=false`):
  - Generatie slaagde op poging 3 (poging 1: te weinig blokken; poging 2: tekst-in-beeld-prompt geweigerd) — de retry-seam doet zijn werk. Vóór fix 8a faalde het lokale model 3× op "3 column-blokken".
  - In-paneel-viewer (iframe ≈926 px): v2 rendert (2 columns, hero, 2 steps-lijsten, 3 vergelijkingsrijen, 6 kerncijfers, takeaway), geen `data-illustrations`, 9 iconen / 0 `<img>`, geen `<script src>`, geen horizontale overflow; sinds 8b het 3-koloms grid (kolommen 251/313/251 px).
  - Standalone report op 1280 px: 3-koloms grid; op 360 px: 9 blokken × 331 px in één kolom, `scrollWidth == innerWidth`.
  - Status-endpoint → `{"status":"none","illustrations":{}}`; serving-route: foute naam 400, ontbrekend bestand 404, onbekend artifact 404.
  - Console: alleen de bekende stale-sessie-polls (`/api/research/status`, `/api/chat/stream_status`) en de favicon-404 van de standalone pagina.
- Screenshots: `.superpowers/sdd/2026-09-03-notebooks-infographic-v2/smoke-*.png` (lokaal, git-ignored).

**Nog niet geverifieerd:** de illustratie-job end-to-end tegen een echte beeld-endpoint. De auto-mode-classifier weigerde het OpenAI-key uit de prod-DB naar :7001 te brengen; fase 2 van de smoke (illustraties binnenkomend via de poll, desktop + 360 px) moet met een door Ed ingevoerde key op :7001 of live op prod na de deploy.

## Rulings tijdens de uitvoering

- HTML-route blijft het bestaande `…/artifacts/{id}/report` (spec noemde `/html`).
- `is_infographic_v2` zoekt de JSON-fence ongeankerd (het model zet soms een zin vóór de fence).
- Extra lengte-caps (takeaway 240, subheading 120, comparison label/value 60/80, steps label 60, key_numbers label 80) blijven als beschermende guards.
- Losse `steps`/`icon_card`/`key_numbers` op topniveau zijn toegestaan (spec Deel C).
- Derde `column` wordt gedegradeerd i.p.v. afgewezen; blokken-telling (5–8) vóór de degradatie.
- Desktop-breakpoint 880 px i.p.v. 960 px (spec-intentie: 3 kolommen op desktop, ook in het paneel).
- Verificatietaak (Task 8) door de regisseur zelf uitgevoerd (server + browser buiten sandbox nodig).

## Open punten / follow-ups

- Modellen zetten soms emoji in `comparison.value` ("✅ Al aanwezig") ondanks de no-emoji-regel → overweeg strippen in de validator.
- Zwakke modellen vullen `key_numbers.number` met proza; `MAX_ILLUSTRATIONS` wordt bewust niet in de validator afgedwongen (job kapt af).
- Janitor keyt op artifact-bestaan, niet op bestandsnaam-lidmaatschap (regeneratie van losse illustraties is buiten scope).
- Time-out-pad van de job en de "geen geldige v2"-tak hebben geen tests.
- `session.query().get()` in de nieuwe tests geeft een SQLAlchemy-deprecatiewaarschuwing (patroon uit bestaande tests).

## Zijspoor: fan-lawaai 14:28

Chatsessie op devstral-small-2 (15,5 GB, 38/41 lagen op GPU) → llama-server 39% CPU. `ollama stop` gedaan; `utility_model` is inmiddels kimi-k3. Advies (#156): devstral uit de picker (`hidden_models`) of `ollama rm`. Memory bijgewerkt.

## Volgende doel

Realtime-gesprek (voice mode) herzien op basis van de OpenAI Realtime API-config van Ed (gpt-realtime-2.1-mini, gpt-realtime-whisper, ash, server_vad 0.5/300/500, far_field, pcm 24 kHz). Read-only diagnose van de huidige voice-mode staat in de scratchpad (`voice-mode-diagnosis.md`): cascade-latency ~10,5 s/beurt, geen barge-in, seriële zin-voor-zin-TTS; integratiepunten: `voiceMode.js`-state-machine hergebruiken, `voiceRecorder.js` + STT/TTS-cascade vervangen door WebRTC + server-VAD, nieuwe `routes/realtime_routes.py` met ephemeral token via een `ModelEndpoint`-rij, `realtime_*`-settings.
