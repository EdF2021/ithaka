# Sessie 2026-08-17 — Notebooks Fase 3: audio overview (podcast)

## Wat er gebouwd is

Eén-klik "Podcast": twee-stemmen-dialoogscript uit notebook-bronnen → per-beurt TTS → WAV-concat →
durable bestand + inline player, als asynchrone job met voortgang.

- **T1** `TTSService.synthesize_voice(text, voice) -> bytes` (`services/tts/tts_service.py`):
  provider-dispatch (kokoro lokaal / endpoint met nieuwe `response_format`-param, nu ook "wav"
  i.p.v. hardcoded "mp3"), RuntimeError bij disabled/browser/leeg resultaat, zelfde cache als
  `synthesize()`.
- **T2** Datamodel: `NotebookArtifact.audio_path` (nullable, migratie via bestaand
  `_migrate_add_*_column`-patroon) + `NOTEBOOK_AUDIO_DIR` in `src/constants.py` (guarded mkdir,
  empirisch getest op read-only tree).
- **T3** Audio-module `src/notebook_audio.py`: `PODCAST_PROMPT`, `parse_dialogue` (S1/S2-regex,
  multi-regel-continuatie), `split_turn` (zinsgrens-split >4500 chars), `concat_wavs` (stdlib
  `wave`), job-runner (`_active_jobs`, `start_podcast_job`, `get_job` met owner-check,
  `resolve_notebook_audio_path`), synthese-hook via `set_synthesizer` voor test-injectie.
- **T4** API in `routes/notebook_routes.py`: `POST /api/notebooks/{id}/podcast`
  (owner→bronnen→TTS-validatievolgorde), `GET .../podcast/{job_id}` (status-poll),
  `GET /api/notebook-audio/{filename}` (regex+ownership → FileResponse, immutable-headers),
  delete-opruiming van audiobestanden; `app.py` geeft de bestaande `tts_service`-instantie door.
- **T5** UI in `static/js/notebooks.js` + CSS: zesde knop "Podcast", pending-rij met fasetekst,
  2s-polling, uitklap-rij met `<audio controls>`, transcript-link, downloadlink.

## Kernbeslissingen

- **Asynchrone job i.p.v. synchrone request — de twee-gates-les uit Fase 2 toegepast.** Spec
  ruling 1/2 spiegelt `research_handler.py`: in-memory `_active_jobs`, `asyncio.create_task`,
  start-POST keert direct terug, UI polt. Geen nieuwe timeout-exemptie nodig — vermijdt de twee
  gate-deadlocks (`wait_for_interactive_quiet`, `has_foreground_activity`) die Fase 2 pas in de
  browser-smoke trof. De LLM-call binnenin de job gebruikt wel `wait_for_quiet=False,
  workload="foreground"` (zelfde semantiek als deep research), omdat de gebruiker interactief
  wacht met open browser.
- **Script als Document + aparte `audio_path`-kolom.** Hergebruik van `NotebookArtifact` met
  `kind="podcast"`; het script blijft een leesbaar transcript-Document (consistent met Fase 2's
  artifact-model), het audiobestand zelf via een nieuwe nullable kolom — geen apart datamodel
  nodig.
- **Alles-WAV met stdlib-concat.** Kokoro levert al WAV; de endpoint-provider kreeg een
  `response_format`-parameter zodat ook die WAV teruggeeft i.p.v. hardcoded mp3. Concatenatie via
  stdlib `wave` (header-validatie + frames-aan-elkaar) — geen ffmpeg/pydub-dependency.
- **`to_thread` voor de synchrone TTS-call in de async job-runner.** `synthesize_voice` is een
  blocking call (kokoro/HTTP); de job draait als asyncio-task, dus elke per-beurt TTS-aanroep
  loopt via `asyncio.to_thread` zodat de event loop niet blokkeert tijdens minutenlange
  audio-generatie.

## Review-vondsten

- **T3** (opus-review): Approved-met-2-Importants — I1 validatiepariteit: de job-validatie week af
  van `_source_entries` (lege-content-bron leidde tot job-error i.p.v. vooraf een 400); I2
  testleak: `set_synthesizer` werd buiten een monkeypatch-context gezet, wat T4's testvolgorde kon
  raken. Beide gefixed in fixronde 1/5 (commit 59af472), re-review: beide ADDRESSED.
- **T4**: Needs fixes — 1 Important: een zelf-toegevoegde boot-time-gate op `set_synthesizer`
  (spec vroeg onvoorwaardelijke wiring) gaf een verkeerde 400-tekst als TTS ná opstarten alsnog
  werd aangezet zonder restart. Fix in ronde 1/5 (commit 054f6e9): onvoorwaardelijke wiring,
  re-review ADDRESSED (120/120 tests).
- **Eindreview** (opus, hele branch): Ready WITH FIXES — 0 Critical, 1 Important: concat-RAM-piek
  (~4x geheugengebruik bij het in-memory samenvoegen van alle WAV-segmenten) → fix F1:
  streaming-concat naar tempfile. Daarnaast een spec-defect: het cache-key-formaat van
  `synthesize_voice` kwam niet overeen met de spec-tekst → F2 (cache-key + response-format-fix,
  met spec-amendement). Fix-wave in één dispatch, F1–F10 in totaal (o.a. duplicate-job-guard,
  download-bestandsnaam-encoding, lege-WAV-guard na concat, MAX_SEGMENT_CHARS 4500→4000 voor de
  OpenAI-cap).

## Smoke

Verse instance :7001 (branch-head) + OpenAI-compatible TTS-stub op :7099 (WAV-tonen per stem,
mono/16-bit/24kHz — zelfde parameters als Kokoro). Flow: Podcast-klik → fase "Script schrijven…" +
knop disabled → klaar in 102s (gemma3-script + stub-TTS) → artifact bovenaan met title-veld.
Player-uitklap: audio 16s, afspelen + seek OK; Range-request geeft 206 met content-range; 401
zonder sessie (auth op de audio-route bevestigd). Transcript-Document opent met volledig
S1/S2-dialoog (NL, brongetrouw: gnomon, breedtegraad, tijdvereffening, Leiden/Utrecht).
Download-link met nette bestandsnaam ("Zonnewijzers — Podcast.wav"). Foutpad live gezien: een
stub-404 gaf "Podcast mislukt: TTS synthesis failed" in het errorpaneel met intacte lijst.
Delete: artifact-row + Document + audiobestand alle drie aantoonbaar weg. Mobiel (smalste
vensterbreedte): player volle breedte, knoppenrij wrapt, geen horizontale overflow.

## Proces

Subagent-driven development volgens plan: Wave A (T1 tts + T2 datamodel, parallel, sonnet), Wave
B (T3 module opus + T5 UI sonnet, parallel, gepinde API-shapes), Wave C (T4 routes, sonnet), dan
T6-controller (sweeps + eindreview opus + stub-smoke + PR). Elke taak in eigen worktree,
cherry-pick-flow naar de feat-branch. Twee fixrondes op taakniveau (T3, T4) plus een fix-wave na
de eindreview (F1-F10). Pre-flight conflict-scan ving een testbestand-botsing tussen T1 en T2
vooraf op; de smoke draait tegen een lokale OpenAI-compatible TTS-stub (:7099, per stem een
andere toonfrequentie) zodat de hele pipeline end-to-end getest wordt zonder kosten.

Ed de Feber, in nauwe samenwerking met Claude
