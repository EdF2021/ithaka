# Notebook-infographic v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the markdown infographic with a JSON-driven, NotebookLM-style HTML layout (columns, steps, hero, comparison bars, key numbers) whose per-block illustrations are generated asynchronously through the existing image pipeline and swapped in live by the viewer.

**Architecture:** The LLM emits one ```json fence validated by `extract_infographic` (Dutch errors, fed back on retry via the existing `_KIND_VALIDATORS` seam). `generate_infographic()` detects JSON vs. legacy markdown and dispatches to a new `_TEMPLATE_V2` renderer. A new `src/notebook_illustrations.py` (modelled on `src/notebook_covers.py`) runs an in-memory async job that calls `do_generate_image` per block (max 5, quality `low`), copies each PNG into `NOTEBOOK_INFOGRAPHICS_DIR` and writes `illustrations: {block_id: filename}` back into the stored JSON after every image. Routes: job start hooked into `POST …/artifacts`, a status endpoint the viewer polls (passive in the interactive gate), an authenticated file-serving route and an hourly janitor.

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy (sqlite), asyncio jobs, vanilla inline JS in the viewer page, pytest (`asyncio_mode=auto`).

**Spec:** `docs/superpowers/specs/2026-09-03-notebooks-infographic-v2-design.md`

## Global Constraints

- Constants rule: every writable path comes from `src/constants.py` (`NOTEBOOK_INFOGRAPHICS_DIR` is new; `GENERATED_IMAGES_DIR` exists). Never `Path(__file__)`, never `/app/...`, never relative `"data/..."`. Guard `os.makedirs` with `try/except OSError`.
- Every generation prompt embeds `DUTCH_OUTPUT_RULE` from `src/notebook_language.py` (already in `_BASE_RULES`; do not inline a copy).
- LLM calls inside a tracked request use `wait_for_quiet=False, workload="foreground"` (already the case in `generate_artifact`; this plan adds no new LLM calls).
- No Unicode emoji in UI or code; inline monochrome SVG only. Viewers are force-light (no `prefers-color-scheme`), no external resources (`<script src` / CDN fonts / remote images forbidden), all interpolated text through `html.escape`.
- `image_quality` admin setting is deliberately **not** followed; illustrations always use quality `low`. `image_model` is followed (via `do_generate_image` defaults).
- Max **5** illustrations per infographic; hero at `1536x1024`, others `1024x1024`; job time-out `JOB_TIMEOUT_SECONDS = 300`.
- Illustration prompts must be English, ≤ 200 chars, and may not contain the words `text`, `label`, `caption`, `words`, `letters` (validator rejects).
- Existing `tests/test_notebook_infographic.py` tests for the legacy markdown parser/renderer stay green — the markdown path is kept for previously generated artifacts.
- Commits: Conventional Commits, `type(scope): summary`. Commit message trailer: end with ONLY `Ed de Feber, in nauwe samenwerking met Claude` — no `Co-Authored-By`.
- Before merge: UI smoke on `:7001` (desktop + 360 px) with output pasted in chat (repo CLAUDE.md rule). Test runner is `.venv/bin/python -m pytest`.

---

## File structure

| File | Responsibility |
|---|---|
| `src/constants.py` (modify) | `NOTEBOOK_INFOGRAPHICS_DIR` + guarded makedirs |
| `src/notebook_infographic.py` (modify) | `is_infographic_v2`, `extract_infographic` (schema), `render_infographic_v2` + `_TEMPLATE_V2` + `_VIEWER_SCRIPT`, dispatch in `generate_infographic`. Legacy parser/renderer/validator untouched. |
| `src/notebook_artifacts.py` (modify) | New `infographic` prompt (JSON schema), `_KIND_VALIDATORS["infographic"] = extract_infographic` |
| `src/notebook_illustrations.py` (create) | Async illustration job, prompt builder, block selection, JSON persistence, filename regex, `resolve_illustration_path`, `artifact_id_from_filename`, `cleanup_orphaned_illustrations`, `get_artifact_job` |
| `routes/notebook_routes.py` (modify) | Job start in `create_artifact`, `GET …/artifacts/{id}/illustrations`, `GET /api/notebook-illustration/{filename}`, `poll_url` into the report route |
| `src/interactive_gate.py` (modify) | Passive pattern for the status poll |
| `app.py` (modify) | Hourly `cleanup_orphaned_illustrations` janitor |
| `static/js/notebookWorkspace.js` (modify) | Tile `title` attribute mentions illustrations |
| `tests/test_notebook_infographic_v2.py` (create) | Schema + detection + renderer |
| `tests/test_notebook_illustrations.py` (create) | Job, persistence, filename whitelist, janitor |
| `tests/test_routes_notebook_infographic.py` (create) | Route behaviour |
| `tests/test_interactive_gate_passive.py` (modify) | New passive pattern |
| `tests/test_notebook_infographic.py` (modify) | One assertion on the prompt (`"Key numbers"` → JSON) |
| `CLAUDE.md`, `docs/sessions/…` (modify/create) | Notebooks paragraph + session log |

Shared vocabulary used across tasks (defined in Task 1 unless noted):

```python
# src/notebook_infographic.py
MAX_ILLUSTRATIONS = 5
BLOCK_TYPES = {"column", "steps", "icon_card", "hero", "comparison", "key_numbers"}
COLUMN_CHILD_TYPES = {"steps", "icon_card", "key_numbers"}
def is_infographic_v2(content: str) -> bool
def extract_infographic(content: str) -> dict   # raises ValueError (Dutch)
def iter_blocks(data: dict) -> list[dict]        # document order, columns' children flattened after their column
def generate_infographic(title, markdown, notebook_name, generated_at, *, illustrations_url_base="/api/notebook-illustration/", poll_url=None) -> str  # Task 3
# src/notebook_illustrations.py (Task 4/5)
JOB_TIMEOUT_SECONDS = 300
ILLUSTRATION_FILE_RE = re.compile(r"^([0-9a-f-]{36})-([a-z0-9][a-z0-9_-]{0,39})-([0-9a-f]{8})\.png$")
ILLUSTRATION_HEADERS = {...}
def build_illustration_prompt(prompt: str, *, hero: bool) -> str
def select_illustration_blocks(data: dict) -> list[dict]
def start_illustration_job(notebook_id, artifact_id, owner, db_session_factory=None) -> str
def get_artifact_job(artifact_id: str, owner: str) -> Optional[dict]
def load_illustrations(content: str) -> dict[str, str]
def artifact_id_from_filename(filename: str) -> str
def resolve_illustration_path(filename: str) -> Path
def cleanup_orphaned_illustrations(db_session_factory, *, max_age_seconds=3600) -> int
```

---

### Task 1: JSON schema extractor + v2 detection

**Files:**
- Modify: `src/notebook_infographic.py` (append after `validate_infographic_markdown`, plus new imports `json` and `from src.notebook_slides import _JSON_FENCE_RE`)
- Test: `tests/test_notebook_infographic_v2.py` (create)

**Interfaces:**
- Consumes: `_JSON_FENCE_RE` from `src/notebook_slides.py`; `_ICONS` (existing dict in this module).
- Produces: `is_infographic_v2(content) -> bool`; `extract_infographic(content) -> dict` with keys `title`, `subtitle`, `takeaway`, `blocks`, `illustrations`; `iter_blocks(data) -> list[dict]`; constants `MAX_ILLUSTRATIONS`, `BLOCK_TYPES`, `COLUMN_CHILD_TYPES`, `TEXT_IN_IMAGE_WORDS`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_notebook_infographic_v2.py`:

```python
"""Infographic v2: JSON schema extraction, legacy detection, v2 renderer.

Pure unit tests — no DB, no LLM. Spec: docs/superpowers/specs/
2026-09-03-notebooks-infographic-v2-design.md (Deel A + C).
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-infographic-v2")

import copy
import json
from datetime import datetime

import pytest

from src.notebook_infographic import (
    MAX_ILLUSTRATIONS,
    extract_infographic,
    is_infographic_v2,
    iter_blocks,
)


def _valid_data():
    return {
        "title": "SamenWijzer in cijfers",
        "subtitle": "Wat de bronnen zeggen",
        "takeaway": "Begeleiding op maat werkt als de data klopt.",
        "blocks": [
            {"id": "bronnen", "type": "column", "heading": "Bronnen", "subheading": "Wat erin gaat",
             "illustration_prompt": "a stack of documents flowing into a funnel",
             "children": [
                 {"id": "stappen", "type": "steps", "heading": "Stappen",
                  "items": [{"label": "Upload", "text": "Bronnen toevoegen"},
                            {"label": "Index", "text": "Chunks in Chroma"}]},
                 {"id": "kaart-a", "type": "icon_card", "heading": "Grounded", "icon": "chat",
                  "text": "Antwoorden alleen uit bronnen."},
             ]},
            {"id": "hero", "type": "hero", "heading": "Eén werkruimte",
             "illustration_prompt": "a glowing hub connecting six nodes",
             "text": "Chat, studio en bronnen in één scherm."},
            {"id": "vergelijk", "type": "comparison", "heading": "Vergelijking",
             "rows": [{"label": "Bronnen", "value": "300 bronnen", "ratio": 0.6},
                      {"label": "Chats", "value": "500 chats", "ratio": 1.0}]},
            {"id": "cijfers", "type": "key_numbers", "heading": "Kerncijfers",
             "items": [{"number": "42%", "label": "geslaagd"},
                       {"number": "3", "label": "panelen"},
                       {"number": "12 weken", "label": "duur"}]},
            {"id": "output", "type": "column", "heading": "Output", "subheading": "Wat eruit komt",
             "children": [
                 {"id": "kaart-b", "type": "icon_card", "heading": "Podcast",
                  "text": "Twee stemmen, één script."},
                 {"id": "kaart-c", "type": "icon_card", "heading": "Video",
                  "illustration_prompt": "a film strip made of soft paper shapes",
                  "text": "Slides met voice-over."},
             ]},
        ],
    }


def _fenced(data):
    return "Hier is de infographic:\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```\n"


# ---- detection ----------------------------------------------------------

def test_detects_bare_json_and_fence_as_v2():
    assert is_infographic_v2(json.dumps(_valid_data())) is True
    assert is_infographic_v2(_fenced(_valid_data())) is True
    assert is_infographic_v2("  \n```json\n{\"a\":1}\n```") is True


def test_detects_legacy_markdown_as_not_v2():
    assert is_infographic_v2("# Titel\n\n## Key numbers\n- **3** — x\n") is False
    assert is_infographic_v2("") is False
    assert is_infographic_v2(None) is False


# ---- extract_infographic: happy path -------------------------------------

def test_extract_accepts_valid_fenced_json():
    data = extract_infographic(_fenced(_valid_data()))
    assert data["title"] == "SamenWijzer in cijfers"
    assert data["subtitle"] == "Wat de bronnen zeggen"
    assert data["takeaway"].startswith("Begeleiding")
    assert [b["type"] for b in data["blocks"]] == ["column", "hero", "comparison", "key_numbers", "column"]
    assert data["illustrations"] == {}


def test_extract_keeps_illustrations_map_and_drops_unknown_icon():
    raw = _valid_data()
    raw["illustrations"] = {"hero": "abc-hero-01234567.png"}
    raw["blocks"][0]["children"][1]["icon"] = "does-not-exist"
    data = extract_infographic(json.dumps(raw))
    assert data["illustrations"] == {"hero": "abc-hero-01234567.png"}
    assert data["blocks"][0]["children"][1]["icon"] is None


def test_iter_blocks_flattens_in_document_order():
    data = extract_infographic(json.dumps(_valid_data()))
    assert [b["id"] for b in iter_blocks(data)] == [
        "bronnen", "stappen", "kaart-a", "hero", "vergelijk", "cijfers", "output", "kaart-b", "kaart-c",
    ]


def test_more_than_max_illustration_prompts_is_allowed():
    raw = _valid_data()
    for b in raw["blocks"]:
        b["illustration_prompt"] = "abstract soft shapes"
        for c in b.get("children", []):
            c["illustration_prompt"] = "abstract soft shapes"
    data = extract_infographic(json.dumps(raw))
    assert sum(1 for b in iter_blocks(data) if b.get("illustration_prompt")) > MAX_ILLUSTRATIONS


# ---- extract_infographic: rejections (Dutch messages) --------------------

