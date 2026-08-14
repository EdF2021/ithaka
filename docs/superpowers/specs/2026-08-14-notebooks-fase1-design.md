# Notebooks Fase 1 — design

*2026-08-14. Volgt op `docs/notebooklm-gap-analyse.md`. Keuzes van Ed: notebook als eerste-klas concept; chat strikt NotebookLM-stijl (alleen bronnen). Verkenning: drie subagent-rapporten, kernvindplaatsen hieronder als `pad:regel`.*

## Doel

Een **notebook** is een afgebakende bronnenset met een eigen, bron-strikte chat en klikbare
citaties. Fase 1 levert: datamodel + API, de Document↔Chroma-brug (één ingest-pad dat zowel
een viewer-Document als embeddings oplevert), notebook-gebonden strikte chat, citaties die
de document-viewer openen, en een Notebooks-UI. De ingest-bugfixes (.docx-mangel, allowlist,
chunk-size) landen apart via branch `fix/rag-ingest-docx` (loopt al).

**Buiten scope Fase 1:** artifacts (study guide, mindmap — Fase 2), audio (Fase 3),
highlight-op-offset in de viewer (follow-up; v1 opent het document), dashboard-card,
delen tussen gebruikers, hernoemen van bestaande RAG-personal-docs-flows.

## Datamodel

Nieuwe tabellen in `core/database.py` (plain `Base`-subclasses; `init_db()`/`create_all`
maakt ontbrekende tabellen aan — geen migratie nodig, patroon: `Note` regel 1632):

- **`notebooks`**: `id` (UUID str PK), `owner` (str, indexed), `name`, `description`
  (nullable), `archived` (bool default False), + `TimestampMixin`.
- **`notebook_sources`**: `id` (UUID str PK), `notebook_id` (FK notebooks.id, CASCADE),
  `document_id` (FK documents.id, SET NULL, nullable), `filename`, `status`
  (`indexed` | `failed`), `chunk_count` (int), `error` (nullable str), + `TimestampMixin`.

Sessie-binding: kolom **`sessions.notebook_id`** (VARCHAR, nullable) via het bestaande
idempotente migrator-patroon (`_migrate_add_owner_column`, core/database.py:778), en
**mee-serialiseren in `Session.to_dict()`** (core/database.py:161 — serialiseert nu bewust
niet alles; zonder deze regel ziet de frontend het veld nooit).

Kolom boven metadata-seeding (het research-spinoff-alternatief) omdat de binding hard,
query-baar en zichtbaar in de sessielijst moet zijn; de detectie-helper blijft één functie.

## Ingest: de Document↔Chroma-brug

Nieuw endpoint `POST /api/notebooks/{id}/sources` (multipart, meerdere bestanden). Pipeline
per bestand, in `src/notebook_ingest.py`:

1. Allowlist-check (zelfde constante als de fix in `fix/rag-ingest-docx`); afgewezen → per-bestand `failed` in de response, geen 400 voor de hele batch.
2. Parse: PDF via bestaand `extract_pdf_text`-pad; Office/EPUB via `markitdown_runtime.convert_to_markdown` (spiegel `src/document_processor.py`); platte tekst direct.
3. Document-id vooraf minten (UUID), chunken met de default 1000/200 (`VectorRAG._split_into_chunks`, rag_vector.py:609).
4. `rag.add_document` met metadata `{source, filename, type, chunk_id, owner, document_id, notebook_id}` — de twee nieuwe sleutels zijn de brug.
5. Pas ná geslaagde embed de `Document`-row aanmaken met het geminte id (owner, title=bestandsnaam; patroon `src/office_doc.py:45-64`) → viewer werkt gratis, en een embed-fout laat geen wees-Document achter.
6. `NotebookSource`-row met status/chunk_count; parse-fout → status `failed` + `error`, geen chunks, geen crash van de batch.

Verwijderen: source → Chroma-delete op `{notebook_id, document_id}` + row weg; Document-row
blijft (bestaat zelfstandig in de bibliotheek). Notebook-delete → alle chunks met
`notebook_id` weg, rows cascaden, sessies worden losgekoppeld (`notebook_id = NULL` →
gedraagt zich weer als gewone chat).

## Scoped retrieval

`VectorRAG.search(query, k, owner, notebook_id=None)` (rag_vector.py:343): bij
`notebook_id` wordt het filter `{"$and": [{"owner": owner}, {"notebook_id": nb}]}`
(Chroma vereist `$and` voor meerdere condities). Doorgeven via `RAGManager.search`
(rag_manager.py:35) → `ChatProcessor._rag_preface` (chat_processor.py:255). In
notebook-modus `k=8` en resultaten genummerd `[1]..[n]` met `document_id` per bron.

## Strikte notebook-chat

Sessie aanmaken vanuit de notebook-UI: `POST /session` krijgt optioneel `notebook_id`
(session_routes.py:320). Detectie server-side: `sess.notebook_id` (geen frontend-vertrouwen).

In `build_chat_context` (routes/chat_helpers.py:626), naast het bestaande
research-spinoff-blok (regels 705-712), een notebook-blok dat:

- `use_rag` forceert (notebook-scoped; `casual_low_signal`-heuristiek genegeerd), `use_memory=False`, `use_web=False`;
- een **statische** strikte grounding-systemprompt injecteert op het `preset_system_prompt`-punt (chat_processor.py:446-450; statisch = KV-cache-veilig per docstring :429): antwoord uitsluitend uit de meegeleverde bronnen, citeer per claim `[n]`, en bij ontbrekende dekking expliciet zeggen dat de bronnen het niet dekken;
- retrieval-resultaten als user-role `untrusted_context_message` blijft leveren (bestaand gehard patroon, prompt_security.py:8);
- **lege-resultaten-branch**: geen chunks boven de drempel → expliciete instructie "geen relevante bronnen gevonden — zeg dat" injecteren (bestaat vandaag niet; stille terugval op modelkennis is precies wat we uitsluiten).

