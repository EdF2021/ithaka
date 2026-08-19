# Notebooks-werkruimte (3-panelen) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** NotebookLM-stijl fullscreen-werkruimte voor notebooks: Bronnen (checkbox-selectie, werkend retrievalfilter) | Gesprek (bestaande gegronde chat + vervolgvraag-chips) | Studio (artifacts + podcast).

**Architecture:** De bestaande `#chat-container` blijft op zijn plek; de werkruimte is een body-class (`notebook-workspace-open`) die een topbalk + linker/rechter-zijpaneel toont en de chat via CSS in het midden klemt. Backend krijgt twee toevoegingen: een `source_ids`-filter door de chat→RAG-keten (Chroma `$in` op `document_id`) en `POST /api/notebooks/{id}/suggest_questions` (utility-model, 3 vervolgvragen).

**Tech Stack:** FastAPI, vanilla-JS ES-modules (geen framework/build), Chroma, pytest (asyncio auto), statische js-tests (tekst-asserties).

**Spec:** `docs/superpowers/specs/2026-08-19-notebooks-workspace-design.md`

## Global Constraints

- Branch: `feat/notebooks-workspace`; Conventional Commits; kleine commits per stap.
- Dageraad-huisstijl: alléén bestaande CSS-variabelen (`--panel`, `--border`, `--fg`, `--red`, `--color-muted`) en bestaande klassen (`.dashboard-card`, `.list-item`, modal-primitieven). **Geen Unicode-emoji** — monochrome inline-SVG of tekst.
- Testcommando's: `.venv/bin/python -m pytest <file> -q` en `node --check static/js/<file>.js`.
- UI-teksten in het Engels (bestaande notebooks-UI is Engels: "Sources", "Generate", …).
- De gewone (niet-notebook) chat mag op geen enkele manier veranderen zolang de werkruimte dicht is.

---

### Task 1: Backend — `source_ids`-filter door de chat→RAG-keten

**Files:**
- Modify: `src/request_models.py:7-24` (ChatRequest)
- Modify: `routes/chat_routes.py:515-570` (chat_endpoint) en `:631-880` (chat_stream, leest raw body)
- Modify: `routes/chat_helpers.py:641-660` (build_chat_context) en `:762` (_preface_kwargs)
- Modify: `src/chat_processor.py:446-460` (build_context_preface), `:275-291` (_rag_preface)
- Modify: `src/rag_manager.py:35-44` (search)
- Modify: `src/rag_vector.py:343-380` (search, where-opbouw) en `:421-439` (_keyword_search_fallback)
- Test: `tests/test_rag_source_filter.py` (nieuw)

**Interfaces:**
- Consumes: bestaande keten (zie regels hierboven; chunk-metadata bevat al `document_id`).
- Produces: `source_ids: Optional[List[str]]` als parameter door de hele keten; `VectorRAG.search(..., source_ids=None)` bouwt `{"document_id": {"$in": source_ids}}` als extra `$and`-conditie. Frontend-contract: JSON-veld `source_ids` (lijst van document-ids) in `/api/chat` en `/api/chat_stream`; weggelaten/None/lege lijst ⇒ geen filter (lege selectie wordt client-side geblokkeerd).

- [ ] **Step 1: Schrijf failing tests** in `tests/test_rag_source_filter.py`. Patroon: instantieer/mock `VectorRAG` zó dat `query_lanes` gemonkeypatcht wordt en de doorgegeven `where` wordt vastgelegd (zie bestaande rag-tests voor constructie; anders een minimale fake-collection). Testgevallen:

