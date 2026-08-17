# Local inference throughput: measured, 2026-07-30

Source: llama-swap proxy request log for the 12:59:34-14:10:53 routed run
(`fully-local` profile, 48 stories), terminated by SIGTERM at 71.3 min before
it wrote a digest. 140 pipeline completions were logged and are the basis for
every number here. Nothing in this file is extrapolated from a single probe.

## What the run actually did

| Quantity | Value |
|---|---|
| Wall clock before kill | 71.3 min |
| Pipeline calls completed | 140 of ~284 expected (49%) |
| Total model time | 7,418 s (123.6 min of serial inference) |
| **Effective concurrency achieved** | **1.73** (client cap was 2) |
| Digest written | No |

Per-call latency was not uniformly slow -- it was *heavy-tailed*:

| Percentile | Latency |
|---|---|
| min | 6.1 s |
| median | 20.5 s |
| mean | 53.0 s |
| p90 | 151.7 s |
| max | 300.3 s |

21% of calls (30 of 140) took over a minute. The mean is 2.6x the median,
so any single-probe estimate of "how slow is local" is close to meaningless --
my own earlier 83 s figure and later 6.7 s figure were both real samples of
the same distribution.

## Finding 1: 8.6% of calls returned nothing at all

12 of 140 calls came back HTTP 502 carrying zero response bytes. **Ten** sat at
~300.0-300.3 s, which is not a coincidence: `LocalHost.timeout` defaulted to
300.0 s, so those are calls the *client* abandoned, logged as 502 from the
proxy's side. The remaining **two returned in 62.0 s and 21.9 s** -- far short
of any timeout -- and have a different cause entirely; see Finding 4, which
splits the twelve into a cold-start class (10) and an eviction class (2). The
distinction matters because only the first is a timeout problem, and the fixes
are different.

`_one()` converts that into `{"text": "", "error": ...}` and counts it under
`errors["http_502"]`. An empty string is not an error downstream -- it flows
into the stage parsers as a legitimately empty answer. For `entailment_check`
an empty answer means no verdict, which routes the claim to repair rather than
publication (fails safe). For `snippet_writing` it means an empty snippet,
which the repair pass is supposed to rebuild.

So the run was not merely slow: roughly one call in twelve produced no content.
For ten of them the proximate cause is a client-side timeout default nobody
chose for this workload; for the other two it is a mid-request model eviction
that no timeout value would have prevented.

## Finding 2: the throughput ceiling is server-side, per model

`--parallel` in `/mnt/a/llama-swap/config.yaml`:

| Model | `--parallel` | ctx | Stages served | Calls/run |
|---|---|---|---|---|
| `qwen-finetune-q3` | **1** | 8192 | coherence, snippets, exclusives | ~226 |
| `v4flash` | unset (reports 4 slots) | 16384 | merge, entailment | ~58 |
| `qwen3-coder-30b` | 1 | 8192 | none | 0 |

`llama-server` serves `--parallel N` requests concurrently and queues the rest.
So ~80% of the run is pinned to one-at-a-time regardless of any client setting.
Raising the client cap alone moves the queue from the client into the server;
it does not add throughput. This is now asserted in code rather than in prose:
`LocalHost.concurrency_warning()` reads `total_slots` off the model's `/props`
and warns when the client cap exceeds it, printed by the runner before the
first call.

## Finding 3 (CORRECTION): there is no VRAM headroom

I previously wrote that `--parallel 4` on `qwen-finetune-q3` was safe because
the model is "small" and "already using q8_0 KV quantization". **Both halves
of that were wrong.** The `--cache-type-k q8_0 --cache-type-v q8_0` flags
belong to `qwen3-coder-30b`, a different model the pipeline never calls.
`qwen-finetune-q3`'s full launch line is:

    --n-gpu-layers 99 --ctx-size 8192 --flash-attn on --threads 8 --parallel 1

No cache-type flags, so its KV cache is unquantized f16.

Measured with `qwen-finetune-q3` loaded:

    16,376 MiB total / 16,026 MiB used / 36 MiB free   (99.8% full)

The GPU is effectively full with one model and one slot. Whether `--parallel 4`
needs *more* VRAM depends on whether llama.cpp treats `--ctx-size` as a total
budget divided among slots (KV unchanged) or as per-slot (KV x4). I have not
measured which applies to this build, and with 36 MiB free the difference is
between "works" and "fails to load". **I am not recommending the config edit
on the strength of an argument I got wrong once already.**

## What to do instead, in order of confidence

1. **Set an explicit timeout below the tail.** `timeout=300` is what produced
   the 12 empty answers. Nothing useful arrives at 300 s that would not have
   arrived by ~120 s (p90 is 151.7 s). A shorter timeout converts a silent
   empty answer into a fast, counted failure -- and the repair pass can then
   act on it inside the same run.
2. **Cut `top_n` from 48 to ~20.** `outlets_config.json` sets 48, written by
   the site's settings panel. Call volume scales with it, and it is the
   largest single term in wall clock. Roughly 40% of the calls for a digest
   whose marginal story past ~20 is unlikely to be read.
