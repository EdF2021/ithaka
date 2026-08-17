# Notebooks Fase 3 — Audio overview (podcast) — Design

*2026-08-17. Bindend voor het implementatieplan. Gebaseerd op drie verkenningen (TTS-subsysteem,
media-serving, async-jobs) — kernvindplaatsen hieronder per onderdeel.*

## Doel

Eén klik "Podcast" in de notebook-detailweergave genereert een twee-stemmen-dialoog-audiobestand
uit de notebook-bronnen: LLM-dialoogscript → per-beurt TTS met eigen stem → WAV-concatenatie →
durable bestand + inline player. Het script zelf wordt als Document bewaard (leesbaar transcript).

**Niet in scope:** video, realtime/interactieve audio, ffmpeg/pydub-dependencies, Kokoro-installatie,
SSE (polling volstaat), meertalige stemkeuze-UI.

## Architectuurbeslissingen (rulings)

1. **Asynchrone job, geen synchrone request.** Fase 2's twee-gates-les: synchroon is onhoudbaar bij
   minutenlange generatie. Spiegel `src/research_handler.py`: in-memory `_active_jobs`-dict,
   `asyncio.create_task`, start-POST keert direct terug, UI polt status. Jobs overleven een restart
   niet (research-precedent); status-poll geeft dan 404 en de UI toont een nette fout.
2. **LLM-call in de job**: via `task_llm_call_async(..., wait_for_quiet=False, workload="foreground")`
   — de job is user-initiated en interactief (gebruiker wacht met open browser); `wait_for_quiet=True`
   zou de job laten wachten tot de browser stil is. Zelfde semantiek als deep research (dat
   `llm_call_async` met foreground-default gebruikt, `llm_core.py:2231`).
3. **Datamodel**: hergebruik `NotebookArtifact` met `kind="podcast"`. `document_id` (NOT NULL) wijst
   naar het script-Document. Nieuwe **nullable kolom `audio_path`** (alleen bestandsnaam) via het
   bestaande `_migrate_add_*_column`-patroon (`core/database.py:711` e.v., aangeroepen in `init_db`).
   Artifact-row + Document + audiobestand ontstaan pas ná volledig geslaagde pipeline (Fase 2-invariant).
4. **Alles WAV, stdlib-concat.** Kokoro levert al WAV (mono/16-bit/24kHz, `tts_service.py:279-283`);
   de endpoint-provider krijgt een `response_format`-parameter zodat `/audio/speech` om `"wav"`
   gevraagd wordt (nu hardcoded `"mp3"`, `tts_service.py:131`). Concat = stdlib `wave`: headers
   valideren (framerate/kanalen/sampwidth gelijk, anders RuntimeError), frames achter elkaar.
   Geen nieuwe dependencies.
5. **Serving naar generated-images-patroon, eigen module-functies.** Nieuwe constante
   `NOTEBOOK_AUDIO_DIR = DATA_DIR / "notebook_audio"` in `src/constants.py` (guarded mkdir zoals de
   rest). Regex-whitelist `^[a-f0-9]{32}\.wav$` (uuid4().hex-bestandsnaam). Ownership via
   artifact-row-lookup (audio_path == filename → notebook → owner), 404 anders. `FileResponse`
   (Range/seek gratis in Starlette 1.3.1); headers zoals `GENERATED_IMAGE_HEADERS` — immutable is
   veilig want elke generatie krijgt een nieuwe bestandsnaam.
6. **TTS-provider is een voorwaarde, geen onderdeel.** Bij `tts_provider` in
   (`disabled`, `browser`) → 400 "TTS is niet geconfigureerd (Settings → TTS)". Stemmen per provider
   met defaults: kokoro `af_heart`/`am_michael`, endpoint `alloy`/`onyx`; overschrijfbaar via
   optionele settings-keys `notebook_podcast_voice_a` / `notebook_podcast_voice_b`.

## Componenten

### 1. TTS-laag (`services/tts/tts_service.py`)

- `_synthesize_api(...)` krijgt extra parameter `response_format: str = "mp3"` die 1-op-1 in de
  payload gaat (backwards compatible).
