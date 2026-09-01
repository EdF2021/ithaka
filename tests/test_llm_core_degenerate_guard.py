"""Unit tests for `_DegenerateStreamGuard` (src/llm_core.py).

Contract under test:
- `check(text)` accumulates lowercased word-tokens (`_DEGENERATE_WORD_RE`,
  tokens of length >= 2) across calls and returns `None` while the stream
  looks like normal, varied text.
- It trips (returns a ready-made `event: error` SSE string) when either:
  (1) the same token repeats for `same_run >= 28` consecutive tokens AND at
      least 100 chars have been seen in total (`total_chars >= 100`), or
  (2) a single token dominates the last <=96-token window: window size
      `>= 72`, that token's count `>= 60`, and its share of the window
      `>= 0.78`, or
  (3) a repeated 4-gram phrase appears `>= 10` times once the window has
      `>= 80` tokens (phrase-loop detection, evaluated only when (1) and (2)
      did not already fire).
- The trip message embeds `self.model` (falling back to the literal
  "model" when the constructor is given a falsy model name) and the
  human-readable `reason` string, as `{"status": 502, "text": ..., "error": ...}`.
- Call sites (`_OpenAIStreamState._emit_delta_events` and the ChatGPT
  subscription/OpenAI-compatible streaming loops) treat a non-None return
  as terminal: they yield the string as-is and abort the stream.
"""
import asyncio
import json

from src import llm_core
from src.llm_core import _DegenerateStreamGuard


def _make_guard(model="test-model"):
    return _DegenerateStreamGuard(model)


def _parse_trip_event(result):
    """Parse a tripped guard's SSE string into (status, text, error)."""
    assert result is not None
    assert result.startswith("event: error\n"), result
    line = [ln for ln in result.splitlines() if ln.startswith("data:")][0]
    payload = json.loads(line[len("data:"):].strip())
    return payload


# --- normal / varied streams never trip ---------------------------------

def test_varied_prose_never_trips():
    guard = _make_guard()
    chunks = [
        "The quick brown fox jumps over the lazy dog. ",
        "It then wanders off toward the river, ",
        "looking for a place to rest in the shade. ",
        "Later, clouds gather and rain begins to fall softly.",
    ]
    for chunk in chunks:
        assert guard.check(chunk) is None


def test_repeated_but_varied_list_formatting_does_not_trip():
    # Numbered/bulleted lists naturally repeat short structural tokens
    # ("item", numbers) but should not look like a degenerate loop as long
    # as no fixed phrase keeps recurring identically.
    templates = [
        "item {i}: covers the setup steps for the {i}th environment\n",
        "item {i}: explains why config {i} needs its own override\n",
        "item {i}: lists the dependencies pulled in by module {i}\n",
        "item {i}: notes a caveat discovered while testing build {i}\n",
    ]
    guard = _make_guard()
    for i in range(1, 15):
        text = templates[i % len(templates)].format(i=i)
        result = guard.check(text)
        assert result is None


def test_empty_text_is_a_noop():
    guard = _make_guard()
    assert guard.check("") is None
    assert guard.check(None) is None
    assert guard.total_chars == 0
    assert guard.recent_tokens == []


def test_tokens_shorter_than_two_chars_are_ignored():
    guard = _make_guard()
    # Single-character "words" (e.g. stray punctuation-adjacent letters)
    # never populate recent_tokens/same_run.
    result = guard.check("a b c d e f g h i j k l m n o p q r s t u v w x y z")
    assert result is None
    assert guard.recent_tokens == []
    assert guard.same_run == 0


# --- condition 1: long same-token run -----------------------------------

def test_same_token_run_just_below_threshold_does_not_trip():
    guard = _make_guard()
    # 27 repeats of "loop" (<100 char threshold would already be satisfied,
    # but same_run stays at 27, one short of the 28 needed).
    text = " ".join(["loop"] * 27)
    assert len(text) >= 100
    result = guard.check(text)
    assert result is None
    assert guard.same_run == 27


def test_same_token_run_trips_at_threshold():
    guard = _make_guard(model="broken-model")
    text = " ".join(["loop"] * 27)
    assert guard.check(text) is None
    # One more occurrence of the same token pushes same_run to 28.
    result = guard.check(" loop")
    assert guard.same_run == 28
    payload = _parse_trip_event(result)
    assert payload["status"] == 502
    assert "broken-model" in payload["text"]
    assert "repeated 'loop' 28 times" in payload["text"]
    assert payload["text"] == payload["error"]


def test_same_token_run_below_char_floor_does_not_trip():
    # same_run reaches 28 but total_chars stays under 100, so condition 1
    # must not fire (guards against tripping on a burst of very short tokens).
    guard = _make_guard()
    result = guard.check(" ".join(["aa"] * 28))
    assert guard.same_run == 28
    assert guard.total_chars < 100
    assert result is None


