"""Generate a two-voice podcast (audio overview) from a notebook's sources.

Pipeline: LLM writes a two-speaker dialogue script -> every turn is synthesized
separately with its own voice -> the WAV segments are concatenated with the
stdlib `wave` module -> the result is written to NOTEBOOK_AUDIO_DIR under a
uuid4-hex filename. The script itself is stored as a markdown Document (so the
viewer, versioning and export come for free) and tied to its notebook by a
NotebookArtifact with kind="podcast" and audio_path=<filename>.

Design notes (see docs/superpowers/specs/2026-08-17-notebooks-fase3-audio-design.md):

* **Asynchronous job, not a synchronous request.** Generation takes minutes, so
  the shape mirrors src/research_handler.py: an in-memory `_active_jobs` dict,
  `asyncio.create_task`, a POST that returns immediately and a UI that polls.
  Jobs do not survive a restart (the research precedent); a status poll then
  404s and the UI reports it.
* **Session-per-phase.** Unlike Fase 2's generate_artifact (which holds the
  caller's request session across its single LLM call), this job runs for
  minutes in the background, so it opens a session per phase and closes it
  again: parking a pooled connection for half an hour is a real starvation
  risk. Everything the job needs after a phase (the notebook name, the source
  text) is copied out as plain values before the session closes.
* **All WAV, no new dependencies.** Kokoro already emits WAV and the endpoint
  provider is asked for `response_format="wav"`; concatenation is stdlib.
  Segments whose WAV parameters disagree (a provider that ignores the format
  request, or returns stereo/44k1) are a clean job error - no resampling in v1.
* **The synthesizer is injected**, not imported: `set_synthesizer(fn)` takes a
  `(text, voice) -> bytes` callable (app.py/routes pass the existing
  TTSService.synthesize_voice). That keeps this module free of the TTS import
  graph and makes the job testable without a GPU or an endpoint.
* **Voices come from settings only.** `resolve_voices()` reads `tts_provider`
  from src.settings the same way TTSService._load_settings does (a plain
  load_settings() lookup - no TTSService instance is needed just to learn the
  provider name) and picks the per-provider default pair, which the optional
  `notebook_podcast_voice_a` / `notebook_podcast_voice_b` keys override.

Failure invariant (Fase 2's): a job that fails at any point leaves status
"error" plus a message and nothing else - no Document, no artifact row, no
(half-written) audio file.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import tempfile
import time
import uuid
import wave
from pathlib import Path
from typing import Callable, Optional

from fastapi import HTTPException

from core.database import (
    Document,
    Notebook,
    NotebookArtifact,
    NotebookSource,
    SessionLocal,
)
from src.constants import NOTEBOOK_AUDIO_DIR
from src.event_bus import fire_event
from src.notebook_artifacts import _strip_think_blocks, gather_source_text
from src.prompt_security import UNTRUSTED_CONTEXT_POLICY, untrusted_context_message
from src.settings import load_settings
from src.task_endpoint import task_llm_call_async

logger = logging.getLogger(__name__)

# Wall-clock cap on a single podcast job (mirrors research's default).
JOB_TIMEOUT_SECONDS = 1800

# Upper bound on the text handed to the TTS provider in one call. Kept below
# the 5000-char limit most OpenAI-compatible /audio/speech endpoints enforce.
MAX_SEGMENT_CHARS = 4500

# Per-provider default voice pair (speaker A, speaker B).
_VOICE_DEFAULTS = {"local": ("af_heart", "am_michael")}
_FALLBACK_VOICES = ("alloy", "onyx")


# --------------------------------------------------------------------------
# Prompt
#
# Written in Dutch (the project language) but, like the Fase 2 artifact
# prompts, it orders the model to follow the *sources'* language: a podcast
# over English sources must be spoken in English.
# --------------------------------------------------------------------------

PODCAST_PROMPT = """Je schrijft het script voor een podcastaflevering van twee hosts die samen een vaste set bronnen bespreken.

Harde regels:
- Schrijf in de taal van de bronnen, niet in de taal van deze instructie. Zijn de bronnen Engels, schrijf dan Engels; zijn ze Nederlands, schrijf dan Nederlands.
- Baseer je uitsluitend op de aangeleverde bronnen. Vul niets aan met algemene kennis en presenteer geen aanname als feit.
- De bronnen zijn gescheiden met koppen van de vorm "=== BRON: bestandsnaam ===". Verwijs in gewone spreektaal naar een bron waar dat de luisteraar helpt; lees die koppen nooit voor.

