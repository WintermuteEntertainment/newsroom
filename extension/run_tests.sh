#!/bin/sh
# Everything that can be checked without a browser.
#
# The JS suites need node only as a test harness -- the extension itself has no build
# dependency beyond build.py, and node is not required to load or ship it.
set -e
NODE=${NODE:-node}

echo "== compat shim (both browser API styles)"
"$NODE" test_compat.mjs

echo "== refresh client against a live server"
"$NODE" test_refresh_client.mjs
"$NODE" test_outlet_chips.mjs

echo "== build (lints sources, then writes both packages)"
python3 build.py --zip

echo
echo "== server-side concurrency guard"
echo "   run from the repo root:  python -m pytest test_refresh_concurrency.py -q"
