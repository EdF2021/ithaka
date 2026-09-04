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

import asyncio
import logging
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from core.database import Document, Meeting, SessionLocal, utcnow_naive
from src.constants import MEETING_AUDIO_DIR
from src.notebook_language import DUTCH_OUTPUT_RULE
from src.prompt_security import UNTRUSTED_CONTEXT_POLICY, untrusted_context_message
from src.task_endpoint import task_llm_call_async

logger = logging.getLogger(__name__)

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


# ── Job runner (part 2) ──
#
# Async job, same shape as src/notebook_audio.py's podcast job: an in-memory
# _active_jobs dict (lost on restart — a status poll then 404s and the UI
# reports it), asyncio.create_task, a synchronous start_* that validates and
# registers before scheduling. Terminal status is also written into the
# Meeting row (status/phase/error/document_id/finished_at) so the list is
# correct after a restart even though the in-memory job is gone.

JOB_TIMEOUT_SECONDS = 3600

# Terminal (non-running) job entries older than this are dropped the next
# time a job starts — mirrors notebook_audio._JOB_EVICT_AFTER_SECONDS.
_JOB_EVICT_AFTER_SECONDS = 1800

# Everything get_job hands out. Deliberately excludes "task" (an asyncio.Task
# is not JSON-serializable) and "owner" (never echo one user's id to a route).
_PUBLIC_JOB_FIELDS = (
    "status", "phase", "segment", "total", "depth", "error",
    "document_id", "meeting_id", "started_at",
)

# uuid4 + ".webm" — the only filename shape resolve_meeting_audio_path (and
# the janitor) will ever touch. fullmatch, not match/search: with only
# match(), "^...\.webm$" still succeeds against "<uuid>.webm\n" because "$"
# matches just before a trailing newline, and a Linux filename can contain
# one — that would defeat the traversal guard this regex exists for.
MEETING_AUDIO_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.webm$"
)

_active_jobs: dict[str, dict] = {}


def resolve_meeting_audio_path(filename: str) -> Optional[Path]:
    """Return the on-disk path for a meeting-audio filename, or None.

    None unless `filename` fullmatches MEETING_AUDIO_RE (uuid4 + ".webm",
    no slashes/dots that could escape MEETING_AUDIO_DIR) and the file
    actually exists there.
    """
    if not filename or not MEETING_AUDIO_RE.fullmatch(filename):
        return None
    path = Path(MEETING_AUDIO_DIR) / filename
    if not path.is_file():
        return None
    return path


def _reap_stale_jobs(now: float) -> None:
    """Drop terminal job entries older than _JOB_EVICT_AFTER_SECONDS."""
    for job_id, entry in list(_active_jobs.items()):
        if entry.get("status") == "running":
            continue
        completed_at = entry.get("completed_at")
        if completed_at is not None and (now - completed_at) > _JOB_EVICT_AFTER_SECONDS:
            _active_jobs.pop(job_id, None)


def get_job(job_id: str, owner: str) -> Optional[dict]:
    """Return a public snapshot of a job, or None for unknown id / wrong owner."""
    entry = _active_jobs.get(job_id)
    if entry is None:
        return None
    if (entry.get("owner") or "") != (owner or ""):
        return None
    return {field: entry.get(field) for field in _PUBLIC_JOB_FIELDS}


def get_job_for_meeting(meeting_id: str, owner: str) -> Optional[dict]:
    """Return the public snapshot of the running job for this meeting/owner, else None."""
    for entry in _active_jobs.values():
        if (
            entry.get("status") == "running"
            and entry.get("owner") == owner
            and entry.get("meeting_id") == meeting_id
        ):
            return {field: entry.get(field) for field in _PUBLIC_JOB_FIELDS}
    return None


def _set_phase(factory, meeting_id: str, entry: dict, phase: Optional[str], **fields) -> None:
    """Update the in-memory job entry and the Meeting row together.

    `phase` (may be None for a terminal update) and every key in `fields`
    (e.g. status/error/document_id) are written to `entry` first — that is
    what get_job/get_job_for_meeting hand back, and it must stay correct
    even if the DB write below fails. The DB write is then best-effort: a
    transient DB hiccup must not kill a 40-minute job, so any exception here
    is logged and swallowed rather than propagated.
    """
    entry["phase"] = phase
    for key, value in fields.items():
        entry[key] = value
    try:
        session = factory()
        try:
            row = session.query(Meeting).filter(Meeting.id == meeting_id).first()
            if row is not None:
                row.phase = phase
                for key, value in fields.items():
                    if hasattr(row, key):
                        setattr(row, key, value)
                session.commit()
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001 - must never kill the job
        logger.warning("Meeting %s: kon voortgang niet opslaan (%s)", meeting_id, exc)


