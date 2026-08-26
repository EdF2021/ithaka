"""Regression: the manage_research chat tool must enforce per-user ownership.

Finding F1: ``do_manage_research`` (src/tools/research.py) read, listed and
deleted ``deep_research/<id>.json`` files with NO owner check, while every
HTTP research route (routes/research/research_routes.py: research_library /
research_detail / research_delete) enforces ``d.get("owner") != user``. Any
non-admin user — or prompt injection running in a victim's chat — could
enumerate every user's research topics, read other users' report bodies and
sources, and delete any user's saved research by id.

These tests pin the tool-layer mirror of the routes' gate:
- list: skip files whose ``owner`` differs from the caller;
- read: a mismatched owner gets the SAME not-found error as a missing id
  (no existence oracle — mirrors the routes' 404-not-403 choice);
- delete: ownership is verified before unlink and the file survives;
- a falsy owner (single-user / auth-disabled / legacy caller) keeps the
  current unscoped behavior, same as src/tools/notes.py's
  ``_note_visible_to_owner``;
- the tool is classified in NON_ADMIN_BLOCKED_TOOLS so the runtime public
  gate (``is_public_blocked_tool``) no longer allows it by omission.
"""
import json

import pytest

from src.tools.research import do_manage_research

OWNER_A = "alice"
OWNER_B = "bob"


@pytest.fixture
def research_dir(tmp_path, monkeypatch):
    """Point the tool at an isolated research dir (no cwd-relative data/)."""
    monkeypatch.setattr("src.tools.research.DEEP_RESEARCH_DIR", str(tmp_path))
    return tmp_path


def _seed(research_dir, rid, owner, query="secret query", body="SECRET-REPORT-BODY"):
    payload = {
        "query": query,
        "result": body,
        "sources": [{"title": "Example", "url": "https://example.com"}],
        "completed_at": 123,
    }
    if owner is not None:
        payload["owner"] = owner
    (research_dir / f"{rid}.json").write_text(json.dumps(payload), encoding="utf-8")


async def test_read_other_owners_research_is_not_found(research_dir):
    _seed(research_dir, "rp-alicereport1", OWNER_A)
    res = await do_manage_research(
        json.dumps({"action": "read", "id": "rp-alicereport1"}), owner=OWNER_B
    )
    assert "error" in res, res
    assert "SECRET-REPORT-BODY" not in json.dumps(res)
    assert "secret query" not in json.dumps(res)


async def test_read_mismatch_error_matches_missing_id_error(research_dir):
    """The mismatch answer must not confirm the file exists: it must be the
    exact same not-found shape a truly missing id gets (routes use 404-not-403
    for the same reason)."""
    _seed(research_dir, "rp-alicereport2", OWNER_A)
    mismatch = await do_manage_research(
        json.dumps({"action": "read", "id": "rp-alicereport2"}), owner=OWNER_B
    )
    missing = await do_manage_research(
        json.dumps({"action": "read", "id": "rp-doesnotexist"}), owner=OWNER_B
    )
    assert "error" in mismatch and "error" in missing
    normalized_mismatch = mismatch["error"].replace("rp-alicereport2", "{id}")
    normalized_missing = missing["error"].replace("rp-doesnotexist", "{id}")
    assert normalized_mismatch == normalized_missing


async def test_delete_other_owners_research_is_denied_and_file_kept(research_dir):
    _seed(research_dir, "rp-alicereport3", OWNER_A)
    res = await do_manage_research(
        json.dumps({"action": "delete", "id": "rp-alicereport3"}), owner=OWNER_B
    )
    assert "error" in res, res
    assert (research_dir / "rp-alicereport3.json").exists(), (
        "another owner's research file was deleted"
    )


async def test_list_hides_other_owners_research(research_dir):
    _seed(research_dir, "rp-alicereport4", OWNER_A, query="alice private topic")
    _seed(research_dir, "rp-bobreport1", OWNER_B, query="bob own topic")
    res = await do_manage_research(json.dumps({"action": "list"}), owner=OWNER_B)
    out = res.get("output", "")
    assert "bob own topic" in out
    assert "alice private topic" not in out
    assert "rp-alicereport4" not in out


async def test_owner_still_reads_and_deletes_own_research(research_dir):
    _seed(research_dir, "rp-bobreport2", OWNER_B, query="bob q", body="bob body")
    res = await do_manage_research(
        json.dumps({"action": "read", "id": "rp-bobreport2"}), owner=OWNER_B
    )
    assert res.get("exit_code") == 0, res
    assert "bob body" in res.get("output", "")
    res = await do_manage_research(
        json.dumps({"action": "delete", "id": "rp-bobreport2"}), owner=OWNER_B
    )
    assert res.get("exit_code") == 0, res
    assert not (research_dir / "rp-bobreport2.json").exists()


async def test_falsy_owner_keeps_single_user_behavior(research_dir):
    """No owner (single-user / auth-disabled / legacy caller) stays unscoped —
    same convention as src/tools/notes.py::_note_visible_to_owner."""
    _seed(research_dir, "rp-legacyreport", None, query="legacy q", body="legacy body")
    res = await do_manage_research(
        json.dumps({"action": "read", "id": "rp-legacyreport"}), owner=None
    )
    assert res.get("exit_code") == 0, res
    assert "legacy body" in res.get("output", "")


def test_manage_research_is_blocked_for_public_users():
    """The runtime non-admin gate must know about manage_research: it is an
    XML/fence-only tool (no FUNCTION_TOOL_SCHEMAS entry), so the
    schema-driven classification never saw it and the denylist allowed it
    by omission."""
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, is_public_blocked_tool

    assert "manage_research" in NON_ADMIN_BLOCKED_TOOLS
    assert is_public_blocked_tool("manage_research") is True
