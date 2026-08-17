# Newsroom Digest — browser extension

> **Installing or building it? Read [INSTALL.md](INSTALL.md).**
> Download `newsroom-digest-source.zip` and extract it to an EMPTY folder -- collecting
> files individually gives you a mix of versions, and the build will refuse.

A **read-only client** for a Newsroom server. It does no summarising itself: a browser cannot
run the pipeline (five stages, hundreds of model calls, roughly an hour on local models), so
this fetches a digest the server has already produced.

## Install (unpacked)

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. **Load unpacked** → select this `extension` folder

The digest replaces your new tab page, and the toolbar icon opens the same list in a popup.

## Settings

Right-click the icon → Options, or the *Settings* link on either surface.

- **Digest server** — which instance to read. Defaults to `http://127.0.0.1:8767`, which
  works out of the box against a newsroom server on the same machine.

  To read an instance on another host, add it to `host_permissions` in
  `manifest.chrome.json` *and* `manifest.firefox.json`, then rebuild:

  ```json
  "host_permissions": ["https://newsroom.example.com/*", "http://localhost/*", "http://127.0.0.1/*"]
  ```

  Setting the URL in Options alone is not enough. The extension reads the digest with a
  privileged cross-origin request, and the browser only grants that for hosts named in the
  manifest — the server sends no CORS headers. A broad `https://*/*` pattern does *not*
  work as a substitute: Firefox treats it as an opt-in "all sites" permission and, even
  once granted, still refused the request in testing (Firefox 153), leaving the digest
  blank with a CORS error in the console.
- **Story counts** — separately for the popup and the new tab page.

## Refresh

*Refresh now* starts a pipeline run on the server. Two deliberate constraints:

- It only ever offers a **non-metered** profile — one that spends nothing on the API. The
  extension holds no password and is not designed to; metered profiles are password-gated
  server-side and stay on the site.
- The profile is always sent **explicitly**. Omitting it would make the server fall back to
  its configured default, which may be a metered one — an accidental charge.

Nothing runs on a schedule. A run happens when someone presses a button.

## Staleness

A digest older than 12 hours is called out on screen. This matters because nothing refreshes
automatically: without the warning, a new tab page would present yesterday's stories as today's
news. Freshness is measured from the run's *completion*, not from when it started reading
feeds — the start time predates the content by the length of the run.

## Files

## Two browsers, one codebase

Chrome and Firefox are built from the same source tree. They differ **only** in the manifest:

| | Chrome | Firefox |
|---|---|---|
| Manifest | `manifest.chrome.json` | `manifest.firefox.json` |
| Extension id | assigned by Chrome | must be declared: AMO does not assign one for MV3 |
| `browser_specific_settings` | rejected as unrecognised | required |
| Settings page | `options_ui` | `options_ui` |

`options_ui` is used rather than `options_page` because it works on both; `options_page` is not
part of Firefox's supported set. `chrome_url_overrides` keeps its `chrome_` prefix on Firefox —
the name is historical, and the key is supported there.

The one genuine API difference is that Firefox exposes a promise-based `browser.*` while Chrome
exposes a callback-based `chrome.*`. That is handled in `compat.js` and nowhere else, so no other
file carries a browser branch. The build refuses if any file reaches the extension API directly.

### Build

```sh
python build.py          # dist/chrome/ and dist/firefox/
python build.py --zip    # also the two archives
```

The build lints before it writes, and refuses on: a missing file, `compat.js` loaded after the
code that needs it, a direct `chrome.*` call outside the shim, a Firefox manifest without an
extension id, a Chrome manifest carrying `browser_specific_settings`, or a version skew between
the two. Each of those refusals is exercised by deliberately breaking the tree and confirming the
build stops.

### Install

**Chrome** — `chrome://extensions` → Developer mode → Load unpacked → `dist/chrome`

**Firefox** — `about:debugging#/runtime/this-firefox` → Load Temporary Add-on → any file inside
`dist/firefox`. Temporary add-ons are dropped when Firefox restarts; for a permanent install the
package needs signing through addons.mozilla.org.

Firefox's new-tab override asks for confirmation the first time, and offers a one-click way back
to the built-in page — that prompt is Firefox's, not this extension's.

## Refresh: one run at a time

The button starts a real run on your server. Runs cannot stack, and this is enforced in two
places because either one alone would be insufficient:

**Server side** (`server.py`) holds a lock acquired without blocking. A second request gets
**409** with a message rather than queueing, and the lock is released in a `finally` so a crashed
or timed-out run cannot wedge the endpoint permanently. This is the guarantee that matters —
anyone can POST to the endpoint, and this extension is only one of several possible callers.

**Client side** (`digest.js`) treats a 409 as *information, not an error*. A refusal usually means
a run the user already started — here, on the site, or in another tab — so the button reports it
plainly and then attaches to the run in progress. A 401 or 400 stays a real error; conflating
them would report a run that never started.

Progress is polled from `GET /api/refresh` every 15s, so an in-flight run is picked up however it
was started, and closing the window does not stop it.

The extension only ever offers **free** profiles. It holds no password and should not: the `local`
profile costs nothing and the server deliberately leaves it open, while every metered profile is
password-gated. The profile is always sent explicitly — omitting it makes the server fall back to
`routing.json`'s active profile, which may be a metered one.

## Tests

```sh
sh run_tests.sh                                  # shim, refresh client, build lint
python -m pytest test_refresh_concurrency.py -q  # from the repo root
```

`test_compat.mjs` runs every assertion twice, once against each browser's API style, because a
Chrome-only run cannot catch a Firefox-only break. `test_refresh_client.mjs` drives the shipped
`startRun` against a real HTTP server that mimics the endpoint's contract, including eight
simultaneous clicks starting exactly one run. `test_refresh_concurrency.py` covers the server
guard over a real socket.

All of these were checked by mutation: each guard was deliberately broken and the corresponding
test confirmed to fail. A test that has never failed has not been shown to work.


## Per-outlet links

Each story shows up to **three outlet chips**, and each one links to *that outlet's own
article* on the story — not the single featured link. Outlets that covered the story closest
(the reporting the snippet was drawn from) come first and are filled in; outlets that only
also carried it are outlined. A `+n` chip names the rest on hover.

The links come from `outlet_links` in the digest payload, so this costs no extra request. It
arrives as a JSON *string* rather than an object because it round-trips through the run's CSV
— `parseOutletLinks` handles both, and a malformed value degrades to unlinked names rather
than dead links.

**Changing the cap of three:** it is the `.slice(0, 3)` in `digest.js`'s `storyNode`. Three
was chosen because a story runs on up to 19 outlets and 24 stories x 19 chips buries the
headlines the list exists to show. `test_outlet_chips.mjs` asserts the cap, so raise the
number there too or the suite will fail — deliberately, so the number cannot drift silently.
