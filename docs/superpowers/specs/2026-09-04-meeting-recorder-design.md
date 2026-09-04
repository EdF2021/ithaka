# Vergaderopname → notulen (meeting recorder) — design

**Datum:** 2026-09-04 · **Status:** goedgekeurd door opdracht (Ed: "werk zelfstandig tot de feature draait") · **Branch:** `feat/meeting-recorder`

## Doel

Een vergadering opnemen vanuit Ithaka (browser-microfoon), de opname stoppen en automatisch
notulen laten maken volgens een strak, vast sjabloon. De notulen verschijnen als Document in de
Library. Het samenvattingsalgoritme is Ed's bewezen recursieve meta-samenvatting uit
`berend/app/pages/6_De_Notulist_Demo.py` (correctiepas + head/tail-recursie met groeiende
splitsgrens), opnieuw geïmplementeerd tegen de bestaande Ithaka-seams.

## Niet in scope (YAGNI)

- Systeem-/tabaudio (`getDisplayMedia`) — alleen microfoon.
- Sprekerherkenning, sentiment (in Ed's code al uitgeschakeld).
- Word-export: Document is Markdown; bestaande Library-export volstaat.
- Eigen STT-/LLM-instellingen: hergebruikt STT-instellingen (Settings → AI Defaults) en de
  task-modelketen (`resolve_task_candidates`: Background Tasks → Utility → Default).
- Upload van een bestaand audiobestand (kan later als tweede ingang op dezelfde pipeline).

## Gebruikersflow

1. Rail-knop "Meetings" (naast Notes/Tasks) opent het paneel *Meetings* (zelfde shape als Notes:
   rechter zijpaneel op desktop, bottom-sheet op mobiel, `Modals.register` voor dock-chip).
2. Formulier: **Title** (verplicht), **Agenda** (optioneel, meerregelig), **Key terms**
   (optioneel; namen/afkortingen/jargon → STT-prompt). Knop **Start recording**.
3. Tijdens opname: timer, rode opname-indicator, statusregel *"Saved up to mm:ss"* (chunks
   worden elke 30 s geüpload; mislukte chunks worden herhaald; blijvende fout → rode melding
   "N chunks not saved"). Verlaten van de pagina geeft een `beforeunload`-waarschuwing.
   Harde limiet 3 uur → auto-stop.
4. **Stop** → opname sluit af, laatste chunk gaat mee, `POST …/finish` start de verwerking.
   Het item in de lijst toont de fase: *splitting → transcribing (i/n) → correcting (i/n) →
   condensing (depth d) → writing minutes → saving*. Het paneel pollt elke 3 s per lopende
   meeting.
5. Klaar → knop **Open minutes** opent het Document (`documentModule.loadDocument(id)`).
   Verder per meeting: **Audio** (download/afspelen), **Reprocess** (bij fout of na herstart),
   **Delete** (verwijdert rij + audio; het Document blijft).

UI-taal: Engels voor labels (consistent met Notes/Tasks/Calendar); foutteksten van de API en de
gegenereerde notulen in het Nederlands (zoals notebooks). Geen emoji; inline monochrome SVG.

## Architectuur

```
static/js/meetings.js ──POST /api/meetings──────────────▶ routes/meeting_routes.py
   │ MediaRecorder(timeslice 30 s)                          │  Meeting-rij (core/database.py)
   ├─POST /api/meetings/{id}/chunks?seq=N ─────────────────▶│  append → MEETING_AUDIO_DIR/{id}.webm
   ├─POST /api/meetings/{id}/finish ───────────────────────▶│  start_processing_job (src/meeting_minutes.py)
   └─GET  /api/meetings/{id}  (poll, passive) ◀────────────┘        │
                                                                    ▼
      ffmpeg split (10 min, ogg/opus) → STTService.transcribe ×N (parallel, sem 3)
      → correctie-LLM ×N (parallel) → recursieve condensatie (Ed) → sjabloon-LLM (+validator)
      → Document(owner, session_id=None, markdown) → Meeting.document_id, status=done
```

### Data

Nieuwe tabel `meetings` (`core/database.py`, `Meeting(TimestampMixin, Base)`):
`id` (uuid str, pk), `owner` (str, index), `title` (str), `agenda` (Text, nullable),
`key_terms` (Text, nullable), `status` (str: `recording|processing|done|error`),
`phase` (str, nullable), `error` (Text, nullable), `audio_path` (str, nullable — bestandsnaam in
`MEETING_AUDIO_DIR`), `bytes_total` (Integer, default 0), `duration_seconds` (Integer,
nullable), `document_id` (str, nullable), `finished_at` (DateTime, nullable). Tabellen worden
aangemaakt via de bestaande `Base.metadata.create_all`; geen migratie nodig (SQLite, nieuwe tabel).

Constanten (`src/constants.py`): `MEETING_AUDIO_DIR = DATA_DIR/meeting_audio` (eager `makedirs`,
guarded zoals `NOTEBOOK_AUDIO_DIR`). Limieten (`src/upload_limits.py`, via `read_byte_limit_env`):
`MEETING_CHUNK_MAX_BYTES` (env `ITHAKA_MEETING_CHUNK_MAX_BYTES`, default 10 MB) en
`MEETING_AUDIO_MAX_BYTES` (env `ITHAKA_MEETING_AUDIO_MAX_BYTES`, default 500 MB, totaal per
meeting). Client-cap `MEETING_MAX_MS = 3 h`.

### Opname & upload (client)

`MediaRecorder(stream, {mimeType:'audio/webm'})` met `start(30000)`. Elke `dataavailable`-blob gaat
in een uploadwachtrij (`createChunkUploader({post})` — pure, node-testbaar): sequentieel, per chunk
max 3 pogingen met backoff 1/2/4 s, telt `uploadedBytes`/`failedChunks`, callback `onStatus`.
Chunks worden **in volgorde** verstuurd (seq oplopend); de server weigert een seq die niet
`expected_seq` is met 409 (client stopt dan met een zichtbare fout — geen stille gaten). Bij
`stop`: wacht tot de wachtrij leeg is (of definitief mislukt), dan `finish`. Mislukte chunks →
`finish` wordt tóch aangeboden maar de UI meldt dat de opname mogelijk onvolledig is.

Server-append: `open(path,'ab')` van het rauwe chunk. MediaRecorder-timeslice-chunks van één
sessie vormen achter elkaar één geldige WebM-stream (eerste chunk draagt de header). ffmpeg
re-encodeert bij het splitsen (`-c:a libopus`), wat ook timestamp-oneffenheden gladstrijkt.

### Verwerking (`src/meeting_minutes.py`)

Async job, zelfde shape als `src/notebook_audio.py`: `_active_jobs` dict, `_PUBLIC_JOB_FIELDS`
(`status, phase, segment, total, depth, error, document_id, meeting_id, started_at`),
`_reap_stale_jobs`, `get_job(job_id, owner)`, `start_processing_job(meeting_id, owner,
db_session_factory=None) -> job_id` (valideert: rij bestaat & eigenaar, `audio_path` bestaat,
STT-provider niet `disabled|browser` → `RuntimeError("STT niet geconfigureerd")`; een al lopende
job voor dezelfde meeting → `ValueError("Verwerking loopt al")`). `JOB_TIMEOUT_SECONDS = 3600`.
Terminale status wordt ook in de `Meeting`-rij geschreven (`status`, `phase`, `error`,
`document_id`, `finished_at`), zodat de lijst na een herstart klopt; een rij `processing` zonder
in-memory job wordt door de GET-route als `error: "Verwerking onderbroken (herstart) — Reprocess"`
gepresenteerd.

Fasen (alle LLM-calls via `task_llm_call_async(messages, owner=owner, wait_for_quiet=False,
workload="foreground")` — de self-deadlock-regel uit CLAUDE.md):

1. **splitting** — `split_audio(src: Path, workdir: Path, segment_seconds=600) -> list[Path]`:
   `ffmpeg -nostdin -y -i src -vn -c:a libopus -b:a 32k -f segment -segment_time 600
   -reset_timestamps 1 workdir/seg_%03d.ogg`; `subprocess.run` in `asyncio.to_thread`, timeout
   900 s. Nul segmenten of non-zero exit → `RuntimeError("Audio kon niet worden gesplitst")`.
2. **transcribing** — per segment `await asyncio.to_thread(stt.transcribe, data,
   prompt=build_stt_prompt(key_terms), timeout=600, filename="seg.ogg")` onder
   `asyncio.Semaphore(3)`; `segment`/`total` worden bijgewerkt. Eén `None` → job-error
   `"Transcriptie mislukt (segment i): <last_error>"`. Lege string is toegestaan (stilte).
   `build_stt_prompt(key_terms)` = Ed's prompt: *"Maak een foutloze transcriptie van het audio
   bestand. In het audiobestand worden de volgende afkortingen, namen, jargon, gebruikt: …"* (zonder
   de tweede zin als `key_terms` leeg is).
