// static/js/voiceRecorder.js

/**
 * Voice recording with optional Speech-to-Text transcription.
 *
 * STT providers:
 *   "disabled"       — record audio as file attachment (original behavior)
 *   "browser"        — use Web Speech API for real-time transcription
 *   "local"          — send recording to server /api/stt/transcribe (Whisper)
 *   "endpoint:<id>"  — send recording to server /api/stt/transcribe (API)
 */

let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let recordingStartTime = null;
let recordingInterval = null;

// Browser STT state
let _recognition = null;
let _browserTranscript = '';

// Cached STT provider — refreshed on settings change
let _sttProvider = 'disabled';

// Set when stopRecording() is called before getUserMedia resolves, so the
// pending promise can release the mic instead of orphaning the stream.
let _cancelRequested = false;

// Active VAD monitor teardown (interval + AudioContext), if any
let _vadCleanup = null;

/**
 * Voice-activity-detection state machine. Pure logic (no WebAudio) so it
 * is unit-testable: feed it one RMS sample per tick, it returns 'stop'
 * when end-of-speech (silence after speech) or the max duration is
 * reached, else null.
 */
export function createVoiceActivityDetector({
  threshold = 0.012,
  silenceMs = 1400,
  minSpeechMs = 250,
  maxMs = 90000,
  tickMs = 100,
} = {}) {
  let speechRunMs = 0;
  let silenceRunMs = 0;
  let totalMs = 0;
  let spoke = false;

  return {
    push(rms) {
      totalMs += tickMs;
      if (totalMs >= maxMs) return 'stop';
      if (rms >= threshold) {
        speechRunMs += tickMs;
        silenceRunMs = 0;
        if (speechRunMs >= minSpeechMs) spoke = true;
      } else {
        silenceRunMs += tickMs;
        speechRunMs = 0;
      }
      if (spoke && silenceRunMs >= silenceMs) return 'stop';
      return null;
    },
    get hasSpeech() { return spoke; },
  };
}

/**
 * Monitor the mic stream with an AnalyserNode and auto-stop on
 * end-of-speech. Returns a cleanup function. Falls back to a hard
 * max-duration timer when WebAudio is unavailable, so the voice-mode
 * loop always completes a turn.
 */
function _startVadMonitor(stream, vadOpts, onAutoStop) {
  const opts = (vadOpts && typeof vadOpts === 'object') ? vadOpts : {};
  const tickMs = opts.tickMs || 100;
  const det = createVoiceActivityDetector({ ...opts, tickMs });
  let ctx = null;
  let timer = null;
  const cleanup = () => {
    if (timer) { clearInterval(timer); timer = null; }
    if (ctx) { try { ctx.close(); } catch (e) { /* ignore */ } ctx = null; }
  };
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) throw new Error('AudioContext not supported');
    ctx = new AC();
    if (ctx.state === 'suspended') ctx.resume().catch(() => {});
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    timer = setInterval(() => {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      if (det.push(rms) === 'stop') {
        cleanup();
        onAutoStop();
      }
    }, tickMs);
    return cleanup;
  } catch (e) {
    console.warn('VAD unavailable, falling back to max-duration stop:', e);
    const maxMs = opts.maxMs || 90000;
    const fallbackTimer = setTimeout(onAutoStop, maxMs);
    return () => clearTimeout(fallbackTimer);
  }
}

/**
 * Fetch current STT provider from server settings
 */
async function refreshSttProvider() {
  try {
    const res = await fetch('/api/stt/stats', { credentials: 'same-origin' });
    if (res.ok) {
      const stats = await res.json();
      _sttProvider = stats.provider || 'disabled';
      // Notify the send button to update its icon
      if (window._updateSendBtnIcon) window._updateSendBtnIcon();
    }
  } catch (e) {
    console.warn('Failed to fetch STT stats:', e);
  }
}

/**
 * Format seconds as MM:SS
 */
function formatTime(seconds) {
  const mins = Math.floor(seconds / 60).toString().padStart(2, '0');
  const secs = (seconds % 60).toString().padStart(2, '0');
  return `${mins}:${secs}`;
}

/**
 * Reset UI state after recording ends
 */
function _resetRecordingUI() {
  isRecording = false;
  if (recordingInterval) {
    clearInterval(recordingInterval);
    recordingInterval = null;
  }
  // Reset send button via global callback
  const sendBtn = document.querySelector('.send-btn');
  if (sendBtn) {
    sendBtn.classList.remove('recording');
    sendBtn.dataset.mode = '';
  }
  if (window._updateSendBtnIcon) {
    setTimeout(window._updateSendBtnIcon, 50);
  }
}

/**
 * Start browser speech recognition alongside recording
 */
function startBrowserSTT() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return;

  _browserTranscript = '';
  _recognition = new SpeechRecognition();
  _recognition.continuous = true;
  _recognition.interimResults = false;
  _recognition.lang = '';

  _recognition.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) {
        _browserTranscript += event.results[i][0].transcript + ' ';
      }
    }
  };

  _recognition.onerror = (e) => {
    console.warn('Browser STT error:', e.error);
  };

  _recognition.start();
}

function stopBrowserSTT() {
  if (_recognition) {
    try { _recognition.stop(); } catch (e) { /* ignore */ }
    _recognition = null;
  }
  return _browserTranscript.trim();
}

/**
 * Send audio to server for transcription
 */
