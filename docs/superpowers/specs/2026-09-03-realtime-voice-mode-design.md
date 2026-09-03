# Realtime-gesprek (voice mode fase 1): OpenAI Realtime API via WebRTC

Status: ontwerp gepresenteerd 2026-09-03; drie aannames staan open voor Eds akkoord (zie
"Aannames"). Nog niet goedgekeurd — implementatie start pas na bevestiging.

## Doel

De huidige voice mode (`static/js/voiceMode.js` + `voiceRecorder.js` + blocking STT/TTS-
cascade) werkt slecht: live gemeten **10,5 s per beurt, waarvan maar 0,62 s LLM-tijd**
(`docs/sessions/2026-08-27-realtime-nl-mcp.md`) — de rest is VAD-stilte-wacht, blocking
Whisper-upload, blocking sequentiële TTS per zin, en vaste re-arm-delays (500/800 ms). Er is
geen barge-in: de mic staat nooit aan terwijl de AI spreekt, dus onderbreken kan niet. Ed wil
dit vervangen door een sessie op de OpenAI Realtime API (WebRTC, server-side VAD, native
audio in/out), op basis van een config die hij zelf in de OpenAI-playground heeft gebouwd en
getest (model `gpt-realtime-2.1-mini`, transcriptie `gpt-realtime-whisper`, stem `ash`,
`far_field`-noisereductie, `server_vad` met threshold 0.5/prefix 300 ms/silence 500 ms, geen
idle-timeout, `output_modalities: ["audio"]`, geen tools, `reasoning.effort: "low"`).

Tweede, gerelateerde klacht: tijdens een realtime-gesprek moet het antwoord direct in het
Nederlands komen — geen Engelse denkstap die hardop wordt uitgesproken vooraf.

## Wat er al is (hergebruik)

- `static/js/voiceMode.js`: state-machine (active/armed/busy), `onStateChange`-notificatie,
  toggle-persistentie (`loadToggleState`/`saveToggleState`). Vorm blijft, interne
  `_armMic`/`_onRecordingDone` worden vervangen voor het Realtime-pad.
- `static/app.js:2418-2467` `initVoiceModeToggle()`: wiring van `#overflow-voice-btn` /
  `#voice-mode-indicator-btn`, settings-gate vóór activatie. Zelfde patroon voor de nieuwe
  toggle.
- `ModelEndpoint`-tabel (`core/database.py`) + admin-UI: bestaand patroon voor
  base_url+api_key per endpoint, gebruikt door `services/stt/stt_service.py` en
  `services/tts/tts_service.py` voor `endpoint:<id>`-providers. De Realtime-sessie hergebruikt
  dit i.p.v. een nieuw geheim-pad te verzinnen.
- `src/chat_processor.py`: `response_language`-instructie wordt nu al onvoorwaardelijk in elke
  chat/agent/voice-beurt geïnjecteerd. Zelfde tekst wordt de basis voor het Realtime
  `session.instructions`-veld.
- `routes/mcp_routes.py`: bestaand precedent voor "geheim blijft server-side, client krijgt
  alleen een kortlevend token" (OAuth `client_secret`-afhandeling).
- `routes/model_routes.py:636`: sluit al `-realtime`/`-transcribe`/`-tts`-modellen uit van de
  tekst-chat-modelpicker — bevestigt dat Realtime-modellen al als aparte categorie worden
  gezien.
- OpenAI Realtime API-referentie (geverifieerd 2026-09-03, `developers.openai.com/api/*`):
  ephemeral key via `POST /v1/realtime/client_secrets` (server-side, `expires_after` 10–7200 s,
  default 600), WebRTC-SDP-uitwisseling via `POST /v1/realtime/calls` met de ephemeral key,
  event-datakanaal `oai-events`. `gpt-realtime-2.1-mini` is een bestaand, bevestigd model-ID
  (128K context, 32.000 max output tokens, reasoning-ondersteuning); pricing gelijk aan
  `gpt-realtime-mini` ($0,60/$0,06/$2,40 tekst, $10/$0,30/$20 audio per 1M tokens).

## Aannames (nog niet bevestigd door Ed)

Deze drie keuzes zijn door mij gemaakt na een verlopen `AskUserQuestion` (300 s timeout) en
Eds "werk zelfstandig, hou zelf de regie" — ze zijn redelijke defaults, geen goedgekeurd
ontwerp. Ed kan ze op elk moment corrigeren; dit spec-bestand documenteert ze expliciet zodat
een correctie een kleine diff is, geen herontwerp.

