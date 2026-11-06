// The chat with Reaper, and the confirmation cards.
//
// Every message goes to /api/chat (one Reaper turn, or plain-code shortcuts, or stand down). A card is built
// only from the server's validated proposal (`proposal.cards`, plain code), never from Reaper's words. Only the
// Confirm button acts: it sends a separate /api/confirm request, which the server checks again. Typing "yes"
// is just another chat message. Everything from the server is shown with textContent, never as HTML.
import { HttpError, post } from './api.js';

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
const words = (s) => String(s || '').replace(/_/g, ' ');
// A Reaper turn with tool rounds can take a while on a slow laptop; past this the page stops waiting.
const CHAT_TIMEOUT_MS = 120000;
const BAD_RESULT = /FAILED|not executed|not decided|not edited|not started|didn't|nothing done|refused|held/i;

export function createChat({ log, form, input, onReply, onBusy, canDecide }) {
  const cards = new Map();     // proposal id -> {node, proposal, confirm, note}
  let stopped = false;
  let pending = 0;

  function scroll() { log.scrollTop = log.scrollHeight; }

  function bubble(who, text, meta) {
    const item = el('div', `msg msg-${who}`);
    item.append(el('p', 'msg-who', who === 'charlie' ? 'You' : 'Reaper'));
    item.append(el('p', 'msg-text', text));
    if (meta) item.append(el('p', 'msg-meta', meta));
    log.append(item);
    scroll();
    return item;
  }

  async function send(text, { spoken = false } = {}) {
    const message = String(text || '').trim();
    if (!message) return;
    bubble('charlie', message, spoken ? 'spoken' : null);
    const waiting = bubble('reaper', 'Working on it.', null);
    waiting.classList.add('msg-pending');
    pending += 1;
    onBusy?.(true);
    try {
      // A reply always comes: the server answers every message, and past CHAT_TIMEOUT_MS the page stops waiting.
      const d = await post('/api/chat', { message: message.slice(0, 2000) }, { timeoutMs: CHAT_TIMEOUT_MS });
      waiting.remove();
      let reply = d.reply || 'No reply.';
      if (d.proposal) reply = withoutConfirmation(reply, d.proposal);
      const meta = d.mode === 'model' ? `${d.seconds} s, $${Number(d.cost_usd).toFixed(4)}`
        : { plain: 'plain code, free', stand_down: 'plain code', stopped: 'stood down', cap: 'turn cap reached',
            fallback: 'model unavailable' }[d.mode] || null;
      const answer = bubble('reaper', reply, meta);
      if (d.mode !== 'stand_down') answer.append(thumbs(reply));
      if (d.mode === 'stand_down') withdrawAll('The Town stood down. This list was withdrawn; nothing was done.');
      if (d.proposal) showCard(d.proposal);
      onReply?.(d, reply);
    } catch (err) {
      waiting.remove();
      const why = err instanceof HttpError && err.status === 401
        ? 'This link has expired (HQ restarted). Open the new link from the terminal.'
        : err instanceof HttpError && err.status === 0
          ? 'No answer from Reaper in time. Nothing was done; ask again.'
          : `Couldn't reach Reaper: ${err.message}`;
      bubble('reaper', why, null).classList.add('msg-error');
    } finally {
      pending -= 1;
      onBusy?.(pending > 0);
    }
  }

  // Thumbs on a reply (the operator profile, engine/profile.py). A thumb on its own is only recorded. A
  // thumbs-down offers explicit choices ("Shorter"...): picking one files a proposal in the queue, and nothing
  // about how Reaper briefs Charlie changes until he approves it there.
  const CHANGES = [['shorter', 'Shorter'], ['more_detail', 'More detail'], ['skip_costs', 'Skip the costs'],
                   ['lead_waiting', "Lead with what's waiting"]];
  function thumbs(reply) {
    const row = el('div', 'msg-rate');
    const note = el('span', 'msg-rate-note');
    note.setAttribute('role', 'status');
    const rate = async (rating, change, buttons) => {
      buttons.forEach((b) => { b.disabled = true; });
      try {
        const d = await post('/api/feedback', { about: 'reaper reply', rating, change: change || null,
                                                excerpt: String(reply).slice(0, 300) });
        note.textContent = d.message || 'Noted.';
      } catch (err) {
        note.textContent = `Not saved: ${err.message}`;
        buttons.forEach((b) => { b.disabled = false; });
      }
    };
    const up = el('button', 'msg-thumb', '\u{1F44D}');
    const down = el('button', 'msg-thumb', '\u{1F44E}');
    up.type = down.type = 'button';
    up.setAttribute('aria-label', 'Good reply');
    down.setAttribute('aria-label', 'Not helpful: choose what to change');
    up.addEventListener('click', () => rate(1, null, [up, down]));
    down.addEventListener('click', () => {
      up.disabled = down.disabled = true;
      const choices = el('span', 'msg-rate-choices');
      const buttons = CHANGES.map(([key, label]) => {
        const b = el('button', 'msg-chip', label);
        b.type = 'button';
        b.addEventListener('click', () => rate(-1, key, [...buttons, skip]));
        return b;
      });
      const skip = el('button', 'msg-chip', 'Just noting it');
      skip.type = 'button';
      skip.addEventListener('click', () => rate(-1, null, [...buttons, skip]));
      choices.append(...buttons, skip);
      row.insertBefore(choices, note);
    });
    row.append(up, down, note);
    return row;
  }

  // The reply ends with R1's confirmation text; the card shows the same items, so the text isn't repeated.
  function withoutConfirmation(reply, proposal) {
    const n = proposal.cards.length;
    const at = reply.lastIndexOf(`Please confirm ${n} item${n === 1 ? '' : 's'}:`);
    const cut = at >= 0 ? reply.slice(0, at).trim() : reply;
    return cut || `Confirm ${n} item${n === 1 ? '' : 's'}: see the card below.`;
  }

  function showCard(proposal, replacing) {
    const node = el('article', 'card');
    node.dataset.proposal = proposal.id;
    node.setAttribute('aria-label', 'Waiting for your confirmation');
    node.append(el('h3', 'card-title', 'Waiting for your confirmation'));
    node.append(el('p', 'card-sub', 'Nothing happens until you press Confirm.'));
    const list = el('ol', 'card-items');
    for (const item of proposal.cards) list.append(cardItem(proposal, item));
    node.append(list);
    if (proposal.refused?.length) {
      node.append(el('p', 'card-refused-title', 'Not included (refused by the checks):'));
      const refused = el('ul', 'card-refused');
      proposal.refused.forEach((r) => refused.append(el('li', null, r)));
      node.append(refused);
    }
    const actions = el('div', 'card-actions');
    const n = proposal.cards.length;
    const confirm = el('button', 'btn btn-confirm', `Confirm ${n} item${n === 1 ? '' : 's'}`);
    confirm.type = 'button';
    const cancel = el('button', 'btn btn-quiet', 'Cancel');
    cancel.type = 'button';
    actions.append(confirm, cancel);
    node.append(actions);
    const note = el('p', 'card-note');
    note.setAttribute('aria-live', 'polite');
    node.append(note);
    confirm.addEventListener('click', () => doConfirm(proposal));
    cancel.addEventListener('click', () => doCancel(proposal));
    if (replacing) replacing.replaceWith(node); else log.append(node);
    cards.set(proposal.id, { node, proposal, confirm, note });
    refresh(proposal.id);
    scroll();
  }

  function cardItem(proposal, item) {
    const li = el('li', `card-item card-${item.kind}`);
    if (item.kind === 'decision') {
      const head = el('p', 'item-head');
      head.append(el('span', `verb verb-${item.decision}`, item.decision === 'approve' ? 'Approve' : 'Reject'),
                  el('span', 'item-id', `#${item.action_id}`), el('span', 'item-type', words(item.action_type)),
                  el('span', 'item-agent', `from ${words(item.agent)}`));
      li.append(head, el('p', 'item-target', item.target));
      if (item.decision === 'reject') li.append(reasonBlock(proposal, item));
    } else if (item.kind === 'edit') {
      const head = el('p', 'item-head');
      head.append(el('span', 'verb verb-edit', 'Edit, then approve'), el('span', 'item-id', `#${item.action_id}`),
                  el('span', 'item-type', words(item.action_type)));
      li.append(head, el('p', 'item-target', item.target), el('p', 'item-field', `Field: ${item.field}`));
      const before = el('div', 'diff diff-before');
      before.append(el('p', 'diff-label', 'Before'), el('p', 'diff-text', item.before ?? '(empty)'));
      const after = el('div', 'diff diff-after');
      after.append(el('p', 'diff-label', 'After'), el('p', 'diff-text', item.after));
      li.append(before, after);
    } else {
      const head = el('p', 'item-head');
      head.append(el('span', 'verb verb-order', 'Order'), el('span', 'item-type', words(item.order)));
      li.append(head, el('p', 'item-line', String(item.line || '').replace(/^Order:\s*/, '')));
    }
    return li;
  }

  // A rejection reason: Charlie's own words, or labelled as Reaper's wording with a way to change it.
  function reasonBlock(proposal, item) {
    const box = el('div', `reason reason-${item.reason_source}`);
    const label = item.reason_source === 'charlie' ? 'Reason (your words)' : "Reason (Reaper's wording)";
    box.append(el('p', 'reason-label', label), el('p', 'reason-text', item.reason));
    const change = el('button', 'btn btn-link', item.reason_source === 'charlie' ? 'Change reason'
      : 'Use my own words');
    change.type = 'button';
    box.append(change);
    change.addEventListener('click', () => {
      const form2 = el('form', 'reason-form');
      const id = `reason-${proposal.id}-${item.action_id}`;
      const lab = el('label', 'reason-form-label', `Your reason for rejecting #${item.action_id}`);
      lab.htmlFor = id;
      const field = el('input', 'reason-input');
      field.id = id;
      field.value = item.reason;
      field.maxLength = 500;
      const save = el('button', 'btn btn-quiet', 'Use these words');
      save.type = 'submit';
      form2.append(lab, field, save);
      change.replaceWith(form2);
      field.focus();
      form2.addEventListener('submit', async (e) => {
        e.preventDefault();
        const reason = field.value.trim();
        if (!reason) return;
        try {
          const d = await post('/api/revise', { proposal_id: proposal.id, action_id: item.action_id, reason });
          const old = cards.get(proposal.id);
          cards.delete(proposal.id);
          if (d.proposal) showCard(d.proposal, old.node);
          else finish(old, ['Nothing left to confirm:', ...d.refused], true);
        } catch (err) {
          box.append(el('p', 'card-error', `Couldn't change it: ${err.message}`));
        }
      });
    });
    return box;
  }

  async function doConfirm(proposal) {
    const card = cards.get(proposal.id);
    if (!card || card.confirm.disabled) return;
    card.confirm.disabled = true;
    card.note.textContent = 'Confirming. The server checks everything again first.';
    try {
      const d = await post('/api/confirm', { proposal_id: proposal.id });
      finish(card, d.results, !d.ok);
      if (d.signature) { bubble('reaper', d.signature, null); onReply?.({ mode: 'signature' }, d.signature); }
    } catch (err) {
      finish(card, [`Not confirmed: ${err.message}`], true);
    }
  }

  async function doCancel(proposal) {
    const card = cards.get(proposal.id);
    if (!card) return;
    try { await post('/api/cancel', { proposal_id: proposal.id }); } catch { /* the server forgets it anyway */ }
    finish(card, ['Cancelled. Nothing done.'], false, 'cancelled');
  }

  function finish(card, results, failed, how = failed ? 'refused' : 'confirmed') {
    cards.delete(card.node.dataset.proposal);
    card.node.classList.add(`card-${how}`);
    card.node.querySelector('.card-title').textContent = { confirmed: 'Confirmed', cancelled: 'Cancelled',
      refused: 'Not done', withdrawn: 'Withdrawn' }[how];
    card.node.querySelector('.card-sub')?.remove();
    card.node.querySelector('.card-actions')?.remove();
    card.node.querySelectorAll('.reason .btn-link, .reason-form').forEach((n) => n.remove());
    const list = el('ul', 'card-results');
    const quiet = how === 'cancelled' || how === 'withdrawn';   // nothing happened: not a failure, not a success
    for (const line of results) {
      list.append(el('li', quiet ? 'result-quiet' : BAD_RESULT.test(line) || failed ? 'result-bad' : 'result-ok', line));
    }
    card.note.replaceWith(list);
    scroll();
  }

  function withdrawAll(why) {
    for (const card of [...cards.values()]) finish(card, [why], false, 'withdrawn');
  }

  function refresh(id) {
    const card = cards.get(id);
    if (!card) return;
    card.confirm.disabled = stopped || !canDecide();
    card.note.textContent = stopped ? 'Stood down: confirming is off until `town resume` in the terminal.'
      : !canDecide() ? "Decisions on this Town are Charlie's call: Confirm is off for this session." : '';
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value;
    input.value = '';
    send(text);
  });

  return {
    send,
    say(text, meta) { bubble('reaper', text, meta); },
    setStopped(on) {
      if (on && !stopped) withdrawAll('The Town stood down. This list was withdrawn; nothing was done.');
      stopped = on;
      for (const id of cards.keys()) refresh(id);
    },
    setAnalysis(on) {
      log.querySelectorAll('.msg-pending .msg-text').forEach((p) => {
        p.textContent = on ? 'Running a deep analysis. This can take a few minutes.' : 'Working on it.';
      });
    },
    get open() { return cards.size; },
  };
}
