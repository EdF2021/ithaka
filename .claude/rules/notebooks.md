---
paths:
  - "routes/notebook_routes.py"
  - "src/notebook_*.py"
  - "static/js/notebook*.js"
  - "tests/test_notebook*.py"
  - "docs/notebooklm-gap-analyse.md"
---

# Notebooks (NotebookLM-style, cross-cutting)

`routes/notebook_routes.py` + `src/notebook_ingest.py` + `static/js/notebooks.js`. Sources are indexed per notebook via a `notebook_id` metadata filter in the RAG layer (`rag_manager.py` / `rag_vector.py`); notebook chat runs strictly grounded with paragraph-level `[n, ¶N]` citations (chunk metadata carries `paragraph_ref`/`section_hint`; multi-turn follow-ups are LLM-condensed to a standalone RAG query) and a server-side tool lockdown (all tools + MCP disabled).

Text artifacts (study guide/briefing/FAQ/quiz/mindmap/flashcards/data table) live in `src/notebook_artifacts.py`, with a validator-retry seam: slide decks validate against a JSON schema, and infographic/flashcards/mindmap each have a format validator (`validate_*_markdown` in their module) that triggers regeneration on malformed output; renderers in `src/notebook_flashcards.py`, `src/notebook_slides.py` (slide-JSON schema + standalone viewer), `src/notebook_infographic.py`, `src/notebook_mindmap.py` (mermaid mindmap parser + interactive collapsible viewer in `notebookWorkspace.js`). Infographic v2 (2026-09-03): the model emits JSON (`extract_infographic`), rendered by `render_infographic_v2`; legacy markdown artifacts still render through the old parser. Per-block AI illustrations come from an async job in `src/notebook_illustrations.py` (covers-style registry, max 5 images at quality `low`, only when `image_gen_enabled`), served via `/api/notebook-illustration/{fn}` with its own hourly janitor; the viewer polls `…/artifacts/{id}/illustrations` (passive in `_PASSIVE_PATTERNS`).

The podcast pipeline (LLM dialogue script → per-turn TTS → streaming WAV concat, async job mirroring `research_handler.py`) lives in `src/notebook_audio.py`; the video pipeline (slide JSON + narration → Pillow PNG frames → per-slide TTS → ffmpeg mp4; ffmpeg + fonts-dejavu-core are in the Docker image) in `src/notebook_video.py`, served via `/api/notebook-video/{fn}`. Both host hourly janitors for orphaned media files (wired in `app.py`). Web sources: search bar in the sources panel → SearXNG → `POST .../sources/url` → `ingest_notebook_url` (fetch via `services/search/content.py`, never the divergent `src/search/` duplicate). Podcast and video status polls are on the passive list in `src/interactive_gate.py` (`_PASSIVE_PATTERNS`).

All generated notebook output (chat, artifacts, podcast, video, question suggestions) is forced to Dutch: every generation prompt embeds `DUTCH_OUTPUT_RULE` from `src/notebook_language.py` — new generators must include it, and the rule is changed there, never inline. Datamodel incl. `NotebookArtifact` (with `audio_path`/`video_path`) and `NotebookSource.url` lives in `core/database.py`.

**Gotcha:** a synchronous LLM call inside a tracked request self-deadlocks on two background gates (`wait_for_interactive_quiet` + the `_local_model_slot` workload gate) — pass `wait_for_quiet=False, workload="foreground"`, or better: use an async job like the podcast does. Regression test: `tests/test_notebooks_gate_seam.py`. Design docs: `docs/notebooklm-gap-analyse.md` (status header lists all phase session logs) and `docs/superpowers/specs/` (fase 2, 3 en 4).
