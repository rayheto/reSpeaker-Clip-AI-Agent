// reSpeaker Clip AI Agent frontend
// Supports three VOICE_INPUT_MODE values:
//   browser — legacy system-microphone push-to-talk only
//   clip    — reSpeaker Clip only (physical button + web click-to-toggle)
//   both    — manual Clip/browser selector (development)

const micBtn = document.getElementById('micBtn');
const clipBtn = document.getElementById('clipBtn');
const clipBtnLabel = document.getElementById('clipBtnLabel');
const clipStatusLine = document.getElementById('clipStatusLine');
const inputModeRow = document.getElementById('inputModeRow');
const inputModeSelect = document.getElementById('inputModeSelect');
const statusEl = document.getElementById('status');
const chatBox = document.getElementById('chatBox');
const audioPlayer = document.getElementById('audioPlayer');

let mediaRecorder;
let audioChunks = [];
let isRecording = false;
let conversationId = null;
let currentAssistantMsg = null;
let lastSentText = null;     // last text request, for Gmail auth retry

let clipMode = false;        // true when the active input is the Clip
let clipRecording = false;   // device recording state from events
let clipBusy = false;        // guards duplicate mouse/touch actions
let clipOffline = true;      // until /api/clip/status says otherwise
let clipEventSource = null;
let clipStartPending = false;
let clipStartedByWeb = false;
let clipDisconnectTimer = null;
let clipDisconnectError = '';

// RTC warm-pause live streaming state (device double-click + web button)
let rtcPhase = 'disconnected';   // disconnected|arming|paused|capturing|finalizing|stopped
let rtcSession = null;
let rtcUtteranceId = null;
let rtcProcessing = false;       // a final STT/LLM pass is running for an utterance
let rtcStreamActive = false;     // a reply is streaming (thinking/token events in flight)
let rtcAssistantMsg = null;      // live assistant bubble for the streaming RTC reply
const utteranceBubbles = new Map();   // utterance_id -> provisional user bubble
const finalTranscripts = new Set();   // utterances whose final text is displayed

const CLIP_OFFLINE_GRACE_MS = 60000;

function pauseTts() {
    if (audioPlayer && !audioPlayer.paused) audioPlayer.pause();
}

// ---- config embedded by the server ---------------------------------------
let CLIP_CONFIG = { input_mode: 'browser', clip_enabled: false, record_mode: 'enhanced' };
try {
    const el = document.getElementById('clip-config');
    if (el) CLIP_CONFIG = JSON.parse(el.textContent);
} catch (e) { console.error('bad clip config', e); }

const INPUT_MODE = CLIP_CONFIG.input_mode || 'browser';
const CLIP_AVAILABLE = !!CLIP_CONFIG.clip_enabled && INPUT_MODE !== 'browser';

// ---- shared UI helpers -----------------------------------------------------

function addMessage(role, text) {
    const msg = document.createElement('div');
    msg.className = 'message ' + role;
    msg.textContent = text;
    chatBox.appendChild(msg);
    chatBox.scrollTop = chatBox.scrollHeight;
}

function setStatus(text, isError) {
    statusEl.textContent = text;
    statusEl.className = 'status' + (isError ? ' error' : '');
}

function setClipStatusLine(text, isError) {
    clipStatusLine.textContent = text || '';
    clipStatusLine.className = 'clip-status' + (isError ? ' error' : '');
}

function clipButtonLabel() {
    if (rtcPhase === 'capturing') return 'Pause';
    if (rtcPhase === 'paused') return 'Resume';
    return clipRecording ? 'Stop' : 'Clip';
}

function setClipUI(recording, busy, offline) {
    clipRecording = !!recording;
    clipBusy = !!busy;
    clipOffline = offline !== undefined ? !!offline : clipOffline;
    clipBtn.classList.toggle('recording', clipRecording || rtcPhase === 'capturing');
    clipBtnLabel.textContent = clipButtonLabel();
    clipBtn.disabled = clipBusy || clipOffline || !CLIP_AVAILABLE;
}

