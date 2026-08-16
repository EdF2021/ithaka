# NotebookLM-functionaliteit in Ithaka — gap-analyse

*2026-08-13 — onderzoek via 3 parallelle verkenningen: NotebookLM-featureset (web + notebooklm-py-skill), Ithaka docs/RAG/chat, Ithaka audio/serving/agent-infra.*

## Samenvatting

NotebookLM = bronnen uploaden → strikt daarop gegronde chat met citaties → gegenereerde artifacts (podcast, video, study guide, mindmap, quiz, datatabel). Ithaka heeft verrassend veel halffabrikaten: RAG + Chroma, documentstore met viewer/versies/PDF-export, deep research met visueel HTML-rapport, TTS met meerdere engines, mermaid-rendering, task scheduler voor achtergrondjobs. Wat ontbreekt is het **structurerende concept** (notebook = afgebakende bronnenset) en de **koppelingen** tussen bestaande onderdelen.

## Wat Ithaka al (bijna) heeft

| NotebookLM-feature | Ithaka-status |
|---|---|
| Deep Research (agentic web-research + rapport) | ✅ Al aanwezig: `src/deep_research.py` + `visual_report.py` — inhoudelijk vergelijkbaar |
| Bron-upload (PDF/Office/EPUB/audio/YouTube) | ✅ Parsing bestaat (`document_processor.py`, markitdown, yt-transcripts) — maar versnipperd, zie gaps |
| Grounded chat met bronnen | 🟡 RAG-preface bestaat, maar altijd gemengd met modelkennis; citaties niet klikbaar |
| Study guide / briefing / FAQ / timeline | 🟡 `create_document` + visual-report-pipeline dekken dit vrijwel — alleen prompts/templates nodig |
| Mindmap | 🟡 Mermaid `mindmap` rendert vandaag al in de markdown-renderer (wel CDN-dependency) |
| Rapporten-bibliotheek / artifact-browsing | 🟡 `documentLibrary.js` + dashboard-widget-patroon zijn de natuurlijke plek |
| Podcast / audio overview | 🔴 TTS-engines kunnen per-call een voice aan, maar API geeft die niet door; geen lange-tekst-synthese, geen audio-URL, geen player |
| Video overviews | 🔴 Geen video-compositie; zwaarste gap |
| Notebook-concept (bronnenset per project) | 🔴 Bestaat niet: één globale Chroma-collectie, alleen `owner`-metadata |

## De 5 structurele gaps

1. **Geen notebook/bronnenset-concept.** Eén globale collectie `ithaka_rag`; geen per-project scoping. Hook: `where`-filter in `src/rag_vector.py:352` + collectienaamgeving `:40`.
2. **Chat is niet bron-strikt.** RAG is preface-injectie; nergens een "antwoord alléén uit de bronnen"-instructie (alleen de injectie-guard in `prompt_security.py:8`). Hook: grounding-systemprompt + `use_rag`-variant "sources-only".
3. **Citaties zijn dood.** `rag_sources` = `{filename, snippet, similarity}`; geen document_id, geen offsets, niet klikbaar (`chatRenderer.js:983`). Pad: metadata verrijken (`rag_vector.py:518`) → doorgeven (`chat_processor.py:270`) → open-at-offset-route + highlight in `document.js`.
4. **Twee losgekoppelde documentwerelden.** Chat-upload → `Document`-row met viewer maar géén embeddings; RAG-panel-upload → embeddings maar géén viewer. Brug slaan = hoogste-hefboom-wijziging (aldus de verkenning). Bijvangst-bugs: RAG-upload mangelt `.docx` (UTF-8-decode van zip, `personal_routes.py:305`), twee chunk-sizes in één collectie, extensie-allowlists spreken elkaar tegen.
5. **Audio-pipeline mist vier schakels** voor een podcast: voice-parameter door `TTSService.synthesize()`/`TTSRequest` heen (engines kunnen het al), server-side chunking voorbij de 5000-char-truncatie (`tts_service.py:158`), concatenatie (geen ffmpeg/pydub in requirements), durable audio-URL + `<audio>`-player (patroon: `generated_images.py`-route, regex whitelist uitbreiden met mp3/wav).

## Advies: drie fasen

**Fase 1 — fundament (grootste waarde):** notebook-concept + bron-strikte chat + klikbare citaties. Dit is wat NotebookLM ís; alle artifacts leunen erop. Omvat de Document↔Chroma-brug en de ingest-bugfixes.

**Fase 2 — tekst-artifacts (bijna gratis):** study guide, briefing, FAQ, quiz/flashcards (JSON + simpele UI), mermaid-mindmap. Generatie via bestaande agent-tools; scheduling via `BUILTIN_ACTIONS` (twee-regel-registratie in `builtin_actions.py:2734`); tonen via documentLibrary + dashboard-card.

**Fase 3 — audio overview:** twee-stemmen-dialoogscript (LLM) → per-beurt TTS met voice-param → concat (ffmpeg) → serve via generated-media-route → player-UI. Bouwt op de vier schakels uit gap 5. Kokoro vereist CUDA; endpoint-TTS als fallback.

**Niet doen:** video overviews (Veo-klasse generatie, disproportioneel), interactieve audio-mode (realtime STT↔TTS-invoeging), Drive-sync.

## Bijlagen (ruwe verkenningsrapporten)

Volledige subagent-rapporten staan in de sessie-log van 2026-08-13; kernvindplaatsen hierboven per gap gelinkt als `pad:regel`.
