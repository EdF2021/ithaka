# Auto-routing van beeld- en videoverzoeken (gpt-image-1.5 / Veo 3.1)

Datum: 2026-09-02. Status: goedgekeurd in chat, wacht op spec-review.

## Doel

Een gebruiker die in de chat om een afbeelding vraagt krijgt die automatisch via
`gpt-image-1.5`; wie om een video vraagt krijgt die automatisch via Veo 3.1, zonder handmatig
Agent-modus of een tool te kiezen. Beide werken in Chat- én Agent-modus.

## Wat er al is

- `generate_image`-tool (`src/tool_schemas.py`, uitgevoerd via de ingebouwde `image_gen`-MCP-server,
  `src/ai_interaction.py:do_generate_image`) met auto-detectie die `gpt-image-1.5` verkiest als
  `image_model` leeg is. Inline rendering in `static/js/chat.js` (`_buildImageBubble`).
- Intent-auto-escalatie: `src/action_intents.py:_ROUTING_PATTERNS` → `classify_tool_intent()`;
  `routes/chat_routes.py` promoveert Chat → Agent als `needs_tools` (met notebook-veto).
- RAG-tool-selectie (`src/tool_index.py`): top-K tools per bericht; er is al een
  "forceer generate_image voor bepaalde modellen"-mechanisme rond regel 349-361.
- Async-job-patroon: `src/notebook_audio.py` (`_active_jobs`, `asyncio.create_task`, POST
  start → UI pollt), media-serve `GET /api/notebook-video/{fn}` (auth + path-safe), uurlijkse
  janitor, passieve poll-paden in `src/interactive_gate.py:_PASSIVE_PATTERNS`.
- Gemini-ModelEndpoint in de DB (`https://generativelanguage.googleapis.com/v1beta/openai`, met key).

## Deel A — Beeld (bounded)

1. **Config (prod):** `image_model = "gpt-image-1.5"` expliciet zetten via `src.settings`.
2. **Intent-categorie `image`** in `_ROUTING_PATTERNS`, NL + EN:
   - werkwoorden: maak/genereer/teken/creëer/ontwerp/render/schets | make/generate/draw/create/design/render/paint
   - objecten: afbeelding/plaatje/foto/illustratie/logo/poster/tekening/icoon/banner | image/picture/photo/illustration/logo/poster/drawing/icon/banner
   - negatieven (geen match): "zoek/vind/search/find … afbeelding", "bekijk/analyseer/beschrijf … (deze) afbeelding" (vision), "upload".
   - `classify_tool_intent` geeft `ToolIntent(True, category="image")`.
3. **Tool-force:** wanneer de intent-categorie `image` is, wordt `generate_image` gegarandeerd in
   de aan het model aangeboden tools opgenomen (naast de RAG-top-K), tenzij de tool-policy of
   het privilege `can_generate_images` het blokkeert. Notebook-sessies: bestaand veto blijft.
4. Geen wijziging aan `do_generate_image` zelf.

## Deel B — Video (architectureel)

### B1. Veo-client — `src/video_gen.py` (nieuw)

- `resolve_gemini_endpoint(db) -> (base_url, api_key)`: eerste enabled `ModelEndpoint` waarvan
  `base_url` `generativelanguage.googleapis.com` bevat; `/openai`-suffix strippen → basis
  `https://generativelanguage.googleapis.com/v1beta`. Geen endpoint → duidelijke fout.
- `start_generation(prompt, *, model, aspect_ratio="16:9", resolution="720p", duration_seconds=8,
  negative_prompt="") -> operation_name`: `POST {base}/models/{model}:predictLongRunning`,
  header `x-goog-api-key`, body `{"instances":[{"prompt":…}],"parameters":{"aspectRatio":…,
  "resolution":…,"durationSeconds":"8","numberOfVideos":1,"negativePrompt":…}}`.
- `poll_operation(operation_name) -> dict`: `GET {base}/{operation_name}`; klaar als `done`.
  Resultaat-URI: `response.generateVideoResponse.generatedSamples[0].video.uri`. Ontbreekt de
  sample terwijl `done` → "geblokkeerd door safety-filter / geen output" (niet gefactureerd
  volgens Google-docs). `error`-veld → foutmelding met `message`.
- `download_video(uri, dest_path)`: GET met `x-goog-api-key`, redirects volgen, streamend naar
  schijf. URI's verlopen na 2 dagen; wij downloaden direct.
- Async job: `_active_jobs: dict[job_id, {status, prompt, model, owner, created, file, error,
  cost_estimate}]`; `asyncio.create_task(_run_job(...))`: start → poll elke 10 s, max 10 min →
  download → status `done`. Alle fouten → status `error` + reden. Timeouts via httpx bounded.
- Opslag: nieuwe constant `VIDEO_DIR = DATA_DIR / "videos"` in `src/constants.py` (guarded
  mkdir). Bestandsnaam `{job_id}.mp4`. `resolve_video_path(fn)` path-safe zoals notebook-video.
- Janitor `cleanup_orphaned_videos(max_age=7d)` uurlijks via `app.py`, zelfde patroon als audio.
- Kostenschatting: `duration × tarief` (720p standaard $0.40/s, fast $0.10/s, lite $0.05/s) in
  het job-record; alleen informatief.

### B2. Routes — `routes/video_routes.py` (nieuw, factory `setup_video_routes`)

- `POST /api/video/generate` `{prompt, aspect_ratio?, duration?}` → `require_privilege(
  "can_generate_videos")`, `video_gen_enabled` check → job-id (202).
