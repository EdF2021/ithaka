# Task: infographic-artifact voor notebooks — report

## Wat er is gebouwd

1. **Kind** (`src/notebook_artifacts.py`): `infographic` toegevoegd aan `_KIND_INSTRUCTIONS`
   (Dutch prompt, forceert exacte structuur: `#` titel, `## Key numbers` met 3-5
   `- **getal** — label`-bullets, 3-4 gewone `## sectie`-koppen, afsluitende
   `>`-blockquote) en `_KIND_LABELS` ("Infographic").
2. **Renderer** (`src/notebook_infographic.py`, nieuw): `generate_infographic(title,
   markdown, notebook_name, generated_at) -> str`. Regex-gebaseerde line-parser
   (`_parse_infographic_markdown`) i.p.v. de markdown→HTML-pipeline van
   `visual_report.py` — geen `nh3`/raw-HTML-passthrough nodig omdat alle content
   via `html.escape` + een eigen `**bold**`→`<strong>`-naconversie gaat (nooit
   raw interpolatie). Zelfstandige HTML, palet/font-stacks gespiegeld van
   `visual_report.py`'s `:root`/dark-media-vars, eigen compacte poster-layout
   (hero, stat-grid, 2-koloms sectiekaarten, takeaway-band, meta-regel). Geen
   `<script>`, geen externe resources.
   - **Fallback bij Dutch/niet-Engelse bronnen**: de generatie-prompt laat het
     model in de brontaal schrijven, dus een Dutch bronset kan "## Kerncijfers"
     i.p.v. "## Key numbers" opleveren. Structurele detectie vangt dit op: als
     de letterlijke "key numbers"-heading niet matcht, wordt de eerste gewone
     sectie waarvan **alle** bullets het strikte stat-bullet-patroon volgen
     alsnog gepromoveerd tot de stat-grid.
   - Graceful fallback op elk niveau: ontbrekende titel/stats/secties/takeaway
     renderen leeg weg i.p.v. te crashen; niet-herkende tekst landt in een
     "Content"-kaart (die full-width spant als het de enige kaart is).
   - Print: `@media print` forceert het lichte palet met `!important`
     (voorkomt licht-op-licht/donker-op-donker als de viewer dark-mode heeft).
3. **Endpoint** (`routes/notebook_routes.py`): report-endpoint branch toegevoegd —
   `kind == "infographic"` → `generate_infographic`, alle andere kinds
   ongewijzigd via `generate_notebook_artifact_report`, podcast blijft 404.
4. **Frontend** (`static/js/notebookWorkspace.js`): `infographic` toegevoegd aan
   `ARTIFACT_KINDS` (7e Generate-knop) en `KIND_LABELS`. Rij-klik heeft geen
   aparte branch nodig (viel al door naar `_openArtifactReport`).
   - **Bewuste afwijking van de brief**: de brief suggereert een uniek
     staafdiagram-icoon voor de knop. De bestaande 6 Generate-knoppen delen
     echter allemaal hetzelfde `_PLUS_ICON` (regel ~824-828: het plus-icoon
     disambigueert *create*-acties van Files-rijen, niet het ene kind van het
     andere). `infographic` toevoegen aan de `ARTIFACT_KINDS`-array levert dus
     automatisch dezelfde plus-icoon-knop op als de andere 5 — dat is "in de
     stijl van de bestaande 6" in de letterlijke zin (identieke opmaak), maar
     niet in de zin van een uniek pictogram. Ik heb voor consistentie met de
     bestaande code gekozen i.p.v. een nieuw uniek icoon te introduceren.
     Controller kan hier op bijsturen als een uniek icoon toch gewenst is.
5. **Labels-bron backend**: "Infographic" toegevoegd aan `ENGLISH_KIND_LABELS`
   in `src/notebook_report.py` (infographic gebruikt dat pad zelf niet, maar
   moet gesynchroniseerd blijven met `_KIND_LABELS`, requirement 5).
6. **Tests**:
   - `tests/test_notebook_infographic.py` (nieuw, 21 tests): kind-registratie,
     parser (volledige structuur, ontbrekende structuur, lege input, Dutch-
     heading-fallback), renderer (stat-kaarten, title-fallback, nooit leeg/
     kapot, HTML-escaping incl. `<script>`/`<img onerror>`, bold→`<strong>`,
     single-card full-width, geen externe resources), route (200 + titel,
     artifact-title-fallback, podcast blijft 404).
   - `tests/test_notebook_report.py`: `test_english_kind_labels_complete`
     aangevuld met `"infographic": "Infographic"`.
   - `tests/test_notebook_workspace_static.py`: 2 nieuwe tests — 7e knop +
     label aanwezig (scoped op `ARTIFACT_KINDS`/`KIND_LABELS`-blokken, geen
     bare substring-check), en een regressietest dat de rij-click-handler
     geen aparte `kind === 'infographic'`-branch heeft (moet gewoon door-
     vallen naar `_openArtifactReport`).
   - `tests/test_services_notebook_artifacts.py`: bestaande
     `test_artifact_kinds_registry_complete` aangevuld met infographic (zou
     anders gefaald hebben op de nieuwe kind).

## Afwijkingen van de brief

- Icoon-keuze (zie punt 4 hierboven) — bewuste keuze voor consistentie met
  bestaande code i.p.v. de gesuggereerde staafdiagram-glyph.
- Route-test staat in het nieuwe `test_notebook_infographic.py` i.p.v.
  toegevoegd aan `test_notebook_report.py` zelf — de brief zegt "in de stijl
  van", niet "in dat bestand", en de afrondingscommando's noemen beide
  bestanden apart.

## Test-run

```
.venv/bin/python -m pytest tests/test_notebook_infographic.py tests/test_notebook_report.py tests/test_notebook_workspace_static.py -q
# 56 passed

.venv/bin/python -m pytest -k notebook -q
# 306 passed, 4667 deselected

.venv/bin/python -m pytest -q   # volledige suite
# 4970 passed, 3 skipped (pre-existing, ongerelateerd)

node --check static/js/notebookWorkspace.js   # OK
python -m py_compile src/notebook_infographic.py   # OK
```

## Zorgen / openstaande punten

- Geen browser-smoke gedraaid (geen UI-surface-wijziging in de zin van CSS/
  layout die niet al door de bestaande knoppen-CSS gedekt wordt — de nieuwe
  knop hergebruikt exact dezelfde `.dashboard-action-btn`/`_PLUS_ICON`-opmaak
  als de andere 6, en de infographic-HTML zelf is een losstaande pagina die
  buiten de app-UI-conventies valt maar wel het `visual_report`-palet
  hergebruikt zoals gevraagd). Als de controller een visuele check wil, is
  dat een aparte stap.
- De Dutch-heading-fallback promoveert de eerste sectie waarvan *alle*
  bullets het strikte stat-patroon volgen. Bij een edge-case waarin een
  gewone sectie toevallig ook volledig uit `**x** — y`-bullets bestaat, kan
  die per ongeluk als stat-grid gerenderd worden i.p.v. de echte
  key-numbers-sectie verderop. Acceptabel risico: de renderer produceert
  hoe dan ook een correcte, niet-lege pagina, en dit scenario vereist dat
  de LLM zowel de Engelse kop mist als toevallig een niet-key-numbers-sectie
  in exact dat bullet-formaat schrijft.
