// Voice for Reaper.
//
// In: the browser's speech recognition (Chrome and Edge send the audio to their speech service; the settings
// say so). Push to talk only: it listens while the mic button or the space bar is held, never in the background.
// Out: Reaper's own voice, made on this laptop by the HQ server (Kokoro + the robot chain, engine/voice.py). The
// page asks for one sentence at a time and fetches the next while the current one plays, so he starts talking
// quickly; the orb pulses with the real sound. If the server can't make it (the voice isn't installed, or an
// error), the browser's own speech is used instead: the deepest local voice, a little lower and slower.
// Numbers, ids and amounts are spoken plainly.
// Narration (off by default) speaks short lines for real message flows only, never over itself.

const STORE = { muted: 'hq.muted', narrate: 'hq.narrate' };
const PITCH = 0.78;
const RATE = 0.94;
const MAX_SPOKEN = 700;          // longer replies: the start is spoken, the rest stays on screen
const NARRATION_GAP_MS = 6000;   // at most one narration line every 6 seconds
// Deepest, most machine-like first. Windows' own voices (David, Mark) are local and robotic.
const PREFERRED = [/microsoft david/i, /microsoft mark/i, /microsoft guy/i, /david/i, /mark/i,
                   /uk english male/i, /\bmale\b/i, /christopher|eric|roger|andrew|brian|steffan/i];

function remember(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : v === '1'; } catch { return fallback; }
}
function store(key, value) {
  try { localStorage.setItem(key, value ? '1' : '0'); } catch { /* private window: kept for this visit only */ }
}

