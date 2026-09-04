// static/js/realtimeVoice.js

/**
 * Realtime Voice — OpenAI Realtime API session over WebRTC.
 *
 * Runs alongside the existing hands-free Voice Mode (voiceMode.js), not
 * instead of it — a second, independent toggle. State machine mirrors
 * voiceMode.js's shape (onStateChange callback) so the host (app.js) can
 * reuse the same toggle/indicator UI conventions.
 *
 * Flow: activate() -> POST /api/realtime/session (mint ephemeral key) ->
 * getUserMedia + RTCPeerConnection -> SDP offer to
 * https://api.openai.com/v1/realtime/calls (Bearer: ephemeral client_secret)
 * -> SDP answer applied -> data channel "oai-events" carries turn/transcript
 * events. The full session config (model, voice, VAD, instructions) is
 * already baked into the client_secret at mint time — nothing is sent over
 * the data channel to configure the session.
 */

/**
 * Map one OpenAI Realtime server event to an internal action. Pure — no
 * DOM/network access — so it's unit-testable in Node.
 * @param {object} event
 */
export function classifyRealtimeEvent(event) {
  if (!event || typeof event.type !== 'string') return { type: 'unknown' }
  switch (event.type) {
    case 'input_audio_buffer.speech_started':
      return { type: 'speech_started' }
    case 'input_audio_buffer.speech_stopped':
      return { type: 'speech_stopped' }
    case 'conversation.item.input_audio_transcription.completed':
      return { type: 'user_transcript', text: event.transcript || '' }
    case 'response.output_audio_transcript.delta':
      return { type: 'assistant_delta', delta: event.delta || '', responseId: event.response_id }
    case 'response.output_audio_transcript.done':
      return { type: 'assistant_done', text: event.transcript || '', responseId: event.response_id }
    case 'response.function_call_arguments.done':
      return {
        type: 'function_call',
        name: event.name || '',
        callId: event.call_id || '',
        arguments: typeof event.arguments === 'string' ? event.arguments : JSON.stringify(event.arguments || {}),
      }
    case 'response.created':
      return { type: 'response_created', responseId: event.response && event.response.id }
    case 'response.done':
      return { type: 'response_done' }
    case 'error':
      return { type: 'error', message: (event.error && event.error.message) || 'Onbekende Realtime-fout' }
    default:
      return { type: 'unknown' }
  }
}

/**
 * Client-side barge-in fallback: if the assistant is mid-speech and the
 * user starts talking, cancel the in-flight response. (Server-side
 * interrupt_response should already handle this — see the spec's
 * Foutafhandeling section — this is the documented-as-unverified fallback.)
 * @param {string} state
 * @param {{type: string}} action
 */
export function shouldCancelForBargeIn(state, action) {
  return state === 'speaking' && action.type === 'speech_started'
}

/**
 * Events to send back over the data channel after a tool call finished.
 * `output` must be a string for OpenAI; anything else is JSON-stringified.
 * Pure — unit-tested in Node.
 * @param {string} callId
 * @param {string|object} output
 */
export function buildFunctionCallOutputEvents(callId, output) {
  const text = typeof output === 'string' ? output : JSON.stringify(output)
  return [
    { type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId, output: text } },
    { type: 'response.create' },
  ]
}

