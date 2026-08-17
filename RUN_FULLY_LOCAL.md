# Run the pipeline fully local (zero metered calls)

Deployed and verified on BBB at **a681bf6** (2026-07-30). All five stages route local;
there is no Claude call left in a routed run.

## Step 1 — deploy to BBB

**BBB is NOT the machine holding `D:\Jazz\newsroom`.** An earlier version of this file said
to run `git pull /mnt/d/jazz/newsroom master` on BBB; that path does not exist there, so the
pull failed and BBB kept running the OLD partial-routing `routing.json` (entailment_check and
snippet_writing still on Claude) — which is why a refresh still cost ~$0.15/run. Deployment
happens by pushing a git bundle as a compute-job input, not by pulling a shared path.

Confirm what BBB is actually running before trusting any cost claim:

```bash
cd ~/newsroom
git log --oneline -1
.venv/bin/python -c "import sys; sys.path.insert(0,'.'); \
  from routing_host import load_routes; r=load_routes('routing.json'); print(r); \
  print('FULLY LOCAL' if all(v=='local' for v in r.values()) else 'STILL METERED')"
```

All five stages must print `local`. If any says `claude`, the fully-local commits are not
deployed and every refresh is still billing the API.

## Step 2 — run it

```bash
cd ~/newsroom
.venv/bin/python claude_pipeline_runner.py --routing routing.json --top-n 100 \
    2>&1 | tee fully_local_run.log
```

Notes on what to expect:

- **Wall clock well over the ~27 min of partial routing**, because entailment_check and
  snippet_writing now run local too. A direct CLI run has no timeout; the 3600s
  `NEWSROOM_REFRESH_TIMEOUT` only applies when `server.py` triggers a refresh.
- **llama-swap will reload between models.** entailment_check and fragment_merge use
  v4flash; the other three use qwen-finetune-q3. `RoutingHost.llm()` batches by model so
  each loads once per call-batch rather than per request, but the pipeline makes several
  batches, so expect a handful of swaps.
- `--min-tokens` defaults to 768. Do not lower it: v4flash reasons before answering and the
  pipeline asks for only 90 tokens at entailment, so a lower floor truncates it before the
  verdict (3 of 7 came back empty at 512).

## Step 3 — the two things worth checking in the output

```bash
# 1. Metered spend must be ZERO.
ls -t usage_*.json | head -1 | xargs cat

# 2. Contract compliance per stage, and the entailment verdict counts.
grep -iE "compliance|contract|unverified|UNSUPPORTED|parse" fully_local_run.log | tail -30
```

**What "good" looks like:** zero metered spend; 100% contract compliance on all five stages;
and an entailment section that reports some verdicts rather than a run of `unverified`. If
you see many `unverified`, that is the truncation ceiling returning — raise `--min-tokens`
to 1024 and re-run rather than assuming the model is wrong.

## The accepted risk, stated plainly

Entailment now runs on v4flash: **effective recall 6/7 (86%, 95% CI 50-98%) with zero false
alarms on 15 supported controls** (Youden's J +0.86). One case in the probe was a shared miss
— both local models called a genuine fabrication supported — so roughly **1 in 7 fabricated
claims can still publish stamped verified**. That is the price of zero metered cost, on a
sample of 7. It is not a solved problem, and if the digest ever matters more than the money,
entailment is the one stage to put back on Claude (it was $0.0728/run, $26.57/yr).

No ensemble with qwen: qwen answers "unsupported" on 82% of all cases (Youden's J +0.06), so
it is barely distinguishable from a constant "unsupported". Pairing it with v4flash would
take false alarms from 0/15 to 12/15 while recall stayed 6/7 — noise, not a second opinion.
