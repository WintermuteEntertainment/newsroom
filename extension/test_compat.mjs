// compat.js is the only file in the extension with a browser branch in it, which makes it the
// only place where a mistake means the extension silently fails on ONE browser and works on
// the other. A Chrome-only test run would never catch a Firefox-only break, so both API
// styles are exercised here against the same assertions.
//
//   node test_compat.mjs
//
// Firefox exposes promise-based browser.*; Chrome exposes callback-based chrome.*. (Chrome MV3
// also supports promises for storage, but the callback path is kept so the shim works on older
// Chromium builds too, and it is tested because it is shipped.)
import fs from 'node:fs';
import assert from 'node:assert/strict';

const source = fs.readFileSync(new URL('./compat.js', import.meta.url), 'utf8');

// compat.js reads globalThis, as it must -- so the fakes are installed there rather than passed
// in, or this would exercise a different lookup than a browser performs.
function loadWith(globals) {
  const saved = { browser: globalThis.browser, chrome: globalThis.chrome };
  globalThis.browser = globals.browser;
  globalThis.chrome = globals.chrome;
  try {
    return new Function(`${source}\nreturn { storage, openOptions };`)();
  } finally {
    globalThis.browser = saved.browser;
    globalThis.chrome = saved.chrome;
  }
}

function firefoxLike() {
  const data = { sync: {}, local: {} };
  const calls = [];
  const area = (name) => ({
    async get(defaults) {
      calls.push(`${name}.get`);
      const out = { ...(defaults || {}) };
      for (const k of Object.keys(out)) if (k in data[name]) out[k] = data[name][k];
      return out;
    },
    async set(items) { calls.push(`${name}.set`); Object.assign(data[name], items); },
    async clear() { calls.push(`${name}.clear`); for (const k of Object.keys(data[name])) delete data[name][k]; },
  });
  return {
    globals: {
      browser: {
        storage: { sync: area('sync'), local: area('local') },
        runtime: { openOptionsPage: async () => { calls.push('openOptions'); return true; } },
      },
      chrome: undefined,
    },
    calls,
  };
}

function chromeLike() {
  const data = { sync: {}, local: {} };
  const calls = [];
  const area = (name) => ({
    get(defaults, cb) {
      calls.push(`${name}.get`);
      const out = { ...(defaults || {}) };
      for (const k of Object.keys(out)) if (k in data[name]) out[k] = data[name][k];
      setTimeout(() => cb(out), 0);
    },
    set(items, cb) { calls.push(`${name}.set`); Object.assign(data[name], items); setTimeout(cb, 0); },
    clear(cb) { calls.push(`${name}.clear`); for (const k of Object.keys(data[name])) delete data[name][k]; setTimeout(cb, 0); },
  });
  return {
    globals: {
      browser: undefined,
      chrome: {
        storage: { sync: area('sync'), local: area('local') },
        runtime: { openOptionsPage: () => { calls.push('openOptions'); return true; } },
      },
    },
    calls,
  };
}

const passed = [];
for (const [label, make] of [['firefox (promise browser.*)', firefoxLike],
                             ['chrome  (callback chrome.*)', chromeLike]]) {
  const env = make();
  const { storage, openOptions } = loadWith(env.globals);

  // Defaults come back when nothing is stored.
  const empty = await storage.sync.get({ endpoint: 'https://example.test', count: 12 });
  assert.equal(empty.endpoint, 'https://example.test', `${label}: defaults not returned`);
  assert.equal(empty.count, 12, `${label}: default count wrong`);

  // A stored value overrides its default without losing the others.
  await storage.sync.set({ count: 30 });
  const stored = await storage.sync.get({ endpoint: 'https://example.test', count: 12 });
  assert.equal(stored.count, 30, `${label}: stored value not returned`);
  assert.equal(stored.endpoint, 'https://example.test', `${label}: default lost alongside a stored value`);

  // The exact cache pattern digest.js uses: a computed key defaulting to null.
  const key = 'cache:https://x.test';
  assert.equal((await storage.local.get({ [key]: null }))[key], null, `${label}: cache miss should be null`);
  await storage.local.set({ [key]: { at: 123, data: { rows: [] } } });
  assert.equal((await storage.local.get({ [key]: null }))[key].at, 123, `${label}: cache hit not returned`);

  // Clearing the cache must not touch settings. Saving new options does exactly this, so if
  // local.clear() reached sync the user's endpoint would be wiped every time they hit Save.
  await storage.local.clear();
  assert.equal((await storage.local.get({ [key]: null }))[key], null, `${label}: local.clear did not clear`);
  assert.equal((await storage.sync.get({ count: 12 })).count, 30, `${label}: local.clear wiped sync settings`);

  // Every method must return a thenable, or an `await` silently resolves to undefined and the
  // caller reads properties off nothing.
  assert.ok(typeof storage.sync.get({}).then === 'function', `${label}: get is not thenable`);
  assert.ok(typeof storage.sync.set({}).then === 'function', `${label}: set is not thenable`);
  assert.ok(typeof storage.local.clear().then === 'function', `${label}: clear is not thenable`);

  await openOptions();
  assert.ok(env.calls.includes('openOptions'), `${label}: openOptions did not reach the runtime`);

  passed.push(label);
}

for (const label of passed) console.log(`  PASS  ${label}`);

// Tree-wide: nothing outside compat.js may reach the extension API directly, or it reintroduces
// the callback/promise split this shim exists to remove.
for (const name of ['digest.js', 'options.js', 'newtab.js', 'popup.js']) {
  const body = fs.readFileSync(new URL(`./${name}`, import.meta.url), 'utf8');
  const leaks = body.match(/\b(chrome|browser)\.[a-zA-Z]/g) || [];
  assert.equal(leaks.length, 0, `${name} reaches the extension API directly: ${leaks}`);
}
console.log('  PASS  no direct extension-API calls outside compat.js');