async function transcribeOnServer(audioBlob) {
  const formData = new FormData();
  formData.append('file', audioBlob, 'audio.webm');

  const res = await fetch('/api/stt/transcribe', {
    method: 'POST',
    credentials: 'same-origin',
    body: formData,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail?.message || 'Transcription failed');
  }

  const data = await res.json();
  return data.text || '';
}

/**
 * Insert transcribed text into the chat input
 */
function insertTranscription(text, showToast) {
  if (!text) return;
  const input = document.getElementById('message');
  if (!input) return;

  const existing = input.value.trim();
  input.value = existing ? existing + ' ' + text : text;

  // Trigger auto-resize and icon update
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();

  if (showToast) showToast('Transcribed');
}

/**
 * Start voice recording
 */
export function startRecording(onFileCreated, showToast, showError, opts = {}) {
  // Check for secure context (getUserMedia requires HTTPS or localhost)
  if (!window.isSecureContext) {
    if (showError) showError('Microphone requires HTTPS. Use a reverse proxy with SSL or access via localhost.');
    _resetRecordingUI();
    return;
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    if (showError) showError('Microphone not supported in this browser.');
    _resetRecordingUI();
    return;
  }

  audioChunks = [];
  _cancelRequested = false;

  navigator.mediaDevices.getUserMedia({ audio: true })
    .then(stream => {
      if (_cancelRequested) {
        // stopRecording() was called while the permission prompt / device
        // open was still pending — release the mic instead of leaking it.
        stream.getTracks().forEach(track => track.stop());
        _resetRecordingUI();
        return;
      }
      mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });

      mediaRecorder.ondataavailable = event => {
        if (event.data.size > 0) {
          audioChunks.push(event.data);
        }
      };

      mediaRecorder.onstop = async () => {
        if (_vadCleanup) { _vadCleanup(); _vadCleanup = null; }
        stream.getTracks().forEach(track => track.stop());

        const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
        const provider = _sttProvider;
        // Outcome for opts.onDone: 'transcribed' | 'empty' | 'error' | 'file'
        let outcome = 'file';

        if (provider === 'browser') {
          const transcript = stopBrowserSTT();
          if (transcript) {
            insertTranscription(transcript, showToast);
            outcome = 'transcribed';
          } else {
            if (showToast) showToast('No speech detected');
            const audioFile = new File([audioBlob], `voice-message-${Date.now()}.webm`, { type: 'audio/webm' });
            if (onFileCreated) onFileCreated(audioFile);
            outcome = 'empty';
          }
        } else if (provider === 'local' || provider.startsWith('endpoint:')) {
          // Show "Transcribing..." feedback
          if (showToast) showToast('Transcribing...', 5000);
          try {
            const transcript = await transcribeOnServer(audioBlob);
            if (transcript) {
              insertTranscription(transcript, showToast);
              outcome = 'transcribed';
            } else {
              if (showToast) showToast('No speech detected');
              outcome = 'empty';
            }
          } catch (e) {
            console.error('STT transcription error:', e);
            if (showError) showError('Transcription failed: ' + e.message);
            // Fallback: attach as file
            const audioFile = new File([audioBlob], `voice-message-${Date.now()}.webm`, { type: 'audio/webm' });
            if (onFileCreated) onFileCreated(audioFile);
            outcome = 'error';
          }
        } else {
          // STT disabled — attach audio file
          const audioFile = new File([audioBlob], `voice-message-${Date.now()}.webm`, { type: 'audio/webm' });
          if (onFileCreated) onFileCreated(audioFile);
        }

        _resetRecordingUI();
        if (opts.onDone) {
          try { opts.onDone(outcome); } catch (e) { console.error('onDone callback error:', e); }
        }
      };

      mediaRecorder.start();
      isRecording = true;
      recordingStartTime = new Date();

      // Voice mode: auto-stop on end-of-speech so the turn completes hands-free
      if (opts.vad) {
        _vadCleanup = _startVadMonitor(stream, opts.vad === true ? {} : opts.vad, () => stopRecording());
      }

      // Start browser STT if that's the provider
      if (_sttProvider === 'browser') {
        startBrowserSTT();
      }

      if (showToast) {
        showToast('Recording...');
      }
    })
    .catch(error => {
      console.error('Microphone access error:', error);
      if (showError) {
        if (error.name === 'NotAllowedError') {
          showError('Microphone access denied. Check browser permissions.');
        } else if (error.name === 'NotFoundError') {
          showError('No microphone found.');
        } else {
          showError('Microphone error: ' + error.message);
        }
      }
      _resetRecordingUI();
    });
}

/**
 * Stop voice recording
 */
export function stopRecording() {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    // isRecording will be set to false in _resetRecordingUI called from onstop
  } else {
    // getUserMedia may still be pending — flag it so the promise releases
    // the mic when it resolves instead of starting an orphaned recording.
    _cancelRequested = true;
    if (_vadCleanup) { _vadCleanup(); _vadCleanup = null; }
    _resetRecordingUI();
  }
}

/**
 * Check if currently recording
 */
export function getIsRecording() {
  return isRecording;
}

/**
 * Initialize recording state
 */
export function init() {
  isRecording = false;
  refreshSttProvider();
}

const voiceRecorderModule = {
  startRecording,
  stopRecording,
  getIsRecording,
  init,
  refreshSttProvider,
  get _sttProvider() { return _sttProvider; },
  set _sttProvider(v) { _sttProvider = v; },
};

export default voiceRecorderModule;
