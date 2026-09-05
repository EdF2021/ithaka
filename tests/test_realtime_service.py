"""RealtimeService — mints OpenAI Realtime ephemeral client secrets. See
docs/superpowers/plans/2026-09-03-realtime-voice-mode.md, Task 2."""

import httpx
import pytest

from services.realtime.realtime_service import RealtimeService


class _FakeEp:
    def __init__(self, base_url="https://api.openai.com/v1", api_key="sk-real-key"):
        self.base_url = base_url
        self.api_key = api_key


class _FakeQuery:
    def __init__(self, ep):
        self._ep = ep

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._ep


class _FakeDb:
    def __init__(self, ep):
        self._ep = ep

    def query(self, *a, **k):
        return _FakeQuery(self._ep)

    def close(self):
        pass


def _wire_fake_db(monkeypatch, ep):
    import src.database as dbmod
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _FakeDb(ep))


def _settings(**overrides):
    base = {
        "realtime_enabled": True,
        "realtime_provider": "endpoint:ep1",
        "realtime_model": "gpt-realtime-2.1-mini",
        "realtime_voice": "ash",
        "realtime_vad_threshold": 0.5,
        "realtime_vad_prefix_ms": 300,
        "realtime_vad_silence_ms": 500,
        "realtime_noise_reduction": "far_field",
        "realtime_max_minutes": 10,
        "realtime_instructions": "Antwoord in het Nederlands.",
        "realtime_tools_enabled": True,
    }
    base.update(overrides)
    return base


def test_build_session_config_shape():
    service = RealtimeService()
    cfg = service.build_session_config(_settings(realtime_tools_enabled=False))

    assert cfg["type"] == "realtime"
    assert cfg["model"] == "gpt-realtime-2.1-mini"
    assert cfg["instructions"] == "Antwoord in het Nederlands."
    assert cfg["tools"] == []
    assert cfg["max_output_tokens"] == "inf"
    assert cfg["output_modalities"] == ["audio"]
    assert cfg["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert cfg["audio"]["input"]["noise_reduction"] == {"type": "far_field"}
    assert cfg["audio"]["input"]["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 500,
        "interrupt_response": True,
    }
    assert cfg["audio"]["output"] == {
        "format": {"type": "audio/pcm", "rate": 24000},
        "voice": "ash",
    }


def test_available_false_when_disabled():
    service = RealtimeService()
    monkeypatch_settings = _settings(realtime_enabled=False)
    service._load_settings = lambda: monkeypatch_settings
    assert service.available is False


def test_available_true_when_enabled_with_endpoint():
    service = RealtimeService()
    service._load_settings = lambda: _settings()
    assert service.available is True


def test_create_session_raises_dutch_error_when_disabled():
    service = RealtimeService()
    service._load_settings = lambda: _settings(realtime_enabled=False)
    with pytest.raises(ValueError, match="Realtime-gesprek staat uit"):
        service.create_session()


def test_create_session_raises_when_endpoint_missing(monkeypatch):
    service = RealtimeService()
    service._load_settings = lambda: _settings(realtime_provider="disabled")
    with pytest.raises(ValueError, match="Geen Realtime-endpoint ingesteld"):
        service.create_session()


def test_create_session_raises_when_endpoint_row_gone(monkeypatch):
    service = RealtimeService()
    service._load_settings = lambda: _settings()
    _wire_fake_db(monkeypatch, ep=None)
    with pytest.raises(ValueError, match="bestaat niet meer"):
        service.create_session()


def test_create_session_mints_client_secret_and_never_leaks_api_key(monkeypatch):
    service = RealtimeService()
    service._load_settings = lambda: _settings()
    _wire_fake_db(monkeypatch, ep=_FakeEp(api_key="sk-super-secret"))

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"value": "ek_abc123", "expires_at": 1234567890}

    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResp()

    import services.realtime.realtime_service as mod
    monkeypatch.setattr(mod.httpx, "post", _fake_post)

    result = service.create_session()

    assert result == {
        "client_secret": "ek_abc123",
        "expires_at": 1234567890,
        "max_minutes": 10,
        "model": "gpt-realtime-2.1-mini",
        "calls_url": "https://api.openai.com/v1/realtime/calls",
        "transcription": None,
    }
    assert "sk-super-secret" not in str(result)
    assert captured["url"] == "https://api.openai.com/v1/realtime/client_secrets"
    assert captured["headers"]["Authorization"] == "Bearer sk-super-secret"
    assert captured["json"]["session"]["model"] == "gpt-realtime-2.1-mini"
    assert captured["json"]["expires_after"] == {"anchor": "created_at", "seconds": 600}


