# 2026-08-27 — Voice mode werkend gemaakt (realtime hands-free loop)

## Aanleiding

Ed meldde "voice mode werkt niet" na de commits f83f071/a5cd448 van 26-08. Systematische
debug (twee parallelle code-analyse-agents + live log/API-onderzoek) vond **vijf**
onafhankelijke oorzaken — de feature kon in de gemergde staat nooit één turn voltooien.

## Root causes

1. **STT stond server-side uit** (`stt_provider: "disabled"`) én er is geen STT-settings-UI
   meer (kaart uit `index.html` verwijderd; `initSttSettings` bailt stil). Voice mode
   weigerde met alleen een console-warn. → STT aangezet via `POST /api/auth/settings`:
   `endpoint:471e5364` (OpenAI) + `whisper-1`.
2. **`voiceRecorderModule` was nooit aan `voiceMode` doorgegeven** (app.js importeerde hem
   maar gaf hem niet mee aan `init()` en zette hem niet op `window`) → `_getRecorder()`
   altijd null → activatie brak af vóór alles.
3. **Geen end-of-speech-detectie**: niets riep ooit `stopRecording()` aan; de mic nam
   eindeloos op en `/api/stt/transcribe` werd nooit bereikt.
4. **Chat-errorpad lekte de busy-state**: bij `!res.ok` op `/api/chat_stream` werd
   `onResponseComplete()` nooit aangeroepen → voice mode voorgoed "AI responding…".
5. **Restore-race op pageload**: `activate()` liep vóór de STT-stats-fetch, waardoor een
   persisted voice mode nooit her-armde.

## Fixes (branch `fix/voice-mode-loop`)

- `voiceRecorder.js`: `createVoiceActivityDetector` (pure state machine, unit-getest) +
  AnalyserNode-monitor; auto-stop na stilte-na-spraak (1.4 s), max-duur-vangnet (90 s),
  WebAudio-loze fallback; `startRecording` kreeg `opts {vad, onDone}`; mic-leak gefixt
  wanneer stop een pending `getUserMedia` kruist.
- `voiceMode.js`: `activate()` async met provider-refresh vóór de guard; re-arm bij lege/
  mislukte transcriptie; deactivate na 3 opeenvolgende transcribe-fouten; recorder wordt
  geïnjecteerd.
- `chat.js`: `onResponseComplete()` ook op de `!res.ok`-paden.
- `app.js`: recorder-module doorgegeven aan `voiceMode.init()` + `window`-global; persist
  via de state-callback (activate is nu async).
- Tests: `tests/test_voice_mode_js.py` (7 stuks, node-based); JS-area 210 groen.

## Smoke (chrome-devtools, :7000, desktop + 360 px mobiel)

Nep-microfoon via monkeypatched `getUserMedia` gevoed met echte TTS-audio
("Wat is de hoofdstad van Frankrijk?"). Volledige loop live bewezen:
arm → VAD-stop → `POST /api/stt/transcribe` 200 (exact transcript) → auto-send →
`chat_stream` 200 (gemma4: "De hoofdstad van Frankrijk is Parijs.") → 6× TTS 200 →
**mic her-armde** (micCalls=2, indicator "listening…"). Nul console-errors. Mobiel:
indicator + armed-mic-knop in viewport, gesprek leesbaar. Server-side E2E apart bewezen:
TTS-audio → whisper-1 → identieke tekst terug.

## Follow-ups

- **STT-settings-UI ontbreekt** — kaart terugbrengen in Settings (of bewust API-only
  laten en documenteren; gotcha staat nu in CLAUDE.md).
- Ed's chat-sessie stond op `gemini-2.5-flash-native-audio-latest` — dat model kan geen
  `generateContent` (404; de fallback naar claude-opus-4-8 vangt het op, met vertraging).
  Model terugzetten op een tekstmodel scheelt latency en log-ruis.
- ONNX/CUDA-errors in de container-logs zijn cosmetisch (fastembed → CPU-fallback);
  desgewenst `onnxruntime-gpu` voor CUDA 12 in het image.
