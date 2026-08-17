#!/usr/bin/env python3
"""Replay a recorded Claude call corpus against local models and score each stage.

Answers one question per stage: could this model's output be USED by the pipeline, and
when it could, did it say the same thing Claude said?

Two separate measurements, because they fail differently:

  compliance -- did the pipeline's own parser extract an answer? A failure here is loud in
                the sense that CONTRACT records it, but silent in the digest: the stage
                falls back (see news_digest.CONTRACT for what each fallback does).

  agreement  -- given a parsed answer, does it match Claude's? This is the more dangerous
                axis. A model that is 100% compliant but agrees only 60% of the time is
                WORSE than one that fails to parse, because the pipeline trusts the wrong
                answer and nothing records it. So a stage is only recommended for local
                inference when both numbers hold up.

Scoring uses the pipeline's real parsers (imported from news_digest), never a
reimplementation -- otherwise the benchmark measures the benchmark, not the pipeline.
Snippet writing has no single right answer, so it is scored on usability (non-empty prose
that passes the pipeline's own snippet_flags) rather than agreement.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import news_digest as nd
from local_host import LocalHost, list_models


def score_coherence(text: str, reference: str) -> tuple[bool, bool | None]:
    """(parsed, agrees) -- agreement on the kept-subset and the is_column flag."""
    j, ref = nd.extract_json(text), nd.extract_json(reference)
    ok = isinstance(j, dict) and "keep" in j
    if not ok or not isinstance(ref, dict):
        return ok, None
    same_keep = sorted(i for i in j.get("keep", []) if isinstance(i, int)) == \
                sorted(i for i in ref.get("keep", []) if isinstance(i, int))
    return True, bool(same_keep and bool(j.get("is_column")) == bool(ref.get("is_column")))


def score_same_diff(text: str, reference: str) -> tuple[bool, bool | None]:
    v, ref = nd.read_same_different(text), nd.read_same_different(reference)
    if v is None:
        return False, None
    return True, (None if ref is None else v == ref)


def entail_verdict(u: str) -> str | None:
    """UNSUPPORTED / SUPPORTED / None. Tested in that order deliberately.

    'UNSUPPORTED' CONTAINS 'SUPPORTED' as a substring, so testing for SUPPORTED first would
    read every rejection as an approval -- the same fail-open trap the pipeline has to avoid.
    """
    u = (u or "").upper()
    if "UNSUPPORTED" in u:
        return "UNSUPPORTED"
    return "SUPPORTED" if "SUPPORTED" in u else None


def score_entail(text: str, reference: str) -> tuple[bool, bool | None]:
    """Compliance = a verdict word is present. Agreement = same SUPPORTED/UNSUPPORTED call.

    Checked in UNSUPPORTED-first order because that substring contains 'SUPPORTED': testing
    for SUPPORTED first would read every rejection as an approval -- the same fail-open trap
    the pipeline itself has to avoid.
    """
    v, r = entail_verdict(text), entail_verdict(reference)
    if v is None:
        return False, None
    return True, (None if r is None else v == r)


def score_dedup(text: str, reference: str) -> tuple[bool, bool | None]:
    j, ref = nd.extract_json(text), nd.extract_json(reference)
    ok = isinstance(j, dict) and "same_as" in j
    if not ok or not isinstance(ref, dict):
        return ok, None
    def norm(v):
        return None if isinstance(v, bool) or not isinstance(v, int) else v
    return True, norm(j.get("same_as")) == norm(ref.get("same_as"))


def score_snippet(text: str, reference: str) -> tuple[bool, bool | None]:
    """No single correct snippet exists, so agreement is not meaningful.

    Usability is: non-empty prose that survives the pipeline's own quality gate. A snippet
    that trips snippet_flags gets rewritten or replaced by a placeholder downstream, so
    failing here is a real cost even though it is not a parse error.
    """
    t = (text or "").strip()
    if not t:
        return False, None
    flags = nd.snippet_flags(t) if hasattr(nd, "snippet_flags") else []
    return True, (not flags)


SCORERS = {
    "coherence_audit": score_coherence,
    "fragment_merge": score_same_diff,
    "entailment_check": score_entail,
    "exclusives_dedup": score_dedup,
    "snippet_writing": score_snippet,
}


def load_corpus(path: Path, per_stage: int) -> list[dict]:
    """Read the corpus, keeping at most per_stage rows of each stage.

    Takes the FIRST n of each stage rather than a random sample so every model is replayed
    against byte-identical prompts -- the comparison between models is the point, and a
    per-run sample would confound it.
    """
    rows, seen = [], {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        st = row.get("stage", "other")
        if st not in SCORERS:
            continue
        seen[st] = seen.get(st, 0) + 1
        if seen[st] <= per_stage:
            rows.append(row)
    return rows


def bench(model: str, rows: list[dict], base_url: str, concurrency: int,
          min_tokens: int = 512) -> dict:
    # min_tokens is recorded in the result: a low floor truncates reasoning models before
    # they answer, so a compliance number is only meaningful alongside the floor it ran under.
    host = LocalHost(model, base_url=base_url, max_concurrency=concurrency,
                     min_tokens=min_tokens)
    reqs = [{"prompt": r["prompt"], "system": r.get("system", ""),
             "max_tokens": r.get("max_tokens", 300), "stage": r["stage"]} for r in rows]
    t0 = time.time()
    results = host.llm(reqs)
    elapsed = time.time() - t0

    per_stage: dict[str, dict] = {}
    for row, res in zip(rows, results):
        st = row["stage"]
        acc = per_stage.setdefault(st, {"n": 0, "parsed": 0, "agree": 0, "comparable": 0,
                                        "errors": 0, "samples": [], "confusion": {}})
        acc["n"] += 1
        if res.get("error"):
            acc["errors"] += 1
        parsed, agrees = SCORERS[st](res.get("text", ""), row.get("reference", ""))
        # Direction of disagreement. For entailment the two directions are not equivalent:
        # false_alarm (model says UNSUPPORTED where Claude said SUPPORTED) triggers the
        # rewrite loop and can end up dropping correct sentences, costing ~3x the calls;
        # missed (model says SUPPORTED where Claude said UNSUPPORTED) means the hallucination
        # check silently passed something Claude rejected. A stage can be tolerable with the
        # first and unusable with the second, so the rate alone cannot decide routing.
        if st == "entailment_check" and parsed:
            mv, rv = entail_verdict(res.get("text", "")), entail_verdict(row.get("reference", ""))
            if rv is not None and mv != rv:
                key = "false_alarm" if mv == "UNSUPPORTED" else "missed"
                acc["confusion"][key] = acc["confusion"].get(key, 0) + 1
            elif rv is not None:
                acc["confusion"]["agreed_" + rv.lower()] = \
                    acc["confusion"].get("agreed_" + rv.lower(), 0) + 1
        if st == "fragment_merge" and parsed:
            mv, rv = nd.read_same_different(res.get("text", "")), \
                     nd.read_same_different(row.get("reference", ""))
            if rv is not None and mv != rv:
                # over_merge collapses two real events into one story (overstates prevalence);
                # under_merge leaves one event fragmented (understates it).
                key = "over_merge" if mv == "SAME" else "under_merge"
                acc["confusion"][key] = acc["confusion"].get(key, 0) + 1
        if parsed:
            acc["parsed"] += 1
        elif len(acc["samples"]) < 3:
            acc["samples"].append({"got": (res.get("text") or "")[:180],
                                   "raw": (res.get("raw") or "")[:180],
                                   "error": res.get("error")})
        if agrees is not None:
            acc["comparable"] += 1
            if agrees:
                acc["agree"] += 1

    for st, a in per_stage.items():
        a["compliance"] = round(a["parsed"] / a["n"], 4) if a["n"] else None
        a["agreement"] = round(a["agree"] / a["comparable"], 4) if a["comparable"] else None
    return {"model": model, "min_tokens": min_tokens,
            "elapsed_s": round(elapsed, 1), "n_calls": len(rows),
            "sec_per_call": round(elapsed / len(rows), 2) if rows else None,
            "usage": host.usage_summary(), "by_stage": per_stage}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", type=Path, default=Path("calls_corpus.jsonl"))
    ap.add_argument("--models", nargs="*", default=None,
                    help="Default: every model the endpoint serves.")
    ap.add_argument("--per-stage", type=int, default=20)
    ap.add_argument("--base-url", default="http://127.0.0.1:8001/v1")
    ap.add_argument("--concurrency", type=int, default=2)
    # Reasoning models need headroom for the preamble before the answer; a floor that is too
    # low scores the token budget instead of the model (v4flash: 32/57 truncated at 64).
    ap.add_argument("--min-tokens", type=int, default=512)
    ap.add_argument("--out", type=Path, default=Path("bench_local.json"))
    ap.add_argument("--stages", nargs="*", default=None,
                    help="Limit to these stages (default: all five).")
    args = ap.parse_args()

    rows = load_corpus(args.corpus, args.per_stage)
    if args.stages:
        rows = [r for r in rows if r["stage"] in set(args.stages)]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["stage"]] = counts.get(r["stage"], 0) + 1
    print(f"corpus: {len(rows)} calls  {counts}")

    models = args.models or list_models(args.base_url)
    # Merge into any existing file: models are benchmarked one job at a time (a full sweep
    # exceeds the remote wall-clock cap), and a re-run of one model REPLACES its own row
    # rather than appending a duplicate.
    out = {"corpus": str(args.corpus), "per_stage": args.per_stage,
           "stage_counts": counts, "results": []}
    if args.out.exists():
        try:
            prior = json.loads(args.out.read_text(encoding="utf-8"))
            out["results"] = [r for r in prior.get("results", [])
                              if r.get("model") not in set(models)]
            out["stage_counts"] = {**prior.get("stage_counts", {}), **counts}
        except Exception as exc:
            print(f"  (ignoring unreadable {args.out}: {exc})")
    # Sequential by model: llama-swap holds one model in VRAM and swaps on demand, so
    # interleaving would reload weights between calls and measure swap time as latency.
    for m in models:
        print(f"\n=== {m} ===", flush=True)
        try:
            r = bench(m, rows, args.base_url, args.concurrency, args.min_tokens)
        except Exception as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            out["results"].append({"model": m, "error": f"{type(exc).__name__}: {exc}"})
            continue
        out["results"].append(r)
        print(f"  {r['elapsed_s']}s total, {r['sec_per_call']}s/call")
        for st, a in sorted(r["by_stage"].items()):
            comp = "n/a" if a["compliance"] is None else f"{a['compliance']*100:5.1f}%"
            agr = "n/a" if a["agreement"] is None else f"{a['agreement']*100:5.1f}%"
            conf = f"  {a['confusion']}" if a.get("confusion") else ""
            print(f"  {st:<18} compliance {comp}  agreement {agr}  (n={a['n']}, err={a['errors']}){conf}")
        args.out.write_text(json.dumps(out, indent=2), encoding="utf-8")  # save as we go

    args.out.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
