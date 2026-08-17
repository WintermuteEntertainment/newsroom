#!/usr/bin/env python3
"""Local-inference host for the news digest pipeline, via an OpenAI-compatible endpoint.

Targets llama-swap (default http://127.0.0.1:8001/v1) so the pipeline can run with no
metered API spend. Exposes the same surface news_digest.py calls on its host object:

    host.reasoning_model() -> str
    host.llm(requests, max_concurrency=N) -> [{"text": ...}, ...]   (positionally matched)

Two things differ from the Anthropic-backed host and both matter:

1. llama-swap serves ONE model at a time, swapping weights on demand. Concurrency above
   what a single loaded model can pipeline buys nothing and risks swap thrash, so the
   default is deliberately low. Requesting a different model mid-run forces a reload --
   which is why the routing host groups requests by model rather than interleaving them.

2. A local instruction-tuned model is chattier than the API models this pipeline was
   written against. The pipeline's parsers now tolerate that (news_digest.extract_json,
   read_same_different), and this host does its share by stripping reasoning blocks and
   code fences before the text is returned. What it does NOT do is invent an answer when
   the model failed to give one: an empty or unusable response comes back empty, so the
   stage records a contract failure instead of a fabricated verdict.

Failures are returned, never raised: one bad response must not abort a run. But it is
recorded, so a host that fails often is visible in the run report rather than silently
degrading the digest.
"""
from __future__ import annotations

import json
import time
import re
import threading
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

DEFAULT_BASE_URL = "http://127.0.0.1:8001/v1"
DEFAULT_MODEL = "v4flash"

# llama.cpp's own default slot count when --parallel is absent from a launch command.
# v4flash runs exactly that way: its cmd sets no --parallel and its own /props reports
# total_slots=4. An absent flag therefore means 4, NOT 1 -- reading it as 1 would invent a
# warning about the one model that never had a concurrency ceiling to begin with.
LLAMA_CPP_DEFAULT_PARALLEL = 4
_PARALLEL_IN_CMD = re.compile(r"--parallel\s+(\d+)")

# Reasoning-style models emit a thinking block before the answer. Some emit the closing tag
# only, having been prompted mid-thought. Both are stripped; see strip_reasoning.
_THINK_PAIR = re.compile(r"<(think|thinking|reasoning|scratchpad)>.*?</\1>", re.S | re.I)
_THINK_OPEN = re.compile(r"<(think|thinking|reasoning|scratchpad)>.*", re.S | re.I)
_THINK_CLOSE = re.compile(r"^.*?</(think|thinking|reasoning|scratchpad)>", re.S | re.I)
_FENCE = re.compile(r"```(?:json|JSON|text)?\s*(.*?)```", re.S)


def strip_reasoning(text: str) -> str:
    """Remove <think> blocks and code fences that wrap the real answer.

    Ordering is deliberate: a CLOSING tag with no opener means everything before it was
    thinking, so that is removed first. Only then is an unclosed OPENING tag handled.

    An unclosed OPENING tag used to blank the reply entirely, on the reasoning that a
    truncated thought is not an answer. That is right for a genuinely truncated reply and
    wrong for a model that emitted its verdict without ever closing the tag -- and this
    function runs BEFORE the pipeline's parsers, so what it deletes here is gone. Measured
    2026-08-10 across every run log on the server: 34 of 93 fragment_merge contract
    failures and 47 of 48 entailment_check failures were recorded as an EMPTY reply, which
    is what a deleted verdict looks like from downstream. The stage that suffered worst was
    the one asking for the shortest answer.

    So an unclosed tag now keeps the LAST non-blank line. A truncated reply still yields no
    verdict, because its final line is reasoning prose and the readers in news_digest
    refuse anything without a verdict word -- the decision moves from "was the tag closed"
    to "is there an answer in it", which is the question that was always meant to be asked.

    NOTE: news_digest.strip_reasoning is a SECOND copy of this rule, and the two have to
    agree. This one runs first, on the raw reply; that one runs on already-stripped text as
    a second line of defence. Change both together.
    """
    if not text:
        return ""
    t = _THINK_PAIR.sub(" ", text)
    if _THINK_CLOSE.search(t):
        t = _THINK_CLOSE.sub(" ", t)
    open_tag = _THINK_OPEN.search(t)
    if open_tag:
        tail = [ln for ln in t[open_tag.start():].splitlines() if ln.strip()]
        # A later line is a conclusion reached AFTER the thinking, so it is kept. Text on
        # the tag's own line is inside the thought ("<think>maybe SAME but") and is
        # dropped -- reading that as an answer is the guess this function must not make.
        t = t[:open_tag.start()] + ("\n" + tail[-1] if len(tail) > 1 else " ")
    fenced = _FENCE.search(t)
    if fenced:
        t = fenced.group(1)
    return t.strip()


