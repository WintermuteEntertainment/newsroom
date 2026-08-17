const stories = document.querySelector('#stories');
const template = document.querySelector('#story-template');
const notice = document.querySelector('#notice');
const count = document.querySelector('#result-count');
const search = document.querySelector('#search');
const refresh = document.querySelector('#refresh');
const profileSelect = document.querySelector('#profile');
const profileWrap = document.querySelector('#profile-wrap');
const profileRun = document.querySelector('#profile-run');
const scanDebug = document.querySelector('#scan-debug');
const lastRefreshed = document.querySelector('#last-refreshed');
const coverageChart = document.querySelector('#coverage-chart');
const coverageSvg = document.querySelector('#coverage-svg');
const coverageTooltip = document.querySelector('#coverage-tooltip');
const toast = document.querySelector('#toast');
let rows = [];

function showNotice(message = '') {
  notice.hidden = !message;
  notice.textContent = message;
}

let toastTimer = null;
function showToast(message, duration = 4000) {
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, duration);
}

function parseOutletLinks(raw) {
  if (!raw) return {};
  try { return JSON.parse(raw) || {}; } catch (err) { return {}; }
}

function render(query = '') {
  const term = query.trim().toLowerCase();
  const shown = rows.filter(row => [row.headline, row.snippet, row.covered_closest_by, row.also_carried_by].join(' ').toLowerCase().includes(term));
  stories.replaceChildren();
  for (const row of shown) {
    const fragment = template.content.cloneNode(true);
    fragment.querySelector('.rank').textContent = String(row.rank).padStart(2, '0');
    fragment.querySelector('.coverage').textContent = `${row.n_panel_outlets}/${row.panel_size} outlets`;
    fragment.querySelector('.articles').textContent = `${row.n_articles} articles`;
    const link = fragment.querySelector('.story-link'); link.textContent = row.headline; link.href = row.link;
    fragment.querySelector('.meter i').style.width = `${(Number(row.n_panel_outlets) / Number(row.panel_size)) * 100}%`;
    fragment.querySelector('.snippet').textContent = row.snippet;
    // One button per covering outlet, linking straight to that outlet's own article on this
    // story (outlet_links) -- not just the single featured link. Outlets covering the story
    // closest (covered_closest_by) are marked so a reader can see which reporting the snippet
    // itself was drawn from, versus outlets that only also carried it.
    const links = parseOutletLinks(row.outlet_links);
    const leaders = new Set((row.covered_closest_by || '').split(';').map(s => s.trim()).filter(Boolean));
    const coverage = fragment.querySelector('.voice-sources');
    const outlets = Object.keys(links).length ? Object.keys(links)
      : [...leaders, ...(row.also_carried_by || '').split(';').map(s => s.trim()).filter(Boolean)];
    outlets.forEach(source => {
      const url = links[source];
      const el = document.createElement(url ? 'a' : 'span');
      el.className = 'chip' + (leaders.has(source) ? ' chip-leader' : '');
      el.textContent = source;
      if (url) { el.href = url; el.target = '_blank'; el.rel = 'noopener'; }
      coverage.append(el);
    });
    const carried = row.also_carried_by ? `Also carried by ${row.also_carried_by}.` : 'No other panel outlets carried this story.';
    fragment.querySelector('.carried').textContent = carried;
    const entailment = row.entailment || 'clean', form = row.snippet_form || 'ok';
    const verify = fragment.querySelector('.verify');
    verify.textContent = `Entailment: ${entailment} · Snippet form: ${form}`;
    verify.classList.toggle('verify-flagged', entailment !== 'clean' || form !== 'ok');
    stories.append(fragment);
  }
  count.textContent = `${shown.length} ${shown.length === 1 ? 'story' : 'stories'}${term ? ' match your search' : ' in this report'}`;
}

// One scatter point per story: x = how many panel outlets covered it (breadth), y = how many
// total articles exist about it (volume). Deliberately the only chart on the page -- kept to
// one, single-series, so it needs no legend and no palette beyond the theme's own accent.
const CHART_W = 560, CHART_H = 220, CHART_M = { top: 14, right: 16, bottom: 32, left: 34 };

function svgEl(tag, attrs) {
  const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}

