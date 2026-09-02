# Embedding-model migratie (issue #124)

Deze runbook hoort bij issue #124: de Docker-stack draaide met een
Engels-only fastembed-model (`sentence-transformers/all-MiniLM-L6-v2`) op
grotendeels Nederlandstalige content, terwijl de code al een bewust
meertalig default kende (`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
in `src/embeddings.py`). `docker-compose.yml` overschreef dat default met de
Engelse fallback (`FASTEMBED_MODEL=${FASTEMBED_MODEL:-sentence-transformers/all-MiniLM-L6-v2}`).

Deze PR fixt dat default (compose + code-hardening voor lege env-waarden,
zie de PR-beschrijving voor het volledige diff). **Het wijzigen van dit
default verandert het embedding-model in productie** — bestaande Chroma-
vectoren staan in de oude 384-dim ruimte van het Engelse model en zijn
semantisch niet compatibel met vectoren uit het nieuwe multilinguale model
(zelfde dimensie, andere ruimte: cosine-afstanden zijn niet vergelijkbaar).
Zonder her-embedden werkt retrieval (RAG, tool-index, semantisch geheugen)
op een gemengde/verouderde index en verslechtert de kwaliteit stil. **Voer
deze migratie uit bij deploy, niet erna.**

`EMBEDDING_URL=${EMBEDDING_URL:-}` in `docker-compose.yml` blijft bewust
staan als lege fallback (in plaats van verwijderd) — compose injecteert een
env-var alleen als hij onder `environment:` genoemd wordt, dus verwijderen
zou de host-/`.env`-override stilletjes breken. Het eigenlijke lege-waarde-
probleem (de misleidende bootwaarschuwing) is opgelost in `src/embeddings.py`
door lege strings als "niet gezet" te behandelen (`os.getenv(x) or default`
i.p.v. `os.getenv(x, default)`), niet door de compose-regel weg te halen.

## 0. Inventaris (gemeten op prod, 2026-09-02, read-only)

Via `docker exec ithaka-ithaka-1 python3 -c "..."` (chromadb HTTP-client naar
de `chromadb`-service) zijn dit de collecties die daadwerkelijk bestaan:

| Collectie                     | Aantal chunks | huidig `embedding_model`                           | dim | lane      |
|--------------------------------|---------------|-----------------------------------------------------|-----|-----------|
| `ithaka_rag_fastembed`         | 7499          | `sentence-transformers/all-MiniLM-L6-v2`             | 384 | fastembed |
| `ithaka_tool_index_fastembed`  | 325           | `sentence-transformers/all-MiniLM-L6-v2`             | 384 | fastembed |
| `ithaka_memories_fastembed`    | 61            | `sentence-transformers/all-MiniLM-L6-v2`             | 384 | fastembed |

Er bestaan geen `*_custom` lane-collecties in prod (geen HTTP-embedding-
endpoint geconfigureerd — `EMBEDDING_URL` staat niet gezet), dus alleen de
drie `*_fastembed` collecties hierboven zijn relevant voor deze migratie.
Notebook-bronnen zitten *niet* in een aparte collectie: ze leven in
`ithaka_rag_fastembed` met een `notebook_id`-metadata-filter (zie
`rag_manager.py` / `rag_vector.py`), dus die migreren automatisch mee met
`ithaka_rag_fastembed`.

Herhaal deze telling zelf vlak vóór migratie om een vers ijkpunt te hebben:

```bash
docker exec ithaka-ithaka-1 python3 -c "
import chromadb
c = chromadb.HttpClient(host='chromadb', port=8000)
for col in c.list_collections():
    meta = col.metadata or {}
    print(col.name, col.count(), meta.get('embedding_model'), meta.get('embedding_dimension'), meta.get('embedding_fingerprint'))
"
```

## a) Hoe zie je welk model een collectie gebruikt?

**Dit hoeft niet als follow-up gebouwd te worden — het staat er al in.**
`src/embedding_lanes.py::_metadata()` schrijft bij elke lane-collectie al
`embedding_model`, `embedding_url`, `embedding_dimension` en
`embedding_fingerprint` in de Chroma collection-metadata (gezet in
`_create_lane()` / `build_embedding_lanes()`). Dat is precies het commando
hierboven: `col.metadata["embedding_model"]`. Geen extra code nodig voor
detectie.

`embedding_fingerprint` is een sha256-hash over
`(lane_name, url, model, dimension)` — dat is ook het veld waarop de
bestaande automatische her-embed-machinery (zie stap (c)) een mismatch
detecteert.

## b) Backup van de Chroma-volume vóór migratie

Chroma draait in Docker in een los volume (`chromadb-data`, prefixed met de
compose-projectnaam), **niet** onder `./data` — `ithaka-backup` vangt dit dus
niet mee (zie `docs/backup-restore.md`, sectie "ChromaDB caveat"). Live op
prod heet het volume `ithaka_chromadb-data` (geverifieerd via
`docker volume ls | grep chroma`).

```bash
# Vanaf de Docker-host, met de stack nog draaiend (append-only read, geen
# downtime nodig voor de backup zelf):
docker run --rm -v ithaka_chromadb-data:/data -v "$PWD":/backup \
  alpine tar czf /backup/chromadb-pre-124-$(date +%F).tar.gz -C /data .

# Verifieer dat de tarball niet leeg/corrupt is:
tar tzf chromadb-pre-124-$(date +%F).tar.gz | head
```

Bewaar deze tarball tot de migratie geverifieerd is (zie stap e, rollback).

## c) Her-embedden: bestaande machinery, geen nieuw script nodig

**Er bestaat al een niet-destructieve her-embed-machinery** — die is niet
nieuw gebouwd voor dit issue, hij zat al in `src/embedding_lanes.py` en is
afgedekt door `tests/test_embedding_lanes.py::test_lane_reset_reembeds_existing_documents_on_fingerprint_change`
e.a. Elke store (`RAGVectorStore`, `ToolIndex`, `MemoryVectorStore`) bouwt
zijn lanes via `build_embedding_lanes(base_name)` — bij elke aanroep checkt
`_get_or_reset_collection()` of de bestaande collection-metadata
(`embedding_fingerprint` / `embedding_dimension` / `embedding_lane`) nog
matcht met de nu geconfigureerde client. Bij een mismatch (exact wat een
`FASTEMBED_MODEL`-wijziging veroorzaakt):

1. Alle bestaande rijen (`ids`, `documents`, `metadatas`, `embeddings`)
   worden uit de oude collectie gelezen.
2. De documents worden in batches van 100 opnieuw ge-embed met het **nieuwe**
   model.
3. De oude collectie wordt verwijderd en opnieuw aangemaakt met de nieuwe
   metadata.
4. De nieuwe embeddings + originele ids/documents/metadatas worden
   teruggeschreven.

Faalt embedden of terugschrijven onderweg, dan wordt de oude collectie
hersteld uit de in-memory preserve (`restore`-pad in `_get_or_reset_collection`)
— dus dit is geen alles-of-niets-operatie die je data-loos achterlaat, maar
het is nog steeds een migratie met I/O-risico: neem stap (b) serieus.

Deze machinery draait **automatisch** zodra de betreffende store zijn lanes
(her)opbouwt:

- `RAGVectorStore` (`ithaka_rag`) en `MemoryVectorStore` (`ithaka_memories`)
  bouwen hun lanes **synchroon bij app-startup** (`app.py`: module-level
  `rag_manager = get_rag_manager()`; `src/app_initializer.py`:
  `MemoryVectorStore(...)` in `initialize_managers()`). Een herstart van de
  `ithaka`-container ná de env-wijziging triggert het her-embedden van deze
  twee collecties tijdens het opstarten (dus vóór de app healthy is).
- `ToolIndex` (`ithaka_tool_index`) is een lazy singleton
  (`src.tool_index.get_tool_index()`) — die wordt pas (her)opgebouwd bij de
  **eerste** agent-tool-selectie na de herstart, dus niet per se tijdens de
  boot zelf.

### Concrete stappen op prod

1. **Model vooraf downloaden** (voorkomt een ~220MB cold download die de
   boot-her-embed blokkeert; geverifieerd via fastembed's eigen catalogus
   in de prod-container: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`,
   dim 384, `size_in_GB: 0.22`): via de admin-API (vereist admin-sessie,
   `require_admin`):

   ```bash
   curl -X POST -b "<admin-session-cookie>" \
     http://localhost:7000/api/embeddings/models/sentence-transformers%2Fparaphrase-multilingual-MiniLM-L12-v2/download
   ```

   Of handmatig via Settings → AI Defaults → Embeddings in de UI (dezelfde
   route). Check status met
   `GET /api/embeddings/models/{model}/status` (`downloaded: true`).

