#!/usr/bin/env python3
"""Standalone Claude (Anthropic API) runner for the news digest pipeline."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from anthropic import Anthropic
import news_digest

# Maps each stage's system prompt text to a short label for the usage report -- lets
# per-stage cost be attributed without threading a label through every call site in
# news_digest.py, since each stage already uses one fixed, distinct system prompt.
SYSTEM_LABELS = {
    news_digest.SYS_COHERE: "coherence_audit",
    news_digest.SYS_MERGE: "fragment_merge",
    news_digest.SYS_SNIP: "snippet_writing",
    news_digest.SYS_ENTAIL: "entailment_check",
    news_digest.SYS_DUP: "exclusives_dedup",
}


class ClaudeHost:
    """Compatibility layer for the pipeline's former Claude-Code host object."""

    def __init__(self, model: str, record_path: Path | None = None) -> None:
        self.client = Anthropic()
        self.model = model
        self.usage_log = []
        self._lock = threading.Lock()
        # When set, every request and Claude's response is appended to a JSONL corpus. That
        # corpus is what makes local-model evaluation reproducible: candidates are replayed
        # against the SAME prompts, scored by the same parsers, and compared to Claude's
        # answer as the reference -- no re-fetching feeds, no drift between runs.
        self.record_path = record_path
        self._record_lock = threading.Lock()

    def reasoning_model(self) -> str:
        return self.model

    def llm(self, requests: list[dict], max_concurrency: int = 6) -> list[dict]:
        def call(request: dict) -> dict:
            system_text = request.get("system", "")
            response = self.client.messages.create(
                model=request.get("model", self.model),
                system=system_text,
                messages=[{"role": "user", "content": request["prompt"]}],
                max_tokens=request.get("max_tokens", 300),
            )
            text = "".join(block.text for block in response.content if block.type == "text")
            usage = response.usage
            stage = SYSTEM_LABELS.get(system_text, "other")
            with self._lock:
                self.usage_log.append({
                    "label": stage,
                    "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                })
            if self.record_path is not None:
                row = {"stage": stage, "system": system_text, "prompt": request["prompt"],
                       "max_tokens": request.get("max_tokens", 300), "reference": text}
                line = json.dumps(row, ensure_ascii=False)
                with self._record_lock:      # one writer at a time; calls run threaded
                    with self.record_path.open("a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
            return {"text": text}

        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            return list(executor.map(call, requests))


CSV_FIELDS = ["rank", "headline", "n_panel_outlets", "panel_size", "n_articles",
              "covered_closest_by", "also_carried_by", "snippet", "entailment",
              "snippet_form", "link", "outlet_links"]

# $ per million tokens, (input, output). Checked 2026-07-28 -- Sonnet 5's introductory rate
# runs through 2026-08-31, then rises to (3, 15); update this if that's passed by the time
# you're reading it. Anything not listed here gets a "pricing unknown" note rather than a
# silently wrong number.
PRICE_PER_MTOK = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
}


def summarize_usage(usage_log: list[dict], model: str, date: str) -> dict:
    by_label = Counter()
    tokens_by_label = {}
    for entry in usage_log:
        label = entry["label"]
        tokens_by_label.setdefault(label, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        tokens_by_label[label]["calls"] += 1
        tokens_by_label[label]["input_tokens"] += entry["input_tokens"]
        tokens_by_label[label]["output_tokens"] += entry["output_tokens"]
    total_input = sum(e["input_tokens"] for e in usage_log)
    total_output = sum(e["output_tokens"] for e in usage_log)
    price = PRICE_PER_MTOK.get(model)
    cost = None if price is None else round(total_input / 1e6 * price[0] + total_output / 1e6 * price[1], 4)
    return {"date": date, "model": model, "total_calls": len(usage_log),
            "total_input_tokens": total_input, "total_output_tokens": total_output,
            "estimated_cost_usd": cost, "pricing_known": price is not None,
            "by_stage": tokens_by_label}


def write_csv(rows: list[dict], path: Path) -> None:
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def summarize_routed_usage(host: "RoutingHost", model: str, date: str) -> dict:
    """Same shape as summarize_usage(), plus the routing/local breakdown attached. Cost
    reflects only the Claude-routed calls -- local inference is unmetered (electricity and
    time, not tokens), so folding it into estimated_cost_usd would understate what routing
    actually saves rather than show it."""
    claude_summary = summarize_usage(host.claude.usage_log, model, date)
    local_summary = host.local.usage_summary() if host.local is not None else \
        {"total_calls": 0, "by_stage": {}}
    claude_summary["local"] = local_summary
    claude_summary["routing"] = host.routing_summary()
    claude_summary["total_calls"] += local_summary.get("total_calls", 0)
    return claude_summary


def main() -> None:
    # outlets_config.json's model/top_n (written by the site's settings panel) take priority
    # over the env vars, which predate it and remain a fallback for manual CLI use.
    cfg = news_digest.load_config()
    parser = argparse.ArgumentParser(description="Run the Claude-backed news digest.")
    parser.add_argument("--model", default=cfg.get("model") or os.environ.get("NEWSROOM_MODEL") or "claude-haiku-4-5-20251001")
    parser.add_argument("--top-n", type=int, default=cfg.get("top_n") or 18)
    parser.add_argument("--max-age-hours", type=float, default=news_digest.MAX_AGE_HOURS)
    parser.add_argument("--output-dir", type=Path, default=Path.cwd())
    parser.add_argument("--record-calls", metavar="PATH", type=Path, default=None,
                        help="Append every (stage, system, prompt, response) to a JSONL corpus "
                             "for replaying against candidate local models.")
    parser.add_argument("--routing", metavar="PATH", type=Path, default=None,
                        help="Route each stage to Claude or local inference per this file "
                             "(see routing.json). Omit to run entirely on Claude.")
    parser.add_argument("--profile", metavar="NAME", default=None,
                        help="Routing profile from routing.json: local (zero cost, all "
                             "local), fast-snippets, safe-verify, fast, claude (all "
                             "metered). Overrides the file's active_profile for this run.")
    parser.add_argument("--local-base-url", default="http://127.0.0.1:8001/v1",
                        help="OpenAI-compatible endpoint for local inference (llama-swap).")
    parser.add_argument("--local-model", default="qwen-finetune-q3",
                        help="Model id requested from --local-base-url for stages routed "
                             "local -- default matches routing.json's measured choice. "
                             "routing.json's per-stage \"model\" overrides this.")
    parser.add_argument("--local-timeout", type=float, default=210.0,
                        help="Seconds before abandoning a local call. Measured 2026-07-30: the "
                             "slowest call that ever returned content took 206.8s, while calls "
                             "sitting at the old 300s default returned nothing and burned 44%% of "
                             "the run's model time. An abandoned call yields an EMPTY answer, "
                             "counted per stage and reported at the end of the run.")
    parser.add_argument("--local-concurrency", type=int, default=4,
                        help="Concurrent requests to the local endpoint. The real ceiling is "
                             "llama-server's --parallel N for the loaded model: above that, "
                             "requests queue server-side and this buys nothing. Check with "
                             "curl <model_port>/props | grep total_slots.")
    parser.add_argument("--min-tokens", type=int, default=768,
                        help="Floor on max_tokens for local calls. A reasoning model spends "
                             "budget thinking before it answers, so a floor sized for a terse "
                             "model truncates it before the verdict appears -- which reads as "
                             "a wrong answer, not a config error. The pipeline asks for 90 "
                             "tokens at entailment; v4flash needs 768 to clear its preamble "
                             "(measured: 3 of 7 verdicts came back empty at 512).")
    args = parser.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set in this environment.")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(args.output_dir)
    claude_host = ClaudeHost(args.model, record_path=args.record_calls)
    host = claude_host
    routing_active = args.routing is not None
    if routing_active:
        from local_host import LocalHost
        from routing_host import RoutingHost, load_routes, load_local_models
        routes = load_routes(args.routing, args.profile)
        local_models = load_local_models(args.routing, args.profile)
        needs_local = any(v == "local" for v in routes.values())
        local_host = (LocalHost(args.local_model, base_url=args.local_base_url,
                                min_tokens=args.min_tokens,
                                max_concurrency=args.local_concurrency,
                                timeout=args.local_timeout)
                      if needs_local else None)
        if local_host is not None:
            # Surface a client-cap/server-slot mismatch BEFORE spending an hour on a run
            # that is silently serialising: the symptom (slow) is identical to the cause
            # being anywhere else, so it has to be stated rather than inferred later.
            _w = local_host.concurrency_warning()
            if _w:
                print(f"WARNING: {_w}", file=sys.stderr)
        host = RoutingHost(claude_host, local_host, routes, local_models=local_models)

    result = news_digest.run(host, args.top_n, args.max_age_hours)
    write_csv(result["rows"], args.output_dir / f"news_digest_{result['date']}.csv")
    write_csv(result["rows"], args.output_dir / "current.csv")
    # The scan metadata captions the rows just written, so it goes live only now.
    # Before this, /api/digest still reports the previous run's window and counts --
    # correct, because those describe the rows the site is still serving.
    news_digest.publish_scan(result)

    usage = summarize_routed_usage(host, args.model, result["date"]) if routing_active \
        else summarize_usage(host.usage_log, args.model, result["date"])
    # Output-contract compliance per stage. Most stages fall back silently on an unparseable
    # response, so a run that "succeeded" is not evidence the model was understood.
    # entailment_check no longer fails open (fixed in 9f12424): a reply carrying no verdict is
    # retried once, then published as "unverified", never as clean. Persisted with usage so any host,
    # Claude or local, is judged on the same measurement.
    usage["contract"] = result["meta"].get("contract", {})
    (args.output_dir / f"usage_{result['date']}.json").write_text(
        json.dumps(usage, indent=2), encoding="utf-8")
    cost = f"${usage['estimated_cost_usd']:.4f}" if usage["pricing_known"] else "unknown (model not in PRICE_PER_MTOK)"
    print(f"Wrote {len(result['rows'])} stories for {result['date']}.")
    print(f"Usage: {usage['total_calls']} calls, {usage['total_input_tokens']:,} in / "
          f"{usage['total_output_tokens']:,} out tokens, est. cost {cost}")
    for label, stats in sorted(usage["by_stage"].items(), key=lambda kv: -kv[1]["input_tokens"]):
        print(f"  {label}: {stats['calls']} calls, {stats['input_tokens']:,} in / {stats['output_tokens']:,} out")
    if routing_active:
        routing_info = usage["routing"]
        print(f"Routing: {routing_info['total_claude_calls']} calls -> claude, "
              f"{routing_info['total_local_calls']} calls -> local ({args.local_model})")
        for stage, counts in sorted(routing_info["calls_by_stage"].items()):
            print(f"  {stage}: {counts}")
        if routing_info["unrecognised_stages"]:
            print(f"  WARNING -- unrecognised stages defaulted to Claude: "
                  f"{routing_info['unrecognised_stages']}")
        local_usage = usage.get("local") or {}
        if local_usage.get("total_calls"):
            print(f"Local backend: {local_usage['total_calls']} calls, "
                  f"{local_usage.get('total_input_tokens', 0):,} in / "
                  f"{local_usage.get('total_output_tokens', 0):,} out tokens "
                  f"(unmetered), errors: {local_usage.get('transport_errors') or {}}")
            # A lost call is not a crash -- it becomes an EMPTY answer that the stage
            # parsers cannot distinguish from a real one. Measured 2026-07-30: 8.6% of
            # calls in a real run returned nothing and no report said so. State it loudly
            # and per stage, with the rate, or the next reader draws quality conclusions
            # from a run that was partly blank.
            lost = local_usage.get("lost_by_stage") or {}
            if lost:
                n_lost = sum(lost.values())
                total = local_usage.get("total_calls") or 0
                pct = (100.0 * n_lost / total) if total else 0.0
                print(f"  WARNING -- {n_lost} of {total} local calls ({pct:.1f}%) returned NO "
                      f"content. These became EMPTY answers, not errors:")
                for stage, n in sorted(lost.items(), key=lambda kv: -kv[1]):
                    print(f"    {stage}: {n} lost")
                print("    A lost entailment_check routes its claim to repair (fails safe); a "
                      "lost snippet_writing leaves an empty snippet for the repair pass.")
    print_contract(usage["contract"])
    if args.record_calls:
        print(f"Recorded call corpus -> {args.record_calls}")


def print_contract(contract: dict) -> None:
    """Print per-stage output-contract compliance, worst first, with failing samples."""
    if not contract:
        print("Output contract: no per-stage data recorded.")
        return
    bad = {k: v for k, v in contract.items() if v.get("failed")}
    total_calls = sum(v["calls"] for v in contract.values())
    total_failed = sum(v["failed"] for v in contract.values())
    print(f"Output contract: {total_calls - total_failed}/{total_calls} responses parsed "
          f"({'clean' if not total_failed else str(total_failed) + ' FAILED'})")
    for stage, v in contract.items():
        rate = "n/a" if v["rate"] is None else f"{v['rate'] * 100:.1f}%"
        flag = "  <-- silent fallback fired" if v["failed"] else ""
        print(f"  {stage}: {v['parsed']}/{v['calls']} parsed ({rate}){flag}")
    for stage, v in bad.items():
        for sample in v["samples"]:
            print(f"    [{stage}] unparsed: {sample!r}")


if __name__ == "__main__":
    main()
