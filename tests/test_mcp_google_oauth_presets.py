"""Google Calendar/Drive MCP presets: in-app OAuth wiring for headless Docker.

The gmail preset already works headless: Ithaka writes the OAuth keys file,
runs the authorize + code-exchange flow itself, stores the token where the npm
server expects it, and maps both paths into the server's env. These tests pin
the same contract for the google-calendar preset (token *file*, multi-account
format of @cocal/google-calendar-mcp) and the google-drive preset (external
token mode of @piotr-agier/google-drive-mcp: tokens injected via env, no file).
"""
import json

import pytest

from src.mcp_presets import get_presets
from routes.mcp_routes import (
    _apply_mcp_oauth_env,
    _ensure_keys_file_from_env,
    _mcp_oauth_token_missing,
    _tokens_to_env_updates,
    _write_oauth_tokens,
)


def _preset(preset_id):
    return next(p for p in get_presets() if p["id"] == preset_id)


# ── preset shapes ─────────────────────────────────────────────────────────


def test_google_calendar_preset_uses_in_app_oauth_with_token_file():
    p = _preset("google-calendar")
    oauth = p["oauth"]
    assert oauth["provider"] == "google"
    assert oauth["keys_file"].startswith("google-calendar/")
    assert oauth["token_file"].startswith("google-calendar/")
    assert oauth["token_format"] == "multi_account"
    assert oauth["env_map"] == {
        "keys_file": "GOOGLE_OAUTH_CREDENTIALS",
        "token_file": "GOOGLE_CALENDAR_MCP_TOKEN_PATH",
    }
    assert any("calendar" in s for s in oauth["scopes"])
    # The UI collects client id/secret; add_server turns them into the keys file.
    assert set(p["env"]) >= {"GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"}


def test_google_drive_preset_uses_maintained_package_with_env_tokens():
    p = _preset("google-drive")
    assert "@piotr-agier/google-drive-mcp" in p["args"]
    oauth = p["oauth"]
    assert oauth["provider"] == "google"
    assert oauth["keys_file"].startswith("google-drive/")
    assert "token_file" not in oauth  # external token mode: env, not a file
    assert oauth["token_env"] == {
        "access_token": "GOOGLE_DRIVE_MCP_ACCESS_TOKEN",
        "refresh_token": "GOOGLE_DRIVE_MCP_REFRESH_TOKEN",
        "client_id": "GOOGLE_DRIVE_MCP_CLIENT_ID",
        "client_secret": "GOOGLE_DRIVE_MCP_CLIENT_SECRET",
    }
    assert any("drive" in s for s in oauth["scopes"])
    assert set(p["env"]) >= {"GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"}


# ── env mapping ───────────────────────────────────────────────────────────


def test_apply_oauth_env_defaults_to_gmail_names():
    env = {}
    _apply_mcp_oauth_env(env, {"keys_file": "/k.json", "token_file": "/t.json"})
    assert env == {
        "GMAIL_OAUTH_PATH": "/k.json",
        "GMAIL_CREDENTIALS_PATH": "/t.json",
    }


def test_apply_oauth_env_skips_file_paths_in_token_env_mode():
    env = {}
    cfg = {
        "keys_file": "/k.json",
        "token_env": {"refresh_token": "GOOGLE_DRIVE_MCP_REFRESH_TOKEN"},
    }
    _apply_mcp_oauth_env(env, cfg)
    assert env == {}  # keys file feeds Ithaka's exchange, never the server env


def test_apply_oauth_env_honors_env_map():
    env = {}
    cfg = {
        "keys_file": "/k.json",
        "token_file": "/t.json",
        "env_map": {
            "keys_file": "GOOGLE_OAUTH_CREDENTIALS",
            "token_file": "GOOGLE_CALENDAR_MCP_TOKEN_PATH",
        },
    }
    _apply_mcp_oauth_env(env, cfg)
    assert env == {
        "GOOGLE_OAUTH_CREDENTIALS": "/k.json",
        "GOOGLE_CALENDAR_MCP_TOKEN_PATH": "/t.json",
    }
    assert "GMAIL_OAUTH_PATH" not in env