function noteClipDisconnected(error) {
    clipOffline = true;
    clipDisconnectError = error || clipDisconnectError || 'disconnected';
    // Disable commands while the link is unavailable, but keep the current
    // status text during the one-minute reconnect grace period.
    setClipUI(clipRecording, clipBusy, true);
    if (clipDisconnectTimer !== null) return;
    clipDisconnectTimer = window.setTimeout(() => {
        clipDisconnectTimer = null;
        if (!clipOffline) return;
        setClipStatusLine('Clip offline — ' + clipDisconnectError, true);
    }, CLIP_OFFLINE_GRACE_MS);
}

function noteClipConnected(label) {
    clipOffline = false;
    clipDisconnectError = '';
    if (clipDisconnectTimer !== null) {
        window.clearTimeout(clipDisconnectTimer);
        clipDisconnectTimer = null;
    }
    if (label) setClipStatusLine(label);
}

function applyMode() {
    const both = INPUT_MODE === 'both';
    inputModeRow.hidden = !both;
    if (INPUT_MODE === 'browser') {
        clipMode = false;
        micBtn.hidden = false;
        clipBtn.hidden = true;
    } else if (INPUT_MODE === 'clip') {
        clipMode = true;
        micBtn.hidden = true;
        clipBtn.hidden = false;
    } else { // both
        clipMode = inputModeSelect.value === 'clip';
        micBtn.hidden = clipMode;
        clipBtn.hidden = !clipMode;
        if (clipMode) {
            micBtn.classList.remove('recording');
        } else {
            micBtn.disabled = false;
        }
    }
    if (CLIP_AVAILABLE) {
        setClipUI(clipRecording, clipBusy);
    }
}

function registerContext(cid) {
    if (!cid || !CLIP_AVAILABLE) return;
    fetch('/api/clip/context', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: cid }),
    }).catch((err) => console.error('register context failed', err));
}

function rememberConversation(cid) {
    if (!cid || cid === conversationId) return;
    conversationId = cid;
    registerContext(cid);
}

// ---- Composio Connect Link flow -------------------------------------------

// Composio returns a hosted OAuth link (a *.composio.dev URL) when a tool
// needs an account. The backend caches the link lazily; we render a Connect
// button when the reply asks the user to connect.
let composioShownLinks = new Set();   // dedupe button per reply text
let composioConnectButtons = [];      // live Connect buttons (disabled after success)
let pendingComposioRequest = null;    // the user request blocked on authorization
let composioBaseline = new Set();     // connected toolkits before the auth flow
let composioAwaiting = false;         // waiting for the user to finish connecting

function looksLikeComposioConnectHint(text) {
    return /连接按钮|connect button/i.test(text || '');
}

function insertComposioConnectButton(replyText) {
    if (composioShownLinks.has(replyText)) return;
    composioShownLinks.add(replyText);
    const wrap = document.createElement('div');
    wrap.className = 'message system';
    const btn = document.createElement('button');
    btn.textContent = 'Connect Account';
    btn.className = 'connect-btn';
    btn.addEventListener('click', openComposioConnect);
    wrap.appendChild(btn);
    chatBox.appendChild(wrap);
    chatBox.scrollTop = chatBox.scrollHeight;
    composioConnectButtons.push(btn);
}

function openComposioConnect() {
    // Fetch the lazily-cached connect link from the backend (the URL is never
    // embedded in the chat reply). Then open it in a new tab.
    fetch('/api/composio/connect/link')
        .then((r) => r.json())
        .then((d) => {
            if (!d.redirect_url) { setStatus('No connection link available', true); return; }
            composioAwaiting = true;
            fetch('/api/composio/auth/connected')
                .then((r2) => r2.json())
                .then((s) => { composioBaseline = new Set(s.connected || []); })
                .catch(() => {});
            window.open(d.redirect_url, '_blank', 'noopener,noreferrer');
        })
        .catch((err) => { setStatus('Error: ' + err.message, true); });
}

