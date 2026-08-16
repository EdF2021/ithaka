# Sessielog 2026-08-14 — Notebooks Fase 1 (NotebookLM-stijl bronnenscoping)

Vervolg op de verkenning `docs/notebooklm-gap-analyse.md` (2026-08-13, 3 parallelle
subagent-rapporten). Ed koos: notebook als eerste-klas concept, chat strikt NotebookLM-stijl
(alleen bronnen). Design: `docs/superpowers/specs/2026-08-14-notebooks-fase1-design.md`.
Uitvoering via SDD-ledger `.superpowers/sdd/2026-08-14-notebooks-fase1/progress.md`, branch
`feat/notebooks-fase1` (off `dev` @ 9fee651).

## Wat gebouwd

- **Datamodel**: tabellen `notebooks` + `notebook_sources` (`core/database.py`), kolom
  `sessions.notebook_id` via het bestaande idempotente migrator-patroon, mee-geserialiseerd
  in `Session.to_dict()`.
- **Ingest-brug** (`src/notebook_ingest.py`): één pad per geüpload bestand dat zowel een
  viewer-`Document` als Chroma-embeddings oplevert (voorheen twee losgekoppelde
  documentwerelden — de hoogste-hefboom-fix uit de gap-analyse). Allowlist-check, parse
  (PDF/Office/EPUB/tekst), chunken (1000/200), embed, pas ná geslaagde embed de
  `Document`-row aanmaken (geen wees-rows bij een embed-fout), `NotebookSource`-status
  (`indexed`/`failed`) per bestand — één mislukt bestand blokkeert de batch niet.
- **Scoped retrieval**: `VectorRAG.search(..., notebook_id=None)` met Chroma `$and`-filter
  op `owner` + `notebook_id`, doorgegeven via `RAGManager.search` → `ChatProcessor`.
- **Strikte notebook-chat**: `build_chat_context` forceert `use_rag`/onderdrukt
  memory+web voor notebook-sessies, injecteert een statische (KV-cache-veilige)
  grounding-systemprompt (citeer `[n]`, zeg expliciet als bronnen iets niet dekken),
  lege-resultaten-branch. Tools dicht via twee grendels: escalatie chat→agent overslaan
  voor notebook-sessies, plus defense-in-depth `block_all_tool_calls=True` in het
  tool-policy-blok.
- **Klikbare `[n]`-citaties**: `rag_sources` krijgt `document_id` + `index`; frontend
  linkt `[n]`-markers naar de bestaande document-viewer via het click-delegate-patroon.
- **Notebooks-UI** (`static/js/notebooks.js`, dashboard.js-patroon): lijst-view
  (aanmaken/archiveren/verwijderen) en detail-view (bronnenlijst, upload-dropzone,
  "Open chat" → maakt/hervat notebook-sessie), sidebar- + rail-knop, `/notebooks`
  deep-link-route, RAG-badge in de sessielijst.

11 commits op `feat/notebooks-fase1` (T1–T9, `8fc7983`..`6e0086b`); ingest-bugfixes
(`.docx`-mangel, allowlist, chunk-size) lopen apart op `fix/rag-ingest-docx`.

## Werkwijze: wave-parallel subagents

Op gebruikersdirectief ("fan out subagents") wave-parallel worktrees voor disjuncte taken,
merge + per-taak review per wave: A=[T1,T2] → B=[T3,T5] → C=[T4,T6] → D=[T7,T8] → T9 +
eindreview. Elke implementer in een eigen worktree, cherry-pick na review-approval; fix-rondes
resumen de oorspronkelijke implementer/reviewer (context intact) i.p.v. een verse agent.
Pre-flight conflict-scan vooraf (bestand-overlap, kwarg-consistentie tussen taken) — geen
tegenspraken gevonden, wel twee bewuste rulings (T7/T8 mogen allebei aan `style.css` appenden;
controller lost het append-conflict bij merge op).

## Incidenten

- **Stale worktree-base** (T1, T2): worktree-agents branchten van een verouderde `dev`-snapshot
  (3485ac9 i.p.v. de actuele feat-branch-head). Geen drift in de geraakte bestanden dus veilig
  cherry-pickbaar, maar structurele ruling: vervolgdispatches moeten implementers eerst laten
  fast-forwarden/mergen naar de actuele branch-head; voor wave A verifieerde de controller
  no-drift per bestand als tussenoplossing.
- **Sessielimiet killt wave D**: de T7- en T8-implementers werden midden in hun run gekilld
  (2026-08-14 ~15:25). T7-worktree had een ongecommitte partial (index.html gewijzigd,
  notebooks.js untracked); T8-worktree had niets. Ruling: beide partials weggegooid, allebei
  vers herdispatcht vanaf de bac814c-merge-baseline (op dat moment 2977bca). Kosten: wat
  dubbel verkenningswerk, geen inhoudelijk verlies.
