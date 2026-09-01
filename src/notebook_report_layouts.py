"""AI-recommended report layouts for the notebook "Rapporten" feature.

Distinct from src/notebook_report.py, which is an unrelated adapter that
renders any notebook artifact through the shared visual-report print-view
pipeline — this module generates and caches the 4 content-aware report
layout *suggestions* shown in the "Aanbevolen indeling" section of the
"Rapport maken" modal, plus the 3 fixed built-in templates shown in the
"Indeling" section.

The actual report generation (turning a chosen layout's instruction into a
markdown Document) happens through the existing generate_artifact pipeline
in src/notebook_artifacts.py with kind="report" — this module only produces
layout *proposals*, it never writes a Document or NotebookArtifact row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from src.notebook_artifacts import (
    _source_entries,
    _strip_think_blocks,
    _VALIDATION_ATTEMPTS,
    gather_source_text,
)
from src.notebook_language import DUTCH_OUTPUT_RULE
from src.prompt_security import UNTRUSTED_CONTEXT_POLICY, untrusted_context_message
from src.task_endpoint import task_llm_call_async

logger = logging.getLogger(__name__)

MAX_SUGGESTIONS = 4

FIXED_TEMPLATES = [
    {
        "key": "overview",
        "title": "Overzichtsdocument",
        "description": "Overzicht van je bronnen met belangrijke inzichten en citaten",
        "instruction": (
            "Schrijf een overzichtsdocument: een heldere samenvatting van de "
            "belangrijkste inzichten uit de bronnen, met per inzicht een korte "
            "toelichting en waar relevant een citaat of concreet voorbeeld uit de bron."
        ),
    },
    {
        "key": "study_material",
        "title": "Studiemateriaal",
        "description": (
            "Quiz met korte antwoorden, voorgestelde essayvragen en woordenlijst "
            "met belangrijke begrippen"
        ),
        "instruction": (
            "Schrijf studiemateriaal: een korte quiz met korte antwoorden, een "
            "aantal voorgestelde essayvragen zonder antwoord, en een woordenlijst "
            "met de belangrijkste begrippen uit de bronnen en hun definitie."
        ),
    },
    {
        "key": "blogpost",
        "title": "Blogpost",
        "description": "Waardevolle inzichten in de vorm van een goed leesbaar artikel",
        "instruction": (
            "Schrijf een blogpost: een goed leesbaar artikel in journalistieke stijl "
            "dat de waardevolste inzichten uit de bronnen toegankelijk overbrengt aan "
            "een lezer die de bronnen niet kent."
        ),
    },
]

_LAYOUT_SUGGESTION_PROMPT = f"""Je bent een assistent die rapportvormen voorstelt op basis van een set bronnen.

Harde regels:
- {DUTCH_OUTPUT_RULE}
- Stel exact 4 rapportvormen voor die aantoonbaar aansluiten bij de daadwerkelijke inhoud van de bronnen hieronder. Verzin geen onderwerpen die niet in de bronnen voorkomen.
- Elke rapportvorm krijgt een korte titel (maximaal 5 woorden), een korte omschrijving van één zin (maximaal 20 woorden) die uitlegt wat het rapport oplevert, en een instructie van 2 tot 4 zinnen die de structuur, stijl en toon van dat rapport beschrijft.
- De 4 rapportvormen moeten onderling verschillen in invalshoek of doel — geen twee bijna-identieke voorstellen.

Lever exact één codefence met taalaanduiding "json" en daarin één JSON-array van 4 objecten, niets anders. Schema:

[
  {{"title": "korte titel", "description": "korte omschrijving", "instruction": "structuur/stijl/toon-instructie van 2 tot 4 zinnen"}}
]

