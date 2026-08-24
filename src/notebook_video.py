# src/notebook_video.py
"""Video overview for a notebook: slide deck + voice-over -> mp4.

NotebookLM-style "video overview" as a narrated slideshow, deliberately not
generative video (see docs/superpowers/specs/2026-08-22-notebooks-fase4-
design.md). The async job mirrors src/notebook_audio.py phase for phase:

  script  - LLM writes slide JSON (title/bullets/narration) via the strict
            extract_slide_deck(require_narration=True) validator, with the
            same feed-the-error-back retry as the podcast script.
  render  - each slide becomes a 1280x720 PNG via Pillow (dark theme,
            bundled/system font, plain deterministic layout).
  tts     - each slide's narration through the injected synthesizer
            (TTSService.synthesize_voice), chunked with the podcast's
            split_turn and concatenated per slide.
  compose - ffmpeg: one still-image+audio segment per slide, then a concat
            of the uniformly encoded segments into one mp4.

Storage follows the podcast pattern exactly: work happens in a hidden
tempdir inside NOTEBOOK_VIDEO_DIR, the mp4 is published atomically with
os.replace, and only then are the Document (readable script) and
NotebookArtifact (kind="video", video_path=<hex>.mp4) rows committed.
In-memory job store only - a restart loses running jobs, the poll 404s.

ffmpeg is an external binary (added to the Dockerfile for this feature);
its absence is a clean job-start error, never a traceback.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from fastapi import HTTPException

from core.database import Document, Notebook, NotebookArtifact, SessionLocal
from src.constants import NOTEBOOK_VIDEO_DIR
from src.event_bus import fire_event
from src.notebook_artifacts import gather_source_text
from src.notebook_audio import concat_wavs_to_file, resolve_voices, split_turn
from src.notebook_language import DUTCH_OUTPUT_RULE
from src.notebook_slides import extract_slide_deck
from src.prompt_security import UNTRUSTED_CONTEXT_POLICY, untrusted_context_message
from src.task_endpoint import task_llm_call_async

logger = logging.getLogger(__name__)

JOB_TIMEOUT_SECONDS = 1800
_SCRIPT_FORMAT_ATTEMPTS = 3
_FFMPEG_TIMEOUT_SECONDS = 300

VIDEO_SIZE = (1280, 720)

VIDEO_PROMPT = f"""Je bent scenarist van een korte uitlegvideo op basis van een vaste set bronnen.

Harde regels:
- {DUTCH_OUTPUT_RULE}
- Baseer je uitsluitend op de aangeleverde bronnen; verzin niets.
- De bronnen zijn gescheiden met koppen van de vorm "=== BRON: bestandsnaam ===".

Maak een video van 5 tot 10 slides. Lever exact één codefence met taalaanduiding "json" en daarin één JSON-object, niets anders. Schema:

{{
  "title": "Nederlandse videotitel",
  "slides": [
    {{
      "title": "slidetitel",
      "bullets": ["punt een", "punt twee"],
      "narration": "de voice-over die bij deze slide wordt uitgesproken"
    }}
  ]
}}

