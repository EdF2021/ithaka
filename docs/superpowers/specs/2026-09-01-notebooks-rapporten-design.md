# Notebooks — "Rapporten": configureerbaar rapport-artifact met AI-aanbevolen indelingen

*2026-09-01 — ontwerp op basis van Ed's referentiescreenshot (NotebookLM "Rapport maken"-modal).*

## Doel

Een nieuwe tegel "Rapporten" naast de bestaande Studio-tegels (Audio, Diapresentatie, Video,
Mindmap, Briefing, Flashcards, Quiz, Infographic, Gegevenstabel, Studiegids, FAQ). Klikken opent
een modal "Rapport maken" met twee secties:

- **Indeling** — vier vaste kaarten: *Zelf rapport maken* (vrije tekstinstructie) en drie
  ingebouwde sjablonen (*Overzichtsdocument*, *Studiemateriaal*, *Blogpost*), elk met een
  potlood-icoon dat aangeeft dat de onderliggende instructie bewerkbaar is vóór generatie.
- **Aanbevolen indeling** — vier AI-gegenereerde suggesties, specifiek afgestemd op de
  daadwerkelijke bron-inhoud van dát notebook (titel + korte omschrijving + bewerkbare
  instructie), met dezelfde potlood-interactie.

Het resultaat is een gewoon markdown-`Document` + `NotebookArtifact`-rij (kind `report`),
getoond via de bestaande viewer/report-pagina — geen nieuw renderformaat.

## Niet-doelen (fase 2)

- Inline bronverwijzingen in het rapport (`[n, ¶N]`, zoals notebook-chat) — expliciet uitgesteld
  zodat het niet half wordt meegebouwd.
- Gestructureerde configuratievelden voor "Zelf rapport maken" (lengte/toon-dropdowns e.d.) —
  v1 is één vrij tekstveld.
- Delen/exporteren buiten wat de bestaande artifact-viewer al biedt.

## Naamgeving — let op bestaande collision

`src/notebook_report.py` bestaat al en doet iets anders: het is de adapter die een gegenereerd
artifact door de gedeelde `visual_report.py`-pipeline rendert (de "Open Visual Report"-pagina met
hero/TOC/typografie). Dat blijft ongewijzigd. De nieuwe module voor dit ontwerp heet
**`src/notebook_report_layouts.py`** om verwarring te voorkomen.

## Architectuur — kernbeslissing

`ARTIFACT_KINDS` in `src/notebook_artifacts.py` is een vaste dict (`kind -> {label, prompt}`),
gevuld uit `_KIND_INSTRUCTIONS` op importtijd. Rapporten hebben per-aanroep variabele instructies
nodig (sjabloon, AI-suggestie of vrije tekst) — dat past niet in een statische registry.

**Gekozen aanpak:** één nieuwe kind `"report"` in `ARTIFACT_KINDS` met een generieke
basisprompt (systeemrol), plus een nieuwe parameter `layout_instruction: str | None` op
`generate_artifact()`, los van het bestaande `focus`-argument. `focus` wordt vandaag alleen
gebruikt voor mindmap-generatie en de `focus_instruction`-string is hardcoded op mindmap-taal
("focus de mindmap op...", "behoud het mermaid-mindmap formaat") — die niet hergebruiken/generiek
maken, gewoon een parallel argument toevoegen zodat mindmap-gedrag ongewijzigd blijft.

**Verworpen alternatief:** een volledig losse generator/opslag-module voor rapporten. Zou
Document-aanmaak, event-firing, de `_VALIDATION_ATTEMPTS`-retry-loop en de
timeout-exemption-logica dupliceren die `generate_artifact` al correct doet.

## Backend

### `src/notebook_report_layouts.py` (nieuw)

- **Vaste sjablonen** (module-constante, geen LLM-call): `Zelf rapport maken` (client-side,
  geen instructie — leeg tekstveld), `Overzichtsdocument`, `Studiemateriaal`, `Blogpost`. Elk
  `{key, title, description, instruction}`, Nederlandse teksten conform het screenshot.
