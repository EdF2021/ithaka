"""Pure logic in static/js/meetings.js — elapsed-time formatting, meeting
status labels, and the sequential chunk uploader. Node-based, mirrors
tests/test_realtime_voice_js.py. See
.superpowers/sdd/2026-09-04-meeting-recorder/task-5-brief.md, Task 5."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _node_eval(source: str):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


# ── formatElapsed ──────────────────────────────────────────────────────────

def test_format_elapsed_zero():
    values = _node_eval(
        """
        const { formatElapsed } = await import('./static/js/meetings.js');
        console.log(JSON.stringify(formatElapsed(0)));
        """
    )
    assert values == "00:00"


def test_format_elapsed_under_a_minute():
    values = _node_eval(
        """
        const { formatElapsed } = await import('./static/js/meetings.js');
        console.log(JSON.stringify(formatElapsed(5000)));
        """
    )
    assert values == "00:05"


def test_format_elapsed_minutes_seconds():
    values = _node_eval(
        """
        const { formatElapsed } = await import('./static/js/meetings.js');
        console.log(JSON.stringify(formatElapsed(65000)));
        """
    )
    assert values == "01:05"


def test_format_elapsed_hours_minutes_seconds():
    values = _node_eval(
        """
        const { formatElapsed } = await import('./static/js/meetings.js');
        console.log(JSON.stringify(formatElapsed(3723000)));
        """
    )
    assert values == "1:02:03"


def test_format_elapsed_exactly_one_hour():
    values = _node_eval(
        """
        const { formatElapsed } = await import('./static/js/meetings.js');
        console.log(JSON.stringify(formatElapsed(3600000)));
        """
    )
    assert values == "1:00:00"


# ── meetingStatusLabel ─────────────────────────────────────────────────────

def test_status_label_recording():
    values = _node_eval(
        """
        const { meetingStatusLabel } = await import('./static/js/meetings.js');
        console.log(JSON.stringify(meetingStatusLabel({ status: 'recording' })));
        """
    )
    assert values == "Recording"


def test_status_label_phases():
    values = _node_eval(
        """
        const { meetingStatusLabel } = await import('./static/js/meetings.js');
        console.log(JSON.stringify({
          splitting: meetingStatusLabel({ status: 'processing', phase: 'splitting' }),
          transcribing: meetingStatusLabel({ status: 'processing', phase: 'transcribing', segment: 2, total: 5 }),
          correcting: meetingStatusLabel({ status: 'processing', phase: 'correcting', segment: 3, total: 5 }),
          condensing: meetingStatusLabel({ status: 'processing', phase: 'condensing', depth: 2 }),
          writing: meetingStatusLabel({ status: 'processing', phase: 'writing' }),
          saving: meetingStatusLabel({ status: 'processing', phase: 'saving' }),
          unknownPhase: meetingStatusLabel({ status: 'processing', phase: null }),
        }));
        """
    )
    assert values == {
        "splitting": "Splitting audio",
        "transcribing": "Transcribing 2/5",
        "correcting": "Correcting 3/5",
        "condensing": "Condensing (depth 2)",
        "writing": "Writing minutes",
        "saving": "Saving",
        "unknownPhase": "Processing",
    }


def test_status_label_done():
    values = _node_eval(
        """
        const { meetingStatusLabel } = await import('./static/js/meetings.js');
        console.log(JSON.stringify(meetingStatusLabel({ status: 'done' })));
        """
    )
    assert values == "Done"


def test_status_label_error():
    values = _node_eval(
        """
        const { meetingStatusLabel } = await import('./static/js/meetings.js');
        console.log(JSON.stringify(meetingStatusLabel({ status: 'error', error: 'STT niet geconfigureerd' })));
        """
    )
    assert values == "Error: STT niet geconfigureerd"


# ── createChunkUploader ─────────────────────────────────────────────────────

def test_uploader_sends_chunks_sequentially_in_order():
    values = _node_eval(
        """
        const { createChunkUploader } = await import('./static/js/meetings.js');
        const calls = [];
        let inFlight = 0, maxInFlight = 0;
        const post = async (seq, blob) => {
          inFlight++; maxInFlight = Math.max(maxInFlight, inFlight);
          calls.push(seq);
          await new Promise((r) => setTimeout(r, 5));
          inFlight--;
        };
        const uploader = createChunkUploader({ post, sleep: async () => {} });
        uploader.enqueue({ size: 10 });
        uploader.enqueue({ size: 10 });
        uploader.enqueue({ size: 10 });
        const result = await uploader.drain();
        console.log(JSON.stringify({ calls, maxInFlight, result }));
        """
    )
    assert values["calls"] == [0, 1, 2]
    assert values["maxInFlight"] == 1
    assert values["result"] == {"uploaded": 3, "failed": 0}


def test_uploader_retries_a_failing_attempt_then_succeeds():
    values = _node_eval(
        """
        const { createChunkUploader } = await import('./static/js/meetings.js');
        const calls = [];
        const sleeps = [];
        let seq1Attempts = 0;
        const post = async (seq, blob) => {
          calls.push(seq);
          if (seq === 1) {
            seq1Attempts++;
            if (seq1Attempts === 1) throw new Error('network blip');
          }
        };
        const uploader = createChunkUploader({ post, sleep: async (ms) => { sleeps.push(ms); } });
        uploader.enqueue({ size: 1 });
        uploader.enqueue({ size: 1 });
        const result = await uploader.drain();
        console.log(JSON.stringify({ calls, sleeps, result }));
        """
    )
    assert values["calls"] == [0, 1, 1]
    assert values["sleeps"] == [1000]
    assert values["result"] == {"uploaded": 2, "failed": 0}


def test_uploader_permanent_failure_stops_after_max_attempts():
    # Brief Step 1(c), verbatim: seq 0 succeeds, seq 1 always rejects. After
    # exhausting maxAttempts (3), failed === 1 and no call ever carries a seq
    # >= 2 — asserted here as "exactly 4 post calls total, none for seq 2".
    values = _node_eval(
        """
        const { createChunkUploader } = await import('./static/js/meetings.js');
        const calls = [];
        const sleeps = [];
        const post = async (seq, blob) => {
          calls.push(seq);
          if (seq === 1) throw new Error('boom');
        };
        const uploader = createChunkUploader({ post, sleep: async (ms) => { sleeps.push(ms); } });
        uploader.enqueue({ size: 1 }); // seq 0 — succeeds
        uploader.enqueue({ size: 1 }); // seq 1 — always rejects
        const result = await uploader.drain();
        console.log(JSON.stringify({ calls, sleeps, result, stats: uploader.stats() }));
        """
    )
    assert values["calls"] == [0, 1, 1, 1]
    assert values["calls"].count(2) == 0
    assert values["sleeps"] == [1000, 2000]
    assert values["result"] == {"uploaded": 1, "failed": 1}
    assert values["stats"]["failedChunks"] == 1


def test_uploader_sweeps_further_chunks_as_failed_without_posting():
    # Extension beyond the brief's literal example: once a chunk permanently
    # fails, a gap is fatal — further already-queued chunks must never be
    # posted (the server would 409 on the seq gap) but still count toward
    # the "N chunks not saved" total the status line shows.
    values = _node_eval(
        """
        const { createChunkUploader } = await import('./static/js/meetings.js');
        const calls = [];
        const post = async (seq, blob) => {
          calls.push(seq);
          if (seq === 1) throw new Error('boom');
        };
        const uploader = createChunkUploader({ post, sleep: async () => {} });
        uploader.enqueue({ size: 1 }); // seq 0 — succeeds
        uploader.enqueue({ size: 1 }); // seq 1 — always rejects
        uploader.enqueue({ size: 1 }); // seq 2 — must never be posted
        const result = await uploader.drain();
        console.log(JSON.stringify({ calls, result }));
        """
    )
    assert 2 not in values["calls"]
    assert values["result"] == {"uploaded": 1, "failed": 2}


def test_uploader_late_enqueue_after_halt_is_counted_failed_without_posting():
    values = _node_eval(
        """
        const { createChunkUploader } = await import('./static/js/meetings.js');
        const calls = [];
        const post = async (seq, blob) => {
          calls.push(seq);
          if (seq === 1) throw new Error('boom');
        };
        const uploader = createChunkUploader({ post, sleep: async () => {} });
        uploader.enqueue({ size: 1 }); // seq 0
        uploader.enqueue({ size: 1 }); // seq 1 — fails permanently
        const firstResult = await uploader.drain();
        uploader.enqueue({ size: 1 }); // seq 2 — enqueued after halt
        console.log(JSON.stringify({ calls, firstResult, statsAfterLateEnqueue: uploader.stats() }));
        """
    )
    assert 2 not in values["calls"]
    assert values["firstResult"] == {"uploaded": 1, "failed": 1}
    assert values["statsAfterLateEnqueue"]["failedChunks"] == 2


def test_uploader_late_enqueue_after_drain_already_parked_settles_it():
    # Regression: a trailing MediaRecorder dataavailable chunk can arrive
    # (and get enqueue()'d) after stop() already called drain() and is
    # awaiting it. If the halted-enqueue path doesn't settle a parked
    # resolver, drain() hangs forever and /finish is never called.
    values = _node_eval(
        """
        const { createChunkUploader } = await import('./static/js/meetings.js');
        const calls = [];
        let resolveSeq1 = null;
        const post = async (seq, blob) => {
          calls.push(seq);
          if (seq === 1) {
            await new Promise((_, reject) => { resolveSeq1 = reject; });
          }
        };
        const uploader = createChunkUploader({ post, maxAttempts: 1, sleep: async () => {} });
        uploader.enqueue({ size: 1 }); // seq 0 — succeeds
        uploader.enqueue({ size: 1 }); // seq 1 — will reject once maxAttempts=1 exhausts it
        // Let seq 0 finish and seq 1's post() start.
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));
        const drainPromise = uploader.drain(); // parks a resolver — nothing pending resolved yet
        resolveSeq1(); // reject seq 1's in-flight post -> permanent failure -> halted
        const result = await Promise.race([
          drainPromise,
          new Promise((resolve) => setTimeout(() => resolve('TIMEOUT'), 500)),
        ]);
        // Late enqueue after the halt — must not hang a *second* drain either.
        uploader.enqueue({ size: 1 }); // seq 2
        console.log(JSON.stringify({ calls, result }));
        """
    )
    assert values["result"] != "TIMEOUT"
    assert values["result"] == {"uploaded": 1, "failed": 1}
    assert 2 not in values["calls"]


def test_uploader_on_status_reports_uploaded_bytes_sum():
    values = _node_eval(
        """
        const { createChunkUploader } = await import('./static/js/meetings.js');
        const statuses = [];
        const post = async () => {};
        const uploader = createChunkUploader({ post, sleep: async () => {}, onStatus: (s) => statuses.push(s) });
        uploader.enqueue({ size: 100 });
        uploader.enqueue({ size: 250 });
        await uploader.drain();
        const last = statuses[statuses.length - 1];
        console.log(JSON.stringify({ uploadedBytes: last.uploadedBytes, uploadedChunks: last.uploadedChunks }));
        """
    )
    assert values == {"uploadedBytes": 350, "uploadedChunks": 2}


def test_uploader_drain_with_nothing_enqueued_resolves_immediately():
    values = _node_eval(
        """
        const { createChunkUploader } = await import('./static/js/meetings.js');
        const uploader = createChunkUploader({ post: async () => {}, sleep: async () => {} });
        const result = await uploader.drain();
        console.log(JSON.stringify(result));
        """
    )
    assert values == {"uploaded": 0, "failed": 0}


# ── MEETING_MAX_MS export ───────────────────────────────────────────────────

def test_meeting_max_ms_is_three_hours():
    values = _node_eval(
        """
        const { MEETING_MAX_MS } = await import('./static/js/meetings.js');
        console.log(JSON.stringify(MEETING_MAX_MS));
        """
    )
    assert values == 3 * 60 * 60 * 1000


# ── module import must not touch document/window at top level ─────────────

def test_module_imports_cleanly_in_node():
    # If this doesn't raise, the module didn't touch `document`/`window` at
    # import time — verifies the guard the brief requires.
    result = subprocess.run(
        ["node", "--input-type=module", "-e", "await import('./static/js/meetings.js'); console.log('ok')"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