def start_processing_job(
    meeting_id: str,
    owner: str,
    db_session_factory=None,
    *,
    stt=None,
    llm_call: Optional[CallFn] = None,
    split=None,
) -> str:
    """Validate, register and schedule a minutes job; return its job id.

    Synchronous (like notebook_audio.start_podcast_job) and therefore
    requires a running event loop: it schedules the work with
    asyncio.create_task. Validation order (each raises the error the route
    maps to an HTTP status):
      1. row exists & owner match -> ValueError("Vergadering niet gevonden")
      2. audio_path set and file exists -> ValueError("Geen audio ontvangen")
      3. STT provider disabled/browser or stt_enabled False ->
         RuntimeError("STT niet geconfigureerd")
      4. a job is already running for this meeting ->
         ValueError("Verwerking loopt al")
    """
    factory = db_session_factory or SessionLocal
    now = time.time()
    _reap_stale_jobs(now)

    stt_obj = stt
    if stt_obj is None:
        from services.stt import get_stt_service
        stt_obj = get_stt_service()

    session = factory()
    try:
        row = (
            session.query(Meeting)
            .filter(Meeting.id == meeting_id, Meeting.owner == owner)
            .first()
        )
        if row is None:
            raise ValueError("Vergadering niet gevonden")
        if not row.audio_path or not (Path(MEETING_AUDIO_DIR) / row.audio_path).is_file():
            raise ValueError("Geen audio ontvangen")

        settings = stt_obj._load_settings()
        if settings.get("stt_provider") in ("disabled", "browser") or not settings.get("stt_enabled"):
            raise RuntimeError("STT niet geconfigureerd")

        if get_job_for_meeting(meeting_id, owner) is not None:
            raise ValueError("Verwerking loopt al")

        row.status = "processing"
        row.phase = "splitting"
        row.error = None
        session.commit()
    finally:
        session.close()

    split_fn = split or split_audio
    call = llm_call
    if call is None:
        async def call(messages):  # noqa: ANN001 - matches CallFn shape
            return await task_llm_call_async(
                messages, owner=owner, wait_for_quiet=False, workload="foreground"
            )

    job_id = uuid.uuid4().hex
    entry = {
        "status": "running",
        "phase": "splitting",
        "segment": 0,
        "total": 0,
        "depth": 0,
        "error": None,
        "document_id": None,
        # SECURITY: ownership is tracked so every read can filter by user.
        "owner": owner or "",
        "meeting_id": meeting_id,
        "started_at": now,
        "completed_at": None,
        "task": None,
    }
    _active_jobs[job_id] = entry
    task = asyncio.create_task(
        _run_job(job_id, meeting_id, owner, factory, stt_obj, call, split_fn)
    )
    # Hold the reference: a bare create_task result can be garbage-collected
    # while still running.
    entry["task"] = task
    return job_id


