# Notebooks fase 4 — Gemini-pariteit: studio-restyle, nieuwe artifacts, video, webbronnen

*2026-08-22 — ontwerp op basis van Ed's referentiescreenshot (Gemini Notebook) en drie parallelle codeverkenningen. Vervolg op `docs/notebooklm-gap-analyse.md` (fase 1–3, alle gemerged).*

## Doel

De notebook-werkruimte functioneel en structureel gelijktrekken met Google's Gemini Notebook
(NotebookLM), binnen Ithaka's eigen donkere thema. Vier deelprojecten, elk een eigen PR:

| # | Deelproject | Kern |
|---|---|---|
| 4a | Studio-restyle + flashcards + gegevenstabel | Gekleurd tegel-grid in de studio; twee nieuwe tekst-artifacts |
| 4b | Diapresentatie | Slide-JSON-artifact + HTML-viewer met navigatie |
| 4c | Video-overview | Slides → PNG (Pillow) + voice-over (TTS) → mp4 (ffmpeg), async job à la podcast |
| 4d | Webbronnen zoeken | SearXNG-zoek in het bronnenpaneel; resultaat → bron-ingest via URL |

Besluiten van Ed (2026-08-22): diashow-video (geen generatieve video-API), structuur van het
screenshot overnemen maar binnen Ithaka's bestaande thema/CSS-variabelen (geen nieuwe
kleurwaarden, geen lichte Gemini-look), "Opslaan in notitie" buiten scope. Volgorde 4a→4b→4c→4d.

## Niet doen

- Echte generatieve video (Veo/Runway) — bewust afgewezen, ook niet als optionele engine.
- Delen/Analytics-knoppen uit het screenshot — geen meerwaarde voor single-tenant Ithaka.
- "Opslaan in notitie" — door Ed geschrapt.
- Lichte pastel-restyling — botst met themasysteem en repo-regel (geen nieuwe kleurwaarden).

## 4a — Studio-restyle + flashcards + gegevenstabel

**UI.** Het studio-paneel krijgt een 2-koloms tegel-grid (zoals het screenshot): per
artifact-soort één tegel met monochroom SVG-icoon, label en een accentkleur uit de bestaande
themavariabelen. Tegels in vaste volgorde: Audio, Diapresentatie (4b), Video (4c), Mindmap,
Rapport/Briefing, Flashcards, Quiz, Infographic, Gegevenstabel, Studiegids, FAQ. Onder het grid
de bestaande artifact-lijst (gegenereerde items, met play-knop voor audio/video). Mobiel (360px):
grid blijft 2 kolommen binnen de bestaande studio-tab.

**Flashcards.** Nieuw kind `flashcards` in `_KIND_INSTRUCTIONS`: 10–15 kaarten in
FAQ-achtige markdown (per kaart een `### voorzijde` met daaronder de achterzijde als alinea —
zelfde parse-robuustheid als de infographic). Weergave via het bestaande report-endpoint met een
eigen compact template (patroon `src/notebook_infographic.py`): klikbare flip-kaartjes met
inline CSS/JS.

**Gegevenstabel.** Nieuw kind `data_table`: feiten/cijfers uit de bronnen als markdown-tabel,
gerenderd via het bestaande generieke report-pad (`src/notebook_report.py`) — geen eigen
renderer nodig.

*Technische verankering: zie sectie "Verkenningsresultaten" hieronder.*

## 4b — Diapresentatie

