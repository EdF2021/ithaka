"""Notebook podcast audio — TTS layer (Fase 3, Task 1) + audio module (Task 3).

Two blocks in one file:

1. The TTS part (`TTSService.synthesize_voice` + `_synthesize_api`
   `response_format` passthrough). Hermetic: `_load_settings` and the
   provider methods are monkeypatched — no network, no Kokoro/GPU, no real DB.
2. `src/notebook_audio.py` — dialogue parsing, turn splitting, WAV concat,
   filename resolving, voice resolution and the async podcast job. Hermetic
   too: a file-backed temp sqlite via `tests.helpers.sqlite_db`, a fake
   `task_llm_call_async`, an injected synthesizer that returns real
   stdlib-generated mini-WAVs, and `NOTEBOOK_AUDIO_DIR` pointed at `tmp_path`.
"""
import pytest

import src.database as db_module
from services.tts.tts_service import TTSService


def _settings(provider, **overrides):
    base = {
        "tts_enabled": True,
        "tts_provider": provider,
        "tts_model": "tts-1",
        "tts_voice": "alloy",
        "tts_speed": "1",
    }
    base.update(overrides)
    return base


class _FakeKokoro:
    def __init__(self, audio=b"RIFF....WAVEfake"):
        self.available = True
        self.calls = []
        self._audio = audio

    def synthesize_raw(self, text, voice):
        self.calls.append((text, voice))
        return self._audio


# ── synthesize_voice: dispatch ──


def test_synthesize_voice_local_dispatches_to_kokoro_with_voice(tmp_path):
    service = TTSService(cache_dir=str(tmp_path))
    fake_kokoro = _FakeKokoro()
    service._load_settings = lambda: _settings("local")
    service._get_kokoro = lambda: fake_kokoro

    result = service.synthesize_voice("hallo daar", "am_michael", use_cache=False)

    assert result == b"RIFF....WAVEfake"
    assert fake_kokoro.calls == [("hallo daar", "am_michael")]


def test_synthesize_voice_endpoint_dispatches_with_voice_and_wav_format(tmp_path):
    service = TTSService(cache_dir=str(tmp_path))
    service._load_settings = lambda: _settings(
        "endpoint:ep1", tts_model="tts-1-hd", tts_speed="1.2"
    )
    calls = []

    def fake_api(text, endpoint_id, model, voice, speed, response_format="mp3"):
        calls.append((text, endpoint_id, model, voice, speed, response_format))
        return b"api-audio-bytes"

    service._synthesize_api = fake_api

    result = service.synthesize_voice("tekst hier", "onyx", use_cache=False)

    assert result == b"api-audio-bytes"
    assert calls == [("tekst hier", "ep1", "tts-1-hd", "onyx", 1.2, "wav")]


# ── synthesize_voice: error paths ──


@pytest.mark.parametrize("provider", ["disabled", "browser"])
def test_synthesize_voice_raises_for_unconfigured_providers(tmp_path, provider):
    service = TTSService(cache_dir=str(tmp_path))
    service._load_settings = lambda: _settings(provider)

    with pytest.raises(RuntimeError):
        service.synthesize_voice("tekst", "alloy")


def test_synthesize_voice_raises_on_none_result(tmp_path):
    service = TTSService(cache_dir=str(tmp_path))
    service._load_settings = lambda: _settings("endpoint:ep1")
    service._synthesize_api = lambda *a, **k: None

    with pytest.raises(RuntimeError):
        service.synthesize_voice("tekst", "alloy", use_cache=False)


def test_synthesize_voice_raises_on_empty_bytes_result(tmp_path):
    service = TTSService(cache_dir=str(tmp_path))
    service._load_settings = lambda: _settings("endpoint:ep1")
    service._synthesize_api = lambda *a, **k: b""

    with pytest.raises(RuntimeError):
        service.synthesize_voice("tekst", "alloy", use_cache=False)


