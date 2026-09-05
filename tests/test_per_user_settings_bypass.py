"""Per-user settings bypass fix (docs/sessions notebook-infographic v2 follow-up).

src/settings.py's get_user_setting(key, owner, default) resolves a per-user
pref (routes.prefs_routes._load_for_user) before falling back to the global
admin default, but is only consulted for the whitelist in _PER_USER_KEYS.
Several call sites for keys in that whitelist were reading the global
get_setting() directly, silently ignoring a user's own preference. This file
covers the two sites without an existing natural test-file home:

- src/chat_handler.py: vision_enabled / vision_model (video_gen.py, the
  video_tools/video_routes/chat_routes/notebook_routes call sites, and
  src/agent_loop.py's _build_base_prompt are covered in their own existing
  test files: tests/test_video_gen.py, tests/test_agent_tools_video.py,
  tests/test_routes_video.py, tests/test_chat_routes_video_privilege.py,
  tests/test_chat_image_routing.py, tests/test_routes_notebook_infographic.py).
- src/agent_loop.py: _build_base_prompt's image_gen_enabled gate on the
  generate_image tool.
"""
import json
import types
import sys


# --------------------------------------------------------------------------
# src/chat_handler.py: vision_enabled / vision_model
# --------------------------------------------------------------------------

def _stub_core_database_for_route_imports(monkeypatch):
    from unittest.mock import MagicMock

    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = []
    models = types.ModuleType("core.models")
    models.ChatMessage = MagicMock()

    db = types.ModuleType("core.database")
    for name in (
        "SessionLocal", "Session", "ChatMessage", "Document",
        "DocumentVersion", "GalleryImage", "ModelEndpoint",
    ):
        setattr(db, name, MagicMock())
    monkeypatch.setitem(sys.modules, "core", core_pkg)
    monkeypatch.setitem(sys.modules, "core.models", models)
    monkeypatch.setitem(sys.modules, "core.database", db)


def _make_upload_store(tmp_path, owner, upload_id):
    upload_dir = tmp_path / f"uploads-{owner}"
    dated = upload_dir / "2026" / "06" / "01"
    dated.mkdir(parents=True)
    path = dated / upload_id
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    index = {
        f"{owner}:h1": {
            "id": upload_id,
            "path": str(path),
            "mime": "image/png",
            "size": path.stat().st_size,
            "name": "img.png",
            "original_name": "img.png",
            "owner": owner,
        },
    }
    (upload_dir / "uploads.json").write_text(json.dumps(index), encoding="utf-8")
    return upload_dir


def _fresh_chat_handler_module(monkeypatch):
    """(Re-)import src.chat_handler once per test against stubbed
    core.models/core.database (mirrors tests/test_security_regressions.py's
    _stub_core_database_for_route_imports pattern) and return the module —
    callers patch module attributes on this same object and build as many
    ChatHandler instances from it as needed, so a second reimport never
    silently drops earlier patches onto a stale module object."""
    sys.modules.pop("src.chat_handler", None)
    _stub_core_database_for_route_imports(monkeypatch)
    import src.chat_handler as chat_handler_mod
    return chat_handler_mod


def _make_handler(chat_handler_mod, tmp_path, owner, upload_id):
    from src.upload_handler import UploadHandler

    upload_dir = _make_upload_store(tmp_path, owner, upload_id)
    handler = UploadHandler(str(tmp_path), str(upload_dir))
    return chat_handler_mod.ChatHandler(None, None, None, None, None, handler)


