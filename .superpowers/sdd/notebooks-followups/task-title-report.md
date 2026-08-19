# Taak-rapport: NotebookArtifact titel-kolom + hernoemen

Status: **DONE**

## Wat is gebouwd

1. **Model** (`core/database.py`): `NotebookArtifact.title = Column(String, nullable=True)`
   (regel ~1768), opgenomen in `to_dict()`. Migratie
   `_migrate_add_notebook_artifact_title_column()` (kopie van het
   `audio_path`-migratiepatroon), geregistreerd in `init_db()` direct na de
   audio_path-migratie.
2. **Fallback-contract**: nieuwe helper `_artifact_dict_with_title(artifact,
   document_title)` in `routes/notebook_routes.py` — `artifact.title or
   document_title`. Gebruikt door `list_artifacts` (was inline `d["title"] =
   title`, dat zou de eigen titel altijd hebben overschreven) en door het
   nieuwe PATCH-endpoint. `get_artifact_report` geeft nu
   `document_title=artifact.title or document.title` door aan
   `generate_notebook_artifact_report` — die functie zelf is niet gewijzigd
   (byte-identiek gedrag bij title=NULL, zie
   `test_report_200_contains_artifact_title`).
3. **Bij aanmaak** (`src/notebook_artifacts.py::generate_artifact`): de al
   berekende `document_title` (`f"{notebook.name} — {spec['label']}"`) wordt
   nu ook op `NotebookArtifact.title` gezet, niet alleen op `Document.title`.
   Podcast-aanmaak (`src/notebook_audio.py`) is bewust ongemoeid gelaten —
   niet genoemd in de opdracht; podcast-artifacts blijven title=NULL en
   vallen terug op de documenttitel, precies zoals vóór deze taak.
4. **PATCH-endpoint**: `PATCH /api/notebooks/{notebook_id}/artifacts/{artifact_id}`
   met body `{"title": "..."}`. Owner-scoped via `_get_owned_notebook` +
   artifact-notebook-filter; outerjoin met Document (niet report-endpoint's
   inner join) zodat een artifact met hard-deleted Document nog steeds
   hernoembaar blijft. Validatie: title moet na `.strip()` een niet-lege
   string van ≤200 tekens zijn, anders 400. Onbekend artifact/vreemde
   notebook/vreemde owner → 404. Response: verrijkte `to_dict()`.
5. **UI** (`static/js/notebookWorkspace.js`): rename-knop (inline monochrome
   SVG-potlood, `_RENAME_ICON`, hetzelfde glyph als sessions.js voor
   sessie-hernoemen gebruikt) op elke Files-rij — ook podcast-rijen, die geen
   "open source document"-knop hebben maar wel een titel dragen. Reuse van
   `.notebook-artifact-opendoc` voor de icoon-chrome (geen nieuwe kleur),
   met een aparte `.notebook-artifact-rename`-klasse voor de JS-listener
   (de bestaande `.notebook-artifact-opendoc`-listener is uitgesloten via
   `:not(.notebook-artifact-rename)` zodat beide knoppen niet dubbel
   reageren). Klik → inline `<input class="grow session-rename-input
   notebook-artifact-rename-input">` (hergebruik van sessions.js' eigen
   inline-rename-input, met `.grow` erbij zodat de rij niet naar een tweede
   regel wrapt — zie zelfreview-fix hieronder). Enter = PATCH + volledige
   Files-herlaad; Escape/blur/lege-of-ongewijzigde waarde = annuleren zonder
   netwerkcall. Klikken in de input of op de rename-knop opent nooit het
   rapport (stopPropagation + closest-guard, zelfde patroon als de
   bestaande delete-/opendoc-knoppen).

## Zelfreview-bevindingen (advisor-ronde, vóór commit)

- **CSS-scope check**: geverifieerd dat `.session-rename-input`
  (style.css:6439) een top-level regel is (brace-diepte 0 na de regel), dus
  gewoon van toepassing binnen het workspace-paneel — geen extra CSS nodig.
