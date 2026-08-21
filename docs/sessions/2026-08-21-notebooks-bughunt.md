# Sessielog 2026-08-21 — bug-hunt over het notebooks-oppervlak

**Branch:** `fix/notebooks-switch-and-validation` · **Basis:** `49488af`

## Aanleiding

De backlog was leeg: geen open issues, geen open PR's, `dev` groen. Het notebooks-oppervlak
(~4600 regels over `routes/notebook_routes.py`, `src/notebook_*.py`, `static/js/notebook*.js`)
landde in twee dagen over drie PR's (#21, #23, #24) en is dus snel gereviewd. Drie parallelle
reviewers met verschillende lenzen — HTTP-autorisatie/validatie, frontend-state/lifecycle,
async/resources — zijn erop losgelaten. Elke bevinding is daarna zelf nagetrokken vóór er iets
aan veranderd is; niet-reproduceerbare vermoedens zijn verworpen.

Vooraf: volledige suite groen op `49488af` (4973 passed, 3 skipped) — de vijf pre-existing
failures uit issue #17 zijn inderdaad weg.

## Bevinding 1 — podcast-poll volgt de gebruiker mee naar het volgende notebook

`static/js/notebookWorkspace.js`

De studio-panelen worden **één keer** gewired (`_wireStudioPanel`, dataset-guard) en zijn dus
een singleton die over notebooks heen gedeeld wordt. `_stopPodcastPoll` hing alleen aan
`closeNotebookWorkspace`'s close-hook. De notebooks-picker wisselt echter van werkruimte door
`openNotebookWorkspace` opnieuw aan te roepen — nooit `close()` — dus die hook vuurde niet.

De comments op regel 1144 en 1198 noemen "notebook switched" expliciet als een geval dat de
poll hoort te stoppen. De intentie stond er wél; alleen het pad ontbrak.

Gemeten in de browser tegen een gemockte job (baseline = fix gestasht, harde reload):

| | zonder fix | met fix |
|---|---|---|
| notebook op scherm | Notebook B | Notebook B |
| podcast-knop | **disabled** | enabled |
| Files-lijst | **"Podcast — Generating audio… 4/20"** (A's job) | "Nothing generated yet" |
| extra polls naar A na de switch (5s) | **2** | 0 |

De fix stopt de poll alléén bij een échte id-wissel. Hetzelfde notebook heropenen laat een
lopende job intact — ook geverifieerd: knop blijft disabled, polls blijven vuren, de
voortgangsrij blijft staan.

## Bevinding 2 — statusmeldingen landen in het verkeerde notebook

`_deleteSource`, `_uploadSources`, `_deleteArtifact` en `_generateArtifact` schreven na hun
`await` ongeconditioneerd in de gedeelde foutregel/zone-tekst, terwijl hun directe buren
`_loadSources`/`_loadArtifacts` daar al een `_openEpoch`-guard voor hadden. Dit is dezelfde
bugklasse als de issue #22-fix in `sessions.js`: state vastgelegd op moment A, gebruikt op
moment B.

**Valkuil die dit bijna in de andere richting misging:** de eerste versie van de fix guardde óók
de reset van de upload-zone en de generate-knop. Fout — die controls zitten in het
wired-once-skelet en worden nooit opnieuw gerenderd bij een open, dus een geguarde reset laat
"Uploading…" en een disabled "Generating…"-knop achter in het volgende notebook. De regel die
eruit volgt en nu als test vastligt: **guard het rapporteren van een resultaat, nooit het
terugzetten naar idle.**

## Bevinding 3 — drie 500's op gewone slechte input

`routes/notebook_routes.py`. Alle drie geverifieerd met een TestClient én daarna live op de
smoke-instance:

- `POST /artifacts` met `{"kind": [1,2,3]}` — `ARTIFACT_KINDS` is een dict, dus de
  membership-test gooide `TypeError: unhashable type`.
- `POST /api/notebooks` met `{"name": 123}` — `AttributeError` op `.strip()`.
- `POST`/`PATCH` met kapotte JSON of een niet-string `description` — `JSONDecodeError`
  respectievelijk `sqlite3.ProgrammingError` bij commit.

De zusterendpoints in hetzelfde bestand (`create_artifact`'s body-parsing, `rename_artifact`)
deden het al goed; `create_notebook`/`update_notebook` waren er nooit langs geweest. Alle drie
nu 400, met de isinstance-posture van `rename_artifact` als voorbeeld.

## Bevinding 4 — WAV-schrijfwerk blokkeerde de event loop

`src/notebook_audio.py`. De podcastlus offloadt de TTS-call expliciet met `asyncio.to_thread`
en het commentaar erboven zegt waarom: *"off the event loop it goes, or the whole app stalls
for the job's duration."* De regel eronder deed vervolgens `writer.add_segment(...)` inline —
megabytes WAV per segment, tientallen segmenten per job, allemaal synchroon op de loop. Ook
`writer.close()` (die de WAV-header herschrijft) stond op de loop. Beide nu in een thread, met
`close()` nadrukkelijk binnen `finally` zodat een gefaald segment de handle nog steeds vrijgeeft.

## Verworpen na eigen controle

De reviewers trokken meer na dan ze rapporteerden; dit is expliciet gecontroleerd en in orde
bevonden: owner-scoping op élke geneste resource, padconstructie voor audio (uitsluitend via
`NOTEBOOK_AUDIO_DIR` + commonpath-guard), XSS op LLM-output (nh3-allowlist in `visual_report`,
`html.escape` in `notebook_infographic`), `notebook_id` dat nooit leeg naar de RAG-laag gaat,
de `workload="foreground"`-override op de scriptcall (nodig, want de browser-heartbeat houdt
`has_foreground_activity()` anders continu waar), en de opruimpaden van `delete_notebook` en
de janitor.

## Verificatie

- Volledige suite: **4999 passed, 3 skipped** (was 4973 — 26 nieuwe tests).
- Elke fixgroep heeft een bijtende baseline: fix gestasht → 13 / 2 / 2 failures respectievelijk.
- Browsersmoke op :7001, desktop 1280px en 360 px mobiel: switch, heropenen, studio-paneel,
  geen horizontale overflow. Console schoon — de resterende 404's zijn routine
  `research/status`- en `chat/stream_status`-polls op een verse sessie plus
  `/static/favicon.ico`, die identiek 404't op :7000 en dus pre-existing is.

## Les

De vier bevindingen zijn drie keer dezelfde vorm: een gedeeld, persistent object (studio-paneel,
foutregel, event loop) dat door code wordt gebruikt alsof het van één notebook of één request
is. De wired-once-optimalisatie die de panelen snel houdt, is precies wat maakt dat state
tussen notebooks lekt — en de guard die dat repareert moet selectief zijn, want hem te breed
toepassen strandt de UI in een niet-idle staat.

Ed de Feber, in nauwe samenwerking met Claude
