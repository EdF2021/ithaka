# Sessie 2026-08-28 — integratiecheck + ui-scale-menufix

## Integraties E2E geprobed (in de app-container, echte tool-calls)

7/8 MCP-servers werkend: memory, filesystem, github (`search_repositories` →
resultaat), browser-playwright, google-drive (echte search), google-calendar
(`list-calendars`), email-imap/smtp (49 tools; teardown-ruis in de probe is
onschuldig). **notion: token 401 "API token is invalid"** — bekend open punt,
nieuw token op notion.so/my-integrations → env van server `af6834c5` →
`POST /api/mcp/servers/af6834c5/reconnect`. ntfy-integratie OK (POST
`http://ntfy/ithaka` → 200).

## Composer-plusmenu viel buiten beeld (PR #76, gemerged)

Rootcause: `:root.ui-scale-125 { zoom: 1.25 }` — `getBoundingClientRect()`
geeft viewport-px, maar JS-gezette `top/left` op het (naar body geportalde)
menu renderen ×1.25 → menu naar rechts-onder, onderste items onbereikbaar.
Fix: coördinaten delen door `menu.currentCSSZoom` in `positionMenu()`
(app.js) + cache-buster-bump. Smoke: 4/4 combinaties (desktop/mobiel ×
100%/125%) groen via chrome-devtools MCP op een :7001-instance.
Zelfde drift zit nog in msg-kebabmenu en export-menu → **issue #77**.

## Lessen

- **Smoke-instance op :7001 erft `CHROMADB_*` uit `.env`** en raakt dan de
  gedeelde chromadb (embedding-lane-change → collection-recreate!). Altijd
  `CHROMADB_PORT` naar een dood poortnummer zetten voor een geïsoleerde run.
  (Dit keer op tijd gestopt; `ithaka_rag_fastembed` intact, 7642 docs.)
- Prod-login heeft TOTP → UI-verificatie op prod loopt via Ed's eigen
  browser; code-identieke smoke op :7001 is het bewijs vooraf.

## Overig

- CLAUDE.md geactualiseerd via `/init`-audit (STT-kaart terug, npx-MCP-
  cachegotcha) — commit `28e0a66`.
