const DEFAULTS = { endpoint: 'http://127.0.0.1:8767', count: 12, newtabCount: 24 };
const $ = (sel) => document.querySelector(sel);

storage.sync.get(DEFAULTS).then((cfg) => {
  $('#endpoint').value = cfg.endpoint;
  $('#count').value = cfg.count;
  $('#newtabCount').value = cfg.newtabCount;
});

function show(message, ok) {
  const el = $('#result');
  el.textContent = message;
  el.className = ok ? 'result ok' : 'result error';
  el.hidden = false;
}

const clamp = (v, fallback) => Math.min(50, Math.max(1, Number(v) || fallback));

$('#save').addEventListener('click', () => {
  const endpoint = $('#endpoint').value.trim().replace(/\/+$/, '');
  if (!/^https?:\/\//.test(endpoint)) {
    return show('The address needs to start with http:// or https://', false);
  }
  const cfg = {
    endpoint,
    count: clamp($('#count').value, DEFAULTS.count),
    newtabCount: clamp($('#newtabCount').value, DEFAULTS.newtabCount),
  };
  // Drop the cached digest so the next tab reflects the new server immediately rather than
  // showing the old one's stories for up to five minutes.
  storage.sync.set(cfg)
    .then(() => storage.local.clear())
    .then(() => show('Saved.', true));
});

// Its own button because a wrong address otherwise surfaces only as an unexplained empty page.
$('#test').addEventListener('click', async () => {
  const endpoint = $('#endpoint').value.trim().replace(/\/+$/, '');
  show('Checking…', true);
  try {
    const response = await fetch(`${endpoint}/api/digest`, { cache: 'no-store' });
    if (!response.ok) return show(`Server answered ${response.status}.`, false);
    const data = await response.json();
    const n = (data.rows || []).length;
    show(n ? `Reached it — ${n} stories, dated ${data.date}.` : 'Reached it, but no digest yet.', !!n);
  } catch (err) {
    show(`Could not reach it. ${err.message}`, false);
  }
});
