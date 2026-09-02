// static/js/voiceMode.js

/**
 * Voice Mode — continuous hands-free voice conversation.
 *
 * Combines STT (voiceRecorder) and TTS (aiTTSManager) into a single
 * conversation loop:
 *
 *   1. Mic arms automatically
 *   2. User speaks → STT transcribes → message auto-sends
 *   3. AI response streams back → TTS auto-plays (via aiTTSManager.autoPlay)
 *   4. After TTS finishes → mic re-arms for the next turn
 *
 * The module owns no UI itself; it reports state changes through the
 * `onStateChange` callback passed to `init()` so the host (app.js) can
 * update the toggle button / send button / status indicators.
 *
 * Dependencies are resolved at call time from `window` to avoid circular
 * imports: `window.aiTTSManager` for TTS and `window.voiceRecorderModule`
 * for STT. The latter is also accepted as an argument to `init()` so the
 * host can inject the live module instance directly.
 */

const VoiceMode = {
  _active: false,
  _armed: false,
  _busy: false,
  _errStreak: 0,
  _onStateChange: null,

  // Saved TTS autoPlay value, restored on deactivate
  _savedAutoPlay: false,

  // Bound input listener reference (for add/removeEventListener)
  _inputHandler: null,
  _submitTimer: null,

  // Injected/resolved recorder module
  _recorder: null,

  /**
   * Initialize voice mode with a state-change callback.
   * @param {(state: {active: boolean, armed: boolean, busy: boolean}) => void} onStateChange
   * @param {object} [recorderModule] — voiceRecorder module instance (defaults to window.voiceRecorderModule)
   */
  init(onStateChange, recorderModule) {
    this._onStateChange = onStateChange || null
    this._recorder = recorderModule || window.voiceRecorderModule || null
  },

  /**
   * Resolve the recorder module lazily in case init() was called before
   * the module was attached to window.
   */
  _getRecorder() {
    if (!this._recorder) this._recorder = window.voiceRecorderModule || null
    return this._recorder
  },

  /**
   * Activate voice mode. Forces TTS autoPlay on, arms the mic, and
   * notifies the host. Async: when the cached STT provider still reads
   * 'disabled' (e.g. restore-on-page-load runs before the stats fetch,
   * or STT was just enabled in settings) it refreshes the provider from
   * the server before deciding.
   */
  async activate() {
    if (this._active) return

    const recorder = this._getRecorder()
    if (!recorder) {
      console.warn('VoiceMode: voiceRecorder module not available')
      return
    }

    // STT must be enabled for voice mode to function. The cached provider
    // may be stale ('disabled' is also the pre-fetch default), so re-check
    // against the server before refusing.
    if (!recorder._sttProvider || recorder._sttProvider === 'disabled') {
      if (recorder.refreshSttProvider) {
        try { await recorder.refreshSttProvider() } catch (e) { /* ignore */ }
      }
    }
    if (!recorder._sttProvider || recorder._sttProvider === 'disabled') {
      if (window.uiModule?.showToast) window.uiModule.showToast('Enable Speech-to-Text to use Voice Mode')
      else console.warn('VoiceMode: STT is disabled')
      return
    }

    // Re-check after the await above: a concurrent activate() may have won
    if (this._active) return

    this._active = true
    this._armed = false
    this._busy = false
    this._errStreak = 0

    // Force TTS auto-play so AI responses are spoken during streaming
    const tts = window.aiTTSManager
    if (tts) {
      this._savedAutoPlay = tts.autoPlay
      tts.autoPlay = true
    }

    this._notify()
    this._armMic()
  },

  /**
   * Deactivate voice mode. Stops any in-flight recording and TTS, then
   * restores the previous TTS autoPlay setting.
   */
  deactivate() {
    if (!this._active) return

    this._active = false
    this._armed = false
    this._busy = false

    // Tear down recording
    const recorder = this._getRecorder()
    if (recorder && recorder.getIsRecording && recorder.getIsRecording()) {
      try { recorder.stopRecording() } catch (e) { /* ignore */ }
    }

    // Stop TTS playback
    const tts = window.aiTTSManager
    if (tts) {
      try { tts.stop() } catch (e) { /* ignore */ }
      tts.autoPlay = this._savedAutoPlay
    }

    // Remove the input listener and any pending submit timer
    this._detachInputListener()
    if (this._submitTimer) {
      clearTimeout(this._submitTimer)
      this._submitTimer = null
    }

    this._notify()
  },

  /**
   * Toggle voice mode on or off.
   */
  toggle() {
    if (this._active) this.deactivate()
    else this.activate()
  },

  /**
   * Arm the microphone for the next utterance. Starts recording via the
   * voiceRecorder module and attaches a one-shot input listener that
   * detects the transcribed text and auto-submits it.
   *
   * @private
   */
  _armMic() {
    if (!this._active || this._busy) return

    const recorder = this._getRecorder()
    if (!recorder) return

    this._armed = true
    this._notify()

    // Attach input listener before recording starts so we catch the
    // transcription as soon as it lands in #message
    this._attachInputListener()

    try {
      recorder.startRecording(
        // onFileCreated — only fires when STT is disabled/fallback; voice
        // mode requires STT so this should be rare. The file is dropped;
        // opts.onDone below decides whether to re-arm.
        (file) => {
          console.warn('VoiceMode: audio file created instead of transcription (STT fallback?)')
        },
        // showToast — keep quiet in voice mode to avoid noise
        null,
        // showError
        (msg) => this._onRecordingError(msg),
        {
          // End-of-speech detection: recording auto-stops after silence,
          // which triggers transcription and closes the loop.
          vad: true,
          onDone: (outcome) => this._onRecordingDone(outcome),
        }
      )
    } catch (e) {
      console.error('VoiceMode: failed to start recording:', e)
      this.deactivate()
    }
  },

  /**
   * Called by the recorder on a recording-time error (voiceRecorder.js'
   * showError callback). A failed STT transcription (bad endpoint,
   * network error, server 500 — see the 2026-09-02 incident where this
   * was only a console.error, invisible to the user) is recoverable: it
   * is also reported via onDone('error') below, whose 3-strikes counter
   * decides whether to deactivate. Any other error (no mic, permission
   * denied, insecure context) has no matching onDone call, so it must
   * deactivate immediately or voice mode would stay "armed" forever.
   *
   * @private
   * @param {string} msg
   */
  _onRecordingError(msg) {
    console.error('VoiceMode: recording error:', msg)
    const isTranscriptionError = typeof msg === 'string' && msg.startsWith('Transcription failed')
    if (isTranscriptionError) {
      const httpMatch = msg.match(/HTTP (\d+)/)
      const detail = httpMatch ? ` (HTTP ${httpMatch[1]})` : ''
      this._showSttError(`Speech recognition failed${detail}: check STT settings`)
      return
    }
    this.deactivate()
    this._showSttError(msg)
  },

  /**
   * Surface an STT failure visibly instead of only logging it. Reuses the
   * app's existing error-toast mechanism (window.uiModule.showError —
   * persistent with a dismiss button, unlike the quieter showToast).
   *
   * @private
   * @param {string} msg
   */
  _showSttError(msg) {
    if (window.uiModule?.showError) window.uiModule.showError(msg)
    else if (window.uiModule?.showToast) window.uiModule.showToast(msg)
  },

  /**
   * Called by the recorder when a recording cycle finishes. On a
   * successful transcription the input listener takes over (auto-send);
   * on silence or a failed transcription the mic re-arms so the loop
   * keeps going instead of hanging. Three transcription errors in a row
   * deactivate voice mode.
   *
   * @private
   * @param {'transcribed'|'empty'|'error'|'file'} outcome
   */
  _onRecordingDone(outcome) {
    if (!this._active || this._busy) return
    if (outcome === 'transcribed') {
      this._errStreak = 0
      return
    }
    if (outcome === 'error') {
      this._errStreak = (this._errStreak || 0) + 1
      if (this._errStreak >= 3) {
        console.error('VoiceMode: 3 transcription errors in a row, deactivating')
        this.deactivate()
        this._showSttError('Voice mode uitgeschakeld na 3 STT-fouten')
        return
      }
    }
    // 'empty' / 'file' / recoverable 'error' — re-arm for another attempt
    this._armed = false
    this._notify()
    setTimeout(() => {
      if (this._active && !this._busy) this._armMic()
    }, 800)
  },

  /**
   * Attach a listener to the #message input that, when voice mode is
   * armed and non-empty text appears, auto-submits the chat form after a
   * short debounce.
   *
   * @private
   */
  _attachInputListener() {
    const input = document.getElementById('message')
    if (!input) return

    this._detachInputListener()

    this._inputHandler = () => {
      if (!this._active || !this._armed) return
      const text = input.value.trim()
      if (!text) return

      // Debounce: wait 300ms for the transcription to settle before sending
      if (this._submitTimer) clearTimeout(this._submitTimer)
      this._submitTimer = setTimeout(() => {
        this._submitTimer = null
        this._onTranscription(text)
      }, 300)
    }

    input.addEventListener('input', this._inputHandler)
  },

  /**
   * Remove the #message input listener.
   *
   * @private
   */
  _detachInputListener() {
    const input = document.getElementById('message')
    if (input && this._inputHandler) {
      input.removeEventListener('input', this._inputHandler)
    }
    this._inputHandler = null
  },

  /**
   * Called when STT transcription has landed in the input. Marks the mic
   * as disarmed, the mode as busy, and submits the chat form.
   *
   * @private
   * @param {string} text
   */
  _onTranscription(text) {
    if (!this._active) return

    this._armed = false
    this._busy = true
    this._detachInputListener()
    this._notify()

    const form = document.getElementById('chat-form')
    if (form) {
      form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }))
    } else {
      // Fallback: click the send button directly
      const sendBtn = document.querySelector('.send-btn')
      if (sendBtn) sendBtn.click()
    }
  },

  /**
   * Called by the host when AI streaming starts. Marks the mode as busy
   * so the mic does not re-arm mid-response.
   */
  onStreamStart() {
    if (!this._active) return
    this._busy = true
    this._armed = false
    this._notify()
  },

  /**
   * Called by the host when the AI response and TTS playback are complete.
   * Clears the busy flag and re-arms the mic for the next turn after a
   * short delay (allows any trailing TTS audio to finish cleanly).
   */
  onResponseComplete() {
    if (!this._active) return
    this._busy = false
    this._notify()

    // Re-arm after a brief pause so TTS audio tail doesn't get captured
    setTimeout(() => {
      if (this._active && !this._busy) this._armMic()
    }, 500)
  },

  /**
   * Notify the host of the current state.
   *
   * @private
   */
  _notify() {
    if (this._onStateChange) {
      this._onStateChange({ active: this._active, armed: this._armed, busy: this._busy })
    }
  },

  /** Whether voice mode is currently active. */
  get isActive() { return this._active },

  /** Whether the mic is currently armed (listening). */
  get isArmed() { return this._armed },

  /** Whether we are waiting for an AI response / TTS playback. */
  get isBusy() { return this._busy },
}

export default VoiceMode
