# Sessie 2026-07-29 — Twelve-rules review src/ kern + implementatie

## Wat er gebeurd is

Twelve-rules engineering review over de src/-kern (10 files, ~11.250 regels:
agent_loop, tool_execution, tool_policy, tool_schemas, chat_handler,
chat_processor, llm_core, prompt_security, url_safety, tool_security) via drie
parallelle review-subagents, daarna alle bevindingen geverifieerd op de bron en
volledig geïmplementeerd (fixes + alle acht Recommends) met vijf implementatie-
subagents en handwerk. Twaalf commits op `dev` (99ad842..6a99b2d), volledige
suite groen: **4566 passed, 0 failed**.

## Belangrijkste fixes

- **Concurrency/state**: dubbel-decrement in de local-model-gate
  (`_local_model_slot`), request-scoped active-document-state (ContextVar-holder
  i.p.v. module-global; cross-sessie doc-corruptie), `used_memories` als
  returnwaarde i.p.v. gedeelde ChatProcessor-state (cross-tenant leak).
- **Security**: SSRF DNS-rebinding dicht via `safe_httpx_request(_async)` —
  gevette IP gepind op de connectie (Host + sni_hostname), handmatige redirects
  met per-hop hervalidatie; alle guarded fetch-sites gemigreerd. Plan-mode
  fail-closed bij schema-importfout (sentinel → allowlist-enforcement in
  ToolPolicy); registry-sync-test eist expliciete classificatie van elke tool.
- **Event loop**: Kimi-UA-probe (tot ~48s sync httpx) via asyncio.to_thread;
  response-cache kreeg TTL (300s) en cachet alleen deterministische calls.
- **Decomposities (gedragsidentiek, tests ongewijzigd groen)**:
  `_stream_llm_inner` → vier provider-generatoren + `_OpenAIStreamState`;
  `stream_agent_loop` → `_select_agent_tools` / `_execute_round_tool_blocks`
  (live async-subgenerator met aclose-cancel) / verifier + loop-breaker met
  `_AgentTurnState`; `_build_system_prompt` → per-bron builders;
  `build_context_preface` → per-bron helpers.
- **Drift uit de fork-hernoem**: GPU-standalone-composes misten de
  tailscale-sidecar; tests pinden nog `ODY_USER` en het oude README-wordmark;
  zes `test_review_regressions`-failures door ontbrekende `core.log_safety`-stub.

## Verificatie

- Volle pytest-suite groen (4566/0) na afloop; per commit focused slices.
- Docker-stack herbouwd; app op :7000 (302 → login) met nieuw image.
- End-to-end SSE-chat-round-trip op verse smoke-instantie (:7001,
  `ITHAKA_DATA_DIR`, qwen3:0.6b via lokale Ollama): 157 delta-events,
  message_saved, [DONE] — door de volledig nieuwe pipeline.
- Browser-UI-check strandde op tooling (Chrome-venster geminimaliseerd,
  screenshot-API-bug); functionele verificatie via API gedaan. Visuele check
  op localhost:7000 blijft een aanrader bij de volgende sessie.

## Valkuilen voor herhaling

- Docker-daemon in deze WSL-distro start niet vanzelf (geen systemd):
  `sudo service docker start` na elke WSL-herstart, of `systemd=true` in
  `/etc/wsl.conf`.
- Subagents die `git stash`/`pop` gebruiken terwijl andere agents werk-in-
  progress hebben: één agent stashde tijdelijk andermans ongecommitte werk weg.
  Expliciet verbieden in agent-prompts (is in latere prompts gedaan).
- `/api/session` en `/api/model-endpoints` zijn Form-encoded, geen JSON.
- `get_active_document()` wordt in chat_routes bewust cross-request gebruikt
  (rescue voor sessieloze docs) — vandaar holder + apart proces-breed pointer,
  géén pure ContextVar.

Ed de Feber, in nauwe samenwerking met Claude
