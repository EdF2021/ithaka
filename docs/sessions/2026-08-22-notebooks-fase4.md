# 2026-08-22 — Notebooks fase 4: Gemini-Notebook-pariteit

**Aanleiding.** Ed wil de notebook-werkruimte laten lijken op Gemini Notebook
(NotebookLM) — structuur én functionaliteit, inclusief videogeneratie
(referentiescreenshot in de sessie). Besluiten: diashow-video (geen
generatieve API), layout-structuur overnemen binnen Ithaka's donkere thema,
"Opslaan in notitie" buiten scope. Ontwerp:
`docs/superpowers/specs/2026-08-22-notebooks-fase4-design.md`; umbrella-issue #36.

## Geleverd (4 PR's, alle gesmoked desktop + 360px vóór merge)

- **PR #35 — 4a Studio-tegels + flashcards + gegevenstabel.** Gemini-achtig
  tegel-grid (accentkleur per soort via bestaande themavars + `color-mix`,
  monochrome SVG-iconen, Audio vooraan). Nieuw: `flashcards` (FAQ-achtige
  markdown → flip-kaartjes, eigen template `src/notebook_flashcards.py`) en
  `data_table` (markdown-tabellen via het generieke report-pad).
- **PR #37 — 4b Diapresentatie.** `slide_deck`: slide-JSON in één json-fence,
  strikte validatie in `src/notebook_slides.py`; `generate_artifact` kreeg
  een per-soort validator-seam met max 3 pogingen (fout + afgekeurd antwoord
  terug naar het model, zelfde herstelvorm als PR #32). Standalone viewer
  met navigatie/teller/pijltjes/notities-toggle; mobiele nav-overflow
  gevonden in de smoke en gefixt (wrap + padding).
- **PR #38 — 4c Video-overview.** Async job (`src/notebook_video.py`)
  gespiegeld aan de podcast-pipeline: script (slide-JSON + verplichte
  `narration`, retry) → Pillow-frames 1280×720 → per-slide TTS (hergebruik
  `split_turn`/`concat_wavs_to_file`) → ffmpeg still+audio-segmenten →
  concat-mp4, atomic publish. Nieuw: `video_path`-kolom (migratie),
  `/api/notebook-video/{fn}` (whitelist + ownership, Range gratis),
  passieve statuspoll in `interactive_gate` (les van PR #28), uurlijkse
  janitor, mp4-opruimen bij delete, ffmpeg+fonts-dejavu-core in het
  Dockerfile, pillow expliciet gepind. Frontend: Video-tegel, fasetekst,
  player-panel met Open script/Download. Live geverifieerd: 111s-video
  end-to-end gegenereerd en afgespeeld (seek werkt).
- **PR #39 — 4d Webbronnen.** Zoekbalk in het bronnenpaneel →
  `source-search` (provider-dispatch `searxng_search_results`, geen
  page-fetches) → "Toevoegen" per resultaat → `sources/url`:
  `fetch_webpage_content` (eigen SSRF-guard + cache) → markdown → ongewijzigd
  `ingest_notebook_file`-pad; failed-fetch wordt failed-bronrij; nieuwe
  nullable `url`-kolom op `notebook_sources` als provenance.

## Testgroei

Suite 5042 → **5112** (fast-lane): 15 flashcards/data_table, 15 slides
(incl. retry-seam), 33 video (subagent-geschreven, hermetisch), 16
webbronnen, 3 gate-tests, static-JS-pins.

## Werkwijze & lessen

- Smoke via een geïsoleerde native instance op :7001 met gekopieerde
  `app.db` + `settings.json` + **`.app_key`** (zonder die sleutel zijn de
  versleutelde endpoint-keys onbruikbaar → stille 401's) en een vers
  smoke-account via `POST /api/auth/setup`.
- `pkill -f`/`pgrep -f` op het eigen commandopatroon kilt de eigen shell
  (exit 144); veilig patroon: `kill $(ps -eo pid,cmd | awk '/…uvicorn…/ &&
  !/awk/ {print $1}')`.
- `gh pr merge` vanuit een branch met uncommitted werk faalt lokaal na de
  remote merge ("not possible to fast-forward") — remote state is dan wél
  gemerged; `git fetch && git reset --hard origin/dev` op dev lost het op.
- Repo heeft een verplicht PR-template met bot-check (Summary/Linked
  Issue/Type/Checklist/How to Test) — PR-body's via `--body-file` in dat
  format aanleveren.
- Mobiel: het Notebooks-dialoog opent op mobiel een notebook-chatsessie,
  niet de werkruimte (pre-existing gedrag, buiten fase-4-scope; desktop
  opent wél de werkruimte). Workspace via `openNotebookWorkspace(nb)`
  (notebook-object, niet id).

## Deploy-verificatie productie (:7000, 2026-08-22 avond)

- `docker compose up -d --build`: image met ffmpeg + fonts-dejavu-core
  gebouwd; `docker exec … which ffmpeg` → `/usr/bin/ffmpeg`, 8
  DejaVu-fonts.
- Desktop-browser (tijdelijk account `smoketest-claude`): werkruimte met
  11-tegel-grid, webbron-zoek → 5 SearXNG-resultaten → "Toevoegen" →
  `Ithaka The Poetry Foundation.md` **indexed**; video-generatie
  end-to-end op prod (fasen script→…→compose 5 segmenten) → mp4 89s,
  1280×720, h264+aac (ffprobe), afspelen + seek in `<video>` OK;
  DB-rij `notebook_artifacts.video_path` matcht bestand.
- Mobiel 360×800 (chrome-devtools `emulate`): tab-bar
  Sources/Chat/Studio, tegel-grid 2 koloms, videoplayer binnen
  viewport, zoekbalk + bronnenlijst OK. Console: alleen pre-existing
  polls (404 research/stream_status, 403 cookbook/state voor
  non-admin) — geen fase-4-fouten.
- Remote: tailscale-node online; https://ithaka.tailb21d35.ts.net/login
  → 200 (via `--resolve` wegens bekende WSL-DNS-gotcha).
- Opgeruimd na verificatie: smoke-notebook (incl. mp4 via het
  delete-cleanup-pad) en het tijdelijke account.

## Open / vervolg

- Pre-existing: mobiel notebook-dialoog → chatsessie i.p.v. werkruimte
  (kandidaat voor eigen issue).
- Gemini-features bewust niet gedaan: Delen/Analytics, "Opslaan in
  notitie", generatieve video.

Ed de Feber, in nauwe samenwerking met Claude