def test_synthesize_voice_raises_when_kokoro_unavailable(tmp_path):
    service = TTSService(cache_dir=str(tmp_path))
    unavailable = _FakeKokoro()
    unavailable.available = False
    service._load_settings = lambda: _settings("local")
    service._get_kokoro = lambda: unavailable

    with pytest.raises(RuntimeError):
        service.synthesize_voice("tekst", "af_heart", use_cache=False)


# ── synthesize_voice: no 5000-char truncation ──


def test_synthesize_voice_does_not_truncate_long_text(tmp_path):
    service = TTSService(cache_dir=str(tmp_path))
    long_text = "x" * 6000
    service._load_settings = lambda: _settings("endpoint:ep1")
    seen = {}

    def fake_api(text, endpoint_id, model, voice, speed, response_format="mp3"):
        seen["len"] = len(text)
        return b"audio"

    service._synthesize_api = fake_api
    service.synthesize_voice(long_text, "alloy", use_cache=False)

    assert seen["len"] == 6000


# ── synthesize_voice: cache ──


def test_synthesize_voice_caches_and_reuses_on_second_call(tmp_path):
    service = TTSService(cache_dir=str(tmp_path))
    service._load_settings = lambda: _settings("endpoint:ep1")
    calls = []

    def fake_api(text, endpoint_id, model, voice, speed, response_format="mp3"):
        calls.append(1)
        return b"cached-bytes"

    service._synthesize_api = fake_api

    first = service.synthesize_voice("cache me", "alloy")
    second = service.synthesize_voice("cache me", "alloy")

    assert first == second == b"cached-bytes"
    assert len(calls) == 1


def test_synthesize_voice_cache_key_distinguishes_voice(tmp_path):
    """Same text, different voice → different cache entries (no cross-voice hit)."""
    service = TTSService(cache_dir=str(tmp_path))
    service._load_settings = lambda: _settings("endpoint:ep1")
    calls = []

    def fake_api(text, endpoint_id, model, voice, speed, response_format="mp3"):
        calls.append(voice)
        return f"audio-{voice}".encode()

    service._synthesize_api = fake_api

    a = service.synthesize_voice("dezelfde tekst", "alloy")
    b = service.synthesize_voice("dezelfde tekst", "onyx")

    assert a == b"audio-alloy"
    assert b == b"audio-onyx"
    assert calls == ["alloy", "onyx"]


# ── existing synthesize() behaviour unchanged ──


def test_synthesize_still_truncates_at_5000_chars(tmp_path):
    service = TTSService(cache_dir=str(tmp_path))
    long_text = "y" * 6000
    service._load_settings = lambda: _settings("endpoint:ep1")
    seen = {}

    def fake_api(text, endpoint_id, model, voice, speed):
        seen["len"] = len(text)
        return b"audio"

    service._synthesize_api = fake_api
    service.synthesize(long_text, use_cache=False)

    assert seen["len"] == 5000


def test_synthesize_disabled_returns_none_not_raise(tmp_path):
    service = TTSService(cache_dir=str(tmp_path))
    service._load_settings = lambda: _settings("disabled")

    assert service.synthesize("tekst", use_cache=False) is None


# ── _synthesize_api: response_format passthrough ──


def test_synthesize_api_default_response_format_is_mp3(monkeypatch, tmp_path):
    """Backward compat: synthesize() calls _synthesize_api without response_format
    and must keep getting mp3 (existing behaviour unchanged)."""
    service = TTSService(cache_dir=str(tmp_path))
    captured = {}

    class FakeEndpoint:
        base_url = "http://fake"
        api_key = None

    class FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return FakeEndpoint()

    class FakeSession:
        def query(self, *a, **k):
            return FakeQuery()

        def close(self):
            pass

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["payload"] = json

        class R:
            content = b"mp3-bytes"

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr("services.tts.tts_service.httpx.post", fake_post)
    monkeypatch.setattr(db_module, "SessionLocal", lambda: FakeSession())

    result = service._synthesize_api("hoi", "ep1", "tts-1", "alloy", 1.0)

    assert result == b"mp3-bytes"
    assert captured["payload"]["response_format"] == "mp3"


