"""Tests for services.stt.stt_service.probe_endpoint — verifies a candidate
STT ModelEndpoint actually implements OpenAI-compatible /audio/transcriptions
before the settings save (routes/auth_routes.py) persists it as the STT
provider. See 2026-09-02 incident: a chat-only endpoint (Google Gemini's
OpenAI-compat base_url, no transcription route) was accepted silently and
then 500'd every voice-mode turn.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ModelEndpoint
import src.database as src_db
from services.stt.stt_service import _tiny_silent_wav, probe_endpoint


def _mem_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(src_db, "SessionLocal", TestSessionLocal)
    # conftest.py stubs `src.database` with MagicMock() placeholders for
    # SessionLocal/ModelEndpoint when it isn't imported yet at collection
    # time (avoids importing the real ORM in lightweight test files). Point
    # the stub's ModelEndpoint at the real mapped class too, or querying
    # with it 500s with a SQLAlchemy ArgumentError.
    monkeypatch.setattr(src_db, "ModelEndpoint", ModelEndpoint)
    return TestSessionLocal


def _add_endpoint(TestSessionLocal, **kwargs):
    db = TestSessionLocal()
    try:
        defaults = dict(id="ep1", name="Test Endpoint", base_url="https://example.com/v1", api_key=None)
        defaults.update(kwargs)
        db.add(ModelEndpoint(**defaults))
        db.commit()
    finally:
        db.close()


def _mock_client(status_code):
    resp = MagicMock()
    resp.status_code = status_code
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(return_value=resp)
    return client


async def test_probe_returns_ok_on_200(monkeypatch):
    TestSessionLocal = _mem_db(monkeypatch)
    _add_endpoint(TestSessionLocal)

    with patch("httpx.AsyncClient", return_value=_mock_client(200)):
        ok, reason = await probe_endpoint("ep1", "whisper-1")

    assert ok is True
    assert reason == ""


async def test_probe_fails_on_404_no_transcription_route(monkeypatch):
    TestSessionLocal = _mem_db(monkeypatch)
    _add_endpoint(TestSessionLocal, base_url="https://generativelanguage.googleapis.com/v1beta/openai")

    with patch("httpx.AsyncClient", return_value=_mock_client(404)):
        ok, reason = await probe_endpoint("ep1", "whisper-1")

    assert ok is False
    assert "404" in reason
    assert "not a transcription-capable" in reason


async def test_probe_fails_on_401_mentions_api_key(monkeypatch):
    TestSessionLocal = _mem_db(monkeypatch)
    _add_endpoint(TestSessionLocal)

    with patch("httpx.AsyncClient", return_value=_mock_client(401)):
        ok, reason = await probe_endpoint("ep1", "whisper-1")

    assert ok is False
    assert "API key" in reason


async def test_probe_fails_on_timeout(monkeypatch):
    TestSessionLocal = _mem_db(monkeypatch)
    _add_endpoint(TestSessionLocal)

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))

    with patch("httpx.AsyncClient", return_value=client):
        ok, reason = await probe_endpoint("ep1", "whisper-1", timeout=1.0)

    assert ok is False
    assert "timed out" in reason


async def test_probe_fails_on_connection_refused(monkeypatch):
    TestSessionLocal = _mem_db(monkeypatch)
    _add_endpoint(TestSessionLocal)

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

    with patch("httpx.AsyncClient", return_value=client):
        ok, reason = await probe_endpoint("ep1", "whisper-1")

    assert ok is False
    assert "could not reach endpoint" in reason


async def test_probe_fails_when_endpoint_not_found(monkeypatch):
    _mem_db(monkeypatch)

    ok, reason = await probe_endpoint("missing-id", "whisper-1")

    assert ok is False
    assert "not found" in reason


@pytest.mark.parametrize("status", [400, 422, 500])
async def test_probe_passes_on_ambiguous_status_codes(monkeypatch, status):
    # A real Whisper-compatible API can reject a degenerate silent probe
    # clip with 400/422 ("audio too short"/"no speech"), or even 500 on an
    # edge case — none of that proves the route is missing, only that the
    # server parsed an OpenAI-style multipart transcription request. Only
    # 404/405 (no route) and 401/403 (auth) should refuse the save.
    TestSessionLocal = _mem_db(monkeypatch)
    _add_endpoint(TestSessionLocal)

    with patch("httpx.AsyncClient", return_value=_mock_client(status)):
        ok, reason = await probe_endpoint("ep1", "whisper-1")

    assert ok is True
    assert reason == ""


def test_tiny_silent_wav_is_a_valid_16khz_mono_pcm_clip():
    import wave
    from io import BytesIO

    with wave.open(BytesIO(_tiny_silent_wav()), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        assert w.getnframes() > 0
