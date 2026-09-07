const statusEl = document.querySelector('#status');
const sessionEl = document.querySelector('#session');
const messagesEl = document.querySelector('#messages');
const preCallEl = document.querySelector('#pre-call');
const callStageEl = document.querySelector('#call-stage');
const stageStatusEl = document.querySelector('#stage-status');
const transcriptEl = document.querySelector('#transcript');
const transcriptToggleEl = document.querySelector('#transcript-toggle');
const startEl = document.querySelector('#start');
const callNoticeEl = document.querySelector('#call-notice');
let socket, micContext, playbackContext, stream, micNode, playbackNode;
let history = [], partialUser = '', partialAssistant = '';
const BATCH = 1600, HEADER = 16;
const MAX_SOCKET_BUFFER = 512 * 1024;
let sequence = 0;
let pending = new Int16Array(BATCH), pendingOffset = 0;
let audioStarted = false, renderPending = false, lastError = '';

function render() {
  renderPending = false;
  messagesEl.innerHTML = '';
  if (!history.length && !partialUser && !partialAssistant) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'Transcript appears here once the conversation starts.';
    messagesEl.appendChild(empty);
    return;
  }
  for (const item of history) {
    const div = document.createElement('div'); div.className = `bubble ${item.role}`; div.textContent = item.text; messagesEl.appendChild(div);
  }
  for (const [role, text] of [['user', partialUser], ['assistant', partialAssistant]]) {
    if (!text) continue;
    const div = document.createElement('div'); div.className = `bubble ${role} partial`; div.textContent = text; messagesEl.appendChild(div);
  }
}
function setStatus(text, state) {
  statusEl.textContent = text;
  stageStatusEl.textContent = text;
  if (state) callStageEl.dataset.state = state;
}
function setCallVisible(visible) {
  preCallEl.classList.toggle('hidden', visible);
  startEl.disabled = visible;
}
function setCallNotice(text = '') {
  callNoticeEl.textContent = text;
  callNoticeEl.classList.toggle('visible', Boolean(text));
}
function scheduleRender() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(render);
}
function sendBatch() {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  if (socket.bufferedAmount > MAX_SOCKET_BUFFER) {
    pending = new Int16Array(BATCH); pendingOffset = 0;
    setStatus('Network backpressure', 'thinking');
    return;
  }
  const packet = new ArrayBuffer(HEADER + BATCH * 2), view = new DataView(packet);
  view.setFloat64(0, Date.now(), false); view.setUint32(8, 0, false); view.setUint32(12, sequence++, false);
  new Int16Array(packet, HEADER).set(pending); socket.send(packet); pending = new Int16Array(BATCH); pendingOffset = 0;
}
function consume(samples) {
  let offset = 0;
  while (offset < samples.length) {
    const count = Math.min(samples.length - offset, BATCH - pendingOffset);
    pending.set(samples.subarray(offset, offset + count), pendingOffset); pendingOffset += count; offset += count;
    if (pendingOffset === BATCH) sendBatch();
  }
}
async function start() {
  if (socket && socket.readyState === WebSocket.OPEN) return;
  lastError = '';
  setCallNotice('');
  setCallVisible(true);
  setStatus('Calling...', 'thinking');
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  socket.onmessage = async ({data}) => {
    const msg = JSON.parse(data);
    if (msg.type === 'session_id') sessionEl.textContent = msg.content;
    if (msg.type === 'lab_status') {
      const value = String(msg.content || '');
      const state = /speak|play|tts/i.test(value) ? 'speaking' : /\blisten/i.test(value) ? 'listening' : 'thinking';
      setStatus(value, state);
    }
    if (msg.type === 'lab_ready' && !audioStarted) {
      try { await initializeAudio(); } catch (error) { lastError = error.message; setStatus(lastError, 'idle'); socket.close(); }
    }
    if (msg.type === 'lab_error') { lastError = msg.content; setStatus(msg.content, 'idle'); }
    if (msg.type === 'partial_user_request') { partialUser = msg.content; setStatus('Listening...', 'listening'); scheduleRender(); }
    if (msg.type === 'final_user_request') { history.push({role:'user', text:msg.content}); partialUser=''; setStatus('Thinking...', 'thinking'); scheduleRender(); }
    if (msg.type === 'partial_assistant_answer') { partialAssistant = msg.content; setStatus('Thinking...', 'thinking'); scheduleRender(); }
    if (msg.type === 'final_assistant_answer') { history.push({role:'assistant', text:msg.content}); partialAssistant=''; scheduleRender(); }
    if (msg.type === 'tts_chunk') {
      setStatus('Speaking...', 'speaking');
      const raw = atob(msg.content), bytes = new Uint8Array(raw.length);
      for (let i=0;i<raw.length;i++) bytes[i]=raw.charCodeAt(i);
      const pcm = new Int16Array(bytes.buffer);
      playbackNode.port.postMessage(pcm, [pcm.buffer]);
    }
    if (msg.type === 'tts_end') playbackNode.port.postMessage({type:'end'});
    if (msg.type === 'stop_tts' || msg.type === 'tts_interruption') { playbackNode.port.postMessage({type:'clear'}); setStatus('Listening...', 'listening'); }
  };
  socket.onclose = () => {
    setStatus(lastError || 'Call ended', 'idle');
    cleanup();
    if (lastError) setCallNotice(lastError);
    setCallVisible(false);
  };
  await new Promise((resolve, reject) => { socket.onopen=resolve; socket.onerror=reject; });
  setStatus('Ringing...', 'thinking');
}
async function initializeAudio() {
  if (audioStarted) return;
  audioStarted = true;
  stream = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true}});
  micContext = new AudioContext({sampleRate:16000});
  playbackContext = new AudioContext({sampleRate:24000});
  await micContext.audioWorklet.addModule('/static/mic-worklet.js');
  await playbackContext.audioWorklet.addModule('/static/playback-worklet.js');
  micNode = new AudioWorkletNode(micContext, 'rvc-lab-mic');
  playbackNode = new AudioWorkletNode(playbackContext, 'rvc-lab-playback');
  micNode.port.onmessage = ({data}) => consume(new Int16Array(data));
  playbackNode.port.onmessage = ({data}) => {
    if (data.type === 'started') { socket.send(JSON.stringify({type:'tts_start'})); setStatus('Speaking...', 'speaking'); }
    if (data.type === 'stopped') { socket.send(JSON.stringify({type:'tts_stop'})); setStatus('Listening...', 'listening'); }
  };
  socket.send(JSON.stringify({
    type:'client_ready', mic_sample_rate:micContext.sampleRate,
    playback_sample_rate:playbackContext.sampleRate, playback_prebuffer_ms:120
  }));
  micContext.createMediaStreamSource(stream).connect(micNode);
  playbackNode.connect(playbackContext.destination);
  setStatus('Listening...', 'listening');
}
function cleanup() {
  if (stream) stream.getTracks().forEach(track => track.stop());
  if (micContext) micContext.close(); if (playbackContext) playbackContext.close();
  stream = micContext = playbackContext = micNode = playbackNode = null;
  audioStarted = false;
  pending = new Int16Array(BATCH); pendingOffset = 0;
}
startEl.onclick = () => start().catch(error => {
  lastError = error.message;
  setStatus(lastError, 'idle');
  setCallNotice(lastError);
  setCallVisible(false);
});
document.querySelector('#stop').onclick = () => { if (socket) socket.close(); else cleanup(); };
transcriptToggleEl.onclick = () => {
  const open = transcriptEl.classList.toggle('open');
  transcriptToggleEl.classList.toggle('is-active', open);
  transcriptToggleEl.title = open ? 'Hide transcript' : 'Show transcript';
  transcriptToggleEl.setAttribute('aria-label', transcriptToggleEl.title);
};
render();
