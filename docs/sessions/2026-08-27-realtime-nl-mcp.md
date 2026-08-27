# 2026-08-27 (ochtend) — Realtime NL-chat + MCP-koppelingen gerepareerd

Vervolg op de voice-mode-fix van vannacht (zie `2026-08-27-voice-mode-fix.md`).
Doel van Ed: (1) een model koppelen dat realtime kan chatten, met Nederlands in de
prompt; (2) Gmail/Drive/Calendar en alle andere MCP-koppelingen repareren; (3) de
STT-settings-kaart terugbrengen.

## Realtime chatmodel + Nederlands

- `gemini-2.5-flash` bleek **gesunset** ("no longer available to new users"); het
  eerder door Ed gekozen `gemini-2.5-flash-native-audio-latest` is een Live-API-model
  dat elke chat/completions-call 404't. Nieuwe default: **`models/gemini-3.5-flash-lite`**
  (0.6-0.8s round-trip; `gemini-3.5-flash` denkt ~20s, `3.7-flash` gaf 503 high demand).
  Fallback-keten: claude-opus-4-8 → gemma4:latest. Let op: default_model moet het
  **`models/`-geprefixte** canonieke id zijn, anders matcht de frontend hem niet.
- Nieuw `response_language`-setting (PR #71): staat op "Nederlands" — onvoorwaardelijke
  system-message op elke chat/agent/voice-beurt (buiten de memory/incognito-gates, dus
  ook op korte voice-utterances).
- Model-picker filtert `native-audio`-modellen weg (PR #71); Ed's vastgelopen sessie is
  omgezet naar flash-lite.
- Live bewezen: Nederlandse hands-free voice-loop in 10.5s totaal, antwoord in 0.62s
  servertijd ("De hoofdstad van Nederland is Amsterdam.").

## MCP-reparaties (PR #70 + runtime)

- **Drive ("Connection closed")**: corrupte `_npx`-cache-entry (install gekild door het
  20s-startup-budget na rebuild; entry zonder bin → `sh: 1: google-drive-mcp: not found`).
  Entry verwijderd + herinstallatie; structureel: **npm-cache op de data-volume**
  (`./data/npm-cache:/app/.npm`, alle drie compose-varianten) → issue #64 dicht.
- **Calendar (lege tool-call-errors na reconnect)**: issue #61 — stdio-sessie hing aan de
  cancel-scope van de request-task. Gefixt: sessie-lifecycle in een eigen runner-task
  (`_StdioSessionHandle`); `/reconnect` levert nu gezonde sessies. Live bewezen:
  reconnect → directe list-events-call OK.
- OAuth-refresh-grants van Drive én Calendar waren al gezond (probe: beide OK) — nooit
  onnodig re-authorizen; het waren proces/cache-problemen.
- **GitHub-MCP**: droeg een token van een ander account (`viktorcuypers`); vervangen door
  Ed's eigen gh-token (out-of-band geverifieerd als EdF2021) via de env-kolom + reconnect.
- **Gmail**: geen MCP-server maar de ingebouwde e-mail (IMAP/SMTP) — werkt (volledige
  Gmail-mappenlijst live opgehaald; INBOX is echt leeg). Gmail-MCP-preset kan desgewenst
  nog via in-app OAuth worden toegevoegd.
- **Notion**: server verbindt maar het API-token is **401 unauthorized** → Ed moet een
  nieuw token maken (notion.so/my-integrations) en in de server-env zetten.
- Dubbele `browser-(playwright)`-rij verwijderd.
- E2E-verificaties met echte data: Calendar (fysio 3 sep; Ajax–Sion 27 aug), Drive
  (TestFile.txt e.a.), GitHub-search, Gmail-mappen.

## STT-settings-kaart (PR #71)

Zichtbare kaart in Settings → AI Defaults met de ids die `initSttSettings` verwacht;
toont live serverstate (OpenAI (API) / whisper-1). Desktop + 360px geverifieerd.

## Open punten

1. **Notion-token vernieuwen** (alleen Ed kan dit) → daarna `POST /api/mcp/servers/{id}/reconnect`.
2. Optioneel: Gmail-MCP-preset autoriseren (in-app OAuth) als agent-toegang tot mail via
   MCP gewenst is naast de ingebouwde e-mail.
3. Model-picker toont native-audio-modellen nog uit de oude `cached_models`-cache tot een
   picker-refresh; filter werkt bij eerstvolgende model-refresh.