class LocalHost:
    """Host object backed by an OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, model: str = DEFAULT_MODEL, base_url: str = DEFAULT_BASE_URL,
                 max_concurrency: int = 4, timeout: float = 210.0,
                 retry_upstream: bool = True, retry_backoff: float = 5.0,
                 temperature: float = 0.0, min_tokens: int = 768) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        # Cap concurrency regardless of what a caller asks for: the pipeline requests 6,
        # which is tuned for a metered remote API, not for one locally-loaded model.
        # The ceiling that matters is SERVER-side -- llama-server serves `--parallel N`
        # requests at once and QUEUES the rest, so a client cap above N buys nothing.
        # Default 4 measured against the BBB config (2026-07-30): v4flash reports
        # total_slots=4, and --parallel 4 on --ctx-size 8192 leaves 2048 tok/slot, which
        # fits every stage's worst case in the 603-call corpus (largest: snippet_writing
        # at ~1108 tok input+output). Raising this without also raising the model's
        # --parallel just moves the queue from here into the server.
        self.max_concurrency = max_concurrency
        # Default 210s measured, not guessed (2026-07-30, 98 logged calls from a real run):
        # the SLOWEST call that ever returned usable content took 206.8s, while 10 calls sat
        # at the old 300s default and returned NOTHING -- those 10 burned 44% of the run's
        # entire model time. 210s is therefore strictly free: it discards no observed
        # success and reclaims ~15 min per run. It is a CLIENT abandon, so an expired call
        # surfaces as errors["<ExcName>"] and {"text": ""} -- see the note on _one().
        self.timeout = timeout
        # Greedy decoding: every stage here is a classification or an extraction with one
        # correct answer, and run-to-run reproducibility is worth more than variety.
        self.temperature = temperature
        # Floor on the token budget -- must clear a reasoning preamble, see _one().
        self.min_tokens = min_tokens
        self.usage_log = []
        self.errors = Counter()
        # Per-STAGE tally of calls that returned no usable content. The flat `errors`
        # Counter says a timeout happened; it cannot say which stage silently lost an
        # answer, and an empty answer is indistinguishable downstream from a real one
        # (measured 2026-07-30: 12 of 140 calls returned "" and nothing recorded where).
        self.lost_by_stage = Counter()
        # Models proven to answer since this process started. llama-swap's /health passes as
        # soon as llama-server binds its port, which is BEFORE the weights finish loading; on
        # a full GPU a swap evicts ~10GB and loads ~10GB, which took >300s on 2026-07-30 and
        # timed out 8 of 9 fragment_merge calls -- the whole stage, silently. Warm serially
        # ONCE per model so that cost is paid by one call instead of every call in the batch.
        self._warmed: set[str] = set()
        # Retry once on an upstream failure. Default on: measured loss without it was 8.6%
        # of calls, and two of the twelve were mid-request deaths no probe can pre-empt.
        self.retry_upstream = retry_upstream
        self.retry_backoff = retry_backoff
        self._lock = threading.Lock()

    def reasoning_model(self) -> str:
        return self.model

    # -- transport ---------------------------------------------------------------------
    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _one(self, request: dict) -> dict:
        """Single completion. Returns {"text": ...}; text is "" if nothing usable came back."""
        system_text = request.get("system", "")
        messages = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": request["prompt"]})
        # Local models spend tokens on preamble the API models do not, and several stages
        # cap max_tokens tight enough (fragment_merge uses 8) that a correct answer would be
        # truncated away. Raise the floor so the verdict itself can land; the parsers, not
        # the token budget, decide what counts as an answer.
        #
        # The floor has to clear the REASONING budget, not just the answer. Measured
        # 2026-07-28: a 64-token floor made v4flash (a reasoning model) fail 32/57 calls with
        # "truncated before answer" -- it spent the whole budget thinking and was cut off
        # before emitting a verdict, and the stages wanting the SHORTEST answers failed worst
        # (fragment_merge 0/9, a one-word verdict). That measured nothing about the model's
        # judgement. A reasoning preamble runs to a few hundred tokens, so the floor must sit
        # above it or the benchmark scores the budget instead of the model.
        #
        # Raised 512 -> 768 on 2026-08-04. 512 cleared the measured preamble but left no
        # margin: v4flash's reasoning length varies with prompt difficulty, and a hard
        # entailment case is exactly where it thinks longest and where a truncated verdict
        # is most costly. Truncation is not a graceful degradation here -- entail_verdict()
        # returns None for a reply with no verdict in it, which the pipeline reports as
        # 'unverified'. Unused budget costs nothing: generation stops at the stop token, so
        # a higher ceiling only pays for tokens actually emitted.
        want = int(request.get("max_tokens", 300))
        payload = {"model": request.get("model") or self.model,
                   "messages": messages,
                   "max_tokens": max(want, self.min_tokens),
                   "temperature": self.temperature,
                   "stream": False}
        stage = request.get("stage") or "other"
        # ONE retry on an upstream failure, because two DISTINCT model-availability faults
        # were measured on 2026-07-30 and a warmup gate only covers the first:
        #
        #   cold start  -- 10 calls, 300 s each, 0 bytes, immediately AFTER a swap:
        #                  llama-swap's /health passes when llama-server binds its port,
        #                  before the weights finish loading. ensure_warm() handles these.
        #   eviction    -- 2 calls, 62.0 s and 21.9 s, 0 bytes, immediately BEFORE the next
        #                  model's health check, after 35 consecutive successes on a model
        #                  that was plainly working. The upstream died mid-request (swap
        #                  eviction or an OOM on a 99.8%-full GPU -- the proxy log cannot
        #                  distinguish them). NO pre-flight probe can prevent this: the
        #                  model was warm when the request was accepted.
        #
        # A retry covers both, and costs nothing on a healthy run. It is deliberately not a
        # retry loop: if the second attempt also fails, the answer is genuinely lost and must
        # be counted as such rather than retried until the run's time budget is gone.
        attempts = 2 if self.retry_upstream else 1
        last_err = ""
        for attempt in range(attempts):
            try:
                body = self._post(payload)
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                last_err = f"HTTP {exc.code}: {detail}"
                key = f"http_{exc.code}"
            except Exception as exc:                  # timeout, connection refused, bad JSON
                last_err = f"{type(exc).__name__}: {exc}"
                key = type(exc).__name__
            if attempt + 1 < attempts:
                with self._lock:
                    self.errors[f"retried_{key}"] += 1
                # Do NOT re-probe here. An eviction leaves the model cold, but a 1-token
                # probe would itself absorb the recovery: the probe lands on the reloading
                # model, and the real request still races it. Instead just clear the warm
                # flag (so the NEXT batch re-warms serially) and back off long enough for a
                # reload to make progress. Measured reload behaviour: the eviction-class
                # failures returned in 22-62 s, so a short sleep is the right order.
                self._warmed.discard(payload["model"])
                time.sleep(self.retry_backoff)
                continue
            with self._lock:
                self.errors[key] += 1
                self.lost_by_stage[stage] += 1
            return {"text": "", "error": last_err}

        choices = body.get("choices") or []
        raw = ""
        if choices:
            msg = choices[0].get("message") or {}
            raw = msg.get("content") or ""
            # Some builds put the thinking in its own field and the answer in content;
            # others inline it. Only content is read, so a separate field is ignored by
            # construction rather than needing to be stripped.
        if not raw.strip():
            # A 200 with empty content is the SAME silent loss as a timeout: a reasoning
            # model that spent its budget thinking returns finish_reason="length" and no
            # content. Downstream cannot tell that from a legitimately empty answer, so it
            # has to be counted here or it is invisible.
            with self._lock:
                self.errors["empty_content"] += 1
                self.lost_by_stage[request.get("stage") or "other"] += 1
        usage = body.get("usage") or {}
        with self._lock:
            self.usage_log.append({
                "label": request.get("stage") or "other",
                "model": payload["model"],
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            })
        finish = (choices[0].get("finish_reason") if choices else None)
        text = strip_reasoning(raw)
        out = {"text": text, "raw": raw}
        if finish == "length" and not text:
            # Ran out of budget while still thinking: no answer was produced. Say so rather
            # than returning a truncated fragment that a parser might half-read.
            out["error"] = "truncated before answer"
        return out

    def server_slots(self) -> int | None:
        """Concurrent slots THIS host's model was launched with, or None.

        This is the number that actually bounds throughput: llama-server runs
        `--parallel N` requests at once and queues the rest, so a client-side
        max_concurrency above N adds latency without adding throughput. Returned as None
        (not an error) when it cannot be determined -- a missing diagnostic must never
        break a run.

        Reads `GET /running` on the swap proxy and parses --parallel out of the launch
        command. It used to ask `{base_url}/props`, and that NEVER WORKED in production:
        with base_url=http://127.0.0.1:8001/v1 the root resolves to llama-swap's own
        /props, which answers 404 "no model id could be identified" because the proxy
        cannot tell which model an unrouted request means. So server_slots() returned None
        on every real run, concurrency_warning() was suppressed, and qwen's --parallel 1
        ceiling stayed invisible for the whole project while the client sent 4 -- the
        single biggest throughput bug in it, hidden by its own diagnostic.

        /running is served regardless of which model is loaded, and reports the actual
        launch cmd, which is authority over the config file: the file said --parallel 4
        for a full day on 2026-08-04 while the running process still had 1.
        """
        import json as _json
        import urllib.error
        import urllib.request
        root = self.base_url.rsplit("/v1", 1)[0]
        try:
            with urllib.request.urlopen(root + "/running", timeout=5) as fh:
                body = _json.loads(fh.read().decode("utf-8")) or {}
        except (urllib.error.URLError, OSError, ValueError, TypeError):
            return None
        entries = body.get("running")
        if not isinstance(entries, list):
            return None
        # Match THIS host's model by name. Reporting whatever happens to be loaded would
        # be actively misleading during a routed run, where the pipeline alternates models:
        # v4flash's 4 slots say nothing about the qwen batch this host is about to send.
        for entry in entries:
            if isinstance(entry, dict) and entry.get("model") == self.model:
                cmd = entry.get("cmd")
                if not cmd:
                    return None
                found = _PARALLEL_IN_CMD.search(cmd)
                # An absent --parallel is not "unknown" -- it is llama.cpp's default.
                return int(found.group(1)) if found else LLAMA_CPP_DEFAULT_PARALLEL
        # Model not loaded yet. Its slot count is genuinely unknown until llama-swap
        # launches it, and guessing here would warn (or reassure) about nothing.
        return None

    def concurrency_warning(self) -> str | None:
        """Human-readable warning when the client cap exceeds the server's slots."""
        slots = self.server_slots()
        if slots and self.max_concurrency > slots:
            return (f"max_concurrency={self.max_concurrency} exceeds the loaded model's "
                    f"--parallel {slots}: {self.max_concurrency - slots} request(s) will "
                    f"queue server-side. Raise --parallel in the llama-swap config, or "
                    f"lower --local-concurrency to {slots}.")
        return None

    def ensure_warm(self, model: str, timeout: float = 900.0) -> dict:
        """Block until `model` actually answers, or give up after `timeout`.

        Idempotent per process. Sends the smallest possible completion; a swap that has to
        evict and load weights can take minutes, so this uses a far longer deadline than a
        normal call. Returns a dict describing what happened -- the caller reports it, and a
        FAILED warmup is a loud signal that the batch about to run will be mostly empty.
        """
        if model in self._warmed:
            return {"model": model, "status": "already_warm", "seconds": 0.0}
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ok"}],
                           "max_tokens": 1, "temperature": 0}).encode("utf-8")
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
            elapsed = time.monotonic() - start
            self._warmed.add(model)
            return {"model": model, "status": "warmed", "seconds": round(elapsed, 1)}
        except Exception as exc:
            elapsed = time.monotonic() - start
            # Deliberately NOT added to _warmed and NOT counted as a lost stage call: this is
            # a probe, not pipeline work. The batch still runs -- refusing to run it would
            # turn a slow load into a hard failure -- but the caller now knows why it is empty.
            with self._lock:
                self.errors["warmup_failed"] += 1
            return {"model": model, "status": "failed", "seconds": round(elapsed, 1),
                    "error": f"{type(exc).__name__}: {exc}"}

    def llm(self, requests: list[dict], max_concurrency: int | None = None) -> list[dict]:
        """Run requests, returning results positionally matched to the input list."""
        if isinstance(requests, dict):        # tolerate the single-request form
            return self._one(requests)
        if not requests:
            return []
        workers = min(self.max_concurrency, max(1, len(requests)))
        if max_concurrency is not None:
            workers = min(workers, max(1, max_concurrency))
        # Pay the cold-swap cost ONCE, serially, before fanning out. Without this the whole
        # batch races a loading model and every request burns the full timeout (measured
        # 2026-07-30: fragment_merge lost 8 of 9 calls this way, reported as nothing).
        warm = self.ensure_warm(self.model)
        if warm["status"] == "warmed":
            print(f"  [local] warmed {warm['model']} in {warm['seconds']}s before "
                  f"{len(requests)} call(s)", flush=True)
        elif warm["status"] == "failed":
            print(f"  [local] WARNING -- {warm['model']} did not answer a 1-token probe in "
                  f"{warm['seconds']}s ({warm.get('error', '')}). Running the batch anyway; "
                  f"expect empty answers, counted per stage below.", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self._one, requests))

    # -- reporting ---------------------------------------------------------------------
    def usage_summary(self) -> dict:
        by_stage: dict[str, dict] = {}
        for entry in self.usage_log:
            st = by_stage.setdefault(entry["label"],
                                     {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            st["calls"] += 1
            st["input_tokens"] += entry["input_tokens"]
            st["output_tokens"] += entry["output_tokens"]
        return {"model": self.model, "backend": self.base_url,
                "total_calls": len(self.usage_log),
                "total_input_tokens": sum(e["input_tokens"] for e in self.usage_log),
                "total_output_tokens": sum(e["output_tokens"] for e in self.usage_log),
                # Local inference is unmetered: the cost of a run is electricity and time,
                # not tokens. Reported as 0.0 with pricing_known so the field means the same
                # thing it does in the Claude runner rather than being absent.
                "estimated_cost_usd": 0.0, "pricing_known": True,
                "transport_errors": dict(self.errors),
                "lost_by_stage": dict(self.lost_by_stage),
                "by_stage": by_stage}


def list_models(base_url: str = DEFAULT_BASE_URL, timeout: float = 30.0) -> list[str]:
    """Model ids the endpoint is serving."""
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/models", timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return [m["id"] for m in body.get("data", [])]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Smoke-test the local inference endpoint.")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()
    served = list_models(args.base_url)
    print("models:", served)
    host = LocalHost(args.model or served[0], base_url=args.base_url)
    r = host.llm([{"prompt": "Reply with the single word OK.", "max_tokens": 16,
                   "stage": "smoke"}])
    print("reply:", repr(r[0].get("text")), "err:", r[0].get("error"))
    print("usage:", json.dumps(host.usage_summary(), indent=2))