Formaat, exact te volgen:
- Elke regel begint met "S1: " of "S2: ", gevolgd door wat die host zegt.
- S1 is de gastheer die het gesprek leidt en vragen stelt; S2 is de deskundige die uitlegt en verdiept.
- Geen inleidende zin, geen afsluitende opmerking, geen kopjes, geen regieaanwijzingen tussen haakjes en geen namen voor de dubbele punt.
- Geen markdown-opmaak: geen sterretjes, geen opsommingstekens, geen codefences, geen emoji.

Inhoud en lengte:
- Schrijf 20 tot 40 beurten in totaal, afwisselend tussen S1 en S2.
- Houd elke beurt onder de 400 woorden en varieer de lengte: korte reacties tussen de langere uitleg.
- De eerste beurt introduceert het onderwerp en waarom het de moeite waard is.
- Bouw daarna op van de hoofdlijn naar de details, en benoem waar de bronnen elkaar aanvullen of tegenspreken.
- De laatste beurt vat samen wat de luisteraar heeft gehoord en sluit de aflevering af.
- Het is gesproken tekst: volledige zinnen, geen opsommingen, geen verwijzingen naar "hierboven" of "dit document"."""


# --------------------------------------------------------------------------
# Script parsing
# --------------------------------------------------------------------------

_SPEAKER_RE = re.compile(r"^\s*(S1|S2)\s*:\s*(.+)$", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def parse_dialogue(script: str) -> list[tuple[str, str]]:
    """Parse a dialogue script into [(speaker, text)] with speaker in S1/S2.

    Reasoning blocks are stripped first (models that emit <think> would
    otherwise get their monologue spoken aloud). Lines without a speaker
    prefix continue the previous turn - models wrap long turns across lines -
    except before the first speaker line, where such text is a preamble
    ("Here is the script:") with no turn to attach to and is dropped.

    Raises RuntimeError when nothing parses: an unusable script must fail the
    job rather than produce a silent or truncated podcast.
    """
    turns: list[list[str]] = []
    for raw_line in _strip_think_blocks(script or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _SPEAKER_RE.match(line)
        if match:
            turns.append([match.group(1).upper(), match.group(2).strip()])
        elif turns:
            turns[-1][1] = f"{turns[-1][1]} {line}".strip()

    parsed = [(speaker, text) for speaker, text in turns if text]
    if not parsed:
        raise RuntimeError(
            "Het model leverde geen bruikbaar dialoogscript op "
            "(geen enkele regel begint met S1: of S2:)"
        )
    return parsed


def _hard_split(text: str, limit: int) -> list[str]:
    """Cut a single over-long sentence on word boundaries within `limit`."""
    chunks = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind(" ")
        if cut <= 0:
            cut = limit  # one unbroken word longer than the limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks or [text]


def split_turn(text: str, limit: int = MAX_SEGMENT_CHARS) -> list[str]:
    """Split one turn into synthesizable pieces of at most `limit` chars.

    Splits on sentence boundaries so the seam between two segments falls where
    a speaker would pause anyway; a single sentence that is itself too long is
    cut on a word boundary as a last resort.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if not sentence:
            continue
        candidate = f"{current} {sentence}" if current else sentence
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        if len(sentence) <= limit:
            current = sentence
        else:
            chunks = _hard_split(sentence, limit)
            parts.extend(chunks[:-1])
            current = chunks[-1]
    if current:
        parts.append(current)
    return parts


# --------------------------------------------------------------------------
# WAV concatenation
# --------------------------------------------------------------------------

