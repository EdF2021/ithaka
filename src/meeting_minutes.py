"""Meeting-recorder minutes pipeline — pure functions (part 1 of 2).

This module holds the prompts, the recursive transcript-condensation
algorithm, the strict minutes template + validator, and the ffmpeg audio
splitter used by the meeting-recorder feature. All functions here take an
injected ``call`` (an ``async def call(messages: list[dict]) -> str``) or an
injected ``run`` (``subprocess.run``-shaped), so they are unit-testable
without an LLM endpoint or a real ffmpeg binary.

Task 3 appends the async job runner (``start_processing_job`` /
``_active_jobs`` / phase tracking) that wires these functions to STT, the
database and ``task_llm_call_async``; it also introduces the
``MEETING_AUDIO_DIR`` constant and the ``Meeting`` model import — neither is
used here.

Spec: docs/superpowers/specs/2026-09-04-meeting-recorder-design.md

The prompts and the condense recursion are Ed's original Notulist code
(``berend/app/pages/6_De_Notulist_Demo.py``), ported here verbatim and
augmented with ``DUTCH_OUTPUT_RULE`` (src/notebook_language.py) and
untrusted-context wrapping (src/prompt_security.py) — the same pattern
``src/notebook_audio.py`` uses for podcast source text.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from src.notebook_language import DUTCH_OUTPUT_RULE
from src.prompt_security import UNTRUSTED_CONTEXT_POLICY, untrusted_context_message

CallFn = Callable[[list], Awaitable[str]]

SEGMENT_SECONDS = 600
CONDENSE_NORM = 5000
CONDENSE_CAP = 70000
REQUIRED_HEADINGS = (
    "## Samenvatting",
    "## Besproken punten",
    "## Besluiten",
    "## Actiepunten",
    "## Volgende vergadering",
)

# ── prompts (verbatim from Ed's Notulist code) ──

STT_PROMPT_BASE = "Maak een foutloze transcriptie van het audio bestand."

CORRECT_SYSTEM = (
    "Jij bent een behulpzame assistent. Jouw taak is het corrigeren van "
    "spelling en grammatica fouten in een transcriptie tekst. Voeg enkel "
    "noodzakelijke punctuatie toe, zoals punten, kommas, en voor de "
    "leesbaarheid deel je de tekst op in alinea's. Zorg ervoor dat de tekst "
    "goed leesbaar is en dat de zinnen logisch zijn opgebouwd. Gebruik "
    "enkel de context die je hebt gekregen. Antwoord altijd in het "
    "Nederlands en geef alleen de gecorrigeerde tekst terug, zonder "
    "inleiding of toelichting."
)

CONDENSE_FINAL_SYSTEM = (
    "Jij bent een zeer goede, door AI getrainde, assistent die expert is in "
    "het begrijpen en samenvatten van natuurlijke taal. Lees de volgende "
    "transcript door en maak daar uitgebreide notulen van in juiste "
    "abstracte paragrafen. Het doel is om de belangrijkste gesprekspunten, "
    "besluiten, en afgesproken acties in de notulen te vatten, zodat je een "
    "coherente en leesbaar verslag oplevert, die een persoon kan helpen om "
    "de hoofdpunten, besluiten en acties van het overleg te begrijpen "
    "zonder het volledige transcript te moeten lezen. Vermijd onnodige "
    "details, herhalingen, of niet ter zake doende punten. Gebruik voor de "
    "samenvatting de actieve, tegenwoordige tijd."
)

CONDENSE_PART_SYSTEM = (
    "Jij bent een zeer goede, door AI getrainde, assistent die expert is in "
    "het begrijpen en samenvatten van natuurlijke taal. De volgende tekst "
    "is een deel van een volledig transcript afkomstig van een opgenomen "
    "overleg. Lees de tekst van dit deel door en maak daar uitgebreide "
    "notulen van in juiste abstracte paragrafen zoals Opening en "
    "mededelingen, hoofdpunten, besluiten, en actielijst, volgende "
    "vergadering. Het doel is om de belangrijkste gesprekspunten in de "
    "notulen te vatten, zodat je een coherente en leesbaar verslag "
    "oplevert, die een persoon kan helpen om de hoofdpunten, de besluiten, "
    "de actielijst, enz. van het overleg te begrijpen, zonder het "
    "volledige transcript te moeten lezen. Onthoud dat dit een "
    "transcriptdeel is en dat mogelijk pas in het volgende deel zaken als "
    "de rondvraag, afsluiting en conclusie worden behandeld indien deze "
    "niet in dit deel voorkomen. Vermijd onnodige details, herhalingen, of "
    "niet ter zake doende punten. Gebruik voor de samenvatting de actieve, "
    "tegenwoordige tijd."
)

MINUTES_SYSTEM = """Je bent een ervaren notulist. Je krijgt een samenvatting (of transcript) van een vergadering en schrijft daar formele notulen van, EXACT volgens dit Markdown-sjabloon. Gebruik precies deze koppen, in deze volgorde, en voeg geen andere koppen toe. Ontbreekt informatie voor een onderdeel, schrijf dan "Geen." onder die kop. Verzin niets dat niet in de bron staat. Schrijf in de actieve, tegenwoordige tijd.