Regels:
- Per slide 2 tot 4 bullets van elk maximaal 10 woorden.
- "narration" is drie tot zes gesproken zinnen: vloeiend, op zichzelf begrijpelijk, geen letterlijke herhaling van de bullets.
- De eerste slide introduceert het onderwerp, de laatste sluit af met de kernboodschap.
- Geen markdown of HTML binnen de JSON-strings."""

_SCRIPT_FORMAT_CORRECTION = (
    "Je vorige antwoord voldeed niet aan het gevraagde formaat. Lever het "
    "antwoord opnieuw: exact één codefence met taalaanduiding \"json\" en "
    "daarin één JSON-object volgens het schema, met per slide een gevulde "
    "\"narration\"."
)


# --------------------------------------------------------------------------
# Slide rendering (Pillow)
# --------------------------------------------------------------------------

# Dark-theme palette for the rendered frames. Deliberately hardcoded hexes
# (like the standalone artifact templates in notebook_flashcards/-_slides):
# the video is a rendered file, not themed UI, so CSS variables don't apply.
_BG = (22, 24, 29)
_PANEL = (31, 34, 41)
_FG = (230, 230, 230)
_MUTED = (150, 155, 165)
_ACCENT = (224, 108, 117)

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)
_FONT_REGULAR_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)


def _load_font(size: int, *, bold: bool = False):
    """Best available TrueType font, PIL bitmap default as last resort."""
    from PIL import ImageFont

    candidates = _FONT_CANDIDATES if bold else _FONT_REGULAR_CANDIDATES
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # older Pillow without size kwarg
        return ImageFont.load_default()


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    """Greedy word wrap measured against the actual font metrics."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        probe = f"{current} {word}".strip()
        if draw.textlength(probe, font=font) <= max_width or not current:
            current = probe
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_slide_png(slide: dict, index: int, total: int) -> bytes:
    """Render one slide dict ({title, bullets}) to PNG bytes (1280x720)."""
    from PIL import Image, ImageDraw

    width, height = VIDEO_SIZE
    img = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(img)

    margin = 90
    content_width = width - 2 * margin

    # Panel backdrop
    draw.rounded_rectangle(
        [40, 40, width - 40, height - 40], radius=18, fill=_PANEL,
    )

    title_font = _load_font(48, bold=True)
    bullet_font = _load_font(30)
    meta_font = _load_font(20)

    y = 110
    for line in _wrap_text(draw, slide.get("title", ""), title_font, content_width)[:3]:
        draw.text((margin, y), line, font=title_font, fill=_FG)
        y += 62
    # Accent rule under the title
    draw.rectangle([margin, y + 6, margin + 220, y + 10], fill=_ACCENT)
    y += 48

    for bullet in slide.get("bullets", [])[:6]:
        wrapped = _wrap_text(draw, bullet, bullet_font, content_width - 40)
        draw.ellipse([margin + 2, y + 12, margin + 14, y + 24], fill=_ACCENT)
        for j, line in enumerate(wrapped[:3]):
            draw.text((margin + 32, y), line, font=bullet_font, fill=_FG)
            y += 42
        y += 12
        if y > height - 130:
            break

    # Footer: progress
    draw.text((margin, height - 86), f"{index} / {total}", font=meta_font, fill=_MUTED)
    bar_width = int((width - 2 * margin) * (index / max(total, 1)))
    draw.rectangle([margin, height - 58, margin + bar_width, height - 54], fill=_ACCENT)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def segment_command(png: str, wav: str, out_mp4: str) -> list[str]:
    """ffmpeg argv for one still-image + audio segment.

    Uniform encode parameters across segments on purpose: the concat step
    stream-copies (`-c copy`), which is only valid when every segment shares
    codec/size/rate.
    """
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-i", png,
        "-i", wav,
        "-c:v", "libx264", "-tune", "stillimage", "-r", "30",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "24000",
        "-shortest",
        out_mp4,
    ]


def concat_command(list_file: str, out_mp4: str) -> list[str]:
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy",
        out_mp4,
    ]


