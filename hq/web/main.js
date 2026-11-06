// Reaper HQ: wires the page together. The map, the orb, the chat, the cards, voice and the live stream.
//
// STAND DOWN is a plain button in the top bar: its request goes straight to the server, which never makes it
// wait behind a chat turn, a confirmation or a publish. Resuming is not on this page (only `town resume`).
import { BASE, HttpError, get, openStream, post, speech } from './api.js';
import { createChat } from './chat.js';
import { createNeeds } from './needs.js';
import { createSites } from './sites.js';
import { createOrb } from './orb.js';
import { createPanels } from './panels.js';
import { bindVillageEvents, createVillage } from './village.js';
import { createVoice } from './voice.js';

const $ = (id) => document.getElementById(id);
const dom = {
  townState: $('town-state'), nowLine: $('now-line'), apiMeter: $('meter-api'), totalMeter: $('meter-total'),
  paused: $('paused'), banner: $('banner'), plates: $('plates'), map: $('map'), feed: $('feed'), tip: $('tip'),
  panel: $('panel'), panelTitle: $('panel-title'), panelBody: $('panel-body'), panelClose: $('panel-close'),
  scoreboard: $('scoreboard'), agentList: $('agent-list'), bubbles: $('bubbles'),
  board: document.querySelector('.board'), boardAgents: $('board-agents'), viewTools: document.querySelector('.view-tools'),
  countWorking: $('count-working'), countWaiting: $('count-waiting'), countBlocked: $('count-blocked'),
  countDone: $('count-done'),
};
const input = $('chat-input');
const status = { stopped: false, busy: false, speaking: false, listening: false, canDecide: true };
const debug = { completed: [], started: [] };
window.HQ = { debug };

const orb = createOrb($('orb'));
const panels = createPanels({ dom, onAsk: (q) => { input.value = q; input.focus(); }, onFrame: (a) => frameBeside(a) });
// Fly the camera to a building, keeping it clear of the building panel on the right.
function frameBeside(agent) {
  const panel = dom.panel.getBoundingClientRect();
  const area = dom.map.getBoundingClientRect();
  map?.frame(agent, dom.panel.hidden ? 0 : Math.max(0, area.right - panel.left));
}
let voice = null;
const chat = createChat({
  log: $('chat-log'), form: $('chat-form'), input,
  canDecide: () => status.canDecide,
  onBusy: (busy) => { status.busy = busy; updateOrb(); },
  onReply: (d, text) => {
    if (d.mode === 'stand_down') markStopped();
    voice.speak(text);
  },
});
voice = createVoice({
  orb,
  voiceStatus: () => get('/api/voice'),
  fetchSpeech: (text) => speech(text),
  onHeard: (text) => chat.send(text, { spoken: true }),
  onStatus: (what, detail) => {
    if (what === 'speaking') status.speaking = true;
    if (what === 'spoken') status.speaking = false;
    if (what === 'listening') status.listening = true;
    if (what === 'stopped') { status.listening = false; input.placeholder = 'Talk to Reaper'; }
    if (what === 'heard') input.placeholder = `Heard: ${detail}`;
    if (what === 'error') chat.say(detail, 'voice');
    updateOrb();
  },
});
window.HQ.chat = chat;
window.HQ.orb = orb;
window.HQ.voiceCtl = () => voice;
let map = null;

function updateOrb() {
  const state = status.stopped ? 'stopped' : status.listening ? 'listening' : status.speaking ? 'speaking'
    : status.busy ? 'thinking' : 'idle';
  orb.setState(state);
  map?.setOrb(state);
  $('orb-status').textContent = { stopped: 'Stood down', listening: 'Listening', speaking: 'Speaking',
                                  thinking: 'Thinking', idle: 'Standing by' }[state];
}

function applyFacts(facts) {
  panels.setFacts(facts);
  status.stopped = facts.stopped;
  chat.setStopped(facts.stopped);
  document.body.classList.toggle('is-stopped', facts.stopped);
  const jarvis = facts.agents.find((a) => a.agent === 'jarvis');
  chat.setAnalysis(jarvis?.state === 'working' && /deep analysis/.test(jarvis.task || ''));
  if (map) {
    map.setStopped(facts.stopped);
    map.setStates(facts.agents);
    map.setQueue(facts.queue.pending);
    map.setRedFlag(facts.queue.blocked > 0);
  }
  updateOrb();
}