def test_same_run_resets_on_different_token():
    guard = _make_guard()
    guard.check(" ".join(["loop"] * 20))
    assert guard.same_run == 20
    guard.check(" different")
    assert guard.same_run == 1
    assert guard.last_token == "different"


# --- condition 2: dominant token in the recent window -------------------

def _interleaved(dominant, filler, dominant_per_group, group_count):
    """Build `dominant_per_group` copies of `dominant` then one `filler`,
    repeated `group_count` times -- keeps same_run well under 28 while
    still letting `dominant` dominate the recent-token window."""
    groups = []
    for _ in range(group_count):
        groups.extend([dominant] * dominant_per_group)
        groups.append(filler)
    return " ".join(groups)


def test_dominant_token_window_trips_without_long_same_run():
    guard = _make_guard()
    # 12 groups of (5 "loop" + 1 "other") = 72 tokens: 60 "loop", 12 "other".
    text = _interleaved("loop", "other", 5, 12)
    result = guard.check(text)
    # Condition 1 must not have fired: same_run never exceeds 5.
    assert guard.same_run <= 5
    payload = _parse_trip_event(result)
    assert "repeated 'loop' 60/72 recent tokens" in payload["text"]


def test_dominant_token_just_below_count_does_not_trip():
    guard = _make_guard()
    # 12 groups of (4 "loop" + ~1 "other"x2) shaped to land count just under 60
    # while keeping the window at 72 tokens total.
    groups = []
    for _ in range(12):
        groups.extend(["loop"] * 4)
        groups.extend(["other", "spare"])
    text = " ".join(groups)
    guard2 = _make_guard()
    result = guard2.check(text)
    assert len(guard2.recent_tokens) == 72
    top_count = guard2.recent_tokens.count("loop")
    assert top_count < 60
    assert result is None


def test_recent_tokens_window_capped_at_96():
    guard = _make_guard()
    guard.check(" ".join(f"word{i}" for i in range(200)))
    assert len(guard.recent_tokens) == 96


# --- condition 3: repeated 4-gram phrase loop ----------------------------

def test_phrase_loop_trips_when_window_reaches_80():
    guard = _make_guard()
    phrase = ["the", "quick", "brown", "fox"]
    text = " ".join(phrase * 20)  # 80 tokens, no single token dominates
    result = guard.check(text)
    assert len(guard.recent_tokens) == 80
    # Neither single-token condition should have fired: each word is only
    # 20/80 = 25% of the window, and no run of 28 identical tokens exists.
    payload = _parse_trip_event(result)
    assert "repeated phrase" in payload["text"]


def test_varied_text_at_80_tokens_does_not_trip_phrase_condition():
    guard = _make_guard()
    text = " ".join(f"distinctword{i}" for i in range(80))
    result = guard.check(text)
    assert len(guard.recent_tokens) == 80
    assert result is None


# --- __init__ / model-name handling --------------------------------------

def test_falsy_model_falls_back_to_default_label():
    guard = _DegenerateStreamGuard("")
    assert guard.model == "model"
    guard_none = _DegenerateStreamGuard(None)
    assert guard_none.model == "model"


def test_model_name_preserved_when_given():
    guard = _DegenerateStreamGuard("nvidia/nemotron-3-nano")
    assert guard.model == "nvidia/nemotron-3-nano"


def test_initial_state_is_clean():
    guard = _make_guard("fresh-model")
    assert guard.last_token == ""
    assert guard.same_run == 0
    assert guard.recent_tokens == []
    assert guard.total_chars == 0


# --- call-site integration: guard trip aborts the real streaming path ---

class _FakeResp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeStreamCtx:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _FakeResp(self._lines)

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, *args, **kwargs):
        return _FakeStreamCtx(self._lines)


def test_openai_compatible_stream_aborts_on_degenerate_repetition(monkeypatch):
    # Drive src.llm_core.stream_llm (the real call site around
    # _OpenAIStreamState._emit_delta_events) with an upstream that repeats a
    # single token forever, and confirm the stream is cut short with the
    # guard's error event instead of flooding the caller.
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeClient(
        ['data: {"choices":[{"delta":{"content":" loop"}}]}'] * 40 + ["data: [DONE]"]
    ))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)

    async def _go():
        out = []
        async for chunk in llm_core.stream_llm(
            "http://local-model:8000/v1/chat/completions",
            "broken-local-model",
            [{"role": "user", "content": "hi"}],
        ):
            out.append(chunk)
        return out

    chunks = asyncio.run(_go())
    full = "".join(chunks)
    assert "event: error" in full
    assert "broken-local-model" in full
    assert "repeated 'loop'" in full
    # The 40 identical deltas must not all have streamed through as normal
    # content once the guard tripped.
    normal_deltas = [c for c in chunks if '"delta": " loop"' in c]
    assert len(normal_deltas) < 40
