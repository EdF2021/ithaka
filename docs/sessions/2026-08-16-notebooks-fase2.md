# Sessie 2026-08-16 — Notebooks Fase 2: tekst-artifacts

## Wat er gebouwd is

Eén-klik-generatie van vijf artifact-soorten (Studiegids, Briefing, FAQ, Quiz, Mindmap) uit
notebook-bronnen:

- **T1** `NotebookArtifact`-datamodel (`core/database.py`), koppeltabel met dubbele
  CASCADE-FK's (notebook + document).
- **T2** Generatiemodule `src/notebook_artifacts.py`: 5 kind-prompts (taal van de bronnen),
  `gather_source_text` met fair-share water-filling-cap (60k, alleen te grote bronnen worden
  geknipt), untrusted-wrap om de bron-payload, think-block-strip op het modelantwoord,
  Document + artifact-row pas ná geslaagde LLM-call.
- **T3** API in `routes/notebook_routes.py`: GET (met `title`-join), POST (400/404/502-mapping
  zonder string-matching), DELETE (artifact + Document), notebook-DELETE ruimt
  artifact-Documents op; FK-cascade-ordering expliciet vanwege SQLAlchemy/SQLite-race.
- **T4** Artifacts-sectie in `static/js/notebooks.js` + CSS (bestaande tokens): kind-knoppen,
  lijst met pill/titel/datum, arm-confirm delete, open-flow naar de document-viewer.

## De twee gates (belangrijkste les)

De spec-ruling "synchrone LLM-call in de request" bleek twee keer te botsen met de
achtergrond-infrastructuur; beide zijn zelf-deadlocks doordat de artifacts-POST als
foreground-request getrackt wordt:

1. `task_llm_call_async` wachtte onvoorwaardelijk op `wait_for_interactive_quiet`; de eigen
   request houdt `_ACTIVE_REQUESTS >= 1` → gate gaat nooit open → 504 op de 45s-hard-timeout.
   Fix: `wait_for_quiet=False`-param + smalle timeout-exemption
   (`POST ^/api/notebooks/[^/]+/artifacts$`, bewust géén breed prefix).
2. `_local_model_slot` liet `workload="background"` op lokale endpoints wachten op
   `has_foreground_activity()` — óók True door de eigen request → ~600s-noodklep per klik.
   Fix: `workload="foreground"` (de gebruiker wacht synchroon).

Gate 2 werd pas in de browser-smoke gevonden: de testsuites mockten precies op de kapotte
grens (route-tests mocken `generate_artifact`, module-tests mocken `task_llm_call_async`).
Er staat nu een regressietest die de echte `task_llm_call_async` binnen
`track_interactive_request` draait (bewezen: faalt zonder fix) plus een kwarg-assertie voor
beide gate-bypasses. Les voor Fase 3: elke "synchrone LLM-call in een request" moet langs
beide gates én de hard-timeout.

## Smoke (verse instance :7001, gemma3:latest via GPU-Ollama :11434)

Alle 5 kinds gegenereerd (57–152s, geen 504, geen gate-stall), lijst nieuwste-eerst met
title-veld, mindmap rendert als mermaid-SVG in Preview, quiz met Antwoorden-sectie (geen
`<details>` — markdown.js forceert die open), delete verwijdert row + Document maar niet de
bron-Documents, mobiel 360px strak. Screenshots in de sessie-chat.

## Proces

Subagent-driven development: 4 taken + eindreview (opus) + 2 fixrondes, alle reviews via
worktree-cherry-pick-flow. Eindreview ving gate 1 met een eigen gate-probe; de smoke ving
gate 2. Spec geamendeerd (sync-ruling, soft-delete-randgeval, quiz-smoke-regel).

Ed de Feber, in nauwe samenwerking met Claude