def test_synthesize_api_explicit_response_format_wav(monkeypatch, tmp_path):
    service = TTSService(cache_dir=str(tmp_path))
    captured = {}

    class FakeEndpoint:
        base_url = "http://fake"
        api_key = None

    class FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return FakeEndpoint()

    class FakeSession:
        def query(self, *a, **k):
            return FakeQuery()

        def close(self):
            pass

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["payload"] = json

        class R:
            content = b"wav-bytes"

            def raise_for_status(self):
                pass

        return R()

    monkeypatch.setattr("services.tts.tts_service.httpx.post", fake_post)
    monkeypatch.setattr(db_module, "SessionLocal", lambda: FakeSession())

    result = service._synthesize_api(
        "hoi", "ep1", "tts-1", "alloy", 1.0, response_format="wav"
    )

    assert result == b"wav-bytes"
    assert captured["payload"]["response_format"] == "wav"


# ==========================================================================
# Audio module (src/notebook_audio.py) — Fase 3, Task 3
#
# Hermetic by construction:
#   * DB   — a file-backed temp sqlite (tests.helpers.sqlite_db, the suite's
#            documented convention) whose sessionmaker is handed to
#            start_podcast_job as db_session_factory, so core.database's
#            shared SessionLocal is never touched.
#   * LLM  — src.notebook_audio.task_llm_call_async is monkeypatched.
#   * TTS  — the synthesizer hook is injected with a fake that returns real
#            stdlib-generated WAV bytes.
#   * disk — NOTEBOOK_AUDIO_DIR is monkeypatched onto tmp_path.
#   * bus  — fire_event is stubbed (the real one schedules on the loop).
# ==========================================================================
import asyncio
import io
import re
import time
import uuid
import wave

from fastapi import HTTPException

import core.database as cdb
import src.notebook_audio as audio
from tests.helpers.sqlite_db import make_temp_sqlite

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


# ── fixtures / helpers ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_active_jobs():
    """_active_jobs is module-level state; never let it leak between tests."""
    audio._active_jobs.clear()
    yield
    audio._active_jobs.clear()


def _wav(n_frames, nchannels=1, sampwidth=2, framerate=24000, fill=b"\x01\x00"):
    """A real, minimal WAV built with the stdlib (no fixture binaries)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.writeframes(fill[:sampwidth] * n_frames * nchannels)
    return buf.getvalue()


def _wav_info(data):
    with wave.open(io.BytesIO(data), "rb") as w:
        return (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes())


def make_notebook(session, owner="own", name="Testboek"):
    nb = cdb.Notebook(id=str(uuid.uuid4()), owner=owner, name=name)
    session.add(nb)
    session.commit()
    return nb


def make_source(session, notebook, filename="a.txt", content="brontekst",
                status="indexed", with_document=True, owner="own"):
    doc_id = None
    if with_document:
        doc = cdb.Document(id=str(uuid.uuid4()), title=filename, owner=owner,
                           current_content=content)
        session.add(doc)
        session.commit()
        doc_id = doc.id
    src = cdb.NotebookSource(id=str(uuid.uuid4()), notebook_id=notebook.id,
                             document_id=doc_id, filename=filename,
                             status=status, chunk_count=1)
    session.add(src)
    session.commit()
    return src


def _seed_notebook(owner="own", name="Testboek", filename="a.txt",
                   content="brontekst", status="indexed"):
    """Create a notebook plus one source; return the notebook id.

    Returns the id (not the ORM object) on purpose: the session is closed in
    the finally, and a detached Notebook cannot refresh its attributes.
    """
    session = _TS()
    try:
        notebook = make_notebook(session, owner=owner, name=name)
        make_source(session, notebook, filename=filename, content=content,
                    status=status, owner=owner)
        return notebook.id
    finally:
        session.close()


class _FakeLLM:
    """Async stand-in for task_llm_call_async that records its arguments."""

    def __init__(self, result="S1: Hallo.\nS2: Dag.", exc=None):
        self.result = result
        self.exc = exc
        self.messages = None
        self.kwargs = None
        self.calls = 0

    async def __call__(self, messages, **kwargs):
        self.calls += 1
        self.messages = messages
        self.kwargs = kwargs
        if self.exc is not None:
            raise self.exc
        return self.result


class _FakeSynth:
    """Injected synthesizer: records (text, voice) and returns real WAV bytes."""

    def __init__(self, exc=None, frames=100, probe=None):
        self.calls = []
        self.exc = exc
        self.frames = frames
        self.probe = probe  # optional callable() run on every call

    def __call__(self, text, voice):
        self.calls.append((text, voice))
        if self.probe is not None:
            self.probe()
        if self.exc is not None:
            raise self.exc
        return _wav(self.frames)


def _prepare(monkeypatch, tmp_path, llm=None, synth=None):
    """Wire the module for a hermetic job run; returns (llm, synth)."""
    llm = llm if llm is not None else _FakeLLM()
    synth = synth if synth is not None else _FakeSynth()
    monkeypatch.setattr(audio, "task_llm_call_async", llm)
    monkeypatch.setattr(audio, "fire_event", lambda *a, **k: None)
    monkeypatch.setattr(audio, "NOTEBOOK_AUDIO_DIR", str(tmp_path))
    monkeypatch.setattr(audio, "load_settings", lambda: {"tts_provider": "local"})
    audio.set_synthesizer(synth)
    return llm, synth


async def _await_job(job_id, owner="own", timeout=10.0):
    """Poll until the job leaves 'running' (bounded); return the job dict."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = audio.get_job(job_id, owner)
        assert job is not None, "job disappeared while polling"
        if job["status"] != "running":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish within the test timeout")


