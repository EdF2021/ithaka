# Sessielog 2026-09-04 — Realtime fase 2 (ask_ithaka), STT-probe-fix, infographic-v2-smoke

Vervolg op `2026-09-03-realtime-voice-mode-fase1.md`. Ed's opdracht: de drie open punten
zelfstandig afwerken (subagents op sonnet, eindreview op opus, regie centraal).

## 0. Prod-bug van gisternacht: geen codebug

`realtime_enabled` op prod stond vanochtend gewoon op `true`; prod-logs tonen drie geslaagde
Realtime-sessies (08:01/08:12/08:30 UTC, `client_secrets` 200). Waarschijnlijk provider gekozen
terwijl de toggle nog uit stond. Gesloten.

## 1. Stash `wip-stt-realtime-whisper-default` → droppen; PR #166

- Onderzoek (docs + live probe op `:7001` met Eds sleutel): `gpt-realtime-whisper` is
  **alleen** geldig in Realtime-transcriptiesessies; `POST /v1/audio/transcriptions` geeft
  `404 model_not_found`. Geaccepteerd door dat endpoint: `gpt-transcribe` (nieuw, aanbevolen
  default, WER 19% vs 40% whisper-1), `gpt-4o-mini-transcribe`, `whisper-1`.
- Prod heeft `stt_model: whisper-1` expliciet gezet → de stash (fallback-default wijzigen)
  had sowieso geen effect. **Actie Ed:** `git stash drop` van die entry; optioneel in
  Settings → STT het model op `gpt-transcribe` zetten (probe accepteert het).
- Bijvangst: de STT-probe meldde bij die 404 "no /audio/transcriptions route" — precies wat Ed
  gisteren op het verkeerde been zette. **PR #166** (issue #165, sonnet-agent in eigen
  worktree): 404 met `error.code == model_not_found` → "model '<x>' not found … check the STT
  model name". 2 nieuwe tests, 12/12 groen. Geen UI-surface → smoke N/A (alleen een
  foutmelding-string in een backend-probe).

## 2. Infographic v2 (#161): illustratiejob live geverifieerd

`feat/infographic-v2` gemerged met `origin/dev` (cd600d8, gepusht), smoke op `:7001` met echte
OpenAI-key, `image_gen_enabled=true`, auto-detect `gpt-image-1.5`:
job `running` → `done` in 15 s, `images/generations` 200, PNG 855 KB owner-gated (200 met
cookie / 401 zonder), `illustrations`-map gepersisteerd in de raw artifact-JSON, hero-`<img>`
gerenderd op 1280 px en 360 px (geen overflow), console leeg. Output als PR-comment + PR-body
bijgewerkt. Alle CI-checks groen. **Merge zelf geblokkeerd door de auto-mode-classifier** —
Ed voert `gh pr merge 161 --squash …` uit (commando staat in de chat), daarna deploy.

## 3. Realtime fase 2 — tool-calling via `ask_ithaka` (branch `feat/realtime-voice-tools`)

Spec `docs/superpowers/specs/2026-09-04-realtime-voice-tools-design.md`, plan
`docs/superpowers/plans/2026-09-04-realtime-voice-tools.md`, SDD-ledger
`.superpowers/sdd/2026-09-04-realtime-voice-tools/progress.md`.

Ontwerp: één function-tool in de Realtime-sessie; de browser stuurt de vraag naar
`POST /api/realtime/ask`; `services/realtime/realtime_ask.py::answer_question` draait de vraag
one-shot door `stream_agent_loop` (alle tools/MCP/RAG) op de task→utility→default-modelketen
(`resolve_task_candidates`), 60 s timeout, ≤ 1500 tekens, Nederlands platte-tekst-prompt.
Preamble ("Momentje, ik zoek het op.") zit in de tool-description. Toggle
`realtime_tools_enabled` (globaal, default aan) in de Realtime-kaart.

| Taak | Commit | Review |
|---|---|---|
| 1 setting + `ASK_ITHAKA_TOOL` in `build_session_config` | 2875919 | approved |
| 2 `answer_question` | 8bd33fc | approved |
| 3 `POST /api/realtime/ask` | 88ee0dc | approved |
| 4 browser: `function_call` → fetch → `function_call_output` + `response.create`; toggle | a88afa7, fix e974384 | 1 Important gefixt (`response_done` overschreef `tool`-state) |
| 5 docs | 75232a4 | controller |

Eindreview (opus): **FIX FIRST** — C1: de 45 s `REQUEST_HARD_TIMEOUT`-middleware kwam vóór de
60 s ask-timeout en de Engelse middleware-string zou worden uitgesproken → `/api/realtime/ask`
in `_TIMEOUT_EXEMPT_PREFIXES`. I1: `speech_started` overschreef de `tool`-state; plus
`response.create`-collision met een server-gestarte response → `_responseActive` /
`_pendingResponseCreate`. I2: spec-claim over "niet goedgekeurd" was onjuist → tekst
gecorrigeerd, tool-aanroepen server-side gelogd (bewuste scope-keuze: geen extra
approval-gate). I3: ontbrekende `event.name` → default `ask_ithaka`. I4: `_handleFunctionCall`
ongetest → 7 Node-tests met gestubde `fetch`/data-channel. Alles in één fix-golf
(22fafbb, 23590c5, fe3c1b3); scoped re-review: alle vijf ADDRESSED, geen nieuwe schade,
**MERGEABLE**. 8 Minor geparkeerd (o.a. Engelse `body must be a JSON object`, geen
`AbortController`, `call_id` onbegrensd in de logregel).

Tests: focus-set 51 groen; volledige suite op e974384: 5753 passed / 3 skipped / 0 failed.

Smoke `:7001` (echte key, branch-HEAD):
- mint `POST /api/realtime/session` → 200 (OpenAI accepteert het tool-schema);
- `POST /api/realtime/ask` "Wat is de hoofdstad van Australië…" → `{"answer":"De hoofdstad van
  Australië is Canberra. …"}` in 2,1 s; lege vraag 400; tools uit 400 "Realtime-tools staan
  uit"; zonder cookie 401;
- browser (chrome-devtools, ingelogd): gesimuleerde `response.created` →
  `response.function_call_arguments.done` → `response.done` over een fake data-channel →
  state `listening → tool → listening`, verstuurd `conversation.item.create{function_call_output,
  output:'{"answer":…}'}` + `response.create`, transcriptregel "Opgezocht via Ithaka: …",
  console leeg;
- Settings-kaart: toggle "Tools (ask_ithaka)" zichtbaar in de Realtime-kaart, aan/uit-roundtrip
  → `realtime_tools_enabled` false → true op de server, "Saved".

**Nog niet gedaan (mic vereist):** echte spraak-smoke met barge-in tijdens een lookup — I1's
collision-pad is nu code-matig afgedekt maar niet live bewezen. Ed test dit na deploy.

## Open / follow-ups

- Ed: `gh pr merge 161`, stash droppen, PR #166 en de fase-2-PR mergen + `docker compose up -d
  --build`; daarna live spraak-smoke fase 2 (vraag met opzoekwerk; praat erdoorheen).
- Fase 2 Minor-lijst in `.superpowers/sdd/2026-09-04-realtime-voice-tools/final-review-report.md`.
- Zichtbare tool-trail in het Realtime-transcript / aparte Realtime-tool-policy (I2).