- Nieuwe publieke methode `synthesize_voice(self, text: str, voice: str) -> bytes`:
  leest provider uit settings; `local` → `kokoro.synthesize_raw(text, voice)`;
  `endpoint:<id>` → `_synthesize_api(text, endpoint_id, model, voice, speed, response_format="wav")`;
  `disabled`/`browser` → `RuntimeError("TTS niet geconfigureerd")`. Geen 5000-char-truncatie hier:
  caller chunked. Cache-key bevat het formaat (`f"{voice}|wav"`, niet de kale `voice`):
  `synthesize()` cachet zijn mp3-output onder dezelfde `voice`-string, dus zonder dat onderscheid
  zou een eerdere chat-TTS-mp3 voor dezelfde tekst/stem hier geserveerd worden en deterministisch
  falen in `wave.open` tot de cache geleegd wordt. Retour None/lege bytes → RuntimeError.

### 2. Audio-module (`src/notebook_audio.py`)

- `PODCAST_PROMPT`: dialoogscript-instructie — taal van de bronnen; twee hosts; formaat exact
  regelgewijs `S1: <tekst>` / `S2: <tekst>`; geen markdown-opmaak, geen preamble; richtlengte
  20–40 beurten, elke beurt ≤ 400 woorden; opening introduceert het onderwerp, slot vat samen.
- `parse_dialogue(script: str) -> list[tuple[str, str]]`: strip_think (hergebruik uit
  `notebook_artifacts`), regelgewijs regex `^\s*(S1|S2)\s*:\s*(.+)$` (case-insensitive),
  multi-regel-beurten: regels zonder prefix horen bij de vorige beurt; lege lijst → RuntimeError.
- `split_turn(text, limit=4000) -> list[str]`: splitst een te lange beurt op zinsgrenzen (4000, niet
  5000: de echte OpenAI `/audio/speech`-limiet is 4096 tekens).
- `concat_wavs_to_file(segments, dest_path) -> int`: stdlib `wave`, streamt rechtstreeks naar
  `dest_path` (geen volledige audio ooit in het geheugen); parameter-mismatch of 0 frames totaal →
  RuntimeError.
- Job-runner naar research-vorm: module-level `_active_jobs: dict[str, dict]`
  (`{status, phase, segment, total, error, artifact, owner, notebook_id, started_at}`).
  `start_podcast_job(notebook_id, owner) -> str` (job_id = uuid4().hex): valideert notebook/owner en
  indexed bronnen (hergebruik query-vorm uit `notebook_artifacts`), TTS-beschikbaarheid; registreert
  entry; `asyncio.create_task(_run(job_id, ...))` met `asyncio.wait_for(..., 1800)`.
  `_run`: fase `script` (gather_source_text + LLM + parse) → fase `tts` (per beurt
  `synthesize_voice`, stem A voor S1 / stem B voor S2, progress `segment/total`) → fase `concat` →
  schrijf `<uuid4().hex>.wav` naar `NOTEBOOK_AUDIO_DIR` → script-Document (titel
  `f"{notebook.name} — Podcast"`, language `markdown`, transcript met sprekerLabels) +
  `NotebookArtifact(kind="podcast", audio_path=<filename>)` → `fire_event("document_created", owner)`
  → status `done` + artifact-dict in de entry. Elke fout → status `error` + boodschap; géén rows,
  géén (half) bestand (tempfile → rename, of unlink bij fout).
  `get_job(job_id, owner) -> dict | None` (owner-check!).
- `resolve_notebook_audio_path(filename) -> Path`: regex-fullmatch + commonpath-guard naar het
  patroon van `src/generated_images.py:20-32`.
- TTS-aanroep loopt via een module-attribuut (`_synthesize = tts_service.synthesize_voice`-injectie
  bij setup) zodat tests hem monkeypatchen; de TTSService-instantie wordt door `app.py` al gebouwd
  en via `setup_notebook_routes` doorgegeven (nieuwe parameter, zie 3).

### 3. API (`routes/notebook_routes.py`)