function markStopped() {
  status.stopped = true;
  chat.setStopped(true);
  document.body.classList.add('is-stopped');
  dom.banner.hidden = false;
  dom.townState.textContent = 'STOOD DOWN';
  dom.townState.className = 'town-state is-stopped';
  map?.setStopped(true);
  updateOrb();
  refreshFacts();
}

async function refreshFacts() {
  try { applyFacts((await get('/api/facts')).facts); } catch { /* the stream brings the next facts */ }
}

let villageTimer = null;
function refreshVillageSoon() {
  clearTimeout(villageTimer);
  villageTimer = setTimeout(async () => {
    try { panels.setVillage(await get('/api/village')); } catch { /* tried again on the next flow */ }
    needs.refresh();
  }, 1500);
}

// Needs you: every item waiting for Charlie, always on screen. Reaper announces each new one once.
const needs = createNeeds({
  list: $('needs-list'), count: $('needs-count'), activity: $('needs-activity'), get, post,
  canDecide: () => status.canDecide,
  onAnnounce: (line) => { chat.say(line, 'Needs you'); voice?.speak(line); },
  onResult: (line) => chat.say(line, 'your decision'),
  // Go only PROPOSES starting autopilot (Reaper's card); it starts when Charlie presses Confirm. Stop ends it.
  autopilot: { line: $('autopilot-line'), go: $('autopilot-go'), stop: $('autopilot-stop'),
               onGo: () => chat.send('go') },
});
window.HQ.needs = needs;
window.HQ.sites = createSites({ dialog: $('sites'), list: $('sites-list'), open: $('sites-open'),
                                close: $('sites-close'), get });
setInterval(() => needs.refresh(), 5000);

// ---------------- STAND DOWN ----------------

$('stand-down').addEventListener('click', async () => {
  const button = $('stand-down');
  button.disabled = true;
  try {
    const d = await post('/api/stand-down');
    chat.say(d.reply, 'STAND DOWN button');
    voice.speak(d.reply);
    markStopped();
  } catch (err) {
    chat.say(`Stand down didn't reach the server (${err.message}). Run town stop in a terminal now.`, 'error');
  } finally {
    button.disabled = false;
  }
});

// ---------------- voice controls ----------------

const mic = $('mic');
function startMic() {
  if (!voice.supportsIn) {
    chat.say('Voice in needs Chrome or Edge (their speech recognition). Typing works everywhere.', 'voice');
    return;
  }
  voice.start();
}
mic.addEventListener('pointerdown', (e) => { e.preventDefault(); startMic(); });
mic.addEventListener('pointerup', () => voice.stop());
mic.addEventListener('pointerleave', () => voice.stop());
mic.addEventListener('click', (e) => {   // keyboard: Enter toggles listening on and off
  if (e.detail !== 0) return;
  if (voice.listening) voice.stop(); else startMic();
});
const typing = (t) => t instanceof HTMLElement && (t.matches('input, textarea, select, button, [contenteditable]')
  || Boolean(t.closest('dialog[open]')));
document.addEventListener('keydown', (e) => {
  if (e.code !== 'Space' || e.repeat || typing(e.target)) return;
  e.preventDefault();
  startMic();
});
document.addEventListener('keyup', (e) => {
  if (e.code === 'Space' && voice.listening) voice.stop();
});
if (!voice.supportsIn) {
  mic.classList.add('is-unavailable');
  mic.title = 'Voice in needs Chrome or Edge. Typing works.';
}

const mute = $('mute');
function showMute() {
  mute.setAttribute('aria-pressed', String(voice.muted));
  mute.textContent = voice.muted ? 'Sound off' : 'Sound on';
}
mute.addEventListener('click', () => { voice.muted = !voice.muted; showMute(); });
showMute();

// ---------------- graphics reset ----------------

