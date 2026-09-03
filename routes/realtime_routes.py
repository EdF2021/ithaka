# routes/realtime_routes.py
"""Realtime voice conversation routes — OpenAI Realtime API over WebRTC.

The backend only mints a short-lived client secret; audio never passes
through Ithaka (the browser connects directly to OpenAI). See
docs/superpowers/specs/2026-09-03-realtime-voice-mode-design.md.
"""

from fastapi import APIRouter, HTTPException
import logging

logger = logging.getLogger(__name__)


def setup_realtime_routes(realtime_service):
    """Setup Realtime voice routes with the provided RealtimeService"""
    router = APIRouter(prefix="/api/realtime", tags=["realtime"])

    @router.post("/session")
    async def create_realtime_session():
        """Mint an ephemeral OpenAI Realtime client secret for the browser
        to open a WebRTC session with. Never returns the underlying
        long-lived API key."""
        try:
            return realtime_service.create_session()
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"message": str(e)})
        except Exception as e:
            logger.error(f"Realtime session mint failed: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"message": "Realtime-sessie starten mislukt"},
            )

    return router