def concat_wavs(segments: list[bytes]) -> bytes:
    """Concatenate WAV segments into one WAV, stdlib only.

    Only the format-defining parameters (channels, sample width, frame rate)
    have to agree - nframes differs per segment by definition, so a whole
    getparams() comparison would reject every valid concat. A segment that is
    unreadable (an endpoint that ignored response_format="wav" and returned
    mp3) or formatted differently is a RuntimeError: v1 does not resample.
    """
    if not segments:
        raise RuntimeError("Geen audiofragmenten om samen te voegen")

    params: Optional[tuple[int, int, int]] = None
    frames: list[bytes] = []
    for index, data in enumerate(segments, start=1):
        try:
            with wave.open(io.BytesIO(data), "rb") as reader:
                current = (
                    reader.getnchannels(),
                    reader.getsampwidth(),
                    reader.getframerate(),
                )
                chunk = reader.readframes(reader.getnframes())
        # Deliberately narrow: a MemoryError on a very long podcast must not be
        # reported as "not a readable WAV file".
        except (wave.Error, EOFError, OSError, ValueError) as exc:
            raise RuntimeError(
                f"Audiofragment {index} is geen leesbaar WAV-bestand ({exc}). "
                "Levert de TTS-provider wel WAV terug?"
            ) from exc
        frames.append(chunk)
        if params is None:
            params = current
        elif current != params:
            raise RuntimeError(
                f"Audiofragment {index} heeft afwijkende WAV-parameters "
                f"(kanalen/bits/samplerate {current} tegenover {params}). "
                "Alle fragmenten moeten hetzelfde formaat hebben."
            )

    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(params[0])
        writer.setsampwidth(params[1])
        writer.setframerate(params[2])
        for chunk in frames:
            writer.writeframes(chunk)
    return output.getvalue()


# --------------------------------------------------------------------------
# File serving
# --------------------------------------------------------------------------

NOTEBOOK_AUDIO_RE = re.compile(r"^[a-f0-9]{32}\.wav$")
NOTEBOOK_AUDIO_HEADERS = {
    # Every generation gets a fresh uuid4 filename, so immutable is safe.
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
}


def resolve_notebook_audio_path(filename: str) -> Path:
    """Map a podcast filename to its on-disk path, or raise HTTPException.

    Mirrors src.generated_images.resolve_generated_image_path: a strict
    whitelist regex plus a commonpath guard, and NOTEBOOK_AUDIO_DIR read from
    the module attribute on every call (never bound at import) so tests can
    point it elsewhere. Ownership is checked by the caller via the artifact
    row - this function only proves the path is inside the audio directory.
    """
    if not isinstance(filename, str) or not NOTEBOOK_AUDIO_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    directory = Path(NOTEBOOK_AUDIO_DIR)
    root = directory.resolve()
    path = (directory / filename).resolve()
    try:
        if os.path.commonpath([str(root), str(path)]) != str(root):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Audio not found")
    return path


# --------------------------------------------------------------------------
# Voices and the synthesizer hook
# --------------------------------------------------------------------------

def resolve_voices() -> tuple[str, str]:
    """Return (voice_a, voice_b) for speakers S1 and S2.

    Settings-only lookup: the provider name is read the way
    TTSService._load_settings reads it (load_settings()["tts_provider"]),
    which is all that is needed to pick a sensible default pair - Kokoro voice
    ids and OpenAI-compatible voice ids share no namespace. Blank overrides
    count as absent, and unreadable settings fall back to the endpoint pair
    rather than failing the job here (synthesis itself will report a real
    provider problem with a better message).
    """
    try:
        settings = load_settings() or {}
    except Exception as exc:  # corrupt/unreadable settings must not kill the job
        logger.warning("Could not read settings for podcast voices: %s", exc)
        settings = {}
    provider = str(settings.get("tts_provider") or "").strip()
    default_a, default_b = _VOICE_DEFAULTS.get(provider, _FALLBACK_VOICES)
    voice_a = str(settings.get("notebook_podcast_voice_a") or "").strip() or default_a
    voice_b = str(settings.get("notebook_podcast_voice_b") or "").strip() or default_b
    return voice_a, voice_b


# (text, voice) -> WAV bytes. Injected at app wiring time with
# TTSService.synthesize_voice; tests inject a fake. Kept as a module attribute
# rather than an import so this module never pulls in the TTS stack.
_synthesizer: Optional[Callable[[str, str], bytes]] = None


def set_synthesizer(fn: Optional[Callable[[str, str], bytes]]) -> None:
    """Install (or clear, with None) the synthesis callable used by jobs."""
    global _synthesizer
    _synthesizer = fn


def get_synthesizer() -> Optional[Callable[[str, str], bytes]]:
    return _synthesizer


# --------------------------------------------------------------------------
# Job runner
#
# In-memory only, exactly like ResearchHandler._active_tasks: a restart loses
# running jobs, the status poll 404s and the UI says so.
# --------------------------------------------------------------------------

_active_jobs: dict[str, dict] = {}

# Everything get_job hands out. Deliberately excludes "task" (an asyncio.Task
# is not JSON-serializable) and "owner" (never echo one user's id to a route).
_PUBLIC_JOB_FIELDS = (
    "status", "phase", "segment", "total", "error", "artifact",
    "notebook_id", "started_at",
)