Nieuw kind `slide_deck`, maar anders dan de tekst-artifacts: het model levert gestructureerde
JSON (`{title, slides: [{title, bullets[], notes}]}`). Opslag in `NotebookArtifact.content` als
JSON-string. Viewer: HTML-slidenavigatie (vorige/volgende, teller, sprekersnotities toggle)
binnen het bestaande artifact-weergavepatroon. Validatie server-side (schema-check + één retry,
zelfde patroon als de podcast-scriptretry uit PR #32).

## 4c — Video-overview

Async job die `src/notebook_audio.py` spiegelt (jobstore, fasen, poll, janitor):

1. **Script**: hergebruik slide-JSON-generatie uit 4b, uitgebreid met een `narration`-veld per
   slide (voice-overtekst, geen letterlijke bullets).
2. **Beeld**: per slide een PNG via Pillow (1280×720, donkere achtergrond + themakleuren,
   meegeleverd font). Deterministische layout: titel, bullets, voortgangsindicator.
3. **Audio**: per slide TTS (bestaande voice-parameter), WAV.
4. **Compositie**: ffmpeg (nieuw in Dockerfile) — per slide beeld+audio, concat naar mp4
   (h264/aac), duur per slide = audioduur + korte marge.
5. **Serve**: mp4 naast de podcast-audio in de artifact-mediaroute; `<video>`-player in de
   studio; janitor dekt ook video's.

Fallback: geen ffmpeg beschikbaar → nette jobfout ("video vereist ffmpeg in het image"), geen crash.

## 4d — Webbronnen zoeken

Zoekbalk boven de bronnenlijst (zoals screenshot). Flow: query → bestaande SearXNG-service →
resultatenlijst (titel, domein, snippet) in het bronnenpaneel → per resultaat "Toevoegen als
bron" → nieuwe ingest-route "bron via URL": pagina ophalen met de bestaande fetch+`url_safety`-
guards, naar tekst, dan het bestaande Document+Chroma-indexeerpad met `notebook_id`. Statussen
(pending/indexed/failed) hergebruiken.

## Testen & verificatie (alle deelprojecten)

- TDD: elke nieuwe module/route krijgt tests naast de bestaande notebook-testbestanden;
  regressietests voor JSON-schema-validatie (4b/4c) en de ffmpeg-fallback (4c).
- Volledige suite + `node --check` op gewijzigde JS.
- Browser-smoke per PR: desktop én 360px, flows van het deelproject + console-check; output
  zichtbaar in de chat vóór merge (harde gate).
- 4c extra: echte end-to-end videogeneratie in de Docker-stack (rebuild met ffmpeg) en afspelen
  in de browser.

## Verkenningsresultaten (verankering)

**Studio-UI (4a/4b).** Tegels: `_studioPanelSkeleton()` in `static/js/notebookWorkspace.js:1299`;
artifact-soorten dubbel gehardcodeerd (JS `ARTIFACT_KINDS`/`KIND_LABELS` :831-840 én Python
`src/notebook_artifacts.py:197`), gepind door `tests/test_services_notebook_artifacts.py:256`
en `tests/test_notebook_workspace_static.py:268` — nieuwe soorten = 4 plekken bijwerken.
Grid `.notebook-artifact-btns` (`static/style.css:41103`, 2 kolommen in 320px-aside; verticaal
groeien is vrij, labels hebben nog geen overflow-afhandeling). Weergavepatronen: report-tab
(`GET .../artifacts/{id}/report`, dispatch in `routes/notebook_routes.py:469`), documentviewer
(mindmap/mermaid), podcast-panel met `<audio>`. Accentkleuren beschikbaar als bestaande vars:
`--red`, `--green`, `--warn`, `--color-accent`, `--color-brand-blue`, `--color-blind-orange`,
`--accent-warm`, `--color-save-green`.

**Podcast-pipeline als video-template (4c).** Joblifecycle `src/notebook_audio.py`: in-memory
`_active_jobs` (:514), fasen `script`→`tts`→`done`, script-retry (max 3, `script_attempt`
zichtbaar), atomic publish (tempfile + `os.replace` :765-794), janitor
`cleanup_orphaned_audio` (:370, uurlijks via `app.py:1232`). Poll: frontend 2s; statusroute
MOET een eigen regel in `src/interactive_gate.py:85` `_PASSIVE_PATTERNS` krijgen (regressie
PR #28). Media-serve: `/api/notebook-audio/{fn}` met whitelist-regex
(`NOTEBOOK_AUDIO_RE`, :323) + ownership-join + `FileResponse` (Range/206 gratis via Starlette)
→ videoroute spiegelen met `^[a-f0-9]{32}\.mp4$`. DB: `NotebookArtifact` heeft géén
content-kolom — script/transcript gaat als gekoppeld `Document`; nieuwe nullable kolom
`video_path` via het idempotente `_migrate_add_*`-patroon (`core/database.py:780`). TTS:
`synthesize_voice(text, voice)` levert WAV zonder truncatie; caller chunkt op
`MAX_SEGMENT_CHARS=4000`. LLM-calls: `task_llm_call_async(..., wait_for_quiet=False,
workload="foreground")` (deadlock-regel). Pillow is aanwezig maar alleen transitief →
expliciet opnemen in `requirements.txt`; ffmpeg + `fonts-dejavu-core` toevoegen aan het
apt-blok (`Dockerfile:22`); subprocess-patroon: `services/youtube/youtube_handler.py:227`
(`asyncio.create_subprocess_exec` + `wait_for` + `kill` bij timeout).

**Search/ingest (4d).** Zoeken: `comprehensive_web_search(query, return_sources=True)` uit
`services/search/core.py:250` (let op: NIET het byte-divergente duplicaat `src/search/`);
`POST /api/search` bestaat al (`routes/search_routes.py:46`). Pagina ophalen:
`fetch_webpage_content` (`services/search/content.py:481`) — eigen SSRF-guard + 2u schijfcache.
Ingest: `ingest_notebook_file` (`src/notebook_ingest.py:100`) accepteert bytes + bestandsnaam →
URL-bron wordt `<paginatitel>.md` + geëxtraheerde tekst door het ongewijzigde pad
(Document-row + Chroma met `notebook_id`); synchron in de request, dus één URL per
"Toevoegen"-call. `NotebookSource` mist een `url`-kolom → nullable kolom via het
migratiepatroon voor bron-provenance. Frontend-haakpunt: zoekblok naast `#nbws-upload-zone` in
`_sourcesPanelSkeleton()` (`notebookWorkspace.js:589`), verversen via bestaand `_loadSources()`.
