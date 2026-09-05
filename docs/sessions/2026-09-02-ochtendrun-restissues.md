# Sessie 2026-09-02 (ochtend) — restissues afgerond, retrieval-diepgang, dependabot-hygiëne

## Aanleiding

Vervolg op de avondrun van 1-9 onder dezelfde `/goal`-voorwaarden (subagent-fanout op sonnet,
regie centraal): de resterende issues #112/#122/#81 + dependabot-afhandeling. Plus de
merge-nasleep van #123 (de "denied merge" bleek een misklik van Ed; alsnog gemerged, gedeployed
en live geverifieerd: poll 1 `2142/live` → poll 2 `2142/memory`, stabiel; #119 dicht).

## Gemergede PRs (8, naast #123)

- **#103** — dependabot actions-bumps (claude-code-action v1.0.210 e.a.).
- **#126** — issue #125: de 5 legitieme python-bumps uit het gesloten dependabot-#102 zónder de
  mcp-pin te schenden + sluitende dependabot-ignore voor mcp (alle update-types).
- **#127** — windowDrag.js zoom-aware (#122): 2 sites (initiële pin + cursor-volg-delta),
  13 tool-windows; smoke-metingen exact (delta 100.0/60.0 bij cursor 100/60 op 125%).
- **#130** — #122 zoom-minors: 4 lokaal-relatieve-delta-sites (slider-ux, notes `_openTextInput`,
  document `_measurePos`, email-FAB), cookbookServe-fallback opgeruimd, sessions-dropdowns
  expliciet `position:fixed`, checklist-grab-test. Review-fixround: gap-constanten buiten de
  deling (meerderheidsprecedent).
- **#131** — issue #112: `NOTEBOOK_RAG_SIMILARITY_THRESHOLD = 0.15` (notebook-pad; algemene chat
  houdt 0.35) + logging van stille lege retrievals (`RAG: 0/n ... top3_similarities`) + debug-log
  van het effectieve where-filter in `VectorRAG.search`.
- **#132** — issue #112 voorstel B: `search_hint`-keten (kale node-label → chat-payload →
  condensatie-fallback) + #22-guard-snapshot-fix. Review-fixround: hint onvoorwaardelijk
  geconsumeerd als eerste statement van `handleChatSubmit` (lek-scenario dichtgezet, regressietest
  bewezen falend op pre-fix). Smoke bewees functioneel: fallback-query = kale label.
- **#134** — issue #133 (bijvangst uit de #130-smoke, zoom-onafhankelijk pre-existing):
  `.ge-adj-row { position: relative }` + font-shorthand→longhands in twee document.js-mirrors.
  Smoke: chip-edit deltaLeft 0px (was −12/−15), highlight exact op selectie (was 80-150px mis).

## Prod-onderzoek #112 (zelf, met echte data)

Node-click-vraag in Eds thesis-notebook (10 bronnen, 258 chunks): applog toonde condensatie-
falen (query = volledige sjabloonzin) én `0 results` raw — terwijl dezelfde zoekopdracht ad-hoc
in de container 8 thesis-hits gaf met similarities 0.219–0.268: allemaal onder de oude
0.35-drempel. Daaruit: #131/#132 hierboven, plus **issue #124**: de compose-fallback zet
`FASTEMBED_MODEL=all-MiniLM-L6-v2` (Engels-only) en overschrijft de meertalige code-default —
alle 7499 prod-embeddings zijn met een Engels model gemaakt; migratieplan in de issue
(modelwissel vereist her-embed; drempel daarna heroverwegen). Bijvangst: compose zet ook lege
`EMBEDDING_URL=` → misleidende boot-warning.

## Dependabot-hygiëne

#102 gesloten (mcp<2→<3-schending), vervangen door #126. Dependabot maakte ze prompt opnieuw
aan (#128 zelfde groep, #129 kale mcp-verbreding) — beide gesloten; als ze ná de nieuwe
volledige mcp-ignore wéér terugkomen is de ignore-syntax het volgende onderzoekspunt.

## Open na deze run

- #124 (embeddingmodel-migratie — bewust apart traject), #122 (rest: `windowResize.js`,
  doclib-pos-restje), #112 (prod-validatie drempel/hint na deploy — zie eindstand), #81
  (release-watch), #135 (uncaught promise in handleChatSubmit bij model-loze send, bijvangst).

## Lessen

- Dezelfde subagent-valkuilen als gisteravond (passief wachten op monitor-taken, sandboxed
  servers) — tweemaal bijgestuurd; briefs bevatten het recept inmiddels expliciet.
- Reviewers verdienen hun plek: hint-lekkage (K), gap-constanten-inconsistentie (N) en de
  gemiste tileManager (#121, gisteren) kwamen allemaal uit onafhankelijke review, niet uit CI.
- Smokes vangen ook pre-existing bugs: #133 (2 stuks) kwam uit de S8-meting, mét bewijs dat ze
  zoom-onafhankelijk waren (zelfde meting op 100%).

Ed de Feber, in nauwe samenwerking met Claude
