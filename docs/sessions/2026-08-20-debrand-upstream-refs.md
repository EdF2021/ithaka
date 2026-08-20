# Sessielog 2026-08-20 — upstream-verwijzingen opruimen

**Branch:** `chore/debrand-upstream-refs` · **PR:** #25 · **Issue:** #26

## Aanleiding

De repo is publiek gemaakt. Ithaka is al sinds de hernoeming van juli losgekoppeld van het
bovenliggende project (geen `upstream` remote, `isFork: false`), maar er stonden nog
verwijzingen in de codebase. Op een private repo waren die onzichtbaar; publiek zijn ze dat
niet meer — en twee ervan gingen bij élke LLM-call de deur uit.

## Wat er is aangepast

**Runtime-URL's** (de belangrijkste categorie — deze verlaten het proces):

- `src/endpoint_resolver.py`, `src/llm_core.py` — de `HTTP-Referer`-header die bij elke
  OpenRouter-call meegaat wees naar de upstream-repo. Nu `https://github.com/EdF2021/ithaka`.
- `package.json` — `repository.url`.
- `static/js/cookbook.js` — de "Don't see a model? Request it →"-link. Wijst nu naar
  `EdF2021/ithaka/issues`; **niet** naar `/discussions`, want discussions staan uit op deze
  repo en de oude discussions-URL zou een dode link zijn.

**Documentatie:**

- `CLAUDE.md` — de fork-omschrijving herschreven. Stond ook nog "private" in, wat sinds
  vanochtend niet meer klopt.
- `specs/architecture-runtime-inventory.md` — parent-issue-regel uit de header weg.
- `tests/test_sanitize_preserves_reasoning.py` — upstream-issuelink uit de docstring.

**Dode `#NNNN`-issuereferenties.** Bare vier-cijferige issuenummers in markdown, pointers naar
de tracker van vóór de ontkoppeling. Steekproef via `gh api repos/EdF2021/ithaka/issues/<N>`:
allemaal 404. Ze renderen als inerte, misleidende tekst. Verwijderd uit `THREAT_MODEL.md`,
`specs/architecture-runtime-inventory.md`, `tests/LAYOUT_INVENTORY.md`,
`tests/OVERSIZED_TEST_SPLIT_PLAN.md` en `tests/TESTING_STANDARD.md` — met de omliggende zin
intact gelaten, zodat een securityclaim in `THREAT_MODEL.md` niet stilletjes verdampt.

**Twee losse vondsten uit de verificatiesweep:**

- `routes/email_routes.py` — een docstring gebruikte een echt bestaand persoonsdomein als
  voorbeeldadres. Nu `friend@example.com`.
- `static/js/slashCommands.js:5271` — **kapot artefact van de hernoeming van juli.** Een
  blinde find/replace had het Homerus-citaat "I am Odysseus, son of Laertes" veranderd in
  "I am Ithaka, son of Laertes". Ithaka is het eiland, niet de man; het citaat was daarmee
  feitelijk onjuist. Vervangen door een ander, ongeschonden Odyssee-citaat dat de naam niet
  noemt (Odyssee IX) — thematisch bovendien beter passend.

## Wat bewust bleef staan

- **`ACKNOWLEDGMENTS.md`, sectie "Fork origin"** — dit is de AGPL-3.0-attributie van het
  bovenliggende werk. De git-historie telt 558 commits van de oorspronkelijke auteur en
  honderden van andere upstream-contributors, tegen 73 van EdF2021; er bestaan géén
  per-bestand copyright-headers, dus dit is de enige prozaïsche bronvermelding in de repo.
  Weghalen is een licentierisico, geen brandingkeuze.
- **`docs/sessions/2026-07-16-fork-hernoem-tailscale.md`** — historisch log dat de hernoeming
  zélf documenteert, inclusief de nog bestaande backup-volumenamen. Herschrijven zou het log
  feitelijk onjuist maken; dat is precies de fout die hierboven bij het Homerus-citaat is
  rechtgezet.
- **`ody_`-tokenprefix** (`app.py`, `companion/`, `src/auth_helpers.py`) — bewust behouden
  voor bestaande tokens, zoals `CLAUDE.md` al vastlegde. Wijzigen breekt live auth.
- **Het `/odyssey`-slashcommando** — citeert Homerus' *Odyssee*, geen productnaam. Een echte,
  werkende feature; buiten scope om te slopen.
- **`services/search/providers.py`** — "Odyssey → Honda Japan" als
  zoekdisambiguatie-voorbeeld, en `tests/test_memory_owner_isolation.py` dat "Odyssey" als
  generieke testcodenaam gebruikt. Beide toevallige treffers.

## Verificatie

- `git remote -v` → alleen `origin → EdF2021/ithaka`; `gh repo view` → `isFork: false`.
- `grep -rn -i 'odysseus\|pewdiepie-archdaemon' --exclude-dir=.git .` → alleen de twee
  bewust behouden bestanden.
- gitleaks over de volledige historie vóór het publiek maken: 1971 commits, geen leaks.
- `node --check` op de gewijzigde JS; `py_compile` op de gewijzigde Python;
  `pytest -k "llm_core or endpoint_resolver"` → 224 passed;
  `pytest tests/test_sanitize_preserves_reasoning.py` → 3 passed.
- Browsersmoke op :7001 — Cookbook → What Fits?: de modellenlijst laadt en de footerlink
  rendert met ongewijzigde styling naar `https://github.com/EdF2021/ithaka/issues`.

## Les

De hernoeming van juli deed een find/replace over de hele boom en liet daar een feitelijk
onjuist Homerus-citaat achter dat een maand lang onopgemerkt in de UI stond. Een de-branding is
geen zoek-en-vervang: elke treffer heeft een reden om er te staan, en die reden bepaalt of je
hem schrapt, herschrijft of met rust laat.

Ed de Feber, in nauwe samenwerking met Claude
