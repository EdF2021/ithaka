"""src/meeting_minutes.py (Task 3) — async job runner, Document save, janitor.

Hermetic: no real STT/LLM/ffmpeg. `stt`/`llm_call`/`split` are injected fakes
(the exact seam start_processing_job exposes for this purpose); the DB is a
file-backed temp sqlite (tests.helpers.sqlite_db); MEETING_AUDIO_DIR is
monkeypatched to a temp directory. asyncio_mode = "auto" (pytest.ini), so
async def tests need no marker.
"""
import asyncio
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import core.database as db
import src.meeting_minutes as mm
from tests.helpers.sqlite_db import make_temp_sqlite

_SessionLocal, _ENGINE, _TMPDB = make_temp_sqlite(db.Base.metadata)


# ── fixtures ──

@pytest.fixture(autouse=True)
def _clean_active_jobs():
    """Never let a job entry from one test leak state (or a pending task) into the next."""
    mm._active_jobs.clear()
    yield
    for entry in list(mm._active_jobs.values()):
        task = entry.get("task")
        if task is not None and not task.done():
            task.cancel()
    mm._active_jobs.clear()


@pytest.fixture()
def audio_dir(tmp_path, monkeypatch):
    d = tmp_path / "meeting_audio"
    d.mkdir()
    monkeypatch.setattr(mm, "MEETING_AUDIO_DIR", str(d))
    return d


def make_meeting(owner="ed", title="Sprintoverleg", agenda="Punt 1\nPunt 2",
                  key_terms=None, audio_path=None, status="recording",
                  duration_seconds=125, created_at=None):
    session = _SessionLocal()
    try:
        row = db.Meeting(
            id=str(uuid.uuid4()),
            owner=owner,
            title=title,
            agenda=agenda,
            key_terms=key_terms,
            status=status,
            audio_path=audio_path,
            duration_seconds=duration_seconds,
        )
        session.add(row)
        session.commit()
        if created_at is not None:
            row.created_at = created_at
            session.commit()
        meeting_id = row.id
    finally:
        session.close()
    return meeting_id


def get_meeting(meeting_id):
    session = _SessionLocal()
    try:
        return session.query(db.Meeting).filter(db.Meeting.id == meeting_id).first()
    finally:
        session.close()


def write_audio_file(audio_dir: Path, name: str = None) -> str:
    name = name or (str(uuid.uuid4()) + ".webm")
    (audio_dir / name).write_bytes(b"fake-webm-bytes")
    return name


class FakeSTT:
    """Fake STT: returns `reply` for every segment (or via `replies`, one per call index)."""

    def __init__(self, reply="seg tekst", replies=None):
        self.reply = reply
        self.replies = replies
        self.last_error = "onbekende fout"
        self.calls = []

    def transcribe(self, audio_bytes, *, prompt=None, timeout=60.0, filename="audio.webm"):
        idx = len(self.calls)
        self.calls.append({"filename": filename, "prompt": prompt, "timeout": timeout})
        if self.replies is not None:
            return self.replies[idx]
        return self.reply

    def _load_settings(self):
        return {"stt_enabled": True, "stt_provider": "local"}


class DisabledSTT(FakeSTT):
    def _load_settings(self):
        return {"stt_enabled": False, "stt_provider": "local"}


class BrowserSTT(FakeSTT):
    def _load_settings(self):
        return {"stt_enabled": True, "stt_provider": "browser"}


MINUTES_TEMPLATE = """## Samenvatting
Het team bespreekt de voortgang.

## Besproken punten
1. Punt een.

## Besluiten
- Besluit een.

## Actiepunten
| Actie | Eigenaar | Deadline |
|---|---|---|
| Iets doen | Ed | vrijdag |

## Volgende vergadering
Geen.
"""


class FakeCall:
    """Valid minutes template for the minutes system prompt; "S" for every other call."""

    def __init__(self):
        self.calls = []

    async def __call__(self, messages):
        self.calls.append(messages)
        system = messages[0]["content"]
        if mm.MINUTES_SYSTEM in system:
            return MINUTES_TEMPLATE
        return "S"


