# Sessie 2026-09-01 (middag/avond) — Rapporten-feature voor notebooks + backlog-veeg

## Aanleiding

Ed: "Ik wil graag een extra functie bij notebook dat heet rapporten" — met een
NotebookLM-referentiescreenshot van de "Rapport maken"-modal (vaste sjablonen + "Aanbevolen
indeling" met AI-gegenereerde, bron-specifieke rapportvormen). Daarvóór in dezelfde sessie:
de drie openstaande backlog-punten uit de vorige handoff.

## Backlog-PRs (alle gemerged naar dev)

- **#104** — issue #101: memory-extractor pinde identity uit één begroeting; auto-pin wordt nu
  overgeslagen bij conflicterende identity-facts.
- **#105** — pr-description/title-checks slaan `synchronize`-events over (minder ruis bij pushes
  naar een open PR).
- **#106** — cookbook pip-installs krijgen een request-timeout-cap (recommend-backlog #89).
- **#107** — e-mail: sqlite-connecties sluiten op exception-paden (29-conn-audit, #89).

## Rapporten-feature (PR #110, issue #109, gemerged 18:43)

NotebookLM-stijl "Rapport maken": nieuwe Studio-tegel **Rapporten** → modal met 4 vaste kaarten
(Zelf rapport maken + Overzichtsdocument/Studiemateriaal/Blogpost) en 4 **AI-aanbevolen
indelingen** op basis van de echte bronnen, gecachet per bron-fingerprint
(`Notebook.report_layouts_json`/`_fingerprint`). Elke kaart → bewerkbaar instructieveld →
`POST /artifacts` met `kind:"report"` + `layout_instruction` → gewoon markdown-artifact via de
bestaande pipeline.

Kern: nieuw `src/notebook_report_layouts.py` (sjablonen, suggestie-LLM-call met
`DUTCH_OUTPUT_RULE`, fingerprint-cache), `kind:"report"` + `layout_instruction`-param in
`notebook_artifacts.py`, route `GET /api/notebooks/{id}/report-layouts`, modal + CSS in
`notebookWorkspace.js`/`style.css`. Spec:
`docs/superpowers/specs/2026-09-01-notebooks-rapporten-design.md`; plan in
`docs/superpowers/plans/`.

### Proces: subagent-driven development

8 taken, per taak een verse implementer (sonnet) + taakreviewer; eind-review op zwaarste model.
Eindreview vond 5 Important-punten, in één fixwave gesloten en her-geverifieerd:

1. `layout_instruction` onge-escaped in de vertrouwde promptzone (kan LLM-tekst uit een
   AI-aanbeveling bevatten) → `_escape_guard_markers`.
2. `GET /report-layouts` vs 45s-hard-timeout (3×60k-char LLM-payload) → per-bron excerpt-cap
   2000 tekens (`_gather_excerpt_text`) i.p.v. een `app.py`-exemption.
3. Statische wiring-test voor de tegel ontbrak → toegevoegd (`test_notebook_workspace_static.py`).
4. Deadlock-kwargs (`wait_for_quiet=False, workload="foreground"`) van de nieuwe call-site
   waren ongetest → assertion.
5. Minors: modal-re-entrancy-guard, `maxlength=2000`, dode CSS-var-fallbacks.

### Verificatie

Pytest 5360 passed / 3 skipped (baseline 5331; +29). Twee browser-smoke-rondes op geïsoleerde
instances (throwaway ChromaDB — de gedeelde vector-store niet aanraken!): ronde 1 zonder LLM
(degradatiepaden, desktop + 360px), ronde 2 met echte lokale Ollama — end-to-end rapport
gegenereerd, Files-pill "Report", inline viewer correct. CI 16/16 groen.

### Lessen / gotcha's

- chrome-devtools-mcp `click` faalt soms stil op meerregelige `<button>`-kaarten; cross-check
  met directe JS-`.click()` bewees dat het een tooling-artefact is, geen app-bug.
- `/api/auth/settings` verwacht een JSON-body (`Content-Type: application/json`) — FormData
  geeft een 500 (`JSONDecodeError`).
- `gh pr edit` faalt op dit repo (classic-projects-deprecation in GraphQL); PR-body wijzigen via
  `gh api repos/.../pulls/{n} -X PATCH --input <json>`.

## Bewust geparkeerd → avondrun

Na de merge is (op Eds `/goal`: "voer al deze issues zelfstandig uit") een tweede run gestart:
issue #56 (suggestie-timeout), #80 (zoom-drift popups), #81 (release-check), Rapporten-polish
(o.a. "geen suggesties"-melding onderscheiden van LLM-uitval), RAG-retrieval-onderzoek en
Rapporten fase 2 (inline citaties). Uitkomsten in een vervolg-sessielog.

Ed de Feber, in nauwe samenwerking met Claude
