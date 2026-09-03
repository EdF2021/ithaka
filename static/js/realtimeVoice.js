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

const RealtimeVoice = {
  _active: false,
  _state: 'idle', // idle | connecting | listening | speaking | error
  _onStateChange: null,
  _pc: null,
  _dc: null,
  _stream: null,
  _audioEl: null,
  _assistantBuffer: '',
  _sessionTimer: null,

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
      const { client_secret, max_minutes } = await sessRes.json()

      this._stream = await navigator.mediaDevices.getUserMedia({ audio: true })
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
      await pc.setLocalDescription(offer)

      const callRes = await fetch('https://api.openai.com/v1/realtime/calls', {
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
        this._state = 'listening'
        this._notify()
        break
      case 'speech_stopped':
        this._notify()
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
        this._state = 'listening'
        this._notify()
        break
      case 'error':
        console.error('RealtimeVoice: server error:', action.message)
        if (window.uiModule?.showError) window.uiModule.showError(action.message)
        break
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
