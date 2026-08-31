# Sessie 2026-08-30/31 — mindmap-fix, STT-hallucinaties, lichte viewers, bearer-gate

## Gemerged (dev)

- **#92** `fix(notebooks)`: mindmap-viewer rendert weer (issue #95). Drieledige root cause: `markmap-autoloader@0.16.5` bestaat niet op npm (CDN-404); de report-CSP (`connect-src 'self'`) blokkeert de runtime-fetches van de autoloader; `svg.__mm` bestaat niet in markmap 0.18. Fix: gepinde d3/markmap-lib/markmap-view-bundles met SRI via `script-src`, expliciete `Transformer`/`Markmap.create`, event-delegation voor node-clicks, markdown JSON-embedded. Meegenomen: 18 stale tests uit `8b8cdf0`/`86d8084` hersteld (focus-kwarg-fakes, in-frame-viewer-contract).
- **#93** `fix(email)`: directe `/api/email/*`-routes weigeren bearer-tokens (403) — scope-handhaving hoort in de `/api/codex/*`-proxy. Codex-proxy en `X-Ithaka-Internal` ongemoeid. Live bewezen met `todos:read`-token.
- **#94** `test(chat-llm)`: 11 tests `_hybrid_retrieve` + 17 tests `_DegenerateStreamGuard` (review-follow-up #89).
- **#97** `fix(stt)`: Whisper-stiltehallucinaties (issue #96, "Ondertiteld door de Amara.org gemeenschap", live ook "TV GELDERLAND 2021."). Tweetraps: ffmpeg-volumedetect-energie-gate (stilte < -50 dB bereikt de provider nooit, faalt open zonder ffmpeg) + frasenfilter-vangnet op beide providers + `vad_filter=True` voor lokale faster-whisper. E2E: stilte → `""`, TTS-spraak → exact transcript.
- **#98** `fix(notebooks)`: alle artifact-viewers altijd licht — `force_light` op `generate_visual_report` (notebook-adapter zet 'm aan; research-rapporten behouden dark-mode), infographic-dark-block verwijderd.

- **#99** `chore(admin)`: dode single-account `initCalDAV` verwijderd (-52 regels; no-op via ontbrekende DOM-ids, gevonden bij de CalDAV-verificatie).

## Overig

- Gmail (`ed.de.feber@gmail.com`) als default e-mailaccount gezet (was Outlook/DavMail) op verzoek van Ed, via `POST /api/email/accounts/{id}/set-default`.
- Google-agenda gekoppeld (13:30): app-wachtwoord werkt alleen op het **legacy** CalDAV-endpoint `www.google.com/calendar/dav/<email>/user/` — het moderne `apidata.googleusercontent.com/caldav/v2` is OAuth-only en gaf Unauthorized. 38 events over 4 Google-kalenders gesynct, 0 fouten; gotcha ook in CLAUDE.md gezet. Infographic-poster (#100) en Brain-uitleg + memory-extractor-issue #101 ook deze sessie.
- Google-agenda als 2e CalDAV-account: onderzocht, **geen code nodig** — multi-account CRUD + Google-URL-mapping (`_google_caldav_events_url`) + per-account writeback bestaan al. Alleen een Google-app-wachtwoord van Ed nodig; instructie in de chat overgedragen. Eén cleanup-kandidaat: dode `initCalDAV()` in `static/js/admin.js:2619` (oude single-account-velden, onbereikbaar).
- Notebooks NL-taalproef en zomaar zijn door Ed zelf verwijderd (test-notebooks); samenwijzer is de actieve.
- GitHub "run failed"-mails verklaard: open PR #91 (dev→main, uit de Qwen-sessie) hertestte bij elke dev-push zijn ongeldige (Nederlandstalige) PR-body via `pull_request_target`. Body template-conform gemaakt → alle checks op #91 groen; PR bewust open gelaten als release-beslissing. Workflow-tuning-kandidaat genoteerd: description/title-checks kunnen `synchronize` overslaan.
- Suite na alle merges: 5306 passed, 3 skipped. Stack draait op dev-equivalente code (NVIDIA-overlay).

## Open backlog (ongewijzigd uit #89-review)

Grote functie-splits (`stream_agent_loop` 1174r e.a.), pip-install-timeout-cap, image-input-validatie, plugin-install-rollback, email 29-sqlite-conn-audit.