def test_create_session_raises_on_http_status_error(monkeypatch):
    service = RealtimeService()
    service._load_settings = lambda: _settings()
    _wire_fake_db(monkeypatch, ep=_FakeEp())

    class _FakeResp:
        status_code = 401

        def raise_for_status(self):
            raise httpx.HTTPStatusError("401", request=None, response=self)

    import services.realtime.realtime_service as mod
    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _FakeResp())

    with pytest.raises(ValueError, match="HTTP 401"):
        service.create_session()


def test_create_session_raises_dutch_error_on_malformed_json_response(monkeypatch):
    service = RealtimeService()
    service._load_settings = lambda: _settings()
    _wire_fake_db(monkeypatch, ep=_FakeEp())

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    import services.realtime.realtime_service as mod
    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _FakeResp())

    with pytest.raises(ValueError, match="Ongeldig antwoord van het Realtime-endpoint"):
        service.create_session()


def test_create_session_raises_on_network_failure(monkeypatch):
    service = RealtimeService()
    service._load_settings = lambda: _settings()
    _wire_fake_db(monkeypatch, ep=_FakeEp())

    def _raise(*a, **k):
        raise httpx.ConnectError("refused")

    import services.realtime.realtime_service as mod
    monkeypatch.setattr(mod.httpx, "post", _raise)

    with pytest.raises(ValueError, match="Kon geen verbinding maken"):
        service.create_session()


def test_build_session_config_declares_ask_ithaka_tool():
    from services.realtime.realtime_service import ASK_ITHAKA_TOOL
    cfg = RealtimeService().build_session_config(_settings(realtime_tools_enabled=True))
    assert cfg["tools"] == [ASK_ITHAKA_TOOL]
    assert cfg["tool_choice"] == "auto"
    assert ASK_ITHAKA_TOOL["type"] == "function"
    assert ASK_ITHAKA_TOOL["name"] == "ask_ithaka"
    assert ASK_ITHAKA_TOOL["parameters"]["required"] == ["question"]
    assert "Momentje" in ASK_ITHAKA_TOOL["description"]


def test_build_session_config_without_tools_when_disabled():
    cfg = RealtimeService().build_session_config(_settings(realtime_tools_enabled=False))
    assert cfg["tools"] == []
    assert "tool_choice" not in cfg


def test_build_session_config_adds_input_transcription_when_model_set():
    service = RealtimeService()
    cfg = service.build_session_config(
        _settings(realtime_transcription_model="gpt-realtime-whisper", stt_language="nl")
    )
    assert cfg["audio"]["input"]["transcription"] == {"model": "gpt-realtime-whisper", "language": "nl"}


def test_build_session_config_transcription_model_without_language():
    service = RealtimeService()
    cfg = service.build_session_config(_settings(realtime_transcription_model="gpt-4o-mini-transcribe"))
    assert cfg["audio"]["input"]["transcription"] == {"model": "gpt-4o-mini-transcribe"}


def test_build_session_config_no_transcription_when_empty():
    service = RealtimeService()
    cfg = service.build_session_config(_settings(realtime_transcription_model="  "))
    assert "transcription" not in cfg["audio"]["input"]


def test_create_session_returns_transcription_config(monkeypatch):
    service = RealtimeService()
    service._load_settings = lambda: _settings(
        realtime_transcription_model="gpt-realtime-whisper", stt_language="nl")
    _wire_fake_db(monkeypatch, ep=_FakeEp(api_key="sk-x"))

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"value": "ek_x", "expires_at": 1}
    monkeypatch.setattr("services.realtime.realtime_service.httpx.post", lambda *a, **k: _Resp())
    result = service.create_session()
    assert result["transcription"] == {"model": "gpt-realtime-whisper", "language": "nl"}


def test_load_settings_includes_transcription_model_and_language(monkeypatch):
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"stt_language": "nl"})
    loaded = RealtimeService()._load_settings()
    assert loaded["realtime_transcription_model"] == "gpt-realtime-whisper"
    assert loaded["stt_language"] == "nl"
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"realtime_transcription_model": ""})
    assert RealtimeService()._load_settings()["realtime_transcription_model"] == ""
