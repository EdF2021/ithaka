# Notebooks Fase 3 (audio overview / podcast) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eén-klik "Podcast": twee-stemmen-dialoogscript uit notebook-bronnen → per-beurt TTS →
WAV-concat → durable bestand + inline player, als asynchrone job met voortgang.

**Spec:** `docs/superpowers/specs/2026-08-17-notebooks-fase3-audio-design.md` — bindend; lees eerst.

## Global Constraints

- Virtualenv: **`/home/eddef/projects/ithaka/.venv/bin/python`** (absoluut pad).
- Commit-messages eindigen met exact `Ed de Feber, in nauwe samenwerking met Claude` — GEEN Co-Authored-By.
- **Geen Unicode-emoji** in UI of code; geen `window.confirm/alert/prompt`.
- CSS: alleen bestaande variabelen/klassen; appends aan het EIND van het notebook-blok in `static/style.css`.
- Tests hermetisch: `make_temp_sqlite`; LLM via monkeypatch op `src.notebook_audio.task_llm_call_async`;
  TTS via monkeypatch op de synthese-hook; geen netwerk; geen echte sleep-loops (poll-logica met korte timeouts).
- Constants-regel: paden alleen via `src/constants.py` (`NOTEBOOK_AUDIO_DIR` toevoegen); guarded mkdir.
- Testbestandsnamen: `tests/test_services_notebook_audio.py` / `tests/test_routes_notebook_audio.py`.
- Geen `.superpowers/`-bestanden committen.
- Raak Fase 1/2-gedrag niet aan behalve waar dit plan het zegt; `tests/test_routes_notebooks.py` en
  `tests/test_services_notebook_artifacts.py` blijven groen.
- Lange job: NIET synchroon in een request; geen nieuwe timeout-exempties nodig (start-POST keert direct terug).

---

### Task 1: TTS-laag — synthesize_voice + response_format

**Files:** Modify: `services/tts/tts_service.py`; Test: `tests/test_services_notebook_audio.py` (nieuw, alleen TTS-deel)

**Interfaces (produces):**
- `_synthesize_api(..., response_format: str = "mp3")` — param 1-op-1 in payload (regel ~131).
- `TTSService.synthesize_voice(self, text: str, voice: str) -> bytes`: provider uit settings;
  `local` → `self.kokoro.synthesize_raw(text, voice)`; `endpoint:<id>` →
  `self._synthesize_api(text, endpoint_id, model, voice, speed, response_format="wav")` (model/speed
  uit settings zoals `synthesize()` dat doet); `disabled`/`browser` → `RuntimeError`;
  None/leeg resultaat → `RuntimeError`. Zelfde cache-mechanisme als `synthesize()` (cache-key bevat voice).
  GEEN 5000-char-truncatie in dit pad.

- [ ] Step 1: failing tests — monkeypatch `_load_settings` en de provider-methodes; asserteer dispatch,
  voice-doorvoer, response_format="wav" bij endpoint, RuntimeError bij disabled/leeg, cache-hit bij tweede call.
- [ ] Step 2: FAIL → implementeer → PASS + `py_compile`. Bestaande `synthesize()`-gedrag ongewijzigd
  (bestaande TTS-tests indien aanwezig groen). Commit `feat(tts): synthesize_voice met per-call voice en response_format`.

---

### Task 2: Datamodel + constants — audio_path-kolom en NOTEBOOK_AUDIO_DIR

**Files:** Modify: `core/database.py`, `src/constants.py`; Test: `tests/test_services_notebook_audio.py` (uitbreiden)

**Interfaces (produces):**
- `src/constants.py`: `NOTEBOOK_AUDIO_DIR` naast `GENERATED_IMAGES_DIR`-patroon, guarded mkdir.
- `NotebookArtifact.audio_path` — `Column(String, nullable=True)`; `to_dict()` krijgt `audio_path`.
- `_migrate_add_notebook_artifact_audio_path_column()` naar het patroon van de bestaande
  `_migrate_add_*_column`-functies (core/database.py:711 e.v.), aangeroepen in `init_db`.

- [ ] Step 1: failing tests — roundtrip met audio_path; to_dict bevat het veld (null default); migratie:
  maak tabel zonder kolom in temp-sqlite, run migratiefunctie, kolom bestaat (spiegel bestaande migratie-tests
  als die er zijn, anders PRAGMA table_info-assert). Step 2: FAIL → implement → PASS + py_compile.
  Commit `feat(notebooks): audio_path op NotebookArtifact + NOTEBOOK_AUDIO_DIR`.

---

### Task 3: Audio-module — script, parser, concat, job-runner

**Files:** Create: `src/notebook_audio.py`; Test: `tests/test_services_notebook_audio.py` (uitbreiden)

**Interfaces:**
- Consumes: `gather_source_text`, `_strip_think_blocks` (import uit `src.notebook_artifacts`),
  `task_llm_call_async`, `NotebookArtifact`/`Document`/`Notebook`, `fire_event`, `NOTEBOOK_AUDIO_DIR`.
