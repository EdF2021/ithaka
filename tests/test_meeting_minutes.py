"""src/meeting_minutes.py (Task 2, part 1) — pure pipeline functions.

Hermetic: no LLM, no ffmpeg binary. `call` and `run` are fake, injected
callables so every function is exercised without network or subprocess
access. Covers: STT/correct/condense prompts + DUTCH_OUTPUT_RULE embedding,
the recursive head/tail condensation algorithm (carry-prepend-before-split),
the strict minutes template validator + one-shot retry, document/header
rendering, duration formatting, and the ffmpeg split command.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.meeting_minutes as mm
from src.notebook_language import DUTCH_OUTPUT_RULE
from src.prompt_security import UNTRUSTED_CONTEXT_POLICY


VALID_MINUTES_MD = """## Samenvatting
Het team bespreekt de sprintvoortgang en de planning voor volgende week.

## Besproken punten
1. Status van de huidige sprint.
2. Blokkades in de teststraat.

## Besluiten
- De release schuift een dag op.

## Actiepunten
| Actie | Eigenaar | Deadline |
|---|---|---|
| Testomgeving herstellen | Ed | vrijdag |

## Volgende vergadering
Dinsdag 10:00, vervolg release-planning.
"""

INVALID_MINUTES_MD = "## Samenvatting\nAlleen een samenvatting, verder niets.\n"


class _SequentialCall:
    """Fake `call`: records every messages list, replies "S<n>" (n = 1-based call index)."""

    def __init__(self):
        self.calls = []

    async def __call__(self, messages):
        self.calls.append(messages)
        return f"S{len(self.calls)}"


class _ConstCall:
    """Fake `call`: records calls, always returns the same fixed reply."""

    def __init__(self, reply):
        self.calls = []
        self.reply = reply

    async def __call__(self, messages):
        self.calls.append(messages)
        return self.reply


class _RecordingCall:
    """Fake `call`: pops replies off a queue in order."""

    def __init__(self, replies):
        self.calls = []
        self._replies = list(replies)

    async def __call__(self, messages):
        self.calls.append(messages)
        return self._replies.pop(0)


# ── build_stt_prompt ──


def test_build_stt_prompt_with_and_without_terms():
    assert mm.build_stt_prompt(None) == mm.STT_PROMPT_BASE
    assert mm.build_stt_prompt("") == mm.STT_PROMPT_BASE
    assert mm.build_stt_prompt("   ") == mm.STT_PROMPT_BASE

    result = mm.build_stt_prompt("PO, OKR")
    assert result == (
        mm.STT_PROMPT_BASE
        + " In het audiobestand worden de volgende afkortingen, namen, "
        "jargon, gebruikt: PO, OKR"
    )
    # surrounding whitespace on the terms themselves is stripped
    assert mm.build_stt_prompt("  PO, OKR  ") == result


# ── condense_split_for_depth ──


def test_condense_split_growth():
    assert mm.condense_split_for_depth(0) == 5000
    assert mm.condense_split_for_depth(1) == 7500
    assert mm.condense_split_for_depth(2) == 10000
    assert mm.condense_split_for_depth(4) == 15000
    assert mm.condense_split_for_depth(100) == 70000


# ── condense_transcript ──


async def test_condense_short_single_final_call():
    call = _ConstCall("  hallo notulen  \n")
    text = "x" * 100

    result = await mm.condense_transcript(text, call)

    assert len(call.calls) == 1
    system = call.calls[0][0]["content"]
    assert mm.CONDENSE_FINAL_SYSTEM in system
    assert DUTCH_OUTPUT_RULE in system
    assert UNTRUSTED_CONTEXT_POLICY in system
    assert result == "hallo notulen"


async def test_condense_long_recurses_with_carry():
    # 15000 chars is chosen (not the round 12000 in the brief's illustration)
    # so that, run through the exact algorithm with split(0)=5000,
    # split(1)=7500, split(2)=10000, the recursion genuinely reaches three
    # depths: part-call, part-call, final-call. With 12000 chars the second
    # depth's work (tail 7000 + "S1\n\n" carry = 7004) already fits under
    # split(1)=7500, so it would finalize after only two calls -- see
    # task-2-report.md for the full arithmetic.
    call = _SequentialCall()
    text = "a" * 15000
    depths = []

    result = await mm.condense_transcript(text, call, on_depth=depths.append)

    assert depths == [0, 1, 2]
    assert len(call.calls) == 3

    # call 1: PART, head = first 5000 chars of the original text
    system1, user1 = call.calls[0][0]["content"], call.calls[0][1]["content"]
    assert mm.CONDENSE_PART_SYSTEM in system1
    assert DUTCH_OUTPUT_RULE in system1
    assert ("a" * 5000) in user1
    assert ("a" * 5001) not in user1

    # call 2: PART again; its source text is carry ("S1") + "\n\n" + tail,
    # so the wrapped content starts the source body with "S1"
    system2, user2 = call.calls[1][0]["content"], call.calls[1][1]["content"]
    assert mm.CONDENSE_PART_SYSTEM in system2
    assert "Source: transcript\nS1" in user2

    # call 3: FINAL, carry is "S2" from call 2
    system3, user3 = call.calls[2][0]["content"], call.calls[2][1]["content"]
    assert mm.CONDENSE_FINAL_SYSTEM in system3
    assert "Source: transcript\nS2" in user3

    assert result == "S3"


async def test_condense_empty_returns_empty_without_calls():
    call = _SequentialCall()

    assert await mm.condense_transcript("", call) == ""
    assert call.calls == []

    assert await mm.condense_transcript("   \n\t  ", call) == ""
    assert call.calls == []


# ── correct_transcript ──


async def test_correct_transcript_falls_back_on_error():
    async def _boom(messages):
        raise RuntimeError("boom")

    result = await mm.correct_transcript("ruwe tekst", _boom)

    assert result == "ruwe tekst"


async def test_correct_transcript_returns_stripped_reply_on_success():
    call = _ConstCall("  Nette tekst.  ")

    result = await mm.correct_transcript("ruwe tekst", call)

    assert result == "Nette tekst."
    system = call.calls[0][0]["content"]
    assert mm.CORRECT_SYSTEM in system
    assert DUTCH_OUTPUT_RULE in system


# ── validate_minutes ──


def test_validate_minutes_ok():
    assert mm.validate_minutes(VALID_MINUTES_MD) == []


def test_validate_minutes_missing():
    md = VALID_MINUTES_MD.replace("## Besluiten\n- De release schuift een dag op.\n\n", "")

    errors = mm.validate_minutes(md)

    assert "ontbreekt: ## Besluiten" in errors


def test_validate_minutes_order():
    # swap "## Besproken punten" and "## Besluiten" sections
    besproken = (
        "## Besproken punten\n"
        "1. Status van de huidige sprint.\n"
        "2. Blokkades in de teststraat.\n\n"
    )
    besluiten = "## Besluiten\n- De release schuift een dag op.\n\n"
    md = VALID_MINUTES_MD.replace(besproken + besluiten, besluiten + besproken)

    errors = mm.validate_minutes(md)

    assert errors == ["volgorde: ## Besluiten"]


def test_validate_minutes_table():
    md = VALID_MINUTES_MD.replace(
        "| Actie | Eigenaar | Deadline |\n|---|---|---|\n", "Geen tabel hier.\n"
    )

    errors = mm.validate_minutes(md)

    assert "ontbreekt: actiepuntentabel" in errors


# ── build_minutes ──


async def test_build_minutes_retries_once_then_returns_valid():
    call = _RecordingCall([INVALID_MINUTES_MD, VALID_MINUTES_MD])

    md, valid = await mm.build_minutes(
        "condensed tekst",
        title="Team overleg",
        agenda=None,
        date_str="2026-09-04",
        duration_str="45 min",
        call=call,
    )

    assert valid is True
    assert md == VALID_MINUTES_MD.strip()
    assert len(call.calls) == 2

    retry_messages = call.calls[1]
    assert retry_messages[-2] == {"role": "assistant", "content": INVALID_MINUTES_MD.strip()}
    assert retry_messages[-1]["role"] == "user"
    assert "sjabloon niet" in retry_messages[-1]["content"]
    assert "## Samenvatting" in retry_messages[-1]["content"]


async def test_build_minutes_returns_first_when_both_invalid():
    other_invalid = "## Samenvatting\nNog steeds fout.\n"
    call = _RecordingCall([INVALID_MINUTES_MD, other_invalid])

    md, valid = await mm.build_minutes(
        "condensed tekst",
        title="Team overleg",
        agenda=None,
        date_str="2026-09-04",
        duration_str="45 min",
        call=call,
    )

    assert valid is False
    assert md == INVALID_MINUTES_MD.strip()
    assert len(call.calls) == 2


async def test_build_minutes_valid_first_try_skips_retry():
    call = _RecordingCall([VALID_MINUTES_MD])

    md, valid = await mm.build_minutes(
        "condensed tekst",
        title="Team overleg",
        agenda=None,
        date_str="2026-09-04",
        duration_str="45 min",
        call=call,
    )

    assert valid is True
    assert md == VALID_MINUTES_MD.strip()
    assert len(call.calls) == 1


# ── render_minutes_document / render_minutes_header ──


def test_render_minutes_document_and_header():
    doc = mm.render_minutes_document("## Samenvatting\ntekst  \n\n", "  transcript hier  ")

    assert doc == "## Samenvatting\ntekst\n\n## Bijlage: transcript\n\ntranscript hier\n"

    header = mm.render_minutes_header(
        title="Sprint review", date_str="4 sep 2026", duration_str="45 min", agenda=None
    )
    assert header == (
        "# Notulen: Sprint review\n\n"
        "**Datum:** 4 sep 2026  ·  **Duur:** 45 min  ·  **Opname:** Ithaka\n\n"
    )

    header_agenda = mm.render_minutes_header(
        title="Sprint review",
        date_str="4 sep 2026",
        duration_str="45 min",
        agenda="1. Punt een\n2. Punt twee",
    )
    assert header_agenda == header + "## Agenda\n\n1. Punt een\n2. Punt twee\n\n"


# ── format_duration ──


def test_format_duration():
    assert mm.format_duration(None) == "onbekend"
    assert mm.format_duration(65) == "1 min"
    assert mm.format_duration(5000) == "1 u 23 min"


# ── split_audio ──


def test_split_audio_builds_ffmpeg_cmd_and_returns_sorted(tmp_path, monkeypatch):
    monkeypatch.setattr(mm.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    calls = []

    def fake_run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        workdir = Path(cmd[-1]).parent
        (workdir / "seg_001.ogg").write_bytes(b"a")
        (workdir / "seg_000.ogg").write_bytes(b"a")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    src = tmp_path / "in.webm"
    src.write_bytes(b"x")
    workdir = tmp_path / "work"
    workdir.mkdir()

    segs = mm.split_audio(src, workdir, run=fake_run)

    assert [s.name for s in segs] == ["seg_000.ogg", "seg_001.ogg"]
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "/usr/bin/ffmpeg"
    assert cmd[cmd.index("-i") + 1] == str(src)
    assert cmd[cmd.index("-segment_time") + 1] == "600"
    assert cmd[-1] == str(workdir / "seg_%03d.ogg")


def test_split_audio_raises_on_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(mm.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="ffmpeg blew up")

    with pytest.raises(RuntimeError, match="Audio kon niet worden gesplitst"):
        mm.split_audio(tmp_path / "in.webm", tmp_path, run=fake_run)


def test_split_audio_raises_when_no_segments(tmp_path, monkeypatch):
    monkeypatch.setattr(mm.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="geen segmenten"):
        mm.split_audio(tmp_path / "in.webm", tmp_path, run=fake_run)


def test_split_audio_raises_when_ffmpeg_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(mm.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="ffmpeg niet gevonden"):
        mm.split_audio(tmp_path / "in.webm", tmp_path)
