# Sessielog 2026-08-19 — Studio-design: visual reports + Generate/Files-scheiding

**Resultaat:** vervolg op PR #21 (werkruimte). Twee verbeteringen op Ed's verzoek: (1) het Studio-paneel
maakt nu visueel onderscheid tussen genereer-acties en resultaat-bestanden; (2) tekst-artifacts openen
als editorial visual report in de stijl van de research-rapporten.

## Wat er is gebouwd

- **`src/notebook_report.py`** (nieuw): adapter die een artifact-document door `generate_visual_report`
  (src/visual_report.py) haalt — hero, TOC, stats-balk (Notebook / Type / Generated), export-toolbar.
  `ENGLISH_KIND_LABELS` als label-bron; `sources=[]` omdat URL-loze pseudo-sources kapotte
  `href=""`-anchors renderen.
- **`GET /api/notebooks/{id}/artifacts/{aid}/report`** (routes/notebook_routes.py): owner-scoped
  HTML-response; podcast → 404; mindmap toegestaan. CSP-pad-allowlist in core/middleware.py strak
  gescoped (`/api/notebooks/` + `/artifacts/` + eindigt op `/report`).
- **`src/visual_report.py`**: twee backward-compatible params (`report_type_label`,
  `generated_by_label`), defaults byte-identiek aan oud gedrag.
- **Studio-paneel** (static/js/notebookWorkspace.js): GENERATE-sectie (6 actieknoppen met
  SVG-plusicoon, 2-koloms grid) | FILES-sectie (rijen: bestandsicoon, kind-pill, titel, datum,
  bron-document-knop, delete). Rij-klik → rapport in nieuw tabblad (`window.open`); podcast houdt
  inline player (nooit /report), mindmap houdt Mermaid-preview. Labels Engels (lost NL/EN-mix
  follow-up uit PR #21 op).
- **Cleanup:** dode detail-view-CSS verwijderd (`.notebook-detail-head` e.a., 0 refs).

## Proces (SDD)

Task A (backend) + Task B (frontend, parallel in worktree tegen vast URL-contract). Task A review
clean; Task B review: 2 Important (error-slot in verkeerde sectie; te losse kind-branch-tests) →
fixronde 1, scoped re-review: beide RESOLVED, 0 nieuwe defects. Merge-conflict style.css (dode class
her-geïntroduceerd door worktree-lag) handmatig opgelost.

## Verificatie

Volle suite 4925 passed / 3 skipped; `node --check` clean. Browser-smoke op verse :7001-instance
(desktop 1400px + `emulate` 360px): Generate/Files-secties zichtbaar en onderscheiden, rij-klik opent
visual report (research-stijl, ook mobiel responsive), bron-document-knop opent doc-viewer,
Escape-cascade (viewer eerst, dan werkruimte) correct, console clean op pre-existing polls na.
Artifact geseed via sqlite (smoke-env heeft geen LLM; `POST /artifacts` geeft dan nette 503-detail).

## Follow-ups

- `NotebookArtifact` heeft geen eigen titel-kolom; rij-titel = documenttitel (werkt), maar hernoemen
  van artifacts is er niet.
- Issue #22 (first-send-grounding-bypass) blijft open.

Ed de Feber, in nauwe samenwerking met Claude