def fake_split_factory(n=2):
    """Returns a `split(src, workdir) -> list[Path]` that writes n segment files."""

    def _split(src, workdir):
        segs = []
        for i in range(n):
            p = workdir / f"seg_{i:03d}.ogg"
            p.write_bytes(b"fake-audio")
            segs.append(p)
        return segs

    return _split


async def run_to_completion(job_id):
    entry = mm._active_jobs[job_id]
    await entry["task"]


def start(meeting_id, owner="ed", stt=None, llm_call=None, split=None):
    return mm.start_processing_job(
        meeting_id, owner, _SessionLocal,
        stt=stt or FakeSTT(), llm_call=llm_call or FakeCall(), split=split or fake_split_factory(),
    )


# ── happy path ──

async def test_happy_path_produces_done_meeting_and_document(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name, title="Sprintoverleg")

    stt = FakeSTT()
    job_id = start(meeting_id, stt=stt)
    await run_to_completion(job_id)

    # STT call kwargs, exactly as the brief specifies them. Call *order* is
    # not asserted here: fix-wave-1 item 4 moved seg.read_bytes() onto a
    # worker thread (asyncio.to_thread), so which segment's transcribe()
    # actually fires first is real thread-scheduling, not deterministic --
    # segment *result* order is what's guaranteed (asyncio.gather preserves
    # input order regardless of completion order; see
    # test_segment_order_preserved_when_first_segment_finishes_last).
    assert len(stt.calls) == 2
    assert [c["timeout"] for c in stt.calls] == [600, 600]
    assert [c["prompt"] for c in stt.calls] == [mm.build_stt_prompt(None)] * 2
    assert {c["filename"] for c in stt.calls} == {"seg_000.ogg", "seg_001.ogg"}

    row = get_meeting(meeting_id)
    assert row.status == "done"
    assert row.phase is None
    assert row.error is None
    assert row.document_id is not None
    assert row.finished_at is not None

    session = _SessionLocal()
    try:
        doc = session.query(db.Document).filter(db.Document.id == row.document_id).first()
    finally:
        session.close()
    assert doc is not None
    assert doc.title.startswith("Notulen – ")
    assert "## Bijlage: transcript" in doc.current_content
    assert "## Actiepunten" in doc.current_content

    workdir = audio_dir / f".meetingjob-{meeting_id}"
    assert not workdir.exists()

    public = mm.get_job(job_id, "ed")
    assert public is not None
    assert set(public.keys()) == set(mm._PUBLIC_JOB_FIELDS)
    assert "owner" not in public
    assert "task" not in public
    assert public["status"] == "done"
    assert public["document_id"] == row.document_id


async def test_happy_path_with_key_terms_builds_prompt_with_terms(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name, key_terms="PO, OKR")

    stt = FakeSTT()
    job_id = start(meeting_id, stt=stt)
    await run_to_completion(job_id)

    row = get_meeting(meeting_id)
    assert row.status == "done"
    expected_prompt = mm.build_stt_prompt("PO, OKR")
    assert expected_prompt != mm.build_stt_prompt(None)
    assert [c["prompt"] for c in stt.calls] == [expected_prompt] * 2


# ── STT failure ──

async def test_stt_none_reports_indexed_segment_error(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name)

    stt = FakeSTT(reply=None)
    job_id = start(meeting_id, stt=stt)
    await run_to_completion(job_id)

    row = get_meeting(meeting_id)
    assert row.status == "error"
    assert "Transcriptie mislukt (segment 1/2)" in row.error

    public = mm.get_job(job_id, "ed")
    assert public["status"] == "error"
    assert "Transcriptie mislukt (segment 1/2)" in public["error"]


async def test_all_segments_empty_reports_no_speech(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name)

    stt = FakeSTT(reply="")
    job_id = start(meeting_id, stt=stt)
    await run_to_completion(job_id)

    row = get_meeting(meeting_id)
    assert row.status == "error"
    assert "Geen spraak herkend" in row.error


