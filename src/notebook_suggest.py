"""Vervolgvraag-suggesties voor notebook-chat (utility-model, best-effort)."""
import asyncio
import json
import logging
import re

from src.task_endpoint import task_llm_call_async

logger = logging.getLogger(__name__)

_SUGGEST_TIMEOUT_S = 8
_JSON_ARRAY_RE = re.compile(r"\[[^\[\]]*\]", re.S)

_PROMPT = (
    "You suggest follow-up questions for a study conversation that is strictly "
    "grounded in a fixed set of sources. Given the user's question and the "
    "assistant's answer, propose exactly 3 short follow-up questions (max 12 "
    "words each, in Dutch) that the sources could "
    "plausibly answer. Reply with ONLY a JSON array of 3 strings."
)


def parse_questions(text):
    """Extract up to 3 question strings from an LLM reply; [] on any failure."""
    m = _JSON_ARRAY_RE.search(text or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = [q.strip() for q in data if isinstance(q, str) and q.strip()]
    return out[:3]


async def suggest_questions(question, answer, owner):
    """Best-effort follow-up questions for a grounded notebook exchange.

    Runs as part of a tracked foreground request, so the two background
    gates must be bypassed (wait_for_quiet=False, workload="foreground") —
    see the notebooks gate-seam regression test.
    """
    messages = [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": f"Question:\n{question[:1000]}\n\nAnswer:\n{answer[:2000]}"},
    ]
    content = await asyncio.wait_for(
        task_llm_call_async(messages, owner=owner, wait_for_quiet=False, workload="foreground"),
        timeout=_SUGGEST_TIMEOUT_S,
    )
    return parse_questions(content)
