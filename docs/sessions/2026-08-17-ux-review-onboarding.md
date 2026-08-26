# Sessie 2026-08-17 (avond): UX-review + eerste-gebruik-fixes

## Aanleiding
Ed: "het is best wel ingewikkeld voor gebruikers om ermee te werken" → UX-expert-review
gevraagd, autonoom uitgewerkt met subagents.

## Gedaan
- **Audit** (3 sporen): browser-walkthrough verse instance (:7002, desktop + 360px) +
  frontend-audit (onboarding/empty states/jargon/navigatie/i18n) + config-frictie-audit
  (settings-oppervlak, foutfeedback, multi-user). Volledig rapport:
  `docs/ux-review-2026-08-17.md`.
- **PR #10** (squash → dev, `3c3b9b9`): /setup-frontdeur gerepareerd (default-sub
  `wizard` i.p.v. dispatcher-fallback, na review), bericht blijft bewaard zonder model,
  `_parseErrorBodyMessage` (JSON.parse, node-behavioral getest), llm_core-hint naar
  bestaande tab, Home-kaart "Connect a model"-CTA, mobiele ✕ terug op alle sheets
  (incl. specifiekere settings/brain-regel — reviewvondst), empty states RAG/Notebooks.
- **Code-review** (low, 10 findings): 9 verwerkt, 1 bewust niet (settings-open-helper
  dedup; module-side-effects, zie rapport).
- **Issues #11/#12/#13**: onboarding-wizard, stale default-model na endpoint-delete,
  admin-only-tabs zichtbaar maken.
- CLAUDE.md aangevuld (CI-checks, GPU-compose, runtime-inventaris) — `6cf389e`.

## Verificatie
188 js-area + 4 nieuwe tests groen; browser-smoke op verse instance: /setup en
/setup theme end-to-end, foutpad zonder rauwe JSON, Home-CTA, mobiele sluitknoppen
(Settings/Brain/Notebooks) klik-getest.

## Open / lessen
- Roadmap-items (jargon-tooltips, navigatie-groepering, i18n, floating-window-stapeling)
  staan geprioriteerd in het rapport; issues dekken de drie hoogste.
- Les: `/code-review` zonder target reviewt de werkboom-diff — geef PR-nummer mee.
- Les: verse-instance-smoke op :7001 botste met een oude achterblijvende instance;
  poort checken vóór start.
