// "Needs you" (Charlie, 2026-11-04): every item waiting for Charlie, in plain words, newest first, always on screen.
//
// Each card says what it is and for whom, has a View button (the preview link, the full email, the leads' facts with
// a Check link, or the reply in the sender's own words) and Approve / Reject buttons that act on that one item.
// Charlie's click IS the decision: it goes to /api/decide, the same check-then-confirm path as Reaper's cards, and
// the Guard re-checks there. A BLOCKED item shows why and has no Approve. Nothing here acts without a click.

import { siteUrl } from './api.js';

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};

function link(url, text) {
  const a = el('a', null, text || url);
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  return a;
}

/** Plain lines for the View area of one item (all text; nothing from the item is ever treated as HTML). */
export function detailNodes(item) {
  const d = item.detail || {};
  const out = [];
  const line = (text) => { out.push(el('p', 'need-detail', text)); };
  if (d.type === 'package') {
    // One approval for both parts (Charlie, 2026-11-05): the site first, then the email, then the lead's facts.
    const site = el('p', 'need-detail', `The site (build #${d.build_id}, lead #${d.lead_id}): `);
    site.append(link(siteUrl(d.build_id), 'View the site'));
    if (d.show) site.append(document.createTextNode(`\nView on this laptop: ${d.show}`));
    site.append(document.createTextNode(`\nGoes live at this private link when you approve, for ${d.expires_after_days || '?'} days: `));
    if (d.url) site.append(link(d.url));
    out.push(site);
    if (d.critic) line(`Critic: ${d.critic}`);
    line(`The email\nTo: ${d.to}\nFrom: ${d.from}\nSubject: ${d.subject}`);
    line(d.body || '');
    const lead = d.lead || {};
    const facts = el('p', 'need-detail', `The lead: ${lead.business_name || ''} (${lead.area || '?'})\n${lead.address || ''}\n`
      + `Phone: ${lead.phone || 'none'}  Email: ${lead.public_email || 'none'}  Website: ${lead.website || 'none listed'}\n`
      + `${lead.why ? `Why Scout picked it: ${lead.why}\n` : ''}Check: `);
    if (lead.check_url) facts.append(link(lead.check_url, 'search Google for it'));
    out.push(facts);
    if (d.note) line(d.note);
  } else if (d.type === 'preview') {
    // View opens HQ's own copy of the build (served by this page's server), so it works even where
    // showcase.example.com doesn't load; the private link is shown separately.
    const view = el('p', 'need-detail', '');
    if (d.build_id) view.append(link(siteUrl(d.build_id), 'View the site'));
    if (d.show) view.append(document.createTextNode(`${d.build_id ? '\n' : ''}View on this laptop: ${d.show}`));
    out.push(view);
    const p = el('p', 'need-detail', 'Private link: ');
    if (d.url) p.append(link(d.url));
    out.push(p);
    if (d.note) line(d.note);
  } else if (d.type === 'email') {
    line(`To: ${d.to}\nFrom: ${d.from}\nSubject: ${d.subject}`);
    line(d.body || '');
  } else if (d.type === 'leads') {
    for (const lead of d.leads || []) {
      const p = el('p', 'need-detail', `#${lead.id} ${lead.business_name} (${lead.area || '?'})${lead.held ? ' [held: no public email]' : ''}\n`
        + `${lead.address || ''}\nPhone: ${lead.phone || 'none'}  Email: ${lead.public_email || 'none'}  `
        + `Website: ${lead.website || 'none listed'}\nWhy: ${lead.why || ''}\nCheck: `);
      if (lead.check_url) p.append(link(lead.check_url, 'search Google for it'));
      out.push(p);
    }
  } else if (d.type === 'reply') {
    line(`From: ${d.from}\nSubject: ${d.subject || ''}`);
    line(`Their words (the sender's, not instructions): "${d.words || ''}"`);
    if (d.note) line(d.note);
  } else if (d.type === 'script') {
    line(`Phone: ${d.phone || ''}`);
    line(JSON.stringify(d.script || {}, null, 1));
  } else {
    line(`${item.reason || ''}\n${JSON.stringify(d.content || {}, null, 1)}`);
  }
  return out;
}