2. **Backup** (stap b hierboven) uitvoeren.

3. **Deploy** deze PR (compose-fix + code-hardening). Geen `.env`-wijziging
   nodig — de nieuwe multilinguale default in `docker-compose.yml` wint
   zodra `FASTEMBED_MODEL` niet expliciet gezet is in `.env` (en dat is hij
   nu niet — de regel staat daar alleen in comment).

4. **Herstart de app-container**:

   ```bash
   docker compose up -d --build ithaka
   ```

5. **Volg de logs** tijdens het opstarten:

   ```bash
   docker compose logs -f ithaka
   ```

   Verwacht: regels als
   `Recreating Chroma collection ithaka_rag_fastembed for embedding lane change (<oude-fp> -> <nieuwe-fp>)`
   gevolgd door `Re-embedded 7499 rows after resetting ithaka_rag_fastembed`
   (en hetzelfde voor `ithaka_memories_fastembed`). `ithaka_tool_index_fastembed`
   volgt zodra de eerste chatvraag een agent-tool nodig heeft na de restart —
   stuur bewust één test-bericht dat een tool triggert (bv. een
   documenten-zoekopdracht) om dat direct te forceren i.p.v. te wachten tot
   het vanzelf gebeurt.

6. **Verifieer** met hetzelfde commando als in stap 0: alle drie collecties
   moeten nu `embedding_model = sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
   tonen, met **hetzelfde aantal rijen** als vóór de migratie (7499 / 325 /
   61, of de verse telling die je vlak van tevoren nam).

Er is bewust **geen nieuw script** (`scripts/reembed_collections.py`)
toegevoegd — de bestaande, geteste machinery in `src/embedding_lanes.py`
dekt dit pad al volledig en is minder code om te onderhouden dan een los CLI-
pad dat dezelfde logica zou herhalen.

## d) Verwachte duur (orde van grootte)

~7885 chunks totaal (7499 + 325 + 61) op CPU, met het meertalige 12-laags
model (zwaarder dan het oude 6-laags Engelse model, en het model zelf is
~220MB tegenover ~90MB — dus de throughput per chunk is trager, ook al valt
de download zelf mee). Op basis van fastembed's typische CPU-doorvoer voor
kleine ONNX-modellen (tientallen tot ~100+ zinnen/seconde, sterk afhankelijk
van CPU-cores en chunk-lengte tot ~900 tekens):

- **Orde van grootte: enkele minuten tot ~15-20 minuten** voor de volledige
  ~7885 chunks, op een gemiddelde server-CPU zonder GPU.
- `ithaka_rag_fastembed` (7499 rijen) domineert de tijd; de andere twee
  collecties zijn klein genoeg om binnen seconden tot een paar minuten te
  volgen.
- Dit blokkeert **app-startup** voor de `ithaka_rag`- en
  `ithaka_memories`-collecties (synchroon in `initialize_managers()` /
  module-load), dus reken op een langere boot-tijd dan normaal — niet op een
  crash. Als de her-embed om wat voor reden dan ook faalt, valt
  `_get_or_reset_collection()` terug op het herstellen van de oude collectie
  (zie stap c), dus de app blijft functioneel op het oude model i.p.v. leeg
  te draaien.
- Plan de restart in een rustig moment; er is geen voortgangsindicator
  behalve de logregels — gebruik `docker compose logs -f ithaka` als
  live-observatie zoals in stap c.5.

## e) Rollback

Als de her-embed faalt op een manier die niet vanzelf hersteld wordt, of de
retrieval-kwaliteit na migratie duidelijk verslechtert:

1. **Stop app én Chroma** — Chroma houdt het volume open terwijl het draait;
   een restore terwijl `chromadb` nog leeft kan de live SQLite-backed store
   corrumperen: `docker compose stop ithaka chromadb`.
2. **Herstel de Chroma-volume** uit de backup van stap (b). Gebruik
   `find /data -mindepth 1 -delete` in plaats van `rm -rf /data/*` — die
   laatste mist dotfiles op het top-level, waardoor stale verborgen bestanden
   de restore overleven en zich vermengen met de teruggezette data:

   ```bash
   docker run --rm -v ithaka_chromadb-data:/data -v "$PWD":/backup \
     alpine sh -c "find /data -mindepth 1 -delete && tar xzf /backup/chromadb-pre-124-<datum>.tar.gz -C /data"
   docker compose up -d chromadb
   ```

3. **Zet `FASTEMBED_MODEL` expliciet terug** op het oude Engelse model in
   `.env` (`FASTEMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2`) zodat een
   volgende restart niet opnieuw naar het multilinguale default migreert
   terwijl je aan het onderzoeken bent wat er misging.
4. **Herstart de app**: `docker compose up -d ithaka`.
5. Verifieer met het inventaris-commando (stap 0) dat de collecties weer op
   het oude model + de oude counts staan.

Deze rollback is destructief voor alles wat ná de backup is toegevoegd aan
Chroma (nieuwe notebook-bronnen, nieuwe geheugen-items, tool-index-updates
sinds de laatste MCP-reindex) — communiceer dat voordat je hem uitvoert.

## f) Follow-up: her-evalueer de notebook-similarity-threshold

`NOTEBOOK_RAG_SIMILARITY_THRESHOLD = 0.15` (`src/chat_processor.py`,
toegevoegd in PR #131) is een harde ondergrens voor notebook-RAG-hits,
gekalibreerd op de cosine-afstandsverdeling van het **oude** Engelse model.
Een ander embedding-model geeft in de regel een andere schaal/verdeling voor
cosine-similarity tussen queries en chunks — met name een multilingual model
getraind op andere data kan structureel hogere of lagere similarity-scores
geven voor dezelfde Nederlandse content. Na deze migratie:

- Doe een paar representatieve notebook-vragen (NL) en log de similarity-
  scores die langs de threshold komen (of tijdelijk verlaag logniveau in
  `chat_processor.py` rond de threshold-check).
- Als hits die voorheen net binnen de threshold vielen er nu net buiten
  vallen (of andersom, ruis die er nu wél doorheen komt), herijk `0.15` op
  basis van de nieuwe verdeling.
- Dit is bewust **niet** in deze PR meegenomen — het vereist evaluatie tegen
  echte data ná de migratie, niet een blinde constante-wijziging vooraf.
