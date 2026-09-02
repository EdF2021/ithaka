# Sessie 2026-09-02 (middag) — restpunten afgerond, embeddingmigratie, Chroma-volume-lek gevonden

## Aanleiding

Vorige sessie brak af halverwege de S11-smoke van PR #136. `/goal`: alle openstaande zaken
zelfstandig afmaken, subagents (sonnet) uitzetten, regie centraal. Vertrekpunt: #136 open (CI
groen, geen smoke), sessielog uncommitted, 26 stale worktrees, issues #135/#124/#122/#112/#81.

## Gemergede PRs (5)

- **#136** — notebook_id-snapshot bij submit + server-side reject van `"undefined"`/`"null"`.
  S11-smoke (desktop + 360px, node-click via `postMessage`) bewees de keten incl. applog
  `RAG: 1/1 … notebook_id=… query='<label>'`. Bijvangst tijdens de smoke: de twee
  no-session-fallback-paden (`chat.js:936/962`) riepen `materializePendingSession()` nog kaal aan
  → sessie zonder notebook_id; gefixt in 8cabf75 met statische regressietest (rood op pre-fix).
- **#137** — issue #124: compose-fallback naar het meertalige fastembed-model (3 compose-files),
  `os.getenv(x) or default` voor lege env-waarden, `.env.example`, runbook
  `docs/embedding-model-migration.md`. Review-fixronde: de runbook-claim "valt terug op oud
  model bij falen" was onjuist (lane wordt weggelaten → RAG/notebooks/memory onbeschikbaar +
  30s-retry-thrash); herschreven met "stop de app"-instructie.
- **#138** — issue #135: `streamingTTS` én `_isAgent` waren `const` binnen de `try` van
  `handleChatSubmit` en werden in de `catch` gelezen → `ReferenceError` (sibling-scope, géén TDZ
  zoals de eerste PR-tekst beweerde; reviewer corrigeerde het mechanisme én vond `_isAgent`).
  A/B-smoke: zonder fix `ReferenceError: streamingTTS is not defined at chat.js:3370` bij Stop;
  met fix 0 unhandled rejections. PR-body moest naar template-vorm (description-check faalde na
  een `gh api PATCH` van de agent).
- **#139** — issue #122 rest: `windowResize.js` zoom-aware (begin/move/end + localStorage in
  lokale px + restore), swipe-dismiss `translateY` in `documentLibrary.js`/`memory.js`, en na
  review ook de FLIP-reflow-transforms in `notes.js`. S12-smoke exact: Δcursor 100 → Δbreedte 100
  viewport-px op 100% én 125% (style 600→680 lokaal), round-trip 125%→100% ratio 1.25, swipe
  dy=40 → `translateY(32px)` op 125% (finger 1:1). Eerste meting leek −62.5: venster stond tegen de
  viewport-rand (correcte clamp).
- **#140** — bijvangst deploy: het `chromadb-data`-volume was op `/chroma/chroma` gemount, maar
  chroma 1.x persisteert op `/data` (`/config.yaml`). De echte store (109 MB, 7499+325+61 rijen)
  leefde in de container-layer; de pre-migratie-backup was 87 bytes. Live gemigreerd: chromadb
  stop → `docker cp /data` (backup `~/ithaka-backups/`) → volume gevuld → mount naar `/data` →
  recreate → tellingen identiek. Docs bijgewerkt in `docs/backup-restore.md`.

## Prod-deploy + embeddingmigratie (#124)

Nieuwe image gebouwd terwijl de stack draaide; app gestopt; Chroma-volume-lek ontdekt en gefixt
(zie #140); app gestart → bestaande lane-machinery her-embedde `ithaka_rag_fastembed` (7499
rijen, 4m50s CPU; onnxruntime-CUDA-provider mist `libcublasLt.so.13`, dus CPU) en
`ithaka_memories_fastembed` (61). Tool-index (325) volgt lazy bij de eerste tool-call. Raw-cosine
probe in het thesis-notebook: thesis-vraag → top-5 thesis-chunks sim 0.362–0.445 (was
0.219–0.268 onder het Engelse model). `NOTEBOOK_RAG_SIMILARITY_THRESHOLD=0.15` blijft staan tot
er hybride scores verzameld zijn (genoteerd in #124).

## Live-validatie #112

Node-click "Source Filtering" in het prod-notebook (10 bronnen): applog `RAG: 8/8 results above
threshold 0.15 (notebook_id='8570ec1c…', source_ids=None, query='Source Filtering')`, geciteerd
antwoord `[3, ¶2][5, ¶4]` uit 8 bronnen. #112 en #124 gesloten; #135/#122 dicht via PR-keywords.

## Nieuw issue

- **#141** — notebook-chat in Agent-modus: geblokkeerd fenced tool-block (lockdown) triggert een
  lege agent-ronde 2 ("Referentiecontext ontvangen…") na het correcte antwoord.

## Opruimen

23 agent-worktrees + ~45 lokale branches verwijderd (allemaal gemerged, schoon); sessielog van de
ochtend gecommit. #81 blijft open als release-watch (docker-CLI nog 29.7.2, pip 26.2.1 — niets te
bumpen, gecommentarieerd).

## Lessen

- Verifieer een backup op inhoud (grootte/entries) vóórdat je erop vertrouwt — de 87-byte tarball
  onthulde het volume-lek dat anders bij de eerstvolgende `docker compose down` data had gekost.
- Reviewers (sonnet, onafhankelijk) vonden opnieuw de echte gaten: `_isAgent`, het TDZ-misverstand,
  de onjuiste runbook-faalmodus, notes.js-reflow. Fix-rondes via `SendMessage` naar dezelfde agent
  werken goed (context behouden).
- Smoke-valkuilen vastgelegd in memory: sessie-cookie gedeeld over poorten op 127.0.0.1, `pkill -f`
  doodt de eigen shell, `createDirectChat` met niet-exacte cache-URL wordt door modelPicker genuld,
  resize-metingen eerst weg van de viewport-rand, `docker exec` zonder `-i` slikt heredoc-stdin.

Ed de Feber, in nauwe samenwerking met Claude
