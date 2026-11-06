// The page around the map: the top bar, building name plates, the building panel, the feed and the scoreboard.
// Every number comes from /api/facts or /api/village (plain code on the server); nothing is estimated here.
// Things the Town doesn't track yet say so, never a made-up zero. All text goes in with textContent.
import { get } from './api.js';

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
const STATE_WORDS = { idle: 'Idle', working: 'Working', waiting_for_approval: 'Waiting on you', done: 'Done',
                      error: 'Error' };
const money = (v) => (v === null || v === undefined ? '?' : `$${Number(v).toFixed(2)}`);
const words = (s) => String(s || '').replace(/_/g, ' ');

export function localTime(ts) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return d.toDateString() === new Date().toDateString() ? time
    : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`;
}

export function createPanels({ dom, onAsk, onFrame }) {
  let facts = null;
  let village = null;
  let titles = { charlie: 'You' };
  const plates = new Map();
  const spots = new Map();     // agent -> where its plate stands (for the speech bubbles)
  const bubbles = new Map();   // agent -> {node, timer}
  let openAgent = null;

  const name = (node) => titles[node] || words(node);

  // ---------------- top bar ----------------

  function setFacts(next) {
    facts = next;
    for (const a of next.agents) titles[a.agent] = a.title.replace(/ \(Reaper\)$/, '');
    titles = { ...titles, mayor: 'Town Hall', jarvis: 'Reaper', charlie: 'You' };
    const stopped = next.stopped;
    dom.townState.textContent = stopped ? 'STOOD DOWN' : 'RUNNING';
    dom.townState.className = `town-state ${stopped ? 'is-stopped' : 'is-running'}`;
    dom.nowLine.textContent = nowLine(next);
    const s = next.spend;
    if (s.readable) {
      meter(dom.apiMeter, 'API', s.api_usd, s.api_cap_usd, s.api_warning);
      meter(dom.totalMeter, 'Total', s.total_usd, s.total_cap_usd, s.total_warning);
      dom.paused.hidden = !s.paused;
      dom.paused.textContent = s.paused ? `Paid work paused: ${s.paused}` : '';
    } else {
      dom.apiMeter.querySelector('.meter-value').textContent = 'unreadable';
      dom.paused.hidden = false;
      dom.paused.textContent = 'Spend unreadable: paid work counts as paused.';
    }
    dom.banner.hidden = !stopped;
    updatePlates();
    renderBoard();
    if (openAgent) refreshPanelState();
  }

  function meter(node, label, value, cap, warn) {
    const share = cap ? Math.min(1, value / cap) : 0;
    node.querySelector('.meter-value').textContent = `${money(value)} of ${money(cap)}`;
    node.querySelector('.meter-fill').style.width = `${(share * 100).toFixed(1)}%`;
    node.classList.toggle('is-warning', Boolean(warn));
    node.setAttribute('aria-label', `${label} spend this month ${money(value)} of ${money(cap)}`
      + (warn ? ', over 80 percent' : ''));
  }

  // One plain sentence: what the Town is doing right now.
  function nowLine(f) {
    if (f.stopped) return 'Everything has stopped. To start again, type town resume in a terminal.';
    const working = f.agents.filter((a) => a.state === 'working');
    const errors = f.agents.filter((a) => a.state === 'error');
    const parts = working.slice(0, 2).map((a) => `${name(a.agent)}: ${a.task || 'working'}.`);
    if (working.length > 2) parts.push(`${working.length - 2} more at work.`);
    const q = f.queue.pending;
    if (q) parts.push(`${q} waiting for your decision${f.queue.blocked ? ` (${f.queue.blocked} blocked)` : ''}.`);
    if (errors.length) parts.push(`${errors.map((a) => name(a.agent)).join(', ')} reported an error.`);
    return parts.length ? parts.join(' ') : 'All quiet. Nothing waiting for you.';
  }

  // ---------------- name plates over the buildings ----------------

  function setVillage(next) {
    village = next;
    updatePlates();
    renderBoard();
    renderScoreboard();
    if (openAgent) refreshPanelState();
  }

  // Called on every frame the camera moves, so it only moves the plates: their text is written when the facts
  // change (updatePlates), and their sizes are measured then and kept, so nothing here forces the browser to
  // lay the page out again mid-frame.
  function placePlates(tops) {
    const seen = new Set();
    let added = false;
    for (const t of tops) {
      seen.add(t.agent);
      if (!plates.has(t.agent)) {
        added = true;
        const plate = el('button', 'plate');
        plate.type = 'button';
        plate.dataset.agent = t.agent;
        plate.append(el('span', 'plate-name', t.info.building), el('span', 'plate-state'), el('span', 'plate-extra'));
        plate.addEventListener('click', () => openPanel(t.agent));
        dom.plates.append(plate);
        plates.set(t.agent, plate);
      }
    }
    for (const [agent, plate] of plates) if (!seen.has(agent)) { plate.remove(); plates.delete(agent); }
    if (added) updatePlates();   // a new plate's text first, so its size is known
    lastTops = tops;
    arrange(tops);
  }
  let lastTops = [];

  // The layout of the fixed boxes (the map's area, the feed, the board, the view buttons, the panel): read once
  // and kept until the window or the panel changes.
  let frameRects = null;
  function rects() {
    if (frameRects) return frameRects;
    const area = dom.plates.getBoundingClientRect();
    const rel = (r) => ({ x: r.left - area.left, y: r.top - area.top, w: r.width, h: r.height });
    frameRects = {
      area,
      blocked: [dom.feed.parentElement, dom.board, dom.viewTools].filter(Boolean).map((n) => rel(n.getBoundingClientRect())),
      panel: dom.panel.hidden ? null : rel(dom.panel.getBoundingClientRect()),
      board: dom.board ? rel(dom.board.getBoundingClientRect()) : null,
    };
    return frameRects;
  }
  window.addEventListener('resize', () => { frameRects = null; });
  new ResizeObserver(() => { frameRects = null; }).observe(dom.board || dom.plates);

  // Each plate goes where it covers the least: above its building, else beside it or below it, avoiding the
  // other plates, the other buildings and the message feed. So no name hides another building.
  function arrange(tops) {
    const { area, blocked } = rects();
    const overlap = (a, b) => Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x))
      * Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
    const placed = [];
    for (const t of tops) {
      const plate = plates.get(t.agent);
      const w = plate._w || (plate._w = plate.offsetWidth);
      const h = plate._h || (plate._h = plate.offsetHeight);
      const b = t.box;
      const options = [
        { x: t.x - w / 2, y: t.y - h - 2 },                       // above
        { x: b.x + b.w + 4, y: b.y + b.h * 0.3 - h / 2 },         // right
        { x: b.x - w - 4, y: b.y + b.h * 0.3 - h / 2 },           // left
        { x: t.x - w / 2, y: b.y + b.h - 6 },                     // below
      ];
      let best = null;
      for (const [i, o] of options.entries()) {
        const r = { x: o.x, y: o.y, w, h };
        let cost = i * 40;                                        // prefer the earlier spots
        for (const other of tops) if (other.agent !== t.agent) cost += overlap(r, other.box) * 0.6;
        for (const p of [...placed, ...blocked]) cost += overlap(r, p) * 2;
        const outside = Math.max(0, -r.x) + Math.max(0, r.x + w - area.width) + Math.max(0, -r.y)
          + Math.max(0, r.y + h - area.height);
        cost += outside * h * 3;
        if (!best || cost < best.cost) best = { ...r, cost };
      }
      placed.push(best);
      const move = `translate(${Math.round(best.x)}px, ${Math.round(best.y)}px)`;
      if (plate._move !== move) { plate.style.transform = move; plate._move = move; }
      if (plate._behind !== Boolean(t.behind)) { plate.classList.toggle('is-behind', Boolean(t.behind)); plate._behind = Boolean(t.behind); }
      spots.set(t.agent, best);
    }
    hideUnderPanel();
    placeBubbles();
  }

  function updatePlates() {
    if (!facts) return;
    const states = Object.fromEntries(facts.agents.map((a) => [a.agent, a]));
    for (const [agent, plate] of plates) {
      const a = states[agent];
      plate.dataset.state = facts.stopped ? 'stopped' : (a?.state || 'idle');
      const chip = plate.querySelector('.plate-state');
      chip.textContent = `${name(agent)}: ${facts.stopped ? 'halted' : STATE_WORDS[a?.state] || 'Idle'}`;
      const extra = extraFor(agent);
      plate.querySelector('.plate-extra').textContent = extra;
      plate.querySelector('.plate-extra').hidden = !extra;
      plate.title = a?.task || '';   // the current task on hover; the visible text is the button's name
      plate._w = plate._h = 0;      // measured again on the next placement (the text may have changed size)
    }
    // A plain list for screen readers (the map itself is a picture).
    dom.agentList.replaceChildren(...facts.agents.map((a) => {
      const li = el('li', null, `${a.building}: ${a.title}, ${STATE_WORDS[a.state] || a.state}`
        + (a.task ? ` (${a.task})` : ''));
      li.dataset.agent = a.agent;
      li.dataset.state = a.state;
      return li;
    }));
  }

  function extraFor(agent) {
    if (!facts) return '';
    if (agent === 'mayor') return facts.queue.pending ? `${facts.queue.pending} waiting on you` : 'Queue empty';
    if (!village) return '';
    if (agent === 'treasurer') return `${money(village.bank.total_usd)} of ${money(village.bank.total_cap_usd)}`;
    if (agent === 'librarian') {
      const iq = village.library;
      return iq.first_try_approval_rate === null ? 'Town IQ: no decisions yet'
        : `Town IQ ${Math.round(iq.first_try_approval_rate * 100)}% of ${iq.decided}`;
    }
    if (agent === 'guard' && village.gatehouse.red_flag) return `Red flag: ${village.gatehouse.blocked_now.length} blocked`;
    const extras = village.extras || {};
    if (agent === 'messenger' && extras.golive) return extras.golive.sending ? 'Sending: ON' : 'Sending: off (drafts)';
    if (agent === 'clerk' && extras.mailroom) {
      const n = extras.mailroom.waiting.length;
      return n ? `${n} repl${n === 1 ? 'y' : 'ies'} for you` : 'No replies waiting';
    }
    return '';
  }

  // ---------------- the building panel ----------------

  // Plates the open panel would half cover are hidden until it closes.
  // From the plates' known spots (no measuring after they've moved).
  function hideUnderPanel() {
    const { panel } = rects();
    for (const [agent, plate] of plates) {
      const r = spots.get(agent);
      const under = Boolean(panel && r) && r.x + r.w > panel.x && r.x < panel.x + panel.w && r.y + r.h > panel.y
        && r.y < panel.y + panel.h;
      if (plate._under !== under) { plate.classList.toggle('is-under-panel', under); plate._under = under; }
    }
  }

  async function openPanel(agent) {
    openAgent = agent;
    dom.panel.hidden = false;
    frameRects = null;
    hideUnderPanel();
    dom.panelBody.replaceChildren(el('p', 'panel-loading', 'Loading.'));
    dom.panelTitle.textContent = plates.get(agent)?.querySelector('.plate-name').textContent || name(agent);
    dom.panelClose.focus();
    try {
      const detail = await get(`/api/agent/${encodeURIComponent(agent)}`);
      if (openAgent === agent) renderPanel(detail);
    } catch (err) {
      dom.panelBody.replaceChildren(el('p', 'card-error', `Couldn't load this building: ${err.message}`));
    }
  }

  function closePanel() {
    const agent = openAgent;
    openAgent = null;
    dom.panel.hidden = true;
    frameRects = null;
    hideUnderPanel();
    plates.get(agent)?.focus();
  }

  function refreshPanelState() {
    // Keep the open panel current when the stream sends new facts (state, task) without refetching.
    const a = facts?.agents.find((x) => x.agent === openAgent);
    const node = dom.panelBody.querySelector('.panel-state');
    if (a && node) {
      node.dataset.state = a.state;
      node.textContent = `${STATE_WORDS[a.state] || a.state}${a.task ? `: ${a.task}` : ''}`;
    }
  }

  function renderPanel(d) {
    const body = [];
    body.push(el('p', 'panel-agent', d.title));
    const state = el('p', 'panel-state', `${STATE_WORDS[d.state] || d.state}${d.task ? `: ${d.task}` : ''}`);
    state.dataset.state = d.state;
    body.push(state);
    body.push(...extraSection(d.agent));
    const ask = el('button', 'btn btn-link panel-ask', 'Ask Reaper about this');
    ask.type = 'button';
    const who = d.title.replace(/ \(Reaper\)$/, '');
    ask.addEventListener('click', () => onAsk(`What is the ${who} doing right now, and why?`));
    body.push(ask);
    body.push(el('h3', 'panel-h', 'Last 10 events'));
    if (d.recent_events.length) {
      const list = el('ol', 'panel-events');
      for (const e of [...d.recent_events].reverse()) {
        const li = el('li');
        li.append(el('span', 'ev-time', localTime(e.ts)), el('span', 'ev-type', words(e.event_type)),
                  el('span', 'ev-detail', eventDetail(e)));
        list.append(li);
      }
      body.push(list);
    } else {
      body.push(el('p', 'panel-empty', 'No events yet.'));
    }
    body.push(el('h3', 'panel-h', 'Playbook'));
    body.push(d.playbook && d.playbook.trim() ? el('pre', 'panel-playbook', d.playbook)
      : el('p', 'panel-empty', 'No playbook written yet. The Librarian adds lessons in Phase 4.'));
    dom.panelBody.replaceChildren(...body);
  }

  function extraSection(agent) {
    const out = [];
    if (agent === 'treasurer' && village) {
      const b = village.bank;
      out.push(el('h3', 'panel-h', 'Bank'));
      const dl = el('dl', 'panel-facts');
      const row = (k, v) => dl.append(el('dt', null, k), el('dd', null, v));
      row('API this month', `${money(b.api_usd)} of ${money(b.api_cap_usd)}${b.api_warning ? ' (over 80%)' : ''}`);
      row('All paid spend', `${money(b.total_usd)} of ${money(b.total_cap_usd)}${b.total_warning ? ' (over 80%)' : ''}`);
      row('Revenue', notTracked('revenue_usd') ? 'not tracked yet' : money(b.revenue_usd));
      row('Monthly recurring', notTracked('mrr_usd') ? 'not tracked yet' : money(b.mrr_usd));
      if (b.paused) row('Paid work', `paused: ${b.paused}`);
      out.push(dl);
    }
    if (agent === 'librarian' && village) {
      const iq = village.library;
      out.push(el('h3', 'panel-h', 'Town IQ'));
      if (iq.first_try_approval_rate === null) {
        out.push(el('p', 'panel-empty', 'No decided proposals yet, so no score.'));
      } else {
        const pct = Math.round(iq.first_try_approval_rate * 100);
        const bar = el('div', 'iq-bar');
        bar.setAttribute('role', 'meter');
        bar.setAttribute('aria-valuemin', '0');
        bar.setAttribute('aria-valuemax', '100');
        bar.setAttribute('aria-valuenow', String(pct));
        bar.setAttribute('aria-label', 'Town IQ, first-try approval rate');
        const fill = el('span', 'iq-fill');
        fill.style.width = `${pct}%`;
        bar.append(fill);
        out.push(bar, el('p', 'panel-note', `${pct}% approved on the first try: ${iq.first_try} of ${iq.decided} `
          + `decided proposals (${iq.edited} edited, ${iq.rejected} rejected).`));
      }
      // The Librarian's lessons (BLUEPRINT 5.2): each with the evidence behind it. Proposed ones wait in the queue.
      out.push(el('h3', 'panel-h', 'Lessons'));
      const lessons = iq.lessons || [];
      if (!lessons.length) {
        out.push(el('p', 'panel-empty', 'No lessons yet. `town librarian review` proposes them from your decisions.'));
      }
      for (const l of lessons) {
        const item = el('div', 'lesson');
        item.append(el('p', 'lesson-text', `#${l.id} (${words(l.agent)}): ${l.text}`));
        const state = l.status === 'approved' ? 'approved' : `proposed, waiting for you as #${l.action_id}`;
        const ids = (l.evidence_ids || []).join(', ');
        item.append(el('p', 'lesson-meta', `${state}. Evidence: ${l.count} of ${l.out_of}${ids ? ` (${ids})` : ''}.`));
        out.push(item);
      }
    }
    if (agent === 'guard' && village) {
      const g = village.gatehouse;
      out.push(el('h3', 'panel-h', 'Blocked now'));
      if (!g.blocked_now.length) out.push(el('p', 'panel-empty', 'Nothing blocked.'));
      for (const b of g.blocked_now) {
        out.push(el('p', 'panel-block', `#${b.id} ${words(b.action_type)}: ${b.target}. ${b.blocks.join('; ')}`));
      }
      out.push(el('p', 'panel-note', `${g.blocks_last_24h} block(s) in the last 24 hours.`));
    }
    const extras = (village && village.extras) || {};
    // The Post Office: the go-live checklist, read-only here. Switching sending on or off is `town golive` in the
    // terminal (Charlie only), never a button on this page.
    if (agent === 'messenger' && extras.golive) {
      const g = extras.golive;
      out.push(el('h3', 'panel-h', 'Go-live checklist'));
      const list = el('ul', 'panel-checklist');
      for (const item of g.checklist) {
        const li = el('li', item.ok ? 'check-ok' : 'check-no', `${item.ok ? 'OK' : 'NO'}  ${item.line}`);
        li.dataset.ok = String(item.ok);
        list.append(li);
      }
      out.push(list);
      out.push(el('p', 'panel-note', g.sending ? 'Real sending is ON. `town golive --off` in the terminal turns it off.'
        : 'Read-only here. Charlie switches sending on with `town golive` in the terminal.'));
    }
    // The Mailroom: replies that need Charlie (the Clerk sorted them; the Town never replies).
    if (agent === 'clerk' && extras.mailroom) {
      out.push(el('h3', 'panel-h', 'Replies waiting for you'));
      const waiting = extras.mailroom.waiting;
      if (!waiting.length) out.push(el('p', 'panel-empty', 'No replies waiting. `town inbox` checks for new ones.'));
      for (const r of waiting) {
        out.push(el('p', 'panel-queue', `#${r.queue_action_id} ${r.business_name || r.from_address}: ${words(r.kind)}`
          + (r.kind === 'call_request' ? ` (call script: town draft ${r.lead_id})` : '')));
      }
    }
    // Autopilot, read-only here: starting and stopping it stays `town autopilot` in the terminal.
    if (agent === 'mayor' && extras.autopilot) {
      const a = extras.autopilot;
      out.push(el('h3', 'panel-h', 'Autopilot'));
      out.push(el('p', 'panel-note', `Autopilot: ${a.on ? 'on' : 'off'}, last cycle ${a.last_cycle ? localTime(a.last_cycle.ts) : 'none yet'}`
        + `${a.next_cycle_at ? `, next cycle ${localTime(a.next_cycle_at)}` : ''}, waiting for you: ${a.waiting_for_you}.`));
      if (a.last_cycle && a.last_cycle.line) out.push(el('p', 'panel-queue', a.last_cycle.line));
    }
    if (agent === 'mayor' && facts) {
      out.push(el('h3', 'panel-h', 'Waiting for your decision'));
      if (!facts.queue.items.length) out.push(el('p', 'panel-empty', 'Queue empty.'));
      for (const i of facts.queue.items) {
        out.push(el('p', i.blocked ? 'panel-block' : 'panel-queue',
                    `#${i.id} ${words(i.action_type)} from ${name(i.agent)}: ${i.target}${i.blocked ? ' (BLOCKED)' : ''}`));
      }
    }
    return out;
  }

  function eventDetail(e) {
    const d = e.details || {};
    const bits = [];
    if (e.action_id) bits.push(`#${e.action_id}`);
    for (const k of ['message', 'verdict', 'reason', 'business_type', 'area', 'lead_id', 'build', 'kind', 'order']) {
      if (d[k] !== undefined && d[k] !== null && typeof d[k] !== 'object') bits.push(`${words(k)} ${d[k]}`);
    }
    return bits.join(', ').slice(0, 160);
  }

  // ---------------- the scoreboard ----------------

  function notTracked(key) {
    return Boolean(village?.scoreboard.not_tracked_yet?.includes(key));
  }

  function renderScoreboard() {
    if (!village) return;
    const s = village.scoreboard;
    const items = [['Leads', s.leads], ['Inspected', s.inspected], ['Built', s.built],
                   ['Critic-passed', s.critic_passed], ['Previews live', s.previews_live],
                   ['Contacted', s.contacted, 'contacted'], ['Replies', s.replies, 'replies'],
                   ['Clients', s.clients, 'clients'], ['Care plans', s.care_plan_subscribers, 'care_plan_subscribers'],
                   ['Revenue', money(s.revenue_usd), 'revenue_usd'], ['Cost this month', money(s.cost_this_month_usd)]];
    dom.scoreboard.replaceChildren(...items.map(([label, value, key]) => {
      const item = el('div', 'score');
      const off = Boolean(key) && notTracked(key);
      item.classList.toggle('is-untracked', off);
      item.append(el('span', 'score-value', off ? 'not tracked yet' : value), el('span', 'score-label', label));
      if (key === 'contacted' && s.notes?.contacted) item.title = s.notes.contacted;
      return item;
    }));
  }

  // ---------------- the side board: live counts and every agent ----------------

  const DOTS = { idle: 'Idle', working: 'Working', waiting_for_approval: 'Waiting on you', done: 'Done', error: 'Error' };

  function renderBoard() {
    if (!facts || !dom.board) return;
    const working = facts.agents.filter((a) => a.state === 'working').length;
    dom.countWorking.textContent = facts.stopped ? '0' : String(working);
    dom.countWaiting.textContent = String(facts.queue.pending);
    dom.countBlocked.textContent = String(facts.queue.blocked);
    dom.countDone.textContent = village && village.done_today !== undefined ? String(village.done_today) : '?';
    const rows = facts.agents.map((a) => {
      const li = el('li');
      const button = el('button', 'board-agent');
      button.type = 'button';
      button.dataset.agent = a.agent;
      const state = facts.stopped ? 'stopped' : a.state;
      button.dataset.state = state;
      const dot = el('span', 'dot');
      dot.setAttribute('aria-hidden', 'true');
      button.append(dot, el('span', 'board-name', a.building),
                    el('span', 'board-state', facts.stopped ? 'Halted' : DOTS[a.state] || a.state));
      button.title = a.task || '';
      button.addEventListener('click', () => { openPanel(a.agent); onFrame?.(a.agent); });
      li.append(button);
      return li;
    });
    dom.boardAgents.replaceChildren(...rows);
  }

  // ---------------- speech bubbles over the sender ----------------

  // A short plain sentence over the building that sent a message ("Builder: the site for Oak Lane Florist is
  // ready"). One bubble per building; a newer message replaces the older one. Text only, never HTML.
  function bubble(flow) {
    const from = flow.from_node;
    const anchor = plates.has(from) ? from : from === 'charlie' ? 'jarvis' : plates.has('guard') ? 'guard' : null;
    if (!anchor || !dom.bubbles) return;
    const old = bubbles.get(anchor);
    if (old) { clearTimeout(old.timer); old.node.remove(); }
    const node = el('p', `bubble kind-${flow.kind}`);
    node.append(el('strong', 'bubble-who', `${name(from)}: `), el('span', null, flow.says || flow.label));
    dom.bubbles.append(node);
    // It stays while the message is on its way (bubbleDone() starts the fade once it arrives), at most 30 s.
    const entry = { node, flow: flow.event_id, timer: setTimeout(() => leave(anchor, entry), 30000) };
    bubbles.set(anchor, entry);
    placeBubbles();
  }

  function bubbleDone(flow) {
    for (const [anchor, entry] of bubbles) {
      if (entry.flow !== flow.event_id) continue;
      clearTimeout(entry.timer);
      entry.timer = setTimeout(() => leave(anchor, entry), 2500);
    }
  }

  function leave(anchor, entry) {
    entry.node.classList.add('is-leaving');
    setTimeout(() => { entry.node.remove(); if (bubbles.get(anchor) === entry) bubbles.delete(anchor); }, 400);
  }

  function placeBubbles() {
    if (!bubbles.size) return;
    const { area, board } = rects();
    for (const [agent, entry] of bubbles) {
      const { node } = entry;
      const spot = spots.get(agent);
      const hide = !spot || plates.get(agent)?._behind;
      if (node.hidden !== Boolean(hide)) node.hidden = Boolean(hide);
      if (hide) continue;
      const w = entry.w || (entry.w = node.offsetWidth);       // measured once, when it first shows
      const h = entry.h || (entry.h = node.offsetHeight);
      let x = Math.max(4, Math.min(area.width - w - 4, spot.x + spot.w / 2 - w / 2));
      const y = Math.max(4, spot.y - h - 8);
      // Never under the side board: slide right of it when it would overlap.
      if (board && y < board.y + board.h && x < board.x + board.w + 6) x = board.x + board.w + 6;
      const move = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
      if (entry.move !== move) { node.style.transform = move; entry.move = move; }
    }
  }

  // ---------------- the activity feed and hover labels ----------------

  function feed(flow, { live = true } = {}) {
    const li = el('li', `feed-item kind-${flow.kind}`);
    li.append(el('span', 'feed-time', localTime(flow.ts)),
              el('span', 'feed-route', `${name(flow.from_node)} to ${name(flow.to_node)}`),
              el('span', 'feed-label', flow.label));
    if (live) li.classList.add('is-new');
    dom.feed.prepend(li);
    while (dom.feed.children.length > 4) dom.feed.lastChild.remove();
  }

  function hover(flow, x, y) {
    if (!flow) { dom.tip.hidden = true; return; }
    dom.tip.textContent = `${name(flow.from_node)} to ${name(flow.to_node)}: ${flow.label}`;
    dom.tip.style.transform = `translate(${Math.round(x + 14)}px, ${Math.round(y - 10)}px)`;
    dom.tip.hidden = false;
  }

  dom.panelClose.addEventListener('click', closePanel);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && openAgent) closePanel(); });

  return { setFacts, setVillage, placePlates, openPanel, feed, hover, bubble, bubbleDone, name, get facts() { return facts; } };
}