```python
def test_where_filter_combines_notebook_and_source_ids(...):
    # search(query, notebook_id="nb1", source_ids=["d1", "d2"]) →
    # where == {"$and": [{"notebook_id": "nb1"}, {"document_id": {"$in": ["d1", "d2"]}}]}

def test_source_ids_none_or_empty_means_no_document_filter(...):
    # source_ids=None én source_ids=[] → where == {"notebook_id": "nb1"} (ongewijzigd gedrag)

def test_keyword_fallback_respects_source_ids(...):
    # fallback-pad: chunk met document_id "d3" wordt overgeslagen bij source_ids=["d1"]

def test_chat_request_accepts_source_ids():
    # ChatRequest(message="x", session="s", source_ids=["a"]) parset; default None
```

- [ ] **Step 2:** Run `pytest tests/test_rag_source_filter.py -q` → verwacht FAIL (TypeError: unexpected keyword / veld bestaat niet).
- [ ] **Step 3: Implementeer de keten.** In `rag_vector.py` search():

```python
def search(self, query, k=5, owner=None, notebook_id=None, source_ids=None):
    ...
    conditions = []
    if owner: conditions.append({"owner": owner})
    if notebook_id: conditions.append({"notebook_id": notebook_id})
    if source_ids:
        conditions.append({"document_id": {"$in": list(source_ids)}})
    ...
```

En in `_keyword_search_fallback`: naast de bestaande notebook_id-skip ook `if source_ids and meta.get("document_id") not in source_ids: continue`. Vervolgens `source_ids` als keyword door `rag_manager.search` → `_rag_preface` → `build_context_preface` → `chat_helpers.build_chat_context` (+ `_preface_kwargs`) → beide endpoints. In `chat_endpoint`: `chat_request.source_ids`. In `chat_stream` (raw body): lees het veld op dezelfde plek waar de andere body-velden gelezen worden; accepteer alleen een lijst van strings, anders negeren. Belangrijk: `source_ids` alleen doorgeven als er óók een notebook_id is (buiten notebooks negeren — het filter is een notebook-feature).
- [ ] **Step 4:** `pytest tests/test_rag_source_filter.py -q` → PASS; draai ook `pytest tests/ -k "rag or chat_routes" -q` als regressiecheck.
- [ ] **Step 5:** Commit `feat(notebooks): source_ids-retrievalfilter door de chatketen`.

---

### Task 2: Backend — `POST /api/notebooks/{id}/suggest_questions`

**Files:**
- Create: `src/notebook_suggest.py`
- Modify: `routes/notebook_routes.py` (nieuw endpoint na het artifacts-blok, regel ~345)
- Test: `tests/test_notebook_suggest.py` (nieuw)

**Interfaces:**
- Consumes: `task_llm_call_async(messages, owner=..., wait_for_quiet=False, workload="foreground")` uit `src/task_endpoint.py:58`; `_get_owned_notebook` (notebook_routes.py:70); `SessionLocal`; `get_current_user`.
- Produces: `POST /api/notebooks/{notebook_id}/suggest_questions` met body `{"question": str, "answer": str}` → `{"questions": ["…", "…", "…"]}` (0–3 strings; parse-falen ⇒ `{"questions": []}`, HTTP 200). `suggest_questions(question, answer, owner) -> list[str]` in `src/notebook_suggest.py`; `parse_questions(text) -> list[str]` als pure functie.

- [ ] **Step 1: Failing tests** in `tests/test_notebook_suggest.py` — twee delen:

```python
# (a) parser als pure functie
from src.notebook_suggest import parse_questions

def test_parse_json_array():
    assert parse_questions('["Waarom?", "Hoe?", "Wat?"]') == ["Waarom?", "Hoe?", "Wat?"]

def test_parse_json_in_prose_and_fences():
    txt = 'Hier zijn ze:\n```json\n["A?", "B?"]\n```'
    assert parse_questions(txt) == ["A?", "B?"]

def test_parse_garbage_returns_empty():
    assert parse_questions("geen json hier") == []
    assert parse_questions('{"vraag": "x"}') == []  # geen array van strings

def test_parse_caps_at_three_and_strips():
    assert parse_questions('[" A? ", "B?", "C?", "D?"]') == ["A?", "B?", "C?"]

# (b) route: zelfde patroon als tests/test_routes_notebook_artifacts.py
#    (make_temp_sqlite, monkeypatch nbr.SessionLocal + nbr.get_current_user,
#     monkeypatch nbr.suggest_questions met een fake) — gevallen:
#    - 200 met {"questions": [...]} voor eigen notebook
#    - 404 voor andermans/onbekend notebook
#    - fake die een Exception gooit → 200 {"questions": []} (nooit 5xx)
#    - body zonder question/answer → 400
```