1. **Realtime komt naast de bestaande voice mode, niet in plaats ervan.** Twee losse toggles;
   de oude STT/TTS-cascade blijft ongewijzigd bestaan voor gebruikers/omgevingen zonder
   OpenAI-sleutel of met een voorkeur voor de bestaande stemmen/providers.
2. **Tool-calling zit niet in fase 1.** De Realtime-sessie krijgt `tools: []`, zoals in Eds
   eigen config. De bestaande tool-infrastructuur (`tool_schemas.py`, `tool_execution.py`,
   `tool_policy.py`) is niet 1-op-1 herbruikbaar over het Realtime-datakanaal (ander protocol
   dan HTTP-tool-execution) en is een aparte, latere fase.
3. **Sessie stopt hard na 10 minuten** als kostenbeheersing (audio-tokens zijn duur: $10–20 per
   1M). Zichtbare waarschuwing in de UI, geen silent cutoff.

## Architectuur

```
Browser (realtimeVoice.js)                    Ithaka backend                    OpenAI
─────────────────────────────                 ───────────────                   ──────
1. Toggle "Realtime gesprek" aan
2.                                    POST /api/realtime/session (cookie-auth)
                                             │
                                             ├─ ModelEndpoint ophalen (realtime_model,
                                             │  api_key)
                                             ├─ session.instructions bouwen
                                             │  (response_language + anti-Engelse-
                                             │  denkstap-regel)
                                             └─ POST /v1/realtime/client_secrets ──────►
                                                                              ◄────── ek_...
                                    ◄──── { client_secret, session } (JSON)
3. RTCPeerConnection: mic-track toevoegen,
   SDP-offer maken
4.                                             POST /v1/realtime/calls
                                                (Authorization: Bearer ek_...) ──────►
                                                                              ◄────── SDP answer
5. WebRTC-verbinding staat: audio in/uit
   direct browser↔OpenAI; events over
   het "oai-events" datakanaal
```

De backend is uitsluitend een **token-minter**: hij ziet nooit audio, alleen de sessie-opzet.
De browser praat na stap 4 rechtstreeks met OpenAI (WebRTC peer-to-peer, geen Ithaka-proxy in
het audiopad) — dit is waarom latency zo laag kan zijn.

## Backend

**Nieuwe route** `routes/realtime_routes.py`, zelfde auth-patroon als de rest van `routes/`
(sessie-cookie via bestaande `AuthManager`-middleware):

