"""Regression coverage for issue #145 part (b): a streamed code fence
squeezed to a couple of characters per line on narrow (mobile) viewports.

Root cause (found via live browser inspection, not guessed — see the PR
description): static/js/streamingRenderer.js's append-mode fence streaming
(`appendOpenFence`) grows an open `<pre><code>` block by calling
`Text.appendData()` on an existing text node as new characters arrive,
instead of inserting new DOM nodes. static/js/chat.js's `.pre-compact`
classifier (`_markCompactPre`) is re-run from a `MutationObserver` that used
to watch only `childList` mutations, so it classified a freshly-streamed
`<pre>` once — while its first line was still short and the block was still
genuinely one line — and never re-evaluated it as `appendData()` grew the
block to many lines. `.pre-compact` carries `padding-right: 200px` (CSS, for
the slim button row), so a stuck-compact multi-line block on a ~300px-wide
mobile message bubble is squeezed to roughly 100px of usable width, wrapping
words down to a couple of characters per line.

The fix (static/js/chat.js) makes the observer also watch `characterData`
mutations and re-run `_markCompactPre` on the owning `<pre>` when they occur,
so the classification stays live as an open fence grows.

chat.js pulls in browser-only globals transitively and can't be imported
wholesale under plain `node --input-type=module` (documented in
test_chat_video_js.py / test_welcome_actions_js.py); this test extracts the
three relevant (non-exported) functions by name and runs them under node with
a minimal fake-DOM shim — the same technique test_chat_video_js.py uses for
chatRenderer.js.

The MutationObserver *wiring* itself (`obs.observe(..., { characterData:
true })`) is asserted at the source level here and exercised end-to-end via
the live browser smoke test (mirrors the documented approach for
streamingRenderer.js in test_streaming_segmenter_js.py: "The renderer's DOM
behavior is exercised against a running app, not here").
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHAT = _REPO / "static" / "js" / "chat.js"
_HAS_NODE = shutil.which("node") is not None


def _extract_function(source: str, name: str) -> str:
    """Pull one top-level `function <name>(...) { ... }` out of a module by
    brace-counting from its opening `{` (regex alone can't find the matching
    close reliably once a function body has nested blocks). Mirrors
    test_chat_video_js.py's helper, minus the `export` requirement — these
    three functions are module-private."""
    marker = f"function {name}("
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
    return source[start : i + 1]


_FAKE_DOM_PRELUDE = r"""
class FakeText {
  constructor(text) {
    this.nodeType = 3;
    this._text = text;
    this.parentElement = null;
  }
  appendData(more) { this._text += more; }
  get textContent() { return this._text; }
}
class FakeEl {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = String(tag || '').toUpperCase();
    this.className = '';
    this.children = [];
    this._classes = new Set();
    this.classList = {
      toggle: (name, force) => {
        const on = force === undefined ? !this._classes.has(name) : !!force;
        if (on) this._classes.add(name); else this._classes.delete(name);
        this.className = Array.from(this._classes).join(' ');
      },
      contains: (name) => this._classes.has(name),
    };
  }
  appendChild(c) {
    this.children.push(c);
    if (c.nodeType === 1 || c.nodeType === 3) c.parentElement = this;
    return c;
  }
  querySelector(sel) {
    const tag = sel.toUpperCase();
    const walk = (node) => {
      for (const c of node.children || []) {
        if (c.nodeType === 1 && c.tagName === tag) return c;
        const found = walk(c);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
  querySelectorAll(sel) {
    const tag = sel.toUpperCase();
    const out = [];
    const walk = (node) => {
      for (const c of node.children || []) {
        if (c.nodeType === 1 && c.tagName === tag) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  closest(sel) {
    const tag = sel.toUpperCase();
    let el = this;
    while (el) {
      if (el.nodeType === 1 && el.tagName === tag) return el;
      el = el.parentElement;
    }
    return null;
  }
  get textContent() {
    return this.children.map((c) => c.textContent).join('');
  }
}
"""


def _run(call_js: str) -> dict:
    if not _HAS_NODE:
        pytest.skip("node binary not on PATH")
    source = _CHAT.read_text(encoding="utf-8")
    fns = "\n\n".join(
        _extract_function(source, name)
        for name in ("_markCompactPre", "_scanCompactPres", "_handleCompactPreMutations")
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


def _build_streaming_pre():
    """Build a <pre><code>text</code></pre> shaped exactly like
    streamingRenderer.js's appendOpenFence: one stable text node the caller
    grows via appendData."""
    return """
        const pre = new FakeEl('pre');
        const code = new FakeEl('code');
        const textNode = new FakeText('');
        code.appendChild(textNode);
        pre.appendChild(code);
    """


def test_single_line_streamed_pre_is_classified_compact():
    out = _run(
        _build_streaming_pre()
        + """
        textNode.appendData('print(');
        _markCompactPre(pre);
        console.log(JSON.stringify({ compact: pre.classList.contains('pre-compact') }));
        """
    )
    assert out["compact"] is True


def test_characterData_mutation_reclassifies_pre_once_it_grows_multiline():
    # This is the exact #145 scenario: the block starts single-line (compact,
    # correctly), then more lines stream in via appendData — a childList-only
    # observer would never re-run _markCompactPre, so the block would stay
    # incorrectly compact forever. _handleCompactPreMutations must fix that
    # up given the same kind of MutationRecord a characterData-observing
    # MutationObserver would deliver.
    out = _run(
        _build_streaming_pre()
        + """
        textNode.appendData('def f():');
        _markCompactPre(pre);  // initial classification, as the observer would do on insert
        const beforeGrowth = pre.classList.contains('pre-compact');

        textNode.appendData('\\n    return 1\\n    return 2\\n');
        // Simulate the MutationObserver delivering a characterData record for
        // this appendData call — same shape __handleCompactPreMutations reads.
        _handleCompactPreMutations([{ type: 'characterData', target: textNode }]);

        console.log(JSON.stringify({
          beforeGrowth,
          afterGrowth: pre.classList.contains('pre-compact'),
        }));
        """
    )
    assert out["beforeGrowth"] is True
    assert out["afterGrowth"] is False


def test_characterData_mutation_outside_any_pre_is_a_no_op():
    out = _run(
        """
        const span = new FakeEl('span');
        const textNode = new FakeText('hello');
        span.appendChild(textNode);
        // Must not throw when the mutated text isn't inside a <pre>.
        _handleCompactPreMutations([{ type: 'characterData', target: textNode }]);
        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert out["ok"] is True


def test_observer_wiring_watches_characterData():
    chat = _CHAT.read_text(encoding="utf-8")
    assert "obs.observe(document.body, { childList: true, subtree: true, characterData: true });" in chat
    assert "new MutationObserver(_handleCompactPreMutations)" in chat
