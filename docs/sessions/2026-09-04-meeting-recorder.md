# Sessielog 2026-09-04 — Meeting recorder (vergaderingen opnemen → notulen)

**Doel (Ed):** "Ik wil een recording mogelijkheid om vergaderingen op te kunnen nemen. Zodra de
vergadering klaar is, stop ik de opname en wordt van het audiobestand notulen volgens een strak
sjabloon gemaakt." Ed leverde zijn Notulist-code (`berend/app/pages/6_De_Notulist_Demo.py`):
het recursieve meta-samenvattingsalgoritme is 1-op-1 overgenomen.

**Uitkomst:** feature gebouwd, gereviewd, gesmoked en als PR op `dev` (issue #175, branch
`feat/meeting-recorder`). Spec `docs/superpowers/specs/2026-09-04-meeting-recorder-design.md`,
plan `docs/superpowers/plans/2026-09-04-meeting-recorder.md`.

## Wat er is gebouwd

| Laag | Bestand | Kern |
|---|---|---|
| Client | `static/js/meetings.js` (+ rail/sidebar in `index.html`, `app.js`, `modalManager.js`, `style.css`-selectors) | Paneel in Notes-vorm; `MediaRecorder` 30 s timeslice → sequentiële chunk-uploader (3 pogingen, backoff, "Saved up to mm:ss"); mic vóór rij; `beforeunload`; 3 h auto-stop; Stop → drain → finish → 3 s poll (tolerant voor 5 fouten); Open minutes / Audio / Reprocess / Delete; mobiel 100dvh-sheet met minimize |
| Routes | `routes/meeting_routes.py` | `POST /api/meetings`, `POST …/chunks?seq=N` (per-meeting lock, 409 bij gat, 413 limieten), `POST …/finish`, `GET` lijst/detail (passive), `GET …/audio`, `DELETE` |
| Job | `src/meeting_minutes.py` | ffmpeg-split 10 min ogg/opus → STT ×3 parallel (key terms als prompt) → correctiepas → Ed's recursie (split = min(70000, 5000 + 5000·d/2), convergentie-guard) → sjabloon-LLM + validator + retry → `Document`; janitor slaat lopende jobs over |
| Fundament | `core/database.py` (`Meeting`), `src/constants.py` (`MEETING_AUDIO_DIR`), `src/upload_limits.py`, `services/stt/stt_service.py` (`prompt/timeout/filename`-kwargs) | |

## Werkwijze

Subagent-driven development: spec + plan door de controller; 5 taken door sonnet-implementers
(T1/T2/T5 parallel, daarna T3 → T4), per taak een sonnet-reviewer, twee gebundelde fix-waves,
opus-eindreview over de hele branch (1 C + 4 I, alle gefixt), scoped re-review. Ledger in
`.superpowers/sdd/2026-09-04-meeting-recorder/progress.md` (lokaal).

## Smoke (:7001 native, echte OpenAI-STT `gpt-transcribe`, task-model `gpt-5.4-mini`)

- **API-flow** (TTS-gegenereerde Nederlandse vergadering van ~1 min als webm, in 2 chunks):
  create → chunk 0/1 → `seq=5` → 409 `{"detail":"Onverwacht chunknummer","expected":2}` →
  finish → `splitting → transcribing 0/1 → condensing → done` in ~12 s → Document
  "Notulen – Projectoverleg SamenWijzer – 04-09-2026" met exact het sjabloon: Agenda (letterlijk),
  Samenvatting, Besproken punten, Besluiten, Actiepunten-tabel (Marieke: budgetoverzicht, volgende
  week vrijdag; Ed: nieuwsbrief, eind september), Volgende vergadering (do 11 sept 10:00),
  Bijlage transcript.
- **UI desktop 1280 px:** rail "Meetings" + sidebar-item; paneel rechts gedockt; rij met
  Done/Open minutes/Audio/Reprocess/Delete; "Open minutes" opent het document-paneel met de
  notulen; geen console-fouten.
- **UI browser-opname** (getUserMedia gestubd met een oscillator-stream): Start → timer/rode dot →
  chunk 0 na 30 s (`Saved up to 00:30`) → Stop → finish → job → `Error: Geen spraak herkend in
  de opname` + Reprocess (verwacht voor een toon). Eindchunk was 0 bytes doordat de
  `AudioContext` zonder user-gesture *suspended* is — testartefact, niet de module.
- **UI mobiel 360 px:** sheet 360×780 (100dvh), hamburger verborgen, `body.meetings-view`,
  minimize-knop zichtbaar (globale mobiele regel verbergt hem; inline override) en werkt.
- **Tests:** 173 (T1–T5 + fix 1) → 139 in de fix-2-slice; alle groen.

## Openstaand / voor Ed

- Echte-microfoon-opname (permissieprompt) op prod bevestigen: Start → praten → Stop → notulen.
- Prod-STT staat op `whisper-1`; `gpt-transcribe` is beter voor Nederlands (kan in Settings).
- Later: upload van bestaand audiobestand als tweede ingang op dezelfde pipeline; Word-export.

## Avond-vervolg (na Ed's live-test)

- **Live-test:** eerste run faalde met "Transcriptie mislukt (segment 1/1): endpoint returned
  HTTP 404" — prod `stt_model` stond op `gpt-realtime-whisper` (bestaat alleen in Realtime-sessies).
  Omgezet naar `gpt-transcribe`; daarna "doet het perfect".
- **#178 (gemerged, live 19:42):** notulen openen in de notebook-stijl visual-report-pagina
  (`GET /api/meetings/{id}/minutes` via `src/visual_report.py`); knop "Document" opent de bron.
- **Realtime vs. notulen-model:** Ed wil `gpt-realtime-whisper` voor de Realtime-conversatie en
  `gpt-4o-mini-transcribe` voor notulen, maar er was één STT-veld. Bevinding: de Realtime-sessie
  stuurde helemaal geen `input_audio_transcription` mee (user-transcript-events vuurden nooit).
  Fix: nieuwe globale setting `realtime_transcription_model` (Realtime-kaart, default
  `gpt-realtime-whisper`, leeg = uit) → `audio.input.transcription` in de sessie-config; het
  STT-veld blijft voor voice mode + notulen.