- `GET /api/video/jobs/{job_id}` → status/record (eigenaar-check). Toegevoegd aan
  `_PASSIVE_PATTERNS` (`^/api/video/jobs/[^/]+$`).
- `GET /api/video/{filename}` → mp4 (auth, `resolve_video_path`, Range-support via FileResponse).

### B3. Tool `generate_video`

- Schema in `src/tool_schemas.py` naast `generate_image`: `prompt` (verplicht), `aspect_ratio`
  (`16:9`|`9:16`), `duration_seconds` (4|6|8, default 8). Beschrijving: "Use whenever the user
  asks to make/generate/create a video/clip/animation."
- Uitvoering in `src/tool_execution.py` als ingebouwde tool (zelfde plek als de
  `generate_image`-mapping): start de job en geeft direct terug
  `{"video_job_id": …, "video_model": …, "status": "running", "cost_estimate": …}`; het
  agent-antwoord meldt dat de video wordt gegenereerd. Geen wachten in de agent-ronde.
- `tool_index.py`: beschrijving voor de RAG-index; tool-force bij intent-categorie `video`.
- Tool-policy/privilege: `can_generate_videos` (default True, zoals images) in de
  privilege-lijst en de `disabled_tools`-logica in `routes/chat_routes.py`.

### B4. Intent-categorie `video`

NL + EN: maak/genereer/creëer/render … video/filmpje/clip/animatie/reel | make/generate/create/
render … video/clip/animation/reel. Negatieven: "zoek/find … video", "youtube", "transcribeer/
samenvat (deze) video", "notebook … video" (studio-video). Zelfde escalatie + tool-force.

### B5. Frontend — `static/js/chat.js`

- `tool_output` met `video_job_id` → statusbubble "Video wordt gegenereerd met {model}
  (meestal 1-3 min, ≈ ${cost})…" met spinner; poll `GET /api/video/jobs/{id}` elke 5 s tot
  `done`/`error` (max 12 min, daarna "duurt te lang, probeer later" + link naar job).
- `done` → `<video controls preload="metadata" src="/api/video/{fn}">` in de bubble (bestaande
  card-/bubble-klassen, geen nieuwe kleuren); `error` → rode regel met reden.
- Persisted history: het tool-event bevat `video_job_id` en (zodra bekend) `video_url`. Bij een
  reload rendert `chat.js` een event mét `video_url` direct als video; een event zónder
  `video_url` start opnieuw de job-poll. `GET /api/video/jobs/{id}` is herstart-bestendig:
  staat de job niet in `_active_jobs` maar bestaat `{job_id}.mp4` op schijf, dan antwoordt de
  route `done` met de bestands-URL; anders 404 → frontend toont "job niet meer bekend".

### B6. Settings

- `src/settings.py`: `video_gen_enabled` (default False; prod True), `video_model`
  (default `veo-3.1-generate-preview`), `video_resolution` (`720p`), `video_aspect_ratio`
  (`16:9`), `video_duration_seconds` (8). Opgenomen in de admin-settings-whitelist.
- `static/js/settings.js`: kaart "Video Generation" onder Image Generation: toggle, model-select
  (veo-3.1-generate-preview / veo-3.1-fast-generate-preview / veo-3.1-lite-generate-preview),
  resolutie, aspect, duur; regel met kostenschatting per clip.

## Foutafhandeling

| Situatie | Gedrag |
|---|---|
| Geen Gemini-endpoint / geen key | tool-output fout "Geen Gemini-endpoint met API-key"; bubble toont het |
| Veo 4xx/5xx bij start | job `error` met status + message-fragment (geen key in tekst) |
| Safety-block (done zonder sample) | job `error` "Geblokkeerd door Veo safety-filter — niet gefactureerd" |
| Poll-timeout > 10 min | job `error` "Time-out"; bestand niet aangemaakt |
| Download faalt | job `error`; retry 1× |
| `video_gen_enabled` uit | tool geblokkeerd met reden, zoals `image_gen_enabled` |

## Tests

- `tests/test_action_intents_media.py`: NL/EN positieven en negatieven voor `image` en `video`;
  bestaand gedrag (web/notes/calendar) ongewijzigd.
- `tests/test_video_gen.py`: `resolve_gemini_endpoint` (suffix-strip, geen endpoint), start
  (body/headers), poll (running/done/safety-block/error), download (redirect, streaming), job-
  loop met gemockte httpx en gepatchte sleep (timeout-pad).
- `tests/test_video_routes.py`: privilege/enabled-gates, owner-check op job, path-traversal op
  `/api/video/{fn}`, passive-pattern-match.
- `tests/test_tool_force_media.py`: intent `image`/`video` → tool aanwezig in aangeboden set;
  geblokkeerd door privilege → afwezig.
- `tests/test_chat_video_js.py` (node): status-bubble en `<video>`-render uit een
  tool_output-event; error-pad.
- Smoke (:7001 + browser, desktop + 360px): "maak een afbeelding van …" in Chat-modus → inline
  beeld via gpt-image-1.5 (echte OpenAI-call); "maak een video van …" → statusbubble → één
  echte Veo-run (≈ $3,20) → inline video afspeelbaar; settings-kaart; reload toont video.

## Buiten scope

Gallery-integratie voor video, lokale videomodellen (Wan), image-to-video/extend, bevestigings-
dialoog vóór generatie (bewust: direct genereren), meerdere video's per verzoek.
