# Notebooks-werkruimte (NotebookLM-stijl 3-panelen-layout) — design

*2026-08-19 — goedgekeurde richting: fullscreen-werkruimte binnen de bestaande SPA
(richting A), werkende bronselectie per vraag, vervolgvraag-chips via utility-model.*

## Doel

De notebooks-UX omvormen van een modal (grid → detail, chat via de gewone chatview)
naar één fullscreen-werkruimte naar het voorbeeld van Google NotebookLM:

```
+------------------+--------------------------------+------------------+
| Bronnen          | Gesprek                        | Studio           |
| (bronnenlijst    | (gegronde chat, [n]-citaties,  | (artifacts +     |
|  met checkboxes) |  vervolgvraag-chips)           |  podcast-player) |
+------------------+--------------------------------+------------------+
```

De backend (notebooks, bronnen-ingest, gegronde chat met citaties, 5 tekst-artifacts,
podcast) bestaat; dit is primair frontend-herstructurering plus twee backend-gaatjes:
een `source_ids`-retrievalfilter en een vervolgvragen-endpoint.

## 1. Layout & navigatie

- Nieuw `static/js/notebookWorkspace.js`: fullscreen-overlay over de chat-area
  (zelfde patroon als dashboard/editor), CSS-grid `bronnen | gesprek | studio`
  ≈ `280px | 1fr | 320px`. Buitenste panelen inklapbaar (chevron in paneelkop).
- De sidebar-knop Notebooks opent de notebook-kiezer (de bestaande grid-view,
  ontdaan van de detail-view): notebook kiezen of aanmaken → werkruimte opent.
  De oude detail-view in de modal vervalt; de code verhuist naar de panelen.
- Sluitknop linksboven ("← Terug") sluit de werkruimte en herstelt de gewone chat.
- Dageraad-huisstijl: uitsluitend bestaande CSS-variabelen (`--card`, `--border`,
  `--fg`, `--red`, …) en bestaande button/input/card-klassen; monochrome inline-SVG,
  geen emoji; Fira Code.

## 2. Bronnen-paneel

- Bronnenlijst (bestaand `GET /api/notebooks/{id}/sources`) met per bron: checkbox,
  naam, type-icoon, verwijderknop. Kop: "Alles selecteren"-checkbox + teller.
- Upload/toevoegen via de bestaande ingest-flow (bestand + tekst/URL zoals nu).
- **Selectie-semantiek:** selectie leeft per notebook in localStorage
  (`notebook_source_sel_<id>`); default = alles aangevinkt, nieuwe bron = aangevinkt.
  Elke chat-request stuurt `source_ids: [document_id, …]` mee.
  - Alles aangevinkt → `source_ids` weglaten (gedrag identiek aan nu, geen filter).
  - Deelverzameling → filter actief; teller "3/5 bronnen" bij de chat-input.
  - Niets aangevinkt → verzenden geblokkeerd met melding "Selecteer minstens één
    bron" (nooit stilletjes terugvallen op het hele notebook).
- **Backend:** `source_ids` gaat door de chatlaag (`chat_routes` → `chat_processor`
  → `rag_manager.search` → `rag_vector.search`) en wordt daar een extra
  `document_id ∈ source_ids`-voorwaarde bovenop het bestaande `notebook_id`-filter
  (chunks dragen `document_id` al in hun metadata — zie `remove_notebook`).
  Vreemde/onbekende ids filteren op niets binnen het notebook (fail-closed);
  het notebook-filter blijft altijd van kracht.

## 3. Gesprek-paneel

- **Geen tweede chat-implementatie.** De bestaande chat-DOM (composer, stream,
  `chatRenderer` incl. [n]-citaties, tool-lockdown server-side) wordt in het
  middenpaneel gemonteerd; de notebook-sessie is de actieve sessie zolang de
  werkruimte open is. Primair mechanisme: de chat-area via CSS/DOM in het grid
  opnemen; als reparenting listeners breekt, terugvallen op CSS-positionering
  van de bestaande chat-area binnen het grid (beslissing in implementatiefase,
  regressietest: gewone chat werkt onveranderd na sluiten werkruimte).
- Sessiebeheer: werkruimte-open → meest recente notebook-sessie selecteren of
  aanmaken (bestaande `_openChat`-logica, maar zonder de werkruimte te verlaten).
  Meerdere sessies per notebook: eenvoudige sessie-switcher in de paneelkop (v1:
  dropdown; geen zijbalk).
- **Vervolgvraag-chips:** na elk afgerond assistent-antwoord roept de frontend
  `POST /api/notebooks/{id}/suggest_questions` aan met de laatste Q+A (max ~2k
  tekens). Server: utility-model, `wait_for_quiet=False, workload="foreground"`
  (twee-gates-les uit CLAUDE.md), timeout ~8s server-side, JSON-array van
  exact 3 korte vragen. Frontend toont ze als chips onder het antwoord;
  klik = vraag in de composer (niet auto-versturen). Elke fout of timeout =
  stil geen chips (nooit een foutmelding in de chat). Streaming wordt er nooit
  door geblokkeerd (fire-and-forget na stream-einde).

## 4. Studio-paneel

- Verhuizing van de bestaande detail-view-artifactcode: lijst van artifacts
  (studiegids/briefing/FAQ/quiz/mindmap), genereer-knoppen per soort, podcast-rij
  met player + bestaande passieve statuspoll, archief/verwijderen zoals nu.
- Artifact openen: document-viewer als overlay bóven de werkruimte (werkruimte
  blijft open; het huidige "modal sluiten en viewer openen" vervalt).
- Paneelkop toont notebooknaam + bronnenteller.

## 5. Mobiel (≤700px)

- Zelfde DOM, drie tabs boven (Bronnen | Gesprek | Studio), één paneel zichtbaar;
  Gesprek is default. Geen aparte mobiele code-paden. Tabs volgen het bestaande
  tab-patroon uit de settings-modal.

## 6. Testen & risico's

- **Backend-tests:** `source_ids`-filter (subset, leeg → geen filter-parameter,
  vreemde ids → fail-closed, notebook-grens blijft), suggest_questions
  (gate-seam-test naar het voorbeeld van `tests/test_notebooks_gate_seam.py`,
  JSON-contract, timeout → nette lege respons).
- **Frontend-tests:** statische js-area-tests (node --check + bestaande
  statische-analysepatronen) voor workspace-module en gewijzigde modules.
- **Browser-smoke (verplicht vóór merge):** desktop + 360px — notebook openen,
  bron uploaden, checkbox-filter aantoonbaar (vraag over uitgevinkte bron →
  "bronnen dekken dit niet"), citaties klikbaar, vervolgvraag-chip klikken,
  artifact genereren + openen, podcast-player, werkruimte sluiten → gewone
  chat onbeschadigd.
- **Risico's:** (1) chat-DOM-reparenting — fallback beschreven in §3;
  (2) suggest-call mag stream/gates nooit blokkeren — fire-and-forget +
  server-timeout; (3) regressie op de bestaande modal-flows — de kiezer
  hergebruikt de grid-code, detail-code verhuist i.p.v. dupliceert.

## Buiten scope

Video-overviews, realtime audio-mode, delen/publiek maken van notebooks,
bron-niveau citatie-offsets (bestaande [n]-citaties blijven zoals ze zijn),
drag-and-drop-herordening van bronnen.
