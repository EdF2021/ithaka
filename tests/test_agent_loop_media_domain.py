"""Image/video requests must not fall into the direct low-signal reply path.

Regression for the integration-smoke gap: chat->agent auto-escalation fired
correctly (category=image), but _classify_agent_request found no domain for
a plain "maak een afbeelding van een vuurtoren bij nacht" first turn, so
low_signal=True sent it down the direct low-signal reply path (no tools at
all, e.g. "Hey." instead of calling generate_image).

Mirrors the mocked-import harness in tests/test_agent_loop.py so this file
doesn't need the full app stack.
"""
import sys
from unittest.mock import MagicMock

_MOCKED_IMPORTS = [
    'sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.ext', 'sqlalchemy.ext.declarative',
    'sqlalchemy.ext.hybrid', 'sqlalchemy.sql', 'sqlalchemy.sql.expression',
    'src.database',
    'src.agent_tools',
    'core.models', 'core.database',
]
_INJECTED_IMPORT_STUBS = {}
_PREEXISTING_AGENT_LOOP = sys.modules.get("src.agent_loop")


def _drop_module_if_same(name, expected):
    if sys.modules.get(name) is expected:
        sys.modules.pop(name, None)
    parent_name, _, attr = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None and getattr(parent, "__dict__", {}).get(attr) is expected:
        delattr(parent, attr)


for mod in _MOCKED_IMPORTS:
    if mod not in sys.modules:
        stub = MagicMock()
        sys.modules[mod] = stub
        _INJECTED_IMPORT_STUBS[mod] = stub

_IMPORTED_AGENT_LOOP = None
try:
    from src.agent_loop import _classify_agent_request, _DOMAIN_TOOL_MAP, _DOMAIN_RULES
    _IMPORTED_AGENT_LOOP = sys.modules.get("src.agent_loop")
finally:
    if _PREEXISTING_AGENT_LOOP is None and _IMPORTED_AGENT_LOOP is not None:
        _drop_module_if_same("src.agent_loop", _IMPORTED_AGENT_LOOP)
    for _mod, _stub in _INJECTED_IMPORT_STUBS.items():
        _drop_module_if_same(_mod, _stub)


def test_image_request_is_not_low_signal():
    intent = _classify_agent_request([], "Maak een afbeelding van een vuurtoren bij nacht")
    assert intent["low_signal"] is False
    assert "media" in intent["domains"]


def test_video_request_is_not_low_signal():
    intent = _classify_agent_request([], "make a video of waves at sunset")
    assert intent["low_signal"] is False
    assert "media" in intent["domains"]


def test_describe_image_request_does_not_add_media_domain():
    intent = _classify_agent_request([], "beschrijf deze afbeelding")
    assert "media" not in intent["domains"]


def test_domain_tool_map_seeds_both_media_tools():
    assert _DOMAIN_TOOL_MAP["media"] == {"generate_image", "generate_video"}


def test_domain_rules_has_a_media_entry():
    # _domain_rules_for_tools does _DOMAIN_RULES[domain] for every domain key
    # in _DOMAIN_TOOL_MAP that overlaps the offered tools — a "media" entry in
    # the tool map without a matching rules entry would KeyError as soon as
    # generate_image/generate_video are offered.
    assert "media" in _DOMAIN_RULES
    assert isinstance(_DOMAIN_RULES["media"], str) and _DOMAIN_RULES["media"]


def test_confirmation_reply_keeps_media_domain_via_recent_context():
    """A terse confirmation ("yes") after a video request must not drop the
    seeded media tools: media_intent() is checked against `text` (this turn)
    and `retrieval_query` (recent user turns, used on an explicit
    continuation) — checking only `text` would return "" for "yes" and lose
    the domain on the very turn the user confirmed."""
    messages = [
        {"role": "user", "content": "maak een video van golven bij zonsondergang"},
        {"role": "assistant", "content": "Oke, welke lengte wil je? 4, 6 of 8 seconden?"},
        {"role": "user", "content": "yes"},
    ]
    intent = _classify_agent_request(messages, "yes")
    assert intent["continuation"] is True
    assert "media" in intent["domains"]
    assert intent["low_signal"] is False
