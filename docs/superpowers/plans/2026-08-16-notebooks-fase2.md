# Notebooks Fase 2 (tekst-artifacts) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eén-klik-generatie van study guide/briefing/FAQ/quiz/mindmap-documenten uit notebook-bronnen, gelist in de notebook-detailweergave.

**Architecture:** Nieuwe koppeltabel `notebook_artifacts` (Document blijft schoon); generatiemodule `src/notebook_artifacts.py` op `task_llm_call_async`; drie endpoints in `routes/notebook_routes.py`; Artifacts-sectie in `static/js/notebooks.js`.

**Tech Stack:** FastAPI + SQLAlchemy + vanilla JS (geen build), mermaid via bestaande CDN-load.

**Spec:** `docs/superpowers/specs/2026-08-16-notebooks-fase2-design.md` — bindend; lees eerst.

## Global Constraints

- Virtualenv: **`/home/eddef/projects/ithaka/.venv/bin/python`** (absoluut pad; `./venv` bestaat niet).
- Commit-messages eindigen met exact `Ed de Feber, in nauwe samenwerking met Claude` — GEEN Co-Authored-By.
- **Geen Unicode-emoji** in UI of code; inline monochroom SVG of platte tekst. Geen `window.confirm/alert/prompt`.
- CSS: alleen bestaande variabelen/klassen; appends in het notebook-blok aan het EIND van `static/style.css`; Dageraad via `:root[data-theme="dageraad"]`-overrides bij de bestaande marker (~40975).
- Tests hermetisch: `tests.helpers.sqlite_db.make_temp_sqlite` voor DB-isolatie (GEEN `:memory:` — per-connection-isolatie breekt in deze codebase); geen netwerk; `task_llm_call_async` altijd monkeypatchen.
- Constants-regel: paden/URLs alleen via `src/constants.py`; nooit hardcoded `/app/...` of `localhost:7000`.
- Bestandsnaam-taxonomie: testbestanden heten `tests/test_services_*.py` / `tests/test_routes_*.py` (auto-tagging).
- Raak `routes/personal_routes.py` en de Fase 1-bestanden niet aan behalve waar dit plan het zegt.

---

### Task 1: Datamodel — NotebookArtifact

**Files:**
- Modify: `core/database.py` (naast `class NotebookSource`, ~1720)
- Test: `tests/test_services_notebook_artifacts.py` (nieuw; alleen de model-tests uit deze taak)

**Interfaces:**
- Produces: `class NotebookArtifact(Base, TimestampMixin)`: tabel `notebook_artifacts`, kolommen `id` (String PK), `notebook_id` (String, `ForeignKey("notebooks.id", ondelete="CASCADE")`, index=True, nullable=False), `document_id` (String, `ForeignKey("documents.id", ondelete="CASCADE")`, nullable=False), `kind` (String, nullable=False). `to_dict()` → `{id, notebook_id, document_id, kind, created_at, updated_at}` (isoformat zoals `NotebookSource.to_dict`).

- [ ] **Step 1: failing test** — in nieuw testbestand, patroon kopiëren van `tests/test_services_notebooks_db.py` (make_temp_sqlite + eigen engine + `Base.metadata.create_all`):

```python
def test_notebook_artifact_roundtrip(tmp_db):
    art = NotebookArtifact(id="a1", notebook_id=nb.id, document_id=doc.id, kind="faq")
    session.add(art); session.commit()
    d = art.to_dict()
    assert d["kind"] == "faq" and d["document_id"] == doc.id and "created_at" in d

def test_artifact_cascade_on_notebook_delete(tmp_db):
    # notebook weg -> artifact-row weg (ORM-cascade zoals NotebookSource-test in test_services_notebooks_db.py)

def test_artifact_cascade_on_document_delete(tmp_db):
    # document weg -> artifact-row weg (FK CASCADE; op sqlite: PRAGMA foreign_keys aan, zie bestaand testbestand)
```