- **Bug gevonden en gefixt**: de input miste `.grow` (flex:1) die de
  vervangen titel-span wél had. In de wrappende flex-rij
  (`.notebook-artifact-item { flex-wrap: wrap }`) duwde de input (met
  `.session-rename-input`'s `width:100%`) de datum + knoppen naar een tweede
  regel. Fix: `input.className = 'grow session-rename-input
  notebook-artifact-rename-input'`. Bevestigd via browser-smoke (zie
  hieronder) op desktop én 360px mobiel — geen wrap, input past netjes op
  één regel.
- **Bug gevonden en gefixt**: `_startArtifactRename` miste de
  `if (!_state.notebook) return;`-guard die alle andere artifact-acties
  (`_openArtifact`, `_deleteArtifact`, `_openArtifactReport`) wel hebben —
  toegevoegd zodat een gesloten workspace niet een crash in de foutmelding
  geeft.
- Commit-bericht: geen Co-Authored-By/Claude-Session-trailer, conform de
  staande gebruikersinstructie — alleen de vereiste ondertekeningsregel.

## Browser-smoke (desktop + 360px mobiel)

Geverifieerd op een verse, geïsoleerde instance (`ITHAKA_DATA_DIR=/tmp/...`,
poort 7002, dit worktree's code + hoofd-venv), met twee handmatig ingevoegde
NotebookArtifact-rijen (faq + podcast, geen LLM nodig voor deze UI-check):

- Files-sectie toont beide rijen met een Rename-knop (potlood); de
  FAQ-rij toont ook de "open source document"-knop ernaast, de
  podcast-rij niet (geen brondocument) maar wél de rename-knop.
- Klik op Rename (FAQ-rij) → inline input met volle titel
  ("Smoke Notebook — FAQ") geselecteerd, geen rij-wrap.
- Tekst vervangen door "Renamed via Smoke Test" + Enter →
  `PATCH .../artifacts/<id>` → 200, gevolgd door een verse
  `GET .../artifacts` → rij toont meteen de nieuwe titel. Bevestigd
  server-side via een directe `GET /artifacts`-call: title persisteert.
- Escape op de rename-knop van de podcast-rij → input verdwijnt, titel
  ongewijzigd, geen netwerkcall (geen nieuwe PATCH in de request-log).
- 360px mobiel (Studio-tab): zelfde rename-flow, input past op één regel,
  geen overlap met de knoppen.
- Console: geen nieuwe fouten van deze feature (de enige geziene 404's zijn
  pre-existing, ongerelateerde polling-endpoints —
  `/api/research/status/...` en `/api/chat/stream_status/...`).

## Tests

`.venv/bin/python -m pytest tests/test_notebook_workspace_static.py
tests/test_routes_notebook_artifacts.py tests/test_notebook_report.py
tests/test_services_notebook_artifacts.py -q` → 91 passed.
`-k notebook` → 281 passed. Volledige suite → 4945 passed, 3 skipped.
`node --check static/js/notebookWorkspace.js` → OK.
`py_compile app.py routes/*.py src/*.py` → OK.

Nieuwe/uitgebreide tests: PATCH happy-path/strip/leeg/te-lang/onbekend-
artifact/vreemde-owner/vreemde-notebook, list-fallback met eigen titel,
report-title-fallback (artifact.title wint van document.title), model-
roundtrip + to_dict-default, migratietest (+ missing-db-noop), generate_
artifact zet title, en vier JS static tests (rename-knop-plek, click-guard-
volgorde, listener-stopPropagation, Enter/Escape/blur-gedrag inclusief de
`.grow`-klasse-assertie).

## Bekende scope-keuzes (geen defecten)

- Podcast-aanmaak (`src/notebook_audio.py`) niet aangepast — buiten de
  letterlijke scope ("generate_artifact / de artifacts-POST"); podcasts
  blijven op de documenttitel-fallback draaien, precies als vóór deze taak.
- `create_artifact`'s POST-response bevat nu ook een `title`-sleutel
  (via `to_dict()`) die er eerder niet was — geen consument leest 'm
  (`_generateArtifact` in de UI herlaadt via een aparte GET), dus geen
  gedragswijziging.
