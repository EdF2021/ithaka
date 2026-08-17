# Sessie 2026-08-17 — Notebooks follow-ups (issues #6/#7/#8)

## Wat er gebouwd is

- **#6 Janitor** (`src/notebook_audio.py` + `app.py`): `cleanup_orphaned_audio()` ruimt
  `.podcast-*.tmp` en verweesde `<hex>.wav`-bestanden ouder dan 1 uur op (job-cap is 1800s,
  dus nooit een lopende job); uurlijkse loop naast `_null_owner_sweep_loop`, 300s startvertraging.
  Age-gate loopt vóór de DB-query zodat een net-gecommitte artifact-row zijn bestand altijd
  beschermt (eindreview-minor).
- **#7 Interactive gate** (`src/interactive_gate.py`): `_PASSIVE_PATTERNS` — één strakke regex
  `^/api/notebooks/[^/]+/podcast/[^/]+$` (GET-only) zodat de 2s-podcast-statuspoll niet meer als
  foreground-activiteit telt en achtergrondtaken tijdens een podcast-generatie een quiet-window
  krijgen. POST-start blijft tracken.
- **#8a Archief-UI** (`static/js/notebooks.js`): archive/unarchive-toggle per rij (one-click,
  inline SVG), "Show archived"-filter, gearchiveerde kaarten gedimd.
- **#8b Open chat hervat**: bestaande notebook-sessie wordt hervat (meest recent actieve) i.p.v.
  elke klik een nieuwe sessie; `notebook_id` zat al in `GET /api/sessions`.
- **#8c** stale JSDoc `_openArtifact` gecorrigeerd.
- **#8d Real-seam-test** (`tests/test_notebooks_gate_seam.py`): oefent de échte
  `_local_model_slot` + `track_interactive_request` rond het artifact-LLM-pad; reviewer bevestigde
  per mutatietest dat de test faalt als `workload="foreground"` regresseert.

## Review-vondsten

Per-taak reviews (sonnet): alle drie spec PASS / APPROVED; 2 T3-minors direct gefixt.
Eindreview hele branch (opus): READY WITH FIXES — 5 minors, alle gefixt (janitor-queryvolgorde,
`_openEpoch`-guard tegen close-na-reopen-race op 3 plekken, rationale-comment, empty-state-tekst,
stale NL-label in CSS-comment). Bevestigd: geen janitor×job-race (mtime = laatste segment-write,
`os.replace` behoudt hem), seam-test lekt geen global state (empirisch geprobeerd met injected
raise).

## Smoke

Verse instance :7001. Janitor-bewijs tegen echte smoke-db: 6 oude bestanden weg (incl. leftovers
van 2 mislukte seed-pogingen), referenced + verse bestanden behouden. Browser: archiveren → rij
weg; "Show archived" → gedimde kaart met Unarchive; unarchive → terug. Open chat 1e klik →
sessie aangemaakt + modal dicht; 2e klik → zélfde sessie hervat (API-bewijs: total=1). Mobiel
360px: bottom-sheet, geen horizontale overflow. Pytest: 174 notebook/gate-tests groen; volle
suite 4825 passed (4 pre-existing GPU-compose-failures, ongerelateerd).

## Proces

3 parallelle sonnet-implementers in eigen worktrees (disjuncte bestanden, pre-flight
conflictscan), cherry-pick naar feat/notebooks-followups, per-taak review + opus-eindreview,
minors door de controller. Sessielimiet halverwege: T1 had zijn commits al staan (alleen het
rapport ontbrak), de T3-reviewer is hervat via een vervolgbericht — beide zonder rework.

Ed de Feber, in nauwe samenwerking met Claude