def test_vision_enabled_uses_owner_pref_over_disabled_global(monkeypatch, tmp_path):
    """vision_enabled is in src.settings._PER_USER_KEYS: an owner with an
    explicit per-user override must not be blocked by a global admin default
    of False, and a different owner with no such pref must still be blocked."""
    import asyncio
    from types import SimpleNamespace

    upload_id_ed = "a" * 32 + ".png"
    upload_id_other = "b" * 32 + ".png"

    import src.settings as settings_mod
    monkeypatch.setattr(
        settings_mod, "get_setting",
        lambda key, default=None: False if key == "vision_enabled" else default,
    )

    def fake_load_for_user(owner=None):
        return {"vision_enabled": True} if owner == "ed" else {}
    monkeypatch.setattr("routes.prefs_routes._load_for_user", fake_load_for_user)

    chat_handler_mod = _fresh_chat_handler_module(monkeypatch)
    seen_calls = []
    monkeypatch.setattr(
        chat_handler_mod, "model_supports_vision",
        lambda model, endpoint_url: seen_calls.append((model, endpoint_url)) or True,
    )

    # ed has an explicit per-user override -> vision path is entered.
    chat_handler = _make_handler(chat_handler_mod, tmp_path, "ed", upload_id_ed)
    sess = SimpleNamespace(id="s1", owner="ed", model="text-model", endpoint_url="")
    _enh, _uc, _tc, _yt, attachment_meta = asyncio.run(
        chat_handler.preprocess_message("hello", [upload_id_ed], sess)
    )
    assert seen_calls, "vision_enabled per-user override must let model_supports_vision run"
    assert attachment_meta and attachment_meta[0].get("vision_model") == "text-model"

    # A different owner has no per-user pref -> falls back to the disabled
    # global default, so the vision path (and model_supports_vision) is
    # skipped entirely.
    chat_handler2 = _make_handler(chat_handler_mod, tmp_path, "other", upload_id_other)
    sess2 = SimpleNamespace(id="s2", owner="other", model="text-model", endpoint_url="")
    _enh2, _uc2, _tc2, _yt2, attachment_meta2 = asyncio.run(
        chat_handler2.preprocess_message("hello", [upload_id_other], sess2)
    )
    assert len(seen_calls) == 1, "no per-user pref -> disabled global must block vision entirely"
    assert not any(m.get("vision") for m in attachment_meta2)


def test_vision_model_uses_owner_pref_over_global_default(monkeypatch, tmp_path):
    """vision_model is in src.settings._PER_USER_KEYS. When the main model
    isn't vision-capable, the VL-description branch reads vision_model via
    get_user_setting — an owner's per-user pref must win over the global
    default. Uses a cached vision description (_load_vision_cache) so the
    resolved vl_model isn't overwritten by analyze_image_with_vl_result's
    own return value (which only happens on an actual, uncached VL call)."""
    import asyncio
    from types import SimpleNamespace

    upload_id_ed = "c" * 32 + ".png"
    upload_id_other = "d" * 32 + ".png"

    import src.settings as settings_mod
    monkeypatch.setattr(
        settings_mod, "get_setting",
        lambda key, default=None: (
            True if key == "vision_enabled" else
            "global-vl-model" if key == "vision_model" else default
        ),
    )

    def fake_load_for_user(owner=None):
        return {"vision_model": "ed-vl-model"} if owner == "ed" else {}
    monkeypatch.setattr("routes.prefs_routes._load_for_user", fake_load_for_user)

    chat_handler_mod = _fresh_chat_handler_module(monkeypatch)
    monkeypatch.setattr(chat_handler_mod, "model_supports_vision", lambda model, endpoint_url: False)
    monkeypatch.setattr(chat_handler_mod, "_load_vision_cache", lambda att_id: "a cached description")
    monkeypatch.setattr(chat_handler_mod, "_sync_upload_vision_to_gallery", lambda *a, **k: None)

    def fail_if_called(*a, **k):
        raise AssertionError("analyze_image_with_vl_result must not run when a cache hit exists")
    monkeypatch.setattr(chat_handler_mod, "analyze_image_with_vl_result", fail_if_called)

    chat_handler = _make_handler(chat_handler_mod, tmp_path, "ed", upload_id_ed)
    sess = SimpleNamespace(id="s1", owner="ed", model="text-only-model", endpoint_url="")
    _enh, _uc, _tc, _yt, attachment_meta = asyncio.run(
        chat_handler.preprocess_message("hello", [upload_id_ed], sess)
    )
    assert attachment_meta[0]["vision_model"] == "ed-vl-model"

    chat_handler2 = _make_handler(chat_handler_mod, tmp_path, "other", upload_id_other)
    sess2 = SimpleNamespace(id="s2", owner="other", model="text-only-model", endpoint_url="")
    _enh2, _uc2, _tc2, _yt2, attachment_meta2 = asyncio.run(
        chat_handler2.preprocess_message("hello", [upload_id_other], sess2)
    )
    assert attachment_meta2[0]["vision_model"] == "global-vl-model"