- **`async def get_recommended_layouts(notebook, db_session, owner) -> list[dict]`** — bouwt de
  brontekst via het bestaande `gather_source_text`, stuurt een LLM-call die vraagt om **exact 4**
  rapportvormen die aantoonbaar bij de inhoud van déze bronnen passen (dezelfde
  "geen onderwerpen verzinnen die niet in de bronnen voorkomen"-regel als de andere
  generatieprompts). Output: één JSON-codefence met een array van
  `{title, description, instruction}` (zelfde "exact één json-codefence"-conventie als
  `slide_deck` in `notebook_artifacts.py`). Retourneert `[]` als er geen bronnen zijn (geen
  foutmelding — de modal toont dan alleen de vaste sjablonen).
- **Caching** — fingerprint over de geïndexeerde bronnen (zelfde `hashlib.sha256` over
  gesorteerde `(filename, text)`-tuples als `_fingerprint_entries` in
  `services/memory/memory_extractor.py`, hier als kleine lokale helper). Bij ongewijzigde
  fingerprint: teruggeven uit `Notebook.report_layouts_json` zonder nieuwe LLM-call.
- **Vertrouwensgrens** — de hardcoded suggestie-instructie in de systeemrol; brontekst via
  `untrusted_context_message` in de gebruikersrol, exact zoals `generate_artifact` het al doet.
- **`DUTCH_OUTPUT_RULE`** verplicht in de suggestie-generatieprompt (titels/omschrijvingen zijn
  zichtbare Nederlandse UI-tekst).

### `src/notebook_artifacts.py` (uitbreiding)

- Nieuwe entry in `_KIND_INSTRUCTIONS['report']`: generieke basisinstructie ("Je maakt een
  rapport op basis van de bronnen en de meegegeven indeling-instructie hieronder. Volg die
  instructie voor structuur, stijl en toon.") + de bestaande `_BASE_RULES` (bronvastheid, geen
  verzinsels) — dezelfde opbouw als de andere kinds.
- Nieuwe entry in `_KIND_LABELS['report'] = 'Rapport'`.
- `generate_artifact(..., focus=None, layout_instruction=None)`: als `layout_instruction` gezet
  is, wordt die — net als `focus` vandaag — aan de **gebruikersrol** toegevoegd (náást de
  bronnen, nooit in de systeemrol), met eigen, niet-mindmap-specifieke bewoording. Alleen
  relevant voor `kind="report"`; voor andere kinds blijft het argument ongebruikt (`None`).

### `src/notebook_report.py` (kleine uitbreiding)

- `ENGLISH_KIND_LABELS['report'] = 'Report'` — anders toont de "Open Visual Report"-pagina voor
  een rapport-artifact geen label.

### Databasemodel (`core/database.py`)

Twee nieuwe kolommen op `Notebook`, via het bestaande migratiepatroon
(`_add_column_if_missing`, zelfde stijl als `notebooks.cover_image`):

```python
_add_column_if_missing('notebooks', 'report_layouts_json', 'TEXT')
_add_column_if_missing('notebooks', 'report_layouts_fingerprint', 'VARCHAR')
```

Geen wijziging aan `NotebookArtifact` nodig — een rapport is gewoon een artifact met
`kind="report"`, net als elk ander tekst-artifact.

### Routes (`routes/notebook_routes.py`)

- **Nieuw:** `GET /api/notebooks/{id}/report-layouts` — retourneert
  `{"templates": [...3 vaste sjablonen...], "recommended": [...tot 4 AI-suggesties...]}`.
  Roept `get_recommended_layouts` aan (cache-aware); geen bronnen → `recommended: []`, geen 400.
- **Uitbreiding:** `POST /api/notebooks/{id}/artifacts` accepteert optioneel
  `layout_instruction: str` in de body (zelfde validatiestijl als het bestaande `focus`-veld:
  moet een string zijn, anders 400; lengte-cap 2000 tekens tegen misbruik), en geeft die door aan
  `generate_artifact`.
