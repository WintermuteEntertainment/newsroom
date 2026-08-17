const DEFAULTS = { endpoint: 'http://127.0.0.1:8767', count: 12, newtabCount: 24 };
const $ = (sel) => document.querySelector(sel);

function settings() {
  return storage.sync.get(DEFAULTS);
}

// The digest is one file identical for every reader, so caching costs nothing in freshness terms.
// Keyed by endpoint so switching servers never shows the previous one's rows. The new tab reads
// the same cache: opening ten tabs must not mean ten fetches.
const CACHE_MS = 5 * 60 * 1000;

async function fetchDigest(endpoint, { force = false } = {}) {
  const key = `cache:${endpoint}`;
  if (!force) {
    const cached = await storage.local.get({ [key]: null });
    const hit = cached[key];
    if (hit && Date.now() - hit.at < CACHE_MS) return hit.data;
  }
  const response = await fetch(`${endpoint.replace(/\/+$/, '')}/api/digest`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`Server returned ${response.status}`);
  const data = await response.json();
  storage.local.set({ [key]: { at: Date.now(), data } });
  return data;
}

// scan.completed_utc is the only honest freshness claim. fetched_utc is written when the run
// STARTS reading feeds, so it predates the content by the whole run (long, on local models) and would
// read "just now" over the PREVIOUS digest. If fetched is newer than completed, a run is in
// flight and we say so instead of implying the content is current.
function freshness(data) {
  const scan = data.scan || {};
  // data.refreshRunning is the server's own answer and wins when present. Comparing the two
  // stamps is only a proxy and fails the same way app.js's did: a run that dies between
  // writing fetched_utc and completed_utc leaves this saying 'refreshing now' forever.
  // Observed on the website 2026-08-04 (run died ~34 min in, banner stuck for hours). An
  // older server omits the field, so fall back to the proxy rather than reporting idle.
  if (typeof data.refreshRunning === 'boolean') {
    if (data.refreshRunning) return 'refreshing now';
  } else if (scan.fetched_utc && (!scan.completed_utc
             || new Date(scan.fetched_utc) > new Date(scan.completed_utc))) {
    return 'refreshing now';
  }
  const stamp = scan.completed_utc || data.updated;
  if (!stamp) return '';
  const mins = Math.round((Date.now() - new Date(stamp).getTime()) / 60000);
  if (mins < 2) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

// A digest older than this is called out explicitly. Nothing refreshes it automatically -- the
// pipeline only runs when someone presses the button -- so on a new tab page a silent stale
// digest would otherwise pass for today's news.
const STALE_HOURS = 12;

function isStale(data) {
  const stamp = (data.scan || {}).completed_utc || data.updated;
  if (!stamp) return false;
  return (Date.now() - new Date(stamp).getTime()) / 3600000 > STALE_HOURS;
}


// ---------------------------------------------------------------------------
// Starting a run
//
// Three rules this code will not break:
//
// 1. The profile is ALWAYS sent explicitly. Omitting it makes the server fall back to
//    routing.json's active_profile, which may be metered -- so an omitted profile is a
//    request to spend the user's money by accident.
// 2. Only non-metered profiles are offered. The extension holds no password and must never
//    hold one; a metered profile would 401 anyway, and asking for a password here would
//    turn a read-only client into a credential store.
// 3. Content-Type is text/plain, not application/json. The server ignores content-type when
//    parsing the body, and text/plain is CORS-safelisted -- so the request needs no preflight.
//    The server answers OPTIONS with 501, so a preflight would fail outright.
// ---------------------------------------------------------------------------

function freeProfiles(data) {
  const profiles = ((data.routing || {}).profiles) || [];
  return profiles.filter((p) => !p.metered);
}

async function startRun(endpoint, profile) {
  const response = await fetch(`${endpoint.replace(/\/+$/, '')}/api/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain' },
    body: JSON.stringify({ profile }),
  });
  const payload = await response.json().catch(() => ({}));
  // 409 is not a fault: the server refuses to stack runs, which is what we want. It usually
  // means the user already started one here, on the site, or in another tab -- so this reports
  // it as information and lets the caller attach to the run in progress rather than showing
  // a red error for correct behaviour.
  if (response.status === 409) {
    const busy = new Error(payload.error || 'A refresh is already running.');
    busy.alreadyRunning = true;
    throw busy;
  }
  if (response.status === 401) throw new Error('That profile bills the API and needs the password on the site.');
  if (!response.ok) throw new Error(payload.error || `Server returned ${response.status}.`);
  return payload;
}

async function runState(endpoint) {
  const response = await fetch(`${endpoint.replace(/\/+$/, '')}/api/refresh`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`Server returned ${response.status}.`);
  return response.json();
}

// A local run is long, so this polls slowly and survives the popup being closed --
// the run continues server-side regardless, and reopening picks the state back up from the server.
const POLL_MS = 15000;

function watchRun(endpoint, onUpdate) {
  const tick = async () => {
    try {
      const state = await runState(endpoint);
      onUpdate(state);
      if (state.running) setTimeout(tick, POLL_MS);
    } catch (err) {
      onUpdate({ running: false, error: err.message });
    }
  };
  tick();
}

function parseOutletLinks(raw) {
  if (!raw) return {};
  if (typeof raw === 'object') return raw;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (err) {
    return {};
  }
}

function storyNode(row, rank) {
  const li = document.createElement('li');
  li.className = 'story';

  if (rank) {
    const n = document.createElement('span');
    n.className = 'rank';
    n.textContent = rank;
    li.appendChild(n);
  }

  const body = document.createElement('div');
  body.className = 'body';

  const a = document.createElement('a');
  a.className = 'headline';
  a.href = row.link || '#';
  a.target = '_blank';
  a.rel = 'noreferrer';
  a.textContent = row.headline || '(untitled)';
  body.appendChild(a);

  if (row.snippet) {
    const p = document.createElement('p');
    p.className = 'snippet';
    p.textContent = row.snippet;
    body.appendChild(p);
  }

  const meta = document.createElement('div');
  meta.className = 'meta';

  const n = row.n_panel_outlets;
  if (n) {
    const count = document.createElement('span');
    count.className = 'count';
    count.textContent = `${n} outlet${n === 1 ? '' : 's'}`;
    if (row.panel_size) count.title = `out of a fixed ${row.panel_size}-outlet panel`;
    meta.appendChild(count);
  }

  // One chip per outlet, linking to THAT outlet's own article on this story -- the digest
  // payload already carries outlet_links (a JSON object of name -> url), so this needs no
  // extra request. Capped at three: a story runs on up to 19 outlets, and 24 x 19 chips
  // would bury the headlines this list exists to show. Outlets that covered the story
  // closest (covered_closest_by -- the reporting the snippet was drawn from) come first
  // and get the leader treatment, matching the website.
  const closest = (row.covered_closest_by || '').split(';').map((s) => s.trim()).filter(Boolean);
  const carried = (row.also_carried_by || '').split(';').map((s) => s.trim()).filter(Boolean);
  const links = parseOutletLinks(row.outlet_links);
  const leaders = new Set(closest);
  const shownOutlets = [...closest, ...carried].slice(0, 3);

  if (shownOutlets.length) {
    const chips = document.createElement('span');
    chips.className = 'chips';
    shownOutlets.forEach((name) => {
      const url = links[name];
      // A span, not a dead <a>: an anchor with no href looks clickable but is not.
      const el = document.createElement(url ? 'a' : 'span');
      el.className = 'chip' + (leaders.has(name) ? ' chip-leader' : '');
      el.textContent = name;
      if (url) {
        el.href = url;
        el.target = '_blank';
        el.rel = 'noopener noreferrer';
      }
      chips.appendChild(el);
    });
    const extra = closest.length + carried.length - shownOutlets.length;
    if (extra > 0) {
      const more = document.createElement('span');
      more.className = 'chip chip-more';
      more.textContent = `+${extra}`;
      more.title = [...closest, ...carried].slice(3).join(', ');
      chips.appendChild(more);
    }
    meta.appendChild(chips);
  }

  // The pipeline checks every snippet against its own sources. Anything not positively clean is
  // shown, not hidden: a summary that failed its check should admit it.
  if (row.entailment && row.entailment !== 'clean') {
    const flag = document.createElement('span');
    flag.className = 'flag';
    flag.textContent = 'unverified';
    flag.title = `Entailment check: ${row.entailment}`;
    meta.appendChild(flag);
  }

  body.appendChild(meta);
  li.appendChild(body);
  return li;
}

async function render({ mode }) {
  const cfg = await settings();
  const limit = mode === 'newtab' ? cfg.newtabCount : cfg.count;

  const site = $('#open-site');
  if (site) site.href = cfg.endpoint;
  const opts = $('#open-options');
  if (opts) opts.addEventListener('click', (e) => { e.preventDefault(); openOptions(); });

  const reload = $('#reload');
  if (reload) reload.addEventListener('click', () => draw({ force: true }));

  const runBtn = $('#run');
  const runNote = $('#run-note');
  let lastData = null;

  function note(text, cls) {
    if (!runNote) return;
    runNote.textContent = text || '';
    runNote.className = `run-note${cls ? ' ' + cls : ''}`;
    runNote.hidden = !text;
  }

  // Reflects server state rather than local optimism: if a run was started from the site, or
  // from another tab, this shows it too.
  function applyRunState(state) {
    if (!runBtn) return;
    if (state.running) {
      runBtn.disabled = true;
      runBtn.textContent = 'Running…';
      note(`Run in progress${state.profile ? ` (${state.profile})` : ''}. Local models take a while — you can close this.`);
      return;
    }
    runBtn.disabled = false;
    runBtn.textContent = 'Refresh now';
    if (state.error) {
      note(`Last run failed. ${state.error}`, 'error');
    } else if (state.finishedJustNow) {
      note('Run finished — reloading the digest.', 'ok');
      draw({ force: true });
    }
  }

  function wireRun() {
    if (!runBtn) return;
    const free = freeProfiles(lastData || {});

    if (!free.length) {
      runBtn.hidden = true;
      note('No free routing profile is available on this server, so a run cannot be started from here.');
      return;
    }
    runBtn.hidden = false;
    const profile = free[0].name;
    runBtn.title = `Starts the "${profile}" profile — ${free[0].why || 'no metered spend'}`;

    runBtn.addEventListener('click', async () => {
      runBtn.disabled = true;
      note('Starting…');
      const follow = () => {
        let wasRunning = false;
        watchRun(cfg.endpoint, (state) => {
          if (wasRunning && !state.running) state.finishedJustNow = true;
          wasRunning = state.running;
          applyRunState(state);
        });
      };

      try {
        await startRun(cfg.endpoint, profile);
        follow();
      } catch (err) {
        if (err.alreadyRunning) {
          // Nothing went wrong and nothing was queued: report the run that IS going and
          // track it, so a second click behaves like watching rather than failing.
          note(err.message);
          follow();
          return;
        }
        note(err.message, 'error');
        runBtn.disabled = false;
      }
    });
  }

  async function draw({ force = false } = {}) {
    const status = $('#status');
    const list = $('#stories');
    list.textContent = '';
    status.hidden = false;
    status.className = 'status';
    status.textContent = 'Loading…';

    try {
      const data = await fetchDigest(cfg.endpoint, { force });
      lastData = data;
      const rows = (data.rows || []).slice(0, limit);

      if (!rows.length) {
        status.textContent = 'No stories yet — the pipeline has not produced a digest.';
      } else {
        rows.forEach((row, i) => list.appendChild(storyNode(row, mode === 'newtab' ? i + 1 : 0)));
        list.hidden = false;
        status.hidden = true;
      }

      const stamp = $('#stamp');
      if (stamp) stamp.textContent = freshness(data);

      const warn = $('#stale');
      if (warn) {
        if (isStale(data)) {
          warn.textContent = `This digest is ${freshness(data)} — nothing refreshes it automatically.`;
          warn.hidden = false;
        } else {
          warn.hidden = true;
        }
      }

      const panel = $('#panel');
      if (panel && rows[0] && rows[0].panel_size) {
        panel.textContent = `ranked by how many of ${rows[0].panel_size} outlets carried each story`;
      }
      const date = $('#date');
      if (date && data.date) date.textContent = data.date;
    } catch (err) {
      status.className = 'status error';
      status.textContent = `Could not reach the digest server at ${cfg.endpoint}. ${err.message}`;
    }
  }

  await draw();
  wireRun();
  // Check once on open: a run may already be under way from the site or another tab.
  if (runBtn) {
    try { applyRunState(await runState(cfg.endpoint)); } catch (err) { /* offline: draw() already said so */ }
  }
}
