# Meeting Recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record a meeting from the browser mic, stop, and get Dutch minutes in a fixed template as a Library Document — via chunked upload, ffmpeg split, parallel STT, Ed's recursive condensation, one template LLM call.

**Architecture:** `static/js/meetings.js` (panel + MediaRecorder timeslice + sequential chunk uploader) → `routes/meeting_routes.py` (Meeting rows, append chunks, finish → async job) → `src/meeting_minutes.py` (job runner mirroring `src/notebook_audio.py`: split → transcribe → correct → condense → minutes → Document). Status via polling `GET /api/meetings/{id}` (passive).

**Tech Stack:** FastAPI, SQLAlchemy (`core/database.py`), ffmpeg (in Docker image), `STTService`, `task_llm_call_async`, vanilla ES modules, pytest + node tests.

**Spec:** `docs/superpowers/specs/2026-09-04-meeting-recorder-design.md` — read it first; it is the authority.

## Global Constraints

- Constants rule: paths only from `src/constants.py` (`MEETING_AUDIO_DIR`); never `Path(__file__)`, `/app/...`, or `"data/..."`.
- Every LLM call inside the job: `task_llm_call_async(messages, owner=owner, wait_for_quiet=False, workload="foreground")` (self-deadlock rule).
- Every generation prompt embeds `DUTCH_OUTPUT_RULE` from `src/notebook_language.py`; transcript text goes in via `untrusted_context_message(...)` from `src.prompt_security` with `UNTRUSTED_CONTEXT_POLICY` in the system message (see `src/notebook_audio.py` ~line 826 for the exact pattern).
- No Unicode emoji in UI or code; inline monochrome SVG; reuse existing CSS variables/classes (`--red`, `.notes-pane`, `.doc-action-icon-btn`, `.memory-toolbar-btn`, `.list-item`, `.icon-rail-btn`); no new colours.
- UI labels English; API error strings and generated content Dutch.
- Commits: Conventional Commits, `feat(meetings): …`; body ends with exactly `Ed de Feber, in nauwe samenwerking met Claude` (no Co-Authored-By trailer).
- Tests: `.venv/bin/python -m pytest <file> -q`; JS: `node --check static/js/meetings.js`. Never run the full suite per task (slow); run the touched files plus `tests/test_notebooks_gate_seam.py`.
- Never dispatch subagents from an implementer.

---

### Task 1: Foundation — constants, limits, `Meeting` model, STT kwargs

**Files:**
- Modify: `src/constants.py` (after `NOTEBOOK_INFOGRAPHICS_DIR` + the guarded makedirs block ~line 51-85)
- Modify: `src/upload_limits.py` (after `STT_MAX_AUDIO_BYTES` ~line 56)
- Modify: `core/database.py` (after `class DocumentVersion` ~line 246)
- Modify: `services/stt/stt_service.py` (`transcribe` ~line 392, `_transcribe_local` ~311, `_transcribe_api` ~346)
- Test: `tests/test_meeting_foundation.py`, `tests/test_stt_transcribe_kwargs.py`

**Interfaces (Produces):**
- `src.constants.MEETING_AUDIO_DIR: str` = `os.path.join(DATA_DIR, "meeting_audio")`, eagerly created inside the same guarded try as `NOTEBOOK_AUDIO_DIR` (degrade, never crash at import).
- `src.upload_limits.MEETING_CHUNK_MAX_BYTES` (env `ITHAKA_MEETING_CHUNK_MAX_BYTES`, default `10 * 1024 * 1024`), `MEETING_AUDIO_MAX_BYTES` (env `ITHAKA_MEETING_AUDIO_MAX_BYTES`, default `500 * 1024 * 1024`), both via `read_byte_limit_env`.
- `core.database.Meeting(TimestampMixin, Base)`, `__tablename__ = "meetings"`: `id` String pk; `owner` String index nullable=False; `title` String nullable=False default "Meeting"; `agenda` Text nullable; `key_terms` Text nullable; `status` String nullable=False default "recording"; `phase` String nullable; `error` Text nullable; `audio_path` String nullable; `bytes_total` Integer default 0; `duration_seconds` Integer nullable; `document_id` String nullable; `finished_at` DateTime nullable. Re-export from `src/database.py` if that module re-exports models explicitly (check `grep -n "Document" src/database.py`).
- `STTService.transcribe(self, audio_bytes: bytes, *, prompt: Optional[str] = None, timeout: float = 60.0, filename: str = "audio.webm") -> Optional[str]`; passes through to `_transcribe_local(audio_bytes, language, prompt=prompt, filename=filename)` and `_transcribe_api(audio_bytes, endpoint_id, model, language, prompt=prompt, timeout=timeout, filename=filename)`. API: `files={"file": (filename, io.BytesIO(audio_bytes), mime)}` with mime `audio/ogg` for `.ogg`, `audio/mpeg` for `.mp3`, `audio/wav` for `.wav`, else `audio/webm`; `data["prompt"] = prompt` when truthy; `httpx.post(..., timeout=timeout)`. Local: `NamedTemporaryFile(suffix=Path(filename).suffix or ".webm")`, `kwargs["initial_prompt"] = prompt` when truthy.

