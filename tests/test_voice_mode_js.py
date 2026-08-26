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


# ── Voice activity detection state machine ─────────────────────────────


def test_vad_stops_after_speech_then_silence():
    values = _node_eval(
        """
        const { createVoiceActivityDetector } = await import('./static/js/voiceRecorder.js');
        const vad = createVoiceActivityDetector({ threshold: 0.01, silenceMs: 500, minSpeechMs: 200, maxMs: 60000, tickMs: 100 });
        let stopAt = null;
        const samples = [];
        // 500ms silence, 400ms speech, then silence until stop
        for (let i = 0; i < 5; i++) samples.push(0.001);
        for (let i = 0; i < 4; i++) samples.push(0.05);
        for (let i = 0; i < 20; i++) samples.push(0.001);
        for (let i = 0; i < samples.length; i++) {
          if (vad.push(samples[i]) === 'stop') { stopAt = i; break; }
        }
        console.log(JSON.stringify({ stopAt, hasSpeech: vad.hasSpeech }));
        """
    )
    # speech ends at index 8; stop should fire once 500ms (5 ticks) of silence follow
    assert values["hasSpeech"] is True
    assert values["stopAt"] == 13


def test_vad_does_not_stop_on_silence_alone_until_max():
    values = _node_eval(
        """
        const { createVoiceActivityDetector } = await import('./static/js/voiceRecorder.js');
        const vad = createVoiceActivityDetector({ threshold: 0.01, silenceMs: 500, minSpeechMs: 200, maxMs: 3000, tickMs: 100 });
        let stopAt = null;
        for (let i = 0; i < 50; i++) {
          if (vad.push(0.001) === 'stop') { stopAt = i; break; }
        }
        console.log(JSON.stringify({ stopAt, hasSpeech: vad.hasSpeech }));
        """
    )
    # never speaks: no end-of-speech stop, only the maxMs cap at 3000ms (tick 29)
    assert values["hasSpeech"] is False
    assert values["stopAt"] == 29


def test_vad_brief_noise_below_min_speech_does_not_latch():
    values = _node_eval(
        """
        const { createVoiceActivityDetector } = await import('./static/js/voiceRecorder.js');
        const vad = createVoiceActivityDetector({ threshold: 0.01, silenceMs: 300, minSpeechMs: 300, maxMs: 60000, tickMs: 100 });
        // 100ms click, then 1s silence — should NOT count as speech + silence stop
        let stopped = false;
        vad.push(0.05);
        for (let i = 0; i < 10; i++) {
          if (vad.push(0.001) === 'stop') stopped = true;
        }
        console.log(JSON.stringify({ stopped, hasSpeech: vad.hasSpeech }));
        """
    )
    assert values == {"stopped": False, "hasSpeech": False}


# ── VoiceMode.activate() STT re-check (restore-on-load race fix) ───────


_VOICE_MODE_DOM_STUB = """
    globalThis.document = {
      getElementById: () => null,
      querySelector: () => null,
    };
    globalThis.window = globalThis;
"""


def test_activate_refreshes_stale_stt_provider_before_guard():
    values = _node_eval(
        _VOICE_MODE_DOM_STUB
        + """
        const voiceMode = (await import('./static/js/voiceMode.js')).default;
        const calls = [];
        const recorder = {
          _sttProvider: 'disabled',
          async refreshSttProvider() { this._sttProvider = 'endpoint:abc'; calls.push('refresh'); },
          startRecording(onFile, onToast, onErr, opts) { calls.push(['start', !!(opts && opts.vad)]); },
          stopRecording() {},
          getIsRecording: () => false,
        };
        voiceMode.init(null, recorder);
        await voiceMode.activate();
        console.log(JSON.stringify({ active: voiceMode.isActive, calls }));
        """
    )
    # the stale 'disabled' provider must be refreshed before the guard,
    # and recording must start with VAD enabled
    assert values["active"] is True
    assert values["calls"] == ["refresh", ["start", True]]


def test_activate_stays_off_when_stt_really_disabled():
    values = _node_eval(
        _VOICE_MODE_DOM_STUB
        + """
        const voiceMode = (await import('./static/js/voiceMode.js')).default;
        let started = false;
        const recorder = {
          _sttProvider: 'disabled',
          async refreshSttProvider() { /* stays disabled */ },
          startRecording() { started = true; },
          stopRecording() {},
          getIsRecording: () => false,
        };
        voiceMode.init(null, recorder);
        await voiceMode.activate();
        console.log(JSON.stringify({ active: voiceMode.isActive, started }));
        """
    )
    assert values == {"active": False, "started": False}


def test_empty_transcription_rearms_mic():
    values = _node_eval(
        _VOICE_MODE_DOM_STUB
        + """
        const voiceMode = (await import('./static/js/voiceMode.js')).default;
        let starts = 0;
        let lastOpts = null;
        const recorder = {
          _sttProvider: 'endpoint:abc',
          async refreshSttProvider() {},
          startRecording(onFile, onToast, onErr, opts) { starts++; lastOpts = opts; },
          stopRecording() {},
          getIsRecording: () => false,
        };
        voiceMode.init(null, recorder);
        await voiceMode.activate();
        const armedBefore = voiceMode.isArmed;
        // Simulate VAD stop → transcription came back empty
        lastOpts.onDone('empty');
        const armedAfterDone = voiceMode.isArmed;
        await new Promise(r => setTimeout(r, 1100));
        console.log(JSON.stringify({ armedBefore, armedAfterDone, starts, armedFinal: voiceMode.isArmed }));
        """
    )
    # after an empty transcription the mic disarms briefly, then re-arms
    assert values["armedBefore"] is True
    assert values["armedAfterDone"] is False
    assert values["starts"] == 2
    assert values["armedFinal"] is True


def test_transcribed_outcome_does_not_double_arm():
    values = _node_eval(
        _VOICE_MODE_DOM_STUB
        + """
        const voiceMode = (await import('./static/js/voiceMode.js')).default;
        let starts = 0;
        let lastOpts = null;
        const recorder = {
          _sttProvider: 'endpoint:abc',
          async refreshSttProvider() {},
          startRecording(onFile, onToast, onErr, opts) { starts++; lastOpts = opts; },
          stopRecording() {},
          getIsRecording: () => false,
        };
        voiceMode.init(null, recorder);
        await voiceMode.activate();
        lastOpts.onDone('transcribed');
        await new Promise(r => setTimeout(r, 1100));
        console.log(JSON.stringify({ starts }));
        """
    )
    # a successful transcription hands off to the input listener; no re-arm
    assert values == {"starts": 1}
