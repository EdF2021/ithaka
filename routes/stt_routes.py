# routes/stt_routes.py
"""STT API routes — multi-provider (local Whisper, API endpoint, browser)."""

from fastapi import APIRouter, HTTPException, UploadFile, File
import logging

from src.upload_limits import read_upload_limited, STT_MAX_AUDIO_BYTES

logger = logging.getLogger(__name__)


def setup_stt_routes(stt_service):
    """Setup STT routes with the provided STT service"""
    router = APIRouter(prefix="/api/stt", tags=["stt"])

    @router.get("/stats")
    async def get_stt_stats():
        """Get STT service statistics"""
        try:
            return stt_service.get_stats()
        except Exception as e:
            logger.error(f"Failed to get STT stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/transcribe")
    async def transcribe_audio(file: UploadFile = File(...)):
        """Transcribe uploaded audio file to text"""
        try:
            if not stt_service.available:
                raise HTTPException(
                    status_code=503,
                    detail={"message": "STT service not available or set to browser mode"}
                )

            audio_bytes = await read_upload_limited(file, STT_MAX_AUDIO_BYTES, "Audio file")
            if not audio_bytes:
                raise HTTPException(status_code=400, detail={"message": "Empty audio file"})

            text = stt_service.transcribe(audio_bytes)
            if text is None:
                # last_error carries the upstream reason (HTTP status,
                # network failure, ...) when the provider's a remote API
                # endpoint — surface it instead of a bare "failed" (the
                # 2026-09-02 incident: a chat-only endpoint 500'd every
                # transcription with no clue why).
                reason = getattr(stt_service, "last_error", None)
                message = f"Transcription failed: {reason}" if reason else "Transcription failed"
                raise HTTPException(
                    status_code=500,
                    detail={"message": message}
                )

            return {"text": text}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Transcription error: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"message": f"Transcription failed: {str(e)}"}
            )

    return router