- [ ] Step 2: run → FAIL (class bestaat niet). Step 3: model toevoegen (spiegel `NotebookSource` incl. relationship op `Notebook` met `cascade="all, delete-orphan"`). Step 4: run → PASS + `python -m py_compile core/database.py`. Step 5: commit `feat(notebooks): NotebookArtifact-datamodel`.

---

### Task 2: Generatiemodule — src/notebook_artifacts.py

**Files:**
- Create: `src/notebook_artifacts.py`
- Test: `tests/test_services_notebook_artifacts.py` (uitbreiden)

**Interfaces:**
- Consumes: `NotebookArtifact` (T1), `Document`/`NotebookSource`/`Notebook` uit `core.database`, `task_llm_call_async` uit `src.task_endpoint` (:58), `fire_event` uit `src.event_bus`, untrusted-wrap uit `src.prompt_security` (lees hoe Fase 1 `untrusted_context_message` gebruikt in `src/chat_processor.py` en spiegel dat patroon voor de bron-payload).
- Produces:
  - `ARTIFACT_KINDS: dict[str, dict]` — keys exact `study_guide, briefing, faq, quiz, mindmap`; per kind `{"label": str, "prompt": str}`. Labels: `Studiegids, Briefing, FAQ, Quiz, Mindmap`. Prompts per spec (taal van de bronnen; markdown; geen preamble; mindmap = één ```mermaid-fence met `mindmap`-syntax; quiz-antwoorden per de HTML-verificatie in Step 1).
  - `MAX_CONTEXT_CHARS = 60_000`
  - `gather_source_text(notebook, db_session) -> str` — indexed sources met `document_id`, blokken `=== BRON: <filename> ===\n<tekst>`, proportionele cap met `\n(bron ingekort)`-marker.
  - `async generate_artifact(notebook_id: str, owner: str, kind: str, db_session) -> NotebookArtifact` — ValueError bij onbekend kind of geen bronnen; Document + artifact-row pas ná geslaagde LLM-call; titel `f"{notebook.name} — {label}"`; `session_id=None`; `fire_event("document_created", owner)` na commit.

- [ ] **Step 1 (verify-first): quiz-formaat** — check `static/js/markdown.js` `mdToHtml`: overleeft raw `<details><summary>` de renderer (niet ge-escaped/gestript)? Zo ja → quiz-prompt gebruikt `<details><summary>Antwoord</summary>…</details>` per vraag; zo nee → "Antwoorden"-sectie onderaan. Leg de uitkomst vast in het task-report.
- [ ] **Step 2: failing tests** (monkeypatch `src.notebook_artifacts.task_llm_call_async` met een async fake die de messages vastlegt en `"# Gegenereerd"` retourneert):

```python
async def test_generate_creates_document_and_row(...):
    art = await generate_artifact(nb.id, "own", "faq", session)
    doc = session.get(Document, art.document_id)
    assert doc.title == "Testboek — FAQ" and doc.owner == "own" and doc.current_content == "# Gegenereerd"

async def test_prompt_contains_source_blocks_and_kind_prompt(...):
    # captured messages: system bevat ARTIFACT_KINDS["faq"]["prompt"]; user bevat "=== BRON: a.txt ==="

async def test_llm_failure_leaves_no_rows(...):
    # fake raise -> geen Document, geen NotebookArtifact

async def test_no_sources_raises(...):  # ValueError
def test_gather_cap_proportional(...):  # 2 bronnen van 50k -> totaal <= 60k, beide '(bron ingekort)'
def test_gather_skips_failed_and_docless(...):
def test_unknown_kind_raises(...)
```

- [ ] Step 3: run → FAIL. Step 4: implementeren. Step 5: run → PASS, `py_compile`. Step 6: commit `feat(notebooks): artifact-generatiemodule (5 kinds, context-cap, task-LLM)`.

---

### Task 3: API — artifacts-endpoints + notebook-delete-opruiming

**Files:**
- Modify: `routes/notebook_routes.py`
- Test: `tests/test_routes_notebook_artifacts.py` (nieuw; patroon `tests/test_routes_notebooks.py`)

**Interfaces:**
- Consumes: `generate_artifact`/`ARTIFACT_KINDS` (T2), `NotebookArtifact` (T1).
- Produces:
  - `GET /api/notebooks/{id}/artifacts` → `{"artifacts": [to_dict...]}` nieuwste eerst.
  - `POST /api/notebooks/{id}/artifacts` body `{"kind": str}` → 200 met `to_dict()`; 400 bij onbekend kind of geen indexed bronnen (melding "Geen geïndexeerde bronnen"); 404 cross-owner/onbekende notebook; LLM-/endpointfout → 502 met str(exc).
  - `DELETE /api/notebooks/{id}/artifacts/{artifact_id}` → verwijdert artifact-row én Document-row; 404 cross-owner.
  - Bestaande notebook-DELETE: vóór de notebook-delete de Documents van alle artifacts van die notebook verwijderen (bron-Documents blijven, zoals Fase 1).

- [ ] Step 1: failing tests (CRUD, owner-404, kind-400, lege-bronnen-400, delete verwijdert Document mee, notebook-delete ruimt artifact-Documents maar niet bron-Documents; `generate_artifact` monkeypatchen op module-niveau van de route-file). Step 2: FAIL. Step 3: implementeren (geen wijziging aan `setup_notebook_routes`-signatuur; `app.py` hoeft niet aangepast). Step 4: PASS + volledige `tests/test_routes_notebooks.py` blijft groen. Step 5: commit `feat(notebooks): artifacts-API (genereer, lijst, verwijder)`.

---

### Task 4: Frontend — Artifacts-sectie in notebooks.js

**Files:**
- Modify: `static/js/notebooks.js` (detail-template in `_showDetail` ~458-482 + nieuwe `_renderArtifacts()`/`_generateArtifact()`/`_openArtifact()`)
- Modify: `static/style.css` (append in notebook-blok; Dageraad-override bij bestaande marker)

**Interfaces:**
- Consumes: T3-API; `window.documentModule.loadDocument(docId)` (`document.js:11038`, export :7067); patronen in notebooks.js: `_fetchJson` (:42), `_showError` (:80), `_armConfirm` (:90), `_openChat`-handoff (:412-456), `closeNotebooks()`.
- Produces: Artifacts-sectie tussen bronnenlijst en upload-zone: rij van 5 knoppen (labels uit vaste lijst `Studiegids/Briefing/FAQ/Quiz/Mindmap`, data-kind-attributen) + `#notebook-artifact-error` + lijst `.notebook-artifact-item` (label-pill, titel, datum, delete-kruisje met `_armConfirm`; mindmap-item toont hint "(Preview voor de mindmap)").

- [ ] Step 1: genereer-flow — klik: knop disabled + tekst "Genereren…", `POST`, bij succes `_renderArtifacts()` verversen, bij fout `_showError('#notebook-artifact-error', …)`, finally knop herstellen. Step 2: open-flow — spiegel `_openChat`: `window.documentModule?.loadDocument` → `closeNotebooks()`; fallback `import('./document.js')`. Step 3: delete-flow met `_armConfirm`. Step 4: CSS (alleen `.notebook-artifact-*`-regels die bestaande klassen niet dekken; kind-pill zoals status-pill). Step 5: `node --check static/js/notebooks.js`. Step 6: commit `feat(notebooks): Artifacts-sectie (genereer, open, verwijder)`.

---

### Task 5 (controller): integratie + smoke

Geen subagent-taak: volledige testsweep (`run_focus --area services` / `--area routes --fast`), `node --check`, eindreview hele branch, browser-smoke per spec-testplan (elk kind op een echte notebook; mindmap in Preview; quiz-details; mobiel 360px), PR + merge met zichtbare smoke-output.