# ── fix-wave-1, item 4: seg.read_bytes() must run off the event loop ──

async def test_segment_read_bytes_runs_in_a_worker_thread(audio_dir, monkeypatch):
    # If `seg.read_bytes()` runs inline in the coroutine (not wrapped in
    # asyncio.to_thread), it fully blocks the single-threaded event loop for
    # its duration -- no other segment's read can overlap it, so max
    # concurrent read_bytes() calls would always be 1 regardless of the
    # semaphore(3). Wrapping it in to_thread lets segments overlap.
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name)

    lock = threading.Lock()
    state = {"current": 0, "max": 0}
    orig_read_bytes = Path.read_bytes

    def slow_read_bytes(self):
        with lock:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        time.sleep(0.05)
        data = orig_read_bytes(self)
        with lock:
            state["current"] -= 1
        return data

    monkeypatch.setattr(Path, "read_bytes", slow_read_bytes)

    stt = FakeSTT()
    job_id = start(meeting_id, stt=stt, split=fake_split_factory(3))
    await run_to_completion(job_id)

    row = get_meeting(meeting_id)
    assert row.status == "done"
    assert state["max"] >= 2


# ── fix-wave-1, item 6(a): semaphore(3) bound on transcription concurrency ──

class _ConcurrencyTrackingSTT:
    def __init__(self):
        self.lock = threading.Lock()
        self.current = 0
        self.max_concurrent = 0

    def transcribe(self, audio_bytes, *, prompt=None, timeout=60.0, filename="audio.webm"):
        with self.lock:
            self.current += 1
            self.max_concurrent = max(self.max_concurrent, self.current)
        time.sleep(0.05)
        with self.lock:
            self.current -= 1
        return f"tekst {filename}"

    def _load_settings(self):
        return {"stt_enabled": True, "stt_provider": "local"}


async def test_transcription_concurrency_bounded_by_semaphore(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name)

    stt = _ConcurrencyTrackingSTT()
    job_id = start(meeting_id, stt=stt, split=fake_split_factory(5))
    await run_to_completion(job_id)

    row = get_meeting(meeting_id)
    assert row.status == "done"
    assert stt.max_concurrent <= 3
    assert stt.max_concurrent >= 2  # genuinely exercised concurrency, not accidentally serial


# ── fix-wave-1, item 6(b): segment order preserved despite out-of-order completion ──

class _OutOfOrderSTT:
    """seg_000 finishes after seg_001 -- asyncio.gather must still return
    results in input order regardless of completion order."""

    def transcribe(self, audio_bytes, *, prompt=None, timeout=60.0, filename="audio.webm"):
        if filename == "seg_000.ogg":
            time.sleep(0.08)
            return "seg0 tekst"
        time.sleep(0.01)
        return "seg1 tekst"

    def _load_settings(self):
        return {"stt_enabled": True, "stt_provider": "local"}


async def test_segment_order_preserved_when_first_segment_finishes_last(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name)

    stt = _OutOfOrderSTT()
    # Correction always fails -> falls back to the raw (unaltered) segment
    # text, so order is checkable directly in the saved transcript.
    job_id = start(meeting_id, stt=stt, llm_call=_RaisingCorrectionCall(), split=fake_split_factory(2))
    await run_to_completion(job_id)

    row = get_meeting(meeting_id)
    assert row.status == "done"
    session = _SessionLocal()
    try:
        doc = session.query(db.Document).filter(db.Document.id == row.document_id).first()
    finally:
        session.close()
    assert doc.current_content.index("seg0 tekst") < doc.current_content.index("seg1 tekst")


# ── fix-wave-1, item 6(g): a raising llm_call for correction still completes ──

class _RaisingCorrectionCall:
    """Raises for CORRECT_SYSTEM messages (-> correct_transcript falls back
    to the raw text); returns a valid template for the minutes call; "S" for
    everything else (condense)."""

    def __init__(self):
        self.calls = []

    async def __call__(self, messages):
        self.calls.append(messages)
        system = messages[0]["content"]
        if mm.CORRECT_SYSTEM in system:
            raise RuntimeError("correction service down")
        if mm.MINUTES_SYSTEM in system:
            return MINUTES_TEMPLATE
        return "S"