`setup_notebook_routes` krijgt een extra keyword-parameter `tts_service=None` (app.py geeft de
bestaande instantie door — dit is de enige app.py-wijziging naast geen).

- `POST /api/notebooks/{id}/podcast` → validatievolgorde als Fase 2 (owner-lookup → 404; geen
  indexed bronnen → 400 "Geen geïndexeerde bronnen"; TTS niet geconfigureerd → 400) →
  `start_podcast_job` → `{"job_id": ..., "status": "running"}`. Geen timeout-exemption nodig
  (keert direct terug).
- `GET /api/notebooks/{id}/podcast/{job_id}` → job-entry (zonder interne velden) of 404
  (onbekend, verkeerde owner, of na restart).
- `GET /api/notebook-audio/{filename}` → regex/ownership-check (artifact-row met
  `audio_path == filename` → notebook → owner) → `FileResponse` met `audio/wav` + immutable-headers.
- Artifact-DELETE en notebook-DELETE: naast Document ook het audiobestand best-effort unlinken
  (`audio_path` van elke te verwijderen podcast-artifact).
- GET-artifacts-lijst: `to_dict` bevat `audio_path` (null voor tekst-artifacts) — de UI leidt de
  player-URL af als `/api/notebook-audio/{audio_path}`.

### 4. UI (`static/js/notebooks.js` + `static/style.css`)

- Zesde knop "Podcast" (data-kind="podcast") in de Artifacts-knoppenrij, met eigen handler
  `_generatePodcast()`: POST → pending-rij bovenaan de artifactlijst met label "Podcast",
  statusregel per fase ("Script schrijven…", "Audio genereren… 3/24", "Samenvoegen…"), polling elke
  2s op de status-route; `done` → `_renderArtifacts()`; `error`/404 → `_showError` +
  pending-rij weg. Knop disabled zolang de job loopt.
- Artifact-rij met `kind === "podcast"`: pill "Podcast"; klik op titel toggle't een uitklap-div
  onder de rij met `<audio controls preload="none" src="/api/notebook-audio/<audio_path>">` +
  tekstlink "Open transcript" (bestaand `_openArtifact`-pad naar het Document) + downloadlink
  (`<a href=... download>` met de audio-URL). Podcast-rij gebruikt dus NIET het standaard
  `_openArtifact`-klikpad.
- CSS: append in het notebook-blok; alleen bestaande tokens; audio-element `width:100%`.
  Geen emoji; geen confirm/alert.

## Fouten & randgevallen

- TTS-provider disabled/browser → 400 vóór jobstart, met melding die naar Settings verwijst.
- Beurt > 4500 chars → `split_turn`, segmenten na elkaar met dezelfde stem.
- WAV-parameter-mismatch tussen segmenten (bijv. endpoint levert stereo/44k1) → job-error met
  duidelijke boodschap (geen resample in v1).
- Server-restart tijdens job → job weg; poll-404; UI meldt "Generatie afgebroken (server herstart)".
- Script zonder parsebare S1/S2-regels → job-error, geen rows.
- Regeneratie maakt altijd een nieuw artifact + nieuw bestand (geen overschrijven) — consistent met
  Fase 2 en veilig voor immutable-cache.
- Library-delete van het script-Document is een soft delete (zie Fase 2-spec-amendement): het
  podcast-artifact + audio blijven bestaan tot hard-delete of artifact/notebook-delete.

## Testplan (browser-smoke, naast pytest)

Voor de smoke zonder betaalde TTS: lokale OpenAI-compatible stub op :7099 die op
`POST /v1/audio/speech` een korte echte WAV (toon, per voice een andere frequentie) teruggeeft;
in de app als endpoint + TTS-provider `endpoint:<id>` geconfigureerd. Dan: Podcast-klik →
voortgangsfasen zichtbaar → player verschijnt → audio speelt/seekt (Range) → transcript opent →
download werkt → delete ruimt bestand op → mobiel 360px. Plus alle Fase 1+2-regressies groen.

Ed de Feber, in nauwe samenwerking met Claude