/** What Reaper's text sounds like out loud: symbols said as words, numbers kept exactly. */
export function spoken(text) {
  let s = String(text || '')
    .replace(/`+/g, '')
    .replace(/https?:\/\/\S+/g, 'a link on screen')
    .replace(/\$(\d+)\.(\d{2})\d*/g, (_, d, c) => {
      const dollars = Number(d);
      const cents = Number(c);
      if (!dollars) return cents ? `${cents} cent${cents === 1 ? '' : 's'}` : 'less than one cent';
      return `${dollars} dollar${dollars === 1 ? '' : 's'}${cents ? ` and ${cents} cent${cents === 1 ? '' : 's'}` : ''}`;
    })
    .replace(/\$(\d+)/g, '$1 dollars')
    .replace(/#(\d+)/g, 'number $1')
    .replace(/(\d+)%/g, '$1 percent')
    .replace(/->|→/g, ' to ')
    .replace(/[*_>|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (s.length > MAX_SPOKEN) {
    const cut = s.lastIndexOf('. ', MAX_SPOKEN);
    s = `${s.slice(0, cut > 200 ? cut + 1 : MAX_SPOKEN)} Details on screen.`;
  }
  return s;
}

/** A short narration line for one real flow, or null (most flows aren't narrated). */
export function narrationLine(flow) {
  const { from_node: from, to_node: to, kind, label } = flow;
  if (kind === 'chat') return null;
  if (/^refused|: refused/.test(label)) return 'Order refused. Details on screen.';
  if (from === 'scout' && kind === 'proposal') return 'Scout reports a new lead.';
  if (to === 'builder' && kind === 'work') return 'Builder at work.';
  if (from === 'builder' && to === 'critic') return 'Build ready. Critic reviewing.';
  if (from === 'critic' && kind === 'result') return /PASS/.test(label) ? 'Critic: pass.' : 'Critic: fail.';
  if (from === 'inspector' && kind === 'result') return 'Inspector scored a lead.';
  if (from === 'messenger' && kind === 'result') return 'Messenger: draft ready for your review.';
  if (kind === 'block') return 'Guard blocked an item.';
  if (kind === 'alert' && /STAND DOWN/.test(label)) return 'Standing down. All units halted.';
  if (kind === 'alert') return `Alert. ${label}.`;
  if (kind === 'decision' && to === 'mayor') return 'Decision received at the Town Hall.';
  if (kind === 'work' && to === 'scout') return 'Scout deployed.';
  if (kind === 'work' && to === 'inspector') return 'Inspector deployed.';
  return null;
}

/** The line cut into sentences (the server makes one at a time). */
export function sentences(text) {
  return String(text || '').split(/(?<=[.!?])\s+/).map((x) => x.trim()).filter(Boolean);
}

/**
 * @param {object} hooks  {orb, onHeard(text), onStatus(what, detail), voiceStatus(), fetchSpeech(text)}
 *   voiceStatus() resolves to {available, reason}; fetchSpeech(text) resolves to the WAV as an ArrayBuffer.
 */
export function createVoice({ orb, onHeard, onStatus, voiceStatus, fetchSpeech }) {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = window.speechSynthesis;
  let muted = remember(STORE.muted, false);
  let narrate = remember(STORE.narrate, false);
  let voice = null;
  let recognizer = null;
  let heard = '';
  let listening = false;
  let lastNarration = 0;
  let nextNarration = null;
  let speakingUntil = 0;
  // Reaper's own voice from the server: null until the server answers, then true or false.
  let own = null;
  let ownReason = '';
  let audio = null;        // the page's audio output, made on first use (after Charlie has clicked or typed)
  let generation = 0;      // a newer line (or Stop) cancels whatever is still being said
  let making = false;      // the server is making a line right now (narration waits for it)
  let source = null;
  voiceStatus?.().then((d) => { own = Boolean(d?.available); ownReason = d?.reason || ''; })
    .catch(() => { own = false; ownReason = "the server didn't answer"; });

  function pickVoice() {
    const voices = synth ? synth.getVoices().filter((v) => /^en/i.test(v.lang)) : [];
    const score = (v) => {
      const rank = PREFERRED.findIndex((p) => p.test(v.name));
      return (v.localService ? 0 : 100) + (rank === -1 ? 50 : rank);
    };
    voice = voices.sort((a, b) => score(a) - score(b))[0] || null;
  }
  if (synth) {
    pickVoice();
    synth.addEventListener?.('voiceschanged', pickVoice);
  }

  function speak(text, { force = false } = {}) {
    if (muted && !force) return;
    const line = spoken(text);
    if (!line) return;
    if (own && fetchSpeech) { speakOwn(line); return; }
    speakBrowser(line);
  }

  let filler = null;       // the browser voice's steady orb pulse (one at a time, always cleared)
  let flushTimer = null;   // narration's one waiting retry

  function stopAll() {
    generation += 1;
    clearInterval(filler);
    filler = null;
    try { source?.stop(); } catch { /* already finished */ }
    source = null;
    synth?.cancel();
  }

  // Reaper's own voice: sentence by sentence; the next one is fetched while the current one plays.
  async function speakOwn(line) {
    stopAll();
    const mine = generation;
    const parts = sentences(line);
    const decode = async (text) => {
      making = true;
      try {
        const wav = await fetchSpeech(text);
        return await audio.decodeAudioData(wav);
      } finally {
        making = false;
      }
    };
    try {
      audio ||= new AudioContext();
      if (audio.state === 'suspended') await audio.resume();
      speakingUntil = Infinity;
      onStatus?.('speaking');
      let next = decode(parts[0]);
      for (let i = 0; i < parts.length; i += 1) {
        const buffer = await next;
        if (mine !== generation) return;
        if (i + 1 < parts.length) next = decode(parts[i + 1]);
        await play(buffer, mine);
        if (mine !== generation) return;
      }
    } catch {
      if (mine !== generation) return;
      own = false;   // something's wrong with it: the browser's voice for the rest of this visit
      ownReason = 'it failed on this line';
      speakBrowser(line);
      return;
    }
    speakingUntil = performance.now();
    onStatus?.('spoken');
  }

  // One sentence through the speakers, with the orb following its loudness.
  function play(buffer, mine) {
    return new Promise((resolve) => {
      const src = audio.createBufferSource();
      src.buffer = buffer;
      const analyser = audio.createAnalyser();
      analyser.fftSize = 512;
      src.connect(analyser);
      analyser.connect(audio.destination);
      const data = new Float32Array(analyser.fftSize);
      let live = true;
      const follow = () => {
        if (!live) return;
        analyser.getFloatTimeDomainData(data);
        let sum = 0;
        for (const v of data) sum += v * v;
        orb.voice?.(Math.min(1, Math.sqrt(sum / data.length) * 7));
        requestAnimationFrame(follow);
      };
      src.onended = () => { live = false; orb.voice?.(0); resolve(); };
      source = src;
      src.start();
      follow();
      if (mine !== generation) { live = false; resolve(); }
    });
  }

  function speakBrowser(line) {
    if (!synth) return;
    synth.cancel();
    const u = new SpeechSynthesisUtterance(line);
    if (voice) u.voice = voice;
    u.pitch = PITCH;
    u.rate = RATE;
    let lastWord = 0;
    // The orb pulses on each word. Some voices send no word events, so a steady pulse fills in. It stops when the
    // line ends, or at the latest after the time the line could possibly take (some browsers never say it ended).
    clearInterval(filler);
    const started = performance.now();
    const longest = 2000 + line.length * 120;
    const mine = setInterval(() => {
      if (performance.now() - started > longest) { done(); return; }
      if (performance.now() - lastWord > 400) orb.pulse(0.5);
    }, 190);
    filler = mine;
    u.onboundary = (e) => { if (e.name === 'word') { lastWord = performance.now(); orb.pulse(1); } };
    u.onstart = () => { speakingUntil = Infinity; onStatus?.('speaking'); };
    const done = () => {
      clearInterval(mine);
      if (filler === mine) filler = null;
      if (speakingUntil === Infinity) { speakingUntil = performance.now(); onStatus?.('spoken'); }
    };
    u.onend = done;
    u.onerror = done;
    synth.speak(u);
  }

  // The microphone's loudness while listening, for the orb (it swells as Charlie talks). Measured in the page
  // with the browser's own audio tools; the sound itself isn't kept or sent anywhere.
  let meter = null;
  async function startMeter() {
    if (meter || !navigator.mediaDevices?.getUserMedia) return;
    meter = { stop: false };
    const mine = meter;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const ctx = new AudioContext();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      ctx.createMediaStreamSource(stream).connect(analyser);
      const data = new Float32Array(analyser.fftSize);
      Object.assign(mine, { stream, ctx });
      const tick = () => {
        if (mine.stop) return;
        analyser.getFloatTimeDomainData(data);
        let sum = 0;
        for (const v of data) sum += v * v;
        orb.level(Math.min(1, Math.sqrt(sum / data.length) * 6));
        requestAnimationFrame(tick);
      };
      if (mine.stop) stopMeter(mine); else tick();
    } catch {
      meter = null;   // no level, no problem: speech recognition reports its own errors
    }
  }
  function stopMeter(which = meter) {
    if (!which) return;
    which.stop = true;
    which.stream?.getTracks().forEach((t) => t.stop());
    which.ctx?.close();
    if (which === meter) meter = null;
    orb.level(0);
  }

  function start() {
    if (!Recognition || listening) return false;
    stopAll();
    recognizer = new Recognition();
    recognizer.lang = 'en-US';
    recognizer.continuous = true;
    recognizer.interimResults = true;
    heard = '';
    recognizer.onresult = (e) => {
      let text = '';
      for (let i = 0; i < e.results.length; i += 1) text += e.results[i][0].transcript;
      heard = text.trim();
      orb.pulse(0.4);
      onStatus?.('heard', heard);
    };
    recognizer.onerror = (e) => {
      const why = {
        'not-allowed': 'Microphone blocked. Allow it from the icon in the address bar, or type instead.',
        'service-not-allowed': 'Microphone blocked. Allow it from the icon in the address bar, or type instead.',
        network: "Chrome's speech service can't be reached (it needs the internet). Type instead.",
        'no-speech': 'Heard nothing. Hold the mic (or Space) while you talk.',
        'audio-capture': 'No microphone found. Type instead.',
      }[e.error];
      if (why) onStatus?.('error', why);
    };
    recognizer.onend = () => {
      listening = false;
      stopMeter();
      onStatus?.('stopped');
      if (heard) onHeard(heard);
      heard = '';
    };
    try {
      recognizer.start();
      listening = true;
      startMeter();
      onStatus?.('listening');
      return true;
    } catch {
      return false;
    }
  }

  function stop() {
    if (recognizer && listening) recognizer.stop();
  }

  // `who` is the sender's plain name ("Builder"): with it, the speech bubble's own words are spoken.
  function narrateFlow(flow, stopped, who) {
    if (!narrate || muted || stopped) return;
    const short = narrationLine(flow);   // which flows are worth saying out loud (most aren't)
    if (!short) return;
    const line = who && flow.says ? `${who}: ${flow.says}.` : short;
    nextNarration = line;   // only the newest line waits; the feed keeps every flow
    if (!flushTimer) flush();   // a retry is already waiting: it will pick up this newest line
  }

  function flush() {
    flushTimer = null;
    if (!nextNarration) return;
    const now = performance.now();
    // Narration waits its turn: never while a line is being made or said, so only one is ever made at a time.
    const busy = making || synth?.speaking || speakingUntil === Infinity || now < speakingUntil + 800;
    if (busy || now - lastNarration < NARRATION_GAP_MS) {
      if (!flushTimer) flushTimer = setTimeout(flush, 1000);   // one retry waiting, however many lines arrive
      return;
    }
    lastNarration = now;
    const line = nextNarration;
    nextNarration = null;
    speak(line);
  }

  return {
    supportsIn: Boolean(Recognition),
    supportsOut: Boolean(synth),
    start, stop, speak,
    get listening() { return listening; },
    get voiceName() {
      if (own) return "Reaper's own voice (made on this laptop)";
      const browser = voice ? voice.name : 'the browser default';
      return own === false && ownReason ? `${browser} (Reaper's own voice: ${ownReason})` : browser;
    },
    get muted() { return muted; },
    set muted(v) { muted = Boolean(v); store(STORE.muted, muted); if (muted) stopAll(); },
    stop: stopAll,
    get narration() { return narrate; },
    set narration(v) { narrate = Boolean(v); store(STORE.narrate, narrate); },
    narrate: narrateFlow,
  };
}
