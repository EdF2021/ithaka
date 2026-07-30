# Design: dashboard-startpagina, MCP-catalogus, skills-audit, plugins

Datum: 2026-07-30 · Branch: `dev` · Status: in uitvoering (autonome opdracht Ed)

Vier onderwerpen uit één brainstorm. Volgorde: 1→2 parallel, daarna 4, daarna 3 (audit).

## 1. Dashboard-startpagina (nieuwbouw, frontend-only)

**Doel:** één overzicht bij openen: vandaag (agenda), automatiseringen, ongelezen mail,
recente sessies, modelstatus, quick actions.

**Architectuur — géén nieuwe backend.** Alle data bestaat al; de dashboard-module
fetcht parallel:
- `GET /api/calendar/events?start=today&end=tomorrow` — agenda vandaag
- `GET /api/tasks?status=active` — automatiseringen (ScheduledTask; toon `next_run`)
- `GET /api/email/unread-state` — goedkope unread-count (index-based)
- `GET /api/sessions` — recente sessies (sort `last_message_at`, top 6)
- `GET /api/models` — user-scoped modellenlijst (30s cache) → count + endpointnamen
Elke widget degradeert zelfstandig (fetch-fout → kaart toont "niet beschikbaar", rest blijft werken).

**Frontend:**
- `static/js/dashboard.js` — nieuw, patroon gespiegeld aan `tasks.js` (modal, draggable,
  `openDashboard/closeDashboard/isDashboardOpen`), registratie in `modalManager.js`.
- Sidebar-item `tool-dashboard-btn` ("Home", inline SVG, boven Chat-tools) in
  `static/index.html`; klik-wiring + `'/dashboard'` in `_routeOpen` (`static/app.js`).
- Auto-open bij load als user-pref `dashboard_autoopen` (default **aan**; toggle in het
  dashboard zelf, opgeslagen via `GET/PUT /api/prefs/dashboard_autoopen`).
- Stijl: bestaande vars (`--panel`, `--border`, `--green`, `--red`), mono, inline SVG,
  geen emoji, responsive (mobiel: kaarten stapelen). CSS in `static/style.css`.
- Widget-klik navigeert: sessie → sessie openen; mail-kaart → email-module; agenda → calendar; enz.

**Tests:** `node --check` op gewijzigde JS; pytest-regressietest die verifieert dat
`index.html` het nav-item bevat en `dashboard.js` de vijf endpoints aanroept (string-level,
zoals bestaande frontend-regressietests). Browser-smoke verplicht (desktop + smal viewport).

## 2. MCP-connector-catalogus (fix + uitbreiding)

**Probleem:** presets bestaan als dead code (`static/js/admin.js:1761` `MCP_PRESETS`,
select `#adm-mcpPreset` bestaat niet in de DOM); het complete MCP-formulier
(`settings.js`, wél HTTP-transport) heeft géén presets.

**Ontwerp:**
- `src/mcp_presets.py` — serverzijde catalogus: lijst dicts
  `{id, name, transport, command?, args?, env?, url?, oauth?, help, tags}`.
  Seed: de 14 presets uit admin.js + `transport`-veld + 2-3 hosted-HTTP-voorbeelden
  (bv. GitHub remote MCP). Env-values blijven placeholders (`<TOKEN>`), nooit secrets.
- `GET /api/mcp/presets` in `routes/mcp_routes.py` (admin-only, zoals de rest).
- `settings.js` MCP-formulier: preset-dropdown bovenaan; keuze vult transport/command/
  args/env/url in (velden blijven editbaar). Dead code + `MCP_PRESETS` uit admin.js verwijderd.

**Tests:** route-test (presets-endpoint vorm + admin-gate, patroon
`test_session_list_owner_scope.py`); validatietest dat elke preset een geldig
transport/velden-combinatie heeft; `node --check`.

## 3. Skills (audit — bestaat al end-to-end)

Bestaand: `services/memory/skills.py` (+format/extractor/importer),
`routes/skills_routes.py` (1662r incl. slash-catalog, invoke, test, builtin-overrides),
prompt-injectie in agent_loop (Jaccard-match + index, untrusted-context-scheiding),
`manage_skills`-tool, Brain-UI met GitHub/skills.sh-import, per-user prefs.

**Deliverable:** gap-audit t.o.v. Claude Code-skills in `docs/skills-audit.md`:
wat is er, hoe gebruik je het, welke gaps (bv. frontmatter-velden, argumenten bij
slash-invocatie, skill-versionering) + advies. Geen code tenzij de audit een kleine,
veilige quick-win vindt. Plugins (§4) dekken de distributie-gap.

## 4. Plugins (nieuwbouw op bestaande infra)

**Doel:** één installeerbaar bundel-formaat à la Claude Code-plugins: skills + MCP-servers
+ (later) commands in één map/zip.

**Formaat** (`<naam>.zip` of map):
```
plugin.json    # {"name", "version", "description", "author?"}
skills/<skill-naam>/SKILL.md   # 0..n skills (bestaand formaat)
mcp.json       # optioneel: [{name, transport, command|url, args, env}] — servers
```

**Backend:** `services/plugins/manager.py` — `PluginManager`:
- `install(zip_bytes)` → valideer (padtraversal-veilig uitpakken, `plugin.json` verplicht,
  size-cap), plaats onder `DATA_DIR/plugins/<name>/`, registreer skills via bestaande
  `SkillsManager`-import (categorie `plugin-<name>`), voeg MCP-servers toe **disabled**
  (admin moet expliciet enablen; hergebruik `McpServer`-DB-model, id-prefix `plugin_<name>_`).
- `uninstall(name)` → skills + MCP-rijen + map weg. `list()` → naam/versie/inhoud/status.
- `routes/plugin_routes.py` — `setup_plugin_routes(...)`, admin-only:
  `GET /api/plugins`, `POST /api/plugins/install` (multipart zip), `DELETE /api/plugins/{name}`.
  Wiring in `app.py` volgens `setup_*_routes`-patroon.
- Constants: `PLUGINS_DIR` in `src/constants.py` (guarded mkdir).

**Frontend:** beheer-sectie in `settings.js` (admin): lijst, zip-upload, verwijderen,
per-plugin inhoud (skills/servers) tonen. Geen marketplace in deze fase (YAGNI).

**Security:** zip-slip-preventie, size-cap (10MB), MCP-servers nooit auto-enabled,
skills gaan door de bestaande untrusted-context-behandeling.

**Tests:** unit (install/uninstall/validatie incl. zip-slip), route-test admin-gate,
integratie: geïnstalleerde plugin-skill verschijnt in skills-index.

## Uitvoeringsplan

- Wave 1 (parallel): A=dashboard (frontend), B=MCP-catalogus. Disjuncte bestanden
  (A: index.html/app.js/dashboard.js/style.css/modalManager.js · B: mcp_presets.py/
  mcp_routes.py/settings.js/admin.js).
- Wave 2: C=plugins (backend+settings.js — ná B wegens settings.js), D=skills-audit (doc).
- Elke wave: focused pytest-slice + `node --check`; na afloop volle suite + browser-smoke
  + screenshots; commits per onderwerp (Conventional Commits); sessielog.
- Agent-regels: geen `git stash`; geen commits door subagents (ik commit).
