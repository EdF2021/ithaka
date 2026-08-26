"""Regression tests for F2: agent file tools must not expose the app's own
credential stores.

DATA_DIR is an allowed tool root, but a handful of files under it ARE the
application's credential stores: sessions.json (live session bearer tokens),
vault.json + .app_key (Bitwarden BW_SESSION / vault key), settings.json
(search API keys), auth.json (bcrypt password hashes), app.db (sqlite) and
the chroma/ vector store. read_file, grep, glob and ls must refuse them by
ABSOLUTE resolved path — compared against the named constants in
``src/constants.py``, not a basename deny-list — while ordinary files under
the allowed roots (including same-named files elsewhere, e.g. an uploaded
settings.json) stay readable.

Mirrors tests/test_tool_path_confinement.py (read_file/write_file) and
tests/test_code_nav_tools.py (grep/glob/ls).
"""
import asyncio
import json
import os
import shutil

import pytest

SECRET = "F2-CANARY-8f3a1c-bw-session"

# Constant names in src/constants.py whose files are app credential stores.
_PROTECTED_FILE_CONSTS = (
    "SESSIONS_FILE", "VAULT_FILE", "SETTINGS_FILE", "AUTH_FILE",
    "APP_KEY_FILE", "APP_DB",
)


def _run(tool, content):
    from src.tool_execution import _direct_fallback
    return asyncio.run(_direct_fallback(tool, content))


# ── Unit: resolver refuses the REAL constant paths ────────────────────────
# The deny check is path-based and fires before any existence check, so these
# run against the real constants without touching the real data dir.

@pytest.mark.parametrize("const_name", _PROTECTED_FILE_CONSTS + ("CHROMA_DIR",))
def test_resolve_tool_path_blocks_real_credential_store(const_name):
    from src import constants
    from src.tool_execution import _resolve_tool_path
    with pytest.raises(ValueError):
        _resolve_tool_path(getattr(constants, const_name))


def test_resolve_tool_path_blocks_file_inside_chroma_dir():
    from src import constants
    from src.tool_execution import _resolve_tool_path
    with pytest.raises(ValueError):
        _resolve_tool_path(os.path.join(constants.CHROMA_DIR, "chroma.sqlite3"))


def test_workspace_resolver_blocks_credential_store(tmp_path, monkeypatch):
    """A workspace bound over the data dir must not re-expose the stores."""
    from src import constants
    from src.tool_execution import _resolve_tool_path_in_workspace
    ws = tmp_path / "ws"
    ws.mkdir()
    vault = ws / "vault.json"
    vault.write_text("{}")
    monkeypatch.setattr(constants, "VAULT_FILE", str(vault))
    with pytest.raises(ValueError):
        _resolve_tool_path_in_workspace(str(ws), "vault.json")


