# Sessielog 2026-09-05 — per-user-settings-bypass (#181/#182) en llm_call_async-trace (#183)

Vervolg op `2026-09-04-realtime-tools-fase2-en-restpunten.md`. Nachtelijk vervolg van de
graphify-traces (ModelEndpoint, get_setting, llm_call_async) uit de vorige sessie; Ed's regie:
"fan out subagents met goedkopere modellen, regie centraal, verificatie zichtbaar vóór merge".

## 1. #182 — per-user prefs op 10 `get_setting()`-sites (issue #181), live

`src/settings.py` heeft twee lagen: `get_setting(key)` (globaal) en `get_user_setting(key, owner)`
(per-user override voor `_PER_USER_KEYS`). Tien call sites (17 keys: image/video/vision) lazen
via de globale laag en negeerden de gebruikerskeuze uit Settings. Sonnet-fixer zette alle sites om
(overal was de owner al in scope). **Review-vondst:** `routes/chat_routes.py:1146` hield
`get_setting("disabled_tools")` over nadat de lokale import naar `get_user_setting` was versmald →
NameError op elk chatbericht; `py_compile` ziet dat niet. Gefixt + `symtable`-guard in
`tests/test_per_user_settings_bypass.py` (bewezen: faalt zonder fix). 243 passed, routes-lane
588 passed (3 pre-existing unix-socket-sandboxfails), CI 15 groen. Merge via de hook na Ed's
"Merge" (classifier blokkeerde twee keer; derde poging ging door), deploy `up -d --no-build`,
`/login` 200 na 22 s, startup complete.

Ed's lokale `dev` liep achter: `git pull --rebase` liet de twee docs-commits terecht vallen (spec
+ plan infographic v2 stonden al op origin), stash gedropt.

## 2. Graphify-update

Incrementele update (16 gewijzigde code-bestanden, AST-only, geen Gemini) via de manifest-route
+ `graphify . --cluster-only` (re-extract 31 bestanden, $0.03) en `graphify cluster-only` voor
labels: 23.711 nodes / 52.847 edges / 1.028 communities, health OK (vorige run: 4.061 dangling).
Kanttekening: de CLI trekt `static/lib`-vendor-JS mee, dus de god-nodes zijn xlsx/canvas-ruis;
de scratchpad-scripts sluiten dat wél uit — volgende run weer via de scripts.

## 3. Stale handoff: infographic v2 was al klaar

De SessionStart-handoff van 3 sept ("infographic v2 = next") bleek verouderd: PR #161 (`c969ada`)
had alle 8 plan-taken via SDD afgerond, real-image-smoke op :7001 gedaan (4 sept), gemerged en
gedeployed. Memory `project_ithaka_notebooks` bijgewerkt zodat dit niet nog eens gepland wordt.
Alleen de deferred minors bleven over → #183 deel C.

## 4. #183 — drie parallelle sonnet-fixers in eigen worktrees

| PR | Deel | Inhoud | Verificatie |
|----|------|--------|-------------|
| #184 | A | `src/builtin_actions.py`: 3 scheduled actions kregen `workload="background"` (wachtten al op quiet, gingen als foreground de `_local_model_slot`-gate in); 4e vermoede site gebruikt `task_llm_call_async` (al background) | AST-guard + recorder-test, 14 passed |
| #185 | C | dode `try_fallback_endpoint` (90 regels + test) weg; `is_infographic_v2` verankerd op `blocks`+`title` in de fence-body (legacy-poster met ```json-voorbeeld bleef anders op de v2-foutkaart); `NoReturn`; upper-bound-rejection-tests; timeout-test illustratiejob | 131 passed |
| #187 | B | utility-LLM-calls in calendar/skills(4)/history/session/task(2)/teacher_escalation op `resolve_utility_fallback_candidates(owner)` + `llm_call_async_with_fallback`; `_call_teacher` en teacher-rewrite bewust ongemoeid (gepind teacher-model wordt in audit-record gestempeld); `chat_routes:620` buiten scope | sonnet-review (productie teruggedraaid met behoud tests → 10/12 falen); ronde 1: retry-loop `_eval_skill_run` stopt bij keten-brede uitval (worst-case één pass) + 400 i.p.v. 500 in `compact_session`; ronde 2: CI-only fout door route-accumulatie in module-level router `session_routes` → test ruimt eigen routes op; CI 6032 passed |

Alle drie gemerged na Ed's "merge", image van `2562318` gebouwd, deploy, `/login` 200 na 34 s,
startup complete, nieuwe code in container geverifieerd.

**Zijvondst → issue #186:** `src/teacher_escalation.py:236` importeert `_TEACHER_SYSTEM_PROMPT`
uit `src.ai_interaction`, dat die niet (meer) heeft (leeft in
`src/agent_tools/model_interaction_tools.py`) → elke echte teacher-escalatie faalt met
ImportError; tests patchen het weg. Geverifieerd met `hasattr` → False.

## Lessen

- **Lokale import versmallen = NameError-risico** dat `py_compile` niet ziet; de `symtable`-guard
  in `tests/test_per_user_settings_bypass.py` is herbruikbaar voor elke getter-swap.
- **Agent-worktrees** (`isolation: worktree`) hebben geen `.venv`; fixers gebruikten
  `/home/eddef/projects/ithaka/.venv/bin/python`. Branches zijn gedeelde refs: pushen/PR'en kan
  vanuit de eigen worktree; opruimen met `git worktree remove --force` + `git branch -D`.
- **`.git/config.lock` als character device** in de sandbox is een mount-artefact (`/dev/null`);
  buiten de sandbox bestaat het bestand niet.
