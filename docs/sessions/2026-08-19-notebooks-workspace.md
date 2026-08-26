# Sessielog 2026-08-19 — Notebooks-werkruimte (NotebookLM-stijl 3-panelen)

**Resultaat:** PR #21 squash-gemerged naar `dev` (`8008111`). Volledige werkruimte live: Bronnen (checkbox-selectie + werkend retrievalfilter) | Gesprek (gegronde chat, sessieswitcher, vervolgvraag-chips) | Studio (artifacts + podcast, uit oude detail-view verhuisd). Mobiel ≤700px via tabs.

## Proces

Subagent-driven development over het 8-tasks-plan (`docs/superpowers/plans/2026-08-19-notebooks-workspace.md`, spec in `docs/superpowers/specs/`): per task een verse implementer + task-review + zo nodig fixrondes met scoped re-review, afgesloten met whole-branch review (zwaarste model) + integratie-smoke. Fixrondes: T1 ×1 (invariant-test), T3 ×1 (collapse-roundtrip + fail-closed open), T5 ×2 (error-bubble-detectie → semantische `chat-error`-marker), T6 ×1 (stale artifact-error), finale wave ×1 (2 Escape-guards + mobiele topbar-wrap). T2/T4/T7 clean bij eerste review.

## Belangrijkste vangsten

- **FormData-gap (T4):** `chat_stream` las `source_ids` alleen uit JSON-body; de browser stuurt FormData — het filter werd in élke echte send stil gedropt. Gefixt met form-field-fallback (attachments-patroon), mutation-checked test.
- **Whole-branch-seams (final review):** Escape sloot de werkruimte achter de open doc-viewer (`body.doc-view` niet gegaurd) en tijdens typen in de composer — precies wat alleen de brede blik ziet (T3-Escape × T6-viewer-ruling).
- **Smoke-les mobiel:** chrome-devtools `resize_page` komt niet onder ~500px; alleen `emulate({viewport:"360x740x2,mobile,touch"})` geeft echte 360px. Daardoor pas in de integratie-smoke gevonden: topbar-overflow → Studio-tab onbereikbaar (gefixt: topbar wrapt naar 2 rijen, `--nbws-topbar-h` 84px mobiel).
- **suggest_questions-timeout (8s)** te krap voor 14b/20b lokale modellen; utility-model op klein model zetten in smoke-envs.

## Architectuurkeuzes (rulings)

- CSS-primair i.p.v. reparenting: `#chat-container`-DOM wordt nooit verplaatst; werkruimte = body-class + fixed panelen.
- z-index-lagen: werkruimte 10005, doc-viewer 10010 (blijft boven), topbar 1; overige modals onder de werkruimte geaccepteerd.
- Semantische `chat-error`-class op de drie chat.js-error-rendersites i.p.v. tekst/kleur-heuristiek.
- Close-hook-registry in `notebookWorkspace.js` (podcast-poll-stop), `_openEpoch`-guards op elk await-pad.

## Verificatie

Volle suite eindstand 4908 passed / 3 skipped; `node --check` clean (chat.js, chatStream.js, notebookWorkspace.js, notebooks.js); CI 7/7 groen; browser-smoke 10/10 PASS na fix-wave (desktop 1280px + echte 360px), volledige log stond in de sessie-chat.

## Follow-ups

- Issue #22: pre-existing first-send-grounding-bypass bij niet-gematerialiseerde sessie.
- Klein: NL/EN-mix in studio-labels (uit oude detail-view), dode detail-view-CSS (style.css ~41058-41113), chips-strip `bottom:92px`-benadering.

Ed de Feber, in nauwe samenwerking met Claude