function onComposioAuthorized() {
    composioAwaiting = false;
    composioConnectButtons.forEach((b) => { b.disabled = true; b.textContent = '✓ Connected'; });
    addMessage('assistant', '✅ Account connected.');
    setStatus('Ready');
    if (pendingComposioRequest) {
        const text = pendingComposioRequest;
        pendingComposioRequest = null;
        sendTextChat(text);
    }
}

// Composio's hosted connect page opens in a plain tab (opener is null), so we
// can't rely on postMessage. Poll for a newly-added connection when the main
// window regains focus after the user returns from the connect tab.
window.addEventListener('focus', () => {
    if (!composioAwaiting || !pendingComposioRequest) return;
    fetch('/api/composio/auth/connected')
        .then((r) => r.json())
        .then((d) => {
            const now = new Set(d.connected || []);
            const added = Array.from(now).filter((s) => !composioBaseline.has(s));
            if (added.length > 0) onComposioAuthorized();
        })
        .catch(() => {});
});

function maybeOfferComposioConnect(responseText, originalRequest) {
    if (!looksLikeComposioConnectHint(responseText)) return;
    pendingComposioRequest = originalRequest || lastSentText || null;
    insertComposioConnectButton(responseText);
}

// ---- browser microphone input (legacy) ------------------------------------

async function startRecording() {
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
        audioChunks = [];

        mediaRecorder.ondataavailable = (event) => {
            if (event.data.size > 0) audioChunks.push(event.data);
        };

        mediaRecorder.onstop = async () => {
            stream.getTracks().forEach((track) => track.stop());
            const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
            await sendVoice(audioBlob);
        };

        mediaRecorder.start();
        isRecording = true;
        micBtn.classList.add('recording');
        setStatus('Listening...');
    } catch (err) {
        setStatus('Mic access denied', true);
        console.error(err);
    }
}

function stopRecording() {
    if (mediaRecorder && isRecording) {
        mediaRecorder.stop();
        isRecording = false;
        micBtn.classList.remove('recording');
        setStatus('Processing...');
    }
}

async function sendVoice(audioBlob) {
    const formData = new FormData();
    formData.append('audio', audioBlob, 'recording.webm');
    if (conversationId) formData.append('conversation_id', conversationId);

    try {
        const response = await fetch('/api/voice', { method: 'POST', body: formData });

        const transcript = response.headers.get('X-Transcript') || '';
        const textResponse = response.headers.get('X-Response') || '';
        rememberConversation(response.headers.get('X-Conversation-Id'));

        if (transcript) addMessage('user', transcript);
        if (textResponse) addMessage('assistant', textResponse);
        maybeOfferComposioConnect(textResponse, transcript);

        if (response.ok) {
            const audioBlob2 = await response.blob();
            const audioUrl = URL.createObjectURL(audioBlob2);
            audioPlayer.src = audioUrl;
            audioPlayer.play();
        }
        setStatus('Ready');
    } catch (err) {
        setStatus('Error: ' + err.message, true);
        console.error(err);
    }
}

micBtn.addEventListener('mousedown', startRecording);
micBtn.addEventListener('mouseup', stopRecording);
micBtn.addEventListener('mouseleave', () => { if (isRecording) stopRecording(); });
micBtn.addEventListener('touchstart', (e) => { e.preventDefault(); startRecording(); });
micBtn.addEventListener('touchend', (e) => { e.preventDefault(); stopRecording(); });

// ---- reSpeaker Clip input ---------------------------------------------------

function clipStart() {
    if (clipBusy || clipOffline) return;
    clipStartPending = true;
    setClipUI(false, true);
    setStatus('Starting Clip...');
    fetch('/api/clip/recordings/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            mode: CLIP_CONFIG.record_mode || 'enhanced',
            conversation_id: conversationId || undefined,
        }),
    })
        .then((resp) => {
            if (!resp.ok) return resp.json().then((d) => { throw new Error(d.error || 'start failed'); });
            return resp.json();
        })
        .then((data) => {
            clipStartPending = false;
            clipStartedByWeb = true;
            rememberConversation(data.conversation_id);
            setClipUI(true, false);
            setStatus('Recording (Clip)...');
        })
        .catch((err) => {
            clipStartPending = false;
            clipStartedByWeb = false;
            setClipUI(false, false);
            setStatus('Clip error: ' + err.message, true);
        });
}