function showCoverageTooltip(point, cx, cy) {
  coverageTooltip.hidden = false;
  coverageTooltip.replaceChildren();
  const title = document.createElement('strong'); title.textContent = point.headline;
  const stats = document.createElement('span');
  stats.textContent = `${point.outlets} outlet${point.outlets === 1 ? '' : 's'} · ${point.articles} article${point.articles === 1 ? '' : 's'}`;
  coverageTooltip.append(title, stats);
  const rect = coverageSvg.getBoundingClientRect();
  const scaleX = rect.width / CHART_W, scaleY = rect.height / CHART_H;
  coverageTooltip.style.left = `${rect.left + cx * scaleX}px`;
  coverageTooltip.style.top = `${rect.top + cy * scaleY - 10}px`;
}
function hideCoverageTooltip() { coverageTooltip.hidden = true; }

function renderCoverageChart(allRows) {
  const points = (allRows || [])
    .map(r => ({ headline: r.headline, outlets: Number(r.n_panel_outlets), articles: Number(r.n_articles) }))
    .filter(p => Number.isFinite(p.outlets) && Number.isFinite(p.articles));
  const xMax = Math.max(0, ...points.map(p => p.outlets)) * 1.1;
  const yMax = Math.max(0, ...points.map(p => p.articles)) * 1.1;
  if (points.length < 2 || xMax <= 0 || yMax <= 0) { coverageChart.hidden = true; return; }
  coverageChart.hidden = false;

  const { top, right, bottom, left } = CHART_M;
  const xScale = v => left + (v / xMax) * (CHART_W - left - right);
  const yScale = v => CHART_H - bottom - (v / yMax) * (CHART_H - top - bottom);

  coverageSvg.replaceChildren();
  coverageSvg.append(svgEl('line', { x1: left, y1: CHART_H - bottom, x2: CHART_W - right, y2: CHART_H - bottom, class: 'chart-axis' }));
  coverageSvg.append(svgEl('line', { x1: left, y1: top, x2: left, y2: CHART_H - bottom, class: 'chart-axis' }));

  const steps = 4;
  for (let i = 0; i <= steps; i++) {
    const xv = Math.round((xMax / 1.1) * (i / steps)), x = xScale(xv);
    coverageSvg.append(svgEl('line', { x1: x, y1: CHART_H - bottom, x2: x, y2: CHART_H - bottom + 4, class: 'chart-tick' }));
    const xt = svgEl('text', { x, y: CHART_H - bottom + 15, class: 'chart-axis-label', 'text-anchor': 'middle' });
    xt.textContent = xv; coverageSvg.append(xt);

    const yv = Math.round((yMax / 1.1) * (i / steps)), y = yScale(yv);
    coverageSvg.append(svgEl('line', { x1: left - 4, y1: y, x2: left, y2: y, class: 'chart-tick' }));
    const yt = svgEl('text', { x: left - 8, y: y + 3, class: 'chart-axis-label', 'text-anchor': 'end' });
    yt.textContent = yv; coverageSvg.append(yt);
  }

  const xCapX = left + (CHART_W - left - right) / 2;
  const xCap = svgEl('text', { x: xCapX, y: CHART_H - 4, class: 'chart-caption', 'text-anchor': 'middle' });
  xCap.textContent = 'Outlets covering the story'; coverageSvg.append(xCap);
  const yCapY = top + (CHART_H - top - bottom) / 2;
  const yCap = svgEl('text', { x: 11, y: yCapY, class: 'chart-caption', 'text-anchor': 'middle', transform: `rotate(-90 11 ${yCapY})` });
  yCap.textContent = 'Articles written'; coverageSvg.append(yCap);

  points.forEach(point => {
    const cx = xScale(point.outlets), cy = yScale(point.articles);
    const dot = svgEl('circle', { cx, cy, r: 5, class: 'chart-dot' });
    const hit = svgEl('circle', { cx, cy, r: 11, class: 'chart-hit' });
    hit.addEventListener('mouseenter', () => { dot.setAttribute('r', 7); showCoverageTooltip(point, cx, cy); });
    hit.addEventListener('mouseleave', () => { dot.setAttribute('r', 5); hideCoverageTooltip(); });
    coverageSvg.append(dot, hit);
  });
}

