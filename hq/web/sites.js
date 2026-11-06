// Sites (Charlie, 2026-11-05): every build the Builder made, newest first, with its status in plain words and a View.
//
// View always opens the build's own files, served read-only by this page's server under the launch token
// (/site/<token>/<build>/: static files only, nothing on them can run), so it works even when showcase.example.com doesn't
// load on this network. A published site's live link is shown next to it, and every row says the laptop command that
// opens it ("View on this laptop: town show 10", the LEAD number). Nothing here decides or publishes anything.

import { siteUrl } from './api.js';

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};

export function siteItem(site) {
  const li = el('li', 'site');
  li.dataset.state = site.state;
  li.dataset.build = String(site.build_id);
  const who = site.lead_id ? `lead #${site.lead_id}, ` : '';
  li.append(el('p', 'site-title', `${site.business} (${who}build #${site.build_id})`));
  li.append(el('p', 'site-status', `${site.status} \u00b7 built ${site.created}`));
  if (site.show) li.append(el('p', 'site-show', `View on this laptop: ${site.show}`));
  const links = el('div', 'site-links');
  if (site.viewable) {
    const view = el('a', 'btn btn-quiet site-view', 'View');
    view.href = siteUrl(site.build_id);
    view.target = '_blank';
    view.rel = 'noopener noreferrer';
    links.append(view);
  }
  if (site.live_url) {
    const live = el('a', 'site-live', 'Live link');
    live.href = site.live_url;
    live.target = '_blank';
    live.rel = 'noopener noreferrer';
    links.append(live);
  }
  if (links.childElementCount) li.append(links);
  return li;
}

export function createSites({ dialog, list, open, close, get }) {
  async function refresh() {
    try {
      const data = await get('/api/sites');
      const sites = data.sites || [];
      list.replaceChildren(...(sites.length ? sites.map(siteItem) : [el('li', 'site-empty', 'No sites built yet.')]));
    } catch (err) {
      list.replaceChildren(el('li', 'site-empty', `Couldn't load the sites (${err.message}).`));
    }
  }
  open.addEventListener('click', () => {
    if (dialog.open) { dialog.close(); return; }
    dialog.show();   // not modal: STAND DOWN stays reachable while it's open
    refresh();
  });
  close.addEventListener('click', () => dialog.close());
  return { refresh };
}