// Windows reset the page's graphics (the village stops drawing): say so; it rebuilds itself when the browser gives
// the graphics back. If they haven't come back after 10 seconds, offer a reload (the Town isn't affected).
let graphicsTimer = null;
function graphicsLost() {
  const box = $('graphics-reset');
  box.querySelector('.graphics-reset-text').textContent = 'Graphics reset, rebuilding…';
  $('graphics-reload').hidden = true;
  box.hidden = false;
  clearTimeout(graphicsTimer);
  graphicsTimer = setTimeout(() => {
    box.querySelector('.graphics-reset-text').textContent = "The graphics didn't come back. Reload the page: the Town "
      + 'carries on meanwhile, and STAND DOWN still works.';
    $('graphics-reload').hidden = false;
  }, 10000);
}
function graphicsBack() {
  clearTimeout(graphicsTimer);
  $('graphics-reset').hidden = true;
}
$('graphics-reload').addEventListener('click', () => window.location.reload());

// The village found the graphics slow and switched to lighter graphics by itself: say so, and how to fix it.
function slowNotice(why, detail) {
  const note = $('slow-note');
  note.querySelector('.slow-why').textContent = why === 'software'
    ? `Chrome is drawing the 3D village without the graphics chip (${detail}), so it switched to lighter graphics.`
    : `The village was drawing slowly (${detail}), so it switched to lighter graphics.`;
  note.hidden = false;
}
$('slow-close').addEventListener('click', () => { $('slow-note').hidden = true; });

// Lighter graphics (lower resolution, no shadows) for a slow laptop: remembered per browser; `&lite=1` in the
// link turns it on for one visit (the browser tests use it).
function graphicsLite() {
  if (new URLSearchParams(window.location.search).get('lite') === '1') return true;
  try { return localStorage.getItem('hq.lite') === '1'; } catch { return false; }
}

// ---------------- Demo mode ----------------

// One button for showing the Town to someone: full screen, the camera drifting slowly round the town, sound and
// narration on (Reaper says what each real message is). The messages are still only the Town's real ones.
const demoButton = $('demo-mode');
let demoBefore = null;
function setDemo(on) {
  if (on === Boolean(demoBefore)) return;
  demoButton.setAttribute('aria-pressed', String(on));
  document.body.classList.toggle('is-demo', on);
  if (on) {
    demoBefore = { muted: voice.muted, narration: voice.narration };
    voice.muted = false;
    voice.narration = true;
    showMute();
    orb.setFpsCap(30);
    document.documentElement.requestFullscreen?.().catch(() => { /* not allowed here: the rest still works */ });
  } else {
    voice.muted = demoBefore.muted;
    voice.narration = demoBefore.narration;
    showMute();
    orb.setFpsCap(0);
    demoBefore = null;
    if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
  }
  map?.setDemo(on);
  const drift = $('view-demo');
  drift?.setAttribute('aria-pressed', String(on));
  if (on) map?.home();
}
demoButton.addEventListener('click', () => setDemo(demoButton.getAttribute('aria-pressed') !== 'true'));
document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement && demoBefore) setDemo(false); });

const settings = $('settings');
$('settings-open').addEventListener('click', () => {
  $('set-mute').checked = voice.muted;
  $('set-narrate').checked = voice.narration;
  $('set-lite').checked = graphicsLite();
  $('voice-name').textContent = voice.supportsOut ? voice.voiceName : 'none (this browser has no speech output)';
  $('voice-in').textContent = voice.supportsIn ? 'available' : 'not available in this browser (typing works)';
  settings.show();   // not modal: STAND DOWN stays reachable while it's open
});
$('set-mute').addEventListener('change', (e) => { voice.muted = e.target.checked; showMute(); });
$('set-narrate').addEventListener('change', (e) => { voice.narration = e.target.checked; });
$('set-lite').addEventListener('change', (e) => {
  try { localStorage.setItem('hq.lite', e.target.checked ? '1' : '0'); } catch { /* private window: this visit only */ }
  map?.setLite(e.target.checked);
});
$('settings-close').addEventListener('click', () => settings.close());

// ---------------- start ----------------

function connection(state) {
  const note = $('connection');
  note.hidden = state === 'live';
  note.dataset.state = state;
  note.textContent = state === 'expired'
    ? 'HQ restarted with a new link. Open the new link printed in the terminal (or run town hq again).'
    : 'Connection lost. Reconnecting; messages that arrive meanwhile will catch up.';
  if (state === 'live') refreshFacts();
}

