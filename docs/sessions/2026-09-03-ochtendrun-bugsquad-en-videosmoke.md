# Sessie 2026-09-03 (ochtend) — bug-squad #153/#145, #154 gemerged, 4s-video-smoke op prod

## Aanleiding

Na sessieverlies (#158 podcast-modal was al live-gecheckt om 09:40) stonden vier punten open:
PR #154 (Veo blocked-reason), issue #153 (Image Generation-dropdown), issue #145 (fenced
code-block in notebook-chat) en de 4-seconden-videotest op prod. `/goal`: alle vier, zelfstandig,
subagents op sonnet. Aanpak: bug-squad-skill — twee sonnet-agents in eigen worktrees (#153 via
playwright op :7002, #145 via chrome-devtools op :7003 + wegwerp-Chroma :8101), #154 en de
videosmoke centraal.

## PRs (alle drie squash-gemerged in `dev`)

- **#154** (e29abb5) — `poll_operation` in `src/video_gen.py` geeft `blocked_reason` terug uit
  `raiMediaFilteredReasons`; `_generate` hangt hem tussen haakjes achter "Geblokkeerd door Veo
  safety-filter — niet gefactureerd". 50 tests groen; geen UI-surface (string landt in bestaande
  foutbubbel), smoke N/A.
- **#159** (a4d791c, issue #153) — `_buildImageModelOptions(allModelIds, currentValue)` in
  `static/js/settings.js`: naast inpaint-modellen ook `gpt-image-*` / `dall-e-*` uit de
  model-cache, en de opgeslagen waarde altijd als optie (ook als hij niet in de cache zit) zodat
  de select nooit terugvalt op "Auto-detect". `initImageSettings` laadt settings vóór models.
  `image_model` is één setting voor inpaint én `do_generate_image` (geen aparte key) — kaart niet
  gesplitst. 7 nieuwe Node-gedreven tests (`tests/test_image_settings_model_options_js.py`).
  Smoke :7002 desktop + 360px: gpt-image-1.5 geselecteerd, opslaan persisteert, geen overflow.
- **#160** (c4c8244, issue #145) — twee losse oorzaken:
  (a) `static/js/markdown.js`: `isNotebookChatSession()` (leest `body.notebook-session`, hetzelfde
  signaal als de RAG-toggle-hide in `sessions.js`) → Run- én Edit-knop weg in notebook-sessies,
  Copy blijft.
  (b) De "1 teken per regel"-squeeze was géén CSS-ancestor-probleem (gemeten 302–360px, `.chat-
  container` had al `min-width:0`) maar de `.pre-compact`-classifier in `static/js/chat.js`: de
  `MutationObserver` keek alleen naar `childList`, terwijl `streamingRenderer.js` een open fence
  via `Text.appendData()` laat groeien. Een `<pre>` dat bij de eerste korte regel `.pre-compact`
  kreeg (`padding-right: 200px`) bleef dat na het groeien → ~100px bruikbare breedte op mobiel.
  Observer luistert nu ook naar `characterData`. Reproduceert ook in gewone chat.
  6 nieuwe tests; volledige suite 5704 passed. Smoke :7003 desktop + 360px vóór/na, gewone chat
  zonder regressie. De CI-check "Check PR description" faalde alleen op een niet-aangevinkt
  duplicate-search-vakje (cosmetisch, na merge).

Twelve-rules-review op #159 en #160: geen Fix-items. Eén Recommend voor later: `_markCompactPre`
draait nu per gestreamd token over het hele `<pre>` (O(n²) bij grote blokken); early-return zodra
het blok al niet-compact is als streaming ooit traag wordt.

## Prod

Container gerebuild (`docker compose up -d --build ithaka`) op c4c8244, daarna herstart om de
smoke-sessietoken van schijf te laden.

## 4s-video-smoke op prod (:7000, playwright, admin)

Prompt: "Maak een video van 4 seconden van een zeilboot die bij zonsondergang over een kalme zee
vaart." Chatmodel gpt-5.1-chat-latest → `generate_video` in ronde 1 (media-route), Veo
`veo-3.1-lite-generate-preview` via `predictLongRunning` 200, klaar binnen ~1 min.

- mp4: `/app/data/videos/e008e9….mp4`, ffprobe `duration=4.000000`, 1280×720, 1.96 MB.
- Desktop 1280×900: video inline in de chat met controls (0:00/0:04), bijschrift met model +
  prompt eronder, sessie auto-genoemd "Zeilboot bij Zonsondergang", kostenlabel $0.084.
- Mobiel 360×740: video 291px breed binnen de bubbel, `scrollWidth === clientWidth === 360`,
  controls zichtbaar.
- Console: alleen de twee pre-existing 404-polls (`/api/research/status`, `/api/chat/
  stream_status`) van de stale sessie; niets nieuws.

## Lessen

- **Prod-smoke-login**: `create_session_trusted()` in een `docker exec`-proces schrijft naar
  `sessions.json` maar de draaiende server heeft zijn eigen in-memory kopie → `docker compose
  restart ithaka` nodig. `/api/auth/me` bestaat niet (404) en `/api/auth/settings` is publiek
  (200 zonder cookie) — verifieer een token met `/api/sessions`.
- Het chrome-devtools-profiel weigert `document.cookie`-writes én stuurt een via `emulate`
  geïnjecteerde `Cookie`-header niet mee; playwright met `context().addCookies()` werkt wel.
- Op de startpagina staan twee `#message`-textareas (hero + composer): `#message:visible`.
  De header-knop "New Chat" opent de dashboard-modal, die klikken onderschept — Escape eerst.
- Sessie-timeline: agents ~13 en ~35 min; samen 5 merges, 1 rebuild.

## Open

- #81 release-watch; `NOTEBOOK_RAG_SIMILARITY_THRESHOLD=0.15` wacht op hybrid-scores.
- `utility_model=devstral-small-2` (15 GB) blijft een VRAM-risico (#156).
- Opgemerkt tijdens de smoke: `image_model` staat op prod nu leeg (`''`) i.p.v. `gpt-image-1.5`;
  auto-detect kiest gpt-image-1.5 dus geen functioneel verschil, maar met #159 kan Ed hem nu
  expliciet zetten.
