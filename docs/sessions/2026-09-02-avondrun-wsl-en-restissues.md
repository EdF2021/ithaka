# Sessie 2026-09-02 (avond) — WSL-geheugen opgeruimd, #141/#101 gefixt, GPU-embeddings

## Aanleiding

Middagrun eindigde met WSL-geheugendruk (supabase kd-copilot gestopt, devstral-small-2
uitgeladen, `.wslconfig` met `memory=12GB` + `autoMemoryReclaim=gradual` aangemaakt; de
`wsl --shutdown` was nog handmatig). `/goal`: WSL-opruiming afmaken, sessielog, #141, #101 en
de onnxruntime-CUDA-provider — zelfstandig, sonnet-subagents in worktrees, regie centraal.

## WSL-opruiming (live)

- `.wslconfig` is actief: `free -h` toont 11 GiB totaal (was onbegrensd), alle containers
  "Up 2 minutes" na de shutdown. Gebruik 3.9 GiB, swap 0.
- Twee losse containers met `restart=always` die niets met Ithaka te maken hebben, gestopt en op
  `restart=no` gezet: `open-webui` en `docker-model-runner`. Gebruik daalde naar 3.2 GiB
  (8.5 GiB beschikbaar). Terug aanzetten: `docker start open-webui`.
- Ithaka-stack draait met `docker-compose.yml` + `docker/gpu.nvidia.yml`; `nvidia-smi` in de
  app-container ziet de RTX 5060 Ti (1.4/16 GB). Ollama heeft GPU-passthrough, geen model geladen.

## PRs