async function boot() {
  let layout;
  let data;
  let village;
  try {
    // A busy moment (say, the database briefly locked) shouldn't leave a blank page: try a few times.
    for (let attempt = 1; ; attempt += 1) {
      try {
        [layout, data, village] = await Promise.all([get('/api/layout'), get('/api/facts'), get('/api/village')]);
        break;
      } catch (err) {
        if (attempt >= 4 || (err instanceof HttpError && err.status === 401)) throw err;
        await new Promise((r) => { setTimeout(r, 1500 * attempt); });
      }
    }
  } catch (err) {
    const expired = err instanceof HttpError && err.status === 401;
    connection(expired ? 'expired' : 'reconnecting');
    $('connection').textContent = expired
      ? 'This link has expired. Open the new link printed in the terminal (or run town hq again).'
      : `Couldn't load the Town (${err.message}). Is town hq still running?`;
    return;
  }
  status.canDecide = data.can_decide !== false;
  $('map-loading').hidden = false;
  map = createVillage(dom.map, layout, {
    base: BASE,
    lite: graphicsLite(),
    onReady: () => {
      $('map-loading').hidden = true;
      panels.placePlates(map.buildingTops());
      setTimeout(() => { window.HQ.graphics = map.graphicsName; }, 0);   // for the error box's details
    },
    onError: (err) => window.HQCrash?.report('the village failed', err?.message || String(err), err?.stack),
    onGraphicsLost: () => graphicsLost(),
    onGraphicsBack: () => graphicsBack(),
    onSlow: (why, detail) => slowNotice(why, detail),
  });
  bindVillageEvents(map, {
    onBuildingClick: (agent) => { panels.openPanel(agent); frameBeside(agent); },
    onMoved: () => panels.placePlates(map.buildingTops()),
    onFlowStart: (flow) => {
      panels.feed(flow);
      panels.bubble(flow);
      voice.narrate(flow, status.stopped, panels.name(flow.from_node));
      debug.started.push(flow);
      if (flow.to_node === 'charlie' || flow.from_node === 'charlie') {
        const column = $('reaper');
        column.classList.remove('is-pinged');
        void column.offsetWidth;   // restart the highlight
        column.classList.add('is-pinged');
      }
    },
    onFlowDone: (flow, visited) => {
      panels.bubbleDone(flow);
      debug.completed.push({ flow, visited });
      if (debug.completed.length > 100) debug.completed.shift();
    },
  });
  window.HQ.map = map;
  const keepClear = () => map.setInset(dom.board.offsetWidth + 12);
  keepClear();
  new ResizeObserver(keepClear).observe(dom.board);
  setTimeout(() => {   // the map didn't draw: say so (chat, cards and STAND DOWN still work without it)
    if (map.ready) return;
    const note = $('connection');
    note.hidden = false;
    note.dataset.state = 'expired';
    note.textContent = "The village map didn't load. Chat, cards and STAND DOWN still work; reload the page to try again.";
  }, 10000);
  applyFacts(data.facts);
  panels.setVillage(village);
  needs.refresh();
  // Catch me up: after a long enough time away, Reaper briefs Charlie once, as the page opens (plain code decides).
  post('/api/catch-up', { auto: true }).then((d) => {
    if (d?.briefing) { chat.say(d.briefing, 'Catch me up'); voice?.speak(d.briefing.split('\n')[0]); }
  }).catch(() => { /* no briefing this time; "what's new?" in the chat asks for one */ });
  map.setRedFlag(village.gatehouse.red_flag);
  data.flows.slice(-4).forEach((f) => panels.feed(f, { live: false }));
  window.addEventListener('resize', () => panels.placePlates(map.buildingTops()));
  $('view-home').addEventListener('click', () => map.home());
  const demo = $('view-demo');
  demo.addEventListener('click', () => {
    const on = demo.getAttribute('aria-pressed') !== 'true';
    demo.setAttribute('aria-pressed', String(on));
    map.setDemo(on);   // a slow drift round the town; messages still only come from the real log
  });
  openStream({
    after: data.last_event_id,
    onFlow: (flow) => { map.play(flow); refreshVillageSoon(); },
    onFacts: applyFacts,
    onStatus: connection,
  });
}

boot();