function renderScan(scan) {
  if (!scan) { scanDebug.hidden = true; return; }
  scanDebug.hidden = false;
  scanDebug.replaceChildren();
  const line = (text, cls = '') => { const p = document.createElement('p'); if (cls) p.className = cls; p.textContent = text; scanDebug.append(p); };

  const kept = scan.kept_by_source || {};
  const totalKept = Object.values(kept).reduce((a, b) => a + b, 0);
  // n_raw is everything the feeds handed over, counted BEFORE the freshness filter runs, so
  // it does not depend on max_age_hours at all. Attaching "in the last Xh" to it overstated
  // the scan by 5.7x on 2026-08-05 -- 1820 claimed against 319 actually inside the 4h window
  // -- and, because the figure never moved when the window was narrowed from 12h to 4h, it
  // read as though the pipeline was ignoring staleness entirely. The window belongs to
  // totalKept, which is the number the text digest has always printed for the same run.
  line(`Scanned ${scan.n_raw ?? '?'} headlines · ${totalKept} published in the last `
    + `${scan.max_age_hours ?? '?'}h, after de-duplication `
    + `· dropped ${scan.n_stale ?? 0} as stale, ${scan.n_undated ?? 0} undated`);

  const bySource = Object.entries(kept).sort((a, b) => b[1] - a[1])
    .map(([source, n]) => `${source} ${n}`).join(' · ');
  if (bySource) line(bySource, 'scan-sources');

  if (scan.degraded_sources && scan.degraded_sources.length) {
    line(`⚠ Barely represented this run (possible feed failure): ${scan.degraded_sources.join(', ')}`, 'scan-warning');
  }
  if (scan.errors && scan.errors.length) {
    line(`⚠ ${scan.errors.length} feed request(s) failed outright: ${scan.errors.map(([url]) => url).join(', ')}`, 'scan-warning');
  }
}

// Last-refreshed stamp. Three timestamps are available and they are NOT interchangeable:
// scan.completed_utc is when THIS run's output actually became current (end of clustering/
// snippet-writing/entailment-checking); scan.fetched_utc is when feeds were READ, at the
// START of that same run -- using it alone means the banner reads several minutes stale the
// instant a user-triggered refresh finishes, since a run isn't instant. data.updated is the
// CSV's filesystem mtime, which resets to "now" if the file is ever copied, restored from
// backup, or synced, silently claiming a refresh that never happened. Prefer completed_utc,
// then fetched_utc, then mtime only when the raw-fetch JSON is absent entirely.
let refreshedAt = null;
// True when feeds have been re-read but the run has not produced output yet.
let runInProgress = false;

