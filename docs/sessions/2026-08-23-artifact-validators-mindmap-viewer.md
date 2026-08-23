# 2026-08-23 — Artifact-validators + interactieve mindmap-viewer

**Aanleiding.** Ed meldde drie renderproblemen in Notebooks: de infographic
toonde markdown-code i.p.v. de gerenderde poster, de flashcards-deck had maar
één kaart, en de mindmap renderde niet. Plus de wens: een échte mindmap
waarin je kan klikken (suggestie: mermaid).

## Rootcause (systematic debugging, één oorzaak voor alle drie)

Modellen die de gevraagde artifact-structuur negeren (vrije proza, verkeerde
kopniveaus, geen mermaid-fence) kwamen ongevalideerd in de database — alleen
`slide_deck` had een validator in de retry-seam van `generate_artifact`
(PR #37). Vijf productie-artifacts (2026-08-20..23) bewezen het patroon:

- infographic: proza/tabellen → alles in de fallback-"Content"-kaart = ruwe
  markdown zichtbaar
- flashcards: "## "-hoofdstukken + één "### " → deck met één kaart
- mindmap: proza zonder ```mermaid-fence → preview rendert niets

De overige soorten (briefing/quiz/faq/study_guide/data_table) zijn niet
kwetsbaar: die gaan door een generieke markdown→HTML-pass
(`_md_to_html`, `src/visual_report.py`).

## Geleverd

- **PR #44** (fixes #43, gemerged): format-validators voor infographic,
  flashcards en mindmap in `_KIND_VALIDATORS` — zelfde retry-met-foutmelding
  als de slide deck, max 3 pogingen. Infographic toetst op de
  renderer-uitkomst (vertaalde "Key numbers"-kop die de parser promoveert
  slaagt gewoon); flashcards ≥3 kaarten met achterzijde, geen "## ";
  mindmap eist fence + `root((...))` + ≥2 hoofdtakken (nieuwe module
  `src/notebook_mindmap.py`). 17 TDD-tests; gevalideerd tegen de echte
  productie-artifacts: 5 kapotte REJECT, 3 goede ACCEPT. Browser-smoke:
  verse infographic gegenereerd op :7001 → echte poster.
- **PR #46** (fixes #45, gemerged): interactieve mindmap-viewer.
  Mermaid-markdown blijft het opslagformat; eigen self-contained viewer
  (patroon van de slides/flashcards-viewers) rendert de boom met klikbare
  in/uitklapbare takken + "Alles uitklappen/inklappen". Werkruimte-klik
  opent nu de viewer; de secundaire knop blijft het ruwe document openen.
  Smoke desktop+360px met echte prod-inhoud; geen overflow, console schoon.
- Prod gerebuild; live bevestigd dat de bestaande goede mindmap de
  interactieve viewer serveert.

## Restpunt

De 5 kapotte artifacts in prod houden hun oude proza-inhoud — verwijderen
en opnieuw genereren in de UI; de validators bewaken nu het format.
Mermaid-CDN blijft alleen voor de chat-preview; report-pagina's blijven
extern-resource-vrij.
