"""Regression: a free-text STT language must never reach Whisper as a name.

A user typing "Nederlands" in the STT settings card once broke voice input —
the value was sent verbatim to the OpenAI /audio/transcriptions API, which
rejected it with a 400. normalize_stt_language() coerces names to ISO-639-1
codes and drops anything unrecognized to auto-detect.
"""

import pytest

from services.stt.stt_service import normalize_stt_language


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Nederlands", "nl"),
        ("nederlands", "nl"),
        ("Dutch", "nl"),
        ("  Nederlands  ", "nl"),
        ("Engels", "en"),
        ("English", "en"),
        ("Duits", "de"),
        ("Frans", "fr"),
        ("nl", "nl"),       # already an ISO code
        ("EN", "en"),       # ISO code, case-insensitive
        ("", ""),           # empty -> auto-detect
        ("   ", ""),        # whitespace -> auto-detect
        ("Klingon", ""),    # unrecognized -> auto-detect, not a 400
        ("nederlandse", ""),  # not an exact name/code -> auto-detect
    ],
)
def test_normalize_stt_language(raw, expected):
    assert normalize_stt_language(raw) == expected