function agoText(then, now) {
  const mins = Math.floor((now - then) / 60000);
  if (mins < 0) return 'just now';          // clock skew between server and browser
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} ${hours === 1 ? 'hour' : 'hours'} ago`;
  const days = Math.floor(hours / 24);
  return `${days} ${days === 1 ? 'day' : 'days'} ago`;
}

function renderRefreshed() {
  if (!refreshedAt) {
    lastRefreshed.textContent = runInProgress ? 'Refreshing now\u2026' : '';
    if (runInProgress) {
      lastRefreshed.title = 'A refresh is running; no completed report exists yet.';
    } else {
      lastRefreshed.removeAttribute('title');
    }
    return;
  }
  const age = `Refreshed ${agoText(refreshedAt, new Date())}`;
  lastRefreshed.textContent = runInProgress ? `${age} \u00b7 refreshing now\u2026` : age;
  lastRefreshed.title = `Report last completed ${refreshedAt.toISOString().replace('T', ' ').slice(0, 16)} UTC`
    + ` (${refreshedAt.toLocaleString()} local)`
    + (runInProgress ? ' \u2014 a newer refresh is in progress; this age describes what you '
       + 'are currently reading, not the run underway.' : '');
}

function setRefreshed(data) {
  const scan = data.scan || {};
  // fetched_utc is DELIBERATELY not in this chain. It is written at the START of a run, so
  // during a run raw_headlines_<date>.json holds a brand-new fetched_utc and NO
  // completed_utc -- while the digest on screen is still the PREVIOUS run's. Falling back
  // to it made the banner read "Refreshed just now" over two-hour-old content the instant a
  // refresh began (observed 2026-07-30 14:40: fetched_utc 18:40 UTC, completed_utc null,
  // digest files from 12:46). Use only stamps that mean "this content became current":
  // completed_utc, else the CSV mtime.
  const stamp = scan.completed_utc || data.updated || null;
  const parsed = stamp ? new Date(stamp) : null;
  refreshedAt = (parsed && !isNaN(parsed.getTime())) ? parsed : null;
  // A run in flight is exactly the case where the two stamps disagree: feeds have been
  // re-read but no new output exists yet. Say so, instead of quietly showing an age that
  // implies the content is newer than it is.
  const fetched = scan.fetched_utc ? new Date(scan.fetched_utc) : null;
  const fetchedOk = fetched && !isNaN(fetched.getTime());
  // data.refreshRunning is the SERVER's own answer, and it wins when present. The stamp
  // comparison is only a proxy: fetched_utc is written when a run STARTS and completed_utc
  // when it finishes, so a run that DIES in between leaves fetched set and completed null
  // forever, and this banner claims "refreshing now" with nothing running. Observed
  // 2026-08-04: a run began 04:55 local, died ~34 min in, and the page still said a refresh
  // was underway two hours later. An older server omits the field, so fall back to the proxy
  // rather than reporting every run as idle.
  runInProgress = (typeof data.refreshRunning === 'boolean')
    ? data.refreshRunning
    : !!(fetchedOk && (!refreshedAt || fetched > refreshedAt));
  renderRefreshed();
}

// Re-tick so a tab left open does not keep insisting the report is "2 min ago" an hour later.
setInterval(renderRefreshed, 60000);

// The masthead/dek/about copy quotes the panel size and freshness window in prose -- those
// numbers change the moment a setting is saved (add an outlet, and every future report has a
// different size), so they're spans updated here rather than text baked into the HTML, which
// would keep claiming the OLD panel size forever regardless of what the pipeline actually did.
function setCountSpans(selector, value) {
  if (value == null) return;
  document.querySelectorAll(selector).forEach(el => { el.textContent = value; });
}

// Tracks the report's identity across calls to load() -- which fires from three places (the
// initial page load, the Refresh button's own completion, and the visibilitychange handler
// silently catching up a backgrounded tab). A toast on every call would fire on the initial
// load (nothing to announce yet) and on every no-op tab-focus re-check (nothing changed,
// most of the time) -- so it only fires when the resolved timestamp actually differs from
// the last one THIS PAGE saw, which is true exactly when a refresh (from this tab, another
// tab, or elsewhere) produced a genuinely newer report.
let lastKnownRefreshStamp = null;

async function load() {
  const response = await fetch('/api/digest'); const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || 'Could not load the digest.');
  rows = data.rows;
  document.querySelector('#report-date').textContent = `Report date · ${data.date}`;
  refresh.title = data.refreshConfigured ? 'Run the configured pipeline' : 'Pipeline command has not been configured yet';
  renderProfiles(data.routing);
  setRefreshed(data);
  const newStamp = refreshedAt ? refreshedAt.getTime() : null;
  if (lastKnownRefreshStamp !== null && newStamp !== null && newStamp !== lastKnownRefreshStamp) {
    showToast('New report loaded.');
  }
  lastKnownRefreshStamp = newStamp;
  renderScan(data.scan);
  renderCoverageChart(rows);
  setCountSpans('.panel-count', rows[0] && rows[0].panel_size);
  setCountSpans('.maxage-count', data.scan && data.scan.max_age_hours);
  render(search.value);
}

function wait(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

// --- Settings dialog: appearance/themes (unlocked, local-only) + outlet panel / pipeline
// knobs (password-gated, persisted server-side via /api/config). --------------------
const settingsDialog = document.querySelector('#settings-dialog');
const themeStylesheet = document.querySelector('#theme-stylesheet');
const settingsLocked = document.querySelector('#settings-locked');
const settingsUnlocked = document.querySelector('#settings-unlocked');
const settingsPassword = document.querySelector('#settings-password');
const settingsLockError = document.querySelector('#settings-lock-error');
const settingsSaveError = document.querySelector('#settings-save-error');
const outletList = document.querySelector('#outlet-list');
const removedWrap = document.querySelector('#removed-defaults-wrap');
const removedList = document.querySelector('#removed-list');
const addOutletName = document.querySelector('#add-outlet-name');
const addOutletDomain = document.querySelector('#add-outlet-domain');
const settingModel = document.querySelector('#setting-model');
const settingTopN = document.querySelector('#setting-topn');
const settingMaxAge = document.querySelector('#setting-maxage');

// --- Themes. Built-in themes (light/dark/slate/sepia) are full hand-picked palettes
// declared in styles.css; 'none' just turns the stylesheet off. 'custom' is the odd one out:
// only its 6 core colors are ever stored (from the 6 pickers below), and the other ~9
// shades used across the site are derived automatically via the color-mix() rules in
// styles.css's [data-theme="custom"] block -- so a saved custom theme stays a plain
// 6-color object, not a 15-value one, matching "customize quickly" as the actual goal. ---
const CORE_VARS = ['paper', 'ink', 'accent', 'dark', 'muted', 'line'];
const BUILTIN_THEMES = ['light', 'dark', 'slate', 'sepia', 'none'];
const themeSwatches = document.querySelectorAll('.theme-swatch');
const customInputs = {};
CORE_VARS.forEach(k => { customInputs[k] = document.querySelector(`#custom-${k}`); });
const customThemeName = document.querySelector('#custom-theme-name');
const savedThemesList = document.querySelector('#saved-themes-list');

