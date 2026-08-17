"""Notebook podcast audio — TTS-layer tests (Fase 3, Task 1).

Only the TTS part (`TTSService.synthesize_voice` + `_synthesize_api`
`response_format` passthrough). The audio module (script parsing, WAV concat,
job runner) lands in a later task and will extend this file. Hermetic:
`_load_settings` and the provider methods are monkeypatched — no network,
no Kokoro/GPU, no real DB.
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