function clipStop() {
    if (clipStartPending) return;
    if (clipBusy || clipOffline) return;
    setClipUI(false, true);
    setStatus('Stopping Clip...');
    fetch('/api/clip/recordings/stop', { method: 'POST' })
        .then((resp) => {
            if (!resp.ok) return resp.json().then((d) => { throw new Error(d.error || 'stop failed'); });
            return resp.json();
        })
        .then((data) => {
            clipStartedByWeb = false;
            if (data.session) setStatus('Processing session ' + data.session + '...');
        })
        .catch((err) => {
            // 409 (not recording) is expected after a physical stop.
            if (String(err.message).indexOf('not recording') === -1 && String(err.message).indexOf('409') === -1) {
                setStatus('Clip error: ' + err.message, true);
            }
            clipStartedByWeb = false;
            setClipUI(false, false);
        });
}

function toggleClipRecording(event) {
    event.preventDefault();
    if (!CLIP_AVAILABLE || clipOffline) {
        setStatus('Clip is offline — check BLE', true);
        return;
    }
    if (rtcPhase === 'capturing') {
        clipStreamPause();
        return;
    }
    if (rtcPhase === 'paused' || rtcPhase === 'arming') {
        clipStreamResume();
        return;
    }
    if (clipRecording) {
        // A web press can also stop a recording started from the physical key.
        clipStop();
        return;
    }
    clipStart();
}

// ---- RTC stream control (warm pause: one utterance per RESUME->PAUSE) -----

function clipStreamResume() {
    if (clipBusy || clipOffline) return;
    // Optimistic UI: the next RESUME interval is a new logical utterance.
    setClipUI(false, true);
    setStatus('Listening (Clip)...');
    fetch('/api/clip/stream/resume', { method: 'POST' })
        .then((resp) => {
            if (!resp.ok) return resp.json().then((d) => { throw new Error(d.error || 'resume failed'); });
            return resp.json();
        })
        .then(() => {
            setClipUI(false, false);
            setStatus('Listening (Clip)...');
        })
        .catch((err) => {
            setClipUI(false, false);
            setStatus('Clip error: ' + err.message, true);
            refreshClipStatus();
        });
}

function clipStreamPause() {
    if (clipBusy || clipOffline) return;
    pauseTts();
    setClipUI(false, true);
    setStatus('Finalizing...');
    fetch('/api/clip/stream/pause', { method: 'POST' })
        .then((resp) => {
            if (!resp.ok) return resp.json().then((d) => { throw new Error(d.error || 'pause failed'); });
            return resp.json();
        })
        .then(() => { setClipUI(false, false); })
        .catch((err) => {
            // Idempotent: a physical double-click may have paused first.
            setClipUI(false, false);
            setStatus('Clip error: ' + err.message, true);
            refreshClipStatus();
        });
}

function renderRtcStatus() {
    applyClipStateLine();
    if (rtcStreamActive) return;  // streaming status owned by thinking/token events
    if (rtcPhase === 'capturing') {
        setStatus('Listening (Clip)...');
    } else if (rtcPhase === 'arming') {
        setStatus('Armed — starting RTC...');
    } else if (rtcPhase === 'finalizing' || rtcProcessing) {
        setStatus('Finalizing...');
    } else if (rtcPhase === 'paused') {
        setStatus('Armed — press Resume to speak');
    }
}

function applyClipStateLine() {
    const labels = {
        capturing: 'Listening — Logged utterance ' + (rtcUtteranceId || ''),
        paused: 'Armed (warm pause) — no BLE audio while paused',
        arming: 'Armed — starting',
        finalizing: 'Finalizing utterance ' + (rtcUtteranceId || ''),
        stopped: 'RTC stopped — click Clip to restart legacy recording',
    };
    const label = labels[rtcPhase];
    if (label && !clipOffline) setClipStatusLine(label);
}