@pytest.mark.parametrize("mutate, fragment", [
    (lambda d: d.pop("title"), "title"),
    (lambda d: d.update(title="x" * 81), "title"),
    (lambda d: d.pop("takeaway"), "takeaway"),
    (lambda d: d.update(blocks=d["blocks"][:4]), "5"),
    (lambda d: d.update(blocks=d["blocks"] + [copy.deepcopy(d["blocks"][2]) | {"id": f"c{i}"} for i in range(4)]), "8"),
    (lambda d: d["blocks"].__setitem__(1, {**d["blocks"][1], "type": "icon_card"}), "hero"),
    (lambda d: d["blocks"].append({**d["blocks"][1], "id": "hero2"}), "hero"),
    (lambda d: [b.update(type="icon_card", text="x") for b in d["blocks"] if b["type"] == "column"], "column"),
    (lambda d: d["blocks"].append({**copy.deepcopy(d["blocks"][0]), "id": "col3",
                                   "children": [{**c, "id": c["id"] + "-x"} for c in d["blocks"][0]["children"]]}), "column"),
    (lambda d: d["blocks"][0].__setitem__("id", "hero"), "uniek"),
    (lambda d: d["blocks"][0].__setitem__("id", "Bad Id!"), "id"),
    (lambda d: d["blocks"][0]["children"].__setitem__(0, {**d["blocks"][0]["children"][0], "type": "hero"}), "column"),
    (lambda d: d["blocks"][0]["children"].__setitem__(0, {**d["blocks"][0]["children"][0], "type": "column", "children": []}), "column"),
    (lambda d: d["blocks"][0].__setitem__("children", d["blocks"][0]["children"][:1]), "children"),
    (lambda d: d["blocks"][0]["children"][0].__setitem__("items", [{"label": "a", "text": "b"}]), "steps"),
    (lambda d: d["blocks"][0]["children"][0]["items"].__setitem__(0, {"label": "a", "text": "x" * 121}), "120"),
    (lambda d: d["blocks"][1].__setitem__("text", "x" * 241), "240"),
    (lambda d: d["blocks"][2].__setitem__("rows", d["blocks"][2]["rows"][:1]), "comparison"),
    (lambda d: d["blocks"][2]["rows"][0].__setitem__("ratio", 1.5), "ratio"),
    (lambda d: d["blocks"][3].__setitem__("items", d["blocks"][3]["items"][:2]), "key_numbers"),
    (lambda d: d["blocks"][3]["items"].__setitem__(0, {"number": "1", "label": "een twee drie vier vijf zes zeven acht negen"}), "8"),
    (lambda d: d["blocks"][1].__setitem__("heading", "x" * 61), "60"),
    (lambda d: d["blocks"][1].__setitem__("illustration_prompt", "x" * 201), "200"),
])
def test_extract_rejects_schema_violations(mutate, fragment):
    raw = _valid_data()
    mutate(raw)
    with pytest.raises(ValueError) as exc:
        extract_infographic(json.dumps(raw))
    assert fragment.lower() in str(exc.value).lower()


@pytest.mark.parametrize("prompt", [
    "a poster with the text 'welkom'",
    "a sign with big LETTERS",
    "chart with a caption",
    "banner with words on it",
    "Label on a jar",
])
def test_extract_rejects_text_in_image_prompts(prompt):
    raw = _valid_data()
    raw["blocks"][1]["illustration_prompt"] = prompt
    with pytest.raises(ValueError) as exc:
        extract_infographic(json.dumps(raw))
    assert "illustration_prompt" in str(exc.value)


def test_extract_allows_prompt_words_that_merely_contain_forbidden_substrings():
    raw = _valid_data()
    raw["blocks"][1]["illustration_prompt"] = "a textile pattern and a labelled-free sky"
    extract_infographic(json.dumps(raw))  # 'textile' / 'labelled' are not whole-word hits


@pytest.mark.parametrize("content, fragment", [
    ("", "geen JSON"),
    ("```json\n{not json}\n```", "ongeldige JSON"),
    ("[1,2]", "geen object"),
])
def test_extract_rejects_non_json(content, fragment):
    with pytest.raises(ValueError) as exc:
        extract_infographic(content)
    assert fragment in str(exc.value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_notebook_infographic_v2.py -q`
Expected: ImportError (`cannot import name 'extract_infographic'`).

- [ ] **Step 3: Implement detection + extractor**

Append to `src/notebook_infographic.py` (add `import json` to the imports and `from src.notebook_slides import _JSON_FENCE_RE` — the slides module has no import back into this one, so no cycle):

```python
# ---------------------------------------------------------------------------
# v2: JSON content model (spec 2026-09-03 Deel A)
# ---------------------------------------------------------------------------

MAX_ILLUSTRATIONS = 5
BLOCK_TYPES = frozenset({"column", "steps", "icon_card", "hero", "comparison", "key_numbers"})
COLUMN_CHILD_TYPES = frozenset({"steps", "icon_card", "key_numbers"})
# Whole-word, case-insensitive: an illustration prompt asking for text in
# the image defeats the whole point of v2 (text stays in HTML).
TEXT_IN_IMAGE_WORDS = ("text", "label", "caption", "words", "letters")
_TEXT_IN_IMAGE_RE = re.compile(r"\b(" + "|".join(TEXT_IN_IMAGE_WORDS) + r")\b", re.IGNORECASE)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_V2_FENCE_START_RE = re.compile(r"^```(?:json)?\s*\n\s*\{", re.DOTALL)

MIN_BLOCKS, MAX_BLOCKS = 5, 8
MAX_COLUMNS = 2


def is_infographic_v2(content: Optional[str]) -> bool:
    """True when stored content is the v2 JSON model (bare object or ```json fence)."""
    s = (content or "").strip()
    return s.startswith("{") or bool(_V2_FENCE_START_RE.match(s))


def _req_str(obj: dict, key: str, where: str, *, max_len: int, required: bool = True) -> Optional[str]:
    val = obj.get(key)
    if val is None or (isinstance(val, str) and not val.strip()):
        if required:
            raise ValueError(f'{where}: veld "{key}" ontbreekt of is leeg')
        return None
    if not isinstance(val, str):
        raise ValueError(f'{where}: veld "{key}" moet een string zijn')
    val = val.strip()
    if len(val) > max_len:
        raise ValueError(f'{where}: veld "{key}" is te lang (maximaal {max_len} tekens)')
    return val


def _validate_block(raw: object, where: str, *, nested: bool, seen_ids: set) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} is geen object")
    block_id = _req_str(raw, "id", where, max_len=40)
    if not _SLUG_RE.match(block_id):
        raise ValueError(f'{where}: veld "id" moet een slug zijn (a-z, 0-9, - of _)')
    if block_id in seen_ids:
        raise ValueError(f'{where}: id "{block_id}" is niet uniek')
    seen_ids.add(block_id)
    btype = _req_str(raw, "type", where, max_len=20)
    if btype not in BLOCK_TYPES:
        raise ValueError(f'{where}: onbekend type "{btype}"')
    if nested and btype not in COLUMN_CHILD_TYPES:
        raise ValueError(f'{where}: type "{btype}" mag niet binnen een column staan')
    heading = _req_str(raw, "heading", where, max_len=60)
    icon = raw.get("icon")
    icon = icon if isinstance(icon, str) and icon in _ICONS else None
    prompt = _req_str(raw, "illustration_prompt", where, max_len=200, required=False)
    if prompt and _TEXT_IN_IMAGE_RE.search(prompt):
        raise ValueError(
            f'{where}: illustration_prompt mag geen tekst in beeld vragen '
            f'(geen {", ".join(TEXT_IN_IMAGE_WORDS)})'
        )
    block = {"id": block_id, "type": btype, "heading": heading, "icon": icon,
             "illustration_prompt": prompt}

    if btype == "column":
        block["subheading"] = _req_str(raw, "subheading", where, max_len=120)
        children = raw.get("children")
        if not isinstance(children, list) or not 2 <= len(children) <= 3:
            raise ValueError(f'{where}: veld "children" moet 2 tot 3 sub-blokken bevatten')
        block["children"] = [
            _validate_block(c, f"{where} > child {i}", nested=True, seen_ids=seen_ids)
            for i, c in enumerate(children, 1)
        ]
    elif btype == "steps":
        items = raw.get("items")
        if not isinstance(items, list) or not 2 <= len(items) <= 5:
            raise ValueError(f"{where}: steps heeft 2 tot 5 items nodig")
        block["items"] = [
            {"label": _req_str(it, "label", f"{where} stap {i}", max_len=60),
             "text": _req_str(it, "text", f"{where} stap {i}", max_len=120)}
            if isinstance(it, dict) else _raise(f"{where} stap {i} is geen object")
            for i, it in enumerate(items, 1)
        ]
    elif btype == "icon_card":
        block["text"] = _req_str(raw, "text", where, max_len=200)
    elif btype == "hero":
        block["text"] = _req_str(raw, "text", where, max_len=240)
    elif btype == "comparison":
        rows = raw.get("rows")
        if not isinstance(rows, list) or not 2 <= len(rows) <= 4:
            raise ValueError(f"{where}: comparison heeft 2 tot 4 rows nodig")
        cleaned_rows = []
        for i, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise ValueError(f"{where} row {i} is geen object")
            ratio = row.get("ratio")
            if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0 <= ratio <= 1:
                raise ValueError(f"{where} row {i}: ratio moet een getal tussen 0 en 1 zijn")
            cleaned_rows.append({
                "label": _req_str(row, "label", f"{where} row {i}", max_len=60),
                "value": _req_str(row, "value", f"{where} row {i}", max_len=80),
                "ratio": float(ratio),
            })
        block["rows"] = cleaned_rows
    elif btype == "key_numbers":
        items = raw.get("items")
        if not isinstance(items, list) or not 3 <= len(items) <= 5:
            raise ValueError(f"{where}: key_numbers heeft 3 tot 5 items nodig")
        cleaned_items = []
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict):
                raise ValueError(f"{where} item {i} is geen object")
            label = _req_str(it, "label", f"{where} item {i}", max_len=80)
            if len(label.split()) > 8:
                raise ValueError(f"{where} item {i}: label maximaal 8 woorden")
            cleaned_items.append({"number": _req_str(it, "number", f"{where} item {i}", max_len=40),
                                  "label": label})
        block["items"] = cleaned_items
    return block


def _raise(msg: str):
    raise ValueError(msg)


def extract_infographic(content: str) -> dict:
    """Parse + validate v2 JSON. Raises ValueError (Dutch) on any schema miss.

    Registered as the `infographic` validator in src/notebook_artifacts.py,
    so the message is fed back to the model on retry — same contract as
    extract_slide_deck. Returns a cleaned dict (stripped strings, unknown
    icons dropped) with an `illustrations` map (block_id -> filename) that
    the illustration job fills in later (src/notebook_illustrations.py).
    """
    m = _JSON_FENCE_RE.search(content or "")
    raw = (m.group(1) if m else (content or "")).strip()
    if not raw:
        raise ValueError("geen JSON gevonden in het antwoord")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"ongeldige JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("JSON is geen object")

    title = _req_str(data, "title", "infographic", max_len=80)
    subtitle = _req_str(data, "subtitle", "infographic", max_len=120, required=False)
    takeaway = _req_str(data, "takeaway", "infographic", max_len=240)
    blocks = data.get("blocks")
    if not isinstance(blocks, list) or not MIN_BLOCKS <= len(blocks) <= MAX_BLOCKS:
        raise ValueError(f'veld "blocks" moet {MIN_BLOCKS} tot {MAX_BLOCKS} blokken bevatten')
    seen: set = set()
    cleaned = [_validate_block(b, f"blok {i}", nested=False, seen_ids=seen)
               for i, b in enumerate(blocks, 1)]
    heroes = sum(1 for b in cleaned if b["type"] == "hero")
    if heroes != 1:
        raise ValueError(f"precies één blok van type hero verwacht (gevonden: {heroes})")
    columns = sum(1 for b in cleaned if b["type"] == "column")
    if columns < 1 or columns > MAX_COLUMNS:
        raise ValueError(f"1 tot {MAX_COLUMNS} blokken van type column verwacht (gevonden: {columns})")

    illustrations = data.get("illustrations")
    if not isinstance(illustrations, dict):
        illustrations = {}
    illustrations = {k: v for k, v in illustrations.items()
                     if isinstance(k, str) and isinstance(v, str)}
    return {"title": title, "subtitle": subtitle, "takeaway": takeaway,
            "blocks": cleaned, "illustrations": illustrations}


def iter_blocks(data: dict) -> List[dict]:
    """All blocks in document order; a column is followed by its children."""
    out: List[dict] = []
    for b in data.get("blocks", []):
        out.append(b)
        out.extend(b.get("children", []))
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_notebook_infographic_v2.py tests/test_notebook_infographic.py -q`
Expected: all pass (the parametrized table has 23 cases; if any message fragment doesn't match, adjust the message, not the test intent).

- [ ] **Step 5: Commit**

```bash
git add src/notebook_infographic.py tests/test_notebook_infographic_v2.py
git commit -m "feat(notebooks): infographic v2 JSON schema extractor + legacy detection

Ed de Feber, in nauwe samenwerking met Claude"
```

---

### Task 2: New generation prompt + validator registration

**Files:**
- Modify: `src/notebook_artifacts.py:184-197` (the `"infographic"` entry in `_KIND_INSTRUCTIONS`), `:30` (import), `:284` (`_KIND_VALIDATORS`)
- Modify: `tests/test_notebook_infographic.py:51`
- Test: `tests/test_notebook_infographic_v2.py` (append)

**Interfaces:**
- Consumes: `extract_infographic` (Task 1).
- Produces: `ARTIFACT_KINDS["infographic"]["prompt"]` asking for the JSON schema; `_KIND_VALIDATORS["infographic"] is extract_infographic`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notebook_infographic_v2.py`:

```python
# ---- prompt + validator wiring -------------------------------------------

def test_prompt_asks_for_json_schema_and_dutch():
    from src.notebook_artifacts import ARTIFACT_KINDS, _KIND_VALIDATORS
    from src.notebook_language import DUTCH_OUTPUT_RULE
    prompt = ARTIFACT_KINDS["infographic"]["prompt"]
    assert DUTCH_OUTPUT_RULE in prompt
    assert '"blocks"' in prompt
    for t in ("column", "steps", "icon_card", "hero", "comparison", "key_numbers"):
        assert f'"{t}"' in prompt
    assert "illustration_prompt" in prompt
    assert "Engels" in prompt                 # illustration prompts in English
    assert "## Key numbers" not in prompt     # legacy markdown structure is gone
    assert _KIND_VALIDATORS["infographic"] is extract_infographic
```