- `POST /api/realtime/session` — geen request-body nodig (leest settings + het geconfigureerde
  `ModelEndpoint`). Bouwt de sessie-config server-side (model, voice, VAD-instellingen,
  noise_reduction, `instructions`) en doet de `client_secrets`-call. Retourneert
  `{client_secret, expires_at, session}` — nooit de lange-levende `api_key`. Faalt met een
  duidelijke Nederlandse fout als er geen `realtime`-endpoint geconfigureerd is
  (`realtime_enabled=true` zonder passend `ModelEndpoint` → 400 "Geen Realtime-endpoint
  ingesteld").

**Nieuwe settings** (`src/settings.py` `DEFAULT_SETTINGS`, parallel aan het bestaande
`stt_*`/`tts_*`-blok):

| key | default | betekenis |
|---|---|---|
| `realtime_enabled` | `False` | schakelt de nieuwe toggle in de UI vrij |
| `realtime_model` | `gpt-realtime-2.1-mini` | modelnaam op het gekozen `ModelEndpoint` |
| `realtime_voice` | `ash` | |
| `realtime_vad_threshold` | `0.5` | |
| `realtime_vad_prefix_ms` | `300` | |
| `realtime_vad_silence_ms` | `500` | |
| `realtime_noise_reduction` | `far_field` | |
| `realtime_max_minutes` | `10` | hard sessie-plafond (aanname 3) |

**`session.instructions`** wordt server-side samengesteld uit: de bestaande
`response_language`-tekst (indien gezet, anders "Nederlands" als default voor dit pad — de
Realtime-sessie is anders dan tekstchat expliciet Nederlandstalig bedoeld) + een vaste regel
tegen het hardop uitspreken van een Engelse denkstap:
`"Antwoord altijd direct in het Nederlands. Denk niet hardop in een andere taal voordat je
antwoordt — geef meteen het Nederlandse antwoord."` Dit lost de tweede klacht op structureel
niveau op: met `reasoning.effort: "low"` en een expliciete instructie is er geen apart
"denk-kanaal" dat apart onderdrukt moet worden zoals bij tekstchat (`<think>`-parsing) — de
instructie stuurt het model rechtstreeks.

## Frontend

**Nieuwe module** `static/js/realtimeVoice.js`, met dezelfde state-shape als `voiceMode.js`
(active/connecting/listening/speaking/error) zodat de bestaande toggle-UI-conventies
(indicator-knop, kleur/animatie per state) hergebruikt kunnen worden:

- `activate()`: `POST /api/realtime/session` → `RTCPeerConnection` opzetten,
  `getUserMedia({audio:true})`, track toevoegen, `createDataChannel("oai-events")`, SDP-offer
  naar `/v1/realtime/calls` met de ephemeral key, answer toepassen, `<audio autoplay>` op
  `pc.ontrack`.
- Datakanaal-events: `input_audio_buffer.speech_started/stopped` → UI-indicator
  ("luistert"/"denkt"); `conversation.item.input_audio_transcription.completed` → user-turn
  toevoegen aan de bestaande chatgeschiedenis (`add_message`, net als bij tekstchat, zodat het
  gesprek zichtbaar en doorzoekbaar blijft); `response.output_audio_transcript.delta/done` →
  assistant-turn opbouwen/afronden in dezelfde geschiedenis; `error` → zichtbare Nederlandse
  foutmelding.
- **Barge-in**: server_vad met `interrupt_response: true` hoort audio-playback op de server
  al te onderbreken bij nieuwe spraak (niet expliciet bevestigd in de WebRTC-gids — zie
  Foutafhandeling); als client-side vangnet stuurt de module bij `speech_started` tijdens
  `speaking`-state een `response.cancel`-event.
- **Sessie-timeout**: bij het bereiken van `realtime_max_minutes` stuurt de module een
  zichtbare melding ("Realtime-sessie gestopt na 10 minuten — heractiveer om door te gaan"),
  géén silent cutoff (conform de bestaande "silent features" memory-regel).
- Instellingen-card in `static/js/settings.js`, naar het patroon van `initSttSettings()`:
  toggle + model/voice-keuze uit het geconfigureerde `ModelEndpoint`.

## Foutafhandeling

- Ontbrekend/verlopen `ModelEndpoint` of API-key → 400 met Nederlandse tekst, UI toont dit
  i.p.v. stil te falen.
- WebRTC-verbinding valt weg (netwerk, ICE-failure) → één automatische reconnect-poging; lukt
  die niet, val terug naar een zichtbare melding + de bestaande voice mode blijft beschikbaar
  als alternatief (aanname 1 maakt dit mogelijk).
- `error`-events van de Realtime-API → getoond in de UI, niet alleen console.
- Barge-in-aanname (server-side `interrupt_response`) is **niet bevestigd** in de huidige
  WebRTC-gids — dit moet empirisch geverifieerd worden tijdens smoke-testen; de client-side
  `response.cancel`-fallback dekt het geval waarin het niet automatisch werkt.

## Testen

- Backend: `routes/realtime_routes.py` — gemockte `httpx`-call naar `client_secrets`, test dat
  de API-key nooit in de response zit, test de 400-foutpaden (geen endpoint, endpoint zonder
  key).
- Frontend: pure-JS unit test voor de sessie-config-builder (welke velden op basis van welke
  settings) — geen echte WebRTC-verbinding in CI, naar het patroon van
  `tests/test_voice_mode_js.py`.
- **Verplicht vóór merge**: live browser-smoke met een echte OpenAI-sleutel, desktop + 360 px
  mobiel — spreek een zin, controleer dat het antwoord meteen Nederlands is (geen Engelse
  denkstap hoorbaar), en test barge-in (praat overheen tijdens een lang antwoord, controleer
  dat de AI stopt). Output zichtbaar in de PR-chat vóór merge (globale CLAUDE.md-regel).

## Niet in scope (fase 1)

- Tool-calling over het Realtime-datakanaal (aanname 2).
- Vervangen/verwijderen van de bestaande STT/TTS-cascade of de podcast-audiopijplijn
  (`src/notebook_audio.py`) — die blijven ongewijzigd.
- Server-side proxying van audio (bewust P2P browser↔OpenAI voor lage latency).
