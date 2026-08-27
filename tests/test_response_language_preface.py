"""Tests for the response_language setting: when set, every chat/agent/voice
turn gets a standing system message naming the reply language, injected as
the first preface message in build_context_preface (unconditional — outside
the memory/incognito/notebook gates, so short voice utterances get it too)."""

from unittest.mock import patch

from src.chat_processor import ChatProcessor
from src.memory import MemoryManager


class _Docs:
    rag_manager = None


def _build_preface(tmp_path, **kwargs):
    processor = ChatProcessor(
        memory_manager=MemoryManager(str(tmp_path)), personal_docs_manager=_Docs()
    )
    preface, _, _, _ = processor.build_context_preface(
        message="hallo",
        session=None,
        owner="alice",
        use_web=False,
        use_rag=False,
        use_memory=False,
        use_skills=False,
        **kwargs,
    )
    return preface


def test_response_language_injected_first_when_set(tmp_path):
    with patch("src.settings.get_setting", side_effect=lambda k, d=None: "Nederlands" if k == "response_language" else d):
        preface = _build_preface(tmp_path)
    assert preface[0]["role"] == "system"
    assert "Nederlands" in preface[0]["content"]


def test_response_language_absent_when_unset(tmp_path):
    with patch("src.settings.get_setting", side_effect=lambda k, d=None: "" if k == "response_language" else d):
        preface = _build_preface(tmp_path)
    assert not any("response language" in (m.get("content") or "") for m in preface)


def test_response_language_precedes_preset_prompt(tmp_path):
    with patch("src.settings.get_setting", side_effect=lambda k, d=None: "Nederlands" if k == "response_language" else d):
        preface = _build_preface(tmp_path, preset_system_prompt="Je bent een piraat.")
    contents = [m.get("content") or "" for m in preface]
    lang_idx = next(i for i, c in enumerate(contents) if "Nederlands" in c)
    preset_idx = next(i for i, c in enumerate(contents) if "piraat" in c)
    assert lang_idx < preset_idx