async def test_correction_llm_failure_falls_back_to_raw_text_and_completes(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name)

    stt = FakeSTT(reply="ruwe segment tekst")
    job_id = start(meeting_id, stt=stt, llm_call=_RaisingCorrectionCall())
    await run_to_completion(job_id)

    row = get_meeting(meeting_id)
    assert row.status == "done"
    assert row.document_id is not None

    session = _SessionLocal()
    try:
        doc = session.query(db.Document).filter(db.Document.id == row.document_id).first()
    finally:
        session.close()
    assert "ruwe segment tekst" in doc.current_content


# ── fix-wave-1, item 6(c): CancelledError mid-transcription -> status error ──

async def test_cancel_mid_transcription_sets_error_status_and_cleans_workdir(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name)

    event = threading.Event()

    class _BlockingSTT:
        def transcribe(self, audio_bytes, *, prompt=None, timeout=60.0, filename="audio.webm"):
            event.wait(timeout=5)
            return "text"

        def _load_settings(self):
            return {"stt_enabled": True, "stt_provider": "local"}

    job_id = start(meeting_id, stt=_BlockingSTT(), split=fake_split_factory(1))
    entry = mm._active_jobs[job_id]

    for _ in range(200):
        if entry.get("phase") == "transcribing":
            break
        await asyncio.sleep(0.01)
    assert entry.get("phase") == "transcribing"
    # Give the worker thread a moment to actually enter transcribe()/event.wait().
    await asyncio.sleep(0.05)

    entry["task"].cancel()
    # Cancelling a Task awaiting an already-started to_thread() call is only
    # delivered once that thread's call returns -- unblock it so the
    # cancellation can actually propagate instead of hanging the test.
    event.set()

    with pytest.raises(asyncio.CancelledError):
        await entry["task"]

    row = get_meeting(meeting_id)
    assert row.status == "error"
    assert row.error == "Verwerking geannuleerd"
    assert entry["status"] == "error"
    assert entry["error"] == "Verwerking geannuleerd"
    assert entry["completed_at"] is not None

    workdir = audio_dir / f".meetingjob-{meeting_id}"
    assert not workdir.exists()


# ── fix-wave-1, item 6(d): get_job / get_job_for_meeting reject wrong owner ──

async def test_get_job_and_get_job_for_meeting_reject_wrong_owner(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name)

    job_id = start(meeting_id)
    try:
        assert mm.get_job(job_id, "wrong-owner") is None
        assert mm.get_job_for_meeting(meeting_id, "wrong-owner") is None
        assert mm.get_job(job_id, "ed") is not None
        assert mm.get_job_for_meeting(meeting_id, "ed") is not None
    finally:
        await run_to_completion(job_id)


# ── fix-wave-1, item 6(e): Meeting.phase transitions observed across phases ──

async def test_meeting_phase_transitions_observed_across_pipeline(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name)
    observed = set()

    def _record():
        session = _SessionLocal()
        try:
            row = session.query(db.Meeting).filter(db.Meeting.id == meeting_id).first()
            if row is not None:
                observed.add(row.phase)
        finally:
            session.close()

    class _PhaseRecordingSTT(FakeSTT):
        def transcribe(self, *args, **kwargs):
            _record()
            return super().transcribe(*args, **kwargs)

    class _PhaseRecordingCall(FakeCall):
        async def __call__(self, messages):
            _record()
            return await super().__call__(messages)

    job_id = start(meeting_id, stt=_PhaseRecordingSTT(), llm_call=_PhaseRecordingCall())
    await run_to_completion(job_id)

    row = get_meeting(meeting_id)
    assert row.status == "done"
    assert "transcribing" in observed
    assert observed & {"correcting", "condensing", "writing"}


# ── fix-wave-1, item 6(f): _set_phase swallows a DB error ──