# --------------------------------------------------------------------------
# src/agent_loop.py: _build_base_prompt's image_gen_enabled gate
# --------------------------------------------------------------------------

def test_build_base_prompt_disables_generate_image_per_owner(monkeypatch):
    """image_gen_enabled is in _PER_USER_KEYS: _build_base_prompt must resolve
    it per the `owner` it's called with (already a keyword param, threaded by
    its caller _cached_agent_base_prompt), not the global admin default."""
    import src.agent_loop as agent_loop
    import src.settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "get_setting",
        lambda key, default=None: False if key == "image_gen_enabled" else default,
    )

    def fake_load_for_user(owner=None):
        return {"image_gen_enabled": True} if owner == "ed" else {}
    monkeypatch.setattr("routes.prefs_routes._load_for_user", fake_load_for_user)

    _prompt_ed, _ = agent_loop._build_base_prompt(
        disabled_tools=set(), mcp_mgr=None, needs_admin=False,
        relevant_tools={"generate_image", "ask_user", "update_plan"},
        owner="ed",
    )
    _prompt_other, _ = agent_loop._build_base_prompt(
        disabled_tools=set(), mcp_mgr=None, needs_admin=False,
        relevant_tools={"generate_image", "ask_user", "update_plan"},
        owner="other",
    )

    # generate_image's tool block only renders when it isn't in `disabled` —
    # assert via the section header text that _assemble_prompt would include
    # for an enabled tool vs strip for a disabled one.
    assert "generate_image" in _prompt_ed
    assert "generate_image" not in _prompt_other


# --------------------------------------------------------------------------
# Regression guard: a get_setting -> get_user_setting swap on a *function-local*
# import must not leave another get_setting() call in the same scope unbound.
# (Caught on review: routes/chat_routes.py kept get_setting("disabled_tools")
# after its local import was narrowed to get_user_setting -> NameError on
# every chat message. py_compile does not catch this; symtable does.)
# --------------------------------------------------------------------------

_SWAPPED_MODULES = (
    "routes/chat_routes.py",
    "routes/notebook_routes.py",
    "routes/video_routes.py",
    "src/agent_loop.py",
    "src/agent_tools/video_tools.py",
    "src/chat_handler.py",
    "src/video_gen.py",
)
_SETTING_NAMES = ("get_setting", "get_user_setting")


def _unbound_setting_refs(path):
    import symtable
    from pathlib import Path

    src = Path(path).read_text(encoding="utf-8")
    top = symtable.symtable(src, path, "exec")
    problems = []

    def bound_here(table, name):
        try:
            sym = table.lookup(name)
        except KeyError:
            return False
        return sym.is_assigned() or sym.is_imported() or sym.is_parameter()

    def walk(table, ancestors):
        for name in _SETTING_NAMES:
            try:
                sym = table.lookup(name)
            except KeyError:
                continue
            if not sym.is_referenced():
                continue
            chain = [table] + ancestors
            if not any(bound_here(t, name) for t in chain):
                problems.append(f"{path}: {name} referenced in {table.get_name()}() but never bound")
        for child in table.get_children():
            walk(child, [table] + ancestors)

    walk(top, [])
    return problems


def test_swapped_modules_keep_every_settings_getter_bound():
    import os
    root = os.path.join(os.path.dirname(__file__), "..")
    problems = []
    for rel in _SWAPPED_MODULES:
        problems.extend(_unbound_setting_refs(os.path.join(root, rel)))
    assert problems == [], "\n".join(problems)
