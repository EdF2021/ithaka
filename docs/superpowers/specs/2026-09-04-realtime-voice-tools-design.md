# Realtime-gesprek fase 2 — tool-calling via `ask_ithaka`

Datum: 2026-09-04. Bouwt voort op fase 1
(`docs/superpowers/specs/2026-09-03-realtime-voice-mode-design.md`, gemerged als PR #162).

## Probleem

De Realtime-sessie (OpenAI Realtime API over WebRTC) draait met `tools: []`. Het model kan
niets opzoeken: geen web search, geen notities/agenda/mail, geen notebook-RAG, geen MCP.
Ed's instructie "schakel over op /model kimi-k3 als je een taak niet kunt uitvoeren" in
`realtime_instructions` doet daarom niets — het Realtime-model kan niet van model wisselen en
heeft geen handen. Elke feitelijke vraag wordt uit het geheugen van `gpt-realtime-2.1-mini`
beantwoord of afgewimpeld.

## Ontwerp in één zin

Eén generiek function-tool `ask_ithaka(question)` in de Realtime-sessie; de browser stuurt de
vraag naar een nieuw backend-endpoint dat de vraag one-shot door Ithaka's normale agent-loop
haalt (met alle tools, MCP en RAG, op het utility-/task-model), en geeft het tekstantwoord terug
aan het Realtime-model, dat het uitspreekt.

Waarom één tool en niet Ithaka's hele tool-set in de sessie: de Realtime-sessie is
latency-gevoelig en heeft een beperkt instructie+tools-budget; de agent-loop heeft al
tool-selectie (RAG-based `tool_index`), tool-policy, MCP en fallback-ketens. Eén delegatie-tool
hergebruikt dat alles zonder duplicatie en zonder dat het spraakmodel tool-schema's hoeft te
begrijpen.

## Architectuur

```
browser (realtimeVoice.js)                          Ithaka backend
 ├─ data-channel event                              ├─ POST /api/realtime/session
 │  response.function_call_arguments.done           │    build_session_config() → tools: [ask_ithaka]
 │  {name:"ask_ithaka", call_id, arguments}         │
 ├─ POST /api/realtime/ask {question, call_id} ───▶ ├─ routes/realtime_routes.py
 │                                                  │    owner = effective_user(request)
 │                                                  │    answer = await answer_question(question, owner)
 │  ◀──────────────── {answer} ─────────────────────┤      services/realtime/realtime_ask.py
 ├─ dc.send conversation.item.create                │        resolve_task_candidates(owner=owner)
 │    {type:function_call_output, call_id, output}  │        stream_agent_loop(...) → delta's concat
 └─ dc.send response.create                         │        asyncio.wait_for(…, ASK_TIMEOUT_S)
```

### Backend

**`services/realtime/realtime_service.py` — `build_session_config(settings)`**

- Nieuwe setting `realtime_tools_enabled` (bool, default `True`, globaal, niet per-user —
  zelfde regime als de andere `realtime_*`-keys).
- Als `True`: `tools = [ASK_ITHAKA_TOOL]`, `tool_choice = "auto"`. Anders `tools: []` zoals nu.
- `ASK_ITHAKA_TOOL` is een module-constante (zuiver data, testbaar):

```json
{
  "type": "function",
  "name": "ask_ithaka",
  "description": "Stel een vraag aan Ithaka, de assistent met toegang tot internet-zoeken, notities, agenda, e-mail, documenten en andere tools. Gebruik dit voor elke vraag die actuele feiten, persoonlijke gegevens van de gebruiker of opzoekwerk vereist — gok niet. Zeg vóór de aanroep één korte zin zoals 'Momentje, ik zoek het op.' Vat het antwoord daarna kort samen in het Nederlands.",
  "parameters": {
    "type": "object",
    "properties": {
      "question": {
        "type": "string",
        "description": "De volledige, zelfstandig begrijpelijke vraag in het Nederlands, inclusief context uit het gesprek."
      }
    },
    "required": ["question"]
  }
}
```

De preamble-instructie ("Momentje, ik zoek het op") zit in de tool-description, niet in
`realtime_instructions` — bestaande gebruikers hebben die instructies al aangepast; de
tool-description reist met de tool mee. Dit is het door OpenAI gedocumenteerde patroon
(realtime-models-prompting: preamble-zin in de tool-description).

**`services/realtime/realtime_ask.py` — nieuw**

```python
ASK_TIMEOUT_S = 60.0
ASK_MAX_ROUNDS = 6
ASK_MAX_CHARS = 1500

async def answer_question(question: str, owner: str | None) -> str
```

- Bouwt `messages = [system, user]`. Systeemprompt: Nederlands, beknopt (≤ 80 woorden),
  platte tekst zonder markdown/opsommingstekens/links (het wordt uitgesproken), geen
  denkstappen, bij tools: resultaat samenvatten, niet citeren. Embedt `DUTCH_OUTPUT_RULE` uit
  `src/notebook_language.py` niet — dat is een notebook-regel; de Realtime-prompt heeft zijn
  eigen, kortere Nederlands-regel.
- Model: `resolve_task_candidates(owner=owner)` (`src/task_endpoint.py`) → eerste kandidaat
  is `(url, model, headers)`, de rest is `fallbacks`. Dit is de hidden-model-cascade
  (Background Tasks → Utility → Default), precies wat Ed met "kimi-k3" bedoelt: niet het
  live chat-model, wel een model met tools.
- Loopt `stream_agent_loop(endpoint_url, model, messages, headers=headers, session_id=None,
  owner=owner, max_rounds=ASK_MAX_ROUNDS, fallbacks=fallbacks)` — `workload` op zijn default
  `"foreground"` laten (nooit `"background"`: self-deadlock op de workload-gate, zie CLAUDE.md
  Notebooks-gotcha). Verzamelt `delta`-chunks die geen thinking zijn (zelfde parse als
  `src/task_scheduler.py::_run_agent_loop`), negeert `tool_start`/`tool_output`/`metrics`.
- Hele call in `asyncio.wait_for(..., ASK_TIMEOUT_S)`. Timeout → `RuntimeError("Het opzoeken
  duurde te lang")`. Leeg resultaat → `RuntimeError("Ithaka gaf geen antwoord")`.
- Resultaat: whitespace-genormaliseerd, afgekapt op `ASK_MAX_CHARS` met "…".
- Geen chat-sessie/DB-rij: de vraag+antwoord verschijnen niet in de chatgeschiedenis (fase 1
  bewaart het Realtime-transcript ook alleen in de DOM).

**`routes/realtime_routes.py` — `POST /api/realtime/ask`**

- Body `{"question": str, "call_id": str (optioneel, alleen voor logging)}`.
- `owner = effective_user(request)` (`src/auth_helpers.py`); route zit achter de globale
  `AuthMiddleware` zoals `/api/realtime/session`.
- Validatie: `question` string, gestript 1–2000 tekens, anders 400.
- Guard: `realtime_enabled` én `realtime_tools_enabled` moeten aan staan, anders 400
  "Realtime-tools staan uit".
- `answer_question` → `{"answer": str}`. `RuntimeError`/`ValueError` → 400 met de Nederlandse
  boodschap; overige → 500 generiek (geen interne details naar de client).
- Passieve-lijst (`src/interactive_gate.py::_PASSIVE_PATTERNS`): **niet** toevoegen — dit is
  een echte foreground-interactie.

### Frontend (`static/js/realtimeVoice.js`)

- `classifyRealtimeEvent`: nieuw geval `response.function_call_arguments.done` →
  `{type: "function_call", name, callId: event.call_id, arguments: event.arguments}`.
- Nieuwe zuivere helper (geëxporteerd, Node-testbaar):
  `buildFunctionCallOutputEvents(callId, output)` → `[{type:"conversation.item.create",
  item:{type:"function_call_output", call_id, output}}, {type:"response.create"}]`. `output` is
  altijd een string (JSON-serialisatie van `{answer}` of `{error}`).
- `_onDataChannelMessage`: geval `function_call` → `this._handleFunctionCall(action)`.
- `_handleFunctionCall`:
  - Alleen `name === "ask_ithaka"`; onbekende naam → output `{"error":"Onbekende tool"}` en
    door (het model hoort dan dat het niet kan).
  - Parse `arguments` (JSON-string) → `question`; parse-fout → `{"error":"Ongeldige argumenten"}`.
  - Serialiseer: één tool-call tegelijk (promise-ketting `this._toolChain`); een
    `response.create` terwijl er nog een response loopt geeft een OpenAI-fout.
  - Indicator-state `tool` ("zoekt op…") zolang de fetch loopt; daarna terug naar `listening`.
  - `POST /api/realtime/ask` (same-origin, JSON). `res.ok` → output `{"answer": …}`; anders
    `{"error": detail || "Het opzoeken is mislukt"}`. Netwerkfout idem.
  - Na elke await: `if (!this._active) return` (fase-1-patroon); bij een gesloten data-channel
    geen send.
  - Verstuurt de twee events uit `buildFunctionCallOutputEvents`.
- Transcript: de vraag verschijnt als tool-notitie in de chat via het bestaande
  `window.chatRenderer.addMessage(...)`-pad, met de tekst "Opgezocht via Ithaka: <question>"
  in dezelfde stijl als de bestaande tool-indicator-regels; het gesproken antwoord komt zoals
  nu al via `response.output_audio_transcript.*` binnen.

### Settings-kaart (`static/index.html`, `static/js/settings.js`)

- In de bestaande "Realtime Conversation"-kaart één extra schakelaar "Tools (ask_ithaka)"
  (`set-realtimeToolsToggle`), opgeslagen als `realtime_tools_enabled` in dezelfde
  `saveRealtime()`-POST. Geen nieuwe kaart, geen nieuwe classes.

## Foutafhandeling

| Situatie | Gedrag |
|---|---|
| Tools uit (`realtime_tools_enabled=false`) | sessie zonder tools, exact fase-1-gedrag |
| Backend 4xx/5xx of netwerkfout | `function_call_output` met `{"error": …}` → model zegt dat het opzoeken mislukte |
| Agent-loop > 60 s | 400 "Het opzoeken duurde te lang" → idem |
| Tweede function_call terwijl de eerste loopt | gequeued, sequentieel afgehandeld |
| Sessie gestopt tijdens de fetch | resultaat weggegooid, niets verstuurd |
| Onbekende tool-naam | `{"error":"Onbekende tool"}` |

## Testen

- `tests/test_realtime_service.py` (uitbreiding): tools aanwezig bij `realtime_tools_enabled=True`
  (exact `ASK_ITHAKA_TOOL`, `tool_choice: "auto"`), afwezig bij `False`; default-setting-test in
  `tests/test_settings_realtime_keys.py`.
- `tests/test_realtime_ask.py` (nieuw): `answer_question` met gepatchte `stream_agent_loop`
  (async generator die SSE-regels yieldt) en gepatchte `resolve_task_candidates`: concat van
  delta's, thinking-delta's genegeerd, afkappen op `ASK_MAX_CHARS`, timeout → RuntimeError met
  Nederlandse tekst, leeg → RuntimeError.
- `tests/test_routes_realtime.py` (uitbreiding): 400 bij lege vraag, 400 bij tools uit, 200
  `{"answer"}` bij gepatchte `answer_question`, 400 bij RuntimeError, 500 bij generieke fout.
- `tests/test_realtime_voice_js.py` (uitbreiding, Node): `classifyRealtimeEvent` op
  `response.function_call_arguments.done`; `buildFunctionCallOutputEvents` exacte vorm.
- Live smoke (Ed of :7001 met echte key): "Wat is het weer in Utrecht?" → preamble hoorbaar →
  antwoord met actuele info; tools-toggle uit → model zegt dat het niet kan opzoeken.

## Buiten scope

- Meerdere/specifieke tools in de Realtime-sessie (weer, agenda-los) — de delegatie dekt ze.
- Streaming van tussenresultaten naar het spraakmodel (Realtime heeft geen async-tool-mechanisme;
  alleen het preamble-patroon is gedocumenteerd).
- Persistente opslag van het Realtime-transcript (fase-1-restpunt, ongewijzigd).
- Tool-approval/plan-mode in de Realtime-flow: `answer_question` draait met de standaard
  tool-policy van de agent-loop; er is géén generieke goedkeuringspoort (alleen
  `agent_email_confirm`/`auto_approve_skills`), dus voor een admin-eigenaar kunnen ook
  shell/python/write_file via spraak draaien. Bewuste keuze voor fase 2: de server logt elke
  tool-aanroep (`realtime_ask`-logger, tool-naam per `tool_start`-event); een zichtbare
  tool-trail in het transcript en/of een aparte Realtime-tool-policy is een follow-up.