- [ ] **Step 1: Tests first** — `tests/test_meeting_foundation.py`:

```python
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-meeting-foundation")

import core.database as db
from src import constants, upload_limits
from tests.helpers.sqlite_db import make_temp_sqlite


def test_meeting_audio_dir_under_data_dir():
    assert constants.MEETING_AUDIO_DIR == os.path.join(constants.DATA_DIR, "meeting_audio")


def test_meeting_limits_defaults():
    assert upload_limits.MEETING_CHUNK_MAX_BYTES == 10 * 1024 * 1024
    assert upload_limits.MEETING_AUDIO_MAX_BYTES == 500 * 1024 * 1024


def test_meeting_row_roundtrip():
    SessionLocal, engine, tmp = make_temp_sqlite(db.Base.metadata)
    s = SessionLocal()
    try:
        s.add(db.Meeting(id="m1", owner="ed", title="Weekly"))
        s.commit()
        row = s.query(db.Meeting).filter_by(id="m1").one()
        assert row.status == "recording" and row.bytes_total == 0 and row.document_id is None
    finally:
        s.close(); tmp.close()
```

`tests/test_stt_transcribe_kwargs.py` (pattern: look at `tests/test_stt_transcribe_error_detail.py` for how `STTService` is constructed and `_load_settings` / `SessionLocal` / `httpx.post` are monkeypatched):

```python
def test_api_transcribe_passes_prompt_timeout_filename(monkeypatch, service_with_endpoint):
    captured = {}
    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"text": "hallo"}
    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured.update(url=url, files=files, data=data, timeout=timeout); return _Resp()
    monkeypatch.setattr("services.stt.stt_service.httpx.post", fake_post)
    monkeypatch.setattr("services.stt.stt_service._audio_is_silent", lambda b: False)
    out = service_with_endpoint.transcribe(b"x" * 100, prompt="Namen: Ed", timeout=600, filename="seg_000.ogg")
    assert out == "hallo"
    assert captured["timeout"] == 600
    assert captured["data"]["prompt"] == "Namen: Ed"
    assert captured["files"]["file"][0] == "seg_000.ogg" and captured["files"]["file"][2] == "audio/ogg"


def test_api_transcribe_default_kwargs_unchanged(monkeypatch, service_with_endpoint):
    # no prompt → no "prompt" key; timeout 60; audio.webm
    ...


def test_local_transcribe_passes_initial_prompt(monkeypatch, service_local):
    # fake whisper model whose .transcribe records kwargs; assert kwargs["initial_prompt"] == "Namen: Ed"
    # and the temp path suffix is ".ogg"
    ...
```

Write the two `...` tests fully (assert the negative case has no `prompt` key and `timeout == 60`; assert `initial_prompt` and suffix for local).

- [ ] **Step 2: Run tests, expect failures** (`ImportError`/`AttributeError`/`TypeError`).
- [ ] **Step 3: Implement** constants, limits, model, STT kwargs exactly per Interfaces. Keep existing STT behaviour byte-identical when kwargs are omitted.
- [ ] **Step 4: Run** the two new test files plus `tests/test_stt_transcribe_error_detail.py tests/test_stt_leak.py tests/test_upload_limits_centralized.py` → all pass.
- [ ] **Step 5: Commit** `feat(meetings): foundation — Meeting model, audio dir/limits, STT prompt/timeout kwargs`.

---

### Task 2: Pipeline pure functions — prompts, condensation, template, split

**Files:**
- Create: `src/meeting_minutes.py` (part 1; Task 3 appends the job runner)
- Test: `tests/test_meeting_minutes.py`

**Interfaces (Produces; Task 3/4 consume):**

```python
# src/meeting_minutes.py
SEGMENT_SECONDS = 600
CONDENSE_NORM = 5000
CONDENSE_CAP = 70000
REQUIRED_HEADINGS = ("## Samenvatting", "## Besproken punten", "## Besluiten", "## Actiepunten", "## Volgende vergadering")

def build_stt_prompt(key_terms: str | None) -> str
def condense_split_for_depth(depth: int) -> int          # min(CONDENSE_CAP, int(CONDENSE_NORM + CONDENSE_NORM * depth / 2))
async def correct_transcript(text: str, call) -> str      # call(messages) -> str; on exception returns text unchanged
async def condense_transcript(text: str, call, *, depth: int = 0, carry: str = "", on_depth=None) -> str
def minutes_system_prompt() -> str
def minutes_user_message(*, condensed: str, title: str, agenda: str | None, date_str: str, duration_str: str) -> dict
def validate_minutes(md: str) -> list[str]                # missing/misordered headings, [] when valid
async def build_minutes(condensed: str, *, title: str, agenda: str | None, date_str: str, duration_str: str, call) -> tuple[str, bool]   # (markdown, valid)
def render_minutes_document(minutes_md: str, transcript: str) -> str
def format_duration(seconds: int | None) -> str           # "1 u 23 min" / "12 min" / "onbekend"
def split_audio(src: Path, workdir: Path, *, segment_seconds: int = SEGMENT_SECONDS, run=subprocess.run) -> list[Path]
```