clipBtn.addEventListener('click', toggleClipRecording);

if (inputModeSelect) {
    inputModeSelect.addEventListener('change', () => { applyMode(); });
}

function handleClipSseEvent(eventName, data) {
    if (eventName === 'connection') {
        if (data && data.connected) {
            noteClipConnected('Clip connected' + (data.status && data.status.device_name ? ' — ' + data.status.device_name : ''));
            setClipUI(clipRecording, false, false);
            // Re-sync recording/RTC state after any reconnect.
            refreshClipStatus();
        } else {
            noteClipDisconnected(data && data.error);
        }
        return;
    }
    if (eventName === 'rtc_state') {
        handleRtcStateEvent(data);
        return;
    }
    if (eventName === 'transcript') {
        handleTranscriptEvent(data);
        return;
    }
    if (eventName === 'thinking') {
        // LLM reply in progress: tool call / generation started.
        rtcStreamActive = true;
        rtcProcessing = true;
        if (data && data.tool) {
            setStatus('AI is using ' + data.tool + '...');
        } else {
            setStatus('Thinking...');
        }
        return;
    }
    if (eventName === 'token') {
        rtcStreamActive = true;
        const text = (data && data.text) || '';
        if (!text) return;
        if (!rtcAssistantMsg) {
            rtcAssistantMsg = document.createElement('div');
            rtcAssistantMsg.className = 'message assistant streaming';
            chatBox.appendChild(rtcAssistantMsg);
        }
        rtcAssistantMsg.textContent += text;
        chatBox.scrollTop = chatBox.scrollHeight;
        return;
    }
    if (eventName === 'recording') {
        if (data && data.action === 'started') {
            clipStartedByWeb = data.trigger === 'web';
            setClipUI(true, false);
            setStatus('Recording (Clip)...');
            setClipStatusLine('Recording session ' + (data.session || '') + ' (' + (data.trigger || '') + ')');
        } else if (data && data.action === 'stopped') {
            clipStartedByWeb = false;
            setClipUI(false, true);
            setStatus('Processing session ' + (data.session || '') + '...');
            setClipStatusLine('Session stopped — downloading');
        }
    } else if (eventName === 'workflow') {
        const s = data && data.status;
        if (s === 'downloading') { setStatus('Downloading audio...'); setClipStatusLine('Downloading ' + data.session); }
        else if (s === 'processing') { setStatus('Transcribing & thinking...'); setClipStatusLine('Processing ' + data.session); }
        else if (s === 'failed') { setStatus('Processing failed: ' + (data.error || 'unknown'), true); setClipStatusLine('', true); setClipUI(false, false); }
    } else if (eventName === 'result') {
        const uid = data.utterance_id != null ? data.utterance_id : null;
        if (uid !== null) {
            // The authoritative final transcript was already rendered by the
            // transcript event: never add the user bubble a second time.
            if (!finalTranscripts.has(uid)) {
                if (data.transcript) addMessage('user', data.transcript);
            }
            utteranceBubbles.delete(uid);
            finalTranscripts.delete(uid);
        } else if (data.transcript) {
            addMessage('user', data.transcript);
        }
        if (rtcAssistantMsg) {
            // A live streamed assistant bubble already carries the answer —
            // finalize its text instead of double-adding a second message.
            if (data.response) rtcAssistantMsg.textContent = data.response;
            rtcAssistantMsg.classList.remove('streaming');
            rtcAssistantMsg = null;
        } else if (data.response) {
            addMessage('assistant', data.response);
        }
        rememberConversation(data.conversation_id);
        maybeOfferComposioConnect(data.response, data.transcript);
        if (data.response) playTts(data.response);
        rtcStreamActive = false;
        rtcProcessing = false;
        setClipUI(false, false);
        setStatus('Ready');
        if (uid !== null) {
            setClipStatusLine('Answered utterance ' + uid + ' (RTC)');
        } else {
            setClipStatusLine('Answered from session ' + data.session + ' (' + (data.trigger || '') + ')');
        }
    }
}