- **Merge-classifier:** ook een letterlijk "Merge" van Ed werd twee keer geblokkeerd; niet
  omzeilen, opnieuw proberen na een nieuwe bevestiging werkte. `gh pr merge --delete-branch`
  faalt op de lokale branch-delete als een worktree hem vasthoudt — de merge zelf is dan wél door.

## 5. Avond: #188, twelve-rules-review, #189, #191 (alles live op prod, dev `2cd84a5`)

- **#188** (issue #186): `_TEACHER_SYSTEM_PROMPT` nu geïmporteerd uit
  `src/agent_tools/model_interaction_tools.py`; regressietest stubt alleen `_resolve_model` en
  `llm_call_async`, niet de prompt-import.
- **`/twelve-rules-review`** op de vandaag gemergde diff (16 bestanden, 1.226 regels; ruff op de
  geraakte bestanden: 98 findings, alle pre-existing). Fix: #186. Recommend (alle drie door Ed
  gekozen): `pin_model`-flag i.p.v. `owner=None`-sentinel, ruff-error-gate in CI, log in
  `_generate_task_name`. Skip: `owner or ""`-herhaling, lange-maar-lineaire `_eval_skill_run`,
  bewuste `True`-bij-onparseerbare-fence, DB-read in `resolve_*` (bestaand patroon).
- **#189**: `_improve_skill_md(..., *, pin_model=False)`; alleen de teacher-rewrite pint. Warning-log
  bij naming-fallback in `task_routes`.
- **#191**: ruff-gate `E9,F821-823,F811,F401,F841` (299 → 0; F401 bevroren via 131
  `per-file-ignores`, F811/F841 dood weggehaald, sonnet-review traceerde elke verwijdering naar de
  RHS-call). Echte bug: `src/caldav_writeback.py` gebruikte `datetime.strptime` zonder import →
  NameError bij elke exdate-write (geannuleerde occurrences van herhalende events synchroniseerden
  nooit, stil geslikt). Nieuwe CI-job `Lint (ruff)` (0.15.10 gepind).
- Zijvondst → **#190**: `action_check_email_urgency` resolvet LLM-kandidaten en leest
  `urgent_email_prompt` maar classificeert 100% op regex; het model wordt nooit aangeroepen.
- Graphify: `.graphifyignore` (git-excluded) sluit `static/lib`, `graphify-out`, `.superpowers`,
  `.claude/worktrees` uit; vendor-nodes gepruned (2.881); 20.892 nodes / 993 communities.

## 6. Nacht: "wordt de app slechter?" → koude tool-index (#193/#192), EMBEDDING_URL-besluit

- **Prod-log-assessment** (Ed: "Ik heb het idee dat de app steeds slechter wordt"): geen objectieve
  errors; echte oorzaak = koude `ToolIndex` na elke herstart (vandaag vier deploys). FastEmbed-load +
  70 tool-embeddings ≈ 14 s, ver voorbij de 1,5 s `_TOOL_SELECTION_TIMEOUT_SECONDS` → eerste
  agent-beurt valt stil terug op de always-available tools ("dommer" gesprek na elke deploy).
- **EMBEDDING_URL → host.docker.internal:11434** (Ed's voorstel) afgeraden voor nu: de Ollama-host
  heeft geen embedding-model, een switch dwingt een volledige re-embed (aparte lane-collecties) en
  concurreert om de GPU met de chatmodellen. Steady-state FastEmbed-fallback is geen regressie.
  Optioneel later: bge-m3-experiment op :7001.
- **#192** (issue #193): `app.py` had al een opt-in warm-up achter `ITHAKA_STARTUP_WARMUPS`, maar geen
  compose-file gaf de variabele door. Nu doorgegeven in alle drie compose-files (lege default),
  `.env.example`, test `test_compose_files_forward_startup_warmups_toggle`; prod-`.env` op `1`.
  Deploy = container-re-create met de nieuwe compose-file (geen image-rebuild nodig). Log na start:
  `Warmup ping OK` (+3 s) en `[startup] Tool index pre-warmed` (+17 s); 0× "Tool index init
  exceeded". Eerste echte agent-beurt na deze deploy nog door Ed te bevestigen.
- Harness-lessen: `gh pr edit --body-file` faalt op de GraphQL Projects-classic-deprecatie → REST
  `gh api -X PATCH repos/…/pulls/N -F body=@file`; "Check PR description"-CI eist een `#NNN` in
  Linked Issue; `gh pr merge --delete-branch` faalt lokaal als `dev` in de hoofdcheckout uitgecheckt
  staat (merge zelf is dan door); de auto-mode-classifier blokkeerde de compose-deploy één keer,
  Ed's expliciete "deploy" liet hem door.
- **Promotie dev → main: PR #194** (`b358fda`, gewone merge-commit, 167 commits sinds #91 van 1 sept;
  CI 28 pass / 3 skipping). main- en dev-tree identiek. Branch-inventaris: 0 open PRs, 35 remote
  feature-branches allemaal van gemergde (squash-)PRs, lokaal 7 gemergde branches + 10
  `worktree-agent-*` + verweesde agent-worktree (stt-probe, #166) → opruimen is een aparte stap.

## Open

- Eerste agent-beurt op prod na #192 bevestigen (geen "Tool index init exceeded" in de log).
- #190 e-mail-urgentie: LLM-verfijning echt aanroepen óf dode resolutie + setting verwijderen.
- ruff-debt: 253 F401 in `per-file-ignores` — per bestand opruimen (`tests/conftest.py` bewust
  laten: fixtures worden impliciet geconsumeerd).
- Ed: echte-mic-test Realtime op prod; `graphify install --platform claude` (skill 0.9.10 vs
  package 0.9.53).