# ── token persistence ─────────────────────────────────────────────────────


def test_write_oauth_tokens_raw_stays_backward_compatible(tmp_path):
    token_file = tmp_path / "credentials.json"
    tokens = {"access_token": "a", "refresh_token": "r", "expires_in": 3599}
    _write_oauth_tokens({"token_file": str(token_file)}, tokens)
    assert json.loads(token_file.read_text()) == tokens


def test_write_oauth_tokens_multi_account_wraps_and_stamps_expiry(tmp_path):
    token_file = tmp_path / "tokens.json"
    tokens = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
    _write_oauth_tokens(
        {"token_file": str(token_file), "token_format": "multi_account"},
        tokens,
    )
    data = json.loads(token_file.read_text())
    assert set(data) == {"normal"}
    inner = data["normal"]
    assert inner["access_token"] == "a"
    assert inner["refresh_token"] == "r"
    assert isinstance(inner["expiry_date"], int)  # ms epoch derived from expires_in


def test_tokens_to_env_updates_maps_all_four_values():
    cfg = {
        "token_env": {
            "access_token": "GOOGLE_DRIVE_MCP_ACCESS_TOKEN",
            "refresh_token": "GOOGLE_DRIVE_MCP_REFRESH_TOKEN",
            "client_id": "GOOGLE_DRIVE_MCP_CLIENT_ID",
            "client_secret": "GOOGLE_DRIVE_MCP_CLIENT_SECRET",
        }
    }
    tokens = {"access_token": "a", "refresh_token": "r"}
    updates = _tokens_to_env_updates(cfg, tokens, "cid", "csec")
    assert updates == {
        "GOOGLE_DRIVE_MCP_ACCESS_TOKEN": "a",
        "GOOGLE_DRIVE_MCP_REFRESH_TOKEN": "r",
        "GOOGLE_DRIVE_MCP_CLIENT_ID": "cid",
        "GOOGLE_DRIVE_MCP_CLIENT_SECRET": "csec",
    }


# ── needs-oauth detection ─────────────────────────────────────────────────


def test_token_missing_env_mode_true_without_refresh_token():
    cfg = {"token_env": {"refresh_token": "GOOGLE_DRIVE_MCP_REFRESH_TOKEN"}}
    assert _mcp_oauth_token_missing(cfg, env={}) is True
    assert (
        _mcp_oauth_token_missing(
            cfg, env={"GOOGLE_DRIVE_MCP_REFRESH_TOKEN": "r"}
        )
        is False
    )


# ── keys file synthesis from env fields ───────────────────────────────────


def test_ensure_keys_file_from_env_writes_file_and_pops_env(tmp_path, monkeypatch):
    import routes.mcp_routes as mr

    monkeypatch.setattr(mr, "_mcp_oauth_base_dir", lambda: tmp_path)
    env = {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csec", "OTHER": "x"}
    cfg = {"keys_file": str(tmp_path / "google-calendar" / "gcp-oauth.keys.json")}
    _ensure_keys_file_from_env(env, cfg)
    keys = json.loads((tmp_path / "google-calendar" / "gcp-oauth.keys.json").read_text())
    assert keys["installed"]["client_id"] == "cid"
    assert keys["installed"]["client_secret"] == "csec"
    assert "GOOGLE_CLIENT_ID" not in env and "GOOGLE_CLIENT_SECRET" not in env
    assert env["OTHER"] == "x"


def test_ensure_keys_file_from_env_noop_without_credentials(tmp_path, monkeypatch):
    import routes.mcp_routes as mr

    monkeypatch.setattr(mr, "_mcp_oauth_base_dir", lambda: tmp_path)
    env = {"OTHER": "x"}
    cfg = {"keys_file": str(tmp_path / "google-drive" / "gcp-oauth.keys.json")}
    _ensure_keys_file_from_env(env, cfg)
    assert not (tmp_path / "google-drive" / "gcp-oauth.keys.json").exists()
    assert env == {"OTHER": "x"}