# ── PODCAST_PROMPT ───────────────────────────────────────────────────────

def test_podcast_prompt_states_the_hard_requirements():
    prompt = audio.PODCAST_PROMPT
    assert "taal van de bronnen" in prompt          # source language, not Dutch
    assert "S1:" in prompt and "S2:" in prompt      # exact line format
    assert "20" in prompt and "40" in prompt        # 20-40 turns
    assert "400" in prompt                          # <= 400 words per turn


# ── parse_dialogue ───────────────────────────────────────────────────────

def test_parse_dialogue_basic_two_speakers():
    turns = audio.parse_dialogue("S1: Welkom.\nS2: Dank je.")
    assert turns == [("S1", "Welkom."), ("S2", "Dank je.")]


def test_parse_dialogue_is_case_insensitive_and_normalises_speaker():
    turns = audio.parse_dialogue("s1: een\nS2 : twee")
    assert turns == [("S1", "een"), ("S2", "twee")]


def test_parse_dialogue_joins_continuation_lines_into_previous_turn():
    script = "S1: eerste regel\nvervolg regel\nS2: antwoord"
    assert audio.parse_dialogue(script) == [
        ("S1", "eerste regel vervolg regel"),
        ("S2", "antwoord"),
    ]


def test_parse_dialogue_drops_preamble_before_the_first_speaker_line():
    script = "Hier is het script:\n\nS1: begin\nS2: eind"
    assert audio.parse_dialogue(script) == [("S1", "begin"), ("S2", "eind")]


def test_parse_dialogue_strips_think_blocks():
    script = "<think>even nadenken\nS9: nep</think>\nS1: echt\nS2: ook echt"
    assert audio.parse_dialogue(script) == [("S1", "echt"), ("S2", "ook echt")]


@pytest.mark.parametrize("script", ["", "   ", "geen sprekerlabels hier", "S3: onbekend"])
def test_parse_dialogue_raises_when_nothing_parses(script):
    with pytest.raises(RuntimeError):
        audio.parse_dialogue(script)


# ── split_turn ───────────────────────────────────────────────────────────

def test_split_turn_short_text_is_returned_whole():
    assert audio.split_turn("Een korte beurt.") == ["Een korte beurt."]


def test_split_turn_splits_on_sentence_boundaries():
    sentence = "Dit is een zin van redelijke lengte. "
    text = sentence * 400  # ~14k chars
    parts = audio.split_turn(text, limit=1000)

    assert len(parts) > 1
    assert all(len(p) <= 1000 for p in parts)
    # No sentence was cut in half: every part ends on a sentence terminator.
    assert all(p.rstrip().endswith(".") for p in parts)
    assert "".join(p.replace(" ", "") for p in parts) == text.replace(" ", "")


