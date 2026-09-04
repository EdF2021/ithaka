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
import uuid
from datetime import datetime, timezone
from typing import Optional

from src.agent_loop import stream_agent_loop
from src.task_endpoint import resolve_task_candidates
from src.tool_index import ASSISTANT_ALWAYS_AVAILABLE, get_tool_index

logger = logging.getLogger(__name__)

ASK_TIMEOUT_S = 60.0
ASK_MAX_ROUNDS = 6
ASK_MAX_CHARS = 1500
# One persistent, ordinary chat session per owner backs every voice ask:
# create_document refuses without a session ("No session context"), and a
# pending send_email approval (agent_email_confirm) is only reachable from
# a chat session. Named so the user can find it in the sidebar.
VOICE_SESSION_NAME = "Realtime gesprek"
VOICE_SESSION_FOLDER = "Realtime"

ASK_SYSTEM_PROMPT = (
    "Je bent Ithaka en handelt een vraag of opdracht af die via een gesproken gesprek "
    "binnenkomt. Het antwoord wordt voorgelezen: antwoord in het Nederlands, beknopt "
    "(maximaal ongeveer 80 woorden), als platte lopende tekst zonder markdown, "
    "opsommingstekens, koppen of links. Gebruik je tools (zoeken, notities, agenda, "
    "e-mail, documenten) wanneer de vraag actuele of persoonlijke informatie vereist, en "
    "vat het resultaat samen in plaats van het te citeren. Vraagt de gebruiker een actie "
    "(e-mail versturen, document maken, afspraak plannen, notitie of taak toevoegen), voer "
    "die dan uit met de bijbehorende tool — schrijf zelf een passende tekst als die "
    "ontbreekt, vraag niet om bevestiging — en bevestig kort wat je gedaan hebt. Staat een "
    "e-mail na het versturen klaar ter goedkeuring (pending), zeg dan dat die in de chat "
    f"'{VOICE_SESSION_NAME}' wacht op akkoord. Geef geen denkstappen, alleen het antwoord."
)

_WS = re.compile(r"\s+")

# Tools every voice question can reach regardless of what RAG retrieves:
# the assistant's always-available set (email, calendar, notes, tasks,
# documents, web_search, memory, ...) plus web_fetch for follow-up reads.
ASK_BASE_TOOLS = frozenset(ASSISTANT_ALWAYS_AVAILABLE) | {"web_search", "web_fetch"}
_TOOL_SELECT_TIMEOUT_S = 2.5


async def _select_tools(question: str) -> set[str]:
    """Compose the relevant-tools set for one voice question.

    Passing an explicit, non-empty set matters: a one-turn Dutch question
    trips the agent loop's English-only "low-signal" classifier, and with
    no caller-provided tools the loop takes its direct-reply path — no
    tools at all (observed on prod 2026-09-04: every ask_ithaka call
    answered in ~1 s without web_search/email/calendar). Same pattern as
    src/task_scheduler.py's compose_task_relevant_tools.
    """
    tools = set(ASK_BASE_TOOLS)
    try:
        tool_idx = await asyncio.wait_for(asyncio.to_thread(get_tool_index), _TOOL_SELECT_TIMEOUT_S)
        if tool_idx:
            retrieved = await asyncio.wait_for(
                asyncio.to_thread(tool_idx.get_tools_for_query, question, 8),
                _TOOL_SELECT_TIMEOUT_S,
            )
            tools |= set(retrieved or ())
    except Exception as e:  # index cold / unavailable — base set still covers the common asks
        logger.info("ask_ithaka tool RAG unavailable (%s); using base tool set", e)
    return tools


def _voice_session_id(owner, endpoint_url: str, model: str) -> Optional[str]:
    """Find or create the owner's persistent "Realtime gesprek" chat session.

    Returns None (and logs) if the database is unavailable — the ask then
    still runs, only document creation and email approval degrade.
    """
    from core.database import Session as DbSession
    from src.database import SessionLocal

    db = None
    try:
        db = SessionLocal()
        existing = (
            db.query(DbSession)
            .filter(DbSession.owner == owner, DbSession.name == VOICE_SESSION_NAME)
            .order_by(DbSession.created_at.desc())
            .first()
        )
        if existing:
            return existing.id
        session_id = str(uuid.uuid4())
        mgr = _session_manager()
        if mgr is not None:
            # The manager inserts the DB row AND primes its in-memory cache,
            # so the chat sidebar shows the session immediately.
            mgr.ensure_task_session(session_id, VOICE_SESSION_NAME, endpoint_url, model, owner=owner)
        else:
            now = datetime.now(timezone.utc)
            db.add(DbSession(
                id=session_id,
                name=VOICE_SESSION_NAME,
                endpoint_url=endpoint_url,
                model=model,
                owner=owner,
                folder=VOICE_SESSION_FOLDER,
                created_at=now,
                updated_at=now,
            ))
            db.commit()
        return session_id
    except Exception as e:
        logger.warning("ask_ithaka: could not resolve voice session for %s: %s", owner, e)
        return None
    finally:
        if db is not None:
            db.close()


def _session_manager():
    try:
        from src.ai_interaction import get_session_manager
        return get_session_manager()
    except Exception:
        return None


async def _collect(question: str, owner, candidates) -> str:
    url, model, headers = candidates[0]
    fallbacks = list(candidates[1:])
    messages = [
        {"role": "system", "content": ASK_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    relevant_tools = await _select_tools(question)
    session_id = await asyncio.to_thread(_voice_session_id, owner, url, model)
    parts: list[str] = []
    async for event_str in stream_agent_loop(
        endpoint_url=url,
        model=model,
        messages=messages,
        headers=headers,
        session_id=session_id,
        owner=owner,
        max_rounds=ASK_MAX_ROUNDS,
        fallbacks=fallbacks,
        relevant_tools=relevant_tools,
    ):
        if not event_str.startswith("data: ") or event_str.startswith("data: [DONE]"):
            continue
        try:
            data = json.loads(event_str[6:])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("type") == "tool_start":
            logger.info("ask_ithaka tool_start owner=%s tool=%s", owner, data.get("tool"))
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
