# Notebook-infographic v2: hybride HTML-layout met AI-illustraties

Status: ontwerp goedgekeurd 2026-09-03 (Ed), implementatieplan volgt.

## Doel

De studio-tegel "Infographic" levert nu één compacte poster (titel, 3–5 kerncijfers, 3–4
bullet-secties, één takeaway). Het referentiebeeld is de NotebookLM-infographic: een
landscape-compositie met thematische kolommen, genummerde stappen, icoon-kaarten met korte
bijschriften, een centraal hero-element, een vergelijkingsblok met balken en flat-vector-
illustraties in pastelkleuren. Doel van v2: die visuele rijkdom benaderen zónder de tekst aan
een beeldmodel over te laten — tekst blijft exact, Nederlands en bron-herleidbaar (HTML), de
illustraties komen uit het bestaande beeldgeneratie-pad.

De tegel wordt **vervangen**, niet verdubbeld: kind `infographic` krijgt het nieuwe formaat;
eerder gegenereerde infographics (markdown) blijven renderen via de bestaande parser.

## Wat er al is (hergebruik)

- `src/notebook_artifacts.py`: `generate_artifact()` met prompt per kind, validator-retry-seam
  (`ARTIFACT_VALIDATORS`), `DUTCH_OUTPUT_RULE`, `wait_for_quiet=False, workload="foreground"`.
- `src/notebook_slides.py::extract_slide_deck`: JSON-fence-parser + schema-validatie met
  Nederlandse foutmeldingen die bij een retry aan het model worden teruggevoerd. Zelfde patroon
  voor het infographic-JSON.
- `src/notebook_infographic.py`: markdown-parser, `_TEMPLATE`, `_PALETTE`, `_ICONS`/`_pick_icon`,
  `generate_infographic()`, `validate_infographic_markdown()`. Blijft bestaan voor oude content.
- `src/notebook_covers.py`: async image-job (in-memory `_active_jobs`, `asyncio.create_task`,
  `JOB_TIMEOUT_SECONDS`, reaper, `do_generate_image(content, owner=…)`, kopie van
  `GENERATED_IMAGES_DIR` naar een eigen map, `resolve_*_path` met regex-whitelist). Dit is de
  mal voor de illustratie-job.
- `routes/notebook_routes.py`: `POST /api/notebooks/{id}/artifacts` (sync, retourneert het
  artifact), `GET …/artifacts/{artifact_id}/html` (rendert per kind), cover-image-routes.
- `src/interactive_gate.py::_PASSIVE_PATTERNS` voor poll-endpoints; uurlijkse janitors voor
  audio/video in `app.py`.
