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


def test_classify_function_call_arguments_done():
    values = _node_eval(
        """
        const { classifyRealtimeEvent } = await import('./static/js/realtimeVoice.js');
        const action = classifyRealtimeEvent({
          type: 'response.function_call_arguments.done',
          call_id: 'call_1', name: 'ask_ithaka', arguments: '{"question":"weer?"}',
        });
        console.log(JSON.stringify(action));
        """
    )
    assert values == {"type": "function_call", "name": "ask_ithaka", "callId": "call_1", "arguments": '{"question":"weer?"}'}


def test_build_function_call_output_events_shape():
    values = _node_eval(
        """
        const { buildFunctionCallOutputEvents } = await import('./static/js/realtimeVoice.js');
        console.log(JSON.stringify(buildFunctionCallOutputEvents('call_1', '{"answer":"18 graden"}')));
        """
    )
    assert values == [
        {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "call_1", "output": '{"answer":"18 graden"}'}},
        {"type": "response.create"},
    ]


def test_build_function_call_output_events_stringifies_non_string():
    values = _node_eval(
        """
        const { buildFunctionCallOutputEvents } = await import('./static/js/realtimeVoice.js');
        console.log(JSON.stringify(buildFunctionCallOutputEvents('c', { error: 'x' })[0].item.output));
        """
    )
    assert values == '{"error":"x"}'


def test_classify_response_created():
    values = _node_eval(
        """
        const { classifyRealtimeEvent } = await import('./static/js/realtimeVoice.js');
        const action = classifyRealtimeEvent({ type: 'response.created', response: { id: 'resp_1' } });
        console.log(JSON.stringify(action));
        """
    )
    assert values == {"type": "response_created", "responseId": "resp_1"}


def test_speech_started_keeps_tool_state_while_fetch_in_flight():
    # Same guard shape as response_done (I1): the user speaking mid-lookup
    # must not clobber the 'tool' indicator while _handleFunctionCall is
    # still awaiting the /api/realtime/ask fetch.
    values = _node_eval(
        """
        const mod = await import('./static/js/realtimeVoice.js');
        const rt = mod.default;
        rt._active = true; rt._state = 'tool';
        rt._dc = { readyState: 'open', send() {} };
        rt._onDataChannelMessage(JSON.stringify({ type: 'input_audio_buffer.speech_started' }));
        const kept = rt._state;
        rt._state = 'speaking';
        rt._onDataChannelMessage(JSON.stringify({ type: 'input_audio_buffer.speech_started' }));
        console.log(JSON.stringify([kept, rt._state]));
        """
    )
    assert values == ["tool", "listening"]


def test_handle_function_call_ok_sends_answer_and_response_create():
    values = _node_eval(
        """
        const mod = await import('./static/js/realtimeVoice.js');
        const rt = mod.default;
        globalThis.window = { chatRenderer: { addMessage(){} } };
        let fetchCall = null;
        globalThis.fetch = async (url, opts) => {
          fetchCall = { url, method: opts.method, body: opts.body };
          return { ok: true, json: async () => ({ answer: '18 graden' }) };
        };
        rt._active = true;
        rt._dc = { readyState: 'open', sent: [], send(s) { this.sent.push(JSON.parse(s)) } };
        await rt._handleFunctionCall({ name: 'ask_ithaka', callId: 'c1', arguments: '{"question":"weer?"}' });
        console.log(JSON.stringify({ sent: rt._dc.sent, state: rt._state, fetchCall }));
        """
    )
    assert values["sent"] == [
        {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "c1", "output": '{"answer":"18 graden"}'}},
        {"type": "response.create"},
    ]
    assert values["state"] == "listening"
    assert values["fetchCall"]["url"] == "/api/realtime/ask"
    assert values["fetchCall"]["method"] == "POST"
    assert json.loads(values["fetchCall"]["body"]) == {"question": "weer?", "call_id": "c1"}


def test_handle_function_call_backend_error():
    values = _node_eval(
        """
        const mod = await import('./static/js/realtimeVoice.js');
        const rt = mod.default;
        globalThis.window = { chatRenderer: { addMessage(){} } };
        globalThis.fetch = async () => ({ ok: false, json: async () => ({ detail: { message: 'Het opzoeken duurde te lang' } }) });
        rt._active = true;
        rt._dc = { readyState: 'open', sent: [], send(s) { this.sent.push(JSON.parse(s)) } };
        await rt._handleFunctionCall({ name: 'ask_ithaka', callId: 'c1', arguments: '{"question":"weer?"}' });
        console.log(JSON.stringify({ sent: rt._dc.sent, state: rt._state }));
        """
    )
    assert values["sent"] == [
        {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "c1", "output": '{"error":"Het opzoeken duurde te lang"}'}},
        {"type": "response.create"},
    ]
    assert values["state"] == "listening"


def test_handle_function_call_network_failure():
    values = _node_eval(
        """
        const mod = await import('./static/js/realtimeVoice.js');
        const rt = mod.default;
        globalThis.window = { chatRenderer: { addMessage(){} } };
        globalThis.fetch = async () => { throw new Error('network down') };
        rt._active = true;
        rt._dc = { readyState: 'open', sent: [], send(s) { this.sent.push(JSON.parse(s)) } };
        await rt._handleFunctionCall({ name: 'ask_ithaka', callId: 'c1', arguments: '{"question":"weer?"}' });
        console.log(JSON.stringify({ sent: rt._dc.sent }));
        """
    )
    assert values["sent"][0]["item"]["output"] == '{"error":"Het opzoeken is mislukt"}'
    assert values["sent"][1] == {"type": "response.create"}