3. **correcting** — per segment `correct_transcript(text)` met Ed's systemprompt (spelling,
   grammatica, alleen noodzakelijke punctuatie, alinea's, alleen gegeven context, Nederlands).
   Parallel (sem 3). Faalt de call → ruwe segmenttekst (job faalt niet). Resultaat: segmenten in
   volgorde samengevoegd met `\n\n`.
4. **condensing** — Ed's recursie, als pure async functie
   `condense_transcript(text, call, *, depth=0, carry="", on_depth=None) -> str`:
   `split = min(70000, int(5000 + 5000*depth/2))`; `work = carry + "\n\n" + text` (carry = vorige
   deelnotulen); `len(work) <= split` → één call met de *eind*-prompt (uitgebreide notulen in
   abstracte alinea's) en klaar; anders head = `work[:split]`, tail = `work[split:]`, call met de
   *deel*-prompt (Opening en mededelingen, hoofdpunten, besluiten, actielijst, volgende
   vergadering; "dit is een deel, rondvraag/afsluiting kunnen in een volgend deel komen"), recursie
   op `(tail, carry=resultaat, depth+1)`. Beide prompts letterlijk uit Ed's code, aangevuld met
   `DUTCH_OUTPUT_RULE`. `on_depth(depth)` werkt `depth` in het job-record bij. Lege tekst → `""`.
5. **writing minutes** — `build_minutes(condensed, *, title, agenda, date_str, duration, call)`
   → één call met een strikt sjabloon (hieronder). Validator `validate_minutes(md)` eist alle
   verplichte koppen in volgorde; bij falen één retry met corrigerende nudge (zoals de
   podcast-scriptretry); daarna toch opslaan met waarschuwing in `phase`-log (niet falen).
6. **saving** — `Document(id, owner, title=f"Notulen – {title} – {date}", language="markdown",
   current_content=render_minutes_document(minutes_md, transcript), session_id=None)`; commit;
   `Meeting.document_id`, `status="done"`. Werkdir (`MEETING_AUDIO_DIR/.meetingjob-<id>/`)
   wordt altijd opgeruimd (`finally`).

Sjabloon (vaste koppen; de LLM vult alleen de inhoud, tabel voor actiepunten; ontbrekende
onderdelen expliciet "Geen."):

```
# Notulen: {title}

**Datum:** {date}  ·  **Duur:** {duration}  ·  **Opname:** Ithaka

## Agenda            (alleen als agenda gegeven; letterlijk overgenomen, niet door de LLM)
## Samenvatting
## Besproken punten
## Besluiten
## Actiepunten
| Actie | Eigenaar | Deadline |
|---|---|---|
## Volgende vergadering
```

`render_minutes_document` plakt daaronder `## Bijlage: transcript` met het gecorrigeerde
transcript. Prompts leven in `src/meeting_minutes.py` en embedden `DUTCH_OUTPUT_RULE`
(`src/notebook_language.py`); transcript gaat als untrusted context (`untrusted_context_message`
uit `src.prompt_security`, zoals de podcast).

### Routes (`routes/meeting_routes.py`, `setup_meeting_routes(...)` in `app.py`)

| Methode/pad | Gedrag |
|---|---|
| `POST /api/meetings` | body `{title, agenda?, key_terms?}` → maakt rij `status=recording` → `{id}`; lege title → 400 |
| `POST /api/meetings/{id}/chunks?seq=N` | multipart `file`; 404 onbekend/andere eigenaar; 409 `seq != expected`; 413 chunk > `MEETING_CHUNK_MAX_BYTES` of totaal > `MEETING_AUDIO_MAX_BYTES`; 400 als status ≠ recording; append → `{seq, bytes_total}` |
| `POST /api/meetings/{id}/finish` | body `{duration_seconds?}`; status `recording|error|done` toegestaan (reprocess); 400 zonder audio ("Geen audio ontvangen"); `start_processing_job` fouten → 400 met Nederlandse tekst; → `{job_id, status:"processing"}` |
| `GET /api/meetings` | lijst van eigenaar, nieuwste eerst, met live `phase/segment/total/depth` uit `get_job` gemengd |
| `GET /api/meetings/{id}` | detail incl. live jobvelden; passive poll (`_PASSIVE_PATTERNS` regex `^/api/meetings/[^/]+$`, alleen GET) |
| `GET /api/meetings/{id}/audio` | `FileResponse` (webm), eigenaarcheck, pad via `resolve_meeting_audio_path` (naam-regex `^[0-9a-f-]{36}\.webm$`, geen traversal) |
| `DELETE /api/meetings/{id}` | verwijdert rij + audiobestand; 409 als job loopt; Document blijft |

Geen timeout-exemptie nodig: chunk-uploads zijn klein, `finish` is asynchroon.

### Janitor

`cleanup_orphaned_meeting_audio(db_session_factory, *, max_age_seconds=3600) -> (n, bytes)`:
verwijdert `.meetingjob-*`-werkdirs en `<uuid>.webm` zonder `Meeting`-rij, beide ouder dan
`max_age_seconds`. Uurlijkse loop in `app.py`, exact het podcast-patroon (eerste run na 300 s).

### Frontend (`static/js/meetings.js`)

ES-module, geladen vanuit `index.html`; export `openPanel/closePanel/togglePanel/isPanelOpen` +
pure helpers `formatElapsed(ms)`, `meetingStatusLabel(m)`, `createChunkUploader({post, maxAttempts,
delays})` (node-getest). Paneel-DOM gebouwd zoals `notes.js` (`.notes-pane`-klassen hergebruikt
via `class="notes-pane meetings-pane"`, `Modals.register('meetings-panel', {railBtnId:'rail-meetings',
sidebarBtnId:'tool-meetings-btn', …})`). `app.js`: `'rail-meetings': 'tool-meetings-btn'` in
`_railToolMap`, `'rail-meetings': 'Meetings'` in de tooltip-map, click-handler op de sidebar-knop.
`index.html`: rail-knop (SVG microfoon) alfabetisch tussen Library en Memory ("Meetings"),
sidebar `list-item#tool-meetings-btn`. Alleen bestaande CSS-variabelen/klassen; de rode
opname-indicator gebruikt `--red`.

### STT-uitbreiding (`services/stt/stt_service.py`)

`transcribe(audio_bytes, *, prompt: str | None = None, timeout: float = 60.0,
filename: str = "audio.webm")` — backwards compatible. API-pad: `files={"file": (filename, …,
mime uit extensie)}`, `data["prompt"]` als gegeven, `httpx.post(..., timeout=timeout)`. Lokaal
pad: `NamedTemporaryFile(suffix=Path(filename).suffix)`, `initial_prompt=prompt`.

## Foutafhandeling

- Mic-permissie geweigerd → toast, meeting-rij blijft `recording` zonder audio; Delete opruimen.
- Netwerk weg tijdens opname → retries; blijvend → rode teller, opname loopt door (lokaal
  buffer in de wachtrij), `finish` na herstel stuurt de rest. Tabsluiting → waarschuwing.
- Herstart tijdens verwerking → rij `processing` zonder job → UI toont fout + Reprocess.
- ffmpeg ontbreekt (native dev) → `RuntimeError("ffmpeg niet gevonden")` → job-error.
- Per-segment STT-fout → job-error met reden; correctie-LLM-fout → ruwe tekst; sjabloon
  ongeldig → één retry, dan opslaan met de beste versie.

## Tests

- `tests/test_meeting_minutes.py`: `condense_transcript` (kort → 1 call eindprompt; lang →
  head/tail met groeiende split, carry-doorgifte, depth-callback; cap 70000), `validate_minutes`,
  `build_minutes`-retry, `render_minutes_document`, `build_stt_prompt`, `split_audio` met
  gemockte `subprocess.run`, jobrunner met fake STT/LLM/ffmpeg (fasen, error-propagatie,
  Document-aanmaak, werkdir-cleanup), `cleanup_orphaned_meeting_audio`.
- `tests/test_routes_meetings.py`: alle routes (owner-isolatie, seq-409, limieten-413,
  finish-zonder-audio-400, reprocess, delete-409-tijdens-job, audio-serve + traversal).
- `tests/test_interactive_gate_meetings.py` (of uitbreiding bestaande): GET detail passive, POST niet.
- `tests/test_stt_transcribe_kwargs.py`: prompt/timeout/filename bereiken httpx resp. faster-whisper.
- `tests/test_meetings_js.py`: node-tests voor `formatElapsed`, `meetingStatusLabel`,
  `createChunkUploader` (volgorde, retry, failedChunks, drain).
- Smoke op :7001 (desktop + 360 px) met echte OpenAI-STT: korte opname → notulen-Document.

## Docs

Sessielog `docs/sessions/2026-09-04-meeting-recorder.md`; CLAUDE.md krijgt één regel in de
architectuurlijst (`src/meeting_minutes.py`, `routes/meeting_routes.py`, `static/js/meetings.js`).
