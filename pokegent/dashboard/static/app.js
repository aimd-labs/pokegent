(() => {
  const status = document.querySelector('#status');
  const decision = document.querySelector('#decision');
  const history = document.querySelector('#history');
  const frame = document.querySelector('#frame');
  const frameLink = document.querySelector('#frame-link');
  const sound = document.querySelector('#sound');
  let audioContext;
  let audioGain;
  let audioSocket;
  let nextStartTime = 0;
  let soundOn = false;
  const AUDIO_RATE = 32768;
  const AUDIO_LATENCY = 0.12;
  const AUDIO_LATE_TOLERANCE = 0.05;
  let streamLive = false;
  let streamBase = '';
  let streamEnabled = false;
  const streamPath = path => streamEnabled ? (streamBase ? `${streamBase}${path}` : path) : null;
  const streamSocket = path => {
    const url = streamPath(path);
    if (!url) return null;
    return url.startsWith('/')
      ? `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${url}`
      : url.replace(/^http/, 'ws');
  };
  const poll = async () => {
    try {
      const response = await fetch('/telemetry', { cache: 'no-store' });
      const data = await response.json();
      const server = data.server || {};
      const session = data.game?.session_id ? ` · session ${data.game.session_id}` : '';
      status.textContent = `${server.emulator_ready ? 'emulator ready' : 'waiting for emulator'}${session}`;
      const current = (data.pokegent || data.pokejev)?.decision;
      decision.textContent = current ? JSON.stringify(current, null, 2) : 'No decision yet.';
      const rows = (current?.recent || []).slice(0, 12);
      history.replaceChildren(...(rows.length ? rows : [{ id: 'empty', result_label: 'No decisions yet.' }]).map(row => {
        const item = document.createElement('li'); item.textContent = row.result_label || row.pick_label || 'decision'; return item;
      }));
      const frameUrl = streamPath(`/frame.png?_=${Date.now()}`);
      if (frameUrl) {
        frame.src = frameUrl;
        frameLink.href = streamPath('/frame.png');
        frameLink.hidden = false;
      } else {
        status.textContent += ' · stream off';
      }
    } catch (error) { status.textContent = `Dashboard unavailable: ${error}`; }
  };
  const playPcm = ({ data }) => {
    const bytes = new Int16Array(data);
    const buffer = audioContext.createBuffer(2, bytes.length / 2, AUDIO_RATE);
    for (let channel = 0; channel < 2; channel++) {
      const output = buffer.getChannelData(channel);
      for (let i = 0; i < output.length; i++) output[i] = bytes[i * 2 + channel] / 32768;
    }
    const now = audioContext.currentTime;
    if (nextStartTime < now - AUDIO_LATE_TOLERANCE) nextStartTime = now + AUDIO_LATENCY;
    nextStartTime = Math.max(nextStartTime, now + AUDIO_LATENCY);
    const source = audioContext.createBufferSource();
    source.buffer = buffer; source.connect(audioGain); source.start(nextStartTime);
    nextStartTime += buffer.duration;
  };
  const pcmPlayer = () => {
    const url = streamSocket('/ws/audio');
    if (!url) return;
    audioSocket = new WebSocket(url);
    audioSocket.binaryType = 'arraybuffer';
    audioSocket.onmessage = playPcm;
    audioSocket.onclose = () => { audioSocket = null; if (soundOn) setTimeout(pcmPlayer, 1000); };
  };
  const frameStream = () => {
    const url = streamSocket('/ws/frames');
    if (!url) return;
    const socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    socket.onmessage = () => { streamLive = true; };
    socket.onclose = () => setTimeout(frameStream, 1000);
  };
  sound.addEventListener('click', () => {
    soundOn = !soundOn;
    if (!audioContext) {
      audioContext = new AudioContext({ sampleRate: AUDIO_RATE });
      audioGain = audioContext.createGain(); audioGain.connect(audioContext.destination);
    }
    if (audioContext.state === 'suspended') audioContext.resume();
    audioGain.gain.value = soundOn ? 1 : 0;
    sound.textContent = soundOn ? 'Sound on' : 'Sound off';
    if (soundOn && !audioSocket) pcmPlayer();
  });
  window.__pokegentStream = () => ({ streamLive });
  window.__pokegentQaAudit = () => [];
  fetch('/stream-info', { cache: 'no-store' }).then(response => response.json()).then(info => {
    streamEnabled = Boolean(info.enabled);
    streamBase = info.enabled ? (info.url || '') : '';
    frameStream();
    poll(); setInterval(poll, 1000);
  }).catch(() => { poll(); setInterval(poll, 1000); });
})();
