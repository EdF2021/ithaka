"""On a transcription failure, STTService.last_error should carry the
upstream reason, and routes/stt_routes.py should fold it into the 500
response's detail message instead of the bare "Transcription failed" the
frontend previously had nothing to show the user (2026-09-02 incident:
voice mode failed on every turn with only a console.error).
"""
import httpx
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


def _wire_fake_db(monkeypatch):
    import src.database as dbmod
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _FakeDb())


def test_transcribe_api_sets_last_error_on_http_status_error(monkeypatch):
    service = STTService()
    _wire_fake_db(monkeypatch)

    class _FakeResp:
        status_code = 404

        def raise_for_status(self):
            raise httpx.HTTPStatusError("404", request=None, response=self)

    import services.stt.stt_service as mod
    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _FakeResp())

    result = service._transcribe_api(b"dummy", "ep1", "whisper-1", "nl")

    assert result is None
    assert service.last_error == "endpoint returned HTTP 404"


def test_transcribe_api_sets_last_error_on_network_failure(monkeypatch):
    service = STTService()
    _wire_fake_db(monkeypatch)

    import services.stt.stt_service as mod

    def _raise(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(mod.httpx, "post", _raise)

    result = service._transcribe_api(b"dummy", "ep1", "whisper-1", "nl")

    assert result is None
    assert "endpoint request failed" in service.last_error


def test_transcribe_resets_last_error_on_next_call(monkeypatch):
    service = STTService()
    service.last_error = "stale error from a previous attempt"
    monkeypatch.setattr(service, "_load_settings", lambda: {
        "stt_enabled": True, "stt_provider": "endpoint:ep1", "stt_model": "whisper-1", "stt_language": "",
    })
    import services.stt.stt_service as mod
    monkeypatch.setattr(mod, "_audio_is_silent", lambda b: False)
    monkeypatch.setattr(service, "_transcribe_api", lambda *a, **k: "hallo")

    result = service.transcribe(b"audio")

    assert result == "hallo"
    assert service.last_error is None


# ---- route wiring: /api/stt/transcribe detail message --------------------


class _FakeUploadFile:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self, n=-1):
        return self._data


async def test_transcribe_route_includes_last_error_in_detail():
    from routes.stt_routes import setup_stt_routes

    class _FakeSttService:
        available = True
        last_error = "endpoint returned HTTP 404 — no /audio/transcriptions route"

        def transcribe(self, audio_bytes):
            return None

    router = setup_stt_routes(_FakeSttService())
    transcribe_audio = next(
        r.endpoint for r in router.routes
        if getattr(r, "path", "") == "/api/stt/transcribe" and "POST" in getattr(r, "methods", set())
    )

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await transcribe_audio(file=_FakeUploadFile(b"audio-bytes"))

    assert exc.value.status_code == 500
    assert "404" in exc.value.detail["message"]


async def test_transcribe_route_falls_back_to_generic_message_without_last_error():
    from routes.stt_routes import setup_stt_routes

    class _FakeSttService:
        available = True
        last_error = None

        def transcribe(self, audio_bytes):
            return None

    router = setup_stt_routes(_FakeSttService())
    transcribe_audio = next(
        r.endpoint for r in router.routes
        if getattr(r, "path", "") == "/api/stt/transcribe" and "POST" in getattr(r, "methods", set())
    )

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await transcribe_audio(file=_FakeUploadFile(b"audio-bytes"))

    assert exc.value.status_code == 500
    assert exc.value.detail["message"] == "Transcription failed"