Gebruik geen markdown binnen de JSON-strings; alleen platte tekst."""

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _fingerprint_sources(entries: list[tuple[str, str]]) -> str:
    """Stable hash of a notebook's (filename, text) source pairs —
    order-independent. Mirrors _fingerprint_entries in
    services/memory/memory_extractor.py."""
    items = sorted(entries)
    h = hashlib.sha256()
    for filename, text in items:
        h.update((filename + "\x1f" + text + "\x1e").encode("utf-8"))
    return h.hexdigest()


def _parse_layout_suggestions(content: str) -> list[dict]:
    """Parse the suggestion LLM's reply into a list of {title, description,
    instruction} dicts. Raises ValueError (Dutch, fed back to the model on
    retry) on any format miss — mirrors extract_slide_deck in
    src/notebook_slides.py."""
    m = _JSON_FENCE_RE.search(content or "")
    if not m:
        raise ValueError("geen JSON gevonden in het antwoord")
    raw = m.group(1).strip()
    if not raw:
        raise ValueError("geen JSON gevonden in het antwoord")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"ongeldige JSON: {e}") from e
    if not isinstance(data, list) or not data:
        raise ValueError("JSON is geen niet-lege array")
    data = data[:MAX_SUGGESTIONS]
    cleaned = []
    for i, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"suggestie {i} is geen object")
        title = item.get("title")
        description = item.get("description")
        instruction = item.get("instruction")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f'suggestie {i}: veld "title" ontbreekt of is leeg')
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f'suggestie {i}: veld "description" ontbreekt of is leeg')
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError(f'suggestie {i}: veld "instruction" ontbreekt of is leeg')
        cleaned.append({
            "title": title.strip(),
            "description": description.strip(),
            "instruction": instruction.strip(),
        })
    return cleaned


async def get_recommended_layouts(notebook, db_session, owner: str) -> list[dict]:
    """Return up to 4 AI-recommended report layouts for `notebook`, cached on
    the Notebook row keyed by a fingerprint of its indexed sources.

    Returns [] (no LLM call, no exception) when the notebook has no usable
    sources, or when the model fails to produce valid output after retries —
    the "Rapport maken" modal still shows the fixed templates either way.
    """
    entries = _source_entries(notebook, db_session)
    if not entries:
        return []

    fingerprint = _fingerprint_sources(entries)
    if notebook.report_layouts_fingerprint == fingerprint and notebook.report_layouts_json:
        try:
            cached = json.loads(notebook.report_layouts_json)
            if isinstance(cached, list):
                return cached
        except (json.JSONDecodeError, TypeError):
            pass  # fall through and regenerate

    source_text = gather_source_text(notebook, db_session)
    user_msg = untrusted_context_message(f"notebook-bronnen: {notebook.name}", source_text)
    messages = [
        {"role": "system", "content": f"{UNTRUSTED_CONTEXT_POLICY}\n\n{_LAYOUT_SUGGESTION_PROMPT}"},
        user_msg,
    ]

    content = ""
    last_error = ""
    suggestions: list[dict] = []
    for attempt in range(_VALIDATION_ATTEMPTS):
        attempt_messages = list(messages)
        if attempt > 0:
            attempt_messages.append({"role": "assistant", "content": content})
            attempt_messages.append({
                "role": "user",
                "content": (
                    "Je vorige antwoord voldeed niet aan het gevraagde formaat "
                    f"({last_error}). Lever het antwoord opnieuw, exact volgens de "
                    "instructie hierboven."
                ),
            })
        try:
            content = await task_llm_call_async(
                attempt_messages, owner=owner, wait_for_quiet=False, workload="foreground"
            )
        except Exception as e:
            logger.warning("Report-layout suggestie-call mislukt: %s", e)
            return []
        content = _strip_think_blocks(content or "").strip()
        if not content:
            last_error = "leeg antwoord"
            continue
        try:
            suggestions = _parse_layout_suggestions(content)
            break
        except ValueError as e:
            last_error = str(e)
            logger.info(
                "Report-layout suggesties: formaat-misser op poging %d/%d: %s",
                attempt + 1, _VALIDATION_ATTEMPTS, last_error,
            )
            continue
    else:
        logger.warning(
            "Report-layout suggesties mislukt na %d pogingen: %s",
            _VALIDATION_ATTEMPTS, last_error,
        )
        return []

    notebook.report_layouts_json = json.dumps(suggestions)
    notebook.report_layouts_fingerprint = fingerprint
    db_session.commit()
    return suggestions
