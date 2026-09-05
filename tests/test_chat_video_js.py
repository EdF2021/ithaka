"""Coverage for inline video-in-chat rendering (generate_video auto-routing,
Task 5 of docs/superpowers/plans/2026-09-02-image-video-autoroute.md).

chatRenderer.js pulls in browser-only globals transitively (ui.js, markdown.js,
tts-ai.js, spinner.js, ...) and can't be imported wholesale under plain
`node --input-type=module` (same limitation documented in
test_welcome_actions_js.py / test_copy_message_strips_thinking_js.py). The
three new builders (buildVideoPendingBubble, buildVideoBubble,
renderVideoError) only touch `document.createElement` and
`spinnerModule.createWhirlpool`, so we extract their source text and run it
under node with a minimal fake-DOM shim — real behavioral coverage rather
than source-grep assertions alone.

chat.js's `json.video_job_id` handling and the pollers's session-switch/
timeout/error branches are covered at the source level, mirroring
test_chat_tool_screenshot_xss.py's approach for similarly wiring-heavy code.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_RENDERER = _REPO / "static" / "js" / "chatRenderer.js"
_CHAT = _REPO / "static" / "js" / "chat.js"
_HAS_NODE = shutil.which("node") is not None


def _extract_function(source: str, name: str) -> str:
    """Pull one top-level `export function <name>(...) { ... }` out of a
    module by brace-counting from its opening `{` (regex alone can't find
    the matching close reliably once a function body has nested blocks)."""
    marker = f"export function {name}("
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0
    i = brace_start
    while True:
        c = source[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = source[start : i + 1]
    return body.replace("export function", "function", 1)


_FAKE_DOM_PRELUDE = r"""
class FakeEl {
  constructor(tag) {
    this.tagName = String(tag || '').toUpperCase();
    this.className = '';
    this.style = {};
    this.dataset = {};
    this.children = [];
    this._text = '';
  }
  appendChild(c) { this.children.push(c); return c; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text; }
  querySelector(sel) {
    const cls = sel.replace('.', '');
    const walk = (node) => {
      if (node.className && node.className.split(' ').includes(cls)) return node;
      for (const c of (node.children || [])) {
        const found = walk(c);
        if (found) return found;
      }
      return null;
    };
    for (const c of this.children) {
      const found = walk(c);
      if (found) return found;
    }
    return null;
  }
}
const document = { createElement: (tag) => new FakeEl(tag) };
const spinnerModule = { createWhirlpool: (size) => ({ element: new FakeEl('span'), size }) };