async def _run_ffmpeg(cmd: list[str]) -> None:
    """Run one ffmpeg command; RuntimeError with stderr tail on failure.

    Same subprocess discipline as services/youtube/youtube_handler.py: the
    timeout wraps communicate(), and on expiry the child is killed and
    reaped so no zombie lingers.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_FFMPEG_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(
            f"ffmpeg overschreed de tijdslimiet van {_FFMPEG_TIMEOUT_SECONDS}s"
        )
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace").strip()[-500:]
        raise RuntimeError(f"ffmpeg faalde (exit {proc.returncode}): {tail}")


# --------------------------------------------------------------------------
# File serving
# --------------------------------------------------------------------------

NOTEBOOK_VIDEO_RE = re.compile(r"^[a-f0-9]{32}\.mp4$")
NOTEBOOK_VIDEO_HEADERS = {
    # Every generation gets a fresh uuid4 filename, so immutable is safe.
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
}


def resolve_notebook_video_path(filename: str) -> Path:
    """Map a video filename to its on-disk path, or raise HTTPException.

    Mirror of notebook_audio.resolve_notebook_audio_path: whitelist regex +
    commonpath guard; NOTEBOOK_VIDEO_DIR read from the module attribute on
    every call so tests can point it elsewhere. Ownership is the caller's
    job (artifact-row join) - this only proves the path stays inside the
    video directory.
    """
    if not isinstance(filename, str) or not NOTEBOOK_VIDEO_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    directory = Path(NOTEBOOK_VIDEO_DIR)
    root = directory.resolve()
    path = (directory / filename).resolve()
    try:
        if os.path.commonpath([str(root), str(path)]) != str(root):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    return path


# --------------------------------------------------------------------------
# Synthesizer injection (same seam as notebook_audio)
# --------------------------------------------------------------------------

_synthesizer: Optional[Callable[[str, str], bytes]] = None


def set_synthesizer(fn: Optional[Callable[[str, str], bytes]]) -> None:
    global _synthesizer
    _synthesizer = fn


def get_synthesizer() -> Optional[Callable[[str, str], bytes]]:
    return _synthesizer


# --------------------------------------------------------------------------
# Job runner (in-memory, mirrors notebook_audio._active_jobs)
# --------------------------------------------------------------------------

_active_jobs: dict[str, dict] = {}
_JOB_EVICT_AFTER_SECONDS = 1800

_PUBLIC_JOB_FIELDS = (
    "status", "phase", "segment", "total", "error", "artifact",
    "notebook_id", "started_at", "script_attempt",
)


def _reap_stale_jobs(now: float) -> None:
    for job_id, entry in list(_active_jobs.items()):
        if entry.get("status") == "running":
            continue
        completed_at = entry.get("completed_at")
        if completed_at is not None and (now - completed_at) > _JOB_EVICT_AFTER_SECONDS:
            _active_jobs.pop(job_id, None)


def get_job(job_id: str, owner: str) -> Optional[dict]:
    entry = _active_jobs.get(job_id)
    if entry is None:
        return None
    if (entry.get("owner") or "") != (owner or ""):
        return None
    return {field: entry.get(field) for field in _PUBLIC_JOB_FIELDS}


def _script_markdown(notebook_name: str, deck: dict) -> str:
    """Render the video script as a readable Document (transcript analog)."""
    lines = [f"# {deck.get('title') or notebook_name} — Video", ""]
    for i, slide in enumerate(deck.get("slides", []), start=1):
        lines.append(f"## {i}. {slide['title']}")
        lines.append("")
        for bullet in slide.get("bullets", []):
            lines.append(f"- {bullet}")
        if slide.get("bullets"):
            lines.append("")
        narration = slide.get("narration", "")
        if narration:
            lines.append(f"*{narration}*")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def start_video_job(notebook_id: str, owner: str, db_session_factory=None) -> str:
    """Validate, register and schedule a video job; return its job id.

    Same error contract as start_podcast_job: ValueError("Notebook niet
    gevonden") -> 404, ValueError("Geen geïndexeerde bronnen") -> 400,
    RuntimeError("TTS niet geconfigureerd") / ffmpeg-missing -> 400.
    """
    factory = db_session_factory or SessionLocal

    now = time.time()
    _reap_stale_jobs(now)
    for entry in _active_jobs.values():
        if (entry.get("status") == "running"
                and entry.get("owner") == owner
                and entry.get("notebook_id") == notebook_id):
            raise ValueError("Er loopt al een video-generatie voor dit notebook")

    session = factory()
    try:
        notebook = (
            session.query(Notebook)
            .filter(Notebook.id == notebook_id, Notebook.owner == owner)
            .first()
        )
        if notebook is None:
            raise ValueError("Notebook niet gevonden")
        if not gather_source_text(notebook, session):
            raise ValueError("Geen geïndexeerde bronnen")
    finally:
        session.close()

    if get_synthesizer() is None:
        raise RuntimeError("TTS niet geconfigureerd")
    if not ffmpeg_available():
        raise RuntimeError(
            "Video vereist ffmpeg in de server-omgeving; het is niet gevonden"
        )

    job_id = uuid.uuid4().hex
    entry = {
        "status": "running",
        "phase": "script",
        "segment": 0,
        "total": 0,
        "error": None,
        "artifact": None,
        "owner": owner or "",
        "notebook_id": notebook_id,
        "started_at": now,
        "completed_at": None,
        "script_attempt": 0,
        "task": None,
    }
    _active_jobs[job_id] = entry
    task = asyncio.create_task(_run_job(job_id, notebook_id, owner, factory))
    entry["task"] = task
    return job_id


async def _run_job(job_id: str, notebook_id: str, owner: str, factory) -> None:
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
        logger.error("Video job %s timed out after %ss", job_id, JOB_TIMEOUT_SECONDS)
        entry["status"] = "error"
        entry["error"] = (
            f"Video-generatie duurde langer dan {JOB_TIMEOUT_SECONDS} seconden"
        )
    except Exception as exc:
        logger.error("Video job %s failed: %s", job_id, exc, exc_info=True)
        entry["status"] = "error"
        entry["error"] = str(exc) or exc.__class__.__name__
    finally:
        entry["completed_at"] = time.time()


async def _generate(entry: dict, notebook_id: str, owner: str, factory) -> None:
    """script -> render -> tts -> compose -> rows. Raises on any failure."""
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
        notebook_name = notebook.name
        source_text = gather_source_text(notebook, session)
    finally:
        session.close()

    if not source_text:
        raise RuntimeError("Geen geïndexeerde bronnen")

    messages = [
        {"role": "system", "content": f"{UNTRUSTED_CONTEXT_POLICY}\n\n{VIDEO_PROMPT}"},
        untrusted_context_message(f"notebook-bronnen: {notebook_name}", source_text),
    ]
    deck: Optional[dict] = None
    last_error: Optional[Exception] = None
    attempt_messages = list(messages)
    for attempt in range(_SCRIPT_FORMAT_ATTEMPTS):
        entry["script_attempt"] = attempt + 1
        script = await task_llm_call_async(
            attempt_messages, owner=owner, wait_for_quiet=False, workload="foreground"
        )
        try:
            deck = extract_slide_deck(script or "", require_narration=True)
            break
        except ValueError as exc:
            last_error = exc
            if attempt == _SCRIPT_FORMAT_ATTEMPTS - 1:
                break
            logger.warning(
                "Video script attempt %d/%d invalid (%s); retrying with a "
                "format correction", attempt + 1, _SCRIPT_FORMAT_ATTEMPTS, exc,
            )
            attempt_messages = messages + [
                {"role": "assistant", "content": (script or "")[:4000]},
                {"role": "user", "content": f"{_SCRIPT_FORMAT_CORRECTION}\n\nFout: {exc}"},
            ]
    if deck is None:
        raise RuntimeError(
            f"Het model leverde geen bruikbaar videoscript op ({last_error})"
        )

    slides = deck["slides"]
    synthesize = get_synthesizer()
    if synthesize is None:
        raise RuntimeError("TTS niet geconfigureerd")
    voice = resolve_voices()[0]

    directory = Path(NOTEBOOK_VIDEO_DIR)
    filename = uuid.uuid4().hex + ".mp4"
    final_path = directory / filename

    # All intermediates live in one hidden workdir inside the video dir so a
    # crash leaves a single .videojob-* directory for the janitor to sweep.
    workdir = Path(tempfile.mkdtemp(dir=str(directory), prefix=".videojob-"))
    try:
        # ── phase: render ──
        entry["phase"] = "render"
        entry["segment"] = 0
        entry["total"] = len(slides)
        png_paths: list[Path] = []
        for i, slide in enumerate(slides, start=1):
            data = await asyncio.to_thread(render_slide_png, slide, i, len(slides))
            p = workdir / f"slide{i:03d}.png"
            p.write_bytes(data)
            png_paths.append(p)
            entry["segment"] = i

        # ── phase: tts ──
        entry["phase"] = "tts"
        entry["segment"] = 0
        wav_paths: list[Path] = []
        for i, slide in enumerate(slides, start=1):
            chunks = split_turn(slide["narration"])
            segments = []
            for chunk in chunks:
                segments.append(await asyncio.to_thread(synthesize, chunk, voice))
            p = workdir / f"audio{i:03d}.wav"
            await asyncio.to_thread(concat_wavs_to_file, segments, p)
            wav_paths.append(p)
            entry["segment"] = i

        # ── phase: compose ──
        entry["phase"] = "compose"
        entry["segment"] = 0
        seg_paths: list[Path] = []
        for i, (png, wav) in enumerate(zip(png_paths, wav_paths), start=1):
            seg = workdir / f"seg{i:03d}.mp4"
            await _run_ffmpeg(segment_command(str(png), str(wav), str(seg)))
            seg_paths.append(seg)
            entry["segment"] = i

        list_file = workdir / "concat.txt"
        # concat-demuxer syntax; paths are ours (hex names in our workdir).
        list_file.write_text("".join(f"file '{p}'\n" for p in seg_paths))
        out_path = workdir / "out.mp4"
        await _run_ffmpeg(concat_command(str(list_file), str(out_path)))
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg leverde een leeg videobestand op")

        # Atomic publish (same directory, so replace stays atomic).
        os.replace(out_path, final_path)
    except OSError as exc:
        raise RuntimeError(
            f"Kon het videobestand niet opslaan in {directory} ({exc})"
        ) from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # ── rows (all-or-nothing with the published file) ──
    session = factory()
    artifact: Optional[NotebookArtifact] = None
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
            title=f"{notebook.name} — Video",
            owner=owner,
            language="markdown",
            current_content=_script_markdown(notebook.name, deck),
            session_id=None,
        ))
        artifact = NotebookArtifact(
            id=str(uuid.uuid4()),
            notebook_id=notebook.id,
            document_id=document_id,
            kind="video",
            title=f"{notebook.name} — Video",
            video_path=filename,
        )
        session.add(artifact)
        session.commit()
    except Exception:
        session.rollback()
        session.close()
        try:
            final_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    try:
        artifact_dict = artifact.to_dict()
    finally:
        session.close()

    try:
        fire_event("document_created", owner)
    except Exception as exc:
        logger.warning("document_created event failed for video %s: %s",
                       artifact_dict.get("id"), exc)

    entry["artifact"] = artifact_dict
    entry["phase"] = "done"
    entry["status"] = "done"


# --------------------------------------------------------------------------
# Janitor (mirrors notebook_audio.cleanup_orphaned_audio)
# --------------------------------------------------------------------------

def cleanup_orphaned_video(db_session_factory, *, max_age_seconds: int = 3600) -> tuple[int, int]:
    """Sweep stale .videojob-* workdirs and orphaned <hex>.mp4 files.

    Returns (tmp_removed, orphans_removed). Everything younger than
    `max_age_seconds` is left alone (a job mid-write or just-published but
    not-yet-committed must never be touched) - the age check happens before
    the DB query, same publish-vs-commit-race reasoning as the audio janitor.
    """
    directory = Path(NOTEBOOK_VIDEO_DIR)
    if not directory.is_dir():
        return (0, 0)
    now = time.time()
    tmp_removed = 0
    orphan_candidates: list[Path] = []

    for path in directory.iterdir():
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age <= max_age_seconds:
            continue
        if path.name.startswith(".videojob-") and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            tmp_removed += 1
        elif path.is_file() and NOTEBOOK_VIDEO_RE.fullmatch(path.name):
            orphan_candidates.append(path)

    orphans_removed = 0
    if orphan_candidates:
        session = db_session_factory()
        try:
            referenced = {
                row[0]
                for row in session.query(NotebookArtifact.video_path)
                .filter(NotebookArtifact.video_path.isnot(None))
                .all()
            }
        finally:
            session.close()
        for path in orphan_candidates:
            if path.name not in referenced:
                try:
                    path.unlink(missing_ok=True)
                    orphans_removed += 1
                except OSError:
                    pass

    if tmp_removed or orphans_removed:
        logger.info(
            "Notebook-video janitor: removed %d tmp workdirs, %d orphaned videos",
            tmp_removed, orphans_removed,
        )
    return (tmp_removed, orphans_removed)