async def _run_job(job_id: str, meeting_id: str, owner: str, factory, stt, call, split) -> None:
    """Job wrapper: wall-clock cap, workdir cleanup and the single place errors are recorded."""
    entry = _active_jobs.get(job_id)
    if entry is None:
        return
    # Computed up front (deterministic from meeting_id) so cleanup in
    # `finally` is correct even if the job fails before the splitting phase
    # ever creates the directory — rmtree(..., ignore_errors=True) is a
    # no-op against a workdir that was never created.
    workdir = Path(MEETING_AUDIO_DIR) / f".meetingjob-{meeting_id}"
    try:
        await asyncio.wait_for(
            _process_job(entry, meeting_id, owner, factory, stt, call, split, workdir),
            timeout=JOB_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        _set_phase(factory, meeting_id, entry, None, status="error", error="Verwerking geannuleerd")
        raise
    except asyncio.TimeoutError:
        logger.error("Meeting job %s timed out after %ss", job_id, JOB_TIMEOUT_SECONDS)
        _set_phase(
            factory, meeting_id, entry, None, status="error",
            error=f"Verwerking duurde langer dan {JOB_TIMEOUT_SECONDS} seconden",
        )
    except Exception as exc:
        logger.error("Meeting job %s failed: %s", job_id, exc, exc_info=True)
        _set_phase(factory, meeting_id, entry, None, status="error", error=str(exc)[:500])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        entry["completed_at"] = time.time()


async def _transcribe_segments(segs: list[Path], entry: dict, stt, key_terms: Optional[str]) -> list[str]:
    """Transcribe every segment under a concurrency cap of 3; deterministic first-failure error.

    Every segment is transcribed regardless of an earlier failure
    (return_exceptions=True) so no to_thread call ever outlives this phase —
    leaving one running past it would let it touch workdir files that the
    caller's `finally` is about to rmtree, and its exception would never be
    retrieved. Results are then scanned in input order so the reported
    "segment i/n" is always the earliest failing segment, independent of
    which one actually finished first.
    """
    sem = asyncio.Semaphore(3)
    prompt = build_stt_prompt(key_terms)
    n = len(segs)

    async def _one(i: int, seg: Path):
        async with sem:
            data = seg.read_bytes()
            text = await asyncio.to_thread(
                stt.transcribe, data, prompt=prompt, timeout=600, filename=seg.name
            )
        # Completed count, not the index: with gather()+Semaphore(3) segment
        # 3 can land before segment 2, so i+1 could move backwards on a
        # poll. No lock needed - no `await` between read and write, so this
        # can't interleave on a single-threaded event loop.
        entry["segment"] = entry.get("segment", 0) + 1
        return text

    results = await asyncio.gather(
        *(_one(i, seg) for i, seg in enumerate(segs)), return_exceptions=True
    )

    texts: list[str] = []
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            # A child CancelledError must propagate as-is (the outer handler
            # records "Verwerking geannuleerd"), not get rewritten below.
            raise result
        if result is None:
            raise RuntimeError(
                f"Transcriptie mislukt (segment {i + 1}/{n}): "
                f"{getattr(stt, 'last_error', None) or 'onbekende fout'}"
            )
        texts.append(result)
    return texts


async def _process_job(
    entry: dict, meeting_id: str, owner: str, factory, stt, call: CallFn, split, workdir: Path
) -> None:
    """The six phases (splitting -> transcribing -> correcting -> condensing -> writing -> saving)."""
    session = factory()
    try:
        row = (
            session.query(Meeting)
            .filter(Meeting.id == meeting_id, Meeting.owner == owner)
            .first()
        )
        if row is None:
            raise RuntimeError("Vergadering niet gevonden")
        title = row.title
        agenda = row.agenda
        key_terms = row.key_terms
        audio_filename = row.audio_path
        created_at = row.created_at
        duration_seconds = row.duration_seconds
    finally:
        session.close()

    if not audio_filename:
        raise RuntimeError("Geen audio ontvangen")
    src = Path(MEETING_AUDIO_DIR) / audio_filename
    if not src.is_file():
        raise RuntimeError("Geen audio ontvangen")

    # ── phase: splitting ──
    _set_phase(factory, meeting_id, entry, "splitting")
    workdir.mkdir(parents=True, exist_ok=True)
    segs = await asyncio.to_thread(split, src, workdir)
    entry["total"] = len(segs)

    # ── phase: transcribing ──
    _set_phase(factory, meeting_id, entry, "transcribing")
    texts = await _transcribe_segments(segs, entry, stt, key_terms)

    # ── phase: correcting ──
    _set_phase(factory, meeting_id, entry, "correcting")
    sem = asyncio.Semaphore(3)

    async def _correct_one(text: str) -> str:
        if not text.strip():
            return ""
        async with sem:
            return await correct_transcript(text, call)

    corrected = await asyncio.gather(*(_correct_one(t) for t in texts))
    transcript = "\n\n".join(c.strip() for c in corrected if c and c.strip())
    if not transcript.strip():
        raise RuntimeError("Geen spraak herkend in de opname")

    # ── phase: condensing ──
    _set_phase(factory, meeting_id, entry, "condensing")
    condensed = await condense_transcript(
        transcript, call, on_depth=lambda d: entry.__setitem__("depth", d)
    )

    # ── phase: writing ──
    _set_phase(factory, meeting_id, entry, "writing")
    date_str = (created_at or datetime.now()).strftime("%d-%m-%Y")
    duration_str = format_duration(duration_seconds)
    minutes, valid = await build_minutes(
        condensed, title=title, agenda=agenda, date_str=date_str,
        duration_str=duration_str, call=call,
    )
    if not valid:
        logger.warning(
            "Meeting %s: notulen-sjabloon niet gevalideerd na retry, toch opgeslagen", meeting_id
        )

    # ── phase: saving ──
    # Not routed through _set_phase: a failed commit here must propagate to
    # the outer handler (row/entry land status="error") rather than being
    # swallowed — a "done" status with no Document would be a lie.
    entry["phase"] = "saving"
    content = (
        render_minutes_header(title=title, date_str=date_str, duration_str=duration_str, agenda=agenda)
        + render_minutes_document(minutes, transcript)
    )
    document_id = str(uuid.uuid4())
    session = factory()
    try:
        session.add(Document(
            id=document_id,
            owner=owner,
            title=f"Notulen – {title} – {date_str}",
            language="markdown",
            current_content=content,
            session_id=None,
        ))
        row = session.query(Meeting).filter(Meeting.id == meeting_id).first()
        if row is not None:
            row.document_id = document_id
            row.status = "done"
            row.phase = None
            row.error = None
            row.finished_at = utcnow_naive()
        session.commit()
    finally:
        session.close()

    entry["status"] = "done"
    entry["document_id"] = document_id
    entry["phase"] = None


# --------------------------------------------------------------------------
# Janitor
#
# Mirrors notebook_audio.cleanup_orphaned_audio, with one extra candidate
# shape: a meeting job's workdir is a *directory* (`.meetingjob-<id>/`), not
# a single tmp file, because splitting writes several segment files into it.
# A hard process kill mid-job leaves that directory behind (nothing else in
# the app ever removes it outside `_run_job`'s own `finally`); a kill right
# after the audio upload finished but before a Meeting row got its
# `audio_path` committed leaves an orphaned `<uuid>.webm`. Both are mtime
# gated by `max_age_seconds` so a job that is still running is never touched.
# --------------------------------------------------------------------------

def cleanup_orphaned_meeting_audio(db_session_factory, *, max_age_seconds: int = 3600) -> tuple[int, int]:
    directory = Path(MEETING_AUDIO_DIR)
    if not directory.is_dir():
        logger.debug("Meeting-janitor: %s bestaat niet, niets te doen", directory)
        return (0, 0)

    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        logger.debug("Meeting-janitor: kon %s niet lezen (%s)", directory, exc)
        return (0, 0)

    # Age-gate before reading the DB, same reasoning as the podcast janitor:
    # shrinks the (already theoretical) publish-vs-commit race window to
    # zero instead of widening it with query latency.
    now = time.time()
    file_candidates: list[tuple[Path, str, int]] = []
    dir_candidates: list[Path] = []
    for path in entries:
        try:
            st = path.stat()
        except OSError:
            continue
        if (now - st.st_mtime) <= max_age_seconds:
            continue
        if path.is_dir():
            if path.name.startswith(".meetingjob-"):
                dir_candidates.append(path)
            continue
        if path.is_file() and MEETING_AUDIO_RE.fullmatch(path.name):
            file_candidates.append((path, path.name, st.st_size))

    if not file_candidates and not dir_candidates:
        logger.debug("Meeting-janitor: niets om op te ruimen")
        return (0, 0)

    session = db_session_factory()
    try:
        referenced_names = {
            row[0]
            for row in session.query(Meeting.audio_path)
            .filter(Meeting.audio_path.isnot(None))
            .all()
        }
    finally:
        session.close()

    removed = 0
    freed = 0
    for path, name, size in file_candidates:
        if name in referenced_names:
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.debug("Meeting-janitor: kon %s niet verwijderen (%s)", name, exc)
            continue
        removed += 1
        freed += size

    for path in dir_candidates:
        try:
            dir_size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        except OSError:
            dir_size = 0
        try:
            shutil.rmtree(path)
        except OSError as exc:
            logger.debug("Meeting-janitor: kon %s niet verwijderen (%s)", path.name, exc)
            continue
        removed += 1
        freed += dir_size

    if removed:
        logger.info(
            "Meeting-janitor: %s verweesd item(en) opgeruimd (%s bytes)", removed, freed,
        )
    else:
        logger.debug("Meeting-janitor: niets om op te ruimen")

    return removed, freed
