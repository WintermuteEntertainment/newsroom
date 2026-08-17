# Routing profiles: toggling cost vs speed

The pipeline runs five LLM stages. Each can go to Claude (fast, metered) or to
llama-swap on the local GPU (slow, free). A **profile** is a named choice about
which stages to pay for.

## Switching

Two ways, and the difference matters.

**Permanent** — edit one field in `routing.json`:

```json
"active_profile": "local"
```

Takes effect on the next refresh. No restart: the runner re-reads `routing.json`
each run.

**One-off** — pass the flag, leave the file alone:

```bash
.venv/bin/python claude_pipeline_runner.py --routing routing.json --profile fast
```

Prefer the flag for a single urgent run. The file stays on `local`, so you cannot
forget to switch back — which is exactly how this project ended up paying for two
days of runs it thought were free.

## The profiles

| Profile | Cost/run | On Claude | Use when |
|---|---|---|---|
| `local` | **$0.0000** | nothing | Default. Overnight, unattended, cost-sensitive. |
| `safe-verify` | $0.0309 | entailment_check | You care most about not publishing a fabricated claim. |
| `fast-snippets` | $0.0333 | snippet_writing | The prose quality of the snippets matters this run. |
| `fast` | $0.0642 | entailment_check, snippet_writing | You want it sooner and will pay a little. |
| `claude` | $0.1719 | all five | You want it now, or llama-swap is down. |

Costs are apportioned from the measured 284-call all-Claude run ($0.172 total) by
each stage's real call share — not estimated. `coherence_audit` alone is $0.0793
of that, which is why it stays local in every profile but `claude`.

## Why these particular splits

`safe-verify` is not arbitrary, but the local entailer is better than an early
benchmark suggested. On a corpus of 7 genuine fabrications plus 15 supported
controls (`ensemble_probe.json`, job `95c3855c`), `v4flash` has **effective recall
6/7 = 86%** (95% CI 50–98%) with **zero false alarms** on all 15 supported cases —
Youden's J = +0.86.

"Effective" is doing real work in that sentence: 3 of its 7 replies carried no
verdict at all at `min_tokens=512`, and `news_digest.bad[]` flags anything that is
not an explicit `supported`. So a missing verdict routes the claim to **repair**,
not to publication — a truncated reply fails safe. Those nulls were the truncation
ceiling fixed in `2dc2b2a`; the runner now defaults `--min-tokens=768`.

The residual risk is one shared miss (`case4`): a genuine fabrication that *both*
local models called supported. That is the gap `safe-verify` buys back for 3 cents
— roughly 1 fabricated claim per 100 stories published and stamped verified, not
the 3 an earlier 4/7 benchmark implied.

Note this is also why `safe-verify` is a judgement call rather than an obvious
default: 86% with no false alarms is defensible for most runs.

`fast-snippets` is about prose, not correctness. Local scored 100% on the
malformedness gate, so snippets come out well-formed either way; Claude writes
them better.

## Design constraints

Two properties worth knowing, because they constrain what a profile can do to you:

1. **A profile can only move work toward Claude.** It cannot move a stage local.
   That direction is a correctness decision requiring a measurement, not a
   convenience toggle, so it is structurally impossible here (there is a test
   asserting it).

2. **Typos fail loudly.** An unknown profile name, or a profile naming a stage
   that does not exist, raises. A silent no-op would mean the toggle did nothing
   and you would not know.

The per-stage `route` fields in `routing.json` remain the single source of truth;
a profile is an overlay on top of them, so it can never contradict the
measurements recorded alongside in `_measured`.

## Checking what actually ran

Never trust the config alone — read the invocation and the spend:

```bash
# what the server will invoke
.venv/bin/python -c "import server; print(server.REFRESH_COMMAND)"

# what a profile resolves to
.venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from routing_host import load_routes
r = load_routes('routing.json', 'fast')
print({s:v for s,v in r.items()})"

# what the last run cost
.venv/bin/python -c "
import json; d=json.load(open('usage_2026-07-30.json'))
print(d['total_calls'], d['estimated_cost_usd'])"
```

A `local` run should leave `total_calls` **unchanged**. If it climbs, the routing
is not being applied — check that `--routing` is in the invocation.