3. **Add KV quantization to `qwen-finetune-q3` BEFORE touching `--parallel`.**
   `--cache-type-k q8_0 --cache-type-v q8_0` (the flags `qwen3-coder-30b`
   already uses) should roughly halve KV footprint and is what would create
   the headroom that `--parallel 4` needs. Test load, confirm free VRAM
   climbs, then and only then raise `--parallel`.
4. **Then raise `--parallel` to 4 and measure.** Per-stage worst-case
   prompt+output from the 603-call corpus: snippet_writing ~1,108 tok,
   coherence_audit ~861, entailment_check ~635, exclusives_dedup ~418,
   fragment_merge ~344. All fit in 8192/4 = 2,048 tok per slot. That part of
   the earlier analysis stands -- context division is not the constraint;
   VRAM is.

## Ceiling, if the above works

CORRECTED 2026-07-30: an earlier version of this table labelled the 7,418 s as
the model time of 284 calls. It is the model time of the **140 calls that ran**
(49% of the run) -- so every projection in it was understated by ~2x, and its
own "71 min" row contradicted the 49%-at-71-min figure in the table above.
Recomputed below on the right basis.

Scaling 7,418 s / 140 calls to a full 284-call run gives **15,048 s** of model
time. With latency held flat:

| Effective concurrency | Projected wall clock |
|---|---|
| 1.0 | 251 min |
| 1.73 (what we got) | **145 min** |
| 4.0 | 63 min |

The 145 min row is the direct check: it reproduces the 71.3 min observed for
49% of the run, which is what makes the other rows trustworthy.

Latency will *not* hold flat -- concurrent slots share compute, so per-call
time rises as throughput does and the real figure lands above 63 min. Treat
that column as an optimistic bound, not a forecast.

Both remedies below attack the 15,048 s directly rather than the concurrency
multiplier: eliminating the cold-swap timeouts removes 44% of it, and halving
`top_n` removes most of the rest.

## Config edits made (client side, committed)

- `LocalHost.max_concurrency` default 2 -> 4, with a comment stating the
  server-side ceiling and why a higher client cap buys nothing.
- New `--local-concurrency` runner flag (default 4) so this needs no code edit.
- New `LocalHost.server_slots()` / `.concurrency_warning()`; the runner prints
  the warning before the first call. Degrades to silence when `/props` is
  absent or the endpoint is dead -- a missing diagnostic must not break a run.
- `test_local_host.py` (committed): 17 tests covering the slot-mismatch cases
  including the real qwen `--parallel 1` case, both failure-to-probe paths, the
  cold-model warmup gate, and per-stage loss accounting. An earlier version of
  this note claimed "12 tests pass" for checks that existed only as a throwaway
  script in /tmp -- true when run, unverifiable afterwards. They are now in the
  repo.

No change was made to `/mnt/a/llama-swap/config.yaml`. That file serves things
beyond the newsroom and the VRAM question above is unresolved.

## Finding 4: the failures were model swaps -- in two classes, not one

Splitting the proxy log by outcome (98 pipeline calls with parsable durations):

| | successful calls only | failed calls |
|---|---|---|
| n | 86 | 12 |
| median | 24.8 s | -- |
| p90 / max | 90.6 s / **206.8 s** | 300.1 s (max; 10 of 12 at the timeout) |
| min | 13.5 s | **21.9 s** (not a timeout -- see below) |
| total time | 3,931 s | **3,086 s = 44% of all model time** |

Two things follow. First, no call that returned content ever took longer than
206.8 s, so a 210 s timeout discards nothing observed and reclaims ~15 min.

Second and more important: the failures are not spread through the run. In
chronological order, with llama-swap's own events interleaved:

```
EVENT: <v4flash> Health check passed
  X300 X300 X300 X300 X300 X300 X300 X300      <- 8 consecutive timeouts
EVENT: <qwen-finetune-q3> Health check passed
  R112 R124 R28 R28 R32 ... (51 successes, ~20 s each)
EVENT: <v4flash> Health check passed
  X300 X300 R81 R74 R189 ...                   <- 2 timeouts, then normal
```

Every failure carries **0 response bytes**, and all twelve cluster at a model
swap -- but in TWO distinct classes, which matters because they need different
fixes. This supersedes the original wording of Finding 1, which attributed all
twelve to the client timeout:

**Cold start (10 calls, positions 0-7 and 59-60).** All at 300.0-300.3 s,
immediately *after* a swap. llama-swap's `/health` passes when `llama-server`
binds its port, which is before the weights finish loading; on a GPU at 99.8%
occupancy a swap must evict ~10 GB and load ~10 GB, and that exceeded 300 s.

**Eviction (2 calls, positions 96-97).** At **62.0 s and 21.9 s** -- far short
of any timeout -- immediately *before* the next model's health check, after 35
consecutive successes on a model that was plainly working:

```
  ... R41.1  R44.6  R73.0   X62.0(0 bytes)  X21.9(0 bytes)
EVENT: <qwen-finetune-q3> Health check passed
```