- [ ] **Step 2:** Run → FAIL (module bestaat niet).
- [ ] **Step 3: Implementeer** `src/notebook_suggest.py`:

```python
"""Vervolgvraag-suggesties voor notebook-chat (utility-model, best-effort)."""
import asyncio, json, logging, re
from src.task_endpoint import task_llm_call_async

logger = logging.getLogger(__name__)
_SUGGEST_TIMEOUT_S = 8
_JSON_ARRAY_RE = re.compile(r"\[[^\[\]]*\]", re.S)

_PROMPT = (
    "You suggest follow-up questions for a study conversation that is strictly "
    "grounded in a fixed set of sources. Given the user's question and the "
    "assistant's answer, propose exactly 3 short follow-up questions (max 12 "
    "words each, same language as the conversation) that the sources could "
    "plausibly answer. Reply with ONLY a JSON array of 3 strings."
)

def parse_questions(text):
    m = _JSON_ARRAY_RE.search(text or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = [q.strip() for q in data if isinstance(q, str) and q.strip()]
    return out[:3]

async def suggest_questions(question, answer, owner):
    messages = [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": f"Question:\n{question[:1000]}\n\nAnswer:\n{answer[:2000]}"},
    ]
    content = await asyncio.wait_for(
        task_llm_call_async(messages, owner=owner, wait_for_quiet=False, workload="foreground"),
        timeout=_SUGGEST_TIMEOUT_S,
    )
    return parse_questions(content)
```

Endpoint in `notebook_routes.py` (importeer `suggest_questions` module-level zodat tests hem kunnen monkeypatchen, zoals bij `generate_artifact`):

```python
@router.post("/api/notebooks/{notebook_id}/suggest_questions")
async def suggest_notebook_questions(request: Request, notebook_id: str):
    user = get_current_user(request)
    try:
        body = await request.json()
    except Exception:
        body = None
    question = (body or {}).get("question") if isinstance(body, dict) else None
    answer = (body or {}).get("answer") if isinstance(body, dict) else None
    if not question or not answer:
        raise HTTPException(status_code=400, detail="question en answer zijn verplicht")
    db_session = SessionLocal()
    try:
        _get_owned_notebook(db_session, notebook_id, user)
    finally:
        db_session.close()
    try:
        questions = await suggest_questions(question, answer, user)
    except Exception:
        logger.info("suggest_questions failed for notebook %s", notebook_id, exc_info=True)
        questions = []
    return {"questions": questions}
```

- [ ] **Step 4:** `pytest tests/test_notebook_suggest.py -q` → PASS.
- [ ] **Step 5:** Commit `feat(notebooks): suggest_questions-endpoint (utility-model, best-effort)`.

---

### Task 3: Frontend — werkruimte-skelet (open/dicht, panelen, kiezer-flow)

**Files:**
- Create: `static/js/notebookWorkspace.js`
- Modify: `static/js/notebooks.js` (grid-klik opent werkruimte i.p.v. `_showDetail`; detail-view-code blijft in deze task staan, verhuist in Task 4/6)
- Modify: `static/index.html` (script-tag naast de andere modules; workspace-root-div direct vóór `#chat-container`)
- Modify: `static/style.css` (nieuw blok naast de notebook-CSS, ~regel 41150)
- Test: `tests/test_notebook_workspace_static.py` (nieuw)

