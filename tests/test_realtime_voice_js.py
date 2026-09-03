"""Pure logic in static/js/realtimeVoice.js — event classification and the
barge-in decision. Node-based, mirrors tests/test_voice_mode_js.py. See
docs/superpowers/plans/2026-09-03-realtime-voice-mode.md, Task 4."""

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


def test_classify_speech_started():
    values = _node_eval(
        """
        const { classifyRealtimeEvent } = await import('./static/js/realtimeVoice.js');
        const action = classifyRealtimeEvent({ type: 'input_audio_buffer.speech_started' });
        console.log(JSON.stringify(action));
        """
    )
    assert values == {"type": "speech_started"}


def test_classify_user_transcript():
    values = _node_eval(
        """
        const { classifyRealtimeEvent } = await import('./static/js/realtimeVoice.js');
        const action = classifyRealtimeEvent({
          type: 'conversation.item.input_audio_transcription.completed',
          transcript: 'hallo daar',
        });
        console.log(JSON.stringify(action));
        """
    )
    assert values == {"type": "user_transcript", "text": "hallo daar"}


def test_classify_assistant_delta_and_done():
    values = _node_eval(
        """
        const { classifyRealtimeEvent } = await import('./static/js/realtimeVoice.js');
        const delta = classifyRealtimeEvent({
          type: 'response.output_audio_transcript.delta', delta: 'Hoi', response_id: 'r1',
        });
        const done = classifyRealtimeEvent({
          type: 'response.output_audio_transcript.done', transcript: 'Hoi daar', response_id: 'r1',
        });
        console.log(JSON.stringify({ delta, done }));
        """
    )
    assert values == {
        "delta": {"type": "assistant_delta", "delta": "Hoi", "responseId": "r1"},
        "done": {"type": "assistant_done", "text": "Hoi daar", "responseId": "r1"},
    }


def test_classify_error_event():
    values = _node_eval(
        """
        const { classifyRealtimeEvent } = await import('./static/js/realtimeVoice.js');
        const action = classifyRealtimeEvent({ type: 'error', error: { message: 'boom' } });
        console.log(JSON.stringify(action));
        """
    )
    assert values == {"type": "error", "message": "boom"}


def test_classify_unknown_event_shape():
    values = _node_eval(
        """
        const { classifyRealtimeEvent } = await import('./static/js/realtimeVoice.js');
        console.log(JSON.stringify([
          classifyRealtimeEvent({ type: 'rate_limits.updated' }),
          classifyRealtimeEvent(null),
          classifyRealtimeEvent({}),
        ]));
        """
    )
    assert values == [{"type": "unknown"}, {"type": "unknown"}, {"type": "unknown"}]


def test_barge_in_cancels_only_while_speaking_on_speech_started():
    values = _node_eval(
        """
        const { shouldCancelForBargeIn } = await import('./static/js/realtimeVoice.js');
        console.log(JSON.stringify({
          whileSpeaking: shouldCancelForBargeIn('speaking', { type: 'speech_started' }),
          whileListening: shouldCancelForBargeIn('listening', { type: 'speech_started' }),
          otherAction: shouldCancelForBargeIn('speaking', { type: 'speech_stopped' }),
        }));
        """
    )
    assert values == {"whileSpeaking": True, "whileListening": False, "otherAction": False}
