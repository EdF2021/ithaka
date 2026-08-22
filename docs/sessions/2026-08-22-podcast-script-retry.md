# Sessielog 2026-08-22 — podcastgeneratie werkte niet meer

**Branch:** `fix/podcast-script-format-retry` · **Basis:** `5acae0c`

## Melding

Ed: "de podcast generatie werkt niet meer."

## Diagnose — eerst uitsluiten, niet gokken

De voor de hand liggende verdachte was mijn `asyncio.to_thread`-wijziging aan de writer-lus van
gisteren (#28). Die is als eerste **uitgesloten** met een end-to-end repro (`_generate` met fakes
voor LLM + TTS): de pipeline draaide schoon door en leverde een geldige 24000 Hz WAV. Niet de
oorzaak.

Daarna de keten van achteren naar voren:

1. **TTS** — `TTSService.synthesize_voice` live in de container: 43 KB RIFF terug. Gezond.
2. **Synthesizer-wiring** — `set_synthesizer` wordt wél aangeroepen (`notebook_routes.py:107`
   vanuit `app.py:720` met `tts_service=tts_service`). Mijn eerste grep suggereerde het
   tegendeel, maar dat was een afgekapte `| head`; de wiring is intact.
3. **Scriptfase** — hier brak het. Tegen Eds echte notebook `12a4d99…`:

   ```
   RuntimeError: Het model leverde geen bruikbaar dialoogscript op
                 (geen enkele regel begint met S1: of S2:)
   script len: 4277
   script head: 'Dit materiaal bevat een schat aan kennis, ideeën en diensten...'
   ```

   Het model levert wél tekst, maar **proza in plaats van `S1:`/`S2:`-dialoog**.

## Root cause

`task_endpoint_id` en `task_model` zijn leeg → de scriptcall valt terug op
`default_model = 'gemma4:latest'`, een zwak lokaal model (geen tools). Dat model:

- werkt prima op korte prompts (`Zeg exact: hallo` → `hallo`) en zelfs op 60k-samenvatting;
- maar produceert bij het **strikte** `S1:`/`S2:`-formaat vaak proza.

Gemeten, intermittent:

| poging | S1/S2-regels | parse |
|---|---|---|
| 1 | 0 (proza) | FAIL |
| 2 | 38 | OK |

In de logs stond bij de faalpoging `[fallback] primary gemma4:latest failed (RuntimeError);
trying next` — een intermittente timeout op de primaire call doet er soms een fallback-model
bovenop dat het formaat óók negeert. De job faalde hard op die ene worp, **zonder enige retry**.

Geen systemisch "gemma4 faalt altijd": het model werkt voor andere calls. Het is format-specifiek
onder grote context.

## Fix

De scriptcall mag tot 3 pogingen doen. Faalt `parse_dialogue`, dan gaat een correctie terug naar
het model — mét het mislukte antwoord erbij — en volgt een nieuwe poging. Een goede eerste poging
kost nog steeds precies één call; drie missers op rij stoppen de job (elke poging herstuurt de
volledige 60k context, dus geen runaway).

**Zichtbaarheid.** Een retried scriptfase is anders een stil 2-3× langere "Writing script…".
`script_attempt` zit nu in `_PUBLIC_JOB_FIELDS` en de UI toont "Rewriting script… (attempt N)".
Dit volgt Eds voorkeur dat achtergrondprocessen een zichtbare status horen te tonen.

**Reikwijdte gecontroleerd.** Alleen de podcast heeft strikte format-parse die hard faalt. De
andere artifact-soorten (studiegids/briefing/FAQ/quiz in `notebook_artifacts.py`) checken enkel
op een leeg antwoord en renderen markdown, dat proza vergeeft; de mindmap kan hooguit een kapotte
mermaid tonen, geen harde job-fail.

## Verificatie

- **Live op de docker-stack**, echt model + echte TTS: attempt 1 gaf proza
  (`Podcast script attempt 1/3 had no S1:/S2: lines; retrying`), de retry sloeg aan, attempt 2
  gaf 24 turns → **10.785.644 bytes WAV**. Exact het scenario dat gisteren faalde.
- **Retry-gedrag** via een gerichte repro over 0/1/2/3 opeenvolgende missers: herstelt bij ≤2,
  stopt bij 3 op precies 3 calls, geen loop.
- **360 px browser**: de retry-status rendert als "Rewriting script… (attempt 2)" in de
  pending-rij, geen page-overflow.
- **Suite: 5010 passed, 3 skipped.** Zeven nieuwe tests drijven de echte `_generate` door de
  writer-lus — de suite testte voorheen alleen `start_podcast_job`-validatie, waardoor deze
  hele lus (en gisteren de `to_thread`-wijziging) ongedekt was. Baseline zonder de fix: bijtende
  failures.

## Les

Het gat dat dit liet ontsnappen: de podcasttests raakten alleen de validatie vóór de job, nooit
de job zelf. Een source-text-assertie op mijn `to_thread`-wijziging van gisteren gaf een vals
gevoel van dekking. Elke wijziging aan een achtergrondjob hoort een test te hebben die de job
écht draait, met fakes voor de externe calls — dan was dit format-gat er eerder uitgekomen.

Ed de Feber, in nauwe samenwerking met Claude
