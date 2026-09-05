"""STTService.transcribe() gains optional prompt/timeout/filename kwargs
(meeting-recorder Task 1) that flow through to the local and API providers:
- prompt -> Whisper's `initial_prompt` (local) / multipart `prompt` field (API)
- timeout -> the API provider's httpx.post timeout
- filename -> the multipart filename / temp-file suffix, driving the mime
  type picked for the API upload

Existing callers (routes/stt_routes.py) call transcribe(audio_bytes) with no
extra kwargs, so the defaults must reproduce the prior behaviour exactly
(timeout=60, filename="audio.webm", no "prompt" key sent at all).
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.stt.stt_service import STTService


class _FakeEp:
    base_url = "http://fake"
    api_key = ""


class _FakeQuery:
    def filter(self, *a, **k):
        return self

    def first(self):
        return _FakeEp()


class _FakeDb:
    def query(self, *a, **k):
        return _FakeQuery()

    def close(self):
        pass


class _FakeWhisperModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return ([], SimpleNamespace(language="nl", language_probability=0.9))


@pytest.fixture
def service_with_endpoint(monkeypatch):
    service = STTService()
    monkeypatch.setattr(service, "_load_settings", lambda: {
        "stt_enabled": True,
        "stt_provider": "endpoint:ep1",
        "stt_model": "whisper-1",
        "stt_language": "",
    })
    import src.database as dbmod
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _FakeDb())
    return service


@pytest.fixture
def service_local(monkeypatch):
    service = STTService()
    monkeypatch.setattr(service, "_load_settings", lambda: {
        "stt_enabled": True,
        "stt_provider": "local",
        "stt_model": "base",
        "stt_language": "",
    })
    return service


def test_api_transcribe_passes_prompt_timeout_filename(monkeypatch, service_with_endpoint):
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"text": "hallo"}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured.update(url=url, files=files, data=data, timeout=timeout)
        return _Resp()

    monkeypatch.setattr("services.stt.stt_service.httpx.post", fake_post)
    monkeypatch.setattr("services.stt.stt_service._audio_is_silent", lambda b: False)

    out = service_with_endpoint.transcribe(b"x" * 100, prompt="Namen: Ed", timeout=600, filename="seg_000.ogg")

    assert out == "hallo"
    assert captured["timeout"] == 600
    assert captured["data"]["prompt"] == "Namen: Ed"
    assert captured["files"]["file"][0] == "seg_000.ogg" and captured["files"]["file"][2] == "audio/ogg"


def test_api_transcribe_default_kwargs_unchanged(monkeypatch, service_with_endpoint):
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"text": "hallo"}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured.update(url=url, files=files, data=data, timeout=timeout)
        return _Resp()

    monkeypatch.setattr("services.stt.stt_service.httpx.post", fake_post)
    monkeypatch.setattr("services.stt.stt_service._audio_is_silent", lambda b: False)

    out = service_with_endpoint.transcribe(b"x" * 100)

    assert out == "hallo"
    assert captured["timeout"] == 60
    assert "prompt" not in captured["data"]
    assert captured["files"]["file"][0] == "audio.webm"
    assert captured["files"]["file"][2] == "audio/webm"


def test_local_transcribe_passes_initial_prompt(monkeypatch, service_local):
    fake_model = _FakeWhisperModel()
    monkeypatch.setattr(service_local, "_get_whisper", lambda: fake_model)
    monkeypatch.setattr("services.stt.stt_service._audio_is_silent", lambda b: False)

    out = service_local.transcribe(b"x" * 100, prompt="Namen: Ed", filename="seg_000.ogg")

    assert out == ""
    assert len(fake_model.calls) == 1
    path, kwargs = fake_model.calls[0]
    assert kwargs["initial_prompt"] == "Namen: Ed"
    assert Path(path).suffix == ".ogg"