def test_handle_function_call_unknown_tool_does_not_fetch():
    values = _node_eval(
        """
        const mod = await import('./static/js/realtimeVoice.js');
        const rt = mod.default;
        globalThis.window = { chatRenderer: { addMessage(){} } };
        let fetchCalled = false;
        globalThis.fetch = async () => { fetchCalled = true; return { ok: true, json: async () => ({}) } };
        rt._active = true;
        rt._dc = { readyState: 'open', sent: [], send(s) { this.sent.push(JSON.parse(s)) } };
        await rt._handleFunctionCall({ name: 'foo', callId: 'c1', arguments: '{}' });
        console.log(JSON.stringify({ sent: rt._dc.sent, fetchCalled }));
        """
    )
    assert values["sent"][0]["item"]["output"] == '{"error":"Onbekende tool"}'
    assert values["fetchCalled"] is False


def test_handle_function_call_missing_name_defaults_to_ask_ithaka():
    # I3: response.function_call_arguments.done without a top-level name
    # must not silently kill the feature — only one tool is declared.
    values = _node_eval(
        """
        const mod = await import('./static/js/realtimeVoice.js');
        const rt = mod.default;
        globalThis.window = { chatRenderer: { addMessage(){} } };
        globalThis.fetch = async () => ({ ok: true, json: async () => ({ answer: '18 graden' }) });
        rt._active = true;
        rt._dc = { readyState: 'open', sent: [], send(s) { this.sent.push(JSON.parse(s)) } };
        await rt._handleFunctionCall({ name: undefined, callId: 'c1', arguments: '{"question":"weer?"}' });
        console.log(JSON.stringify({ sent: rt._dc.sent, state: rt._state }));
        """
    )
    assert values["sent"] == [
        {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "c1", "output": '{"answer":"18 graden"}'}},
        {"type": "response.create"},
    ]
    assert values["state"] == "listening"


def test_handle_function_call_defers_response_create_when_active():
    # I1: never fire response.create into a response the server already has
    # active — defer until the pending response.done clears _responseActive.
    values = _node_eval(
        """
        const mod = await import('./static/js/realtimeVoice.js');
        const rt = mod.default;
        globalThis.window = { chatRenderer: { addMessage(){} } };
        globalThis.fetch = async () => ({ ok: true, json: async () => ({ answer: '18 graden' }) });
        rt._active = true;
        rt._responseActive = true;
        rt._dc = { readyState: 'open', sent: [], send(s) { this.sent.push(JSON.parse(s)) } };
        await rt._handleFunctionCall({ name: 'ask_ithaka', callId: 'c1', arguments: '{"question":"weer?"}' });
        const afterCall = { sent: rt._dc.sent.slice(), pending: rt._pendingResponseCreate };
        rt._onDataChannelMessage(JSON.stringify({ type: 'response.done' }));
        console.log(JSON.stringify({ afterCall, sentAfterDone: rt._dc.sent, pendingAfterDone: rt._pendingResponseCreate }));
        """
    )
    assert values["afterCall"]["sent"] == [
        {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "c1", "output": '{"answer":"18 graden"}'}},
    ]
    assert values["afterCall"]["pending"] is True
    assert values["sentAfterDone"][-1] == {"type": "response.create"}
    assert values["pendingAfterDone"] is False


def test_handle_function_call_deactivate_mid_fetch_sends_nothing():
    values = _node_eval(
        """
        const mod = await import('./static/js/realtimeVoice.js');
        const rt = mod.default;
        globalThis.window = { chatRenderer: { addMessage(){} } };
        const dcMock = { readyState: 'open', sent: [], send(s) { this.sent.push(JSON.parse(s)) } };
        rt._dc = dcMock;
        rt._active = true;
        globalThis.fetch = () => new Promise((resolve) => {
          rt.deactivate();
          resolve({ ok: true, json: async () => ({ answer: 'x' }) });
        });
        await rt._handleFunctionCall({ name: 'ask_ithaka', callId: 'c1', arguments: '{"question":"weer?"}' });
        console.log(JSON.stringify({ sent: dcMock.sent }));
        """
    )
    assert values["sent"] == []


def test_response_done_keeps_tool_state_while_fetch_in_flight():
    # OpenAI ends the function-call turn with response.done while the
    # /api/realtime/ask fetch is still running — the indicator must stay on
    # 'tool' until _handleFunctionCall finishes (task-4 review finding).
    values = _node_eval(
        """
        const mod = await import('./static/js/realtimeVoice.js');
        const rt = mod.default;
        rt._active = true; rt._state = 'tool';
        rt._dc = { readyState: 'open', send() {} };
        rt._onDataChannelMessage(JSON.stringify({ type: 'response.done' }));
        const kept = rt._state;
        rt._state = 'speaking';
        rt._onDataChannelMessage(JSON.stringify({ type: 'response.done' }));
        console.log(JSON.stringify([kept, rt._state]));
        """
    )
    assert values == ["tool", "listening"]



def test_build_transcription_update_event():
    values = _node_eval(
        """
        const { buildTranscriptionUpdateEvent } = await import('./static/js/realtimeVoice.js');
        console.log(JSON.stringify({
          on: buildTranscriptionUpdateEvent({ model: 'gpt-realtime-whisper', language: 'nl' }),
          off: buildTranscriptionUpdateEvent(null),
          empty: buildTranscriptionUpdateEvent({}),
        }));
        """
    )
    assert values["on"] == {
        "type": "session.update",
        "session": {"type": "realtime", "audio": {"input": {"transcription": {"model": "gpt-realtime-whisper", "language": "nl"}}}},
    }
    assert values["off"] is None
    assert values["empty"] is None