def test_set_phase_swallows_db_error_but_still_updates_entry():
    def bad_factory():
        raise RuntimeError("DB down")

    entry = {}
    meeting_id = make_meeting()

    # Must not raise, despite the factory always failing.
    mm._set_phase(bad_factory, meeting_id, entry, "transcribing", segment=2, total=5)

    assert entry["phase"] == "transcribing"
    assert entry["segment"] == 2
    assert entry["total"] == 5
    # The DB write never landed (factory always raises) -- row is untouched.
    row = get_meeting(meeting_id)
    assert row.phase is None


class _FlakySplittingFactory:
    """Fails exactly on its 3rd call -- the "splitting" phase's _set_phase
    DB write (call 1 = start_processing_job's own validation session, call 2
    = _process_job's row-field read) -- everything else succeeds normally.
    Proves a single transient DB hiccup inside _set_phase doesn't kill the
    job."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls == 3:
            raise RuntimeError("DB blip")
        return _SessionLocal()


async def test_job_completes_done_despite_one_set_phase_db_failure(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name)

    job_id = mm.start_processing_job(
        meeting_id, "ed", _FlakySplittingFactory(),
        stt=FakeSTT(), llm_call=FakeCall(), split=fake_split_factory(),
    )
    await run_to_completion(job_id)

    row = get_meeting(meeting_id)
    assert row.status == "done"
    assert row.document_id is not None


# ── validation errors ──

def test_start_unknown_meeting_raises_value_error():
    with pytest.raises(ValueError, match="Vergadering niet gevonden"):
        start("does-not-exist")


def test_start_wrong_owner_raises_value_error(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(owner="ed", audio_path=audio_name)
    with pytest.raises(ValueError, match="Vergadering niet gevonden"):
        start(meeting_id, owner="someone-else")


def test_start_missing_audio_raises_value_error():
    meeting_id = make_meeting(audio_path=None)
    with pytest.raises(ValueError, match="Geen audio ontvangen"):
        start(meeting_id)


def test_start_missing_audio_file_on_disk_raises_value_error(audio_dir):
    meeting_id = make_meeting(audio_path=str(uuid.uuid4()) + ".webm")
    with pytest.raises(ValueError, match="Geen audio ontvangen"):
        start(meeting_id)


def test_start_stt_disabled_raises_runtime_error(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name)
    with pytest.raises(RuntimeError, match="STT niet geconfigureerd"):
        start(meeting_id, stt=DisabledSTT())


def test_start_stt_browser_provider_raises_runtime_error(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name)
    with pytest.raises(RuntimeError, match="STT niet geconfigureerd"):
        start(meeting_id, stt=BrowserSTT())


async def test_start_duplicate_raises_value_error(audio_dir):
    audio_name = write_audio_file(audio_dir)
    meeting_id = make_meeting(audio_path=audio_name)

    job_id = start(meeting_id)
    try:
        with pytest.raises(ValueError, match="Verwerking loopt al"):
            start(meeting_id)
    finally:
        await run_to_completion(job_id)


# ── resolve_meeting_audio_path ──

def test_resolve_meeting_audio_path_rejects_traversal(audio_dir):
    assert mm.resolve_meeting_audio_path("../x.webm") is None


def test_resolve_meeting_audio_path_rejects_non_uuid(audio_dir):
    assert mm.resolve_meeting_audio_path("x.webm") is None


def test_resolve_meeting_audio_path_accepts_real_uuid_file(audio_dir):
    name = write_audio_file(audio_dir)
    resolved = mm.resolve_meeting_audio_path(name)
    assert resolved == audio_dir / name


def test_resolve_meeting_audio_path_none_when_file_absent(audio_dir):
    name = str(uuid.uuid4()) + ".webm"
    assert mm.resolve_meeting_audio_path(name) is None


def test_resolve_meeting_audio_path_rejects_trailing_newline(audio_dir):
    # Pins fullmatch over match/search: with match(), "^...\.webm$" still
    # succeeds against "<uuid>.webm\n" because "$" matches just before a
    # trailing newline, and a Linux filename can contain one.
    name = str(uuid.uuid4()) + ".webm"
    (audio_dir / (name + "\n")).write_bytes(b"x")
    assert mm.resolve_meeting_audio_path(name + "\n") is None


# ── janitor ──

def _set_mtime(path: Path, seconds_ago: int):
    ts = time.time() - seconds_ago
    import os
    os.utime(path, (ts, ts))


def test_cleanup_orphaned_meeting_audio_removes_old_orphan_and_workdir(audio_dir):
    # Old orphan file (no Meeting row references it) -> removed.
    orphan = write_audio_file(audio_dir)
    _set_mtime(audio_dir / orphan, 7200)

    # Old workdir -> removed (with its contents).
    workdir = audio_dir / f".meetingjob-{uuid.uuid4()}"
    workdir.mkdir()
    (workdir / "seg_000.ogg").write_bytes(b"leftover")
    _set_mtime(workdir, 7200)
    _set_mtime(workdir / "seg_000.ogg", 7200)

    # Referenced file, old mtime -> kept (a Meeting row points at it).
    referenced = write_audio_file(audio_dir)
    _set_mtime(audio_dir / referenced, 7200)
    make_meeting(audio_path=referenced)

    # Young orphan file -> kept (not old enough yet).
    young = write_audio_file(audio_dir)

    removed, freed = mm.cleanup_orphaned_meeting_audio(_SessionLocal, max_age_seconds=3600)

    assert removed == 2
    assert freed > 0
    assert not (audio_dir / orphan).exists()
    assert not workdir.exists()
    assert (audio_dir / referenced).exists()
    assert (audio_dir / young).exists()


def test_cleanup_orphaned_meeting_audio_skips_dir_with_running_job(audio_dir):
    # Fix-wave-1, item 5: a workdir whose meeting has a "running" entry in
    # _active_jobs must be skipped regardless of its mtime age -- e.g. a job
    # stuck mid-LLM-call in the condensing phase can go well past
    # max_age_seconds without touching a segment file, and the janitor must
    # not race that job's own `finally: shutil.rmtree(workdir, ...)`.
    meeting_id = str(uuid.uuid4())
    workdir = audio_dir / f".meetingjob-{meeting_id}"
    workdir.mkdir()
    (workdir / "seg_000.ogg").write_bytes(b"leftover")
    _set_mtime(workdir, 7200)
    _set_mtime(workdir / "seg_000.ogg", 7200)

    mm._active_jobs["fake-job-id"] = {"status": "running", "meeting_id": meeting_id}

    removed, freed = mm.cleanup_orphaned_meeting_audio(_SessionLocal, max_age_seconds=3600)

    assert removed == 0
    assert freed == 0
    assert workdir.exists()


def test_cleanup_orphaned_meeting_audio_removes_dir_once_job_no_longer_running(audio_dir):
    # Sibling of the above: once the job entry is gone (or not "running"),
    # the same old workdir is fair game again.
    meeting_id = str(uuid.uuid4())
    workdir = audio_dir / f".meetingjob-{meeting_id}"
    workdir.mkdir()
    (workdir / "seg_000.ogg").write_bytes(b"leftover")
    _set_mtime(workdir, 7200)
    _set_mtime(workdir / "seg_000.ogg", 7200)

    mm._active_jobs["fake-job-id"] = {"status": "done", "meeting_id": meeting_id}

    removed, freed = mm.cleanup_orphaned_meeting_audio(_SessionLocal, max_age_seconds=3600)

    assert removed == 1
    assert freed > 0
    assert not workdir.exists()


def test_cleanup_orphaned_meeting_audio_empty_dir_is_noop(tmp_path, monkeypatch):
    d = tmp_path / "empty_meeting_audio"
    d.mkdir()
    monkeypatch.setattr(mm, "MEETING_AUDIO_DIR", str(d))
    removed, freed = mm.cleanup_orphaned_meeting_audio(_SessionLocal, max_age_seconds=3600)
    assert (removed, freed) == (0, 0)
