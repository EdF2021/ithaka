# services/plugins/manager.py
"""Plugin bundle manager.

A plugin is a zip with, at its root (or under a single top-level folder):

    plugin.json                    # {"name", "version", "description?", "author?"}
    skills/<skill-name>/SKILL.md   # 0..n skills (existing SKILL.md format)
    mcp.json                       # optional: [{name, transport, command|url, args, env}]

Install extracts the bundle to ``PLUGINS_DIR/<name>/``, registers each skill
through the existing SkillsManager under category ``plugin-<name>`` and adds
McpServer rows with id prefix ``plugin_<name>_``, ALWAYS disabled — an admin
must enable (and thereby connect) them explicitly. Nothing here ever
connects an MCP server.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import zipfile
from typing import Dict, List, Optional

from src.constants import PLUGINS_DIR

logger = logging.getLogger(__name__)

PLUGIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
MAX_PLUGIN_ZIP_BYTES = 10 * 1024 * 1024          # compressed upload cap
MAX_PLUGIN_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # zip-bomb guard
MCP_TRANSPORTS = ("stdio", "sse", "http")


class PluginError(ValueError):
    """Invalid plugin bundle or operation — maps to HTTP 400."""


class PluginNotInstalledError(PluginError):
    """Named plugin is not installed — maps to HTTP 404."""


def _member_is_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


class PluginManager:
    """Install / uninstall / list plugin bundles."""

    def __init__(self, skills_manager, plugins_dir: Optional[str] = None,
                 session_factory=None):
        self.skills_manager = skills_manager
        self.plugins_dir = plugins_dir or PLUGINS_DIR
        # Injectable for tests; defaults to the app DB lazily so importing
        # this module never forces DB init.
        self._session_factory = session_factory
        # Guarded mkdir: the source tree is read-only in Docker — an
        # unwritable path must degrade (installs will fail with a clear
        # error) instead of crashing at import/construction time.
        try:
            os.makedirs(self.plugins_dir, exist_ok=True)
        except OSError as e:
            logger.warning("Plugins dir %s not writable: %s", self.plugins_dir, e)

    # ------------------------------------------------------------------
    # DB access
    # ------------------------------------------------------------------

    def _db(self):
        if self._session_factory is not None:
            return self._session_factory()
        from core.database import SessionLocal
        return SessionLocal()

    @staticmethod
    def _mcp_id_prefix(name: str) -> str:
        return f"plugin_{name}_"

    def _mcp_rows_for(self, db, name: str):
        from core.database import McpServer
        prefix = self._mcp_id_prefix(name)
        # NOTE: filter in Python, not with SQL LIKE — "_" is a LIKE wildcard
        # and plugin ids contain several.
        return [srv for srv in db.query(McpServer).all()
                if (srv.id or "").startswith(prefix)]

    # ------------------------------------------------------------------
    # Zip validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_members(zf: zipfile.ZipFile, dest_dir: str) -> List[zipfile.ZipInfo]:
        """Zip-slip prevention: resolve every member path and require it to
        stay inside ``dest_dir``. Rejects absolute paths, ``..`` components,
        symlink members and zip bombs. Returns the file members to extract."""
        dest_real = os.path.realpath(dest_dir)
        members: List[zipfile.ZipInfo] = []
        total_uncompressed = 0
        for info in zf.infolist():
            raw = info.filename or ""
            norm = raw.replace("\\", "/")
            if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
                raise PluginError(f"unsafe path in zip (absolute): {raw!r}")
            if any(part == ".." for part in norm.split("/")):
                raise PluginError(f"unsafe path in zip (traversal): {raw!r}")
            if _member_is_symlink(info):
                raise PluginError(f"unsafe member in zip (symlink): {raw!r}")
            target = os.path.realpath(os.path.join(dest_real, norm))
            if os.path.commonpath([dest_real, target]) != dest_real:
                raise PluginError(f"unsafe path in zip (escapes target): {raw!r}")
            if info.is_dir():
                continue
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_PLUGIN_UNCOMPRESSED_BYTES:
                raise PluginError("plugin zip expands past the uncompressed size cap")
            members.append(info)
        return members

    @staticmethod
    def _strip_prefix(names: List[str]) -> str:
        """If every entry lives under one top-level folder (folder-zipped
        bundle), return that ``folder/`` prefix; else empty string."""
        tops = {n.replace("\\", "/").split("/", 1)[0] for n in names if n.strip("/")}
        if len(tops) == 1:
            top = next(iter(tops))
            if all("/" in n.replace("\\", "/") or n.rstrip("/") == top
                   for n in names if n.strip("/")):
                return top + "/"
        return ""

    @staticmethod
    def _parse_manifest(raw: bytes) -> Dict:
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise PluginError(f"plugin.json is not valid JSON: {e}") from e
        if not isinstance(manifest, dict):
            raise PluginError("plugin.json must be a JSON object")
        name = manifest.get("name")
        if not isinstance(name, str) or not PLUGIN_NAME_RE.match(name):
            raise PluginError(
                "plugin.json 'name' must match ^[a-z0-9][a-z0-9-]{1,40}$")
        version = manifest.get("version")
        if not isinstance(version, str) or not version.strip():
            raise PluginError("plugin.json 'version' is required")
        return manifest

    @staticmethod
    def _parse_mcp_config(raw: bytes, plugin_name: str) -> List[Dict]:
        from services.memory.skill_format import slugify
        try:
            entries = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise PluginError(f"mcp.json is not valid JSON: {e}") from e
        if isinstance(entries, dict):
            # Tolerate {"servers": [...]} wrapping.
            entries = entries.get("servers", [])
        if not isinstance(entries, list):
            raise PluginError("mcp.json must be a list of server objects")
        out: List[Dict] = []
        seen_ids: set = set()
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise PluginError(f"mcp.json entry {i} must be an object")
            srv_name = entry.get("name")
            if not isinstance(srv_name, str) or not srv_name.strip():
                raise PluginError(f"mcp.json entry {i}: 'name' is required")
            transport = entry.get("transport", "stdio")
            if transport not in MCP_TRANSPORTS:
                raise PluginError(
                    f"mcp.json entry {i}: transport must be one of {MCP_TRANSPORTS}")
            command = entry.get("command")
            url = entry.get("url")
            if transport == "stdio" and not command:
                raise PluginError(f"mcp.json entry {i}: 'command' is required for stdio")
            if transport in ("sse", "http") and not url:
                raise PluginError(f"mcp.json entry {i}: 'url' is required for {transport}")
            args = entry.get("args") or []
            env = entry.get("env") or {}
            if not isinstance(args, list) or not isinstance(env, dict):
                raise PluginError(f"mcp.json entry {i}: 'args' must be a list and 'env' an object")
            server_id = f"plugin_{plugin_name}_{slugify(srv_name, fallback=str(i))}"
            if server_id in seen_ids:
                raise PluginError(f"mcp.json entry {i}: duplicate server name {srv_name!r}")
            seen_ids.add(server_id)
            out.append({
                "id": server_id,
                "name": srv_name.strip(),
                "transport": transport,
                "command": command,
                "url": url,
                "args": args,
                "env": env,
            })
        return out

    # ------------------------------------------------------------------
    # Install / uninstall
    # ------------------------------------------------------------------

    def install(self, zip_bytes: bytes, owner: Optional[str] = None) -> Dict:
        """Validate and install a plugin bundle. Reinstalling an already
        installed plugin is an upgrade: old artifacts are removed first."""
        if not zip_bytes:
            raise PluginError("empty upload")
        if len(zip_bytes) > MAX_PLUGIN_ZIP_BYTES:
            raise PluginError(
                f"plugin zip exceeds the {MAX_PLUGIN_ZIP_BYTES // (1024 * 1024)}MB size cap")
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile as e:
            raise PluginError("upload is not a valid zip archive") from e

        with zf:
            names = zf.namelist()
            prefix = "" if "plugin.json" in names else self._strip_prefix(names)
            manifest_member = f"{prefix}plugin.json"
            if manifest_member not in names:
                raise PluginError("plugin.json is missing from the bundle root")
            manifest = self._parse_manifest(zf.read(manifest_member))
            name = manifest["name"]

            # Validate optional mcp.json BEFORE any destructive step.
            mcp_member = f"{prefix}mcp.json"
            mcp_entries = (self._parse_mcp_config(zf.read(mcp_member), name)
                           if mcp_member in names else [])

            plugin_dir = os.path.join(self.plugins_dir, name)
            members = self._validate_members(zf, plugin_dir)

            # Upgrade path: clear previous skills / MCP rows / files first.
            self._remove_artifacts(name)

            os.makedirs(plugin_dir, exist_ok=True)
            for info in members:
                rel = info.filename.replace("\\", "/")
                if prefix and rel.startswith(prefix):
                    rel = rel[len(prefix):]
                if not rel:
                    continue
                dest = os.path.join(plugin_dir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)

        skills = self._register_skills(name, plugin_dir, owner=owner)
        servers = self._register_mcp_servers(mcp_entries)

        logger.info("Plugin %s v%s installed (%d skills, %d MCP servers)",
                    name, manifest["version"], len(skills), len(servers))
        return {
            "name": name,
            "version": manifest["version"],
            "description": manifest.get("description", ""),
            "skills": skills,
            "mcp_servers": servers,
        }

    def _register_skills(self, name: str, plugin_dir: str,
                         owner: Optional[str] = None) -> List[str]:
        """Register every ``skills/<skill>/SKILL.md`` with the SkillsManager
        under category ``plugin-<name>`` (existing on-disk skill format)."""
        from services.memory.skill_importer import SkillImportError, _is_text_file

        skills_src = os.path.join(plugin_dir, "skills")
        installed: List[str] = []
        if not os.path.isdir(skills_src):
            return installed
        for entry in sorted(os.listdir(skills_src)):
            skill_dir = os.path.join(skills_src, entry)
            skill_md = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            files: Dict[str, str] = {}
            for root, _dirs, fnames in os.walk(skill_dir, followlinks=False):
                for fname in fnames:
                    if not _is_text_file(fname):
                        continue  # binaries stay in the plugin dir only
                    fpath = os.path.join(root, fname)
                    rel = os.path.relpath(fpath, skill_dir).replace(os.sep, "/")
                    try:
                        with open(fpath, encoding="utf-8") as f:
                            files[rel] = f.read()
                    except (OSError, UnicodeDecodeError) as e:
                        logger.warning("Skipping unreadable plugin file %s: %s", fpath, e)
            try:
                sk = self.skills_manager.import_bundle_from_files(
                    files, owner=owner, category=f"plugin-{name}",
                    source_url=f"plugin:{name}")
                # An admin explicitly installing a plugin IS the approval step:
                # without publish the skill stays a draft and never reaches the
                # prompt index or the slash-command catalog.
                self.skills_manager.update_skill(
                    sk["name"], {"status": "published"}, owner=owner)
                installed.append(sk["name"])
            except SkillImportError as e:
                logger.warning("Plugin %s: skill %s rejected: %s", name, entry, e)
        return installed

    def _register_mcp_servers(self, entries: List[Dict]) -> List[Dict]:
        """Create McpServer rows, ALWAYS disabled and never connected.
        Mirrors the row shape of POST /api/mcp/servers (routes/mcp_routes.py)
        minus the connect step — the admin must enable each server explicitly."""
        if not entries:
            return []
        from core.database import McpServer
        created: List[Dict] = []
        db = self._db()
        try:
            for e in entries:
                srv = McpServer(
                    id=e["id"],
                    name=e["name"],
                    transport=e["transport"],
                    command=e["command"],
                    args=json.dumps(e["args"]),
                    env=json.dumps(e["env"]),
                    url=e["url"],
                    is_enabled=False,  # NEVER auto-enabled
                )
                db.add(srv)
                created.append({"id": e["id"], "name": e["name"],
                                "transport": e["transport"], "is_enabled": False})
            db.commit()
        finally:
            db.close()
        return created

    def _remove_artifacts(self, name: str) -> bool:
        """Remove a plugin's skills category, MCP rows and extracted files.
        Tolerant of partial state (used by both uninstall and upgrade)."""
        removed = False
        # Skills: whole category dir plugin-<name> under the skills root.
        cat_dir = os.path.join(self.skills_manager.skills_root, f"plugin-{name}")
        if os.path.isdir(cat_dir):
            shutil.rmtree(cat_dir, ignore_errors=True)
            removed = True
        # MCP rows with our id prefix.
        db = self._db()
        try:
            rows = self._mcp_rows_for(db, name)
            for srv in rows:
                db.delete(srv)
                removed = True
            if rows:
                db.commit()
        finally:
            db.close()
        # Extracted plugin files.
        plugin_dir = os.path.join(self.plugins_dir, name)
        if os.path.isdir(plugin_dir):
            shutil.rmtree(plugin_dir, ignore_errors=True)
            removed = True
        return removed

    def uninstall(self, name: str) -> Dict:
        if not isinstance(name, str) or not PLUGIN_NAME_RE.match(name):
            raise PluginError("invalid plugin name")
        if not os.path.isdir(os.path.join(self.plugins_dir, name)):
            raise PluginNotInstalledError(f"plugin {name!r} is not installed")
        self._remove_artifacts(name)
        logger.info("Plugin %s uninstalled", name)
        return {"status": "uninstalled", "name": name}

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_plugins(self) -> List[Dict]:
        out: List[Dict] = []
        if not os.path.isdir(self.plugins_dir):
            return out
        db = self._db()
        try:
            for entry in sorted(os.listdir(self.plugins_dir)):
                plugin_dir = os.path.join(self.plugins_dir, entry)
                manifest_path = os.path.join(plugin_dir, "plugin.json")
                if not os.path.isfile(manifest_path):
                    continue
                try:
                    with open(manifest_path, encoding="utf-8") as f:
                        manifest = json.load(f)
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning("Unreadable plugin manifest %s: %s", manifest_path, e)
                    manifest = {}
                name = manifest.get("name") or entry
                cat_dir = os.path.join(self.skills_manager.skills_root, f"plugin-{name}")
                skills_count = 0
                if os.path.isdir(cat_dir):
                    skills_count = sum(
                        1 for root, _d, files in os.walk(cat_dir, followlinks=False)
                        if "SKILL.md" in files)
                servers = [{"id": srv.id, "name": srv.name,
                            "transport": srv.transport,
                            "is_enabled": bool(srv.is_enabled)}
                           for srv in self._mcp_rows_for(db, name)]
                out.append({
                    "name": name,
                    "version": manifest.get("version", ""),
                    "description": manifest.get("description", ""),
                    "skills_count": skills_count,
                    "mcp_servers": servers,
                })
        finally:
            db.close()
        return out