Change `tests/test_notebook_infographic.py:51` from `assert "Key numbers" in ARTIFACT_KINDS["infographic"]["prompt"]` to `assert '"blocks"' in ARTIFACT_KINDS["infographic"]["prompt"]`.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_notebook_infographic_v2.py -k prompt -q`
Expected: FAIL (`'"blocks"' in prompt` false).

- [ ] **Step 3: Replace the prompt and validator**

In `src/notebook_artifacts.py` change the import on line 30 to `from src.notebook_infographic import extract_infographic` and set `"infographic": extract_infographic,` in `_KIND_VALIDATORS` (keep the comment above it). Replace the `"infographic"` entry in `_KIND_INSTRUCTIONS` with:

```python
    "infographic": """Maak een infographic: één landscape-compositie met thematische kolommen, genummerde stappen, icoon-kaarten, een centraal hero-element, een vergelijkingsblok en kerncijfers. De tekst blijft in HTML; illustraties worden apart gegenereerd op basis van jouw prompts.

Lever exact één codefence met taalaanduiding "json" en daarin één JSON-object, niets anders. Schema:

{
  "title": "titel in het Nederlands (max 80 tekens)",
  "subtitle": "ondertitel (optioneel, max 120 tekens)",
  "takeaway": "één zin met de kernboodschap",
  "blocks": [
    {"id": "slug", "type": "column", "heading": "kop", "subheading": "korte toelichting",
     "illustration_prompt": "optioneel", "children": [ ...2 tot 3 sub-blokken van type steps, icon_card of key_numbers... ]},
    {"id": "slug", "type": "steps", "heading": "kop", "items": [{"label": "kort", "text": "max 120 tekens"}]},
    {"id": "slug", "type": "icon_card", "heading": "kop", "icon": "sleutel", "text": "1 tot 2 zinnen, max 200 tekens"},
    {"id": "slug", "type": "hero", "heading": "kop", "illustration_prompt": "...", "text": "max 240 tekens"},
    {"id": "slug", "type": "comparison", "heading": "kop", "rows": [{"label": "kort", "value": "letterlijke bronwaarde", "ratio": 0.6}]},
    {"id": "slug", "type": "key_numbers", "heading": "kop", "items": [{"number": "42%", "label": "max 8 woorden"}]}
  ]
}

Regels voor de structuur:
- 5 tot 8 blokken op het hoogste niveau; precies één "hero"; één of twee "column"-blokken (elk met 2 tot 3 children; children mogen zelf geen column of hero zijn).
- "steps" heeft 2 tot 5 stappen; "comparison" 2 tot 4 rijen; "key_numbers" 3 tot 5 items.
- "id" is een unieke slug (a-z, 0-9, - of _); "heading" maximaal 60 tekens.
- "icon" is optioneel en één van: sources, audio, video, chat, graph, bars, target, warning, gear, people, doc, search, spark. Laat weg als geen sleutel past.
- "illustration_prompt" is optioneel, in het Engels, maximaal 200 tekens: beschrijf een eenvoudige scène of metafoor zonder merknamen of personen en vraag nooit om tekst in beeld (geen text, label, caption, words, letters). Geef er maximaal 5, in elk geval bij de hero.

Regels voor de inhoud:
- Elk cijfer, elke stap en elke vergelijkingswaarde moet herleidbaar zijn tot de bronnen; verzin niets.
- "ratio" alleen als de bronnen een vergelijkbare grootheid geven; anders het comparison-blok weglaten. "value" is de letterlijke bronwaarde (bijvoorbeeld "300 bronnen").
- Zijn er geen cijfers in de bronnen, gebruik dan een telwoord of kort feit als "number" (bijvoorbeeld "3 panelen").
- Alle tekstvelden in het Nederlands, behalve "illustration_prompt". Geen markdown of HTML binnen de JSON-strings.""",
```

Also update the module comment at `src/notebook_infographic.py:1-30` (module docstring) with one paragraph: "v2 (2026-09-03): new artifacts store the JSON model from `extract_infographic`; `generate_infographic` dispatches on `is_infographic_v2`. The markdown path below stays for previously generated artifacts."

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_notebook_infographic_v2.py tests/test_notebook_infographic.py tests/test_services_notebook_artifacts.py -q`
Expected: pass. (`validate_infographic_markdown` still exists and its own tests still pass; it is simply no longer registered.)

- [ ] **Step 5: Commit**

```bash
git add src/notebook_artifacts.py src/notebook_infographic.py tests/test_notebook_infographic_v2.py tests/test_notebook_infographic.py
git commit -m "feat(notebooks): infographic prompt emits v2 JSON, validated by extract_infographic

Ed de Feber, in nauwe samenwerking met Claude"
```

---

### Task 3: v2 renderer + dispatch in `generate_infographic`

**Files:**
- Modify: `src/notebook_infographic.py` (append `_TEMPLATE_V2`, `_VIEWER_SCRIPT`, block renderers, `render_infographic_v2`; change `generate_infographic` signature)
- Test: `tests/test_notebook_infographic_v2.py` (append)

**Interfaces:**
- Consumes: `extract_infographic`, `iter_blocks`, `is_infographic_v2`, `MAX_ILLUSTRATIONS` (Task 1); existing `_PALETTE`, `_ICONS`, `_ICON_ATTRS`, `_pick_icon`, `_esc_bold`, `_parse_infographic_markdown`, `_render_grid_html`, `_TEMPLATE`.
- Produces:
  - `render_infographic_v2(data: dict, notebook_name: str, generated_at: datetime, *, illustrations_url_base: str, poll_url: Optional[str]) -> str`
  - `generate_infographic(title, markdown, notebook_name, generated_at, *, illustrations_url_base="/api/notebook-illustration/", poll_url=None) -> str` — keyword-only additions, so every existing caller keeps working.
  - Page contract used by the viewer script and Task 6: root `<div class="ig2-wrap" data-illustrations="pending" data-poll-url="…">` only when `poll_url` is given; every block has `<div class="ig2-art" data-block-id="<id>">`; with an illustration the slot contains `<img class="ig2-img" src="<base><filename>" loading="lazy" alt="">`, otherwise an icon circle.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notebook_infographic_v2.py`:

```python
# ---- renderer v2 ----------------------------------------------------------

from src.notebook_infographic import generate_infographic, render_infographic_v2  # noqa: E402

_AT = datetime(2026, 9, 3)


def test_generate_dispatches_legacy_markdown_to_old_renderer():
    md = "# Oud\n\n## Key numbers\n- **3** — panelen\n\n## Sectie\n- feit\n\n> takeaway\n"
    out = generate_infographic(title=None, markdown=md, notebook_name="NB", generated_at=_AT)
    assert 'class="ig-grid"' in out
    assert "ig2-wrap" not in out


def test_generate_dispatches_v2_json_to_new_renderer():
    out = generate_infographic(title="x", markdown=_fenced(_valid_data()), notebook_name="NB", generated_at=_AT)
    assert 'class="ig2-wrap"' in out
    assert "SamenWijzer in cijfers" in out
    assert "Wat de bronnen zeggen" in out
    assert "Begeleiding op maat werkt" in out            # takeaway
    assert "NB" in out
    assert "ig-grid\"" not in out


def test_generate_v2_invalid_json_falls_back_to_legacy_fallback_card():
    # Stored raw content after 3 failed validation attempts never reaches
    # the DB (generate_artifact raises), but a hand-edited/corrupt row must
    # still render something rather than 500.
    out = generate_infographic(title="Kapot", markdown="{\"title\": 1}", notebook_name="NB", generated_at=_AT)
    assert "<html" in out
    assert "kon niet als infographic worden gerenderd" in out


def test_render_v2_contains_all_block_types_and_grid():
    data = extract_infographic(json.dumps(_valid_data()))
    out = render_infographic_v2(data, "NB", _AT, illustrations_url_base="/api/notebook-illustration/", poll_url=None)
    for cls in ("ig2-grid", "ig2-column", "ig2-steps", "ig2-card", "ig2-hero", "ig2-cmp-row", "ig2-stats", "ig-takeaway"):
        assert cls in out, cls
    assert 'data-block-id="hero"' in out
    assert 'style="width:60%"' in out          # comparison ratio 0.6
    assert 'style="width:100%"' in out
    assert "42%" in out and "geslaagd" in out  # key numbers
    assert out.count('class="ig2-step-n"') == 2   # two numbered steps (CSS rule excluded)
    assert "@media (max-width: 959px)" in out  # mobile breakpoint per spec (< 960)
    assert "min-width: 0" in out


def test_render_v2_without_illustrations_shows_icons_not_img():
    data = extract_infographic(json.dumps(_valid_data()))
    out = render_infographic_v2(data, "NB", _AT, illustrations_url_base="/api/notebook-illustration/", poll_url=None)
    assert "<img" not in out
    assert out.count('class="ig2-icon"') == 9  # one per block incl. column children
    assert "data-illustrations" not in out
    assert "<script src" not in out


