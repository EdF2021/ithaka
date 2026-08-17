# Notebooks Fase 2 — tekst-artifacts: design

*2026-08-16. Volgt op Fase 1 (gemerged, PR #2) en `docs/notebooklm-gap-analyse.md` Fase 2-advies:
study guide, briefing, FAQ, quiz, mermaid-mindmap — "bijna gratis" via bestaande onderdelen.
Verkenning: twee subagent-rapporten; vindplaatsen hieronder als `pad:regel` (dev-head 04edca7).*

## Doel

Vanuit de notebook-detailweergave genereert de gebruiker met één klik een **artifact** uit de
bronnen: study guide, briefing, FAQ, quiz of mindmap. Een artifact is een gewoon
markdown-**Document** (viewer, versies, export gratis) dat aan de notebook gekoppeld blijft en
in de detailweergave gelist wordt.

**Buiten scope:** audio (Fase 3), geplande auto-regeneratie (BUILTIN_ACTIONS —
bevestigd alleen nodig voor scheduling, `src/builtin_actions.py:2734`), interactieve
flashcard-UI met eigen state, artifacts in de Library apart filteren, delen.

## Datamodel

Nieuwe tabel **`notebook_artifacts`** in `core/database.py` (patroon `NotebookSource`,
create_all maakt hem aan):

- `id` (UUID str PK), `notebook_id` (FK notebooks.id, CASCADE), `document_id`
  (FK documents.id, **CASCADE** — een artifact zonder document is betekenisloos; delete in de
  Library ruimt de artifact-row mee op), `kind` (str: `study_guide` | `briefing` | `faq` |
  `quiz` | `mindmap`), + `TimestampMixin`; `to_dict()` levert alle velden + `created_at`.

Geen nieuwe kolommen op `Document` (bewust: geen schema-vervuiling; de koppeltabel is de bron
van waarheid, zoals `notebook_sources`).

## Generatie — `src/notebook_artifacts.py`

`ARTIFACT_KINDS`: dict `kind → {label, title_suffix, prompt}`. Systeem-/promptregels per kind
(alle prompts: "schrijf in de taal van de bronnen", markdown-output, geen preamble):

- **study_guide**: gestructureerde studiegids — kernconcepten, definities, samenvattingen per bron, studievragen.
- **briefing**: zakelijke briefing — kernpunten, context, implicaties, one-pager.
- **faq**: 8–12 vraag/antwoord-paren uit de bronnen.
- **quiz**: 8–10 toetsvragen; per vraag het antwoord in een `<details><summary>Antwoord</summary>…</details>`-blok
  **mits** raw HTML de markdown-renderer overleeft (verifieer `static/js/markdown.js` `mdToHtml`);
  anders een "Antwoorden"-sectie onderaan. De plan-taak bevat deze verificatiestap expliciet.
- **mindmap**: één ```mermaid-fence met `mindmap`-syntax (max ~3 niveaus, labels kort,
  geen speciale tekens die mermaid breken); één regel uitleg eronder. Viewer-Preview rendert
  mermaid al (`document.js:9720-9722` roept `renderMermaid` aan; CDN-load `index.html:215`).

Pipeline `generate_artifact(notebook_id, owner, kind, db_session) -> NotebookArtifact`:

1. Bronnen verzamelen: `NotebookSource` met `status=="indexed"` en `document_id is not None`
   → `Document.current_content` (vol-tekst staat daar, `notebook_ingest.py:148`). Geen bronnen → `ValueError`.
2. **Context-cap**: totaal ≤ `MAX_CONTEXT_CHARS = 60_000`; bij overschrijding per bron
   proportioneel afkappen en een regel "(bron ingekort)" aan het bronblok toevoegen.
   Bronblokken gescheiden met `=== BRON: <filename> ===`-headers.
3. LLM-call: `task_llm_call_async(messages, owner=owner)` (`src/task_endpoint.py:58` — resolvet
   task→utility→default endpoints zonder chat-sessie, workload="background"). Messages:
   één system (kind-prompt) + één user (bronblokken, via bestaand untrusted-wrap-patroon uit
   `src/prompt_security.py` — brontekst is untrusted input).
4. Pas ná geslaagde call: `Document`-row (patroon `notebook_ingest.py:148-154`:
   `id`, `title = "<notebook-naam> — <label>"`, `owner`, `current_content`, `session_id=None`
   → Library-doc) + `fire_event("document_created", owner)` (`src/event_bus.py`, live refresh)
   + `NotebookArtifact`-row. LLM-fout → exception omhoog, geen rows.
5. Regeneratie van hetzelfde kind maakt een **nieuw** artifact (geen overschrijven; de
   gebruiker verwijdert oude zelf).

## API — uitbreiding `routes/notebook_routes.py`

- `GET /api/notebooks/{id}/artifacts` — lijst (nieuwste eerst).
- `POST /api/notebooks/{id}/artifacts` — body `{"kind": ...}`; valideert kind (400), notebook
  owner-gescoped (404), geen indexed bronnen → 400 met duidelijke melding; Chroma is niet
  nodig (vol-tekst uit de DB). Synchroon (LLM-call kan tientallen seconden duren; frontend
  disabled de knop). LLM-/endpointfout → 502 met melding.

  **Amendement (fix-wave, na eind-review):** synchroon blijft de ruling, maar niet zomaar.
  `task_llm_call_async` (`src/task_endpoint.py:58`) wacht standaard op
  `wait_for_interactive_quiet` — bedoeld om echte achtergrondtaken (scheduler, e-mailpollers) te
  laten wachten tot de UI stil is. Deze POST is zelf al een getrackte foreground-request
  (`_InteractiveActivityMiddleware`, `app.py:201-215`): de wait zou dus wachten op het stil
  worden van precies de request die op die wait wacht — een deadlock die nooit opheft
  (`BACKGROUND_TASK_MAX_WAIT_SECONDS` default `0` = oneindig wachten). Fix: `generate_artifact`
  roept `task_llm_call_async(..., wait_for_quiet=False)` aan — de gate is voor achtergrondtaken,
  niet voor een in-request caller die zichzelf blokkeert. Onafhankelijk daarvan viel deze route
  ook buiten de whitelist van `_RequestTimeoutMiddleware` (`app.py:172-184`, hard 45s-timeout),
  dus elke generatie 504'de sowieso. Tweede fix: een smalle, route-specifieke uitzondering
  (regex `^/api/notebooks/[^/]+/artifacts$`, alleen POST — bewust geen brede
  `/api/notebooks`-prefix, want die zou ook upload/ingest vrijstellen) in plaats van deze route
  aan de generieke prefixlijst toe te voegen.
- `DELETE /api/notebooks/{id}/artifacts/{artifact_id}` — verwijdert artifact-row **én**
  Document-row (artifact is gegenereerde inhoud, geen gebruikersdata).

Alles owner-gescoped via de bestaande auth-dependency; cross-owner → 404.

## Notebooks-UI — uitbreiding `static/js/notebooks.js`

In `_showDetail` (`notebooks.js:458-482`), tussen bronnenlijst en upload-zone, een
**Artifacts-sectie**:

- Vijf genereer-knoppen (bestaande buttonklassen; labels: Studiegids, Briefing, FAQ, Quiz,
  Mindmap). Klik → knop disabled + "Genereren…", `POST .../artifacts`, daarna lijst verversen;
  fout → `_showError`-patroon (eigen `#notebook-artifact-error`, `:empty`-CSS).
- Lijst van bestaande artifacts (label, titel, datum): klik → open in viewer via het
  `window.documentModule.loadDocument(docId)`-singleton-patroon (spiegel `_openChat`,
  `notebooks.js:412-456`: disable → try → singleton → `closeNotebooks()` → fallback
  hash+reload); delete-kruisje met `_armConfirm`-inline-bevestiging (geen `window.confirm`).
- Mindmap-artifact: na openen toont de viewer Write-mode; de kaart krijgt een hint
  "(Preview voor de mindmap)" — programmatisch Preview forceren is buiten scope.

CSS: append in het bestaande notebook-blok aan het eind van `static/style.css` (regels
~40837-41046), Dageraad-overrides bij de bestaande `:root[data-theme="dageraad"]`-marker
(~40975); alleen bestaande variabelen/klassen, geen emoji.

## Fouten & randgevallen

- Notebook zonder (geslaagde) bronnen → 400 "Geen geïndexeerde bronnen".
- LLM-endpoint niet geconfigureerd/offline → 502; UI toont de melding, knop komt terug.
- Zeer grote bronnen → context-cap (stap 2), artifact vermeldt ingekorte bronnen.
- Document uit de Library verwijderd → artifact-row cascadet mee (FK CASCADE). Let op: een
  delete vanuit de Library (`DELETE /api/document/{id}`, `routes/document_routes.py:726-751`) is
  een **soft delete** (`is_active = False`; de row blijft bestaan). De FK CASCADE vuurt pas bij
  een echte row-delete, dus een via de Library "verwijderd" artifact blijft in de notebook's
  Artifacts-lijst staan totdat een hard-delete de Document-row daadwerkelijk verwijdert — dat
  gebeurt via Tidy/AI-tidy (junk-detectie, `routes/document_routes.py:911` e.v.) of via de
  notebook's eigen artifact-DELETE/notebook-DELETE routes (die wél hard deleten).
- Notebook-delete → artifacts cascaden mee; hun Documents blijven bestaan? **Nee** — ruling:
  notebook-delete verwijdert ook artifact-Documents (gegenereerde inhoud hoort bij de
  notebook; bron-Documents blijven juist wél, zoals in Fase 1). Implementatie: in de bestaande
  notebook-delete-route de artifact-Documents expliciet mee verwijderen vóór de cascade.

## Testplan

- **`tests/test_services_notebook_artifacts.py`** (area_services): kinds-registry compleet;
  bronverzameling filtert failed/document-loze sources; cap kort proportioneel af; prompt
  bevat bronheaders; Document + artifact-row pas ná geslaagde (gemonkeypatchte)
  `task_llm_call_async`; LLM-fout → geen rows; lege notebook → ValueError.
- **`tests/test_routes_notebook_artifacts.py`** (area_routes): CRUD + owner-scoping (404),
  ongeldig kind → 400, lege notebook → 400, delete verwijdert row + Document,
  notebook-delete ruimt artifact-Documents op. Patroon: `tests/test_routes_notebooks.py`.
- **JS**: `node --check`; browser-smoke (verplicht vóór merge): elk kind genereren op een echte
  notebook, artifact opent in viewer, mindmap rendert in Preview, quiz toont een
  antwoordensectie onderaan (geen `<details>`-blok per vraag — dat zou door `markdown.js` alsnog
  force-opened worden; zie de kind-instructie in `src/notebook_artifacts.py`), delete werkt;
  desktop + mobiel viewport, screenshots.
