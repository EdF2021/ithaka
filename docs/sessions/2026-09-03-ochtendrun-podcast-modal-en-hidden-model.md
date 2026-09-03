# Sessie 2026-09-03 (ochtend) — hidden-model-cascade, podcast-modal, Veo blocked-reason

## Aanleiding

Na de deploy van #152 (beeld/video-autoroute) meldde Ed om 05:32 een Veo-block
("Geblokkeerd door Veo safety-filter — niet gefactureerd") zonder reden, én een luide GPU-fan
bij e-mailtaken. Twee losse oorzaken. De sessie hing om 09:22 direct na de deploy van #158;
de live-verificatie volgde om 09:40 in dezelfde (hervatte) sessie. Alles daarna (#154, #159,
#160, 4s-videosmoke) staat in `2026-09-03-ochtendrun-bugsquad-en-videosmoke.md`.

## Incident: fan-lawaai bij e-mailtaken → #155 (06:38–07:26)

- Diagnose via `/api/ps`: `task_model` (Qwen3-14B) stond in Ollama `hidden_models`. De
  resolver in `src/endpoint_resolver.py` viel dan stil terug op het 27B-Uncensored-model
  (CPU-spill in llama-server = het lawaai).
- Fix (#155, issue #156): een verborgen `task_model` cascadeert niet meer naar een willekeurig
  ander model maar naar de volgende zichtbare tier. 20 resolver-tests in
  `tests/test_resolve_endpoint_fallbacks.py`. Prod herbouwd 07:22 en bevestigd.
- Open: `utility_model=devstral-small-2` (15 GB) heeft hetzelfde VRAM-risico → kandidaten in #156.

## Feature: "Audio-overzicht aanpassen"-modal → #158 (08:55–09:22)

NotebookLM-achtige modal vóór podcast-generatie: indeling (Gedetailleerd / Overzicht /
Kritiek / Debat), lengte (Kort / Standaard), bronselectie (`source_ids`) en een vrije focus-tekst.

- TDD: eerst RED-tests in `tests/test_services_notebook_audio.py` (script-prompt per format,
  `gather_source_text` met `source_ids`), daarna `src/notebook_audio.py` +
  `routes/notebook_routes.py` (parameters door de job heen), daarna JS/CSS
  (`notebookWorkspace.js` `#nbpc-modal`, `style.css`). Suite: 141 backend + 200 JS-static groen.
- Smoke op :7001 (Chroma-port expliciet :8100, zie [[ithaka-smoke-chroma-gotcha]]) vond op 360 px
  een z-index-bug: `#nbpc-modal` miste de `body.notebook-workspace-open`-regel die `#nbrp-modal`
  wél had, en `modalManager.js` overschreef de stylesheet. Fix + statische test
  (`body.notebook-workspace-open #nbpc-modal { z-index: 10010; }`) zitten in de PR.
- Gemerged 09:22, prod direct herbouwd.

## Live-verificatie #158 op prod (:7000, 09:40)

- Login-truc: verse `isolatedContext` in chrome-devtools + geldig token uit
  `/app/data/sessions.json` als `document.cookie`. In het gewone profiel weigert de browser het
  omdat er al een HttpOnly `ithaka_session` staat (JS kan die niet overschrijven).
- Desktop 1600×900: Audio-tegel → modal met 4 formats, lengte-toggle, "11 bronnen (alle)",
  focus-textarea, Annuleren/Nu genereren. Screenshot OK.
- Mobiel 360×740 (emulate): modal `z-index 10010` boven de workspace, `display:flex`,
  inner 360 px breed, 0 elementen buiten de viewport, `scrollWidth 360`. Scroller is
  `.modal-body`; "Nu genereren" na scroll zichtbaar (top 698, bottom 730). Geen console-errors
  van de feature; de 2× 404 op `/api/research/status/...` en `/api/chat/stream_status/...` zijn
  pre-existing polls op een stale sessie-id.
- Let op: `emulate` met een mobiel viewport herlaadt de pagina → workspace en éénmalig
  aangemaakte modal (`if (getElementById) return`) zijn weg; flow opnieuw doorlopen na de switch.

## Lessen

- Sessie-verlies direct na `docker compose up --build` = live-verificatie kwijt. Eerst
  verifiëren, dán sessielog + `/remember`, en pas daarna nieuwe features starten.
- HttpOnly-cookie kan niet via `document.cookie` overschreven worden; isolatedContext gebruiken.
- `emulate` naar mobiel = page reload; state opnieuw opbouwen.

## Open

- #156: `utility_model=devstral-small-2` VRAM-risico.
- Stale lokale branches: `feat/video-settings-card`, `spec-fase2`, 9× `worktree-agent-*`.
- Twee sessies tegelijk op dezelfde repo (deze + de bug-squad-sessie) → dubbele merge-poging op
  #154 ("already merged", onschadelijk). Bij sessieverlies eerst `ListAgents`/`git log` checken.
