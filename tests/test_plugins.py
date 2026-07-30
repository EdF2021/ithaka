"""Plugin subsystem tests (services/plugins + routes/plugin_routes).

Covers: install happy path (skill registered via SkillsManager, McpServer row
created disabled), zip-slip rejection, missing/invalid plugin.json, the 10MB
size cap, uninstall cleanup, reinstall-as-upgrade, and the admin gate on the
routes (pattern from tests/test_session_list_owner_scope.py /
test_backup_import_skills_dedup.py: require_admin is patched on the routes
module per test).
"""
import io
import json
import os
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as cdb
from core.database import McpServer
from services.memory.skills import SkillsManager
from services.plugins.manager import (
    MAX_PLUGIN_ZIP_BYTES,
    PluginError,
    PluginManager,
    PluginNotInstalledError,
)
import routes.plugin_routes as plugin_routes
from routes.plugin_routes import setup_plugin_routes


SKILL_MD = """---
name: demo-skill
description: Demo skill shipped by the test plugin
version: 1.0.0
category: general
status: published
source: user
---

## When to Use
When verifying that plugin-shipped skills register correctly.

## Procedure
1. Install the plugin.
2. Check the skills index.
"""


def make_plugin_zip(name="demo-plugin", version="1.0.0", *, with_manifest=True,
                    with_skill=True, with_mcp=True, extra_members=None,
                    description="Demo plugin for tests"):
    """Build the example plugin bundle in memory (plugin.json + 1 skill + mcp.json)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if with_manifest:
            zf.writestr("plugin.json", json.dumps(
                {"name": name, "version": version, "description": description}))
        if with_skill:
            zf.writestr("skills/demo-skill/SKILL.md", SKILL_MD)
            zf.writestr("skills/demo-skill/references/notes.md", "extra notes\n")
        if with_mcp:
            zf.writestr("mcp.json", json.dumps([{
                "name": "Demo Server",
                "transport": "stdio",
                "command": "echo",
                "args": ["hello"],
                "env": {"DEMO": "1"},
            }]))
        for member, content in (extra_members or {}).items():
            zf.writestr(member, content)
    return buf.getvalue()


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    cdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def env(tmp_path, db_factory):
    skills_manager = SkillsManager(str(tmp_path / "data"))
    pm = PluginManager(
        skills_manager,
        plugins_dir=str(tmp_path / "plugins"),
        session_factory=db_factory,
    )
    return pm, skills_manager, db_factory


def _mcp_rows(db_factory):
    db = db_factory()
    try:
        return [(s.id, s.name, bool(s.is_enabled)) for s in db.query(McpServer).all()]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

def test_install_happy_path(env):
    pm, sm, db_factory = env
    result = pm.install(make_plugin_zip(), owner="alice")

    assert result["name"] == "demo-plugin"
    assert result["version"] == "1.0.0"
    assert result["skills"] == ["demo-skill"]

    # Skill is registered through the existing SkillsManager under plugin-<name>
    skills = [s for s in sm.load_all() if s["category"] == "plugin-demo-plugin"]
    assert len(skills) == 1
    assert skills[0]["name"] == "demo-skill"
    assert skills[0]["owner"] == "alice"
    skill_md = os.path.join(sm.skills_root, "plugin-demo-plugin", "demo-skill", "SKILL.md")
    assert os.path.isfile(skill_md)
    # Sibling reference files come along
    assert os.path.isfile(os.path.join(
        sm.skills_root, "plugin-demo-plugin", "demo-skill", "references", "notes.md"))

    # McpServer row: prefixed id, NEVER auto-enabled
    rows = _mcp_rows(db_factory)
    assert len(rows) == 1
    server_id, server_name, enabled = rows[0]
    assert server_id.startswith("plugin_demo-plugin_")
    assert server_name == "Demo Server"
    assert enabled is False

    # list_plugins reflects the install
    listed = pm.list_plugins()
    assert len(listed) == 1
    assert listed[0]["name"] == "demo-plugin"
    assert listed[0]["skills_count"] == 1
    assert listed[0]["mcp_servers"][0]["is_enabled"] is False


def test_zip_slip_member_rejected(env, tmp_path):
    pm, _sm, db_factory = env
    evil = make_plugin_zip(extra_members={"../evil.txt": "pwned"})
    with pytest.raises(PluginError, match="unsafe path"):
        pm.install(evil)
    # Nothing was extracted or registered
    assert not os.path.exists(str(tmp_path / "plugins" / "demo-plugin"))
    assert not os.path.exists(str(tmp_path / "plugins" / "evil.txt"))
    assert not os.path.exists(str(tmp_path / "evil.txt"))
    assert _mcp_rows(db_factory) == []


def test_absolute_path_member_rejected(env):
    pm, _sm, _db = env
    evil = make_plugin_zip(extra_members={"/tmp/evil.txt": "pwned"})
    with pytest.raises(PluginError, match="unsafe path"):
        pm.install(evil)


def test_missing_plugin_json_rejected(env):
    pm, _sm, _db = env
    with pytest.raises(PluginError, match="plugin.json"):
        pm.install(make_plugin_zip(with_manifest=False))


def test_invalid_plugin_name_rejected(env):
    pm, _sm, _db = env
    with pytest.raises(PluginError, match="name"):
        pm.install(make_plugin_zip(name="Bad Name!"))


def test_missing_version_rejected(env):
    pm, _sm, _db = env
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("plugin.json", json.dumps({"name": "demo-plugin"}))
    with pytest.raises(PluginError, match="version"):
        pm.install(buf.getvalue())


def test_size_cap_enforced(env):
    pm, _sm, _db = env
    with pytest.raises(PluginError, match="size cap"):
        pm.install(b"x" * (MAX_PLUGIN_ZIP_BYTES + 1))


def test_not_a_zip_rejected(env):
    pm, _sm, _db = env
    with pytest.raises(PluginError, match="zip"):
        pm.install(b"definitely not a zip")


def test_uninstall_cleans_everything(env, tmp_path):
    pm, sm, db_factory = env
    pm.install(make_plugin_zip(), owner="alice")

    result = pm.uninstall("demo-plugin")
    assert result["status"] == "uninstalled"

    assert not os.path.exists(os.path.join(sm.skills_root, "plugin-demo-plugin"))
    assert [s for s in sm.load_all() if s["category"] == "plugin-demo-plugin"] == []
    assert _mcp_rows(db_factory) == []
    assert not os.path.exists(str(tmp_path / "plugins" / "demo-plugin"))
    assert pm.list_plugins() == []


def test_uninstall_unknown_plugin_raises(env):
    pm, _sm, _db = env
    with pytest.raises(PluginNotInstalledError):
        pm.uninstall("nope")


def test_reinstall_is_upgrade(env, tmp_path):
    pm, sm, db_factory = env
    pm.install(make_plugin_zip(version="1.0.0"), owner="alice")
    # Same version is allowed; upgrade replaces, never duplicates.
    result = pm.install(make_plugin_zip(version="1.0.0"), owner="alice")
    assert result["skills"] == ["demo-skill"]  # no demo-skill-2 dedup suffix

    skills = [s for s in sm.load_all() if s["category"] == "plugin-demo-plugin"]
    assert [s["name"] for s in skills] == ["demo-skill"]
    assert len(_mcp_rows(db_factory)) == 1

    # New version wins on a real upgrade
    result = pm.install(make_plugin_zip(version="2.0.0"), owner="alice")
    assert result["version"] == "2.0.0"
    listed = pm.list_plugins()
    assert len(listed) == 1
    assert listed[0]["version"] == "2.0.0"
    rows = _mcp_rows(db_factory)
    assert len(rows) == 1
    assert rows[0][2] is False  # still disabled after upgrade


# ---------------------------------------------------------------------------
# Routes: admin gate + error mapping
# ---------------------------------------------------------------------------

def _make_client(pm):
    app = FastAPI()
    app.include_router(setup_plugin_routes(pm))
    return TestClient(app)


def test_routes_admin_gate(env, monkeypatch):
    pm, _sm, _db = env
    # Auth enabled, no auth manager configured on app.state -> require_admin 403s.
    monkeypatch.setenv("AUTH_ENABLED", "true")
    client = _make_client(pm)

    assert client.get("/api/plugins").status_code == 403
    assert client.post(
        "/api/plugins/install",
        files={"file": ("demo.zip", make_plugin_zip(), "application/zip")},
    ).status_code == 403
    assert client.delete("/api/plugins/demo-plugin").status_code == 403


def test_routes_work_for_admin(env, monkeypatch):
    pm, sm, _db = env
    monkeypatch.setattr(plugin_routes, "require_admin", lambda r: None)
    client = _make_client(pm)

    r = client.post(
        "/api/plugins/install",
        files={"file": ("demo.zip", make_plugin_zip(), "application/zip")},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "demo-plugin"

    r = client.get("/api/plugins")
    assert r.status_code == 200
    assert r.json()[0]["name"] == "demo-plugin"

    # Invalid bundle -> 400 with a reason
    r = client.post(
        "/api/plugins/install",
        files={"file": ("bad.zip", b"not a zip", "application/zip")},
    )
    assert r.status_code == 400
    assert "zip" in r.json()["detail"]

    r = client.delete("/api/plugins/demo-plugin")
    assert r.status_code == 200
    assert client.delete("/api/plugins/demo-plugin").status_code == 404
    assert [s for s in sm.load_all() if s["category"] == "plugin-demo-plugin"] == []
