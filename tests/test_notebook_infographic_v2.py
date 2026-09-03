"""Infographic v2: JSON schema extraction, legacy detection, v2 renderer.

Pure unit tests — no DB, no LLM. Spec: docs/superpowers/specs/
2026-09-03-notebooks-infographic-v2-design.md (Deel A + C).
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-infographic-v2")

import copy
import json
from datetime import datetime

import pytest

from src.notebook_infographic import (
    MAX_ILLUSTRATIONS,
    extract_infographic,
    is_infographic_v2,
    iter_blocks,
)


def _valid_data():
    return {
        "title": "SamenWijzer in cijfers",
        "subtitle": "Wat de bronnen zeggen",
        "takeaway": "Begeleiding op maat werkt als de data klopt.",
        "blocks": [
            {"id": "bronnen", "type": "column", "heading": "Bronnen", "subheading": "Wat erin gaat",
             "illustration_prompt": "a stack of documents flowing into a funnel",
             "children": [
                 {"id": "stappen", "type": "steps", "heading": "Stappen",
                  "items": [{"label": "Upload", "text": "Bronnen toevoegen"},
                            {"label": "Index", "text": "Chunks in Chroma"}]},
                 {"id": "kaart-a", "type": "icon_card", "heading": "Grounded", "icon": "chat",
                  "text": "Antwoorden alleen uit bronnen."},
             ]},
            {"id": "hero", "type": "hero", "heading": "Eén werkruimte",
             "illustration_prompt": "a glowing hub connecting six nodes",
             "text": "Chat, studio en bronnen in één scherm."},
            {"id": "vergelijk", "type": "comparison", "heading": "Vergelijking",
             "rows": [{"label": "Bronnen", "value": "300 bronnen", "ratio": 0.6},
                      {"label": "Chats", "value": "500 chats", "ratio": 1.0}]},
            {"id": "cijfers", "type": "key_numbers", "heading": "Kerncijfers",
             "items": [{"number": "42%", "label": "geslaagd"},
                       {"number": "3", "label": "panelen"},
                       {"number": "12 weken", "label": "duur"}]},
            {"id": "output", "type": "column", "heading": "Output", "subheading": "Wat eruit komt",
             "children": [
                 {"id": "kaart-b", "type": "icon_card", "heading": "Podcast",
                  "text": "Twee stemmen, één script."},
                 {"id": "kaart-c", "type": "icon_card", "heading": "Video",
                  "illustration_prompt": "a film strip made of soft paper shapes",
                  "text": "Slides met voice-over."},
             ]},
        ],
    }


def _fenced(data):
    return "Hier is de infographic:\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```\n"


# ---- detection ----------------------------------------------------------

def test_detects_bare_json_and_fence_as_v2():
    assert is_infographic_v2(json.dumps(_valid_data())) is True
    assert is_infographic_v2(_fenced(_valid_data())) is True
    assert is_infographic_v2("  \n```json\n{\"a\":1}\n```") is True


def test_detects_legacy_markdown_as_not_v2():
    assert is_infographic_v2("# Titel\n\n## Key numbers\n- **3** — x\n") is False
    assert is_infographic_v2("") is False
    assert is_infographic_v2(None) is False


# ---- extract_infographic: happy path -------------------------------------

def test_extract_accepts_valid_fenced_json():
    data = extract_infographic(_fenced(_valid_data()))
    assert data["title"] == "SamenWijzer in cijfers"
    assert data["subtitle"] == "Wat de bronnen zeggen"
    assert data["takeaway"].startswith("Begeleiding")
    assert [b["type"] for b in data["blocks"]] == ["column", "hero", "comparison", "key_numbers", "column"]
    assert data["illustrations"] == {}


def test_extract_keeps_illustrations_map_and_drops_unknown_icon():
    raw = _valid_data()
    raw["illustrations"] = {"hero": "abc-hero-01234567.png"}
    raw["blocks"][0]["children"][1]["icon"] = "does-not-exist"
    data = extract_infographic(json.dumps(raw))
    assert data["illustrations"] == {"hero": "abc-hero-01234567.png"}
    assert data["blocks"][0]["children"][1]["icon"] is None


def test_iter_blocks_flattens_in_document_order():
    data = extract_infographic(json.dumps(_valid_data()))
    assert [b["id"] for b in iter_blocks(data)] == [
        "bronnen", "stappen", "kaart-a", "hero", "vergelijk", "cijfers", "output", "kaart-b", "kaart-c",
    ]


def test_more_than_max_illustration_prompts_is_allowed():
    raw = _valid_data()
    for b in raw["blocks"]:
        b["illustration_prompt"] = "abstract soft shapes"
        for c in b.get("children", []):
            c["illustration_prompt"] = "abstract soft shapes"
    data = extract_infographic(json.dumps(raw))
    assert sum(1 for b in iter_blocks(data) if b.get("illustration_prompt")) > MAX_ILLUSTRATIONS


# ---- extract_infographic: rejections (Dutch messages) --------------------

@pytest.mark.parametrize("mutate, fragment", [
    (lambda d: d.pop("title"), "title"),
    (lambda d: d.update(title="x" * 81), "title"),
    (lambda d: d.pop("takeaway"), "takeaway"),
    (lambda d: d.update(blocks=d["blocks"][:4]), "5"),
    (lambda d: d.update(blocks=d["blocks"] + [copy.deepcopy(d["blocks"][2]) | {"id": f"c{i}"} for i in range(4)]), "8"),
    (lambda d: d["blocks"].__setitem__(1, {**d["blocks"][1], "type": "icon_card"}), "hero"),
    (lambda d: d["blocks"].append({**d["blocks"][1], "id": "hero2"}), "hero"),
    (lambda d: [b.update(type="icon_card", text="x") for b in d["blocks"] if b["type"] == "column"], "column"),
    (lambda d: d["blocks"].append({**copy.deepcopy(d["blocks"][0]), "id": "col3",
                                   "children": [{**c, "id": c["id"] + "-x"} for c in d["blocks"][0]["children"]]}), "column"),
    (lambda d: d["blocks"][0].__setitem__("id", "hero"), "uniek"),
    (lambda d: d["blocks"][0].__setitem__("id", "Bad Id!"), "id"),
    (lambda d: d["blocks"][0]["children"].__setitem__(0, {**d["blocks"][0]["children"][0], "type": "hero"}), "column"),
    (lambda d: d["blocks"][0]["children"].__setitem__(0, {**d["blocks"][0]["children"][0], "type": "column", "children": []}), "column"),
    (lambda d: d["blocks"][0].__setitem__("children", d["blocks"][0]["children"][:1]), "children"),
    (lambda d: d["blocks"][0]["children"][0].__setitem__("items", [{"label": "a", "text": "b"}]), "steps"),
    (lambda d: d["blocks"][0]["children"][0]["items"].__setitem__(0, {"label": "a", "text": "x" * 121}), "120"),
    (lambda d: d["blocks"][1].__setitem__("text", "x" * 241), "240"),
    (lambda d: d["blocks"][2].__setitem__("rows", d["blocks"][2]["rows"][:1]), "comparison"),
    (lambda d: d["blocks"][2]["rows"][0].__setitem__("ratio", 1.5), "ratio"),
    (lambda d: d["blocks"][3].__setitem__("items", d["blocks"][3]["items"][:2]), "key_numbers"),
    (lambda d: d["blocks"][3]["items"].__setitem__(0, {"number": "1", "label": "een twee drie vier vijf zes zeven acht negen"}), "8"),
    (lambda d: d["blocks"][1].__setitem__("heading", "x" * 61), "60"),
    (lambda d: d["blocks"][1].__setitem__("illustration_prompt", "x" * 201), "200"),
])
def test_extract_rejects_schema_violations(mutate, fragment):
    raw = _valid_data()
    mutate(raw)
    with pytest.raises(ValueError) as exc:
        extract_infographic(json.dumps(raw))
    assert fragment.lower() in str(exc.value).lower()


@pytest.mark.parametrize("prompt", [
    "a poster with the text 'welkom'",
    "a sign with big LETTERS",
    "chart with a caption",
    "banner with words on it",
    "Label on a jar",
])
def test_extract_rejects_text_in_image_prompts(prompt):
    raw = _valid_data()
    raw["blocks"][1]["illustration_prompt"] = prompt
    with pytest.raises(ValueError) as exc:
        extract_infographic(json.dumps(raw))
    assert "illustration_prompt" in str(exc.value)


def test_extract_allows_prompt_words_that_merely_contain_forbidden_substrings():
    raw = _valid_data()
    raw["blocks"][1]["illustration_prompt"] = "a textile pattern and a labelled-free sky"
    extract_infographic(json.dumps(raw))  # 'textile' / 'labelled' are not whole-word hits


@pytest.mark.parametrize("content, fragment", [
    ("", "geen JSON"),
    ("```json\n{not json}\n```", "ongeldige JSON"),
    ("[1,2]", "geen object"),
])
def test_extract_rejects_non_json(content, fragment):
    with pytest.raises(ValueError) as exc:
        extract_infographic(content)
    assert fragment in str(exc.value)


# ---- prompt + validator wiring -------------------------------------------

def test_prompt_asks_for_json_schema_and_dutch():
    from src.notebook_artifacts import ARTIFACT_KINDS, _KIND_VALIDATORS
    from src.notebook_language import DUTCH_OUTPUT_RULE
    prompt = ARTIFACT_KINDS["infographic"]["prompt"]
    assert DUTCH_OUTPUT_RULE in prompt
    assert '"blocks"' in prompt
    for t in ("column", "steps", "icon_card", "hero", "comparison", "key_numbers"):
        assert f'"{t}"' in prompt
    assert "illustration_prompt" in prompt
    assert "Engels" in prompt                 # illustration prompts in English
    assert "## Key numbers" not in prompt     # legacy markdown structure is gone
    assert _KIND_VALIDATORS["infographic"] is extract_infographic