## Samenvatting
(3-8 zinnen: doel van de vergadering en de belangrijkste uitkomsten)

## Besproken punten
(genummerde lijst; per punt 1-3 zinnen; volg de agenda als die gegeven is)

## Besluiten
(opsommingslijst; elk besluit één regel, concreet geformuleerd)

## Actiepunten
| Actie | Eigenaar | Deadline |
|---|---|---|
(één rij per actie; eigenaar/deadline "-" als onbekend)

## Volgende vergadering
(datum/tijd/onderwerpen als genoemd, anders "Geen.")

Begin je antwoord direct met "## Samenvatting". Geen inleiding, geen titel, geen afsluiting."""


def build_stt_prompt(key_terms: Optional[str]) -> str:
    """STT prompt; appends the key-terms sentence only when terms are given."""
    if key_terms and key_terms.strip():
        return (
            STT_PROMPT_BASE
            + " In het audiobestand worden de volgende afkortingen, namen, "
            "jargon, gebruikt: " + key_terms.strip()
        )
    return STT_PROMPT_BASE


def condense_split_for_depth(depth: int) -> int:
    """Growing head-size per recursion depth, capped at CONDENSE_CAP."""
    return min(CONDENSE_CAP, int(CONDENSE_NORM + CONDENSE_NORM * depth / 2))


def _messages(system: str, user_text: str) -> list[dict]:
    """System = untrusted-context policy + prompt + DUTCH_OUTPUT_RULE; user = wrapped source text."""
    return [
        {
            "role": "system",
            "content": f"{UNTRUSTED_CONTEXT_POLICY}\n\n{system}\n\n{DUTCH_OUTPUT_RULE}",
        },
        untrusted_context_message("transcript", user_text),
    ]


async def correct_transcript(text: str, call: CallFn) -> str:
    """Spelling/grammar pass over one segment; falls back to the raw text on any error."""
    try:
        reply = await call(_messages(CORRECT_SYSTEM, text))
        return reply.strip()
    except Exception:
        return text


async def condense_transcript(
    text: str,
    call: CallFn,
    *,
    depth: int = 0,
    carry: str = "",
    on_depth: Optional[Callable[[int], Any]] = None,
) -> str:
    """Ed's head/tail recursion: condense in growing chunks, carrying the previous partial forward."""
    work = (
        (carry.strip() + "\n\n" + text.strip()).strip() if carry else text.strip()
    )
    if not work:
        return ""
    if on_depth:
        on_depth(depth)
    split = condense_split_for_depth(depth)
    if len(work) <= split:
        return (await call(_messages(CONDENSE_FINAL_SYSTEM, work))).strip()
    head, tail = work[:split], work[split:]
    partial = (await call(_messages(CONDENSE_PART_SYSTEM, head))).strip()
    return await condense_transcript(
        tail, call, depth=depth + 1, carry=partial, on_depth=on_depth
    )