- **#143** — issue #141: `ToolPolicy.discard_blocked_tool_calls` (alleen gezet in
  `_apply_notebook_tool_lockdown`); een gedetecteerd-maar-geblokkeerd fenced/native tool-block
  wordt direct na `_resolve_tool_blocks` verworpen zodat ronde 1 het eindantwoord is — mits er
  proza overblijft (fence-only → ronde 2 blijft). Grounding-prompt zegt nu expliciet dat er geen
  tools zijn. Review-ronde: native-pad-test + één gedeelde `skip_fenced`-expressie.
  A/B-smoke met een nep-OpenAI-endpoint (`fake_llm.py`, altijd proza + ```python-fence, model
  `fake-fence` met `supports_tools=false` — mét `true` slaat de fence-parser over): pre-fix
  2 AI-bubbles (`Tool blocked before start by policy: python` → `Agent round 2 … Referentiecontext
  ontvangen`), fix 1 bubble (`discarding 1 tool block(s) - notebook lockdown blocks all tools this
  turn`), desktop + 360px, 0 console-errors. Bijvangst → **#145** (fence rendert als code-block met
  Run-knop; op 360px één teken per regel — vooraf bestaand).
- **#142** — issue #101: identity-facts pinnen alleen nog als het bronvenster een
  eerste-persoons-zelfverklaring bevat (`_has_self_stated_identity`, NL+EN incl. "ik werk/woon",
  "X hier", "aangenaam", "je mag me X noemen", curly apostrof); naamconflict-check kent nu ook
  `Name: X`/`named X` en behandelt "Ed" vs "Ed de Feber" als compatibel (token-subset). Twee
  review-rondes: (1) module-global `_extractions_since_audit` brak een andere test → autouse-reset
  in conftest; (2) **echte-data-smoke faalde** op de eerste versie: gpt-oss geeft het feit als
  `Name: Thiermen Naaij`, wat buiten `_extract_name_value` viel → gate greep niet → pinned. Gate
  losgekoppeld van fact-text-parsing. Hersmoke op :7001 (gpt-oss:latest, verse memories):
  begroeting → `Identity low-signal … pinned: null`; "Mijn naam is Ed." → `pinned: true`.
- **#144** — GPU-embeddings: `requirements-gpu-nvidia.txt` (`onnxruntime-gpu[cuda,cudnn]==1.29.0`
  + `nvidia-cublas~=13.0`) via Dockerfile-build-arg `GPU_EXTRAS`, alleen gezet in de
  nvidia-compose-overlays; `preload_dlls()` in `src/embeddings.py`. Plain `onnxruntime` levert géén
  CUDA-provider; de .so op prod kwam uit een handmatig gepipte `onnxruntime-gpu 1.28.0` in
  `data/local` (uid-1000 user-site schaduwt het image). Agent-verificatie: CUDA-provider actief,
  200 zinnen 0.70s koud/0.02s warm vs 4.53s CPU; image 1.68 → 4.04 GB.

## Incident: voice mode kapot (19:27)

`POST /api/stt/transcribe` → 500 sinds 17:04: `stt_provider = endpoint:9496eb9f` = **Google
Gemini**, wiens OpenAI-compat-laag geen `/audio/transcriptions` heeft (404). Endpoints waren om
17:06 bewerkt. Probe vanuit de container: Gemini 404, OpenAI 200. Prod-setting via `src.settings`
omgezet naar `endpoint:471e5364` (OpenAI, whisper-1); STTService leest settings per call, geen
herstart. Structurele fix (PR volgt): probe van het endpoint bij opslaan + zichtbare STT-fout in
voice mode (nu alleen console.error + stille deactivatie na 3 fouten).

- **#146** — STT-probe bij opslaan (`STTService.probe_endpoint`: mini-WAV naar
  `/audio/transcriptions`; 404/405/401/403 → 400 en niet opslaan; `stt_skip_probe` voor offline),
  500-detail met upstream-status, zichtbare toasts in voice mode via `uiModule.showError`, en
  bijvangst: `showError` deactiveerde voice mode al bij de **eerste** fout (3-strikes-teller was
  dode code) en `saveSTT()` toonde "Saved" ook bij een afgewezen save. Smoke op :7001 met een
  Ollama-endpoint (404) en de nep-server (200): API + UI-kaart desktop en 360px, zie PR.

## Deploy (20:12)

`docker compose up -d --build` na #146 (één herstart voor #142/#143/#144/#146). Vooraf de
handmatige `onnxruntime-gpu 1.28.0`-schaduw uit `data/local` verplaatst naar
`~/ithaka-backups/data-local-onnxruntime-shadow-*` (326 MB; de `nvidia*`-packages daar zijn
Cookbook-afhankelijkheden en blijven). Startlog: `fastembed onnxruntime providers:
['CUDAExecutionProvider', 'CPUExecutionProvider']`; `encode()` van 200 teksten 0.07s (was ~4.5s
CPU). Image 1.68 → 4.04 GB. `/login` 200 desktop + mobiel; STT-provider bevestigd
`endpoint:471e5364`.

## Feature: auto-routing beeld (gpt-image-1.5) en video (Veo 3.1) — #152 (22:16–00:35)

Op verzoek van Ed: "als er om een afbeelding gevraagd wordt automatisch gpt-image-1.5, om video
automatisch Veo 3.1". Brainstorm → spec (`docs/superpowers/specs/2026-09-02-image-video-autoroute-
design.md`, Veo-REST geverifieerd tegen de Google-docs, prijzen 0.40/0.10/0.05 $/s) → plan
(`docs/superpowers/plans/2026-09-02-image-video-autoroute.md`, interface-contracten zodat vijf
sonnet-agents parallel konden bouwen) → PR's #150 (Veo-client + async jobs + routes + janitor),
#151 (tool `generate_video`, privilege `can_generate_videos`, SSE-doorgifte, `TOOL_TAGS`), #149
(intent-categorieën `image`/`video`, tool-force, schema, agent_loop-domein `media`), #147
(settings-kaart), #148 (chat-videobubble + poll). Integratiebranch gesmoked, dan in volgorde
B→C→A→D→E gemerged; prod herbouwd; `image_model=gpt-image-1.5`, `video_gen_enabled=True`.

Live gevonden en gefixt tijdens de smoke (fixtures waren groen, echte data niet):
- agent_loop's eigen classifier kende geen media-domein → `low_signal=True` → directe no-tools-route,
  ook in Agent-modus; gpt-oss antwoordde "Hey.". Fix: `media_intent()` als gedeelde bron +
  `_DOMAIN_TOOL_MAP["media"]`.
- Veo 3.1 weigert `numberOfVideos` (400) en eist `durationSeconds` als **getal** (de docs-
  samenvatting zei string). Twee fix-commits op #150.
- GPU-contentie: prod's skills-audit laadde devstral/Qwen-27B in Ollama, waardoor gpt-oss op de
  smoke-instance verhongerde (0.8 tok/s, time-out). Video-smoke daarom met gpt-4.1-mini gedaan.

Eindresultaat op :7001: beeld 1024x1024 via gpt-image-1.5 ($0.034) inline; video 4 s 1280x720
h264+aac via veo-3.1-generate-preview in 42 s ($1.60) inline, desktop + 360px, reload en verse
browsercontext OK, auth op het mp4-pad (401 zonder cookie). Follow-up: #153 (Image-kaart toont
gpt-image-1.5 niet in de dropdown). Kosten van deze sessie: 3 Veo-calls, waarvan 1 geslaagd
(mislukte niet gefactureerd).

Les: bij settings-kaarten opent een synthetische `.click()` op de sidebar-knop niet de
`open()`→`initAll()`-route; gebruik de echte uid-klik, anders lijkt de kaart "kapot".

## Lessen

- **Echte data vóór merge**: de eerste #142-versie was groen op fixtures en faalde live omdat het
  LLM `Name: X` teruggeeft. Een smoke met het echte model op :7001 (2 berichten → extractor)
  kost 3 minuten en ving het.
- **Deterministische A/B voor model-gedrag**: een nep-OpenAI-endpoint (`fake_llm.py`: SSE-stream
  met vaste tekst, `pinned_models` + `skip_probe` bij aanmaken) maakt "model stuurt een fence"
  reproduceerbaar; `supports_tools=true` laat de fence-parser overslaan (`_is_api_model`), dus
  `false` zetten om het pad te raken.
- **Gemini via OpenAI-compat is geen STT-endpoint**: de STT-kaart bood elk endpoint aan; nu
  geprobed bij opslaan. Bij "voice mode doet het niet" eerst `grep "stt/transcribe"` in de applog.
- `docker exec` zonder `-i` slikt heredoc-stdin stil (leeg logbestand); `pgrep -f` met het
  patroon in de eigen cmdline = exit 144 (bestandsnaam splitsen: `"fake_ll""m.py"`).
- Sonnet-reviewers vonden opnieuw de echte gaten (test-isolatie via module-global, subset-naam
  false-conflict, native-pad zonder test, dode 3-strikes-teller); fix-rondes via `SendMessage`.

Ed de Feber, in nauwe samenwerking met Claude
