# Sessielog 2026-07-31 — Dageraad-port (gekozen UX-richting → echte app)

Vervolg op 2026-07-30 (dashboard/MCP/skills/plugins + UX-mockuprondes). Ed koos mockup-8
"Dageraad" (docs/design/dageraad-mockup.html + -brief.md); deze sessie is de port naar de
echte app, in drie fasen, elk met eigen commit + browser-smoke op :7000.

## Fase 1 — thema (06936c0)

- Preset `dageraad` in `THEMES` (static/js/theme.js): nachtblauw `#0A1220`, amber
  `#F0A868` (brand/send/toggle), roze `#D77A8C` als red-token; advanced tokens voor
  bubbles/sidebar/composer/code.
- Ambient `bg-pattern-dageraad-gloed`: CSS-only (twee fixed radial-gradients vanaf de
  onderrand), geregistreerd als statisch pattern, default voor het thema, option in
  `#theme-bg-pattern-select`.
- `<html data-theme="...">` gespiegeld bij elke theme-apply én in het first-paint-script
  in index.html (geen FOUC) — de scoping-hook voor fase 2/3.

## Fase 2 — layout-polish (6c99152)

- Alles gescoped op `:root[data-theme="dageraad"]` (blok onderaan style.css); default UI
  pixel-identiek (geverifieerd: dark = 16px radius / 10px 12px padding).
- Berichten 16px/1.65 + padding 14px 18px; composer radius 18 + amber focus-glow; modals
  radius 18/padding 16 **desktop-only** (`@media (min-width: 769px)`) — anders sloopt de
  hogere specificiteit van de attribute-selector de mobiele bottom-sheet (flat top,
  safe-area-padding). Dashboard-kaarten radius 14 + oplicht-hover (400ms, geen transforms).
- Dashboard-header toont tijdsafhankelijke begroeting (Goedenacht/-morgen/-middag/-avond)
  alleen onder Dageraad; helper `_dashboardGreeting(hour)` puur/testbaar.
- Globale fix (alle thema's): `.agent-controls`/`.agent-progress`/`.workflow-info`
  gebruikten hardcoded lichte hexes → theme-vars/color-mix.
- Bekende keuze: Dageraad-`.msg`-padding wint van `density-compact` (gedocumenteerd in CSS).

## Fase 3 — intro-choreografie (f45fe7a)

- `static/js/dageraadIntro.js`: `maybePlayDageraadIntro()` (gates: thema, reduced-motion,
  eenmaal per load) + `playDageraadIntro()`. Fasen via CSS-classes + setTimeouts:
  warm (horizon-gloed stijgt) → titel ("Ithaka" / "Yours for the voyage.") → FLIP-morph
  naar `.sidebar-brand-title` (fade-fallback als onzichtbaar, bv. mobiel) → reveal
  (rise-in van rail/sidebar/chat via body-class, daarna opgeruimd).
- Skip: klik of Escape → direct eindstaat. Replay-knop in theme-popup, alleen zichtbaar
  onder Dageraad. `prefers-reduced-motion`: JS-check + CSS `display:none`-vangnet.
- Wiring: één try/catch-call vroeg in `startIthakaApp()` (app.js) — kan sessie-load nooit
  blokkeren.

## Verificatie

- Per fase docker rebuild + smoke op :7000 (chrome-devtools-mcp, sessie-token-cookie):
  theme-picker rendert dageraad automatisch; tokens/pattern/data-theme kloppen; wisselen
  dark↔dageraad ruimt op; mobiel 390px zonder regressies; intro speelt/skipt/replays.
  Fase-3-bouwer verifieerde daarnaast zelf op een geïsoleerde instance (:7002) incl.
  formule-exacte FLIP-check en echte CDP-Escape.
- Volledige suite na afloop: **4615 passed, 3 skipped** (was 4594; +21 dageraad-tests in
  tests/test_dageraad_theme.py).

## Werkwijze (wat werkte)

- Verkenner (sonnet) → precieze bouw-briefs → één builder (sonnet) per fase, sequentieel
  omdat alle fasen style.css raken; regie/smoke/commits zelf.
- Smoke-truc: intro is korter (2,4s) dan de MCP-screenshot-roundtrip; titelfase vastgelegd
  door de fase-timers tijdelijk te bevriezen (setTimeout-patch in page-context) en de
  phase-classes handmatig te zetten — daarna echte klik-skip als cleanup.

## Open punten

- Ollama-subscription-cloudmodellen: wachten op Ed's upgrade op ollama.com
  (recept: `docker exec ollama ollama pull <model>:cloud` + endpoint-refresh).
- Evt. vervolg-polish: muis-parallax op de gloed (bewust weggelaten — body-background
  kan niet goedkoop transformen), welcome-screen-typografie onder Dageraad.