# ── Dispatch-level tests against a stand-in data dir ──────────────────────

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A stand-in DATA_DIR under /tmp (an allowed tool root) whose
    credential-store constants are monkeypatched to real files carrying a
    canary secret, plus ordinary files that must stay reachable."""
    from src import constants
    d = tmp_path / "appdata"
    d.mkdir()
    mapping = {
        "SESSIONS_FILE": "sessions.json",
        "VAULT_FILE": "vault.json",
        "SETTINGS_FILE": "settings.json",
        "AUTH_FILE": "auth.json",
        "APP_KEY_FILE": ".app_key",
        "APP_DB": "app.db",
    }
    for const, name in mapping.items():
        p = d / name
        p.write_text(json.dumps({"secret": SECRET}))
        monkeypatch.setattr(constants, const, str(p))
    chroma = d / "chroma"
    chroma.mkdir()
    (chroma / "chroma.sqlite3").write_text(SECRET)
    monkeypatch.setattr(constants, "CHROMA_DIR", str(chroma))
    # Ordinary files that must remain readable/listable.
    (d / "notes.txt").write_text(f"ordinary note {SECRET}")
    uploads = d / "uploads"
    uploads.mkdir()
    (uploads / "settings.json").write_text('{"user_file": "not the app settings"}')
    return d


# ── read_file ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "name",
    ["sessions.json", "vault.json", "settings.json", "auth.json", ".app_key", "app.db"],
)
def test_read_file_denies_credential_store(data_dir, name):
    r = _run("read_file", str(data_dir / name))
    assert r.get("exit_code") == 1, f"read_file returned contents: {r}"
    assert SECRET not in json.dumps(r)
    err = r.get("error") or ""
    # Refusal must not leak whether the file exists.
    assert "not found" not in err


def test_read_file_denies_chroma_contents(data_dir):
    r = _run("read_file", str(data_dir / "chroma" / "chroma.sqlite3"))
    assert r.get("exit_code") == 1
    assert SECRET not in json.dumps(r)


def test_read_file_ordinary_file_still_works(data_dir):
    r = _run("read_file", str(data_dir / "notes.txt"))
    assert r.get("exit_code") == 0, r
    assert SECRET in r["output"]


def test_read_file_same_basename_elsewhere_still_works(data_dir):
    """Exclusion is by absolute path, not basename: an unrelated
    uploads/settings.json stays readable."""
    r = _run("read_file", str(data_dir / "uploads" / "settings.json"))
    assert r.get("exit_code") == 0, r
    assert "user_file" in r["output"]


# ── write_file (same resolver: no planting a hash in auth.json) ───────────

def test_write_file_denies_credential_store(data_dir):
    target = data_dir / "auth.json"
    r = _run("write_file", f"{target}\n{{\"users\": {{}}}}")
    assert r.get("exit_code") == 1, r
    assert SECRET in target.read_text()  # untouched


# ── grep ──────────────────────────────────────────────────────────────────

def _assert_grep_excludes_stores(result):
    assert result["exit_code"] == 0, result
    out = result.get("output") or ""
    assert "notes.txt" in out  # legitimate hit still returned
    for name in ("sessions.json", "vault.json", "auth.json", ".app_key", "app.db"):
        assert name not in out, f"grep leaked {name}: {out}"
    assert "chroma.sqlite3" not in out


def test_grep_excludes_credential_stores(data_dir):
    r = _run("grep", json.dumps({"pattern": SECRET, "path": str(data_dir)}))
    _assert_grep_excludes_stores(r)


def test_grep_excludes_credential_stores_python_fallback(data_dir, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    r = _run("grep", json.dumps({"pattern": SECRET, "path": str(data_dir)}))
    _assert_grep_excludes_stores(r)


@pytest.mark.skipif(shutil.which("rg") is None, reason="targets the ripgrep fast-path")
def test_grep_excludes_credential_stores_rg(data_dir):
    r = _run("grep", json.dumps({"pattern": SECRET, "path": str(data_dir)}))
    _assert_grep_excludes_stores(r)


# ── glob ──────────────────────────────────────────────────────────────────

def test_glob_excludes_credential_stores(data_dir):
    r = _run("glob", json.dumps({"pattern": "**/*.json", "path": str(data_dir)}))
    assert r["exit_code"] == 0, r
    out = r.get("output") or ""
    for name in ("sessions.json", "vault.json", "auth.json"):
        assert str(data_dir / name) not in out, f"glob leaked {name}: {out}"
    assert str(data_dir / "settings.json") not in out
    # same-named ordinary file elsewhere still surfaced
    assert str(data_dir / "uploads" / "settings.json") in out


def test_glob_literal_does_not_confirm_credential_store(data_dir):
    r = _run("glob", json.dumps({"pattern": "vault.json", "path": str(data_dir)}))
    assert r["exit_code"] == 0, r
    assert str(data_dir / "vault.json") not in (r.get("output") or "")


# ── ls ────────────────────────────────────────────────────────────────────

def test_ls_omits_credential_stores(data_dir):
    r = _run("ls", str(data_dir))
    assert r["exit_code"] == 0, r
    out = r["output"]
    assert "notes.txt" in out
    assert "uploads/" in out
    for name in ("sessions.json", "vault.json", "settings.json", "auth.json",
                 "app.db", "chroma"):
        assert name not in out, f"ls leaked {name}: {out}"


def test_ls_denies_chroma_dir(data_dir):
    r = _run("ls", str(data_dir / "chroma"))
    assert r["exit_code"] == 1, r
    assert SECRET not in json.dumps(r)