- Produces (per spec §2): `PODCAST_PROMPT`, `parse_dialogue`, `split_turn(text, limit=4500)`,
  `concat_wavs`, `_active_jobs`, `start_podcast_job(notebook_id, owner, db_session_factory) -> str`,
  `get_job(job_id, owner)`, `resolve_notebook_audio_path(filename)` (regex `^[a-f0-9]{32}\.wav$` +
  commonpath-guard), synthese-hook `set_synthesizer(fn)` (of module-attribuut) voor injectie/tests.
- LLM-call: `wait_for_quiet=False, workload="foreground"` met de spec-rationale als comment.
- Stemmen: settings `notebook_podcast_voice_a/b`, defaults per provider (kokoro `af_heart`/`am_michael`,
  endpoint `alloy`/`onyx`).
- Foutpad: status `error` + melding; géén Document/artifact/bestand (tempfile → rename bij succes).

- [ ] Step 1: failing tests — parser (S1/S2, multi-regel-continuatie, think-strip, leeg → RuntimeError);
  split_turn op zinsgrens; concat_wavs met échte mini-WAVs (stdlib gegenereerd; mismatch → RuntimeError);
  job-flow end-to-end met gemockte LLM + synth (asyncio: start → poll tot done → artifact-row + Document +
  bestand bestaan; foutpad: synth raise → error-status, geen rows, geen bestand; owner-check op get_job).
- [ ] Step 2: FAIL → implement → PASS + py_compile. Commit `feat(notebooks): podcast-audiomodule (script, per-beurt TTS, concat, job)`.

---

### Task 4: API — podcast-routes + audio-serving + delete-opruiming

**Files:** Modify: `routes/notebook_routes.py`, `app.py` (alleen `tts_service=` doorgeven); Test: `tests/test_routes_notebook_audio.py` (nieuw, patroon `tests/test_routes_notebook_artifacts.py`)

**Interfaces (per spec §3):**
- `setup_notebook_routes(..., tts_service=None)`.
- `POST /api/notebooks/{id}/podcast` → validatievolgorde: owner-404 → bronnen-400 ("Geen geïndexeerde
  bronnen") → TTS-400 ("TTS is niet geconfigureerd") → `{"job_id","status":"running"}`.
- `GET /api/notebooks/{id}/podcast/{job_id}` → job-dict of 404.
- `GET /api/notebook-audio/{filename}` → regex + artifact-ownership-lookup → `FileResponse`
  (`audio/wav`, immutable-headers); 404 bij onbekend/cross-owner.
- Artifact-DELETE + notebook-DELETE: audio-bestanden best-effort unlinken.
- GET-artifacts: `audio_path` in de items (komt gratis uit to_dict, asserteer).

- [ ] Step 1: failing tests — `start_podcast_job`/`get_job` gemonkeypatcht op route-module-niveau;
  serve-route met echt temp-bestand in gemockte NOTEBOOK_AUDIO_DIR (monkeypatch constant);
  400/404-matrix; delete unlinkt bestand; `tests/test_routes_notebooks.py` blijft groen.
- [ ] Step 2: FAIL → implement → PASS + py_compile app.py/routes. Commit `feat(notebooks): podcast-API (start, status, audio-serving, opruiming)`.

---

### Task 5: Frontend — Podcast-knop, voortgang, player

**Files:** Modify: `static/js/notebooks.js`, `static/style.css`

**Interfaces (per spec §4):** zesde knop data-kind="podcast" met `_generatePodcast()` (POST → pending-rij
met fase-tekst → 2s-polling → done: `_renderArtifacts()`; error/404: `_showError('#notebook-artifact-error')`
+ pending-rij weg + knop herstellen); podcast-rij: pill, titel-klik toggle't uitklap met
`<audio controls preload="none">` + "Open transcript"-link (bestaand `_openArtifact`-pad) +
downloadlink; API-shapes exact per spec (URL `/api/notebook-audio/{audio_path}`).

- [ ] Step 1: implementeer flows (patronen: `_fetchJson`, `_showError`, `_armConfirm`, bestaande
  `_artifactRow`-structuur; polling met `setTimeout`-loop die stopt bij close/done/error).
- [ ] Step 2: CSS-append (alleen `.notebook-podcast-*`/audio-regels, bestaande tokens). Step 3:
  `node --check static/js/notebooks.js`. Commit `feat(notebooks): Podcast-knop, voortgang en audio-player`.

---

### Task 6 (controller): integratie + smoke + PR

Testsweep (`run_focus --area services` / `--area routes --fast`, notebook-suites), eindreview hele
branch (opus), browser-smoke per spec-testplan met lokale OpenAI-compatible TTS-stub (:7099, per
voice een andere toonfrequentie), mobiel 360px, PR + merge met zichtbare smoke-output.