function markActiveSwatch(themeId) {
  themeSwatches.forEach(btn => btn.classList.toggle('active', btn.dataset.themeId === themeId));
}

function applyTheme(themeId) {
  document.documentElement.dataset.theme = themeId;
  themeStylesheet.disabled = (themeId === 'none');
  if (themeId !== 'custom') CORE_VARS.forEach(k => document.documentElement.style.removeProperty(`--${k}`));
  localStorage.setItem('newsroom-theme', themeId);
  markActiveSwatch(BUILTIN_THEMES.includes(themeId) ? themeId : '');
}

function applyCustomPalette(colors, { persist = true } = {}) {
  document.documentElement.dataset.theme = 'custom';
  themeStylesheet.disabled = false;
  CORE_VARS.forEach(k => { if (colors[k]) document.documentElement.style.setProperty(`--${k}`, colors[k]); });
  if (persist) {
    localStorage.setItem('newsroom-theme', 'custom');
    localStorage.setItem('newsroom-active-custom', JSON.stringify(colors));
  }
  markActiveSwatch('');
}

function currentCoreColors() {
  const computed = getComputedStyle(document.documentElement);
  const out = {};
  CORE_VARS.forEach(k => { out[k] = computed.getPropertyValue(`--${k}`).trim(); });
  return out;
}

function fillCustomInputs(colors) {
  CORE_VARS.forEach(k => { if (colors[k]) customInputs[k].value = colors[k]; });
}

function loadCustomThemes() {
  try { return JSON.parse(localStorage.getItem('newsroom-custom-themes') || '{}'); }
  catch (err) { return {}; }
}
function saveCustomThemes(themes) { localStorage.setItem('newsroom-custom-themes', JSON.stringify(themes)); }

function renderSavedThemes() {
  const themes = loadCustomThemes();
  savedThemesList.replaceChildren();
  Object.keys(themes).forEach(name => {
    const li = document.createElement('li');
    const label = document.createElement('span'); label.textContent = name;
    const load = document.createElement('button'); load.type = 'button'; load.textContent = 'Load';
    load.addEventListener('click', () => { applyCustomPalette(themes[name]); fillCustomInputs(themes[name]); });
    const del = document.createElement('button'); del.type = 'button'; del.textContent = 'Delete';
    del.addEventListener('click', () => { delete themes[name]; saveCustomThemes(themes); renderSavedThemes(); });
    li.append(label, load, del);
    savedThemesList.append(li);
  });
}

themeSwatches.forEach(btn => btn.addEventListener('click', () => applyTheme(btn.dataset.themeId)));
markActiveSwatch(document.documentElement.dataset.theme || 'light');

document.querySelector('#customize-details').addEventListener('toggle', event => {
  if (event.target.open) fillCustomInputs(currentCoreColors());
});
CORE_VARS.forEach(k => customInputs[k].addEventListener('input', () => applyCustomPalette(currentInputColors())));
function currentInputColors() {
  const out = {};
  CORE_VARS.forEach(k => { out[k] = customInputs[k].value; });
  return out;
}

document.querySelector('#save-theme-btn').addEventListener('click', () => {
  const name = customThemeName.value.trim();
  if (!name) return;
  const themes = loadCustomThemes();
  themes[name] = currentInputColors();
  saveCustomThemes(themes);
  customThemeName.value = '';
  renderSavedThemes();
});

renderSavedThemes();