function dump(el) {
  if (el == null) return null;
  return {
    tag: el.tagName,
    className: el.className,
    text: el.textContent,
    src: el.src === undefined ? null : el.src,
    controls: el.controls === undefined ? null : el.controls,
    preload: el.preload === undefined ? null : el.preload,
    maxWidth: (el.style && el.style.maxWidth) || null,
    color: (el.style && el.style.color) || null,
    dataset: el.dataset,
    children: (el.children || []).map(dump),
  };
}
"""


def _run_builders(call_js: str) -> dict:
    if not _HAS_NODE:
        pytest.skip("node binary not on PATH")
    source = _RENDERER.read_text(encoding="utf-8")
    fns = "\n\n".join(
        _extract_function(source, name)
        for name in ("buildVideoPendingBubble", "buildVideoBubble", "renderVideoError")
    )
    script = _FAKE_DOM_PRELUDE + "\n" + fns + "\n" + call_js
    result = subprocess.run(
        ["node", "-e", script],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed:\nSTDERR:\n{result.stderr}\nSTDOUT:\n{result.stdout}")
    return json.loads(result.stdout.splitlines()[-1])


# ── buildVideoPendingBubble ─────────────────────────────────────────────


def test_pending_bubble_shows_model_and_cost_and_spinner():
    out = _run_builders(
        """
        const el = buildVideoPendingBubble('abc123', 'veo-3.1-generate-preview', 3.2);
        console.log(JSON.stringify(dump(el)));
        """
    )
    assert set(out["className"].split()) >= {"msg", "msg-ai", "generated-image-wrap", "generated-video-wrap"}
    assert out["dataset"]["videoJobId"] == "abc123"
    # role line shows the short model name
    role = out["children"][0]
    assert role["className"] == "role"
    assert role["text"] == "veo-3.1-generate-preview"
    # status row: spinner + cost/label text
    status = out["children"][1]["children"][0]
    assert status["className"] == "thinking-indicator generated-video-status"
    assert len(status["children"]) == 2
    spinner_el, label_el = status["children"]
    assert spinner_el["tag"] == "SPAN"  # the whirlpool spinner's .element
    assert "Generating video with veo-3.1-generate-preview" in label_el["text"]
    assert "usually 1-3 min" in label_el["text"]
    assert "~$3.20" in label_el["text"]


def test_pending_bubble_without_cost_omits_dollar_sign():
    out = _run_builders(
        """
        const el = buildVideoPendingBubble('job2', 'veo-3.1-fast-generate-preview', null);
        console.log(JSON.stringify(dump(el)));
        """
    )
    label_el = out["children"][1]["children"][0]["children"][1]
    assert "usually 1-3 min" in label_el["text"]
    assert "$" not in label_el["text"]


# ── buildVideoBubble ─────────────────────────────────────────────────────


def test_finished_bubble_renders_video_element_with_src():
    out = _run_builders(
        """
        const el = buildVideoBubble({video_url: '/api/video/abc123.mp4', model: 'veo-3.1-generate-preview', prompt: 'waves at sunset'});
        console.log(JSON.stringify(dump(el)));
        """
    )
    body = out["children"][1]
    video = body["children"][0]
    assert video["tag"] == "VIDEO"
    assert video["src"] == "/api/video/abc123.mp4"
    assert video["controls"] is True
    assert video["preload"] == "metadata"
    assert video["maxWidth"] == "100%"
    caption = body["children"][1]
    assert "veo-3.1-generate-preview" in caption["text"]
    assert "waves at sunset" in caption["text"]


def test_finished_bubble_without_url_shows_placeholder_text():
    out = _run_builders(
        """
        const el = buildVideoBubble({model: 'veo-3.1-generate-preview'});
        console.log(JSON.stringify(dump(el)));
        """
    )
    body = out["children"][1]
    assert body["text"] == "[Video unavailable]"
    assert body["children"] == []


# ── renderVideoError ─────────────────────────────────────────────────────


def test_render_video_error_replaces_body_with_red_line():
    out = _run_builders(
        """
        const pending = buildVideoPendingBubble('job3', 'veo-3.1-generate-preview', 1.6);
        renderVideoError(pending, 'Blocked by Veo safety filter');
        console.log(JSON.stringify(dump(pending)));
        """
    )
    body = out["children"][1]
    assert len(body["children"]) == 1
    err = body["children"][0]
    assert err["text"] == "Blocked by Veo safety filter"
    assert err["color"] == "var(--red)"


def test_render_video_error_default_message():
    out = _run_builders(
        """
        const pending = buildVideoPendingBubble('job4', 'veo-3.1-generate-preview', 1.6);
        renderVideoError(pending, undefined);
        console.log(JSON.stringify(dump(pending)));
        """
    )
    err = out["children"][1]["children"][0]
    assert err["text"] == "Video generation failed"


# ── chat.js wiring (source-level, mirrors test_chat_tool_screenshot_xss.py) ──


def test_chat_js_handles_video_job_id_sse_event():
    chat = _CHAT.read_text(encoding="utf-8")
    assert "if (json.video_job_id)" in chat
    assert "chatRenderer.buildVideoPendingBubble(json.video_job_id, json.video_model, json.video_cost_estimate)" in chat
    assert "startVideoJobPoll(json.video_job_id, videoBubble" in chat


def test_chat_js_exports_start_video_job_poll():
    chat = _CHAT.read_text(encoding="utf-8")
    assert "function startVideoJobPoll(jobId, bubbleEl" in chat
    assert "startVideoJobPoll,\n" in chat  # present in the public chatModule API


def test_chat_js_video_poll_has_timeout_and_error_and_404_paths():
    chat = _CHAT.read_text(encoding="utf-8")
    assert "VIDEO_POLL_MAX_MS" in chat
    assert "Video is taking too long; check back later" in chat
    assert "Video job no longer known" in chat
    assert "chatRenderer.renderVideoError(bubbleEl, job.error || 'Video generation failed')" in chat
    assert "chatRenderer.buildVideoBubble(job)" in chat


def test_chat_js_video_poll_stops_on_session_switch():
    chat = _CHAT.read_text(encoding="utf-8")
    assert "sessionModule.getCurrentSessionId() !== startedSessionId" in chat


def test_chat_js_video_poll_fires_first_check_immediately():
    # A job that already finished (e.g. reload landing after completion, or
    # one that's since vanished) should resolve right away, not after the
    # first 5s interval tick.
    chat = _CHAT.read_text(encoding="utf-8")
    fn_start = chat.index("function startVideoJobPoll(jobId, bubbleEl")
    fn_body = chat[fn_start : fn_start + 3000]
    assert "const intervalId = setInterval(tick, VIDEO_POLL_INTERVAL_MS);" in fn_body
    assert fn_body.index("tick();") > fn_body.index("const intervalId = setInterval(tick, VIDEO_POLL_INTERVAL_MS);")


def test_chat_js_video_poll_guards_detached_bubble():
    chat = _CHAT.read_text(encoding="utf-8")
    assert "if (bubbleEl.isConnected)" in chat


def test_chat_renderer_history_replay_restarts_or_renders_video():
    renderer = _RENDERER.read_text(encoding="utf-8")
    assert "if (ev.video_job_id)" in renderer
    assert "buildVideoBubble({ video_url: ev.video_url, model: ev.video_model, prompt: ev.command, job_id: ev.video_job_id })" in renderer
    assert "window.chatModule?.startVideoJobPoll?.(ev.video_job_id, videoBubble)" in renderer


def test_chat_renderer_exports_new_builders():
    renderer = _RENDERER.read_text(encoding="utf-8")
    assert "export function buildVideoPendingBubble(jobId, model, costEstimate)" in renderer
    assert "export function buildVideoBubble(job)" in renderer
    assert "export function renderVideoError(bubbleEl, message)" in renderer
    for name in ("buildVideoBubble", "buildVideoPendingBubble", "renderVideoError"):
        assert f"  {name},\n" in renderer  # listed in the default chatRenderer export object


# ── node --check on both changed files ───────────────────────────────────


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_node_check_chat_js():
    result = subprocess.run(["node", "--check", str(_CHAT)], cwd=_REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_node_check_chat_renderer_js():
    result = subprocess.run(["node", "--check", str(_RENDERER)], cwd=_REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
