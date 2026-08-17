# Newsroom

A daily news digest that scans major outlets, clusters the same story across them, ranks
by how widely it is being covered, and writes a plain-text snippet for each. Every stage
runs on a local GPU, so a full run costs nothing.

The web front end renders the ranked stories with coverage breadth, a source panel, and
outbound links to the original articles.

## Status

Working and in daily use. The pipeline runs fully locally at **$0.00 per run**; call loss
is **0.4%** (1 of 228 calls in the last measured run, down from 8.6%).

## How it works

Headlines are fetched from the configured outlets' RSS feeds, then passed through five
stages, each of which can be routed independently to either the local GPU or the Anthropic
API:

| stage | what it does |
|---|---|
| `fragment_merge` | decides whether two headlines describe the same story |
| `exclusives_dedup` | folds single-outlet duplicates together |
| `entailment_check` | verifies a snippet is supported by its sources |
| `snippet_writing` | writes the plain-text summary |
| `coherence_audit` | final pass over the assembled digest |

`routing.json` holds the per-stage routing plus named profiles (`local`, `fast`,
`safe-verify`, `claude`) that trade cost against speed. Profiles that bill the API are
password-protected; a fully-local refresh is free and needs no password.

## Requirements

- Python 3.11+ (`pip install -r requirements.txt`)
- For free local runs: a GPU running [llama-swap](https://github.com/mostlygeek/llama-swap)
  with an OpenAI-compatible endpoint (default `http://127.0.0.1:8001`)
- For metered runs: an Anthropic API key in `ANTHROPIC_API_KEY`

Local inference is optional in principle — the pipeline runs entirely on the API — but
that is what costs money, which is the whole reason for the local routing.

## Quick start

```bash
pip install -r requirements.txt
python claude_pipeline_runner.py --routing routing.json --profile local   # generate a digest
python server.py                                                          # view at :8767
```

## Configuration

Everything machine-specific is an environment variable, so a clone runs without editing
source:

| variable | default | what it does |
|---|---|---|
| `NEWSROOM_REFRESH_COMMAND` | (unset) | command the web Refresh button runs |
| `NEWSROOM_REFRESH_TIMEOUT` | `3600` | seconds before a run is abandoned |
| `NEWSROOM_MODEL` | `claude-haiku-4-5` | model for API-routed stages |
| `NEWSROOM_REMOTE` | `newsroom-host` | SSH host for `remote_refresh.py` |
| `NEWSROOM_REMOTE_DIR` | `~/newsroom` | that host's checkout path |
| `ANTHROPIC_API_KEY` | (unset) | required only for metered profiles |

Outlets are edited in the web UI (password-protected) and persist to
`outlets_config.json`, which is deliberately untracked — it is per-deployment state.

## Tests

```bash
python -m pytest -q
```

143 tests. The guard tests are worth knowing about, because each encodes a bug that
actually happened and each was verified to *fail* on the real pre-fix state:

- `test_refresh_gate.py` — the metered-run password gate, driven over a real socket
- `test_cache_version.py` — fails if `app.js`/`styles.css` change without a `?v=` bump
- `test_banner.py` — fails if the freshness stamp regresses to claiming a run finished
- `test_verdict_recovery.py` — pins the reply shapes local models actually emit, so a
  verdict parser cannot silently regress to dropping them

`test_run_eta_wording.py` reads `extension/dist/`, which is a build output — those tests
fail in a fresh clone until the extension is built.

## Deployment notes

### Running it locally for development

```bash
cd /path/to/newsroom
python server.py
```

Then open `http://127.0.0.1:8767`.

## Refreshing the report

`news_digest.py` expects a `host` object with `reasoning_model()` and `llm()` methods.
`claude_pipeline_runner.py` implements that against the Anthropic API (default model
`claude-haiku-4-5-20251001`, override with `NEWSROOM_MODEL`). `pipeline_runner.py` is the
original OpenAI-backed version, kept in case a real (non-ChatGPT-plan) OpenAI API key
becomes available.

To enable the **Refresh report** button, set `NEWSROOM_REFRESH_COMMAND` to the command that
runs your pipeline and writes a new `news_digest_YYYY-MM-DD.csv` here, e.g.:

```powershell
$env:NEWSROOM_REFRESH_COMMAND = 'python claude_pipeline_runner.py'
python server.py
```

`ANTHROPIC_API_KEY` must be set in the environment running the pipeline. The server only
binds to `127.0.0.1`.

`remote_refresh.py` is a leftover convenience for running the pipeline over SSH.