const RealtimeVoice = {
  _active: false,
  _state: 'idle', // idle | connecting | listening | speaking | tool | error
  _onStateChange: null,
  _pc: null,
  _dc: null,
  _stream: null,
  _audioEl: null,
  _assistantBuffer: '',
  _sessionTimer: null,
  _toolChain: Promise.resolve(),
  _responseActive: false,
  _pendingResponseCreate: false,

  /**
   * @param {(state: {active: boolean, state: string}) => void} onStateChange
   */
  init(onStateChange) {
    this._onStateChange = onStateChange || null
  },

  async activate() {
    if (this._active) return
    this._active = true
    this._state = 'connecting'
    this._notify()

    try {
      const sessRes = await fetch('/api/realtime/session', { method: 'POST', credentials: 'same-origin' })
      if (!sessRes.ok) {
        const err = await sessRes.json().catch(() => ({}))
        const detail = typeof err.detail === 'string' ? err.detail : (err.detail && err.detail.message)
        throw new Error(detail || 'Kon geen Realtime-sessie starten')
      }
      const { client_secret, max_minutes, calls_url } = await sessRes.json()

      // Re-check after every await below: a concurrent deactivate() (the
      // user re-toggling, or toggling while a mic-permission prompt is
      // open) may have won while this call was suspended. Without this, a
      // deactivate() that ran mid-await would find nothing to tear down
      // yet, report idle, and then this call would resume regardless and
      // silently build a live session (mic + peer connection + audio)
      // behind a UI that shows inactive. Mirrors the same guard in
      // voiceMode.js's activate().
      if (!this._active) return

      this._stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (!this._active) {
        this._stream.getTracks().forEach((t) => t.stop())
        this._stream = null
        return
      }

      const pc = new RTCPeerConnection()
      this._pc = pc
      this._stream.getTracks().forEach((track) => pc.addTrack(track, this._stream))

      this._audioEl = document.createElement('audio')
      this._audioEl.autoplay = true
      pc.ontrack = (e) => { this._audioEl.srcObject = e.streams[0] }

      const dc = pc.createDataChannel('oai-events')
      this._dc = dc
      dc.onmessage = (e) => this._onDataChannelMessage(e.data)

      // Ruling (plan Task 4): detect a mid-session drop and fall back to a
      // visible error instead of silently looking "connected" while dead.
      // A single automatic reconnect attempt (spec's Foutafhandeling
      // section) is deferred to a follow-up — this only ensures the drop
      // is *noticed*, matching the spec's fallback half ("val terug naar
      // een zichtbare melding"); the existing voice mode stays available
      // regardless since this toggle never touches it.
      pc.onconnectionstatechange = () => {
        if (!this._active) return
        if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') {
          console.error('RealtimeVoice: connection dropped:', pc.connectionState)
          if (window.uiModule?.showError) window.uiModule.showError('Realtime-verbinding verbroken')
          this.deactivate()
        }
      }

      const offer = await pc.createOffer()
      if (!this._active) { this.deactivate(); return }

      await pc.setLocalDescription(offer)
      if (!this._active) { this.deactivate(); return }

      const callRes = await fetch(calls_url, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${client_secret}`,
          'Content-Type': 'application/sdp',
        },
        body: offer.sdp,
      })
      if (!callRes.ok) throw new Error(`OpenAI Realtime-verbinding mislukt (HTTP ${callRes.status})`)
      const answerSdp = await callRes.text()
      await pc.setRemoteDescription({ type: 'answer', sdp: answerSdp })
      if (!this._active) { this.deactivate(); return }

      this._state = 'listening'
      this._notify()

      if (max_minutes) {
        this._sessionTimer = setTimeout(() => this._onSessionTimeout(), max_minutes * 60 * 1000)
      }
    } catch (e) {
      console.error('RealtimeVoice: activation failed:', e)
      this._state = 'error'
      this._notify()
      if (window.uiModule?.showError) window.uiModule.showError(e.message || 'Realtime-gesprek kon niet starten')
      this.deactivate()
    }
  },

  deactivate() {
    if (this._sessionTimer) { clearTimeout(this._sessionTimer); this._sessionTimer = null }
    if (this._dc) { try { this._dc.close() } catch (e) { /* ignore */ }; this._dc = null }
    if (this._pc) { try { this._pc.close() } catch (e) { /* ignore */ }; this._pc = null }
    if (this._stream) { this._stream.getTracks().forEach((t) => t.stop()); this._stream = null }
    this._active = false
    this._state = 'idle'
    this._assistantBuffer = ''
    this._responseActive = false
    this._pendingResponseCreate = false
    this._toolChain = Promise.resolve()
    this._notify()
  },

  toggle() {
    if (this._active) this.deactivate()
    else this.activate()
  },

  /** @private */
  _onSessionTimeout() {
    if (window.uiModule?.showToast) {
      window.uiModule.showToast('Realtime-sessie gestopt na de tijdslimiet — heractiveer om door te gaan')
    }
    this.deactivate()
  },

  /** @private */
  _onDataChannelMessage(raw) {
    let event
    try { event = JSON.parse(raw) } catch (e) { return }
    const action = classifyRealtimeEvent(event)

    if (shouldCancelForBargeIn(this._state, action) && this._dc && this._dc.readyState === 'open') {
      this._dc.send(JSON.stringify({ type: 'response.cancel' }))
    }

    switch (action.type) {
      case 'speech_started':
        if (this._state !== 'tool') this._state = 'listening'
        this._notify()
        break
      case 'speech_stopped':
        this._notify()
        break
      case 'response_created':
        this._responseActive = true
        break
      case 'user_transcript':
        if (action.text.trim() && window.chatRenderer?.addMessage) window.chatRenderer.addMessage('user', action.text, null, null)
        break
      case 'assistant_delta':
        this._state = 'speaking'
        this._assistantBuffer += action.delta
        this._notify()
        break
      case 'assistant_done': {
        const text = action.text || this._assistantBuffer
        this._assistantBuffer = ''
        if (text.trim() && window.chatRenderer?.addMessage) window.chatRenderer.addMessage('assistant', text, null, null)
        break
      }
      case 'response_done':
        // The function-call turn ends with its own response.done while the
        // /api/realtime/ask fetch is still in flight — keep the 'tool' state
        // until _handleFunctionCall finishes.
        this._responseActive = false
        if (this._pendingResponseCreate && this._dc && this._dc.readyState === 'open') {
          this._dc.send(JSON.stringify({ type: 'response.create' }))
          this._pendingResponseCreate = false
        }
        if (this._state !== 'tool') this._state = 'listening'
        this._notify()
        break
      case 'function_call':
        this._toolChain = this._toolChain.then(() => this._handleFunctionCall(action)).catch((e) => { console.error('RealtimeVoice: tool call failed:', e) })
        break
      case 'error':
        console.error('RealtimeVoice: server error:', action.message)
        if (window.uiModule?.showError) window.uiModule.showError(action.message)
        break
    }
  },

  /** @private — one call at a time (chained via _toolChain). */
  async _handleFunctionCall(action) {
    if (!this._active) return
    const name = action.name || 'ask_ithaka' // exactly one tool is declared server-side (I3)
    let output
    if (name !== 'ask_ithaka') {
      output = { error: 'Onbekende tool' }
    } else {
      let question = ''
      try { question = String(JSON.parse(action.arguments || '{}').question || '') } catch (e) { question = '' }
      if (!question.trim()) {
        output = { error: 'Ongeldige argumenten' }
      } else {
        this._state = 'tool'
        this._notify()
        if (window.chatRenderer?.addMessage) window.chatRenderer.addMessage('assistant', 'Opgezocht via Ithaka: ' + question, null, null)
        try {
          const res = await fetch('/api/realtime/ask', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question, call_id: action.callId }),
          })
          if (!this._active) return
          if (res.ok) {
            const data = await res.json()
            if (!this._active) return
            output = { answer: data.answer || '' }
          } else {
            const err = await res.json().catch(() => ({}))
            if (!this._active) return
            const detail = err.detail && (typeof err.detail === 'string' ? err.detail : err.detail.message)
            output = { error: detail || 'Het opzoeken is mislukt' }
          }
        } catch (e) {
          output = { error: 'Het opzoeken is mislukt' }
        }
        if (!this._active) return
        this._state = 'listening'
        this._notify()
      }
    }
    if (!this._dc || this._dc.readyState !== 'open') return
    // Never fire response.create into a response the server already has
    // active — OpenAI rejects that with conversation_already_has_active_response
    // (I1). Defer it until the pending response.done clears _responseActive.
    const events = buildFunctionCallOutputEvents(action.callId, output)
    this._dc.send(JSON.stringify(events[0]))
    if (this._responseActive) {
      this._pendingResponseCreate = true
    } else {
      this._dc.send(JSON.stringify(events[1]))
    }
  },

  /** @private */
  _notify() {
    if (this._onStateChange) this._onStateChange({ active: this._active, state: this._state })
  },

  get isActive() { return this._active },
  get state() { return this._state },
}

export default RealtimeVoice
