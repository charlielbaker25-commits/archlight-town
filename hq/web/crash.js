// Never a blank page: any error on the page shows up in a box with the exact details and a "Copy error details"
// button, so Charlie can paste it to Claude Code. Loaded first, as a plain script with no imports, so it works even
// when the 3D code itself fails to load. The Town is never affected: this page only shows things and asks.
(function () {
  'use strict';
  const IGNORE = [/ResizeObserver loop/i];   // the browser's own harmless notices, not errors
  let shown = 0;

  function details(kind, message, extra) {
    const lines = [
      `What: ${kind}`,
      `Message: ${message}`,
      `When: ${new Date().toISOString()}`,
      `Page: Reaper HQ`,
      `Browser: ${navigator.userAgent}`,
      `Screen: ${window.innerWidth}x${window.innerHeight} at ${window.devicePixelRatio || 1}x,`
        + ` full screen ${document.fullscreenElement ? 'yes' : 'no'}, demo mode ${document.body?.classList.contains('is-demo') ? 'on' : 'off'}`,
    ];
    try {
      const map = window.HQ && window.HQ.map && window.HQ.map.debug && window.HQ.map.debug();
      if (map) lines.push(`Map: ${JSON.stringify({ memory: map.memory, timing: map.timing, fps: map.fps, walking: map.walking.length, queued: map.queued, pixelRatio: map.pixelRatio })}`);
      if (window.HQ && window.HQ.graphics) lines.push(`Graphics: ${window.HQ.graphics}`);
    } catch (e) { /* the map itself may be what broke */ }
    if (extra) lines.push('', String(extra).slice(0, 4000));
    return lines.join('\n');
  }

  function show(kind, message, extra) {
    if (IGNORE.some((re) => re.test(String(message)))) return;
    shown += 1;
    const box = document.getElementById('crash');
    const pre = document.getElementById('crash-details');
    const text = details(kind, message, extra);
    if (!box || !pre) { console.error(text); return; }
    pre.textContent = shown > 1 ? `${text}\n\n(${shown} errors so far; this is the latest.)` : text;
    box.hidden = false;
  }

  window.HQCrash = { report: show };

  window.addEventListener('error', (e) => {
    if (e.target && e.target !== window && (e.target.src || e.target.href)) {
      show('a file failed to load', e.target.src || e.target.href);   // a script or stylesheet that didn't arrive
      return;
    }
    show('page error', e.message || 'unknown error', e.error && e.error.stack);
  }, true);
  window.addEventListener('unhandledrejection', (e) => {
    const r = e.reason;
    show('unfinished task failed', (r && r.message) || String(r), r && r.stack);
  });

  document.addEventListener('DOMContentLoaded', () => {
    const copy = document.getElementById('crash-copy');
    copy.addEventListener('click', async () => {
      const text = document.getElementById('crash-details').textContent;
      try {
        await navigator.clipboard.writeText(text);
        copy.textContent = 'Copied';
      } catch (err) {
        // No clipboard access: select the text so Ctrl+C copies it.
        const range = document.createRange();
        range.selectNodeContents(document.getElementById('crash-details'));
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        copy.textContent = 'Selected: press Ctrl+C';
      }
      setTimeout(() => { copy.textContent = 'Copy error details'; }, 2500);
    });
    document.getElementById('crash-close').addEventListener('click', () => { document.getElementById('crash').hidden = true; });
    document.getElementById('crash-reload').addEventListener('click', () => window.location.reload());
  });
}());