**Interfaces:**
- Consumes: `#chat-container` (index.html:986), sidebar-element (`nav`/`#sidebar` — exacte id in index.html opzoeken), `window.sessionModule` (`getSessions/loadSessions/selectSession`), `notebooks.js` `_openChat`-logica (sessie kiezen/aanmaken, `_resolveChatConfig`).
- Produces: `openNotebookWorkspace(nb)` / `closeNotebookWorkspace()` / `isNotebookWorkspaceOpen()` (default export-bundel + `window.notebookWorkspace`), body-class `notebook-workspace-open`, DOM-ids `#nbws-root`, `#nbws-topbar`, `#nbws-sources`, `#nbws-studio`, `#nbws-tabbar`. Panelen-API voor Task 4/6: lege containers `#nbws-sources-body`, `#nbws-studio-body` plus `_state = { notebook, sources: [], selection: Set }`.

- [ ] **Step 1: Failing static test** `tests/test_notebook_workspace_static.py`, patroon van `test_settings_admin_managed_tabs_static.py` (tekst-asserties op broncode):

```python
_WS = (_REPO / "static" / "js" / "notebookWorkspace.js").read_text(encoding="utf-8")
_NB = (_REPO / "static" / "js" / "notebooks.js").read_text(encoding="utf-8")
_HTML = (_REPO / "static" / "index.html").read_text(encoding="utf-8")
_CSS = (_REPO / "static" / "style.css").read_text(encoding="utf-8")

def test_workspace_module_exports_open_close():
    assert "export function openNotebookWorkspace" in _WS
    assert "export function closeNotebookWorkspace" in _WS

def test_body_class_drives_layout_and_chat_stays_untouched():
    assert "notebook-workspace-open" in _WS and "notebook-workspace-open" in _CSS
    # de werkruimte mag chat-container niet verplaatsen of verbergen:
    assert "chat-container" not in _WS or "appendChild" not in _WS

def test_grid_click_opens_workspace_not_detail():
    assert "openNotebookWorkspace" in _NB

def test_index_html_has_workspace_root():
    assert 'id="nbws-root"' in _HTML
```

- [ ] **Step 2:** Run → FAIL. `node --check` op nog niet bestaand bestand overslaan.
- [ ] **Step 3: Implementeer.** `#nbws-root` in index.html (hidden by default) met topbalk (terug-knop met chevron-SVG, notebooknaam, sessie-dropdown-placeholder `#nbws-session-select`, mobiele tabbar) en twee `<aside>`-panelen. CSS-kern:

```css
body.notebook-workspace-open #nbws-root { display: flex; }
#nbws-root { display: none; position: fixed; inset: 0; z-index: 10005; pointer-events: none; }
#nbws-root .nbws-panel { pointer-events: auto; background: var(--panel); border-color: var(--border); }
#nbws-sources { width: 280px; border-right: 1px solid var(--border); }
#nbws-studio { width: 320px; border-left: 1px solid var(--border); }
#nbws-topbar { pointer-events: auto; }
body.notebook-workspace-open #chat-container {
  margin-left: 280px; margin-right: 320px; padding-top: var(--nbws-topbar-h, 44px);
}
body.notebook-workspace-open .sidebar /* exacte selector opzoeken */ { display: none; }
```

Panelen krijgen ook `padding-top: var(--nbws-topbar-h)` zodat de topbalk (volle breedte, fixed top) erboven ligt. Collapse-knoppen per paneel togglen een `nbws-collapsed`-class (breedte → 0, chat-marge → 0; CSS-transitie op margin/width). `openNotebookWorkspace(nb)`: zet body-class, vult topbalk, roept de (uit notebooks.js herbruikte/verplaatste) sessie-kies-logica aan **zonder** de kiezer-modal te sluiten vóór succes, sluit daarna de notebooks-modal. `closeNotebookWorkspace()`: verwijdert body-class, stopt polls (Task 6), laat de actieve sessie staan. Escape sluit de werkruimte alleen als er geen modal boven ligt (gebruik het bestaande `escMenuStack`-patroon als dat generiek is, anders een eigen keydown-listener die checkt of `.modal` open is). In `notebooks.js`: grid-kaart-klik → `openNotebookWorkspace(nb)` (dynamic import zoals `_openArtifact` dat doet voor documentModule).
- [ ] **Step 4:** `pytest tests/test_notebook_workspace_static.py -q` → PASS; `node --check static/js/notebookWorkspace.js static/js/notebooks.js`.
- [ ] **Step 5:** Commit `feat(notebooks): werkruimte-skelet (3 panelen om bestaande chat)`.

