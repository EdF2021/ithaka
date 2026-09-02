# Sessie 2026-09-01 (avond) — autonome issue-veeg met subagent-fanout

## Aanleiding

Ed's `/goal`: "voer al deze issues zelfstandig uit. Stel mij geen vragen. Fan out subagents.
Gebruik voor de subagents goedkopere modellen. Hou zelf de regie." — op de openstaande lijst
na de Rapporten-merge (#110): issues #56/#80/#81, Rapporten-polish, Rapporten fase 2 en het
RAG-retrieval-onderzoek. Werkvorm: sonnet-implementers/reviewers/smoke-agents in eigen
worktrees, regie/reviews/merges centraal; elke PR met CI + bewijs in chat vóór merge.

## Gemergede PRs (7)

- **#111** — issue #56: `_SUGGEST_TIMEOUT_S` 8→30s + zichtbare WARNING bij timeout (Qwen3-14B
  haalde 8s nooit; suggesties waren stil altijd leeg).
- **#113** — Rapporten-polish: `recommended_status` (`ok`/`no_sources`/`unavailable`) zodat de
  modal LLM-uitval niet meer als "geen bronnen" verkoopt; NL-strings; `_JSON_FENCE_RE`
  single-line-fence; docstring; testgaps.
- **#114** — issue #112: mindmap-node-click deed `closeNotebookWorkspace()` vóór de
  submit-dispatch → `source_ids` viel stil weg; volgorde geswapt + RAG-logregel verrijkt met
  notebook_id/source_ids/query. (Restgat: de issue-#22 fail-closed-guard zit ná een await en is
  hiermee níét hersteld — open in #112.)
- **#116** — issue #115 (gevonden door de smoke van #113): Rapporten-modal op mobiel volledig
  achter het opake Studio-tabpaneel (`.modal` z 250 vs `#nbws-root` 10005); fix spiegelt het
  `.doc-editor-pane`-precedent (10010).
- **#118** — issue #117, Rapporten fase 2: bron-niveau citaties. Genummerde
  `=== BRON [n]: ... ===`-headers (alleen kind=report), citatie-instructie + `## Bronnen`,
  `validate_report_markdown` in de retry-seam (bewaakt alléén out-of-range-nummers).
  Bewuste afwijking van "[n, ¶N] zoals chat": ¶N is buiten de chat-UI onresolvebaar.
  E2E-smoke met echte lokale Ollama: correcte citaties + Bronnen-lijst.
- **#120** — issue #119 (Eds mailteller-vraag): dashboard-unread was stale én las de verkeerde
  cache-bucket — route zocht onder letterlijk `"default"`, de mail-UI indexeert onder het
  account-UUID. Fix: account-resolutie via `_get_email_config` + TTL 120s met live IMAP
  UNSEEN-fallback (en stale-cache-terugval bij IMAP-fouten).
- **#121** — issue #80: zoomOf/toLocalPx-uitrol over 24 bestanden/~90 sites buiten chat/export,
  incl. review-vondst `tileManager.js` (tile-snap/fullscreen) en de width/height-restfout op
  notes-drag-ghosts + tour-halo's. Onafhankelijke review (APPROVE-MET-FIXES → fix-round →
  smoke bij ui-scale-125). Rest van de klasse (windowDrag.js e.a.): issue #122.

## Overig

- **#81** release-watch: docker-CLI max 29.7.2, pip max 26.2.1 — geen actie mogelijk; comment
  op de issue.
- **RAG-onderzoek** (aanleiding: thesis-notebook miste retrieval bij node-click): notebook_id-
  filter en history-vervuiling met bewijs uitgesloten; hoofdhypothese = zwakke kale node-query
  + condensatie-fallback met generieke sjabloonwoorden in de 30%-keyword-score. Voorstel B
  (condensatie ankeren op de kale node-label) geparkeerd in #112.
- Sessielog middagrun + fase-2-spec-sectie direct op dev gepusht.

## Lessen / gotcha's

- **`src/chroma_client.py` negeert `ITHAKA_DATA_DIR`** — een smoke-instance met verse datadir
  verbindt stilzwijgend met de prod-Chroma en kan een re-embed van de prod-collectie triggeren.
  Smoke-recept voortaan: ALTIJD `CHROMADB_HOST/PORT` expliciet naar een wegwerp-container of
  onbereikbare poort zetten, ook als de test "geen Chroma nodig" heeft.
- Subagent-smokes: chrome-devtools `click` faalt vaak stil op deze app → standaard
  `evaluate_script` met `element.click()`; mobiel via `emulate`, niet resize_page; servers
  starten mét sandbox-uit (geïsoleerde netns anders onbereikbaar) — twee agents strandden op
  wachten-op-monitor en moesten bijgestuurd (poll met curl i.p.v. monitor-taken).
- `gh pr checks`-watchloops direct na PR-aanmaak kunnen te vroeg DONE zeggen (checks nog niet
  geregistreerd) — eerst ~30s slapen.

## Eindstand & open punten

- Prod herbouwd op dev t/m #121; live geverifieerd: app gezond, Mail-widget toont de echte
  live-telling (2140, screenshot in sessie).
- **PR #123** (unread altijd live tellen met in-memory TTL-cache — heft het 907↔2140-flip-floppen
  van het partiële index-pad op): de "denied merge" van 2026-09-01 bleek een misklik; op Eds
  "waarom merge je niet??" (2026-09-02 08:00) alsnog gemerged (`183ab45`), prod herbouwd en live
  geverifieerd: poll 1 `2142/live`, poll 2 `2142/memory` — stabiel. Issue #119 gesloten.
- #121-restgap: mobile-only notes-drag-ghost-maat niet visueel verifieerbaar (native touch-DnD
  buiten CDP-bereik) — genoteerd op #122.
- Open issues na deze run: #112 (RAG voorstel B + prod-validatie), #122 (zoom-restklasse),
  #119 (tot #123 landt), #81 (release-watch), plus pre-existing #103/#102 (dependabot).

Ed de Feber, in nauwe samenwerking met Claude
