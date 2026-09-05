"""can_generate_videos privilege gate + video_gen_enabled global gate for the
generate_video tool in routes/chat_routes.py.

routes/chat_routes.py's streaming handler is not practically invokable in a
unit test (huge function wired to live session/db/model state), so this pins
the source text directly — the same technique used for the equivalent
can_generate_images gate (tests/test_gallery_image_privileges.py) and for the
sibling test_agent_loop_video_forward.py.
"""
from pathlib import Path


def _source():
    return Path("routes/chat_routes.py").read_text(encoding="utf-8")


def test_can_generate_videos_privilege_disables_tool():
    source = _source()
    assert 'if not _privs.get("can_generate_videos", True):' in source
    assert 'disabled_tools.add("generate_video")' in source


def test_video_gen_enabled_global_setting_disables_tool():
    source = _source()
    # Per-user override (get_user_setting, see src/settings.py _PER_USER_KEYS)
    # so a user who enabled video generation for themselves isn't blocked by
    # a global default-off admin setting.
    assert 'get_user_setting("video_gen_enabled", _user or "", False)' in source
    # Must appear near the global-disable gate and add generate_video, mirroring
    # how image_gen_enabled gates generate_image.
    idx = source.index('get_user_setting("video_gen_enabled", _user or "", False)')
    tail = source[idx:idx + 300]
    assert 'disabled_tools.add("generate_video")' in tail


def test_can_generate_videos_privilege_default_in_core_auth():
    auth_source = Path("core/auth.py").read_text(encoding="utf-8")
    assert '"can_generate_videos": True' in auth_source


def test_admin_js_has_video_generation_label():
    admin_js = Path("static/js/admin.js").read_text(encoding="utf-8")
    assert "can_generate_videos: 'Video generation'" in admin_js