Tools dicht, twee grendels (verkenning: `chat_mode` is geen garantie — auto-escalatie
chat→agent op intent/web-heuristiek, chat_routes.py:598-716):

1. In chat_routes: notebook-sessie → escalatie overslaan, `chat_mode` blijft `"chat"` (pad zonder tools, :1258).
2. Defense-in-depth in het tool-policy-blok (:940-1016): notebook-sessie → `block_all_tool_calls=True` (patroon `[CMP]`-sessies, :975).

Ody-LoRA-caveat (minimal-prompt-paden droppen de preface, agent_loop.py:4021-4046) raakt
alleen agent-mode en is met grendel 1 uitgesloten.

## Citaties

- `rag_sources`-items krijgen `document_id` en `index` (1-based) erbij; bestaande SSE-event (`chat_routes.py:1058`) en persistentie (`meta_data`) ongewijzigd qua vorm.
- Frontend: in notebook-sessies rendert `chatRenderer` `[n]`-markers in de assistant-tekst als links naar het bijbehorende `document_id`; klik loopt via het bestaande click-delegate (chatRenderer.js:1147-1206) met een nieuwe `cite`-branch → `document.js` `loadDocument(docId)` (:7067). Bestaande "Sources"-`<details>`-box (buildRagSourcesBox, :983) blijft en toont bestandsnaam + snippet per nummer.
- Markers zonder bijbehorende bron (model hallucineert `[9]`) renderen als platte tekst.

## Notebooks-UI

Nieuw `static/js/notebooks.js`, gekopieerd van het `dashboard.js`-patroon (modal, template-
string, `open/close/is*Open`, draggable, Escape/click-outside). Registratie: `_AUTO_WIRE`
(modalManager.js:1405), sidebar-knop + rail-knop (inline monochroom SVG, lucide-stijl
`viewBox 0 0 24 24 stroke=currentColor`, patroon dashboard.js `_ICONS`), `_routeOpen['/notebooks']`
(app.js:1181). Styling: bestaande `--panel/--border/--fg`-variabelen, `.modal`/`.list-item`/
`.dashboard-card`-klassen; Dageraad-verfijning via append-only `:root[data-theme="dageraad"]`-
overrides (patroon style.css:40583). Geen emoji.

Twee views in de modal:

1. **Lijst**: notebook-cards (naam, #bronnen, laatst gebruikt), "Nieuw notebook" (naam + optionele beschrijving), archiveren/verwijderen (verwijderen met confirm).
2. **Detail**: bronnenlijst (bestandsnaam, status, chunk_count, delete), upload-dropzone (patroon rag.js:139-169, maar naar `/api/notebooks/{id}/sources`), knop **"Open chat"** → maakt (of hervat de laatste) notebook-sessie via `POST /session` met `notebook_id` en opent die in de normale chat-UI. In de chat-header een notebook-badge zolang de sessie gebonden is.

## API-overzicht

Nieuw `routes/notebook_routes.py` (factory `setup_notebook_routes(...)`, gewired in app.py
zoals de ~40 bestaande):

- `GET /api/notebooks` · `POST /api/notebooks` · `PATCH /api/notebooks/{id}` (naam/beschrijving/archived) · `DELETE /api/notebooks/{id}`
- `GET /api/notebooks/{id}/sources` · `POST /api/notebooks/{id}/sources` (multipart) · `DELETE /api/notebooks/{id}/sources/{source_id}`

Alles owner-gescoped via de bestaande auth-dependency; cross-owner toegang → 404.

## Fouten & randgevallen

- Parse-fout één bestand → `failed`-source met foutmelding zichtbaar in de UI; rest van de batch gaat door.
- Chroma onbereikbaar bij ingest → 503, geen half-aangemaakte rows (Document-row komt pas ná geslaagde embed, zie pipeline-stap 5).
- Notebook zonder (geslaagde) bronnen → chat antwoordt conform lege-resultaten-branch.
- Verwijderde source → citaties in oude berichten verwijzen naar een Document dat nog bestaat (bewust: Document-rows blijven).

## Testplan

- **`tests/test_services_notebooks.py`** (area_services): ingest-pipeline (docx→markitdown-mock, pdf, txt; Document-row + chunks + metadata-sleutels; failed-status), scoped search (`notebook_id`-filter, cross-notebook-lek = fail), delete-opruiming.
- **`tests/test_routes_notebooks.py`** (area_routes): CRUD + owner-scoping (404 cross-owner), multipart-upload, source-delete.
- **`tests/test_routes_notebook_chat.py`** (area_routes): notebook-sessie → strikte prompt aanwezig, memory/web onderdrukt, tools geblokkeerd (escalatie-input), lege-resultaten-instructie, `rag_sources` met `document_id`+`index`.
- **JS**: `node --check` op gewijzigde bestanden; citatie-linkify unit-testbaar indien er een bestaand js-testpatroon is (anders browser-smoke).
- **Browser-smoke (verplicht vóór merge)**: notebook aanmaken → bron uploaden → strikte chat-vraag (in bron én buiten bron) → citatie-klik opent viewer; desktop + mobiel viewport, screenshots.
