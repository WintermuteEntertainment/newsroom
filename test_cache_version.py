"""Guard: a changed static asset must carry a bumped ?v= cache key.

Cloudflare edge-cached app.js for 4h (observed cf-cache-status HIT, age 1869,
max-age 14400). server.py now sends no-store, but that only prevents NEW stale
entries -- a copy already at the edge is only evicted by a fresh cache key. So
index.html references assets as app.js?v=N, and N must move whenever the asset
does. This has been missed twice:

  - b5113fc changed styles.css (dropdown CSS) and left v=7 from 46d6d25
  - 2923d6b changed app.js (banner fix) and left v=8 from ffa186e

The second one would have served the dropdown WITHOUT the banner fix to any
browser holding a cached app.js?v=8. This test makes that a failure, not a
discovery weeks later.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent
ASSETS = ("app.js", "styles.css")


def _git(*args):
    return subprocess.run(
        ["git", "-c", f"safe.directory={REPO}", *args],
        cwd=REPO, capture_output=True, text=True).stdout.strip()


def _version_in_html(asset):
    html = (REPO / "index.html").read_text(encoding="utf-8")
    m = re.search(rf"{re.escape(asset)}\?v=(\d+)", html)
    assert m, f"index.html does not reference {asset} with a ?v= cache key"
    return int(m.group(1))


def test_every_asset_is_referenced_with_a_version():
    for asset in ASSETS:
        assert _version_in_html(asset) > 0


def test_asset_not_changed_more_recently_than_its_cache_key():
    """The commit that last touched the asset must not be newer than the one
    that last touched its ?v= reference."""
    stale = []
    for asset in ASSETS:
        v = _version_in_html(asset)
        asset_commit = _git("log", "-1", "--format=%ct", "--", asset)
        key_commit = _git("log", "-1", "-S", f"{asset}?v={v}", "--format=%ct", "--", "index.html")
        if not asset_commit or not key_commit:
            continue  # asset or key not in history yet (new file)
        if int(asset_commit) > int(key_commit):
            stale.append(
                f"{asset} last changed at {asset_commit} but ?v={v} was set at "
                f"{key_commit} -- bump it or an edge-cached copy survives")
    assert not stale, "; ".join(stale)


def test_versions_are_unique_per_asset_reference():
    """A duplicated key across assets is legal but makes the staleness check
    ambiguous, since -S matches the wrong line."""
    html = (REPO / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r"(\w+\.\w+)\?v=(\d+)", html)
    assert refs, "no versioned asset references found in index.html"
    for asset, v in refs:
        assert html.count(f"{asset}?v={v}") == 1, f"{asset}?v={v} appears more than once"