// ---- RTC event handling ----------------------------------------------------

function handleRtcStateEvent(data) {
    if (!data || !data.phase) return;
    const previousPhase = rtcPhase;
    rtcPhase = data.phase;
    if (data.session) rtcSession = data.session;
    if (data.utterance_id != null) {
        // A new logical utterance starts: stop any pending TTS to reduce
        // feedback while the user speaks again.
        if (data.phase === 'capturing' && data.utterance_id !== rtcUtteranceId) {
            pauseTts();
        }
        rtcUtteranceId = data.utterance_id;
    }
    if (data.error) {
        setClipStatusLine('RTC error: ' + data.error, true);
    }
    if (data.phase === 'capturing') {
        rtcProcessing = false;
    } else if (data.phase === 'finalizing') {
        // A finalize job is now in flight; mark processing immediately so the
        // next renderRtcStatus (or a paused event right after) shows
        // "Finalizing..." instead of the armed prompt.
        rtcProcessing = true;
    }
    renderRtcStatus();
    setClipUI(clipRecording, clipBusy, clipOffline);
}

function handleTranscriptEvent(data) {
    if (!data || data.utterance_id == null) return;
    const uid = data.utterance_id;
    if (data.final) {
        if (data.skipped || !data.text) {
            // Too short / empty authoritative result: no LLM invocation and
            // no stale partial text left in the conversation.
            removeProvisionalBubble(uid);
            return;
        }
        finalTranscripts.add(uid);
        let bubble = utteranceBubbles.get(uid);
        if (bubble) {
            bubble.textContent = data.text;
            bubble.classList.remove('provisional');
            bubble.classList.add('final');
        } else {
            bubble = addProvisionalBubble(uid, data.text);
            bubble.classList.remove('provisional');
            bubble.classList.add('final');
        }
        return;
    }
    if (!data.text) return;
    let bubble = utteranceBubbles.get(uid);
    if (bubble) {
        bubble.textContent = data.text;
    } else {
        addProvisionalBubble(uid, data.text);
    }
}

function addProvisionalBubble(uid, text) {
    const msg = document.createElement('div');
    msg.className = 'message user provisional';
    msg.textContent = text;
    chatBox.appendChild(msg);
    chatBox.scrollTop = chatBox.scrollHeight;
    utteranceBubbles.set(uid, msg);
    return msg;
}

function removeProvisionalBubble(uid) {
    const bubble = utteranceBubbles.get(uid);
    if (bubble) bubble.remove();
    utteranceBubbles.delete(uid);
}

function refreshClipStatus() {
    fetch('/api/clip/status')
        .then((resp) => (resp.ok ? resp.json() : Promise.reject(new Error('status ' + resp.status))))
        .then((data) => {
            if (!data.connected) {
                noteClipDisconnected(data.last_error);
            } else {
                const suffix = data.transfer_active ? ' — downloading' : '';
                noteClipConnected('Clip connected — ' + (data.device_name || data.device_id || '') + suffix);
                if (data.rtc_phase) {
                    rtcPhase = data.rtc_phase;
                    if (data.rtc_session) rtcSession = data.rtc_session;
                    if (data.rtc_utterance_id != null) rtcUtteranceId = data.rtc_utterance_id;
                    rtcProcessing = !!data.rtc_processing;
                }
                renderRtcStatus();
                // Recording is not busy; only in-flight transfers/requests disable it.
                setClipUI(!!data.recording, !!data.transfer_active, false);
                if (data.recording) setClipUI(true, false);
            }
        })
        .catch((err) => { noteClipDisconnected(err && err.message); });
}