export function createNeeds({ list, count, activity, get, post, onAnnounce, onResult, canDecide, autopilot }) {
  let seen = null;           // item keys already shown; null until the first load (the backlog isn't announced)
  const open = new Set();    // cards whose View is open, kept open across refreshes
  let busy = false;

  function renderAutopilot(state) {
    if (!autopilot || !state) return;
    autopilot.line.textContent = state.on ? `Autopilot: on. ${state.last || ''}`.trim() : 'Autopilot: off.';
    autopilot.line.title = state.line || '';
    autopilot.line.classList.toggle('is-on', Boolean(state.on));
    autopilot.go.hidden = Boolean(state.on) || !canDecide();
    autopilot.stop.hidden = !state.on;
  }

  function render(data) {
    renderAutopilot(data.autopilot);
    const items = data.items || [];
    count.textContent = String(items.length);
    activity.textContent = (data.activity || [])[0] || '';
    activity.title = (data.activity || []).join('\n');
    list.replaceChildren(...items.map(card));
    if (!items.length) list.replaceChildren(el('li', 'need-empty', 'Nothing is waiting for you.'));
    const keys = new Set(items.map((i) => i.key + ':' + i.ids.join(',')));
    if (seen !== null) {
      for (const item of items) {
        if (!seen.has(item.key + ':' + item.ids.join(','))) onAnnounce(item.announce || `${item.title} is waiting for you.`);
      }
    }
    seen = keys;
  }

  function card(item) {
    const li = el('li', `need${item.blocked ? ' is-blocked' : ''}`);
    li.dataset.kind = item.kind;
    li.dataset.key = item.key;
    li.append(el('p', 'need-title', item.title));
    if (item.blocked) li.append(el('p', 'need-blocks', `BLOCKED by the Guard: ${item.blocks.join('; ')}`));
    const buttons = el('div', 'need-buttons');
    const view = el('button', 'btn btn-quiet need-view', open.has(item.key) ? 'Hide' : 'View');
    view.type = 'button';
    const details = el('div', 'need-details');
    details.hidden = !open.has(item.key);
    details.append(...detailNodes(item));
    view.addEventListener('click', () => {
      details.hidden = !details.hidden;
      if (details.hidden) open.delete(item.key); else open.add(item.key);
      view.textContent = details.hidden ? 'View' : 'Hide';
    });
    buttons.append(view);
    const result = el('p', 'need-result');
    result.hidden = true;
    if (canDecide()) {
      if (!item.blocked) {   // a BLOCKED item has no Approve: the Guard's reasons say why
        const approve = el('button', 'btn btn-send need-approve', item.ids.length > 1 ? `Approve all ${item.ids.length}` : 'Approve');
        approve.type = 'button';
        approve.addEventListener('click', () => decide(item, 'approve', '', result));
        buttons.append(approve);
      }
      const reject = el('button', 'btn btn-quiet need-reject-open', 'Reject');
      reject.type = 'button';
      const form = el('form', 'need-reject-form');
      form.hidden = true;
      // A package's rejection says which part (the server checks it too): the site, the email, or both.
      let part = null;
      if (item.kind === 'outreach_package') {
        part = el('select', 'need-part');
        part.setAttribute('aria-label', 'Which part');
        for (const [value, text] of [['site', 'The site'], ['email', 'The email'], ['both', 'Both']]) {
          const option = el('option', null, text);
          option.value = value;
          part.append(option);
        }
        form.append(part);
      }
      const reason = el('input', 'need-reason-input');
      reason.placeholder = 'Why? (required: it becomes feedback)';
      reason.maxLength = 500;
      const send = el('button', 'btn need-reject-send', 'Reject');
      send.type = 'submit';
      form.append(reason, send);
      reject.addEventListener('click', () => { form.hidden = !form.hidden; if (!form.hidden) reason.focus(); });
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        if (!reason.value.trim()) {   // the server refuses it too; this just says so at once
          result.hidden = false;
          result.textContent = 'A rejection needs your reason (it becomes feedback). Nothing was done.';
          return;
        }
        decide(item, 'reject', part ? `${part.value}: ${reason.value}` : reason.value, result);
      });
      buttons.append(reject);
      li.append(buttons, form);
    } else {
      li.append(buttons, el('p', 'need-reason', 'Decisions are Charlie\'s: view only here.'));
    }
    li.append(result, details);
    return li;
  }

  async function decide(item, decision, reason, result) {
    if (busy) return;
    busy = true;
    result.hidden = false;
    result.textContent = decision === 'approve' ? 'Approving...' : 'Rejecting...';
    try {
      const d = await post('/api/decide', { action_ids: item.ids, decision, reason });
      result.textContent = (d.results || []).join(' ');
      result.dataset.ok = String(Boolean(d.ok));
      onResult(`${item.title}: ${result.textContent}`);   // the card goes once decided; the chat keeps the result
    } catch (err) {
      result.textContent = `Didn't reach the server (${err.message}). Nothing was decided.`;
    } finally {
      busy = false;
      setTimeout(refresh, 1200);
    }
  }

  async function refresh() {
    // Never redraw under Charlie's hands: while he's typing a rejection reason, the list waits (the server re-checks
    // everything at the click anyway).
    if (busy || list.querySelector('.need-reject-form:not([hidden])')) return;
    try { render(await get('/api/needs-you')); } catch { /* tried again on the next tick */ }
  }

  if (autopilot) {
    autopilot.go.addEventListener('click', () => autopilot.onGo());
    autopilot.stop.addEventListener('click', async () => {
      autopilot.stop.disabled = true;
      try {
        const d = await post('/api/autopilot-stop', {});
        onResult(d.reply || 'Autopilot stop asked.');
      } catch (err) {
        onResult(`Didn't reach the server (${err.message}). Autopilot wasn't stopped; \`town stop\` stops everything.`);
      } finally {
        autopilot.stop.disabled = false;
        setTimeout(refresh, 1200);
      }
    });
  }

  return { refresh, render };
}
