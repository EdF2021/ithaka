# Sessie 2026-07-30 — dashboard, MCP-catalogus, skills-audit, plugins

## Wat er gebeurd is

Vier onderwerpen uit één brainstorm autonoom uitgewerkt (spec → bouw → smoke),
met vier verken-subagents en drie bouw-subagents onder regie. Acht commits op
`dev` (3948344..1946d4d). Volle suite: **4594 passed, 3 skipped** (+28 tests).

1. **Dashboard-startpagina** (`feat(dashboard)` 227812f + fix 0f45d1a):
   frontend-only Home-module (tasks.js-patroon) met 5 widgets op bestaande
   API's (calendar/tasks/unread-state/sessions/models), Promise.allSettled met
   per-widget degradatie, quick actions, pref `dashboard_autoopen` (default
   aan), `/dashboard`-deep-link (backend-route ontbrak — toegevoegd).
2. **MCP-connector-catalogus** (`feat(mcp)` 1adabb7): dode `MCP_PRESETS` in
   admin.js (select bestond niet in DOM) gemigreerd naar `src/mcp_presets.py`
   (15 stdio + 2 hosted-HTTP), `GET /api/mcp/presets` (admin), preset-picker
   in het settings-MCP-formulier.
3. **Skills**: geen bouw — audit (`docs/skills-audit.md`, d294862). Bestaat al
   end-to-end en veiliger dan Claude Code (untrusted-context-scheiding); gap =
   distributie → plugins.
4. **Plugins** (`feat(plugins)` 1742830 + fix 1946d4d): zip-bundel
   (plugin.json + skills/ + mcp.json), `PluginManager` (zip-slip/size-caps,
   slug-validatie), admin-routes, beheer-UI in settings. MCP-servers uit
   plugins altijd `is_enabled=False`.

## Verificatie

- Volle pytest-suite groen; per feature focused slices door de bouwers.
- Browser-smoke op localhost:7000 (docker, 2× herbouwd): dashboard auto-open
  met echte data, smal-layout (380px) stapelt correct, preset-picker vult
  GitHub-preset in, demo-plugin `demo-reis` via UI-upload geïnstalleerd →
  skill `/reisplanner` in slash-catalog, MCP-rij disabled. Screenshots in chat.

## Gevonden door smoke (niet door tests)

- `/dashboard`-URL gaf backend-404: SPA-deep-links zijn expliciete routes in
  app.py (`/notes`, `/calendar`, …) — nieuw pad daar ook registreren.
- Plugin-skill zonder `status:`-frontmatter bleef **draft** en kwam nooit in
  prompt-index/slash-catalog. Testfixture had toevallig `status: published`.
  Fix: PluginManager publiceert na import (admin-install = goedkeuring) +
  regressietest. Les: pytest-groen ≠ feature werkt; echte bundel testen.

## Valkuilen voor herhaling

- Settings-panel (`settings-panels`) scrollt niet met muiswiel op
  modal-body-coördinaten; `scrollIntoView` via JS werkt ook niet altijd —
  secties zoals "MCP Servers"/"Plugins" zitten onder Integrations → Add
  Integration → MCP Tool Server resp. Agent Tools (onderaan).
- `resize_window` (claude-in-chrome) rapporteert succes maar een
  gemaximaliseerd Windows-venster verandert niet echt van maat; responsive
  testen door de modal-container zelf te knijpen.
- Slash-catalog response-vorm is `{"skills": [...]}` (niet `entries`).
- Demo-plugin `demo-reis` staat nog geïnstalleerd (bewust, als voorbeeld);
  weghalen kan via Settings → Agent Tools → Plugins of
  `DELETE /api/plugins/demo-reis`.

Ed de Feber, in nauwe samenwerking met Claude