def minutes_system_prompt() -> str:
    return f"{UNTRUSTED_CONTEXT_POLICY}\n\n{MINUTES_SYSTEM}\n\n{DUTCH_OUTPUT_RULE}"


def minutes_user_message(
    *,
    condensed: str,
    title: str,
    agenda: Optional[str],
    date_str: str,
    duration_str: str,
) -> dict:
    content = f"Titel: {title}\nDatum: {date_str}\nDuur: {duration_str}\n"
    if agenda:
        content += "Agenda:\n" + agenda + "\n\n"
    content += "Bron:\n" + condensed
    return untrusted_context_message("vergadering: " + title, content)


def validate_minutes(md: str) -> list[str]:
    """Missing/misordered required headings + missing action-item table -> list of error strings."""
    errors = []
    prev_idx = -1
    for heading in REQUIRED_HEADINGS:
        match = re.search(rf"^{re.escape(heading)}\s*$", md, re.M)
        if not match:
            errors.append(f"ontbreekt: {heading}")
            continue
        idx = match.start()
        if idx <= prev_idx:
            errors.append(f"volgorde: {heading}")
        else:
            prev_idx = idx
    if not re.search(
        r"\|\s*Actie\s*\|\s*Eigenaar\s*\|\s*Deadline\s*\|", md, re.I
    ):
        errors.append("ontbreekt: actiepuntentabel")
    return errors


async def build_minutes(
    condensed: str,
    *,
    title: str,
    agenda: Optional[str],
    date_str: str,
    duration_str: str,
    call: CallFn,
) -> tuple[str, bool]:
    """One call against the strict template; one corrective retry if it doesn't validate."""
    messages = [
        {"role": "system", "content": minutes_system_prompt()},
        minutes_user_message(
            condensed=condensed,
            title=title,
            agenda=agenda,
            date_str=date_str,
            duration_str=duration_str,
        ),
    ]
    first = (await call(messages)).strip()
    errors = validate_minutes(first)
    if not errors:
        return first, True

    retry_messages = messages + [
        {"role": "assistant", "content": first},
        {
            "role": "user",
            "content": "Je antwoord volgt het sjabloon niet: "
            + "; ".join(errors)
            + ". Geef de volledige notulen opnieuw, exact volgens het "
            "sjabloon, beginnend met '## Samenvatting'.",
        },
    ]
    second = (await call(retry_messages)).strip()
    if not validate_minutes(second):
        return second, True
    return first, False


def render_minutes_header(
    *, title: str, date_str: str, duration_str: str, agenda: Optional[str]
) -> str:
    header = (
        f"# Notulen: {title}\n\n"
        f"**Datum:** {date_str}  ·  **Duur:** {duration_str}  ·  **Opname:** Ithaka\n\n"
    )
    if agenda:
        header += "## Agenda\n\n" + agenda + "\n\n"
    return header


def render_minutes_document(minutes_md: str, transcript: str) -> str:
    return minutes_md.rstrip() + "\n\n## Bijlage: transcript\n\n" + transcript.strip() + "\n"


def format_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "onbekend"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours} u {minutes} min"
    return f"{minutes} min"


def split_audio(
    src: Path,
    workdir: Path,
    *,
    segment_seconds: int = SEGMENT_SECONDS,
    run=subprocess.run,
) -> list[Path]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg niet gevonden")
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        str(workdir / "seg_%03d.ogg"),
    ]
    result = run(cmd, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise RuntimeError(
            "Audio kon niet worden gesplitst: " + (result.stderr or "")[-400:]
        )
    segs = sorted(workdir.glob("seg_*.ogg"))
    if not segs:
        raise RuntimeError("Audio kon niet worden gesplitst: geen segmenten")
    return segs