def get_job(job_id: str, owner: str) -> Optional[dict]:
    """Return a snapshot of a job, or None for unknown id / wrong owner."""
    entry = _active_jobs.get(job_id)
    if entry is None:
        return None
    if (entry.get("owner") or "") != (owner or ""):
        return None
    return {field: entry.get(field) for field in _PUBLIC_JOB_FIELDS}


def _transcript_markdown(notebook_name: str, turns: list[tuple[str, str]]) -> str:
    """Render the spoken dialogue as a readable transcript Document."""
    lines = [f"# {notebook_name} — Podcast", ""]
    for speaker, text in turns:
        lines.append(f"**{speaker}:** {text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def start_podcast_job(notebook_id: str, owner: str, db_session_factory=None) -> str:
    """Validate, register and schedule a podcast job; return its job id.

    Synchronous (like ResearchHandler.start_research) and therefore requires a
    running event loop: it schedules the work with asyncio.create_task. The
    validation errors are the ones the route maps to HTTP status codes:
    ValueError("Notebook niet gevonden") -> 404,
    ValueError("Geen geïndexeerde bronnen") -> 400 and
    RuntimeError("TTS niet geconfigureerd") -> 400.

    `db_session_factory` is a callable returning a Session (default
    core.database.SessionLocal); the job opens one per phase through it.
    """
    factory = db_session_factory or SessionLocal

    session = factory()
    try:
        notebook = (
            session.query(Notebook)
            .filter(Notebook.id == notebook_id, Notebook.owner == owner)
            .first()
        )
        if notebook is None:
            raise ValueError("Notebook niet gevonden")
        # Cheap existence probe, same filter as notebook_artifacts._source_entries.
        # The full text is gathered in the job itself, not twice here.
        has_source = (
            session.query(NotebookSource.id)
            .filter(
                NotebookSource.notebook_id == notebook.id,
                NotebookSource.status == "indexed",
                NotebookSource.document_id.isnot(None),
            )
            .first()
            is not None
        )
        if not has_source:
            raise ValueError("Geen geïndexeerde bronnen")
    finally:
        session.close()

    if get_synthesizer() is None:
        raise RuntimeError("TTS niet geconfigureerd")

    job_id = uuid.uuid4().hex
    entry = {
        "status": "running",
        "phase": "script",
        "segment": 0,
        "total": 0,
        "error": None,
        "artifact": None,
        # SECURITY: ownership is tracked so every read can filter by user.
        "owner": owner or "",
        "notebook_id": notebook_id,
        "started_at": time.time(),
        "task": None,
    }
    _active_jobs[job_id] = entry
    task = asyncio.create_task(_run_job(job_id, notebook_id, owner, factory))
    # Hold the reference: a bare create_task result can be garbage-collected
    # while still running.
    entry["task"] = task
    return job_id


async def _run_job(job_id: str, notebook_id: str, owner: str, factory) -> None:
    """Job wrapper: wall-clock cap plus the single place errors are recorded."""
    entry = _active_jobs.get(job_id)
    if entry is None:
        return
    try:
        await asyncio.wait_for(
            _generate(entry, notebook_id, owner, factory),
            timeout=JOB_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        entry["status"] = "cancelled"
        entry["error"] = "Generatie afgebroken"
        raise
    except asyncio.TimeoutError:
        logger.error("Podcast job %s timed out after %ss", job_id, JOB_TIMEOUT_SECONDS)
        entry["status"] = "error"
        entry["error"] = (
            f"Podcast-generatie duurde langer dan {JOB_TIMEOUT_SECONDS} seconden"
        )
    except Exception as exc:
        logger.error("Podcast job %s failed: %s", job_id, exc, exc_info=True)
        entry["status"] = "error"
        entry["error"] = str(exc) or exc.__class__.__name__


async def _generate(entry: dict, notebook_id: str, owner: str, factory) -> None:
    """Script -> per-turn TTS -> concat -> file + rows. Raises on any failure."""
    # ── phase: script ──
    entry["phase"] = "script"
    session = factory()
    try:
        notebook = (
            session.query(Notebook)
            .filter(Notebook.id == notebook_id, Notebook.owner == owner)
            .first()
        )
        if notebook is None:
            raise RuntimeError("Notebook niet gevonden")
        # Copied out as plain values: the session closes before the LLM call.
        notebook_name = notebook.name
        source_text = gather_source_text(notebook, session)
    finally:
        session.close()

    if not source_text:
        raise RuntimeError("Geen geïndexeerde bronnen")

    messages = [
        {"role": "system", "content": f"{UNTRUSTED_CONTEXT_POLICY}\n\n{PODCAST_PROMPT}"},
        untrusted_context_message(f"notebook-bronnen: {notebook_name}", source_text),
    ]
    # This job is user-initiated and interactive: the user is waiting with an
    # open browser and the UI polls for progress. wait_for_quiet=True would
    # park the call until foreground traffic goes quiet - exactly the wrong
    # trade-off here (and the polling itself keeps traffic non-quiet).
    # workload="foreground" tells the local-model slot in src/llm_core.py the
    # same thing, so a local endpoint does not wait on has_foreground_activity()
    # either. Same semantics as deep research and as Fase 2's generate_artifact.
    script = await task_llm_call_async(
        messages, owner=owner, wait_for_quiet=False, workload="foreground"
    )
    turns = parse_dialogue(script or "")

    # ── phase: tts ──
    voice_a, voice_b = resolve_voices()
    plan: list[tuple[str, str]] = []
    for speaker, text in turns:
        voice = voice_a if speaker == "S1" else voice_b
        for chunk in split_turn(text):
            plan.append((chunk, voice))
    if not plan:
        raise RuntimeError("Het script bevat geen tekst om uit te spreken")

    synthesize = get_synthesizer()
    if synthesize is None:
        raise RuntimeError("TTS niet geconfigureerd")

    entry["phase"] = "tts"
    entry["segment"] = 0
    entry["total"] = len(plan)
    audio_segments: list[bytes] = []
    for index, (chunk, voice) in enumerate(plan):
        # The synthesizer is blocking (HTTP call or GPU inference); off the
        # event loop it goes, or the whole app stalls for the job's duration.
        audio_segments.append(await asyncio.to_thread(synthesize, chunk, voice))
        entry["segment"] = index + 1

    # ── phase: concat ──
    entry["phase"] = "concat"
    merged = await asyncio.to_thread(concat_wavs, audio_segments)

    # ── write the file, then the rows ──
    filename = uuid.uuid4().hex + ".wav"
    directory = Path(NOTEBOOK_AUDIO_DIR)
    final_path = directory / filename
    temp_path: Optional[Path] = None
    try:
        # NOTEBOOK_AUDIO_DIR is created eagerly (and guarded) in src.constants;
        # this module never mkdirs. An absent or unwritable directory surfaces
        # here as a clean job error instead of a traceback.
        with tempfile.NamedTemporaryFile(
            dir=str(directory), prefix=".podcast-", suffix=".tmp", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(merged)
        # Atomic publish: no reader ever sees a half-written file.
        os.replace(temp_path, final_path)
    except OSError as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise RuntimeError(
            f"Kon het audiobestand niet opslaan in {directory} ({exc})"
        ) from exc

    session = factory()
    try:
        notebook = (
            session.query(Notebook)
            .filter(Notebook.id == notebook_id, Notebook.owner == owner)
            .first()
        )
        if notebook is None:
            raise RuntimeError("Notebook niet gevonden")
        document_id = str(uuid.uuid4())
        session.add(Document(
            id=document_id,
            title=f"{notebook.name} — Podcast",
            owner=owner,
            language="markdown",
            current_content=_transcript_markdown(notebook.name, turns),
            session_id=None,
        ))
        artifact = NotebookArtifact(
            id=str(uuid.uuid4()),
            notebook_id=notebook.id,
            document_id=document_id,
            kind="podcast",
            audio_path=filename,
        )
        session.add(artifact)
        session.commit()
        # After the commit but before the close: commit expires the attributes
        # and to_dict() needs a live session to refresh them.
        artifact_dict = artifact.to_dict()
    except Exception:
        session.rollback()
        # Rows and file are all-or-nothing: drop the audio we just published.
        try:
            final_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        session.close()

    # After the commit: a failing Library refresh must not undo a stored podcast.
    try:
        fire_event("document_created", owner)
    except Exception as exc:
        logger.warning("document_created event failed for podcast %s: %s",
                       artifact_dict.get("id"), exc)

    entry["artifact"] = artifact_dict
    entry["phase"] = "done"
    entry["status"] = "done"