def test_split_turn_hard_splits_a_single_oversized_sentence():
    text = "woord " * 2000  # no sentence terminator at all
    parts = audio.split_turn(text, limit=500)

    assert len(parts) > 1
    assert all(len(p) <= 500 for p in parts)


def test_split_turn_default_limit_is_4500():
    parts = audio.split_turn("zin. " * 2000)
    assert all(len(p) <= 4500 for p in parts)


# ── concat_wavs ──────────────────────────────────────────────────────────

def test_concat_wavs_joins_frames_and_keeps_parameters():
    # Deliberately different durations: an implementation comparing whole
    # getparams() tuples (which include nframes) would wrongly reject these.
    a, b = _wav(100), _wav(250)

    result = audio.concat_wavs([a, b])

    assert _wav_info(result) == (1, 2, 24000, 350)


def test_concat_wavs_single_segment_roundtrips():
    assert _wav_info(audio.concat_wavs([_wav(42)])) == (1, 2, 24000, 42)


@pytest.mark.parametrize("mismatch", [
    {"framerate": 44100},
    {"nchannels": 2},
    {"sampwidth": 1},
])
def test_concat_wavs_raises_on_parameter_mismatch(mismatch):
    with pytest.raises(RuntimeError):
        audio.concat_wavs([_wav(100), _wav(100, **mismatch)])


def test_concat_wavs_raises_on_unreadable_segment():
    with pytest.raises(RuntimeError):
        audio.concat_wavs([_wav(100), b"dit-is-geen-wav-maar-mp3"])


def test_concat_wavs_raises_on_empty_input():
    with pytest.raises(RuntimeError):
        audio.concat_wavs([])


# ── resolve_notebook_audio_path ──────────────────────────────────────────

def test_resolve_notebook_audio_path_returns_existing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(audio, "NOTEBOOK_AUDIO_DIR", str(tmp_path))
    name = uuid.uuid4().hex + ".wav"
    (tmp_path / name).write_bytes(_wav(10))

    assert audio.resolve_notebook_audio_path(name) == (tmp_path / name).resolve()