---

### Task 4: Frontend — Bronnen-paneel met werkende selectie

**Files:**
- Modify: `static/js/notebookWorkspace.js` (bronnen-paneel vullen)
- Modify: `static/js/notebooks.js` (upload-helpers herbruikbaar exporteren of kopiëren van `_uploadSources`/`_setupUploadZone`/`_deleteSource`; oude `_renderSources` uit detail-view weghalen zodra vervangen)
- Modify: `static/js/chat.js` of `static/js/chatStream.js` — op de plek waar de request-payload voor `/api/chat_stream` wordt opgebouwd: `source_ids` toevoegen
- Modify: `static/style.css` (bron-rij met checkbox; teller-badge bij composer)
- Test: uitbreiden `tests/test_notebook_workspace_static.py`

**Interfaces:**
- Consumes: `GET/POST/DELETE /api/notebooks/{id}/sources`; Task 1's `source_ids`-contract; `_state.selection` uit Task 3.
- Produces: `window.notebookWorkspace.getSourceIdsForChat()` → `null` (alles geselecteerd of werkruimte dicht) | `string[]` (subset) | `[]` (niets — verzenden blokkeren). localStorage-sleutel `notebook_source_sel_<notebookId>` met JSON-array van ge-DEselecteerde ids (default alles aan; nieuwe bronnen automatisch aan).

- [ ] **Step 1: Failing static tests:**

```python
def test_chat_payload_includes_source_ids_hook():
    src = (_REPO / "static" / "js" / "chatStream.js").read_text(encoding="utf-8") \
          + (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    assert "getSourceIdsForChat" in src

def test_empty_selection_blocks_send():
    assert "Select at least one source" in _WS

def test_selection_persisted_per_notebook():
    assert "notebook_source_sel_" in _WS
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implementeer.** Bronnenlijst-rijen (`.list-item`) met native `<input type="checkbox">`, bestandsnaam, verwijder-knop (inline-confirm via het `_armConfirm`-patroon). Kop: "Select all"-checkbox (indeterminate bij deelverzameling) + "n/m sources"-teller die ook naast de composer als badge staat (klein element in de topbalk boven de composer, niet ín de chat-input-bar — de chat-DOM blijft onaangeraakt). Upload-zone bovenin het paneel (hergebruik FormData-flow). Selectie-wijziging → localStorage + teller-update. In de chat-payload-opbouw (chatStream.js/chat.js): 

```js
const nbws = window.notebookWorkspace;
const srcIds = nbws && nbws.isNotebookWorkspaceOpen() ? nbws.getSourceIdsForChat() : null;
if (srcIds !== null) payload.source_ids = srcIds;
```

plus vóór verzenden: `if (srcIds && srcIds.length === 0) { toon melding "Select at least one source"; return; }` — melding via het bestaande status/toast-mechanisme (zelfde als "Using <model>"-melding, `#/status`-element uit de recon).
- [ ] **Step 4:** Tests + `node --check` op alle gewijzigde js → PASS.
- [ ] **Step 5:** Commit `feat(notebooks): bronnen-paneel met werkend source-filter`.

---

### Task 5: Frontend — Gesprek-koppeling + vervolgvraag-chips

