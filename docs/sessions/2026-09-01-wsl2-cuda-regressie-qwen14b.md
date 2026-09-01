# Sessie 2026-09-01 — WSL2 CUDA-regressie gediagnosticeerd, lokaal Qwen3-14B hersteld

## Aanleiding

Ed: "Klopt het dat er iets mis is met ithaka live?" App zelf bleek gezond (stack net herstart,
302 in 5 ms), maar elke chat/poller-call naar het primaire lokale model
`hf.co/Qwen/Qwen3-14B-GGUF:Q4_K_M` faalde met `cudaMalloc failed: out of memory` en verspilde
20–40 s vóór de cloud-fallback (gemma4:31b). Geen repo-wijziging nodig; alles infra.

## Root cause (vervangt de num_ctx-diagnose van 30-8)

**WSL2 CUDA-driverregressie**, niet de modelconfig:

- NVIDIA-driver 616.56 geïnstalleerd 20-8-2026 (Windows-update KB5120708 op 30-8 — de dag van
  de eerste OOM). nvidia-smi in WSL toont UMD 615.65.06 vs KMD 616.56, ook na verse WSL-boot.
- Symptoom: `cudaMemGetInfo` rapporteert ~15 GB vrij op de RTX 5060 Ti (16 GB), maar `cudaMalloc`
  faalt zodra het totaal ~10,5 GB nadert. Bewijs-bracket: llama3.2:3b (2,5 GB) en qwen3.5
  (5,1 GB) laden prima; Qwen3-14B én deepseek-r1:14b (~9 GB weights) falen zelfs op een
  280–768 MB vervolg-alloc. Windows-kant gebruikt maar ~0,5 GB (GPU Process Memory-counters).
- Ollama's scheduler respecteert `OLLAMA_GPU_OVERHEAD`, maar llama.cpp's interne
  "fit params"-stap (ollama 0.32.13) vraagt de driver zélf en gelooft de leugen → laadt alsnog
  41/41 lagen → crash. Alleen expliciete `num_gpu` wordt gehonoreerd.
- Nuance op 30-8-diagnose: Ithaka's OpenAI-compat-pad (`/v1/chat/completions`) stuurt geen
  `num_ctx` mee; de 40960 kwam uit de Modelfile. Maar zelfs num_ctx 8192 faalde — driver dus.

## Workaround (live)

1. Modelfile `hf.co/Qwen/Qwen3-14B-GGUF:Q4_K_M` her-created met `PARAMETER num_ctx 16384` +
   `PARAMETER num_gpu 30` (kopie: `/root/.ollama/Modelfile.qwen14b` in de container).
2. Container `ollama` herstart met `OLLAMA_GPU_OVERHEAD=5905580032` (5,5 GiB) en
   `OLLAMA_KEEP_ALIVE=2h` (was 5 m; cold load duurt ~80 s door mmap-uit bij RAM-druk).
3. Resultaat: 30/41 lagen op GPU, 8,1 GB VRAM, ~7,5 tok/s; warm antwoord ~11 s.

## Verificatie

- Ithaka's exacte pad: `POST /v1/chat/completions` zonder options → `'ok'`, ctx 16384, 8,1 GB.
- App-logs: `LLM async call … succeeded in 29.52s (attempt 1)` — geen 500's/fallback meer;
  email-pollers (cal-extract) draaien weer lokaal.
- Browser-smoke (chrome-mcp, localhost:7000): bericht in bestaande Qwen-sessie → antwoord van
  `Qwen3-14B-GGUF:Q4_K_M` zónder "→ gemma4:latest"-fallback-badge (die bij de 07:37-poging
  van vóór de fix nog wel staat).

## Open / na driverfix

- **Echte fix is Windows-kant**: nieuwere driver dan 616.56 of rollback naar de pre-20-8-versie;
  geen publieke bugmeldingen gevonden (1-9). Na fix: `num_gpu 30` weer uit de Modelfile
  (volle snelheid); `num_ctx 16384` mag blijven (40960 paste nooit op deze kaart).
- deepseek-r1:14b, gemma4:latest en gpt-oss:20b lokaal hebben géén num_gpu-cap en blijven
  kapot zolang de driver lek is (cloud-varianten werken).
- E-mail-MCP-verwijdering (surf.nl, sessie van 07:54–08:40) stond los hiervan; werkboom was
  schoon op untracked `QWEN.md` na.
