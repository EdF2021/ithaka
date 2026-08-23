# 2026-08-23 — HF-modellen: Ollama-probe, cookbook-pad en prebuilt-fix

**Aanleiding.** Vervolg op de HF-verkenning uit de (vastgelopen) ochtendsessie.
Ed akkoord met het 3-stappenvoorstel: (1) goedkope Ollama-GGUF-probe,
(2) het echte cookbook-pad, (3) gaten dichten. Modelkeuze-opdracht:
"een goede allrounder die comfortabel in 16 GB past".

## Stap 1 — Ollama-probe (klaar, nul code)

- **Keuze: Qwen3-14B, Q4_K_M** (officiële `Qwen/Qwen3-14B-GGUF`, 9,0 GB):
  ~7 GB VRAM-marge voor KV-cache, tool-calling werkt (gat dat gemma4 laat),
  sterk NL, Apache 2.0. Mistral Small 24B (~13,5 GB) afgewezen: te krap.
- `ollama pull hf.co/Qwen/Qwen3-14B-GGUF:Q4_K_M` → generatie- én
  tool-call-test groen via de OpenAI-API van Ollama.
- Prod heeft de Ollama-endpoint al (`host.docker.internal:11434`, refresh
  auto) → model verschijnt vanzelf in de modelkiezer; vanuit de
  prod-container bevestigd via `/v1/models`.
- **Let op:** Ollama's default context (40k) duwt het net over 16 GB →
  10% CPU-offload. Met 8-16k context past het volledig op GPU.
- Bijvangst: achtergebleven `wan_probe.py`-proces (gesloten t2v-spike)
  hield ~15 GB VRAM vast — gekilld.

## Stap 2 — cookbook-pad (bewezen op :7001-smoke-instance)

Volledige flow via Ithaka's eigen API: `hf-gguf-files` →
`POST /api/model/download` (Qwen3-0.6B, DOWNLOAD_OK) →
`POST /api/model/serve` (tmux, llama-server :8082, SERVER_UP) →
auto-registratie online → model in modelkiezer → chatantwoord
("7 keer 8 is 56.") in de browser-UI. Admin-gate: alleen sessie-cookie,
`ody_`-tokens werken niet op cookbook-routes.

**Gevonden bug:** de prebuilt-fetch faalde op elke moderne host
(llama.cpp's `releases/latest` is nu een nightly-pointer; Linux
CUDA-prebuilts bestaan niet meer; assets zijn .tar.gz). Host-serve
crashte daardoor op een source-build zonder cmake (exit 127).

## Stap 3 — gaten gedicht

- **PR #41** (issue #42, gemerged, CI groen, 3 TDD-tests): nightly-pointer
  resolven, NVIDIA→CPU-asset-fallback, tar.gz-extractie.
  Live gevalideerd: gegenereerd blok kiest `b10566/…-ubuntu-x64.tar.gz`.
- **`.env.example`**: `HF_TOKEN` gedocumenteerd (3621c75). Token zelf is
  aan Ed (Cookbook → Settings slaat 'm versleuteld op; env is fallback).
- Prod-container gerebuild na de merge.

## Restpunten / inzichten

- GPU-serving via cookbook-llama.cpp kan op deze machine niet (geen nvcc
  in container/host; Linux CUDA-prebuilts bestaan niet): **Ollama blijft de
  GGUF-GPU-runtime**, cookbook-llama.cpp is CPU-fallback.
- Losse scripts (`diffusion_server.py`, `add_hwfit_models.py`, e.a.) lezen
  alleen `HF_TOKEN` uit env en missen het versleutelde UI-token;
  `add_hwfit_models.py` slikt gated-repo-fouten stil in. Kandidaat-follow-up:
  `load_stored_hf_token()` hergebruiken + stille except loggen.
- Cookbook-testslice heeft 1 pre-existing lokale failure
  (`test_cookbook_docker_access.py::test_container_opt_in_with_unix_socket_is_allowed`).