@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "notebook_audio/x.wav",
    "ABCDEF0123456789abcdef0123456789.wav",   # uppercase hex not allowed
    "deadbeef.wav",                            # too short
    "0123456789abcdef0123456789abcdef.mp3",    # wrong extension
    "0123456789abcdef0123456789abcdef",        # no extension
    "",
])
def test_resolve_notebook_audio_path_rejects_bad_filenames(monkeypatch, tmp_path, bad):
    monkeypatch.setattr(audio, "NOTEBOOK_AUDIO_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        audio.resolve_notebook_audio_path(bad)
    assert exc.value.status_code == 400


def test_resolve_notebook_audio_path_404_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(audio, "NOTEBOOK_AUDIO_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        audio.resolve_notebook_audio_path(uuid.uuid4().hex + ".wav")
    assert exc.value.status_code == 404


def test_resolve_notebook_audio_path_reads_the_dir_at_call_time(monkeypatch, tmp_path):
    """The module attribute is resolved per call, so monkeypatching works."""
    name = uuid.uuid4().hex + ".wav"
    other = tmp_path / "elders"
    other.mkdir()
    (other / name).write_bytes(_wav(5))
    monkeypatch.setattr(audio, "NOTEBOOK_AUDIO_DIR", str(other))

    assert audio.resolve_notebook_audio_path(name).parent == other.resolve()


# ── voice resolution ─────────────────────────────────────────────────────

def test_resolve_voices_defaults_for_local_provider(monkeypatch):
    monkeypatch.setattr(audio, "load_settings", lambda: {"tts_provider": "local"})
    assert audio.resolve_voices() == ("af_heart", "am_michael")


def test_resolve_voices_defaults_for_endpoint_provider(monkeypatch):
    monkeypatch.setattr(audio, "load_settings", lambda: {"tts_provider": "endpoint:ep1"})
    assert audio.resolve_voices() == ("alloy", "onyx")


def test_resolve_voices_settings_override_wins(monkeypatch):
    monkeypatch.setattr(audio, "load_settings", lambda: {
        "tts_provider": "local",
        "notebook_podcast_voice_a": "bf_emma",
        "notebook_podcast_voice_b": "bm_george",
    })
    assert audio.resolve_voices() == ("bf_emma", "bm_george")


def test_resolve_voices_blank_override_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(audio, "load_settings", lambda: {
        "tts_provider": "endpoint:ep1",
        "notebook_podcast_voice_a": "   ",
        "notebook_podcast_voice_b": "",
    })
    assert audio.resolve_voices() == ("alloy", "onyx")


def test_resolve_voices_survives_broken_settings(monkeypatch):
    def boom():
        raise OSError("settings.json unreadable")

    monkeypatch.setattr(audio, "load_settings", boom)
    assert audio.resolve_voices() == ("alloy", "onyx")


# ── synthesizer hook ─────────────────────────────────────────────────────

def test_set_synthesizer_roundtrip():
    def fn(text, voice):
        return b""

    audio.set_synthesizer(fn)
    assert audio.get_synthesizer() is fn
    audio.set_synthesizer(None)
    assert audio.get_synthesizer() is None


# ── start_podcast_job: validation ────────────────────────────────────────

async def test_start_rejects_unknown_notebook(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="Notebook niet gevonden"):
        audio.start_podcast_job("does-not-exist", "own", _TS)


async def test_start_rejects_foreign_notebook(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    nb_id = _seed_notebook()

    with pytest.raises(ValueError, match="Notebook niet gevonden"):
        audio.start_podcast_job(nb_id, "iemand-anders", _TS)


async def test_start_rejects_notebook_without_indexed_sources(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    nb_id = _seed_notebook(status="failed")

    with pytest.raises(ValueError, match="Geen geïndexeerde bronnen"):
        audio.start_podcast_job(nb_id, "own", _TS)


async def test_start_rejects_when_no_synthesizer_is_configured(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    audio.set_synthesizer(None)
    nb_id = _seed_notebook()

    with pytest.raises(RuntimeError, match="TTS niet geconfigureerd"):
        audio.start_podcast_job(nb_id, "own", _TS)


async def test_failed_validation_registers_no_job(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        audio.start_podcast_job("nope", "own", _TS)
    assert audio._active_jobs == {}


# ── start_podcast_job: happy path ────────────────────────────────────────

async def test_job_produces_document_artifact_and_audio_file(monkeypatch, tmp_path):
    llm, synth = _prepare(
        monkeypatch, tmp_path,
        llm=_FakeLLM("S1: Welkom bij de show.\nS2: Fijn om er te zijn.\nS1: Tot slot."),
    )
    nb_id = _seed_notebook(name="Testboek", content="brontekst over vogels")

    job_id = audio.start_podcast_job(nb_id, "own", _TS)
    assert re.fullmatch(r"[a-f0-9]{32}", job_id)

    job = await _await_job(job_id)
    assert job["status"] == "done", job.get("error")
    assert job["phase"] == "done"
    assert job["segment"] == job["total"] == 3

    s = _TS()
    try:
        art = s.query(cdb.NotebookArtifact).filter_by(notebook_id=nb_id).one()
        audio_path = art.audio_path
        assert art.kind == "podcast"
        assert re.fullmatch(r"[a-f0-9]{32}\.wav", audio_path)
        doc = s.get(cdb.Document, art.document_id)
        assert doc.title == "Testboek — Podcast"
        assert doc.language == "markdown"
        assert doc.owner == "own"
        assert "Welkom bij de show." in doc.current_content
        assert "S1" in doc.current_content and "S2" in doc.current_content
        artifact_id = art.id
    finally:
        s.close()

    # Exactly the finished WAV on disk — no leftover tempfile.
    assert [p.name for p in tmp_path.iterdir()] == [audio_path]
    assert _wav_info((tmp_path / audio_path).read_bytes()) == (1, 2, 24000, 300)

    # job dict carries the artifact for the UI, and never the asyncio task.
    assert job["artifact"]["id"] == artifact_id
    assert job["artifact"]["audio_path"] == audio_path
    assert "task" not in job


async def test_job_alternates_voices_per_speaker(monkeypatch, tmp_path):
    _, synth = _prepare(
        monkeypatch, tmp_path,
        llm=_FakeLLM("S1: een\nS2: twee\nS1: drie"),
    )
    nb_id = _seed_notebook()

    job = await _await_job(audio.start_podcast_job(nb_id, "own", _TS))
    assert job["status"] == "done", job.get("error")
    assert synth.calls == [
        ("een", "af_heart"), ("twee", "am_michael"), ("drie", "af_heart"),
    ]


async def test_job_llm_call_uses_foreground_workload_without_quiet_gate(monkeypatch, tmp_path):
    llm, _ = _prepare(monkeypatch, tmp_path)
    nb_id = _seed_notebook(name="Testboek", content="brontekst over vogels")

    job = await _await_job(audio.start_podcast_job(nb_id, "own", _TS))
    assert job["status"] == "done", job.get("error")
    assert llm.kwargs["owner"] == "own"
    assert llm.kwargs["wait_for_quiet"] is False
    assert llm.kwargs["workload"] == "foreground"
    system = "\n".join(m["content"] for m in llm.messages if m["role"] == "system")
    user = "\n".join(m["content"] for m in llm.messages if m["role"] == "user")
    assert audio.PODCAST_PROMPT in system
    assert "=== BRON: a.txt ===" in user
    assert "brontekst over vogels" in user


async def test_job_splits_an_oversized_turn_into_multiple_segments(monkeypatch, tmp_path):
    long_turn = "Dit is een zin. " * 500  # ~8000 chars > 4500
    _, synth = _prepare(
        monkeypatch, tmp_path,
        llm=_FakeLLM(f"S1: {long_turn}\nS2: kort"),
    )
    nb_id = _seed_notebook()

    job = await _await_job(audio.start_podcast_job(nb_id, "own", _TS))
    assert job["status"] == "done", job.get("error")
    assert job["total"] == len(synth.calls) > 2
    # All segments of the split turn keep speaker A's voice.
    assert [v for _, v in synth.calls[:-1]] == ["af_heart"] * (len(synth.calls) - 1)
    assert synth.calls[-1][1] == "am_michael"


async def test_job_reports_progress_while_synthesising(monkeypatch, tmp_path):
    seen = []
    holder = {}

    def probe():
        job = audio.get_job(holder["job_id"], "own")
        seen.append((job["phase"], job["segment"], job["total"]))

    _prepare(
        monkeypatch, tmp_path,
        llm=_FakeLLM("S1: een\nS2: twee"),
        synth=_FakeSynth(probe=probe),
    )
    nb_id = _seed_notebook()

    holder["job_id"] = audio.start_podcast_job(nb_id, "own", _TS)
    job = await _await_job(holder["job_id"])
    assert job["status"] == "done", job.get("error")
    assert [phase for phase, _, _ in seen] == ["tts", "tts"]
    assert [total for _, _, total in seen] == [2, 2]
    # `segment` counts *completed* segments, so it still reads 0 while the
    # first one is being synthesized. Measured from inside the synthesizer, so
    # there is no timing race: the counter is bumped after each call returns.
    assert [seg for _, seg, _ in seen] == [0, 1]


async def test_job_fires_document_created_after_commit(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    fired = []
    monkeypatch.setattr(audio, "fire_event", lambda *a, **k: fired.append(a))
    nb_id = _seed_notebook()

    job = await _await_job(audio.start_podcast_job(nb_id, "own", _TS))
    assert job["status"] == "done", job.get("error")
    assert fired == [("document_created", "own")]


# ── start_podcast_job: failure paths ─────────────────────────────────────

def _document_count():
    s = _TS()
    try:
        return s.query(cdb.Document).count()
    finally:
        s.close()


def _assert_no_traces(tmp_path, notebook_id, documents_before):
    """A failed job leaves no artifact row, no Document and no file at all.

    The Document check is relative to a pre-job snapshot because the temp DB
    is shared by the whole module (earlier tests legitimately leave rows).
    The directory check catches a stray .tmp as well as a published .wav.
    """
    s = _TS()
    try:
        assert s.query(cdb.NotebookArtifact).filter_by(notebook_id=notebook_id).count() == 0
        assert s.query(cdb.Document).count() == documents_before
    finally:
        s.close()
    assert list(tmp_path.iterdir()) == []


async def test_synth_failure_leaves_error_status_and_no_traces(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path, synth=_FakeSynth(exc=RuntimeError("TTS endpoint down")))
    nb_id = _seed_notebook()
    documents_before = _document_count()

    job = await _await_job(audio.start_podcast_job(nb_id, "own", _TS))
    assert job["status"] == "error"
    assert "TTS endpoint down" in job["error"]
    _assert_no_traces(tmp_path, nb_id, documents_before)


async def test_llm_failure_leaves_error_status_and_no_traces(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path, llm=_FakeLLM(exc=RuntimeError("endpoint down")))
    nb_id = _seed_notebook()
    documents_before = _document_count()

    job = await _await_job(audio.start_podcast_job(nb_id, "own", _TS))
    assert job["status"] == "error"
    assert "endpoint down" in job["error"]
    _assert_no_traces(tmp_path, nb_id, documents_before)


async def test_unparsable_script_leaves_error_status_and_no_traces(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path, llm=_FakeLLM("Sorry, ik kan dit niet."))
    nb_id = _seed_notebook()
    documents_before = _document_count()

    job = await _await_job(audio.start_podcast_job(nb_id, "own", _TS))
    assert job["status"] == "error"
    assert job["error"]
    _assert_no_traces(tmp_path, nb_id, documents_before)


async def test_wav_parameter_mismatch_between_segments_errors_cleanly(monkeypatch, tmp_path):
    class _StereoDrift(_FakeSynth):
        def __call__(self, text, voice):
            self.calls.append((text, voice))
            return _wav(100) if len(self.calls) == 1 else _wav(100, nchannels=2)

    _prepare(monkeypatch, tmp_path, llm=_FakeLLM("S1: een\nS2: twee"), synth=_StereoDrift())
    nb_id = _seed_notebook()
    documents_before = _document_count()

    job = await _await_job(audio.start_podcast_job(nb_id, "own", _TS))
    assert job["status"] == "error"
    assert job["error"]
    _assert_no_traces(tmp_path, nb_id, documents_before)


async def test_unwritable_audio_dir_errors_cleanly(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(audio, "NOTEBOOK_AUDIO_DIR", str(tmp_path / "bestaat-niet"))
    nb_id = _seed_notebook()
    documents_before = _document_count()

    job = await _await_job(audio.start_podcast_job(nb_id, "own", _TS))
    assert job["status"] == "error"
    assert "bestaat-niet" in job["error"]
    _assert_no_traces(tmp_path, nb_id, documents_before)


# ── get_job ──────────────────────────────────────────────────────────────

async def test_get_job_owner_check_and_unknown_id(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    nb_id = _seed_notebook()

    job_id = audio.start_podcast_job(nb_id, "own", _TS)
    assert audio.get_job(job_id, "own") is not None
    assert audio.get_job(job_id, "iemand-anders") is None
    assert audio.get_job("onbekend", "own") is None
    await _await_job(job_id)


async def test_get_job_returns_a_copy_not_the_live_entry(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    nb_id = _seed_notebook()

    job_id = audio.start_podcast_job(nb_id, "own", _TS)
    snapshot = audio.get_job(job_id, "own")
    snapshot["status"] = "geknoeid"
    await _await_job(job_id)
    assert audio._active_jobs[job_id]["status"] == "done"


def test_job_timeout_constant_is_thirty_minutes():
    assert audio.JOB_TIMEOUT_SECONDS == 1800