// Unlocking resets on every page load (this flag is never persisted) -- a deliberate
// tradeoff for "simple password": low friction within one visit, no permanent bypass.
let unlocked = false;
let activeOutlets = [];   // [{name, source: 'default'|'added', fetch, domain}]
let removedDefaults = []; // [name, ...]
// Whether activeOutlets/removedDefaults reflect a SUCCESSFUL read of /api/config.
// It starts false and is only set by loadSettingsForm(). Saving posts the outlet
// lists as the complete new truth -- removed_outlets and added_outlets REPLACE the
// stored config rather than patching it -- so posting them while they are still the
// empty initial value silently deletes every custom outlet and restores every
// removed default. The server cannot catch this: {removed:[], added:[]} is exactly
// what a legitimate "reset to defaults" looks like, and it passes validate_config.
// Hence the guard has to live here, at the only place that knows the difference
// between "the user cleared the list" and "the list never loaded".
let settingsLoaded = false;

function outletRow(name, buttonLabel, onClick, tag = '') {
  const li = document.createElement('li');
  const label = document.createElement('span'); label.textContent = name;
  li.append(label);
  if (tag) { const t = document.createElement('span'); t.className = 'outlet-tag'; t.textContent = tag; li.append(t); }
  const btn = document.createElement('button'); btn.type = 'button'; btn.textContent = buttonLabel;
  btn.addEventListener('click', onClick);
  li.append(btn);
  return li;
}

function fetchTag(o) {
  if (o.fetch === 'direct') return 'direct feed';
  if (o.domain) return `google news · ${o.domain}`;
  return 'google news';
}

function renderOutlets() {
  outletList.replaceChildren();
  activeOutlets.forEach(o => {
    outletList.append(outletRow(o.name, 'Remove', () => {
      activeOutlets = activeOutlets.filter(x => x.name !== o.name);
      if (o.source === 'default') removedDefaults.push(o.name);
      renderOutlets();
    }, fetchTag(o)));
  });
  removedWrap.hidden = removedDefaults.length === 0;
  removedList.replaceChildren();
  removedDefaults.forEach(name => {
    removedList.append(outletRow(name, 'Restore', () => {
      removedDefaults = removedDefaults.filter(x => x !== name);
      activeOutlets.push({ name, source: 'default', fetch: null, domain: null });
      renderOutlets();
    }));
  });
}

async function loadSettingsForm() {
  // Clear the flag FIRST: if any step below throws, the panel must not be left
  // marked as loaded while showing whatever the previous read put there.
  settingsLoaded = false;
  const response = await fetch('/api/config');
  if (!response.ok) throw new Error(`/api/config returned ${response.status}`);
  const data = await response.json();
  if (!Array.isArray(data.active_outlets)) {
    // A 200 carrying no outlet array is a malformed read, not an empty panel --
    // `|| []` below would otherwise turn it into a convincing-looking wipe.
    throw new Error('/api/config returned no outlet list');
  }
  activeOutlets = data.active_outlets;
  removedDefaults = data.removed_defaults || [];
  settingModel.value = data.model || '';
  settingTopN.value = data.top_n || '';
  settingMaxAge.value = data.max_age_hours || '';
  renderOutlets();
  settingsLoaded = true;
}

document.querySelector('#settings-btn').addEventListener('click', async () => {
  settingsDialog.showModal();
  if (!unlocked) return;
  // Reopening the panel re-reads config. This was previously fire-and-forget: a
  // failed read left the outlet lists stale or empty with nothing on screen to
  // say so, and the next Save wrote that emptiness back.
  try { await loadSettingsForm(); }
  catch (err) {
    settingsSaveError.textContent = 'Could not load current settings -- saving is disabled until this loads. Close and reopen the panel.';
    settingsSaveError.hidden = false;
  }
});

const settingsUnlockBtn = document.querySelector('#settings-unlock');