- **MCP-gat gevonden en dichtgezet** (T6, review Critical): de tool-lockdown blokkeerde het
  gewone toolregister, maar miste `mcp__*`-tools — aantoonbaar bereikbaar via expliciete
  agent-mode op een notebook-sessie. Reviewer leverde concrete fix-routes (allowlist-sentinel
  of `dataclasses.replace(block_all, disable_mcp)`); fix-ronde loste dit op plus twee
  aanvullende Critical/Important-bevindingen (guard-mechanisme zelf ongetest, non-streaming
  `/api/chat`-pad onbeveiligd via `use_research`-bypass). Re-review: alles empirisch geverifieerd
  (mcp geblokkeerd, plan-mode/guide-only-interacties schoon).

## Open punten / bewust uitgesteld (uit de ledger)

- T1: `NotebookSource.to_dict()` laat `updated_at` weg (brief-letterlijk); cascade-test dekt
  alleen ORM-cascade, niet de `SET NULL`-tak; migrator-upgrade-pad ongetest.
- T2: `rag_manager.py`'s conditionele `notebook_id`-forward bestaat alleen om een
  over-gepinde mock in `test_rag_search_signature.py` te vriendjes — voorstel die assertie
  later te versoepelen.
- T3: temp-file-spool-pad ongetest (rond gepatcht); dubbel `mkstemp`-blok in `_extract_text`;
  naamgeving-asymmetrie `extract_pdf_text`; "parse failed"-melding bijna dood (extractors
  slikken fouten in → "no extractable text").
- T4: dubbele try/except-log-wrapper rond `remove_notebook` (2 plekken); `?archived=1`-ordening
  ongetest.
- T5: suppressie-keten in `build_chat_context` ongetest (wel leesbaar geverifieerd);
  geforceerde RAG overschrijft een expliciete `use_rag=false` — vandaar de T7/T8-carry om de
  RAG-toggle te verbergen/uitschakelen op notebook-sessies; `POST /session` accepteert een
  onbestaand/vreemd `notebook_id` zonder ownership-check (geen datalek — Chroma's
  owner-filter `$and` dekt dit af, alleen een dode sessie als gevolg).
- T6: `_forced_tools`-sturing vuurt nog op geblokkeerde tools; `hidden_tools` niet gezet;
  onnodige followup-regex-pass voor notebook-sessies. Buiten scope genoteerd:
  `allow_background_extraction` staat nu ook uit voor notebook-sessies (memory/skills
  post-extractie uit) — consistent met `mem_enabled=False`, vermoedelijk bedoeld, ongetest.
- T7: N+1 counts in de lijst-view; verouderd geworden code-comment (gefixt tijdens
  fix-ronde); detail-error wordt niet gecleard bij view-wissel; dubbelklik op de upload-zone;
  sidebar-plaatsing is een smaakkeuze.
- T8: minors uitgesteld: substring-guard, attribuut-injectie-regex, re-render-degradatie,
  keyboard-shortcuts/archived-peek-bypass, `selectSession`-meta-undefined, rename-modal
  (self-heals via `loadSessions()`-repaint). (Een eerdere "dode `#rag-toggle`"-claim bleek
  onjuist: `chat.js:1225` leest hem wél, `app.js:2380` heeft de handler.)

## Eindresultaat

- T8 doorliep twee fixrondes (class-based RAG-hide via `body.notebook-session` omdat
  `_syncRagIndicator` de inline-hide terugdraaide; badge-clipping opgelost met
  inline-flex + `.chat-title-text`-span; header-rename las de badge-tekst mee — gefixt met
  `refreshChatMetaTitle()`-export). Re-review: alles opgelost.
- T9: volledige sweep groen — services 1175, routes-fast 384, security 648, 45 notebook-tests,
  188 area_js; py_compile en `node --check` schoon.
- Eindreview (hele branch, meest capabele model): security en de UI→sessie→strikte-chat-keten
  zonder gaten; één blocker gevonden en gefixt — de keyword-fallback van `VectorRAG.search`
  negeerde `notebook_id`, waardoor een Chroma-storing in notebook-modus het hele corpus als
  "notebook-bronnen" kon presenteren (`b26f911`, met pinnende test). Alle 22 geparkeerde
  minors geadjudiceerd als ship.
- Browser-smoke (verse instance, poort 7001, desktop + 360px): CRUD, upload (.txt/.docx →
  `indexed`), NOTEBOOK-badge, verborgen RAG-knop, in-bron-antwoord met klikbare `[1]` die de
  document-viewer opent, Sources-box met `[n]`-prefix, en de buiten-bron-weigering ("De
  notebook bronnen bevatten geen informatie over de hoofdstad van Frankrijk", gemma4).
  De smoke ving één echte integratiebug die alle tests misten: `get_rag_manager()` levert de
  kale `VectorRAG` en geen wrapper — upload-500, gefixt in `09211ac`.
  Kanttekening: een klein model (llama3.2:3b) negeert de grounding-prompt en beantwoordt
  wereldkennis-vragen gewoon; bron-strikt gedrag vraagt een competent model.

## Volgende stappen
- Fase 2 (tekst-artifacts: study guide/briefing/FAQ/quiz, mermaid-mindmap) en Fase 3
  (audio overview) volgens `docs/notebooklm-gap-analyse.md` — niet in deze branch.
