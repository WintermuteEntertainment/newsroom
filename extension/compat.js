// Cross-browser storage and runtime access.
//
// Firefox exposes a promise-based `browser.*`; Chrome exposes a callback-based `chrome.*`.
// Firefox also provides a `chrome.*` alias, so callbacks would technically work everywhere --
// but normalising to promises in ONE place means no other file needs a browser branch, and
// the two builds share byte-identical logic.
const ext = globalThis.browser ?? globalThis.chrome;
const usesPromises = typeof globalThis.browser !== 'undefined';

const store = (area) => ({
  get(defaults) {
    if (usesPromises) return ext.storage[area].get(defaults);
    return new Promise((resolve) => ext.storage[area].get(defaults, resolve));
  },
  set(items) {
    if (usesPromises) return ext.storage[area].set(items);
    return new Promise((resolve) => ext.storage[area].set(items, resolve));
  },
  clear() {
    if (usesPromises) return ext.storage[area].clear();
    return new Promise((resolve) => ext.storage[area].clear(resolve));
  },
});

const storage = { sync: store('sync'), local: store('local') };

function openOptions() {
  return ext.runtime.openOptionsPage();
}