- Frontend: `static/js/notebookWorkspace.js` (`ARTIFACT_KINDS`, tegel-labels/iconen,
  `_openArtifact`), viewers zijn force-light (#98).

## Deel A — Inhoudsmodel (JSON)

De LLM levert één JSON-object in een ```json-fence. Schema (validatie in
`src/notebook_infographic.py::extract_infographic`, fouten in het Nederlands, zelfde stijl als
`extract_slide_deck`):

```json
{
  "title": "string, verplicht, ≤ 80 tekens",
  "subtitle": "string, optioneel, ≤ 120 tekens",
  "takeaway": "string, verplicht, één zin",
  "blocks": [ /* 5–8 blokken, zie types */ ]
}
```

Elk blok heeft `id` (slug, uniek), `type`, `heading` (≤ 60 tekens), optioneel `icon`
(sleutel uit `_ICONS`, anders keyword-match via `_pick_icon`) en optioneel
`illustration_prompt` (Engels, ≤ 200 tekens; renderer voegt zelf de stijlsuffix toe).

| type | verplichte velden | begrenzing |
|---|---|---|
| `column` | `subheading`, `children` (2–3 sub-blokken van type `steps`/`icon_card`/`key_numbers`) | max 2 columns per infographic; geen nesting dieper dan 1 |
| `steps` | `items[]` van `{label, text}` | 2–5 stappen, `text` ≤ 120 tekens |
| `icon_card` | `text` | 1–2 zinnen, ≤ 200 tekens |
| `hero` | `text` | precies 1 per infographic, ≤ 240 tekens |
| `comparison` | `rows[]` van `{label, value, ratio}` | 2–4 rijen; `ratio` 0–1; `value` is de letterlijke bronwaarde ("300 bronnen · 500 chats") |
| `key_numbers` | `items[]` van `{number, label}` | 3–5; `label` ≤ 8 woorden |

Validatieregels naast het schema: precies één `hero`; minstens één `column`; `illustration_prompt`
mag geen tekst-in-beeld vragen (validator weigert prompts met "text", "label", "caption",
"words", "letters"); totaal aantal blokken met `illustration_prompt` ≤ 5 (overige worden door de
renderer genegeerd, in documentvolgorde). Bronregels in de prompt: elk cijfer, elke stap en elke
vergelijkingswaarde herleidbaar tot de bronnen, `ratio` alleen als de bronnen een vergelijkbare
grootheid geven (anders `comparison` weglaten), `DUTCH_OUTPUT_RULE`, `illustration_prompt` in het
Engels en zonder merknamen of personen.

Persistentie: `document.current_content` bevat het JSON (zoals slide-decks). Na de
illustratie-job wordt een veld `illustrations: {"<block_id>": "<bestandsnaam>"}` aan het JSON
toegevoegd; ontbreekt het, dan rendert de viewer iconen.

Detectie oud/nieuw in `generate_infographic()`: content die (na strip) met `{` of een
```json-fence begint → v2-renderer; anders de bestaande markdown-parser. Geen migratie.

## Deel B — Illustratie-job (async)

Nieuwe module `src/notebook_illustrations.py`, gemodelleerd op `notebook_covers.py`:

- `start_illustration_job(notebook_id, artifact_id, owner, db_session_factory) -> job_id`.
  Wordt aangeroepen door `create_artifact` direct nadat een `infographic`-artifact is
  opgeslagen, alleen als de admin-setting `image_gen_enabled` waar is; anders geen job en
  het artifact blijft bij iconen. Eén actieve job per `artifact_id` (herstart vervangt niet).
- `_generate`: leest het JSON, neemt de eerste ≤ 5 blokken met `illustration_prompt`, roept per
  blok `do_generate_image("<prompt>, flat vector illustration, pastel palette, soft shapes,
  white background, no text, no letters\n\n1024x1024\nlow", owner=owner)` aan (sequentieel;
  `hero` krijgt `1536x1024`), kopieert het resultaat naar `NOTEBOOK_INFOGRAPHICS_DIR`
  (nieuwe constante in `src/constants.py`, guarded `os.makedirs`), bestandsnaam
  `<artifact_id>-<block_id>-<hex8>.png`. Na elk gelukt beeld wordt het JSON bijgewerkt en
  opgeslagen, zodat een halverwege mislukte job de al gemaakte beelden behoudt.
- Fouten: per blok gelogd en overgeslagen; de job eindigt `done` met `illustrations` voor wat
  gelukt is en `errors: n`. Time-out `JOB_TIMEOUT_SECONDS = 300`. Job-registry in-memory met
  reaper zoals covers; na herstart van de app is een lopende job weg, het artifact blijft
  geldig (iconen of deels illustraties).
- Kosten: max 5 beelden per infographic op kwaliteit `low`; de kwaliteitsinstelling
  `image_quality` van de admin wordt bewust **niet** gevolgd (voorspelbare kosten), wel het
  ingestelde `image_model`.
- Serving: `GET /api/notebook-illustration/{filename}` (whitelist-regex + `resolve_path`,
  zelfde patroon als covers; alleen ingelogd). Janitor `cleanup_orphaned_illustrations()`:
  verwijdert bestanden ouder dan 1 uur waarvan het `artifact_id`-prefix niet meer bestaat;
  uurlijks gewired in `app.py` naast de audio/video-janitors.

Status-endpoint voor de viewer: `GET /api/notebooks/{id}/artifacts/{artifact_id}/illustrations`
→ `{"status": "running"|"done"|"none", "illustrations": {"<block_id>": "/api/notebook-illustration/<fn>"}}`.
`none` als er geen job is of `image_gen_enabled` uit staat. Patroon toegevoegd aan
`_PASSIVE_PATTERNS`.

## Deel C — Renderer en layout

In `src/notebook_infographic.py` een tweede template `_TEMPLATE_V2` + `render_infographic_v2(data,
notebook_name, generated_at, illustrations_url_base)`; `generate_infographic()` kiest op basis
van de content-detectie uit Deel A.

Layout (CSS-grid, force-light, bestaande `--*`-variabelen en `_PALETTE`):

- Desktop (≥ 960 px): titelbalk + ondertitel; daaronder een 3-koloms grid
  `[column] [hero boven, comparison/key_numbers onder] [column]`. Blokken buiten de columns
  (losse `icon_card`/`steps`) vullen de middenkolom onder de hero in documentvolgorde.
- Mobiel (< 960 px, getest op 360 px): één kolom, documentvolgorde, `min-width: 0` op
  alle grid-kinderen, geen horizontale overflow.
- Per blok: illustratie-slot bovenin (`<img>` met `data-block-id`, `loading="lazy"`); zonder
  illustratie een inline SVG-icoon uit `_ICONS` in een gekleurde cirkel (bestaand patroon).
  `steps` als genummerde lijst met verbindingslijn; `comparison` als rijen met een balk
  (`width: ratio*100%`) en de letterlijke `value` ernaast; `key_numbers` als het bestaande
  stat-grid; `hero` als breed paneel met grotere illustratie.
- Takeaway als afsluitende blockquote-balk, zoals nu.
- Viewer-script (inline, geen externe deps): als de pagina met `data-illustrations="pending"`
  is gerenderd, poll het status-endpoint elke 3 s (max 120 s); per binnengekomen `block_id` het
  icoon vervangen door de `<img>` met een korte fade. Bij `done`/time-out stoppen. Geen
  herlaad van de pagina.

Studio-tegel: label blijft "Infographic", icoon blijft; tegelbeschrijving noemt illustraties.
Geen tweede tegel.

## Foutafhandeling

- Ongeldig JSON / schema → `ValueError` met Nederlandse melding → retry via de bestaande seam;
  na de laatste poging wordt de ruwe content opgeslagen en rendert `generate_infographic()`
  de bestaande markdown-fallback-kaart ("kon niet als infographic worden gerenderd").
- Beeldgeneratie uit/niet geconfigureerd → status `none`, viewer toont iconen, geen melding.
- Beeldfout per blok → overslaan, loggen, rest doorgaan. Alle blokken mislukt → `done` met
  lege map; viewer blijft bij iconen.
- Verwijderd notebook/artifact tijdens de job → job stopt bij de eerstvolgende opslag
  (artifact niet gevonden) en markeert `done`; janitor ruimt de bestanden op.

## Tests

- `tests/test_notebook_infographic_v2.py`: schema-validatie (elk type, grenzen, precies één
  hero, tekst-in-beeld-prompt geweigerd, >5 illustration_prompts toegestaan maar afgekapt),
  oud/nieuw-detectie, renderer-snapshots (desktop-markup bevat grid + alle bloktypes; iconen
  zonder illustraties; `<img>` mét), takeaway aanwezig, geen `<script src`.
- `tests/test_notebook_illustrations.py`: job met gemockte `do_generate_image` (alle gelukt;
  één mislukt → rest bewaard; alles mislukt → `done` leeg; `image_gen_enabled=false` → geen
  job), bestandsnaam-whitelist en path-traversal, janitor (weesbestand weg, actief bestand
  blijft, jonger dan 1 uur blijft).
- `tests/test_routes_notebook_infographic.py`: `POST artifacts` kind `infographic` start de
  job (gemockt) en retourneert het artifact; status-endpoint `none`/`running`/`done`;
  `GET …/html` rendert v2 en oud-formaat; serving-route weigert vreemde namen.
- `tests/test_interactive_gate.py`: nieuw poll-pad is passief.
- Regressie: bestaande `test_notebook_infographic*`-tests blijven groen.

Smoke (CLAUDE.md-regel): notebook met echte bron op :7001, infographic genereren, viewer op
desktop en 360 px vóór en ná het binnenkomen van illustraties, en één keer met
`image_gen_enabled=false`; console zonder nieuwe fouten.

## Buiten scope

Bewerken van blokken in de viewer, export naar PNG/PDF, meerdere pagina's, het volgen van
`image_quality`, regeneratie van losse illustraties, en migratie van oude infographics.