These died *mid-request*: llama-swap evicted v4flash to load qwen while their
requests were still in flight (an OOM on a full GPU would look identical in the
proxy log -- it cannot distinguish them). **No pre-flight probe can prevent
this**, because the model was warm when the request was accepted. So the
v4flash/entailment segment lost 4 calls, not 2.

Mapping the segments onto the stage order in `news_digest.run()`:

| Log segment | Stage | Model | Outcome |
|---|---|---|---|
| `X300` x8 | **`fragment_merge`** (~9 calls) | v4flash, cold | **8 of 9 destroyed** |
| `R` x51 | `snippet_writing` | qwen, warm | fine |
| `X300` x2 then `R` x35, `X62`, `X22` | `entailment_check` | v4flash | 2 lost cold + **2 lost to eviction** |

`fragment_merge` was effectively wiped out: the stage that re-joins stories the
coherence audit split apart did nothing, returned empty strings, and **no part
of the run report said so.** That is the real defect this measurement found --
worse than the wall clock, because it is invisible.

Lowering the timeout *alone* would have made it worse: the same calls fail,
just sooner, and the model never gets to finish loading.

### Fixes

Two fault classes, two mechanisms:

- `LocalHost.ensure_warm(model)` -- one serial 1-token probe with a 900 s
  deadline before any batch fans out, idempotent per process. The cold-load
  cost is paid once by one call instead of concurrently by every call. A failed
  probe is reported loudly and does *not* block the batch (turning a slow load
  into a hard failure would be worse) and is not counted as lost pipeline work.
  **This addresses the cold-start class only.**
- `retry_upstream` (default on) -- ONE retry per call on any upstream failure,
  which is what covers the eviction class, since nothing pre-flight can. It is
  deliberately not a retry *loop*: a second failure means the answer is
  genuinely lost and gets counted, rather than retried until the time budget is
  gone. The retry does **not** re-probe before retrying -- a probe would land on
  the reloading model and absorb the recovery, leaving the real request to fail
  anyway. It clears the warm flag (so the next batch re-warms serially) and
  backs off `retry_backoff` seconds, defaulting to 5 s because the observed
  eviction failures returned in 22-62 s.
- `LocalHost.lost_by_stage` -- per-stage tally of calls that returned no usable
  content, covering HTTP errors, transport failures, *and* HTTP 200s with empty
  content (a reasoning model that spends its budget on `reasoning_content`
  returns exactly that). Persisted in `usage_<date>.json`.
- The run report now prints a `WARNING` naming the loss count, the rate, and
  the per-stage breakdown, plus which stages fail safe. Previously the only
  trace was an undifferentiated `transport_errors` dict at the end of one line,
  which is why an 8.6% loss rate went unnoticed.


## Finding 5: the fixes verified on a full run (2026-07-30, 14:40-15:35)

The first complete fully-local run. 228 local calls, 0 Claude calls, $0.00,
55 min wall clock (vs the 71-min run that was killed before writing anything).

| | killed run (12:59-14:10) | fixed run (14:40-15:35) |
|---|---|---|
| calls lost, no content | 12 / 140 = **8.6%** | **1 / 228 = 0.4%** |
| digest written | no | **yes, 20 stories** |
| wall clock | 71 min (incomplete) | **55 min (complete)** |
| cost | $0.00 | $0.00 |

The mechanism is visible in the counters rather than inferred: the run recorded
`retried_TimeoutError: 10` against `TimeoutError: 1`. Ten calls hit the fault
and were **recovered by the single retry**; one exhausted it. Under the previous
code all eleven would have become empty strings that no stage could distinguish
from a real answer, and the 8-of-9 `fragment_merge` wipeout would have recurred.

`ensure_warm` fired twice and both probes were cheap: 4.1 s before the 133-call
`coherence_audit` batch, 0.2 s before a later single call (already warm). The
serialised cold-load cost is therefore ~4 s per model per run, against the
~50 min of dead time that cold-start failures cost the killed run.

### What the instrumentation now exposes (previously invisible)

The loss warning and contract report both fired, and they disagree about where
the weakness is -- which is the point of having both:

| Stage | calls | parsed | lost |
|---|---|---|---|
| `coherence_audit` | 134 | 133 (99.3%) | 0 |
| `snippet_writing` | 24 | 24 (100%) | 0 |
| `entailment_check` | 23 | 23 (100%) | **1** |
| `exclusives_dedup` | 40 | 40 (100%) | 0 |
| `fragment_merge` | 7 | **4 (57%)** | 0 |

`fragment_merge` is the real quality problem, and it is not a transport problem
at all -- every call returned, and 3 of 7 returned something unusable. The
recorded samples show the model ignoring the output contract entirely: one reply
was the opening of a Sherlock Holmes joke, others were `intuitionDIFFERENT` and
`ghiSAME` -- the SAME/DIFFERENT verdict fused to a stray token. That is a
prompt/model-fit failure for `qwen-finetune-q3` on this stage specifically. The
one `coherence_audit` miss is different in kind: a `keep` array truncated
mid-list, i.e. an output-token ceiling, not a misunderstood instruction.

Neither of those would have appeared in any earlier report.