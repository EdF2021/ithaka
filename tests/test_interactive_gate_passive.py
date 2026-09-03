"""Regression test for issue #7: podcast-statuspoll must not count as
foreground activity in the interactive gate.

`should_track_interactive_request` matched only exact paths / static
prefixes, so the podcast job-status poller (a dynamic-id path,
GET /api/notebooks/{notebook_id}/podcast/{job_id}, polled every 2s by
static/js/notebooks.js's _pollPodcast) fell through to "track as
interactive" for the whole lifetime of a podcast job (up to
JOB_TIMEOUT_SECONDS = 1800s). That crowded out genuine background work
(scheduler, email polling) far more than the poll interval alone would
suggest, since it stacked with the podcast job's own foreground LLM/TTS
work and normal browser use.

This test exercises the new `_PASSIVE_PATTERNS` regex branch directly
against `should_track_interactive_request`.
"""
from src.interactive_gate import should_track_interactive_request


def test_podcast_status_poll_get_is_not_tracked():
    path = "/api/notebooks/nb-123/podcast/job-456"
    assert should_track_interactive_request(path, "GET") is False


def test_podcast_job_start_post_is_still_tracked():
    # Same notebook, but this is the job-start route (no job-id segment) —
    # it cannot match the status-poll regex, and it must keep counting as a
    # real foreground/user action.
    path = "/api/notebooks/nb-123/podcast"
    assert should_track_interactive_request(path, "POST") is True


def test_similar_but_longer_podcast_path_is_still_tracked():
    # One segment too many after /podcast/ — must not match the narrow
    # status-poll shape.
    path = "/api/notebooks/nb-123/podcast/job-456/extra"
    assert should_track_interactive_request(path, "GET") is True


def test_similar_notebooks_route_is_still_tracked():
    # A different, non-podcast notebooks sub-route must not be swept up by
    # a too-broad pattern.
    path = "/api/notebooks/nb-123/sources"
    assert should_track_interactive_request(path, "GET") is True


def test_podcast_status_poll_post_is_still_tracked():
    # The regex branch is GET-only (method is already a parameter of this
    # function): a POST to the same path shape must not be silently swept
    # into the passive bucket alongside the real status poll.
    path = "/api/notebooks/nb-123/podcast/job-456"
    assert should_track_interactive_request(path, "POST") is True


# --- existing exact/prefix behavior stays unchanged (one spot check each) --

def test_existing_exact_path_still_passive():
    assert should_track_interactive_request("/api/activity/heartbeat", "GET") is False


def test_existing_prefix_still_passive():
    assert should_track_interactive_request("/api/health/live", "GET") is False


# ── fase 4c: video-statuspoll gets the same passive treatment ─────────────

def test_video_status_poll_get_is_not_tracked():
    path = "/api/notebooks/nb-123/video/job-456"
    assert should_track_interactive_request(path, "GET") is False


def test_video_job_start_post_is_still_tracked():
    path = "/api/notebooks/nb-123/video"
    assert should_track_interactive_request(path, "POST") is True


def test_similar_but_longer_video_path_is_still_tracked():
    path = "/api/notebooks/nb-123/video/job-456/extra"
    assert should_track_interactive_request(path, "GET") is True


def test_infographic_illustrations_status_poll_get_is_not_tracked():
    assert should_track_interactive_request(
        "/api/notebooks/nb-123/artifacts/art-456/illustrations", "GET") is False


def test_infographic_illustrations_post_and_sibling_paths_are_still_tracked():
    assert should_track_interactive_request(
        "/api/notebooks/nb-123/artifacts/art-456/illustrations", "POST") is True
    assert should_track_interactive_request(
        "/api/notebooks/nb-123/artifacts/art-456/report", "GET") is True
    assert should_track_interactive_request(
        "/api/notebooks/nb-123/artifacts/art-456/illustrations/extra", "GET") is True