settingsUnlockBtn.addEventListener('click', async () => {
  settingsLockError.hidden = true;
  if (!settingsPassword.value) {
    settingsLockError.textContent = 'Enter the password.';
    settingsLockError.hidden = false;
    return;
  }

  // The password is verified by the SERVER before the panel opens. This used to set
  // unlocked = true on any non-empty string, on the reasoning that the client gate was
  // presentation only since /api/config re-checks on save. That was true but misleading: the
  // panel opened to a wrong password and only rejected it later, which reads as a broken
  // rotation rather than a wrong password.
  //
  // /api/unlock checks and does nothing else -- it neither reveals settings nor changes state.
  settingsUnlockBtn.disabled = true;
  const priorLabel = settingsUnlockBtn.textContent;
  settingsUnlockBtn.textContent = 'Checking...';
  try {
    const response = await fetch('/api/unlock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: settingsPassword.value }),
    });
    if (response.status === 401) {
      settingsLockError.textContent = 'Incorrect password.';
      settingsLockError.hidden = false;
      settingsPassword.select();
      return;
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      settingsLockError.textContent = payload.error || `Could not check the password (${response.status}).`;
      settingsLockError.hidden = false;
      return;
    }
  } catch (err) {
    // A network failure must NOT open the panel. Failing closed matters here: the alternative
    // is that losing the tunnel unlocks settings for anyone who can load the page.
    settingsLockError.textContent = 'Could not reach the server to check the password.';
    settingsLockError.hidden = false;
    return;
  } finally {
    settingsUnlockBtn.disabled = false;
    settingsUnlockBtn.textContent = priorLabel;
  }

  unlocked = true;
  settingsLocked.hidden = true;
  settingsUnlocked.hidden = false;
  try { await loadSettingsForm(); }
  catch (err) {
    settingsLockError.textContent = 'Could not load current settings -- saving is disabled until this loads.';
    settingsLockError.hidden = false;
  }
});

document.querySelector('#add-outlet-btn').addEventListener('click', () => {
  const name = addOutletName.value.trim(), domain = addOutletDomain.value.trim().toLowerCase();
  if (!name || !domain) return;
  if (removedDefaults.includes(name)) {
    // Re-adding a name that matches a removed default restores its original route --
    // the server applies the same rule, this just keeps the panel's own display in sync.
    removedDefaults = removedDefaults.filter(x => x !== name);
    activeOutlets.push({ name, source: 'default', fetch: null, domain: null });
  } else if (!activeOutlets.some(o => o.name === name)) {
    activeOutlets.push({ name, source: 'added', fetch: 'google_news', domain });
  }
  addOutletName.value = ''; addOutletDomain.value = '';
  renderOutlets();
});

