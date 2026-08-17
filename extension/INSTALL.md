# Installing the Newsroom digest extension

Two ways in. **Read "Which one do I want?" first** -- picking wrong is the single most common
way this goes sideways.

## Which one do I want?

| | You want | Download |
|---|---|---|
| **Just use it** | the extension in your browser | `newsroom-digest-chrome.zip` or `newsroom-digest-firefox.zip` |
| **Change it** | to edit the code and rebuild | `newsroom-digest-source.zip` |

The two are not interchangeable. The browser packages are **already built** -- there is no
`build.py` inside them and nothing to run. The source package is **not loadable** by a browser
as-is, because it contains two manifests and a browser needs exactly one named `manifest.json`.

## Just use it

You do not need Python, and you do not run `build.py`.

### Chrome / Edge / Brave

1. Unzip `newsroom-digest-chrome.zip` -- you get a folder with `manifest.json` in it.
2. Go to `chrome://extensions`.
3. Turn on **Developer mode** (top right).
4. Click **Load unpacked** and select the unzipped **folder** (not the zip, not a file inside it).

### Firefox

1. Unzip `newsroom-digest-firefox.zip`.
2. Go to `about:debugging#/runtime/this-firefox`.
3. Click **Load Temporary Add-on** and pick **any file** inside the folder.

Firefox drops temporary add-ons when it restarts -- that is Firefox's rule for unsigned
extensions, not a bug here. A permanent install needs the package signed through
addons.mozilla.org.

### Then

Your new tab becomes the digest, and the toolbar icon shows the same list in a popup. If it is
empty, open the extension's options and use **Test connection** -- an unreachable endpoint and
an empty digest look identical otherwise.

## Change it

Needs Python 3.9 or newer. Nothing else -- no npm, no build toolchain.

1. Download `newsroom-digest-source.zip`.
2. **Extract it to an empty folder.** Not into a folder that already has an older copy in it,
   and not into `Downloads` alongside other loose files.
3. From inside the extracted `extension` folder:

```
python build.py --zip
```

That writes `dist/chrome/` and `dist/firefox/`, plus a zip of each. Load whichever you need
using the steps above.

### Why the whole tree, and not individual files

Downloading files one at a time produces a folder where the files come from **different
versions**, which does not work and does not fail obviously. It shows up as a page of refusals:

```
build refused:
  - newtab.html does not load compat.js
  - digest.js calls chrome.* directly; route it through compat.js
  ...
```

Every one of those lines is true, and none of them is the actual problem. The actual problem is
that some files predate a change and others follow it.

`build.py` now detects this directly, using the checksum list (`SOURCES.sha256`) that ships
inside the source zip:

```
build refused:
  - this source tree does not match the released set, so the files come from different
    versions and will not work together:
      popup.js -- differs from the release
```

If you are editing the sources on purpose, delete `SOURCES.sha256`. The cross-browser checks
below keep working without it -- only the "did you assemble this correctly" check goes away.

### What the build refuses, and why each one matters

`build.py` will not produce a package with any of these in it. Each is a mistake that yields a
**silently broken extension in one browser only**, which is the expensive kind to debug:

| Refusal | Why it is fatal |
|---|---|
| a file does not load `compat.js` | `storage` is undefined at runtime; the page renders empty |
| `compat.js` loads *after* `digest.js` | same, but intermittent, which is worse |
| a file calls `chrome.*` directly | Firefox aliases `chrome.*` but its API returns promises, so the callback never fires -- Chrome works, Firefox silently does not |
| `manifest.firefox.json` has no `gecko.id` | addons.mozilla.org refuses to sign an MV3 extension without one |
| `manifest.chrome.json` has `browser_specific_settings` | Chrome rejects the entire manifest |
| the two manifests disagree on `version` | bug reports become ambiguous about which build is running |

### Tests

```
sh run_tests.sh
```

The JavaScript suites need Node only as a test harness -- the extension itself does not. If
Node is not on your PATH, pass it: `NODE=/path/to/node sh run_tests.sh`.

## Pointing it at your own server

Open the extension's options. The endpoint defaults to a specific public instance, so **if you
were given this extension by someone else, it is reading their server** until you change it.
Any host running the newsroom server works, including `http://127.0.0.1:8767`.
