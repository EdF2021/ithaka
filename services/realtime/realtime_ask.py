"""Fase 2 of the Realtime voice mode: the browser forwards the model's
`ask_ithaka(question)` function call here, and we answer it by running the
question once through Ithaka's normal agent loop (tools, MCP, RAG) on the
background-task / utility model chain. See
docs/superpowers/specs/2026-09-04-realtime-voice-tools-design.md.

Gotcha (CLAUDE.md, Notebooks): this runs inside a tracked foreground
request, so it must never wait on the interactive gate or use
workload="background" — stream_agent_loop's default "foreground" is the
correct, non-deadlocking choice.
"""

import asyncio
import json
import logging
import re

from src.agent_loop import stream_agent_loop
from src.task_endpoint import resolve_task_candidates

logger = logging.getLogger(__name__)

ASK_TIMEOUT_S = 60.0
ASK_MAX_ROUNDS = 6
ASK_MAX_CHARS = 1500

ASK_SYSTEM_PROMPT = (
    "Je bent Ithaka en beantwoordt een vraag die via een gesproken gesprek binnenkomt. "
    "Het antwoord wordt voorgelezen: antwoord in het Nederlands, beknopt (maximaal "
    "ongeveer 80 woorden), als platte lopende tekst zonder markdown, opsommingstekens, "
    "koppen of links. Gebruik je tools (zoeken, notities, agenda, e-mail, documenten) "
    "wanneer de vraag actuele of persoonlijke informatie vereist, en vat het resultaat "
    "samen in plaats van het te citeren. Geef geen denkstappen, alleen het antwoord."
)

_WS = re.compile(r"\s+")


async def _collect(question: str, owner, candidates) -> str:
    url, model, headers = candidates[0]
    fallbacks = list(candidates[1:])
    messages = [
        {"role": "system", "content": ASK_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    parts: list[str] = []
    async for event_str in stream_agent_loop(
        endpoint_url=url,
        model=model,
        messages=messages,
        headers=headers,
        session_id=None,
        owner=owner,
        max_rounds=ASK_MAX_ROUNDS,
        fallbacks=fallbacks,
    ):
        if not event_str.startswith("data: ") or event_str.startswith("data: [DONE]"):
            continue
        try:
            data = json.loads(event_str[6:])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and "delta" in data and not data.get("thinking"):
            parts.append(str(data["delta"]))
    return "".join(parts)


async def answer_question(question: str, owner) -> str:
    """Run `question` through the agent loop once and return plain spoken
    text. Raises ValueError (bad input / no model) or RuntimeError (timeout,
    empty answer) with Dutch messages; the route maps both to HTTP 400."""
    question = (question or "").strip()
    if not question:
        raise ValueError("Lege vraag")

    candidates = resolve_task_candidates(owner=owner)
    if not candidates:
        raise ValueError("Geen model beschikbaar voor ask_ithaka")

    try:
        raw = await asyncio.wait_for(_collect(question, owner, candidates), ASK_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("ask_ithaka timed out after %.0fs (owner=%s)", ASK_TIMEOUT_S, owner)
        raise RuntimeError("Het opzoeken duurde te lang") from None

    text = _WS.sub(" ", raw).strip()
    if not text:
        raise RuntimeError("Ithaka gaf geen antwoord")
    if len(text) > ASK_MAX_CHARS:
        text = text[:ASK_MAX_CHARS].rstrip() + "…"
    return text
