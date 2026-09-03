"""Realtime voice mode (fase 1) — mints ephemeral OpenAI Realtime client
secrets. The backend never touches audio: the browser connects directly to
OpenAI over WebRTC using the short-lived client_secret this service mints.
See docs/superpowers/specs/2026-09-03-realtime-voice-mode-design.md.

Provider config mirrors STTService/TTSService:
  "disabled"        — realtime voice mode unavailable
  "endpoint:<id>"   — OpenAI-compatible /v1/realtime/client_secrets via ModelEndpoint
"""

import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)


class RealtimeService:
    def _load_settings(self) -> dict:
        from src.settings import load_settings
        saved = load_settings()
        return {
            "realtime_enabled": saved.get("realtime_enabled", False),
            "realtime_provider": saved.get("realtime_provider", "disabled"),
            "realtime_model": saved.get("realtime_model", "gpt-realtime-2.1-mini"),
            "realtime_voice": saved.get("realtime_voice", "ash"),
            "realtime_vad_threshold": saved.get("realtime_vad_threshold", 0.5),
            "realtime_vad_prefix_ms": saved.get("realtime_vad_prefix_ms", 300),
            "realtime_vad_silence_ms": saved.get("realtime_vad_silence_ms", 500),
            "realtime_noise_reduction": saved.get("realtime_noise_reduction", "far_field"),
            "realtime_max_minutes": saved.get("realtime_max_minutes", 10),
            "realtime_instructions": saved.get("realtime_instructions", ""),
        }

    @property
    def available(self) -> bool:
        settings = self._load_settings()
        if not settings["realtime_enabled"]:
            return False
        return settings["realtime_provider"].startswith("endpoint:")

    def build_session_config(self, settings: dict) -> dict:
        """Pure builder: settings dict -> OpenAI Realtime session config.
        No network I/O — kept separate from create_session() for testing."""
        return {
            "type": "realtime",
            "model": settings["realtime_model"],
            "instructions": settings["realtime_instructions"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "noise_reduction": {"type": settings["realtime_noise_reduction"]},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": settings["realtime_vad_threshold"],
                        "prefix_padding_ms": settings["realtime_vad_prefix_ms"],
                        "silence_duration_ms": settings["realtime_vad_silence_ms"],
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": settings["realtime_voice"],
                },
            },
            "output_modalities": ["audio"],
            "tools": [],
            "max_output_tokens": "inf",
        }

    def _resolve_endpoint(self, provider: str) -> tuple[str, Optional[str]]:
        if not provider.startswith("endpoint:"):
            raise ValueError("Geen Realtime-endpoint ingesteld")

        endpoint_id = provider.split(":", 1)[1]
        from src.database import SessionLocal, ModelEndpoint

        db = SessionLocal()
        try:
            ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
            if not ep:
                raise ValueError("Geconfigureerd Realtime-endpoint bestaat niet meer")
            return ep.base_url.rstrip("/"), ep.api_key
        finally:
            db.close()

    def create_session(self) -> dict:
        """Mints an ephemeral OpenAI Realtime client secret. Raises
        ValueError with a Dutch message on any failure — the route turns
        that into a 400. Never returns the long-lived api_key."""
        settings = self._load_settings()
        if not settings["realtime_enabled"]:
            raise ValueError("Realtime-gesprek staat uit")

        base_url, api_key = self._resolve_endpoint(settings["realtime_provider"])
        if not api_key:
            raise ValueError("Realtime-endpoint heeft geen API-key ingesteld")

        session_config = self.build_session_config(settings)
        url = base_url + "/realtime/client_secrets"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "expires_after": {"anchor": "created_at", "seconds": 600},
            "session": session_config,
        }

        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=30)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = getattr(e.response, "status_code", "?")
            logger.error(f"Realtime client_secrets mint failed: HTTP {status}")
            raise ValueError(f"OpenAI Realtime-endpoint gaf HTTP {status}") from e
        except Exception as e:
            logger.error(f"Realtime client_secrets mint failed: {e}")
            raise ValueError(f"Kon geen verbinding maken met het Realtime-endpoint: {e}") from e

        try:
            data = r.json()
        except Exception as e:
            logger.error(f"Realtime client_secrets mint returned invalid JSON: {e}")
            raise ValueError("Ongeldig antwoord van het Realtime-endpoint") from e

        return {
            "client_secret": data.get("value"),
            "expires_at": data.get("expires_at"),
            "max_minutes": settings["realtime_max_minutes"],
            "model": session_config["model"],
        }


# Module-level singleton
_realtime_service = None

def get_realtime_service() -> RealtimeService:
    global _realtime_service
    if _realtime_service is None:
        _realtime_service = RealtimeService()
    return _realtime_service