document.querySelector('#settings-save').addEventListener('click', async () => {
  settingsSaveError.hidden = true;
  if (!settingsLoaded) {
    // Refuse rather than post. Everything below treats activeOutlets as the complete
    // new outlet set, so saving from an unloaded panel is a silent delete of every
    // custom outlet -- the failure the user would only notice days later, as sources
    // quietly missing from the digest.
    settingsSaveError.textContent = 'Settings have not loaded yet, so saving would erase your outlet list. Close and reopen the panel.';
    settingsSaveError.hidden = false;
    return;
  }
  const body = {
    password: settingsPassword.value,
    removed_outlets: removedDefaults,
    added_outlets: activeOutlets.filter(o => o.source === 'added').map(o => ({ name: o.name, domain: o.domain })),
    model: settingModel.value.trim(),
    top_n: Number(settingTopN.value) || null,
    max_age_hours: Number(settingMaxAge.value) || null,
  };
  try {
    const response = await fetch('/api/config', { method: 'POST', body: JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not save settings.');
    // Same rule as loadSettingsForm: only adopt an outlet list that actually arrived.
    // A malformed 200 here would otherwise leave the panel looking empty and armed
    // to wipe on the next save.
    if (Array.isArray(data.active_outlets)) {
      activeOutlets = data.active_outlets;
      removedDefaults = data.removed_defaults || [];
      renderOutlets();
    } else {
      settingsLoaded = false;
    }
  } catch (err) {
    settingsSaveError.textContent = err.message;
    settingsSaveError.hidden = false;
  }
});

search.addEventListener('input', () => render(search.value));
// Routing profile dropdown. The server is the only source of truth for which profiles
// exist and what they cost -- this never hardcodes a profile name or a price, because a
// UI that disagrees with routing.json about what a run costs is worse than no UI.
// Absent or empty routing (older server, unparseable routing.json) leaves the control
// hidden, so the page degrades to exactly its previous behaviour.
let profilesRendered = false;
// The routing payload from the last /api/digest. The refresh button needs it to describe
// the run it is about to start, now that it no longer sits beside the dropdown.
let lastRouting = null;
function renderProfiles(routing) {
  lastRouting = routing || null;
  if (!routing || !Array.isArray(routing.profiles) || !routing.profiles.length) {
    profileWrap.hidden = true;
    return;
  }
  profileWrap.hidden = false;
  // Rebuild only once: re-rendering on every poll would discard a selection the user
  // made while a refresh was running.
  if (!profilesRendered) {
    profileSelect.innerHTML = '';
    for (const p of routing.profiles) {
      const opt = document.createElement('option');
      opt.value = p.name;
      const cost = typeof p.cost === 'number'
        ? (p.cost === 0 ? 'free' : `$${p.cost.toFixed(4)}`)
        : '';
      opt.textContent = cost ? `${p.name} — ${cost}` : p.name;
      // Full rationale on hover, including which stages get metered.
      const stages = (p.claude_stages || []).length
        ? `Metered stages: ${p.claude_stages.join(', ')}.`
        : 'Nothing metered — every stage runs on the local GPU, which is slower.';
      opt.title = `${stages}\n\n${p.why || ''}`.trim();
      profileSelect.appendChild(opt);
    }
    if (routing.active) profileSelect.value = routing.active;
    profilesRendered = true;
  }
  updateProfileHint();
}

function updateProfileHint() {
  const opt = profileSelect.selectedOptions[0];
  profileSelect.title = opt ? opt.title : '';
}
profileSelect.addEventListener('change', updateProfileHint);

// Starting a run, shared by the page's Refresh button and the locked panel's routing
// control. `body` is whatever the caller wants to send: {} for the configured default,
// or {profile, password} for a metered run. Polls until the run ends, then reloads.
async function startRun(body, eta, controls) {
  for (const el of controls) el.disabled = true;
  refresh.innerHTML = '<span aria-hidden="true">↻</span> Refreshing…';
  showNotice(eta);
  try {
    const start = await fetch('/api/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const startData = await start.json();
    if (!start.ok) throw new Error(startData.error || 'Refresh failed.');
    let state = startData;
    while (state.running) { await wait(4000); state = await (await fetch('/api/refresh')).json(); }
    if (state.error) throw new Error(state.error);
    await load(); showNotice('');
  }
  catch (error) { showNotice(error.message); }
  finally {
    for (const el of controls) el.disabled = false;
    refresh.innerHTML = '<span aria-hidden="true">↻</span> Refresh report';
  }
}

// The page's own Refresh button sends NO profile, so the server runs routing.json's
// active_profile. It cannot start a metered run by itself: if active_profile is metered
// the server answers 401 and the message below points at the settings panel. Choosing a
// profile lives behind the password because a metered run spends money.
refresh.addEventListener('click', () => {
  const active = (lastRouting && lastRouting.active) || null;
  const entry = (lastRouting && (lastRouting.profiles || []).find(p => p.name === active)) || null;
  // Set expectations without quoting a number. Local run time moves with the model, the
  // llama-swap --parallel setting and top_n, so any figure here goes stale silently and a
  // user who sees it overshot thinks the run hung. Say which choice is slower instead.
  const eta = entry && entry.metered
    ? 'Fetching, clustering, and writing the report. This is the quicker option.'
    : 'Running every stage on the local GPU — no API spend, but considerably slower. Leave it going.';
  return startRun({}, eta, [refresh]);
});

// Metered runs start here, from inside the unlocked panel, and carry the password. The
// server re-checks it -- this is not the gate, just the way to satisfy it.
profileRun.addEventListener('click', () => {
  const chosen = profileSelect.value || null;
  if (!chosen) return;
  const opt = profileSelect.selectedOptions[0];
  const metered = opt && !/—\s*free/.test(opt.textContent);
  const eta = metered
    ? `Running ${chosen} — metered stages billed to the API. This is the quicker option.`
    : `Running ${chosen} on the local GPU — no API spend, but considerably slower. Leave it going.`;
  settingsDialog.close();
  return startRun({ profile: chosen, password: settingsPassword.value },
                  eta, [refresh, profileSelect, profileRun]);
});

// A tab left open only ever sees new data when IT triggers a refresh -- switch away, let a
// refresh happen elsewhere (another tab, the settings panel, the boot-time schedule) and
// come back, and this tab keeps showing whatever it loaded last. Re-fetching on regained
// visibility catches it up silently; errors here are swallowed rather than shown, since a
// background catch-up failing shouldn't blank out a page that's already displaying fine.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') load().catch(() => {});
});

load().catch(error => showNotice(error.message));