function openClipEvents() {
    if (!CLIP_AVAILABLE || clipEventSource) return;
    const es = new EventSource('/api/clip/events');
    clipEventSource = es;
    es.onopen = () => { refreshClipStatus(); };
    es.onerror = () => {
        // EventSource retries automatically.  Short stream/BLE interruptions
        // stay silent and only become visible after the shared grace period.
        noteClipDisconnected('event stream disconnected');
    };
    ['connection', 'recording', 'workflow', 'result', 'rtc_state', 'transcript', 'thinking', 'token'].forEach((name) => {
        es.addEventListener(name, (ev) => {
            let data = {};
            try { data = JSON.parse(ev.data || '{}'); } catch (_) {}
            handleClipSseEvent(name, data);
        });
    });
}

// ---- text chat (SSE) ---------------------------------------------------------

const textForm = document.getElementById('textForm');
const textInput = document.getElementById('textInput');

textForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = textInput.value.trim();
    if (!text) return;
    textInput.value = '';
    sendTextChat(text);
});

function handleSseEvent(rawEvent) {
    const lines = rawEvent.split('\n');
    let event = 'message';
    let dataStr = '';
    for (const line of lines) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) dataStr += line.slice(5).trim();
    }
    let data = {};
    try { data = JSON.parse(dataStr); } catch (_) {}

    if (event === 'thinking') {
        setStatus('AI is using ' + (data.tool || 'a tool') + '...');
    } else if (event === 'token') {
        if (!currentAssistantMsg) {
            currentAssistantMsg = document.createElement('div');
            currentAssistantMsg.className = 'message assistant';
            currentAssistantMsg.textContent = '';
            chatBox.appendChild(currentAssistantMsg);
        }
        currentAssistantMsg.textContent += data.text || '';
        chatBox.scrollTop = chatBox.scrollHeight;
    } else if (event === 'done') {
        if (currentAssistantMsg) {
            currentAssistantMsg.textContent = data.response || currentAssistantMsg.textContent;
            currentAssistantMsg = null;
        } else if (data.response) {
            addMessage('assistant', data.response);
        }
        rememberConversation(data.conversation_id);
        maybeOfferComposioConnect(data.response, lastSentText);
        if (data.response) playTts(data.response);
        setStatus('Ready');
    } else if (event === 'error') {
        addMessage('assistant', 'Error: ' + (data.message || 'unknown error'));
        setStatus('Error', true);
    }
}

async function sendTextChat(text) {
    addMessage('user', text);
    lastSentText = text;
    setStatus('Thinking...');

    try {
        const response = await fetch('/api/chat/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, conversation_id: conversationId }),
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf('\n\n')) !== -1) {
                const rawEvent = buffer.slice(0, idx);
                buffer = buffer.slice(idx + 2);
                handleSseEvent(rawEvent);
            }
        }
    } catch (err) {
        setStatus('Error: ' + err.message, true);
        console.error(err);
    }
}

async function playTts(text) {
    try {
        const resp = await fetch('/api/tts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
        });
        if (!resp.ok) return;
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        audioPlayer.src = url;
        audioPlayer.play();
    } catch (err) {
        console.error('TTS failed', err);
    }
}

// ---- Composio tool selection (side panel) -----------------------------------

const toolsBtn = document.getElementById('toolsBtn');
const toolsSidebar = document.getElementById('toolsSidebar');
const toolsOverlay = document.getElementById('toolsOverlay');
const toolsCloseBtn = document.getElementById('toolsCloseBtn');
const toolsApplyBtn = document.getElementById('toolsApplyBtn');
const toolkitSearch = document.getElementById('toolkitSearch');
const toolkitList = document.getElementById('toolkitList');

const allToolkits = [];   // cached catalog from the backend
const selectedSet = new Set();  // slugs currently checked

function openToolsSidebar() {
    toolsSidebar.hidden = false;
    toolsOverlay.hidden = false;
    toolkitSearch.value = '';
    if (allToolkits.length === 0) {
        loadToolkits();
    } else {
        renderToolkits(allToolkits);
    }
}

function closeToolsSidebar() {
    toolsSidebar.hidden = true;
    toolsOverlay.hidden = true;
}