**Files:**
- Modify: `static/js/notebookWorkspace.js` (sessie-dropdown, chips)
- Modify: `static/js/notebooks.js` (`_openChat` opsplitsen: sessie-resolve-deel herbruikbaar maken zonder modal-close)
- Modify: `static/js/chatStream.js` (hook/event bij stream-einde — als er al een event of callback bestaat: gebruiken; anders een `document.dispatchEvent(new CustomEvent('ithaka:chat-stream-done', {detail:{sessionId}}))` op het bestaande afrondpunt)
- Test: uitbreiden `tests/test_notebook_workspace_static.py`

**Interfaces:**
- Consumes: Task 2's endpoint; `window.sessionModule`; stream-einde-hook.
- Produces: sessie-dropdown `#nbws-session-select` (bestaande notebook-sessies + "New chat"-optie); chips-container die na elk antwoord max 3 knoppen toont; klik → composer vullen (`#message`-textarea value + focus), niet auto-versturen.

- [ ] **Step 1: Failing static tests:**

```python
def test_stream_done_event_exists():
    stream = (_REPO / "static" / "js" / "chatStream.js").read_text(encoding="utf-8")
    assert "ithaka:chat-stream-done" in stream or "chatStreamDone" in stream

def test_chips_fetch_suggest_endpoint_and_fail_silently():
    assert "/suggest_questions" in _WS
    assert "catch" in _WS  # fetch-fout mag nooit een chatfout tonen
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implementeer.** Stream-einde-hook op het punt waar chatStream de assistant-message finaliseert (zoek het bestaande afrondpad; er is al een plek waar de UI "klaar"-status zet). Workspace-listener: alleen reageren als werkruimte open én event-sessie == actieve notebook-sessie; haal laatste user-vraag + assistant-antwoord uit de door sessionModule/chat bijgehouden state (of uit de DOM: laatste twee message-nodes in `#chat-history`), POST naar suggest_questions, render chips in een vaste strip direct boven de topbalk-onderrand van het middengebied (CSS: gepositioneerd boven de chat-input-bar, `pointer-events:auto`). Nieuwe vraag verstuurd → chips leegmaken. Sessie-dropdown: vult uit `sm.getSessions().filter(s => s.notebook_id === nb.id)`, wissel → `sm.selectSession(id)`; "New chat" → bestaande create-flow (`POST /api/session` met notebook_id + `_resolveChatConfig`).
- [ ] **Step 4:** Tests + `node --check` → PASS.
- [ ] **Step 5:** Commit `feat(notebooks): gesprek-paneel — sessieswitcher + vervolgvraag-chips`.

---

### Task 6: Frontend — Studio-paneel (artifacts + podcast)

**Files:**
- Modify: `static/js/notebookWorkspace.js` (studio-paneel)
- Modify: `static/js/notebooks.js` — verplaats `_renderArtifacts`, `_generateArtifact`, `_generatePodcast`, `_pollPodcast`, `_stopPodcastPoll`, `_togglePodcastPanel`, `_openArtifact` naar de werkruimte (of naar een gedeeld hulpbestand `static/js/notebookArtifactsUi.js` als beide plekken ze nodig hebben — kiezer heeft ze NIET nodig, dus verplaatsen en de detail-view + `_showDetail` volledig verwijderen)
- Modify: `static/index.html` (script-tag als nieuw bestand ontstaat)
- Test: uitbreiden `tests/test_notebook_workspace_static.py`

**Interfaces:**
- Consumes: bestaande artifact/podcast-endpoints (zie routes-tabel in recon); `window.documentModule.loadDocument(docId)`.
- Produces: studio-paneel met genereer-knoppenrij (5 soorten + podcast), artifact-lijst, podcast-rij met `<audio>`-player; `_openArtifact` opent de document-viewer als overlay **zonder** de werkruimte te sluiten (de viewer (`#doc-editor-pane`) heeft `z-index` boven `#nbws-root` nodig — check `10010`-regel uit style.css:14242, werkruimte zit op 10005).