`call` is an `async def call(messages: list[dict]) -> str` injected by the job runner (Task 3 wraps `task_llm_call_async`). Keeping `call` injectable is what makes these functions unit-testable without an LLM.

**Prompts (verbatim, from Ed's Notulist code; each system prompt = the text below + `"\n\n" + DUTCH_OUTPUT_RULE`):**

- `STT_PROMPT_BASE = "Maak een foutloze transcriptie van het audio bestand."`; with key terms append `" In het audiobestand worden de volgende afkortingen, namen, jargon, gebruikt: " + key_terms.strip()`.
- `CORRECT_SYSTEM = "Jij bent een behulpzame assistent. Jouw taak is het corrigeren van spelling en grammatica fouten in een transcriptie tekst. Voeg enkel noodzakelijke punctuatie toe, zoals punten, kommas, en voor de leesbaarheid deel je de tekst op in alinea's. Zorg ervoor dat de tekst goed leesbaar is en dat de zinnen logisch zijn opgebouwd. Gebruik enkel de context die je hebt gekregen. Antwoord altijd in het Nederlands en geef alleen de gecorrigeerde tekst terug, zonder inleiding of toelichting."`
- `CONDENSE_FINAL_SYSTEM = "Jij bent een zeer goede, door AI getrainde, assistent die expert is in het begrijpen en samenvatten van natuurlijke taal. Lees de volgende transcript door en maak daar uitgebreide notulen van in juiste abstracte paragrafen. Het doel is om de belangrijkste gesprekspunten, besluiten, en afgesproken acties in de notulen te vatten, zodat je een coherente en leesbaar verslag oplevert, die een persoon kan helpen om de hoofdpunten, besluiten en acties van het overleg te begrijpen zonder het volledige transcript te moeten lezen. Vermijd onnodige details, herhalingen, of niet ter zake doende punten. Gebruik voor de samenvatting de actieve, tegenwoordige tijd."`
- `CONDENSE_PART_SYSTEM = "Jij bent een zeer goede, door AI getrainde, assistent die expert is in het begrijpen en samenvatten van natuurlijke taal. De volgende tekst is een deel van een volledig transcript afkomstig van een opgenomen overleg. Lees de tekst van dit deel door en maak daar uitgebreide notulen van in juiste abstracte paragrafen zoals Opening en mededelingen, hoofdpunten, besluiten, en actielijst, volgende vergadering. Het doel is om de belangrijkste gesprekspunten in de notulen te vatten, zodat je een coherente en leesbaar verslag oplevert, die een persoon kan helpen om de hoofdpunten, de besluiten, de actielijst, enz. van het overleg te begrijpen, zonder het volledige transcript te moeten lezen. Onthoud dat dit een transcriptdeel is en dat mogelijk pas in het volgende deel zaken als de rondvraag, afsluiting en conclusie worden behandeld indien deze niet in dit deel voorkomen. Vermijd onnodige details, herhalingen, of niet ter zake doende punten. Gebruik voor de samenvatting de actieve, tegenwoordige tijd."`
- `MINUTES_SYSTEM` (new, strict template):

```
Je bent een ervaren notulist. Je krijgt een samenvatting (of transcript) van een vergadering en schrijft daar formele notulen van, EXACT volgens dit Markdown-sjabloon. Gebruik precies deze koppen, in deze volgorde, en voeg geen andere koppen toe. Ontbreekt informatie voor een onderdeel, schrijf dan "Geen." onder die kop. Verzin niets dat niet in de bron staat. Schrijf in de actieve, tegenwoordige tijd.

## Samenvatting
(3-8 zinnen: doel van de vergadering en de belangrijkste uitkomsten)

## Besproken punten
(genummerde lijst; per punt 1-3 zinnen; volg de agenda als die gegeven is)

## Besluiten
(opsommingslijst; elk besluit één regel, concreet geformuleerd)

## Actiepunten
| Actie | Eigenaar | Deadline |
|---|---|---|
(één rij per actie; eigenaar/deadline "-" als onbekend)

## Volgende vergadering
(datum/tijd/onderwerpen als genoemd, anders "Geen.")

Begin je antwoord direct met "## Samenvatting". Geen inleiding, geen titel, geen afsluiting.
```

`minutes_user_message` builds `untrusted_context_message("vergadering: " + title, condensed)` and prefixes the content with `f"Titel: {title}\nDatum: {date_str}\nDuur: {duration_str}\n"` plus, when agenda, `"Agenda:\n" + agenda + "\n\n"`; then `"Bron:\n"` + condensed. (Check `untrusted_context_message`'s signature in `src/prompt_security.py` and use it as `notebook_audio.py` does — you may build the string first and wrap once.)

**`condense_transcript` algorithm (Ed's recursion, exact):**

```python
async def condense_transcript(text, call, *, depth=0, carry="", on_depth=None):
    work = (carry.strip() + "\n\n" + text.strip()).strip() if carry else text.strip()
    if not work:
        return ""
    if on_depth:
        on_depth(depth)
    split = condense_split_for_depth(depth)
    if len(work) <= split:
        return (await call(_messages(CONDENSE_FINAL_SYSTEM, work))).strip()
    head, tail = work[:split], work[split:]
    partial = (await call(_messages(CONDENSE_PART_SYSTEM, head))).strip()
    return await condense_transcript(tail, call, depth=depth + 1, carry=partial, on_depth=on_depth)
```

`_messages(system, user_text)` → `[{"role":"system","content": f"{UNTRUSTED_CONTEXT_POLICY}\n\n{system}\n\n{DUTCH_OUTPUT_RULE}"}, untrusted_context_message("transcript", user_text)]`.

**`build_minutes`:** first call with `minutes_user_message`; `errors = validate_minutes(md)`; if errors: second call with the same messages plus `{"role":"assistant","content": md}` and `{"role":"user","content": "Je antwoord volgt het sjabloon niet: " + "; ".join(errors) + ". Geef de volledige notulen opnieuw, exact volgens het sjabloon, beginnend met '## Samenvatting'."}`; return `(best, valid)` where best = second if valid else first, valid = whether the returned one validates.

**`validate_minutes`:** for each heading in `REQUIRED_HEADINGS` find its index at a line start (`re.search(rf"^{re.escape(h)}\s*$", md, re.M)`); missing → `"ontbreekt: <h>"`; indices not strictly increasing → `"volgorde: <h>"`; also require `"| Actie | Eigenaar | Deadline |"` present (case-insensitive header line) → `"ontbreekt: actiepuntentabel"`.

**`render_minutes_document(minutes_md, transcript)`** → `minutes_md.rstrip() + "\n\n## Bijlage: transcript\n\n" + transcript.strip() + "\n"`. The job runner (Task 3) prepends the header block `# Notulen: {title}\n\n**Datum:** {date}  ·  **Duur:** {dur}  ·  **Opname:** Ithaka\n\n` and the optional `## Agenda\n\n{agenda}\n\n` — put that in `render_minutes_header(*, title, date_str, duration_str, agenda) -> str` here too (export it).

**`split_audio`:** `ffmpeg = shutil.which("ffmpeg")`; None → `RuntimeError("ffmpeg niet gevonden")`. `cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-c:a", "libopus", "-b:a", "32k", "-f", "segment", "-segment_time", str(segment_seconds), "-reset_timestamps", "1", str(workdir / "seg_%03d.ogg")]`; `run(cmd, capture_output=True, text=True, timeout=900)`; non-zero → `RuntimeError("Audio kon niet worden gesplitst: " + stderr[-400:])`; `segs = sorted(workdir.glob("seg_*.ogg"))`; empty → `RuntimeError("Audio kon niet worden gesplitst: geen segmenten")`; return segs.

- [ ] **Step 1: Tests** in `tests/test_meeting_minutes.py` — at minimum:
  - `test_build_stt_prompt_with_and_without_terms`
  - `test_condense_split_growth`: depths 0,1,2,4 → 5000, 7500, 10000, 15000; depth 100 → 70000.
  - `test_condense_short_single_final_call`: fake `call` records system prompts; text 100 chars → exactly one call whose system contains `CONDENSE_FINAL_SYSTEM` and `DUTCH_OUTPUT_RULE`; returns stripped reply.
  - `test_condense_long_recurses_with_carry`: text of 12000 "a" chars, fake call returns `"S<n>"` per call; assert calls = [part(5000 chars), part(...), final]; the second call's user content starts with `"S1"` (carry prepended); `on_depth` saw `[0, 1, 2]`.
  - `test_condense_empty_returns_empty_without_calls`.
  - `test_correct_transcript_falls_back_on_error`.
  - `test_validate_minutes_ok / missing / order / table`.
  - `test_build_minutes_retries_once_then_returns_best` (first reply invalid, second valid → valid True; both invalid → returns first, False).
  - `test_render_minutes_document_and_header`.
  - `test_format_duration` (None→"onbekend", 65→"1 min", 5000→"1 u 23 min").
  - `test_split_audio_builds_ffmpeg_cmd_and_returns_sorted` (fake `run` creates `seg_001.ogg`, `seg_000.ogg`; `shutil.which` patched) and `_raises_on_nonzero` and `_raises_when_ffmpeg_missing`.
- [ ] **Step 2: Run, expect ImportError.**
- [ ] **Step 3: Implement** `src/meeting_minutes.py` part 1 with a module docstring pointing at the spec and Ed's original.
- [ ] **Step 4: Run** `tests/test_meeting_minutes.py` → pass.
- [ ] **Step 5: Commit** `feat(meetings): minutes pipeline — prompts, recursive condensation, template validator, ffmpeg split`.

---

### Task 3: Job runner, Document save, janitor

**Files:**
- Modify: `src/meeting_minutes.py` (append)
- Test: `tests/test_meeting_minutes_job.py`

**Interfaces (Consumes Task 1+2; Produces for Task 4):**

```python
JOB_TIMEOUT_SECONDS = 3600
_JOB_EVICT_AFTER_SECONDS = 1800
_PUBLIC_JOB_FIELDS = ("status", "phase", "segment", "total", "depth", "error", "document_id", "meeting_id", "started_at")
MEETING_AUDIO_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.webm$")

def resolve_meeting_audio_path(filename: str) -> Optional[Path]   # None unless MEETING_AUDIO_RE matches and file exists under MEETING_AUDIO_DIR
def get_job(job_id: str, owner: str) -> Optional[dict]
def get_job_for_meeting(meeting_id: str, owner: str) -> Optional[dict]   # running job for that meeting, else None
def start_processing_job(meeting_id: str, owner: str, db_session_factory=None, *, stt=None, llm_call=None, split=None) -> str
def cleanup_orphaned_meeting_audio(db_session_factory, *, max_age_seconds: int = 3600) -> tuple[int, int]
```

- `stt` default: `from services.stt import get_stt_service; get_stt_service()` — this is the same singleton `app.py` line 744-745 injects into the STT routes, so no extra hook is needed. Provider `disabled|browser` or `stt_enabled False` (read via `stt._load_settings()`) → `RuntimeError("STT niet geconfigureerd")`.
- `llm_call` default: `async def _default_call(messages): return await task_llm_call_async(messages, owner=owner, wait_for_quiet=False, workload="foreground")` (owner bound per job).
- `split` default: `split_audio`.
- Validation in `start_processing_job` (synchronous, before `create_task`): row exists & owner match else `ValueError("Vergadering niet gevonden")`; `audio_path` set and file exists else `ValueError("Geen audio ontvangen")`; STT settings provider `disabled|browser` or `stt_enabled False` → `RuntimeError("STT niet geconfigureerd")`; `get_job_for_meeting` not None → `ValueError("Verwerking loopt al")`. Then set row `status="processing", phase="splitting", error=None`, commit; register entry `{status:"running", phase:"splitting", segment:0, total:0, depth:0, error:None, document_id:None, meeting_id, owner, started_at: time.time(), completed_at: None}`; `entry["task"] = asyncio.create_task(_run(...))`.

**`_run_job(job_id, meeting_id, owner, factory, stt, call, split)`** wrapped in `asyncio.wait_for(..., JOB_TIMEOUT_SECONDS)`; phases update `entry["phase"]` and the DB row's `phase` (helper `_set_phase(factory, meeting_id, entry, phase, **fields)`):
1. `splitting`: `workdir = Path(MEETING_AUDIO_DIR) / f".meetingjob-{meeting_id}"`, mkdir; `segs = await asyncio.to_thread(split, src, workdir)`; `entry["total"] = len(segs)`.
2. `transcribing`: `sem = asyncio.Semaphore(3)`; per segment `await asyncio.to_thread(stt.transcribe, seg.read_bytes(), prompt=build_stt_prompt(key_terms), timeout=600, filename=seg.name)`; `None` → `RuntimeError(f"Transcriptie mislukt (segment {i+1}/{n}): {getattr(stt,'last_error',None) or 'onbekende fout'}")`; `entry["segment"]` = completed count.
3. `correcting`: `await correct_transcript(text, call)` per non-empty segment under the semaphore; join with `"\n\n"` → `transcript`. Empty transcript overall → `RuntimeError("Geen spraak herkend in de opname")`.
4. `condensing`: `condensed = await condense_transcript(transcript, call, on_depth=lambda d: entry.__setitem__("depth", d))`.
5. `writing`: `minutes, valid = await build_minutes(condensed, title=..., agenda=..., date_str=row.created_at.strftime("%d-%m-%Y"), duration_str=format_duration(row.duration_seconds), call=call)`; log a warning when not valid.
6. `saving`: `content = render_minutes_header(...) + minutes + bijlage` (use `render_minutes_document`); `Document(id=uuid4, owner=owner, title=f"Notulen – {title} – {date_str}", language="markdown", current_content=content, session_id=None)`; commit; row `document_id`, `status="done"`, `phase=None`, `finished_at=now`; entry `status="done"`, `document_id`.
- `except asyncio.CancelledError` → row/entry `status="error"`, `error="Verwerking geannuleerd"`; re-raise. `except Exception as exc` → `status="error"`, `error=str(exc)[:500]`; log. `finally`: `shutil.rmtree(workdir, ignore_errors=True)`; `entry["completed_at"] = time.time()`.

**`cleanup_orphaned_meeting_audio`:** mirror `notebook_audio.cleanup_orphaned_audio` (read it): candidates = `.meetingjob-*` dirs and `MEETING_AUDIO_RE` files older than `max_age_seconds` (mtime); load all `Meeting.audio_path` in one query; remove files whose name is not referenced and every stale workdir (`shutil.rmtree`); per-item try/except; return `(count, bytes)`.

- [ ] **Step 1: Tests** `tests/test_meeting_minutes_job.py` using `make_temp_sqlite` + a temp `MEETING_AUDIO_DIR` (monkeypatch `src.meeting_minutes.MEETING_AUDIO_DIR`), fake `stt` object (`transcribe(...)` returns `"seg tekst"`, `last_error`), fake async `llm_call` (returns valid template text for MINUTES prompts, `"S"` otherwise), fake `split` that writes two `seg_00x.ogg` files. Use `asyncio.run`/pytest asyncio auto mode; await `entry["task"]` via `src.meeting_minutes._active_jobs[job_id]["task"]`.
  - happy path → row `done`, Document exists with title prefix `Notulen – `, content contains `## Bijlage: transcript` and `## Actiepunten`, workdir removed, `get_job` public fields only (no `owner`/`task`).
  - stt returns None → status error, message contains `Transcriptie mislukt (segment 1/2)`.
  - all segments empty → error `Geen spraak herkend`.
  - validation errors: unknown meeting, other owner, missing audio, STT disabled (monkeypatch `stt._load_settings`), duplicate start.
  - `resolve_meeting_audio_path` rejects `../x.webm`, `x.webm`, accepts a real uuid file.
  - `cleanup_orphaned_meeting_audio` removes old orphan + old workdir, keeps referenced/young files.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/test_meeting_minutes.py tests/test_meeting_minutes_job.py tests/test_notebooks_gate_seam.py` → pass.
- [ ] **Step 5: Commit** `feat(meetings): async processing job, Document save, audio janitor`.

---

### Task 4: Routes, app wiring, passive poll

**Files:**
- Create: `routes/meeting_routes.py`
- Modify: `app.py` (router include next to the notebook router; janitor loop after the illustrations janitor ~line 1290)
- Modify: `src/interactive_gate.py` (`_PASSIVE_PATTERNS`)
- Test: `tests/test_routes_meetings.py`, extend `tests/test_interactive_gate*.py` (find the existing file with `grep -ln "_PASSIVE_PATTERNS\|should_track_interactive_request" tests/*.py`)

**Interfaces:**
`setup_meeting_routes(get_current_user, SessionLocal) -> APIRouter` — look at how `setup_notebook_routes` is called in `app.py` and mirror its dependency style (`get_current_user` is the auth dependency function used by other routers; `SessionLocal` from `core.database`).

Route behaviour per spec table (all owner-scoped; `user = get_current_user(request)` pattern as in `routes/notebook_routes.py`):
- `POST /api/meetings` JSON `{title, agenda?, key_terms?}` — `title.strip()` empty → 400 `"Titel is verplicht"`; title ≤ 200 chars, agenda/key_terms ≤ 20000 chars (413 otherwise); create `Meeting(id=uuid4, owner=user, title, agenda, key_terms, status="recording", audio_path=f"{id}.webm")`; return `_serialize(row, job=None)`.
- `POST /api/meetings/{id}/chunks?seq=N` — `seq: int` query; `file: UploadFile`; row 404 `"Vergadering niet gevonden"`; status ≠ recording → 400 `"Opname is al afgesloten"`; expected seq stored on the row? Use `bytes_total`-independent counter: add a module-level `_expected_seq: dict[str, int]` is lost on restart — instead derive: store `chunk_count` ... **Ruling:** add column? No — keep it simple: the route keeps `_next_seq: dict[meeting_id, int]` in memory, initialised to 0 on create; a chunk with `seq != _next_seq.get(id, 0)` → 409 `{"detail": "Onverwacht chunknummer", "expected": n}`. (Restart mid-recording loses the counter; client then gets 409 and shows the error — acceptable, spec'd.) Read with `read_upload_limited(file, MEETING_CHUNK_MAX_BYTES, "Audio chunk")`; empty → 400; `bytes_total + len > MEETING_AUDIO_MAX_BYTES` → 413 `"Opname te groot"`; append with `open(path, "ab")`; update `bytes_total`; return `{"seq": seq, "bytes_total": n}`.
- `POST /api/meetings/{id}/finish` JSON `{duration_seconds?}` — status in `recording|error|done` else 400 `"Verwerking loopt al"`; set `duration_seconds` when given (int ≥ 0); `start_processing_job(id, user)` → `ValueError/RuntimeError` → 400 with str; return `{"job_id", "status": "processing"}`.
- `GET /api/meetings` — rows of owner ordered `created_at desc` limit 200 → `{"meetings": [_serialize(row, get_job_for_meeting(row.id, user))]}`.
- `GET /api/meetings/{id}` — `_serialize(row, job)`; when `row.status == "processing"` and job is None → present `status: "error"`, `error: "Verwerking onderbroken (herstart) — gebruik Reprocess"` (do not mutate the row).
- `GET /api/meetings/{id}/audio` — `resolve_meeting_audio_path(row.audio_path)`; None → 404; `FileResponse(path, media_type="audio/webm", filename=f"{safe_title}.webm")`.
- `DELETE /api/meetings/{id}` — running job → 409 `"Verwerking loopt nog"`; unlink audio (missing_ok); delete row; `{"ok": true}`.
- `_serialize(row, job)` → `{id, title, agenda, key_terms, status, phase, error, bytes_total, duration_seconds, document_id, created_at (iso), finished_at (iso|None), segment, total, depth}` with job fields overriding `phase/segment/total/depth` while running.

`app.py`: `from routes.meeting_routes import setup_meeting_routes`; `app.include_router(setup_meeting_routes(...))`; janitor loop `_meeting_audio_janitor_loop` copied from the podcast one, calling `cleanup_orphaned_meeting_audio`.

`src/interactive_gate.py`: append `re.compile(r"^/api/meetings/[^/]+$")` with a comment in the same style (GET detail poll every 3 s by `static/js/meetings.js`).

- [ ] **Step 1: Tests** `tests/test_routes_meetings.py` — copy the fixture style of `tests/test_routes_notebook_audio.py` (file-backed temp sqlite, `FastAPI()` + `include_router`, `TestClient`, fake auth dependency returning `"ed"` / `"bob"`), monkeypatch `routes.meeting_routes.start_processing_job` / `get_job_for_meeting` / `MEETING_AUDIO_DIR`. Cover: create (+400 empty title), chunks in order → bytes_total grows and file content equals concatenation; seq skip → 409 with `expected`; chunk over limit → 413; total over limit → 413 (monkeypatch `MEETING_AUDIO_MAX_BYTES` small); other owner → 404; finish without audio → 400 `Geen audio ontvangen` (real `start_processing_job` path: don't patch it for this one); finish happy (patched) → `processing`; list/detail shapes incl. interrupted-processing presentation; audio serve 200 + traversal-style ids 404; delete 409 while job running, 200 otherwise removes file. Gate test: `should_track_interactive_request("/api/meetings/abc", "GET") is False`, `("/api/meetings/abc", "POST") is True`, `("/api/meetings", "GET") is True`, `("/api/meetings/abc/chunks", "POST") is True`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** routes, wiring, gate pattern.
- [ ] **Step 4: Run** the new tests + `tests/test_interactive_gate*.py` + `.venv/bin/python -m py_compile app.py routes/meeting_routes.py`; also start the app briefly to prove import wiring: `ITHAKA_DATA_DIR=$TMPDIR/mt .venv/bin/python -c "import app"` (must not raise).
- [ ] **Step 5: Commit** `feat(meetings): REST routes, app wiring, passive status poll, janitor loop`.

---

### Task 5: Frontend — panel, recorder, chunk uploader

**Files:**
- Create: `static/js/meetings.js`
- Modify: `static/index.html` (rail button between `rail-archive` and `rail-memory`; sidebar `list-item#tool-meetings-btn` after `tool-notes-btn`'s neighbour Library; `<script type="module" src="/static/js/meetings.js">` next to `notes.js`'s tag — find it with `grep -n "notes.js" static/index.html`)
- Modify: `static/app.js` (import `meetingsModule` like `notesModule`; `_railToolMap` entry; tooltip map entry `'rail-meetings': 'Meetings'`; sidebar click → `meetingsModule.togglePanel()`), `static/js/modalManager.js` (~line 1413 map: `'meetings-panel': { rail: 'rail-meetings', sidebar: 'tool-meetings-btn' }`), `static/js/keyboard-shortcuts.js` (~line 102 map entry `'meetings-panel': 'tool-meetings-btn'` — only if that map is required for chip restore; read the file header comment to decide)
- Test: `tests/test_meetings_js.py` (node, pattern of `tests/test_realtime_voice_js.py`)

**Interfaces (Consumes Task 4 API):**

```js
// static/js/meetings.js — pure exports (no DOM at import time!)
export function formatElapsed(ms)                 // "00:00", "01:05", "1:02:03"
export function meetingStatusLabel(m)             // recording→"Recording", processing→ phase map: splitting "Splitting audio", transcribing `Transcribing ${segment}/${total}`, correcting `Correcting ${segment}/${total}`, condensing `Condensing (depth ${depth})`, writing "Writing minutes", saving "Saving"; done→"Done"; error→`Error: ${m.error}`
export function createChunkUploader({ post, maxAttempts = 3, delays = [1000, 2000, 4000], onStatus })
  // returns { enqueue(blob), drain(): Promise<{uploaded, failed}>, stats() }
  // sequential: one in flight; seq increments per enqueue; post(seq, blob) -> Promise resolving on 2xx, rejecting otherwise;
  // on reject retry up to maxAttempts with delays[i] (use injectable `sleep` option defaulting to setTimeout);
  // after final failure: failed += 1, stop sending further chunks (a gap is fatal — server would 409), keep them counted as failed;
  // onStatus({uploadedBytes, uploadedChunks, failedChunks, pending}) after each attempt.
export function openPanel(), closePanel(direction), togglePanel(), isPanelOpen()
export const MEETING_MAX_MS = 3 * 60 * 60 * 1000
```

`sleep` must be injectable for tests (`createChunkUploader({ post, sleep: async () => {} })`).

**Panel (mirror `notes.js` openPanel shape, simplified):** pane `id="meetings-pane" class="notes-pane meetings-pane"`, header with mic SVG + "Meetings" + minimize button (`.modal-minimize-btn`), body:
1. Form card: `<input id="meeting-title" class="memory-search-input" placeholder="Title">`, `<textarea id="meeting-agenda" placeholder="Agenda (optional)">`, `<input id="meeting-terms" placeholder="Key terms: names, acronyms (optional)">`, button `#meeting-record-btn` (`.memory-toolbar-btn`) "Start recording" ↔ "Stop" (add class `danger` while recording), `<span id="meeting-timer">00:00</span>` with a small `<span class="meeting-rec-dot">` (inline style `background: var(--red)`, 8 px circle, only while recording), `<div id="meeting-upload-status">` text ("Saved up to 01:30" / "2 chunks not saved" in `var(--red)`).
2. List `#meeting-list`: per meeting a `.list-item`-style row: title, date, status label, buttons: "Open minutes" (when `document_id`; calls `window.documentModule?.loadDocument(id)` — fall back to `import('/static/js/document.js').then(m => m.default?.loadDocument ? m.default.loadDocument(id) : m.loadDocument(id))` as `notebookWorkspace.js` ~line 1675 does; read that helper and reuse its approach), "Audio" (`<a href="/api/meetings/{id}/audio" download>`), "Reprocess" (status error/done, and not recording), "Delete" (`confirm()` is forbidden in this codebase? check how notes.js deletes — mirror it; if it uses a custom confirm, use that; otherwise delete without confirm but show a toast with the title).

**Recording flow:**
- Start: `POST /api/meetings` → id; `navigator.mediaDevices.getUserMedia({audio:true})`; `new MediaRecorder(stream, {mimeType:'audio/webm'})`; uploader = `createChunkUploader({ post: (seq, blob) => fetch(`/api/meetings/${id}/chunks?seq=${seq}`, {method:'POST', body: form}).then(r => { if (!r.ok) throw new Error(r.status); }) , onStatus })`; `ondataavailable` → `uploader.enqueue(e.data)` when `size > 0`; `start(30000)`; timer interval 500 ms; `window.addEventListener('beforeunload', _warn)`; auto-stop at `MEETING_MAX_MS`.
- Stop: `mediaRecorder.stop()` → `onstop`: tracks stop; `await uploader.drain()`; `POST /api/meetings/${id}/finish` with `{duration_seconds}`; if `failed > 0` show toast "Recording may be incomplete (N chunks not saved)" but still finish; start polling.
- Polling: every 3 s `GET /api/meetings/{id}` for each meeting with status `processing`; stop when done/error; re-render row. On panel open: `GET /api/meetings` and resume polling for processing rows.
- Toasts via the existing `uiModule.showToast` pattern — see how `notes.js` shows toasts and reuse.
- Mobile ≤ 768 px: same inline bottom-sheet styles as notes; swipe-dismiss is optional — do not copy `_wireNotesSwipeDismiss`; the minimize button suffices.

- [ ] **Step 1: Node tests** `tests/test_meetings_js.py`: `formatElapsed` cases; `meetingStatusLabel` for each phase; `createChunkUploader`: (a) 3 enqueues → post called with seq 0,1,2 in order, one at a time (assert no overlap by tracking `inFlight` max = 1); (b) first attempt of seq 1 rejects then resolves → posted twice, `failed 0`, `sleep` called once with 1000; (c) always-rejecting seq 1 → after 3 attempts `failed === 1` and seq 2 never posted; `drain()` resolves `{uploaded: 1, failed: 1}` (pending chunks count as failed); (d) `onStatus` receives `uploadedBytes` sum.
  Importing the module in node must not touch `document`/`window` at top level — guard DOM access inside functions.
- [ ] **Step 2: Run, expect module-not-found.**
- [ ] **Step 3: Implement** module, HTML, app.js wiring. `node --check static/js/meetings.js static/app.js`.
- [ ] **Step 4: Run** `tests/test_meetings_js.py` and any existing tests that parse `index.html`/rail ids (`grep -ln "rail-notes" tests/*.py`) → pass.
- [ ] **Step 5: Commit** `feat(meetings): Meetings panel — mic recording, chunked upload, status list, open minutes`.

---

### Task 6 (controller): docs + smoke + PR

Handled by the controller, not an implementer: CLAUDE.md line, session log, `.venv/bin/python tests/run_focus.py --area routes --fast` + services slice, smoke on :7001 (desktop + 360 px, real OpenAI STT + task model) with screenshots, issue + PR (template), merge after visible smoke, deploy via worktree recipe, live check.
