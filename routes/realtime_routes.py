# routes/realtime_routes.py
"""Realtime voice conversation routes — OpenAI Realtime API over WebRTC.

The backend only mints a short-lived client secret; audio never passes
through Ithaka (the browser connects directly to OpenAI). See
docs/superpowers/specs/2026-09-03-realtime-voice-mode-design.md.
"""

from fastapi import APIRouter, HTTPException, Request
import logging

from services.realtime.realtime_ask import answer_question
from src.auth_helpers import effective_user
from src.settings import get_setting

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

    @router.post("/ask")
    async def ask_ithaka(request: Request):
        """Fase 2: the browser forwards the Realtime model's ask_ithaka
        function call here. Runs the question through the normal agent
        loop (tools/MCP/RAG) one-shot and returns plain text for speech.
        Auth: global AuthMiddleware (like /session); owner = effective_user."""
        owner = effective_user(request)
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail={"message": "body must be a JSON object"})
        question = body.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise HTTPException(status_code=400, detail={"message": "Lege of te lange vraag"})
        if not get_setting("realtime_enabled", False) or not get_setting("realtime_tools_enabled", True):
            raise HTTPException(status_code=400, detail={"message": "Realtime-tools staan uit"})
        try:
            answer = await answer_question(question.strip(), owner)
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail={"message": str(e)})
        except Exception as e:
            logger.error(f"ask_ithaka failed (call_id={body.get('call_id')}): {e}", exc_info=True)
            raise HTTPException(status_code=500, detail={"message": "Opzoeken via Ithaka mislukt"})
        return {"answer": answer}

    return router