- [ ] **Step 1: Failing static tests:**

```python
def test_detail_view_is_gone_from_notebooks_modal():
    assert "_showDetail" not in _NB

def test_workspace_keeps_running_when_artifact_opens():
    assert "closeNotebookWorkspace" not in _between(_WS, "function _openArtifact", "\n}\n")

def test_podcast_poll_stops_on_workspace_close():
    assert "_stopPodcastPoll" in _WS
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implementeer.** Code verhuizen met minimale aanpassing: containers-ids veranderen van `#notebook-artifacts` naar `#nbws-studio-body`-kinderen; `_openArtifact` laat de modal-close weg en checkt na `loadDocument` alleen op succes; poll-lifecycle gekoppeld aan werkruimte-open i.p.v. modal-open. In `notebooks.js` blijft alleen: modal-kiezer (grid + nieuw-notebook-form) en de upload-helpers die Task 4 hergebruikt.
- [ ] **Step 4:** Tests + `node --check` → PASS. Draai óók de volledige js-area-slice: `.venv/bin/python tests/run_focus.py --area js`.
- [ ] **Step 5:** Commit `feat(notebooks): studio-paneel — artifacts en podcast in de werkruimte`.

---

### Task 7: Mobiel (≤700px) + afronding

**Files:**
- Modify: `static/style.css` (media-query) en `static/js/notebookWorkspace.js` (tabbar-wiring)
- Test: uitbreiden `tests/test_notebook_workspace_static.py`

**Interfaces:**
- Consumes: DOM uit Task 3 (`#nbws-tabbar` met drie knoppen `data-nbws-tab="sources|chat|studio"`).
- Produces: op ≤700px tonen tabs precies één "paneel" (sources/studio full-width over de chat; tab "chat" verbergt beide panelen en toont de chat vol); Gesprek is default bij openen.

- [ ] **Step 1: Failing static test:**

```python
def test_mobile_tabbar_exists():
    assert 'data-nbws-tab="chat"' in _HTML or 'data-nbws-tab="chat"' in _WS
    assert "max-width: 700px" in _between(_CSS, "#nbws-root", "/* nbws-end */")
```

(zet een `/* nbws-end */`-marker aan het einde van het CSS-blok in Task 3 zodat `_between` ankert.)
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implementeer.** Media-query: panelen worden `position: fixed; inset: var(--nbws-topbar-h) 0 0 0; width: 100%` en alleen zichtbaar met een `nbws-tab-active`-class; chat-marges → 0. Tabbar (drie tekstknoppen in de topbalk, settings-tab-stijl `.settings-nav-item`-analoog maar eigen class) togglet die classes. Default-tab chat bij elke open.
- [ ] **Step 4:** Tests + `node --check` → PASS.
- [ ] **Step 5:** Commit `feat(notebooks): mobiele tabs voor de werkruimte`.

---

### Task 8: Integratie — volle suite, browser-smoke, PR

**Files:** geen nieuwe; dit is verificatie.

- [ ] **Step 1:** `.venv/bin/python -m pytest -q` (volledige suite) → alles groen; `node --check` op alle gewijzigde js-bestanden.
- [ ] **Step 2:** Smoke-instance (`ITHAKA_DATA_DIR=<vers> … --port 7003`), browser-smoke desktop 1280px én 360px conform de spec-checklist §6: notebook maken → 2 bronnen uploaden → chat met citaties → bron uitvinken → vraag over uitgevinkte bron levert "sources do not cover"-antwoord → chips verschijnen en vullen composer → artifact genereren + openen (werkruimte blijft) → werkruimte sluiten → gewone chat werkt. Console vrij van nieuwe fouten. Screenshots bewaren.
- [ ] **Step 3:** PR naar dev met smoke-output; code-review verwerken; CI groen; merge conform de merge-gate (smoke-output zichtbaar in chat).