async function loadToolkits() {
    try {
        const resp = await fetch('/api/composio/toolkits');
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'failed to load toolkits');
        allToolkits.length = 0;
        allToolkits.push(...(data.toolkits || []));
        selectedSet.clear();
        (data.selected || []).forEach((s) => selectedSet.add(s));
        renderToolkits(allToolkits);
    } catch (err) {
        toolkitList.innerHTML = '<div class="tools-empty">Error: ' + err.message + '</div>';
    }
}

function renderToolkits(items) {
    const q = (toolkitSearch.value || '').trim().toLowerCase();
    const filtered = items.filter((t) => {
        if (!q) return true;
        return t.slug.toLowerCase().includes(q) || (t.name || '').toLowerCase().includes(q);
    });
    if (filtered.length === 0) {
        toolkitList.innerHTML = '<div class="tools-empty">No matching apps.</div>';
        return;
    }
    toolkitList.innerHTML = '';
    for (const t of filtered) {
        const row = document.createElement('label');
        row.className = 'tools-item' + (selectedSet.has(t.slug) ? ' selected' : '');

        // logo (fall back to a letter avatar)
        const logo = document.createElement('span');
        logo.className = 'tools-item-logo';
        if (t.logo) {
            const img = document.createElement('img');
            img.src = t.logo;
            img.alt = '';
            img.loading = 'lazy';
            img.onerror = () => { img.remove(); logo.textContent = (t.name || t.slug)[0]; };
            logo.appendChild(img);
        } else {
            logo.textContent = (t.name || t.slug)[0];
        }

        // body: name + slug
        const body = document.createElement('span');
        body.className = 'tools-item-body';
        const name = document.createElement('span');
        name.className = 'tools-item-name';
        name.textContent = t.name || t.slug;
        const slug = document.createElement('span');
        slug.className = 'tools-item-slug';
        slug.textContent = t.slug;
        body.appendChild(name);
        body.appendChild(slug);

        // right: auth badge + tool count
        const meta = document.createElement('span');
        meta.className = 'tools-item-meta';
        if (t.auth_badge) {
            const badge = document.createElement('span');
            badge.className = 'tools-item-badge';
            badge.textContent = t.auth_badge;
            meta.appendChild(badge);
        }
        const count = document.createElement('span');
        count.className = 'tools-item-count';
        count.textContent = (t.tools_count != null ? t.tools_count + ' tools' : '');
        meta.appendChild(count);

        // checkbox
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = row.classList.contains('selected');
        cb.addEventListener('change', () => {
            if (cb.checked) { selectedSet.add(t.slug); row.classList.add('selected'); }
            else { selectedSet.delete(t.slug); row.classList.remove('selected'); }
        });

        row.appendChild(logo);
        row.appendChild(body);
        row.appendChild(meta);
        row.appendChild(cb);
        toolkitList.appendChild(row);
    }
}

async function applyToolkits() {
    try {
        const resp = await fetch('/api/composio/toolkits', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ toolkits: Array.from(selectedSet) }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'failed to apply');
        closeToolsSidebar();
        setStatus('Ready');
    } catch (err) {
        setStatus('Error: ' + err.message, true);
    }
}

if (toolsBtn) {
    toolsBtn.addEventListener('click', openToolsSidebar);
    toolsCloseBtn.addEventListener('click', closeToolsSidebar);
    toolsOverlay.addEventListener('click', closeToolsSidebar);
    toolsApplyBtn.addEventListener('click', applyToolkits);
    toolkitSearch.addEventListener('input', () => renderToolkits(allToolkits));
    // Preload the catalog so the sidebar is populated the moment it opens.
    loadToolkits();
}

// ---- init ---------------------------------------------------------------------

if (INPUT_MODE === 'both') {
    inputModeSelect.value = 'clip';
}
applyMode();
if (CLIP_AVAILABLE) {
    refreshClipStatus();
    // SSE drives low-latency updates; polling also heals a stale offline label
    // if the event stream misses a short BLE disconnect/reconnect transition.
    window.setInterval(refreshClipStatus, 5000);
    openClipEvents();
} else if (INPUT_MODE !== 'browser') {
    setClipStatusLine('Clip runtime unavailable on this server', true);
    setClipUI(false, false, true);
}
