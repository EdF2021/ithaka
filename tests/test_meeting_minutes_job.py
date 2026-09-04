"""src/meeting_minutes.py (Task 3) — async job runner, Document save, janitor.

Hermetic: no real STT/LLM/ffmpeg. `stt`/`llm_call`/`split` are injected fakes
(the exact seam start_processing_job exposes for this purpose); the DB is a
file-backed temp sqlite (tests.helpers.sqlite_db); MEETING_AUDIO_DIR is
monkeypatched to a temp directory. asyncio_mode = "auto" (pytest.ini), so
async def tests need no marker.
"""
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

    # STT call kwargs, exactly as the brief specifies them.
    assert len(stt.calls) == 2
    assert [c["timeout"] for c in stt.calls] == [600, 600]
    assert [c["prompt"] for c in stt.calls] == [mm.build_stt_prompt(None)] * 2
    assert [c["filename"] for c in stt.calls] == ["seg_000.ogg", "seg_001.ogg"]

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


def test_cleanup_orphaned_meeting_audio_empty_dir_is_noop(tmp_path, monkeypatch):
    d = tmp_path / "empty_meeting_audio"
    d.mkdir()
    monkeypatch.setattr(mm, "MEETING_AUDIO_DIR", str(d))
    removed, freed = mm.cleanup_orphaned_meeting_audio(_SessionLocal, max_age_seconds=3600)
    assert (removed, freed) == (0, 0)