- **Timeout-exemption** — `ARTIFACTS_GENERATE_PATH_RE` in `notebook_artifacts.py` dekt al
  `POST /artifacts`, dus rapport-generatie zelf heeft al een exemption. De nieuwe
  `report-layouts`-route heeft die niet automatisch; omdat de suggestie-call alleen 4 korte
  titel/omschrijving/instructie-objecten hoeft te produceren (klein outputbudget, dus doorgaans
  ruim onder de 45s hard-timeout van `app.py`) is een exemption in eerste instantie niet nodig —
  **maar dit wordt tijdens implementatie geverifieerd** (zelfde smoke-aanpak als de bestaande
  artifact-generatie) en zo nodig alsnog toegevoegd, even smal als de bestaande regex (alleen dit
  pad, geen `/api/notebooks`-prefix).

## Frontend (`static/js/notebookWorkspace.js`)

- Nieuwe tegel **"Rapporten"** (Nederlands — bevestigd door Ed, bewuste uitzondering op de
  Engelse-tegellabel-conventie van de overige tegels) met een eigen icoon in `_KIND_ICONS`, naast
  de bestaande `ARTIFACT_KINDS`-tegels maar met een ander klikgedrag: opent de modal in plaats
  van direct te genereren.
- **`KIND_LABELS.report = 'Rapporten'`**, **`_KIND_ICONS.report = ...`** (nieuw monochroom SVG-icoon,
  bestaande stijl) — nodig zodat al-gegenereerde rapport-artifacts correct gelabeld worden in de
  artifact-lijst.
- **Modal "Rapport maken"**: bij openen `GET /report-layouts` ophalen; sectie *Indeling*
  (4 vaste kaarten, "Zelf rapport maken" + 3 sjablonen) direct tonen, sectie
  *Aanbevolen indeling* vullen zodra de call terugkomt (skeleton/loading state ertussen).
- **Kaartklik** (elke kaart behalve "Zelf rapport maken"): opent één gedeelde tussenstap — een
  bewerkbaar tekstveld vooraf gevuld met de kaart se `instruction`, plus een "Genereer"-knop. Dat
  is de betekenis van het potlood-icoon. "Zelf rapport maken" opent hetzelfde veld, leeg.
- **Genereren**: `POST /artifacts` met `kind: "report"` en `layout_instruction` = de (eventueel
  bewerkte) tekst uit het veld. Verder identiek aan de bestaande generate-flow (loading state,
  foutafhandeling, artifact verschijnt in de lijst).
- Mobiel (360px): modal wordt een full-screen overlay, zelfde patroon als bestaande
  studio-modals/dialogen in deze codebase.

## Testen

- **`src/notebook_report_layouts.py`**: fingerprint-cache hit/miss (ongewijzigde bronnen →
  geen nieuwe LLM-call; gewijzigde bronnen → wel), JSON-parsing met retry (mirrort de
  `slide_deck`-extractor-tests), lege-bronnen-pad (`recommended: []`, geen exceptie),
  rolplaatsing (brontekst nooit in systeemrol), `DUTCH_OUTPUT_RULE` aanwezig in de prompt.
- **`generate_artifact` met `kind="report"`**: `layout_instruction` landt in de gebruikersrol
  (niet system), afwezigheid van `layout_instruction` faalt niet (generieke basisprompt volstaat),
  bestaande validatie-retry-mechaniek ongewijzigd voor andere kinds.
- **Route-tests** (`routes/notebook_routes.py`): `GET /report-layouts` — cache-hit/miss,
  onbekend/vreemd notebook → 404, geen bronnen → 200 met lege `recommended`. `POST /artifacts`
  met `kind="report"` — `layout_instruction` doorgevoerd, ontbrekend/verkeerd type → 400 (zelfde
  patroon als het bestaande `focus`-veld), lengte-cap gehandhaafd.
- **Migratie**: bestaande `test_review_regressions.py`-stijl smoke dat de twee nieuwe kolommen
  idempotent worden toegevoegd op een bestaande database (net als de andere
  `_add_column_if_missing`-migraties elders getest worden).
- **UI-smoke** (voor merge, per CLAUDE.md): modal openen, sjabloon-kaart bewerken en genereren,
  "Zelf rapport maken" met vrije tekst genereren, "Aanbevolen indeling" met echte
  notebook-bronnen bekijken — desktop én 360px mobiel, console-check.

## Besluit uit spec-review

Tegellabel is **"Rapporten"** (Nederlands) — bevestigd door Ed, bewuste uitzondering op de
Engelse-tegellabel-conventie van de overige Studio-tegels.