def test_render_v2_with_illustrations_renders_img_with_lazy_loading():
    raw = _valid_data()
    raw["illustrations"] = {"hero": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa-hero-0123abcd.png"}
    data = extract_infographic(json.dumps(raw))
    out = render_infographic_v2(data, "NB", _AT, illustrations_url_base="/api/notebook-illustration/", poll_url=None)
    assert ('<img class="ig2-img" src="/api/notebook-illustration/'
            'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa-hero-0123abcd.png" loading="lazy" alt="">') in out
    assert out.count('class="ig2-icon"') == 8


def test_render_v2_pending_embeds_poll_url_and_inline_script():
    data = extract_infographic(json.dumps(_valid_data()))
    out = render_infographic_v2(data, "NB", _AT, illustrations_url_base="/api/notebook-illustration/",
                                poll_url="/api/notebooks/nb1/artifacts/a1/illustrations")
    assert 'data-illustrations="pending"' in out
    assert 'data-poll-url="/api/notebooks/nb1/artifacts/a1/illustrations"' in out
    assert "<script>" in out and "<script src" not in out
    assert "3000" in out and "120000" in out   # 3 s interval, 120 s cap


def test_render_v2_escapes_html_in_text():
    raw = _valid_data()
    raw["title"] = "<b>x</b>"
    raw["blocks"][1]["text"] = "a <script>alert(1)</script> b"
    data = extract_infographic(json.dumps(raw))
    out = render_infographic_v2(data, "NB", _AT, illustrations_url_base="/x/", poll_url="/p\"><x")
    assert "<b>x</b>" not in out and "&lt;b&gt;x&lt;/b&gt;" in out
    assert "<script>alert" not in out
    assert 'data-poll-url="/p&quot;&gt;&lt;x"' in out


def test_render_v2_forces_light_theme():
    data = extract_infographic(json.dumps(_valid_data()))
    out = render_infographic_v2(data, "NB", _AT, illustrations_url_base="/x/", poll_url=None)
    assert "prefers-color-scheme" not in out
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_notebook_infographic_v2.py -k "render or dispatch" -q`
Expected: ImportError on `render_infographic_v2`.

- [ ] **Step 3: Implement the renderer**

Append to `src/notebook_infographic.py`. The CSS/HTML template goes through `str.format`, so every literal brace is doubled; the viewer script is a plain constant substituted as `{script}` so its braces are **not** doubled.

```python
# ---------------------------------------------------------------------------
# v2 rendering (spec Deel C)
# ---------------------------------------------------------------------------

_TEMPLATE_V2 = """\
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --font-display: 'Charter', 'Iowan Old Style', Georgia, serif;
  --font-body: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --bg: #fbf9f4; --bg-surface: #ffffff; --border: rgba(0,0,0,0.08);
  --text: #1a1817; --text-dim: #5a5651; --text-muted: #8a8580;
  --accent: #b8543a; --gold: #c9952e; --gold-bg: rgba(201,149,46,0.09);
  --radius: 12px; --shadow-sm: 0 1px 3px rgba(0,0,0,0.05);
}}
/* Always-light, like every notebook viewer (#98). */
body {{ font-family: var(--font-body); background: var(--bg); color: var(--text); line-height: 1.5; font-size: 15px; -webkit-font-smoothing: antialiased; }}
.ig2-wrap {{ max-width: 1280px; margin: 0 auto; padding: 2rem 1.5rem 2.5rem; }}
.ig2-head {{ text-align: center; padding: 0 0 1.6rem; }}
.ig2-head-label {{ text-transform: uppercase; letter-spacing: 0.28em; font-size: 0.66rem; font-weight: 600; color: var(--accent); margin-bottom: 0.7rem; }}
.ig2-head h1 {{ font-family: var(--font-display); font-size: clamp(1.6rem, 3.2vw, 2.4rem); font-weight: 700; line-height: 1.12; letter-spacing: -0.02em; max-width: 60ch; margin: 0 auto; }}
.ig2-head p {{ color: var(--text-dim); margin-top: 0.5rem; font-size: 0.98rem; }}

/* Desktop: [column] [hero + rest] [column]. Wrappers are flex columns so
   `order` (document index) keeps document order inside each wrapper. */
.ig2-grid {{ display: grid; grid-template-columns: 1fr 1.25fr 1fr; gap: 1.5rem; align-items: start; }}
.ig2-grid.ig2-grid--one {{ grid-template-columns: 1.25fr 1fr; }}
.ig2-col, .ig2-center {{ display: flex; flex-direction: column; gap: 1.25rem; min-width: 0; }}
.ig2-block {{ order: var(--o, 0); min-width: 0; background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow-sm); padding: 1.1rem 1.15rem 1.2rem; }}
.ig2-block h2 {{ font-size: 0.86rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 0.45rem; }}
.ig2-block p {{ color: var(--text-dim); font-size: 0.88rem; }}
.ig2-art {{ display: flex; justify-content: center; margin-bottom: 0.75rem; }}
.ig2-icon {{ width: 58px; height: 58px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; background: var(--pc-tint); color: var(--pc); }}
.ig2-icon svg {{ width: 28px; height: 28px; }}
.ig2-img {{ width: 100%; max-width: 320px; height: auto; border-radius: 10px; display: block; opacity: 1; transition: opacity 0.4s ease; }}
.ig2-img--fade {{ opacity: 0; }}

.ig2-column {{ background: transparent; border: 0; box-shadow: none; padding: 0; }}
.ig2-column-head {{ padding: 0 0.25rem 0.75rem; text-align: center; }}
.ig2-column-head h2 {{ color: var(--pc); }}
.ig2-column-head p {{ font-size: 0.82rem; }}
.ig2-column-body {{ display: flex; flex-direction: column; gap: 1rem; }}

.ig2-steps {{ list-style: none; counter-reset: none; position: relative; margin-top: 0.3rem; }}
.ig2-steps li {{ display: grid; grid-template-columns: 28px 1fr; gap: 0.6rem; position: relative; padding-bottom: 0.9rem; }}
.ig2-steps li::before {{ content: ""; position: absolute; left: 13px; top: 28px; bottom: 0; width: 2px; background: var(--pc-tint); }}
.ig2-steps li:last-child::before {{ display: none; }}
.ig2-steps li:last-child {{ padding-bottom: 0; }}
.ig2-step-n {{ width: 28px; height: 28px; border-radius: 50%; background: var(--pc); color: #fff; font-size: 0.78rem; font-weight: 700; display: inline-flex; align-items: center; justify-content: center; }}
.ig2-steps strong {{ display: block; font-size: 0.86rem; }}

.ig2-hero {{ text-align: center; padding: 1.4rem 1.4rem 1.5rem; }}
.ig2-hero .ig2-img {{ max-width: 100%; }}
.ig2-hero .ig2-icon {{ width: 96px; height: 96px; }}
.ig2-hero .ig2-icon svg {{ width: 44px; height: 44px; }}
.ig2-hero h2 {{ font-family: var(--font-display); font-size: 1.3rem; text-transform: none; letter-spacing: -0.01em; }}
.ig2-hero p {{ font-size: 0.98rem; color: var(--text); }}

.ig2-cmp-row {{ display: grid; grid-template-columns: minmax(64px, auto) 1fr auto; gap: 0.5rem 0.8rem; align-items: center; margin-bottom: 0.6rem; }}
.ig2-cmp-label {{ font-size: 0.7rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; text-align: right; }}
.ig2-cmp-track {{ height: 16px; border-radius: 8px; background: var(--bg); border: 1px solid var(--border); overflow: hidden; }}
.ig2-cmp-fill {{ height: 100%; border-radius: 8px; background: var(--pc); }}
.ig2-cmp-value {{ font-size: 0.78rem; color: var(--text-dim); white-space: nowrap; }}

.ig2-stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(96px, 1fr)); gap: 0.6rem; margin-top: 0.4rem; }}
.ig2-stat {{ text-align: center; padding: 0.6rem 0.4rem; border-radius: 10px; background: var(--pc-tint); }}
.ig2-stat-n {{ font-family: var(--font-display); font-size: 1.35rem; font-weight: 700; color: var(--pc); line-height: 1.1; }}
.ig2-stat-l {{ font-size: 0.72rem; color: var(--text-dim); margin-top: 0.2rem; }}

.ig-takeaway {{ margin-top: 1.6rem; padding: 0.9rem 1.3rem; border-left: 3px solid var(--gold); background: var(--gold-bg); border-radius: 0 var(--radius) var(--radius) 0; font-family: var(--font-display); font-style: italic; font-size: 0.98rem; text-align: center; }}
.ig2-meta {{ text-align: center; font-size: 0.75rem; color: var(--text-muted); margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--border); }}

/* Mobile: one column in document order. Wrappers dissolve (display:
   contents) so `order` sorts every block globally. */
@media (max-width: 959px) {{
  .ig2-grid, .ig2-grid.ig2-grid--one {{ display: flex; flex-direction: column; gap: 1rem; }}
  .ig2-col, .ig2-center {{ display: contents; }}
  .ig2-wrap {{ padding: 1.25rem 0.9rem 2rem; }}
  .ig2-cmp-row {{ grid-template-columns: 1fr auto; }}
  .ig2-cmp-label {{ grid-column: 1 / -1; text-align: left; }}
}}
@media print {{ body {{ background: #fff !important; }} .ig2-block {{ box-shadow: none; }} }}
</style>
</head>
<body>
<div class="ig2-wrap"{pending_attrs}>
  <div class="ig2-head">
    <div class="ig2-head-label">Ithaka &mdash; Infographic</div>
    <h1>{title}</h1>{subtitle_html}
  </div>
  {grid_html}
  <div class="ig-takeaway">{takeaway}</div>
  <div class="ig2-meta">{notebook_name} &middot; {date}</div>
</div>
{script}
</body>
</html>
"""

# Not run through str.format — braces are literal. Polls the status endpoint
# every 3 s (max 120 s) and swaps each icon for its <img> with a short fade.
_VIEWER_SCRIPT = """<script>
(function () {
  var root = document.querySelector('.ig2-wrap[data-illustrations="pending"]');
  if (!root) return;
  var url = root.getAttribute('data-poll-url');
  if (!url) return;
  var INTERVAL = 3000, MAX_MS = 120000, started = Date.now();
  function stop() { root.removeAttribute('data-illustrations'); }
  function apply(map) {
    Object.keys(map || {}).forEach(function (id) {
      var slot = root.querySelector('.ig2-art[data-block-id="' + CSS.escape(id) + '"]');
      if (!slot || slot.getAttribute('data-done') === '1') return;
      slot.setAttribute('data-done', '1');
      var img = new Image();
      img.className = 'ig2-img ig2-img--fade';
      img.alt = '';
      img.onload = function () {
        slot.innerHTML = '';
        slot.appendChild(img);
        requestAnimationFrame(function () { img.classList.remove('ig2-img--fade'); });
      };
      img.src = map[id];
    });
  }
  function tick() {
    fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && d.illustrations) apply(d.illustrations);
        if (!d || d.status !== 'running' || Date.now() - started > MAX_MS) { stop(); return; }
        setTimeout(tick, INTERVAL);
      })
      .catch(function () {
        if (Date.now() - started > MAX_MS) { stop(); return; }
        setTimeout(tick, INTERVAL);
      });
  }
  setTimeout(tick, INTERVAL);
})();
</script>"""


def _icon_body(block: dict) -> str:
    key = block.get("icon")
    return _ICONS[key] if key in _ICONS else _pick_icon(block.get("heading", ""))


def _art_html(block: dict, illustrations: dict, url_base: str) -> str:
    """Illustration slot: <img> when the job delivered one, else icon circle."""
    filename = illustrations.get(block["id"])
    if filename:
        src = html.escape(url_base + filename, quote=True)
        inner = f'<img class="ig2-img" src="{src}" loading="lazy" alt="">'
    else:
        inner = f'<span class="ig2-icon"><svg {_ICON_ATTRS}>{_icon_body(block)}</svg></span>'
    return f'<div class="ig2-art" data-block-id="{html.escape(block["id"], quote=True)}">{inner}</div>'


def _block_body_html(block: dict) -> str:
    t = block["type"]
    if t == "steps":
        items = "".join(
            f'<li><span class="ig2-step-n">{i}</span><div><strong>{html.escape(it["label"])}</strong>'
            f'<p>{html.escape(it["text"])}</p></div></li>'
            for i, it in enumerate(block["items"], 1)
        )
        return f'<ol class="ig2-steps">{items}</ol>'
    if t in ("icon_card", "hero"):
        return f"<p>{html.escape(block['text'])}</p>"
    if t == "comparison":
        return "".join(
            '<div class="ig2-cmp-row">'
            f'<div class="ig2-cmp-label">{html.escape(r["label"])}</div>'
            f'<div class="ig2-cmp-track"><div class="ig2-cmp-fill" style="width:{round(r["ratio"] * 100)}%"></div></div>'
            f'<div class="ig2-cmp-value">{html.escape(r["value"])}</div>'
            "</div>"
            for r in block["rows"]
        )
    if t == "key_numbers":
        return '<div class="ig2-stats">' + "".join(
            f'<div class="ig2-stat"><div class="ig2-stat-n">{html.escape(it["number"])}</div>'
            f'<div class="ig2-stat-l">{html.escape(it["label"])}</div></div>'
            for it in block["items"]
        ) + "</div>"
    return ""


def _block_html(block: dict, order: int, palette_index: int, illustrations: dict, url_base: str) -> str:
    color, tint = _PALETTE[palette_index % len(_PALETTE)]
    style = f"--o:{order};--pc:{color};--pc-tint:{tint}"
    t = block["type"]
    if t == "column":
        children = "".join(
            _block_html(c, order, palette_index + 1 + i, illustrations, url_base)
            for i, c in enumerate(block["children"])
        )
        return (
            f'<section class="ig2-block ig2-column" style="{style}">'
            f'<div class="ig2-column-head">{_art_html(block, illustrations, url_base)}'
            f'<h2>{html.escape(block["heading"])}</h2><p>{html.escape(block["subheading"])}</p></div>'
            f'<div class="ig2-column-body">{children}</div></section>'
        )
    cls = {"hero": "ig2-hero", "icon_card": "ig2-card", "steps": "ig2-card ig2-card--steps",
           "comparison": "ig2-card ig2-card--cmp", "key_numbers": "ig2-card ig2-card--stats"}[t]
    return (
        f'<section class="ig2-block {cls}" style="{style}">'
        f'{_art_html(block, illustrations, url_base)}<h2>{html.escape(block["heading"])}</h2>'
        f'{_block_body_html(block)}</section>'
    )


def render_infographic_v2(data: dict, notebook_name: str, generated_at: datetime, *,
                          illustrations_url_base: str, poll_url: Optional[str]) -> str:
    """Render a validated v2 dict (from extract_infographic) as the poster page."""
    illustrations = data.get("illustrations") or {}
    blocks = data["blocks"]
    columns = [b for b in blocks if b["type"] == "column"]
    hero = next(b for b in blocks if b["type"] == "hero")
    rest = [b for b in blocks if b["type"] not in ("column", "hero")]

    order = {b["id"]: i for i, b in enumerate(blocks)}
    palette = {b["id"]: i * 3 for i, b in enumerate(blocks)}

    def _render(b):
        return _block_html(b, order[b["id"]], palette[b["id"]], illustrations, illustrations_url_base)

    left = _render(columns[0])
    right = _render(columns[1]) if len(columns) > 1 else ""
    center = _render(hero) + "".join(_render(b) for b in rest)
    grid_cls = "ig2-grid" if right else "ig2-grid ig2-grid--one"
    grid_html = (
        f'<div class="{grid_cls}"><div class="ig2-col">{left}</div>'
        f'<div class="ig2-center">{center}</div>'
        + (f'<div class="ig2-col">{right}</div>' if right else "")
        + "</div>"
    )

    pending_attrs = ""
    script = ""
    if poll_url:
        pending_attrs = f' data-illustrations="pending" data-poll-url="{html.escape(poll_url, quote=True)}"'
        script = _VIEWER_SCRIPT
    subtitle_html = f"<p>{html.escape(data['subtitle'])}</p>" if data.get("subtitle") else ""
    return _TEMPLATE_V2.format(
        title=html.escape(data["title"]),
        subtitle_html=subtitle_html,
        grid_html=grid_html,
        takeaway=html.escape(data["takeaway"]),
        notebook_name=html.escape(notebook_name or ""),
        date=html.escape(generated_at.strftime("%d-%m-%Y")),
        pending_attrs=pending_attrs,
        script=script,
    )
```

Then change `generate_infographic` to:

```python
def generate_infographic(
    title: Optional[str],
    markdown: str,
    notebook_name: str,
    generated_at: datetime,
    *,
    illustrations_url_base: str = "/api/notebook-illustration/",
    poll_url: Optional[str] = None,
) -> str:
    """Render a stored infographic artifact as a self-contained HTML page.

    v2 content (JSON, see is_infographic_v2) goes to render_infographic_v2;
    `poll_url` (set by the route while an illustration job runs) makes the
    page poll for illustrations. Legacy markdown keeps the poster renderer
    below. Corrupt v2 JSON degrades to the legacy fallback card with a
    Dutch notice instead of raising.
    """
    if is_infographic_v2(markdown):
        try:
            data = extract_infographic(markdown)
        except ValueError as exc:
            fallback_md = (
                f"# {(title or 'Infographic').strip()}\n\n"
                f"Deze inhoud kon niet als infographic worden gerenderd ({exc}).\n"
            )
            return _render_legacy(title, fallback_md, notebook_name, generated_at)
        return render_infographic_v2(
            data, notebook_name, generated_at,
            illustrations_url_base=illustrations_url_base, poll_url=poll_url,
        )
    return _render_legacy(title, markdown, notebook_name, generated_at)


def _render_legacy(title, markdown, notebook_name, generated_at) -> str:
    parsed = _parse_infographic_markdown(markdown)
    effective_title = parsed["title"] or (title or "").strip() or "Infographic"
    return _TEMPLATE.format(
        title=html.escape(effective_title),
        grid_html=_render_grid_html(parsed),
        notebook_name=html.escape(notebook_name or ""),
        date=html.escape(generated_at.strftime("%B %d, %Y")),
    )
```

(The old body of `generate_infographic` moves into `_render_legacy` unchanged; the old docstring's title-precedence paragraph moves with it.)

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_notebook_infographic_v2.py tests/test_notebook_infographic.py -q`
Expected: all pass. If `test_render_v2_contains_all_block_types_and_grid` fails on the icon count, check `_art_html` is emitted once per block including column heads (9 blocks in the fixture).

- [ ] **Step 5: Visual check**

Run a throwaway render and open it in the browser (desktop + 360 px):

```bash
.venv/bin/python - <<'EOF'
import json, sys
sys.path.insert(0, "tests")
from datetime import datetime
from test_notebook_infographic_v2 import _valid_data
from src.notebook_infographic import generate_infographic
open("$SCRATCH/ig2-preview.html", "w").write(
    generate_infographic("t", json.dumps(_valid_data()), "Preview", datetime.now()))
EOF
```

Open `file://$SCRATCH/ig2-preview.html` (`$SCRATCH` = the session scratchpad directory from your system prompt) via the chrome-devtools MCP (`new_page`, `emulate` 360 px), take screenshots, confirm: 3-column grid on desktop, single column in document order on mobile, no horizontal scroll (`document.documentElement.scrollWidth <= innerWidth`). Tweak CSS only if something is visibly broken.

- [ ] **Step 6: Commit**

```bash
git add src/notebook_infographic.py tests/test_notebook_infographic_v2.py
git commit -m "feat(notebooks): infographic v2 renderer with illustration slots + live viewer poll

Ed de Feber, in nauwe samenwerking met Claude"
```

---

### Task 4: Illustration job module (`src/notebook_illustrations.py`)

**Files:**
- Modify: `src/constants.py:53` (add `NOTEBOOK_INFOGRAPHICS_DIR` + guarded makedirs next to the `NOTEBOOK_VIDEO_DIR` block)
- Create: `src/notebook_illustrations.py`
- Test: `tests/test_notebook_illustrations.py` (create)

**Interfaces:**
- Consumes: `extract_infographic`, `iter_blocks`, `MAX_ILLUSTRATIONS` (Task 1); `do_generate_image(content, owner=…) -> dict` from `src/ai_interaction.py` (returns `{"image_url": "/api/generated-image/<hex>.png"}` or `{"error": …}`); `NOTEBOOK_INFOGRAPHICS_DIR`, `GENERATED_IMAGES_DIR` from `src/constants.py`; `Document`, `Notebook`, `NotebookArtifact`, `SessionLocal` from `core/database.py`.
- Produces (used by Tasks 5–6):
  - `JOB_TIMEOUT_SECONDS = 300`, `ILLUSTRATION_FILE_RE`, `ILLUSTRATION_HEADERS`
  - `build_illustration_prompt(prompt: str, *, hero: bool) -> str` → `"<prompt>, flat vector illustration, pastel palette, soft shapes, white background, no text, no letters\n\n<size>\nlow"`
  - `select_illustration_blocks(data: dict) -> list[dict]` (first ≤ 5 blocks with `illustration_prompt`, document order)
  - `load_illustrations(content: str) -> dict[str, str]` (empty dict on non-v2/invalid content)
  - `start_illustration_job(notebook_id, artifact_id, owner, db_session_factory=None) -> str` (raises `ValueError("Artifact niet gevonden")` / `ValueError("Er loopt al een illustratie-job voor dit artifact")`)
  - `get_artifact_job(artifact_id, owner) -> Optional[dict]` with keys `status` (`running`/`done`/`error`/`cancelled`), `illustrations` (partial map), `errors` (int)
  - `_generate_image(content, owner) -> dict` — thin seam over `do_generate_image`, monkeypatched in tests
  - `_active_jobs: dict` (tests clear it)

- [ ] **Step 1: Add the constant**

In `src/constants.py`, after line 53 (`NOTEBOOK_COVERS_DIR = …`) add:

```python
# Infographic v2 illustrations (src/notebook_illustrations.py): one PNG per
# block, named "<artifact_id>-<block_id>-<hex8>.png".
NOTEBOOK_INFOGRAPHICS_DIR = os.path.join(DATA_DIR, "notebook_infographics")
```

and after the `NOTEBOOK_VIDEO_DIR` makedirs block:

```python
try:
    os.makedirs(NOTEBOOK_INFOGRAPHICS_DIR, exist_ok=True)
except OSError:
    pass
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_notebook_illustrations.py`:

```python
"""Infographic v2 illustration job (src/notebook_illustrations.py).

Hermetic: `_generate_image` is a fake that drops a PNG into a tmp
GENERATED_IMAGES_DIR, both dirs are monkeypatched to tmp_path, and the DB is
a file-backed temp sqlite (tests.helpers.sqlite_db) — same posture as
tests/test_notebook_video.py.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-illustrations")

import asyncio
import json
import os as _os
import time
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

import core.database as cdb
import src.notebook_illustrations as ill
from src.notebook_infographic import extract_infographic
from tests.helpers.sqlite_db import make_temp_sqlite

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)

_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _data(n_prompts=3):
    """Valid v2 JSON with `n_prompts` illustration prompts (hero always first)."""
    blocks = [
        {"id": "hero", "type": "hero", "heading": "Hero", "text": "t", "illustration_prompt": "a soft hub"},
        {"id": "col", "type": "column", "heading": "Col", "subheading": "s", "children": [
            {"id": "c1", "type": "icon_card", "heading": "C1", "text": "t", "illustration_prompt": "a leaf"},
            {"id": "c2", "type": "icon_card", "heading": "C2", "text": "t", "illustration_prompt": "a stone"},
        ]},
        {"id": "k1", "type": "icon_card", "heading": "K1", "text": "t", "illustration_prompt": "a cloud"},
        {"id": "k2", "type": "icon_card", "heading": "K2", "text": "t", "illustration_prompt": "a river"},
        {"id": "k3", "type": "icon_card", "heading": "K3", "text": "t", "illustration_prompt": "a hill"},
        {"id": "k4", "type": "icon_card", "heading": "K4", "text": "t", "illustration_prompt": "a tree"},
    ]
    count = 0
    for b in blocks:
        for x in [b] + b.get("children", []):
            if "illustration_prompt" in x:
                if count >= n_prompts:
                    x.pop("illustration_prompt")
                count += 1
    return {"title": "T", "takeaway": "one sentence", "blocks": blocks}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    ill._active_jobs.clear()
    gen = tmp_path / "generated"
    out = tmp_path / "infographics"
    gen.mkdir()
    out.mkdir()
    monkeypatch.setattr(ill, "GENERATED_IMAGES_DIR", str(gen))
    monkeypatch.setattr(ill, "NOTEBOOK_INFOGRAPHICS_DIR", str(out))
    yield
    ill._active_jobs.clear()


def _make_rows(data, owner="own"):
    s = _TS()
    try:
        nb = cdb.Notebook(id=str(uuid.uuid4()), name="NB", owner=owner)
        s.add(nb)
        doc = cdb.Document(id=str(uuid.uuid4()), title="Doc", owner=owner,
                           current_content="```json\n" + json.dumps(data) + "\n```")
        s.add(doc)
        s.commit()
        art = cdb.NotebookArtifact(id=str(uuid.uuid4()), notebook_id=nb.id,
                                   document_id=doc.id, kind="infographic", title="T")
        s.add(art)
        s.commit()
        return nb.id, art.id, doc.id
    finally:
        s.close()


def _fake_image_gen(fail_for=(), fail_all=False):
    """Returns (fake, calls). Writes a PNG into GENERATED_IMAGES_DIR per call."""
    calls = []

    async def fake(content, owner):
        calls.append((content, owner))
        prompt_line = content.split("\n", 1)[0]
        if fail_all or any(f in prompt_line for f in fail_for):
            return {"error": "boom"}
        name = uuid.uuid4().hex + ".png"
        Path(ill.GENERATED_IMAGES_DIR, name).write_bytes(b"\x89PNG-fake")
        return {"image_url": f"/api/generated-image/{name}"}
    return fake, calls


def _content(doc_id):
    s = _TS()
    try:
        return s.query(cdb.Document).get(doc_id).current_content
    finally:
        s.close()


# ---- pure helpers ---------------------------------------------------------

def test_build_illustration_prompt_adds_style_suffix_size_and_low_quality():
    p = ill.build_illustration_prompt("a leaf", hero=False)
    assert p.startswith("a leaf, flat vector illustration, pastel palette, soft shapes, white background, no text, no letters")
    assert p.endswith("\n\n1024x1024\nlow")
    assert ill.build_illustration_prompt("hub", hero=True).endswith("\n\n1536x1024\nlow")


def test_select_illustration_blocks_caps_at_five_in_document_order():
    data = extract_infographic(json.dumps(_data(n_prompts=7)))
    picked = ill.select_illustration_blocks(data)
    assert [b["id"] for b in picked] == ["hero", "c1", "c2", "k1", "k2"]


def test_load_illustrations_reads_map_and_tolerates_bad_content():
    d = _data()
    d["illustrations"] = {"hero": "x.png"}
    assert ill.load_illustrations(json.dumps(d)) == {"hero": "x.png"}
    assert ill.load_illustrations("# markdown") == {}
    assert ill.load_illustrations("{bad") == {}


@pytest.mark.parametrize("name, ok", [
    (f"{_UUID}-hero-0123abcd.png", True),
    (f"{_UUID}-kaart_a-0123abcd.png", True),
    (f"{_UUID}-hero-0123abcd.jpg", False),
    ("../../etc/passwd", False),
    (f"{_UUID}-Hero-0123abcd.png", False),
    (f"{_UUID}-hero-0123abc.png", False),
    ("nouuid-hero-0123abcd.png", False),
])
def test_filename_whitelist(name, ok):
    assert bool(ill.ILLUSTRATION_FILE_RE.fullmatch(name)) is ok


def test_artifact_id_from_filename():
    assert ill.artifact_id_from_filename(f"{_UUID}-hero-0123abcd.png") == _UUID
    with pytest.raises(ValueError):
        ill.artifact_id_from_filename("bad.png")


# ---- job ---------------------------------------------------------------------

async def test_job_generates_all_and_persists_map_incrementally(monkeypatch):
    fake, calls = _fake_image_gen()
    monkeypatch.setattr(ill, "_generate_image", fake)
    nb_id, art_id, doc_id = _make_rows(_data(3))

    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    await ill._active_jobs[job_id]["task"]

    job = ill.get_artifact_job(art_id, "own")
    assert job["status"] == "done"
    assert job["errors"] == 0
    assert set(job["illustrations"]) == {"hero", "c1", "c2"}
    stored = ill.load_illustrations(_content(doc_id))
    assert stored == job["illustrations"]
    for fn in stored.values():
        assert ill.ILLUSTRATION_FILE_RE.fullmatch(fn)
        assert fn.startswith(art_id + "-")
        assert Path(ill.NOTEBOOK_INFOGRAPHICS_DIR, fn).exists()
    # hero uses the wide size, others square; quality always low
    assert calls[0][0].endswith("\n\n1536x1024\nlow")
    assert calls[1][0].endswith("\n\n1024x1024\nlow")
    assert all(c[1] == "own" for c in calls)
    # Stored content is re-serialised as bare JSON and still validates.
    extract_infographic(_content(doc_id))


async def test_job_skips_failed_block_and_keeps_the_rest(monkeypatch):
    fake, _ = _fake_image_gen(fail_for=("a leaf",))
    monkeypatch.setattr(ill, "_generate_image", fake)
    nb_id, art_id, doc_id = _make_rows(_data(3))
    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    await ill._active_jobs[job_id]["task"]
    job = ill.get_artifact_job(art_id, "own")
    assert job["status"] == "done"
    assert job["errors"] == 1
    assert set(job["illustrations"]) == {"hero", "c2"}
    assert set(ill.load_illustrations(_content(doc_id))) == {"hero", "c2"}


async def test_job_all_failed_ends_done_with_empty_map(monkeypatch):
    fake, _ = _fake_image_gen(fail_all=True)
    monkeypatch.setattr(ill, "_generate_image", fake)
    nb_id, art_id, doc_id = _make_rows(_data(2))
    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    await ill._active_jobs[job_id]["task"]
    job = ill.get_artifact_job(art_id, "own")
    assert job["status"] == "done"
    assert job["illustrations"] == {}
    assert job["errors"] == 2
    assert ill.load_illustrations(_content(doc_id)) == {}


async def test_job_stops_when_artifact_deleted_mid_run(monkeypatch):
    nb_id, art_id, doc_id = _make_rows(_data(3))
    calls = []

    async def fake(content, owner):
        calls.append(content)
        if len(calls) == 1:
            s = _TS()
            try:
                s.query(cdb.NotebookArtifact).filter(cdb.NotebookArtifact.id == art_id).delete()
                s.commit()
            finally:
                s.close()
        name = uuid.uuid4().hex + ".png"
        Path(ill.GENERATED_IMAGES_DIR, name).write_bytes(b"x")
        return {"image_url": f"/api/generated-image/{name}"}
    monkeypatch.setattr(ill, "_generate_image", fake)
    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    await ill._active_jobs[job_id]["task"]
    assert len(calls) == 1                       # no second image after the row vanished
    assert ill._active_jobs[job_id]["status"] == "done"


def test_start_rejects_unknown_or_foreign_artifact():
    nb_id, art_id, _ = _make_rows(_data(1))
    with pytest.raises(ValueError, match="niet gevonden"):
        ill.start_illustration_job(nb_id, "nope", "own", _TS)
    with pytest.raises(ValueError, match="niet gevonden"):
        ill.start_illustration_job(nb_id, art_id, "someone-else", _TS)


async def test_start_rejects_second_job_for_same_artifact(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(content, owner):
        started.set()
        await release.wait()
        return {"error": "x"}
    monkeypatch.setattr(ill, "_generate_image", slow)
    nb_id, art_id, _ = _make_rows(_data(1))
    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    await started.wait()
    with pytest.raises(ValueError, match="loopt al"):
        ill.start_illustration_job(nb_id, art_id, "own", _TS)
    release.set()
    await ill._active_jobs[job_id]["task"]


def test_start_with_no_illustration_prompts_registers_done_job_without_images(monkeypatch):
    nb_id, art_id, _ = _make_rows(_data(0))
    job_id = ill.start_illustration_job(nb_id, art_id, "own", _TS)
    assert ill._active_jobs[job_id]["status"] == "done"
    assert ill.get_artifact_job(art_id, "own")["illustrations"] == {}


def test_get_artifact_job_is_owner_scoped():
    nb_id, art_id, _ = _make_rows(_data(0))
    ill.start_illustration_job(nb_id, art_id, "own", _TS)
    assert ill.get_artifact_job(art_id, "own") is not None
    assert ill.get_artifact_job(art_id, "other") is None
    assert ill.get_artifact_job("unknown", "own") is None
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_notebook_illustrations.py -q`
Expected: `ModuleNotFoundError: No module named 'src.notebook_illustrations'`.

- [ ] **Step 4: Implement the module**

Create `src/notebook_illustrations.py`:

```python
"""Infographic v2 illustrations: one AI image per block, generated async.

Modelled on src/notebook_covers.py: an in-memory ``_active_jobs`` registry,
``asyncio.create_task``, a start call that returns immediately and a viewer
that polls (GET /api/notebooks/{id}/artifacts/{artifact_id}/illustrations).
Jobs do not survive a restart; the artifact stays valid either way (icons,
or whatever illustrations already landed).

Per block: build_illustration_prompt -> do_generate_image (quality "low",
hero 1536x1024, others 1024x1024) -> copy the PNG from GENERATED_IMAGES_DIR
into NOTEBOOK_INFOGRAPHICS_DIR as "<artifact_id>-<block_id>-<hex8>.png" ->
write {"illustrations": {block_id: filename}} back into the artifact's
Document JSON. Persisting after *each* image means a job that dies halfway
keeps what it already produced. Failures are per block: logged, skipped,
counted in ``errors``; the job still ends "done".

Spec: docs/superpowers/specs/2026-09-03-notebooks-infographic-v2-design.md (Deel B).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from core.database import Document, Notebook, NotebookArtifact, SessionLocal
from src.constants import GENERATED_IMAGES_DIR, NOTEBOOK_INFOGRAPHICS_DIR
from src.notebook_infographic import MAX_ILLUSTRATIONS, extract_infographic, iter_blocks

logger = logging.getLogger(__name__)

JOB_TIMEOUT_SECONDS = 300
_JOB_EVICT_AFTER_SECONDS = 1800

_STYLE_SUFFIX = (
    ", flat vector illustration, pastel palette, soft shapes, white background, "
    "no text, no letters"
)
_HERO_SIZE = "1536x1024"
_BLOCK_SIZE = "1024x1024"
_QUALITY = "low"  # deliberately not the admin's image_quality: predictable cost

# "<artifact uuid>-<block slug>-<hex8>.png"
ILLUSTRATION_FILE_RE = re.compile(
    r"^([0-9a-f-]{36})-([a-z0-9][a-z0-9_-]{0,39})-([0-9a-f]{8})\.png$"
)
_GENERATED_NAME_RE = re.compile(r"^[a-f0-9]{8,64}\.(png|jpg|jpeg|webp)$")

ILLUSTRATION_HEADERS = {
    "Cache-Control": "private, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
}

_active_jobs: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def build_illustration_prompt(prompt: str, *, hero: bool) -> str:
    """do_generate_image content: prompt line, blank, size, quality."""
    size = _HERO_SIZE if hero else _BLOCK_SIZE
    return f"{prompt.strip()}{_STYLE_SUFFIX}\n\n{size}\n{_QUALITY}"


def select_illustration_blocks(data: dict) -> list[dict]:
    """First MAX_ILLUSTRATIONS blocks (document order) that carry a prompt."""
    picked = [b for b in iter_blocks(data) if b.get("illustration_prompt")]
    return picked[:MAX_ILLUSTRATIONS]


def load_illustrations(content: str) -> dict[str, str]:
    """block_id -> filename from stored v2 content; {} for anything else."""
    try:
        return dict(extract_infographic(content).get("illustrations") or {})
    except ValueError:
        return {}


def artifact_id_from_filename(filename: str) -> str:
    m = ILLUSTRATION_FILE_RE.fullmatch(filename or "")
    if not m:
        raise ValueError("Invalid illustration filename")
    return m.group(1)


# ---------------------------------------------------------------------------
# Job registry
# ---------------------------------------------------------------------------

def _reap_stale_jobs(now: float) -> None:
    for job_id, entry in list(_active_jobs.items()):
        if entry.get("status") == "running":
            continue
        completed_at = entry.get("completed_at")
        if completed_at is not None and (now - completed_at) > _JOB_EVICT_AFTER_SECONDS:
            _active_jobs.pop(job_id, None)


def _find_job(artifact_id: str) -> Optional[dict]:
    """Newest registry entry for this artifact (running preferred)."""
    best = None
    for entry in _active_jobs.values():
        if entry.get("artifact_id") != artifact_id:
            continue
        if entry.get("status") == "running":
            return entry
        if best is None or (entry.get("started_at") or 0) > (best.get("started_at") or 0):
            best = entry
    return best


def get_artifact_job(artifact_id: str, owner: str) -> Optional[dict]:
    entry = _find_job(artifact_id)
    if entry is None or (entry.get("owner") or "") != (owner or ""):
        return None
    return {
        "status": entry.get("status"),
        "illustrations": dict(entry.get("illustrations") or {}),
        "errors": int(entry.get("errors") or 0),
    }


def _load_artifact_data(session, artifact_id: str, owner: str) -> Optional[tuple[dict, Document]]:
    row = (
        session.query(NotebookArtifact, Document)
        .join(Document, Document.id == NotebookArtifact.document_id)
        .join(Notebook, Notebook.id == NotebookArtifact.notebook_id)
        .filter(NotebookArtifact.id == artifact_id, Notebook.owner == owner)
        .first()
    )
    if row is None:
        return None
    artifact, document = row
    return extract_infographic(document.current_content), document


def start_illustration_job(notebook_id: str, artifact_id: str, owner: str,
                           db_session_factory=None) -> str:
    """Validate, register and schedule an illustration job; return its id.

    Raises ValueError when the artifact is unknown/foreign/not v2, or when a
    job for it is already running. A v2 artifact without any
    illustration_prompt registers an already-"done" job (no task).
    """
    factory = db_session_factory or SessionLocal
    now = time.time()
    _reap_stale_jobs(now)
    running = _find_job(artifact_id)
    if running is not None and running.get("status") == "running":
        raise ValueError("Er loopt al een illustratie-job voor dit artifact")

    session = factory()
    try:
        loaded = _load_artifact_data(session, artifact_id, owner)
        if loaded is None:
            raise ValueError("Artifact niet gevonden")
        data, _document = loaded
    except ValueError as exc:
        if "niet gevonden" in str(exc):
            raise
        raise ValueError(f"Artifact is geen geldige v2-infographic: {exc}") from exc
    finally:
        session.close()

    blocks = select_illustration_blocks(data)
    job_id = uuid.uuid4().hex
    entry = {
        "status": "running" if blocks else "done",
        "owner": owner or "",
        "notebook_id": notebook_id,
        "artifact_id": artifact_id,
        "illustrations": dict(data.get("illustrations") or {}),
        "errors": 0,
        "started_at": now,
        "completed_at": None if blocks else now,
        "task": None,
    }
    _active_jobs[job_id] = entry
    if blocks:
        entry["task"] = asyncio.create_task(_run_job(job_id, artifact_id, owner, factory, blocks))
    return job_id


async def _run_job(job_id: str, artifact_id: str, owner: str, factory, blocks: list[dict]) -> None:
    entry = _active_jobs.get(job_id)
    if entry is None:
        return
    try:
        await asyncio.wait_for(_generate(entry, artifact_id, owner, factory, blocks),
                               timeout=JOB_TIMEOUT_SECONDS)
        entry["status"] = "done"
    except asyncio.CancelledError:
        entry["status"] = "cancelled"
    except asyncio.TimeoutError:
        # Whatever landed before the time-out is already persisted.
        entry["status"] = "done"
        entry["errors"] = int(entry.get("errors") or 0) + 1
        logger.warning("Illustration job %s timed out after %ss", job_id, JOB_TIMEOUT_SECONDS)
    except Exception as exc:
        entry["status"] = "error"
        logger.warning("Illustration job %s failed: %s", job_id, exc, exc_info=True)
    finally:
        entry["completed_at"] = time.time()


async def _generate_image(content: str, owner: str) -> dict:
    """Seam over the image pipeline (monkeypatched in tests)."""
    from src.ai_interaction import do_generate_image
    return await do_generate_image(content, owner=owner)


def _copy_generated(result: dict, artifact_id: str, block_id: str) -> str:
    """Copy the pipeline's PNG into NOTEBOOK_INFOGRAPHICS_DIR; return the filename."""
    if not isinstance(result, dict) or result.get("error"):
        raise RuntimeError(result.get("error", "Onbekende fout") if isinstance(result, dict) else "Onbekende fout")
    source_name = (result.get("image_url") or "").rsplit("/", 1)[-1]
    if not _GENERATED_NAME_RE.fullmatch(source_name):
        raise RuntimeError("Image generation returned unexpected URL format")
    source = Path(GENERATED_IMAGES_DIR) / source_name
    if not source.exists():
        raise RuntimeError(f"Generated image not found: {source_name}")
    dest_dir = Path(NOTEBOOK_INFOGRAPHICS_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{artifact_id}-{block_id}-{uuid.uuid4().hex[:8]}.png"
    shutil.copy2(str(source), str(dest_dir / filename))
    return filename


def _persist(factory, artifact_id: str, owner: str, block_id: str, filename: str) -> bool:
    """Write illustrations[block_id] into the Document JSON. False when the
    artifact is gone (job should stop)."""
    session = factory()
    try:
        loaded = _load_artifact_data(session, artifact_id, owner)
        if loaded is None:
            return False
        data, document = loaded
        data.setdefault("illustrations", {})[block_id] = filename
        document.current_content = json.dumps(data, ensure_ascii=False, indent=2)
        session.commit()
        return True
    finally:
        session.close()


async def _generate(entry: dict, artifact_id: str, owner: str, factory, blocks: list[dict]) -> None:
    for block in blocks:
        block_id = block["id"]
        content = build_illustration_prompt(block["illustration_prompt"], hero=(block["type"] == "hero"))
        try:
            result = await _generate_image(content, owner)
            filename = _copy_generated(result, artifact_id, block_id)
        except Exception as exc:
            entry["errors"] = int(entry.get("errors") or 0) + 1
            logger.warning("Illustration for %s/%s failed: %s", artifact_id, block_id, exc)
            continue
        if not await asyncio.to_thread(_persist, factory, artifact_id, owner, block_id, filename):
            logger.info("Illustration job for %s stopped: artifact gone", artifact_id)
            try:
                (Path(NOTEBOOK_INFOGRAPHICS_DIR) / filename).unlink(missing_ok=True)
            except OSError:
                pass
            return
        entry["illustrations"][block_id] = filename
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_notebook_illustrations.py -q`
Expected: all pass. Note `test_job_stops_when_artifact_deleted_mid_run` deletes the row before the first `_persist`, so exactly one image call happens and the job ends `done`.

- [ ] **Step 6: Commit**

```bash
git add src/constants.py src/notebook_illustrations.py tests/test_notebook_illustrations.py
git commit -m "feat(notebooks): async illustration job for infographic v2

Ed de Feber, in nauwe samenwerking met Claude"
```

---

### Task 5: File resolving + janitor

**Files:**
- Modify: `src/notebook_illustrations.py` (append)
- Test: `tests/test_notebook_illustrations.py` (append)

**Interfaces:**
- Produces: `resolve_illustration_path(filename) -> Path` (raises `HTTPException` 400/404, same shape as `resolve_cover_image_path`); `cleanup_orphaned_illustrations(db_session_factory, *, max_age_seconds=3600) -> int` (files removed).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notebook_illustrations.py`:

```python
# ---- resolve + janitor ------------------------------------------------------

def _age(path, seconds):
    old = time.time() - seconds
    _os.utime(path, (old, old))


def test_resolve_illustration_path_rejects_bad_names_and_traversal(tmp_path):
    for bad in ("../x.png", "x.png", f"{_UUID}-hero-0123abcd.PNG", "", "a/b.png"):
        with pytest.raises(HTTPException) as exc:
            ill.resolve_illustration_path(bad)
        assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        ill.resolve_illustration_path(f"{_UUID}-hero-0123abcd.png")
    assert exc.value.status_code == 404


def test_resolve_illustration_path_returns_existing_file():
    name = f"{_UUID}-hero-0123abcd.png"
    Path(ill.NOTEBOOK_INFOGRAPHICS_DIR, name).write_bytes(b"x")
    assert ill.resolve_illustration_path(name).name == name


def test_cleanup_removes_old_orphans_keeps_referenced_and_fresh():
    nb_id, art_id, _ = _make_rows(_data(0))
    d = Path(ill.NOTEBOOK_INFOGRAPHICS_DIR)
    referenced_old = d / f"{art_id}-hero-0123abcd.png"
    orphan_old = d / f"{_UUID}-hero-0123abcd.png"
    orphan_fresh = d / f"{_UUID}-c1-0123abcd.png"
    stray = d / "notes.txt"
    for p in (referenced_old, orphan_old, orphan_fresh, stray):
        p.write_bytes(b"x")
    _age(referenced_old, 7200)
    _age(orphan_old, 7200)
    _age(stray, 7200)

    removed = ill.cleanup_orphaned_illustrations(_TS, max_age_seconds=3600)

    assert removed == 1
    assert referenced_old.exists()
    assert not orphan_old.exists()
    assert orphan_fresh.exists()
    assert stray.exists()          # non-matching names are never touched


def test_cleanup_missing_dir_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(ill, "NOTEBOOK_INFOGRAPHICS_DIR", str(tmp_path / "nope"))
    assert ill.cleanup_orphaned_illustrations(_TS) == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_notebook_illustrations.py -k "resolve or cleanup" -q`
Expected: AttributeError (`resolve_illustration_path`).

- [ ] **Step 3: Implement**

Append to `src/notebook_illustrations.py`:

```python
# ---------------------------------------------------------------------------
# Serving + janitor
# ---------------------------------------------------------------------------

def resolve_illustration_path(filename: str) -> Path:
    """Whitelist + containment check. Raises HTTPException(400/404)."""
    from fastapi import HTTPException
    if not isinstance(filename, str) or not ILLUSTRATION_FILE_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    root = Path(NOTEBOOK_INFOGRAPHICS_DIR).resolve()
    path = (Path(NOTEBOOK_INFOGRAPHICS_DIR) / filename).resolve()
    try:
        if os.path.commonpath([str(root), str(path)]) != str(root):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Illustration not found")
    return path


def cleanup_orphaned_illustrations(db_session_factory, *, max_age_seconds: int = 3600) -> int:
    """Remove illustration files older than `max_age_seconds` whose artifact
    id prefix no longer exists. Returns the number removed. Age is checked
    before the DB query so a just-written file of a still-uncommitted job
    is never touched (same reasoning as the audio/video janitors)."""
    directory = Path(NOTEBOOK_INFOGRAPHICS_DIR)
    if not directory.is_dir():
        return 0
    now = time.time()
    candidates: list[tuple[Path, str]] = []
    for path in directory.iterdir():
        m = ILLUSTRATION_FILE_RE.fullmatch(path.name)
        if not m or not path.is_file():
            continue
        try:
            if now - path.stat().st_mtime <= max_age_seconds:
                continue
        except OSError:
            continue
        candidates.append((path, m.group(1)))
    if not candidates:
        return 0
    session = db_session_factory()
    try:
        wanted = {aid for _p, aid in candidates}
        existing = {
            row[0] for row in session.query(NotebookArtifact.id)
            .filter(NotebookArtifact.id.in_(wanted)).all()
        }
    finally:
        session.close()
    removed = 0
    for path, artifact_id in candidates:
        if artifact_id in existing:
            continue
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_notebook_illustrations.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/notebook_illustrations.py tests/test_notebook_illustrations.py
git commit -m "feat(notebooks): illustration path whitelist + orphan janitor

Ed de Feber, in nauwe samenwerking met Claude"
```

---

### Task 6: Routes, passive gate pattern, hourly janitor

**Files:**
- Modify: `routes/notebook_routes.py` (imports `:14-45`; `create_artifact` `:441-501`; report route `:660-666`; new routes after the cover routes `:931-953`)
- Modify: `src/interactive_gate.py:85-95`
- Modify: `app.py:1253-1265` (add a janitor loop after the video one)
- Test: `tests/test_routes_notebook_infographic.py` (create), `tests/test_interactive_gate_passive.py` (append)

**Interfaces:**
- Consumes: `generate_infographic(..., poll_url=)` (Task 3); `start_illustration_job`, `get_artifact_job`, `load_illustrations`, `resolve_illustration_path`, `artifact_id_from_filename`, `ILLUSTRATION_HEADERS`, `cleanup_orphaned_illustrations` (Tasks 4–5); `get_setting` from `src/settings.py`.
- Produces:
  - `POST /api/notebooks/{id}/artifacts` with `kind=infographic` starts the job when `get_setting("image_gen_enabled", False)` is truthy; response unchanged (`artifact.to_dict()`).
  - `GET /api/notebooks/{id}/artifacts/{artifact_id}/illustrations` → `{"status": "running"|"done"|"none", "illustrations": {block_id: "/api/notebook-illustration/<fn>"}}`.
  - `GET /api/notebook-illustration/{filename}` → PNG, owner-scoped via the artifact-id prefix.
  - Report route passes `poll_url` while a job is `running`.

- [ ] **Step 1: Write the failing route tests**

Create `tests/test_routes_notebook_infographic.py`:

```python
"""Infographic v2 routes: job start on POST, status endpoint, serving, report.

Fixture pattern copied from tests/test_routes_notebook_artifacts.py.
`generate_artifact` and the illustration-job functions are monkeypatched on
the route module; rows are real (file-backed temp sqlite).
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-infographic-routes")

import json
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import routes.notebook_routes as nbr
import src.notebook_illustrations as ill
from tests.helpers.sqlite_db import make_temp_sqlite

_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _v2_json(illustrations=None):
    d = {
        "title": "T", "takeaway": "one sentence",
        "blocks": [
            {"id": "hero", "type": "hero", "heading": "Hero", "text": "t", "illustration_prompt": "a hub"},
            {"id": "col", "type": "column", "heading": "Col", "subheading": "s", "children": [
                {"id": "c1", "type": "icon_card", "heading": "C1", "text": "t"},
                {"id": "c2", "type": "icon_card", "heading": "C2", "text": "t"},
            ]},
            {"id": "k1", "type": "icon_card", "heading": "K1", "text": "t"},
            {"id": "k2", "type": "icon_card", "heading": "K2", "text": "t"},
            {"id": "k3", "type": "icon_card", "heading": "K3", "text": "t"},
        ],
    }
    if illustrations:
        d["illustrations"] = illustrations
    return json.dumps(d)


class _FakeRagManager:
    def __init__(self):
        self.vector_rag = self

    def add_document(self, text, metadata):
        return True

    def remove_notebook(self, notebook_id, document_id=None):
        pass

    def _split_into_chunks(self, text):
        return [text]


@pytest.fixture()
def ts(monkeypatch):
    test_session_local, engine, tmpfile = make_temp_sqlite(db.Base.metadata)
    monkeypatch.setattr(nbr, "SessionLocal", test_session_local)
    ill._active_jobs.clear()
    yield test_session_local
    ill._active_jobs.clear()
    tmpfile.close()


def _client(monkeypatch, user="ed"):
    monkeypatch.setattr(nbr, "get_current_user", lambda request: user)
    app = FastAPI()
    app.include_router(nbr.setup_notebook_routes(rag_manager=_FakeRagManager()))
    return TestClient(app, raise_server_exceptions=False)


def _rows(ts, content, owner="ed", kind="infographic"):
    s = ts()
    try:
        nb = db.Notebook(id=str(uuid.uuid4()), name="NB", owner=owner)
        s.add(nb)
        doc = db.Document(id=str(uuid.uuid4()), title="Doc", owner=owner,
                          language="markdown", current_content=content)
        s.add(doc)
        s.commit()
        art = db.NotebookArtifact(id=str(uuid.uuid4()), notebook_id=nb.id,
                                  document_id=doc.id, kind=kind, title="T")
        s.add(art)
        s.commit()
        return nb.id, art.id
    finally:
        s.close()


def _fake_generate(content):
    async def fake(notebook_id, owner, kind, db_session, focus=None, layout_instruction=None):
        document_id = str(uuid.uuid4())
        db_session.add(db.Document(id=document_id, title="Gen", owner=owner,
                                   language="markdown", current_content=content, session_id=None))
        artifact = db.NotebookArtifact(id=str(uuid.uuid4()), notebook_id=notebook_id,
                                       document_id=document_id, kind=kind, title="Gen")
        db_session.add(artifact)
        db_session.commit()
        db_session.refresh(artifact)
        return artifact
    return fake


# ---- POST /artifacts starts the job ---------------------------------------

def test_post_infographic_starts_job_when_image_gen_enabled(ts, monkeypatch):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate(_v2_json()))
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True if key == "image_gen_enabled" else default)
    started = []
    monkeypatch.setattr(nbr, "start_illustration_job",
                        lambda notebook_id, artifact_id, owner: started.append((notebook_id, artifact_id, owner)) or "job1")
    client = _client(monkeypatch)
    nb_id, _ = _rows(ts, "# whatever")
    r = client.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "infographic"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "infographic"
    assert started == [(nb_id, r.json()["id"], "ed")]


def test_post_infographic_skips_job_when_image_gen_disabled(ts, monkeypatch):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate(_v2_json()))
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(nbr, "start_illustration_job", lambda *a, **k: pytest.fail("must not start"))
    client = _client(monkeypatch)
    nb_id, _ = _rows(ts, "# whatever")
    r = client.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "infographic"})
    assert r.status_code == 200


def test_post_infographic_job_start_failure_does_not_fail_request(ts, monkeypatch):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate(_v2_json()))
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)

    def boom(*a, **k):
        raise ValueError("nope")
    monkeypatch.setattr(nbr, "start_illustration_job", boom)
    client = _client(monkeypatch)
    nb_id, _ = _rows(ts, "# whatever")
    r = client.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "infographic"})
    assert r.status_code == 200


def test_post_other_kind_never_starts_job(ts, monkeypatch):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate("# faq"))
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)
    monkeypatch.setattr(nbr, "start_illustration_job", lambda *a, **k: pytest.fail("must not start"))
    client = _client(monkeypatch)
    nb_id, _ = _rows(ts, "# whatever")
    assert client.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).status_code == 200


# ---- status endpoint --------------------------------------------------------

def test_status_none_when_image_gen_disabled(ts, monkeypatch):
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: default)
    client = _client(monkeypatch)
    nb_id, art_id = _rows(ts, _v2_json({"hero": f"{_UUID}-hero-0123abcd.png"}))
    r = client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/illustrations")
    assert r.status_code == 200
    assert r.json() == {"status": "none", "illustrations": {}}


def test_status_none_without_job_but_returns_stored_map(ts, monkeypatch):
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)
    client = _client(monkeypatch)
    nb_id, art_id = _rows(ts, _v2_json())
    # store a map on the doc for this artifact id
    s = ts()
    try:
        art = s.query(db.NotebookArtifact).get(art_id)
        doc = s.query(db.Document).get(art.document_id)
        doc.current_content = _v2_json({"hero": f"{art_id}-hero-0123abcd.png"})
        s.commit()
    finally:
        s.close()
    r = client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/illustrations")
    assert r.json() == {"status": "none",
                        "illustrations": {"hero": f"/api/notebook-illustration/{art_id}-hero-0123abcd.png"}}


def test_status_running_and_done_follow_job_registry(ts, monkeypatch):
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)
    client = _client(monkeypatch)
    nb_id, art_id = _rows(ts, _v2_json())
    ill._active_jobs["j"] = {"status": "running", "owner": "ed", "artifact_id": art_id,
                             "illustrations": {"hero": f"{art_id}-hero-0123abcd.png"}, "errors": 0,
                             "started_at": 1.0, "completed_at": None}
    r = client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/illustrations").json()
    assert r["status"] == "running"
    assert r["illustrations"] == {"hero": f"/api/notebook-illustration/{art_id}-hero-0123abcd.png"}
    ill._active_jobs["j"]["status"] = "done"
    assert client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/illustrations").json()["status"] == "done"


def test_status_404_for_foreign_notebook_or_unknown_artifact(ts, monkeypatch):
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)
    nb_id, art_id = _rows(ts, _v2_json(), owner="someone-else")
    client = _client(monkeypatch, user="ed")
    assert client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/illustrations").status_code == 404
    nb2, _ = _rows(ts, _v2_json(), owner="ed")
    assert client.get(f"/api/notebooks/{nb2}/artifacts/nope/illustrations").status_code == 404


# ---- report route -------------------------------------------------------------

def test_report_renders_v2_with_poll_url_while_running(ts, monkeypatch):
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)
    client = _client(monkeypatch)
    nb_id, art_id = _rows(ts, _v2_json())
    ill._active_jobs["j"] = {"status": "running", "owner": "ed", "artifact_id": art_id,
                             "illustrations": {}, "errors": 0, "started_at": 1.0, "completed_at": None}
    r = client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert 'class="ig2-wrap"' in r.text
    assert f'data-poll-url="/api/notebooks/{nb_id}/artifacts/{art_id}/illustrations"' in r.text


def test_report_renders_v2_without_poll_when_no_job(ts, monkeypatch):
    monkeypatch.setattr(nbr, "get_setting", lambda key, default=None: True)
    client = _client(monkeypatch)
    nb_id, art_id = _rows(ts, _v2_json())
    r = client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert 'class="ig2-wrap"' in r.text
    assert "data-illustrations" not in r.text


def test_report_still_renders_legacy_markdown(ts, monkeypatch):
    client = _client(monkeypatch)
    nb_id, art_id = _rows(ts, "# Oud\n\n## Key numbers\n- **3** — x\n\n## S\n- f\n\n> t\n")
    r = client.get(f"/api/notebooks/{nb_id}/artifacts/{art_id}/report")
    assert r.status_code == 200
    assert 'class="ig-grid"' in r.text


# ---- serving route --------------------------------------------------------------

def test_serve_illustration_owner_scoped(ts, monkeypatch, tmp_path):
    monkeypatch.setattr(ill, "NOTEBOOK_INFOGRAPHICS_DIR", str(tmp_path))
    nb_id, art_id = _rows(ts, _v2_json(), owner="ed")
    name = f"{art_id}-hero-0123abcd.png"
    (tmp_path / name).write_bytes(b"\x89PNG")
    r = _client(monkeypatch, user="ed").get(f"/api/notebook-illustration/{name}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert _client(monkeypatch, user="other").get(f"/api/notebook-illustration/{name}").status_code == 404


def test_serve_illustration_rejects_bad_names(ts, monkeypatch, tmp_path):
    monkeypatch.setattr(ill, "NOTEBOOK_INFOGRAPHICS_DIR", str(tmp_path))
    client = _client(monkeypatch)
    assert client.get("/api/notebook-illustration/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)
    assert client.get("/api/notebook-illustration/x.png").status_code == 400
    assert client.get(f"/api/notebook-illustration/{_UUID}-hero-0123abcd.png").status_code == 404
```

Append to `tests/test_interactive_gate_passive.py`:

```python
def test_infographic_illustrations_status_poll_get_is_not_tracked():
    assert should_track_interactive_request(
        "/api/notebooks/nb-123/artifacts/art-456/illustrations", "GET") is False


def test_infographic_illustrations_post_and_sibling_paths_are_still_tracked():
    assert should_track_interactive_request(
        "/api/notebooks/nb-123/artifacts/art-456/illustrations", "POST") is True
    assert should_track_interactive_request(
        "/api/notebooks/nb-123/artifacts/art-456/report", "GET") is True
    assert should_track_interactive_request(
        "/api/notebooks/nb-123/artifacts/art-456/illustrations/extra", "GET") is True
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_routes_notebook_infographic.py tests/test_interactive_gate_passive.py -q`
Expected: AttributeError on `nbr.get_setting` / `nbr.start_illustration_job`, 404s on the new routes, gate assertion failures.

- [ ] **Step 3: Implement the routes**

In `routes/notebook_routes.py`:

Imports (near line 36-45):

```python
from src.notebook_illustrations import (
    ILLUSTRATION_HEADERS,
    artifact_id_from_filename,
    get_artifact_job,
    load_illustrations,
    resolve_illustration_path,
    start_illustration_job,
)
from src.settings import get_setting, load_settings
```

In `create_artifact`, replace `return artifact.to_dict()` with:

```python
            if kind == "infographic" and get_setting("image_gen_enabled", False):
                # Fire-and-forget: the viewer polls for illustrations. A
                # failure to start must never turn a successfully stored
                # artifact into an error response.
                try:
                    start_illustration_job(notebook_id, artifact.id, user)
                except Exception as exc:
                    logger.warning("Illustration job not started for %s: %s", artifact.id, exc)
            return artifact.to_dict()
```

In `get_artifact_report`, replace the `elif artifact.kind == "infographic":` branch with:

```python
            elif artifact.kind == "infographic":
                job = get_artifact_job(artifact.id, user)
                poll_url = None
                if job is not None and job.get("status") == "running":
                    poll_url = (f"/api/notebooks/{nb.id}/artifacts/{artifact.id}/illustrations")
                html_content = generate_infographic(
                    title=artifact.title or document.title,
                    markdown=document.current_content,
                    notebook_name=nb.name,
                    generated_at=datetime.now(),
                    poll_url=poll_url,
                )
```

After the cover routes (before `return router`):

```python
    # ---- GET /api/notebooks/{id}/artifacts/{artifact_id}/illustrations ----
    @router.get("/api/notebooks/{notebook_id}/artifacts/{artifact_id}/illustrations")
    async def get_artifact_illustrations(request: Request, notebook_id: str, artifact_id: str):
        """Viewer poll: job status + illustration URLs. Passive in the
        interactive gate (src/interactive_gate.py _PASSIVE_PATTERNS)."""
        user = get_current_user(request)
        db_session = SessionLocal()
        try:
            nb = _get_owned_notebook(db_session, notebook_id, user)
            row = (
                db_session.query(NotebookArtifact, Document)
                .join(Document, Document.id == NotebookArtifact.document_id)
                .filter(NotebookArtifact.id == artifact_id, NotebookArtifact.notebook_id == nb.id)
                .first()
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Artifact not found")
            _artifact, document = row
            content = document.current_content
        finally:
            db_session.close()
        if not get_setting("image_gen_enabled", False):
            return {"status": "none", "illustrations": {}}
        job = get_artifact_job(artifact_id, user)
        status = "none"
        illustrations = load_illustrations(content)
        if job is not None:
            status = "running" if job.get("status") == "running" else "done"
            illustrations.update(job.get("illustrations") or {})
        return {
            "status": status,
            "illustrations": {
                block_id: f"/api/notebook-illustration/{fn}"
                for block_id, fn in illustrations.items()
            },
        }

    # ---- GET /api/notebook-illustration/{filename} ----
    @router.get("/api/notebook-illustration/{filename}")
    async def serve_notebook_illustration(request: Request, filename: str):
        user = get_current_user(request)
        path = resolve_illustration_path(filename)
        artifact_id = artifact_id_from_filename(filename)
        db_session = SessionLocal()
        try:
            row = (
                db_session.query(Notebook.owner)
                .join(NotebookArtifact, NotebookArtifact.notebook_id == Notebook.id)
                .filter(NotebookArtifact.id == artifact_id)
                .first()
            )
            if row is None or row[0] != user:
                raise HTTPException(status_code=404, detail="Illustration not found")
        finally:
            db_session.close()
        return FileResponse(str(path), media_type="image/png", headers=ILLUSTRATION_HEADERS)
```

In `src/interactive_gate.py`, add to `_PASSIVE_PATTERNS` after the `/video/` pattern:

```python
    # The infographic-illustrations poller (GET
    # /api/notebooks/{id}/artifacts/{artifact_id}/illustrations, polled every
    # 3s by the v2 viewer page in src/notebook_infographic.py) — same
    # reasoning as the podcast/video pollers above.
    re.compile(r"^/api/notebooks/[^/]+/artifacts/[^/]+/illustrations$"),
```

In `app.py`, after the video janitor block (line ~1265) add:

```python
    # Infographic-illustration janitor — sweeps NOTEBOOK_INFOGRAPHICS_DIR
    # hourly for PNGs whose artifact no longer exists (same shape as above).
    async def _notebook_illustration_janitor_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                from src.notebook_illustrations import cleanup_orphaned_illustrations
                await asyncio.to_thread(cleanup_orphaned_illustrations, SessionLocal)
            except Exception as e:
                logger.debug(f"Notebook illustration janitor skipped: {e}")
    _startup_tasks.append(asyncio.create_task(_notebook_illustration_janitor_loop()))
```

(Mirror the exact sleep/structure of `_notebook_video_janitor_loop` at `app.py:1255-1265` — copy it and change the import + message.)

- [ ] **Step 4: Run tests + syntax checks**

```bash
.venv/bin/python -m pytest tests/test_routes_notebook_infographic.py tests/test_interactive_gate_passive.py tests/test_routes_notebook_artifacts.py tests/test_notebook_infographic.py -q
.venv/bin/python -m py_compile app.py routes/notebook_routes.py src/interactive_gate.py src/notebook_illustrations.py
```
Expected: all pass, no compile errors.

- [ ] **Step 5: Commit**

```bash
git add routes/notebook_routes.py src/interactive_gate.py app.py tests/test_routes_notebook_infographic.py tests/test_interactive_gate_passive.py
git commit -m "feat(notebooks): illustration job start, status + serving routes, janitor wiring

Ed de Feber, in nauwe samenwerking met Claude"
```

---

### Task 7: Studio tile copy + docs

**Files:**
- Modify: `static/js/notebookWorkspace.js:1787-1788` (tile markup)
- Modify: `CLAUDE.md` (Notebooks paragraph in Architecture)
- Modify: `docs/notebooklm-gap-analyse.md` (status header line)

**Interfaces:** none (copy only).

- [ ] **Step 1: Tile tooltip**

In `_studioPanelSkeleton`, add a per-kind tooltip map above the function and use it on the generic tiles:

```js
// Tooltips for the generic artifact tiles — the only place a tile says more
// than its label. Infographic v2 (2026-09-03) generates AI illustrations
// per block when image generation is enabled in Settings.
const _KIND_TITLES = {
  infographic: 'Infographic met AI-illustraties (als beeldgeneratie aan staat)',
};
```

and change the tile template to include `title="${_esc(_KIND_TITLES[kind] || KIND_LABELS[kind])}"` on the `<button>`.

Run: `node --check static/js/notebookWorkspace.js` → no output.

- [ ] **Step 2: Existing static tests**

Run: `.venv/bin/python -m pytest tests/test_notebook_workspace_static.py -q`
Expected: pass (if a test asserts the exact tile markup, update it to include the `title` attribute).

- [ ] **Step 3: Docs**

In `CLAUDE.md`, in the Notebooks bullet, after the sentence on `src/notebook_infographic.py` renderers, add: "Infographic v2 (2026-09-03): the model emits JSON (`extract_infographic`), rendered by `render_infographic_v2`; legacy markdown artifacts still render through the old parser. Per-block AI illustrations come from an async job in `src/notebook_illustrations.py` (covers-style registry, max 5 images at quality `low`, only when `image_gen_enabled`), served via `/api/notebook-illustration/{fn}` with its own hourly janitor; the viewer polls `…/artifacts/{id}/illustrations` (passive in `_PASSIVE_PATTERNS`)."

In `docs/notebooklm-gap-analyse.md`, add the spec + plan paths to the status header list.

- [ ] **Step 4: Commit**

```bash
git add static/js/notebookWorkspace.js CLAUDE.md docs/notebooklm-gap-analyse.md
git commit -m "docs(notebooks): infographic v2 tile tooltip + architecture notes

Ed de Feber, in nauwe samenwerking met Claude"
```

---

### Task 8: Full suite, smoke on :7001, PR

**Files:**
- Create: `docs/sessions/2026-09-03-infographic-v2.md`

- [ ] **Step 1: Full test suite + syntax checks**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m py_compile app.py routes/*.py src/*.py
node --check static/js/notebookWorkspace.js
```
Expected: 0 failures (baseline before this work: 5704 passed). Paste the summary line in chat.

- [ ] **Step 2: Smoke instance**

Follow the memory `reference_ithaka_smoke_chroma_gotcha`: fresh data dir, explicit Chroma host/port, sandbox off so the browser can reach it.

```bash
mkdir -p $SCRATCH/ig2-smoke
CHROMADB_HOST=localhost CHROMADB_PORT=8100 ITHAKA_DATA_DIR=$SCRATCH/ig2-smoke \
  .venv/bin/python -m uvicorn app:app --port 7001
```
(run in background with `dangerouslyDisableSandbox: true`; create the first account via `POST /api/auth/setup`; copy `image_model`/OpenAI key into the smoke settings via Settings → Image Generation so `image_gen_enabled` can be toggled).

- [ ] **Step 3: Browser smoke (chrome-devtools MCP)**

1. Log in, create a notebook, add one real source (a PDF from `docs/` or a URL).
2. `image_gen_enabled = false`: click the Infographic tile → open the artifact → desktop screenshot + `emulate` 360 px screenshot; verify: 3-column grid / single column, icons in every slot, no `data-illustrations` attribute, `document.documentElement.scrollWidth <= window.innerWidth`, console without new errors.
3. `image_gen_enabled = true`: generate again → open immediately → confirm `data-illustrations="pending"` and a `GET …/illustrations` request every 3 s in the network panel → wait for images to fade in (desktop + 360 px screenshots) → confirm the attribute is removed when status is `done` and the files exist under `$SCRATCH/ig2-smoke/notebook_infographics/`.
4. Reload the artifact → images render from the stored map without polling.
5. Legacy check: pick an existing markdown infographic on prod-data (or insert one) → still renders the old poster.

Paste the full smoke output (commands + observations + screenshot paths) in chat **before** any merge.

- [ ] **Step 4: Session log + PR**

Write `docs/sessions/2026-09-03-infographic-v2.md` (what shipped, test counts, smoke evidence, open points: e.g. cost per infographic at `low`, hero size support per model). Then:

```bash
git add docs/sessions/2026-09-03-infographic-v2.md
git commit -m "docs(sessions): infographic v2 implementation + smoke

Ed de Feber, in nauwe samenwerking met Claude"
git push -u origin feat/infographic-v2
gh pr create --base dev --title "feat(notebooks): infographic v2 — hybrid HTML layout with AI illustrations" --body-file - <<'EOF'
Implements docs/superpowers/specs/2026-09-03-notebooks-infographic-v2-design.md
(plan: docs/superpowers/plans/2026-09-03-notebooks-infographic-v2.md).

- JSON content model + Dutch validator (retry seam), legacy markdown kept
- v2 renderer: columns / steps / hero / comparison / key numbers, force-light, mobile single column
- async illustration job (max 5, quality low, hero 1536x1024), incremental persistence, janitor
- status + serving routes, passive gate pattern, viewer live-swap

Smoke: see PR chat (desktop + 360px, with and without image_gen_enabled).

Ed de Feber, in nauwe samenwerking met Claude
EOF
```

Merge only after the smoke output is visible in chat and Ed gives the go (global CLAUDE.md rule).

---

## Self-review notes

- **Spec coverage:** Deel A → Task 1–2; Deel B (job, prompt suffix, sizes, quality `low`, incremental persistence, per-block errors, time-out 300, registry/reaper, serving, janitor, status endpoint, passive pattern) → Tasks 4–6; Deel C (layout, slots, steps/comparison/key_numbers/hero renderers, takeaway, viewer script 3 s/120 s, tile copy) → Tasks 3 + 7; Foutafhandeling (invalid JSON → retry then fallback card; image gen off → `none`; per-block failure; artifact deleted mid-job) → Tasks 3, 4, 6; Tests section → the three new test files + gate test; Smoke → Task 8.
- **Deviation from spec, documented:** the HTML route is `GET …/artifacts/{artifact_id}/report` (existing), not `/html` as written in the spec. The spec's "kwaliteit `image_quality` niet volgen" is honoured because `do_generate_image` only substitutes the admin quality when the requested quality is `medium`; we always pass `low`.
- **Type consistency:** `generate_infographic(..., *, illustrations_url_base, poll_url)` is used identically in Task 3 tests and Task 6 route; `get_artifact_job` returns `status/illustrations/errors` in Task 4 and is read that way in Task 6; `ILLUSTRATION_FILE_RE` group 1 is the artifact id in Task 4, 5 and the serving route.
