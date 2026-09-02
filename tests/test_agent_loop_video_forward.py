"""generate_video's video_* keys must reach both the tool_output SSE payload
and the persisted tool_events entry, the same way generate_image's image_*
keys already do.

agent_loop.py's streaming generator is not practically unit-testable in
isolation (it is a single very large async generator wired to the live
session/db state), so — like the existing image_url forwarding — this pins
the two source locations directly, the same technique
tests/test_gallery_image_privileges.py uses for routes/gallery_routes.py.
"""
from pathlib import Path


VIDEO_KEYS = ("video_job_id", "video_model", "video_status", "video_cost_estimate", "video_url")


def _source():
    return Path("src/agent_loop.py").read_text(encoding="utf-8")


def test_video_keys_forwarded_in_tool_output_sse():
    source = _source()
    for key in VIDEO_KEYS:
        assert f'"{key}"' in source, f"{key} missing from agent_loop.py forwarding"


def test_video_keys_forwarded_alongside_image_keys_twice():
    """Both the streamed tool_output_data dict and the persisted tool_event
    dict must carry the video keys — mirroring the two existing image_url
    forwarding sites (grep image_url in src/agent_loop.py)."""
    source = _source()
    video_tuple_occurrences = source.count(
        '("video_job_id", "video_model", "video_status", "video_cost_estimate", "video_url")'
    )
    assert video_tuple_occurrences >= 2, (
        "expected the video forwarding tuple at both the tool_output_data site "
        "(~line 3597) and the tool_event site (~line 3698)"
    )
