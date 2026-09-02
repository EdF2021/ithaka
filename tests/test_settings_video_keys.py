"""Task 4 (image/video auto-route plan): settings defaults for video
generation. Mirrors the image_* keys already in DEFAULT_SETTINGS /
_PER_USER_KEYS — see docs/superpowers/plans/2026-09-02-image-video-autoroute.md,
Task 4.
"""

from src.settings import DEFAULT_SETTINGS, _PER_USER_KEYS, get_setting


def test_video_defaults_present_with_exact_values():
    assert DEFAULT_SETTINGS["video_gen_enabled"] is False
    assert DEFAULT_SETTINGS["video_model"] == "veo-3.1-generate-preview"
    assert DEFAULT_SETTINGS["video_resolution"] == "720p"
    assert DEFAULT_SETTINGS["video_aspect_ratio"] == "16:9"
    assert DEFAULT_SETTINGS["video_duration_seconds"] == 8


def test_video_keys_are_per_user():
    for key in (
        "video_gen_enabled",
        "video_model",
        "video_resolution",
        "video_aspect_ratio",
        "video_duration_seconds",
    ):
        assert key in _PER_USER_KEYS


def test_get_setting_video_model_default(tmp_path, monkeypatch):
    from src import settings as settings_module

    monkeypatch.setattr(settings_module, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_module._invalidate_caches()
    assert get_setting("video_model") == "veo-3.1-generate-preview"
    assert get_setting("video_gen_enabled") is False
    assert get_setting("video_resolution") == "720p"
    assert get_setting("video_aspect_ratio") == "16:9"
    assert get_setting("video_duration_seconds") == 8
