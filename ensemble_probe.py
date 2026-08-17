"""Do v4flash and qwen miss the SAME fabrications?

If their misses are disjoint, an OR-ensemble (any UNSUPPORTED verdict wins) recovers recall
that neither model has alone -- affordable only because local inference is free.
Reuses news_digest.entail_verdict so the verdict is classified by the same tested code the
pipeline uses (UNSUPPORTED tested before SUPPORTED; a verdict-less reply is None, not clean).
"""
import json, sys, collections
sys.path.insert(0, ".")
import news_digest as nd
from local_host import LocalHost

MODELS = ["v4flash", "qwen-finetune-q3"]
rows = [json.loads(l) for l in open("calls_corpus.jsonl", encoding="utf-8")]
ent = [r for r in rows if r.get("stage") == "entailment_check"]

# Genuine fabrications only: reference says UNSUPPORTED, and the reason is not the
# fail-open defect complaining that no summary text was supplied.
DEFECT = ("no actual summary", "no summary text", "sources are only headlines",
          "only headlines", "no summary was provided")
neg, defect = [], []
for r in ent:
    if nd.entail_verdict(r.get("reference", "")) != "unsupported":
        continue
    (defect if any(d in r["reference"].lower() for d in DEFECT) else neg).append(r)

# Dedupe by prompt: the corpus repeats prompts across the 3 entailment passes.
seen, cases = set(), []
for r in neg:
    if r["prompt"] not in seen:
        seen.add(r["prompt"]); cases.append(r)
pos = [r for r in ent if nd.entail_verdict(r.get("reference", "")) == "supported"]
seenp, pos_cases = set(), []
for r in pos:
    if r["prompt"] not in seenp:
        seenp.add(r["prompt"]); pos_cases.append(r)
pos_cases = pos_cases[:15]  # specificity control: false positives are the ensemble's cost

print(f"entailment rows={len(ent)} unsupported={len(neg)+len(defect)} "
      f"(genuine={len(neg)} defect_artifacts={len(defect)}) distinct_genuine={len(cases)}")
print(f"specificity control: {len(pos_cases)} distinct supported cases\n")

results = {}
for m in MODELS:
    h = LocalHost(m, base_url="http://127.0.0.1:8001/v1")
    per = {}
    for label, batch in (("neg", cases), ("pos", pos_cases)):
        reqs = [{"system": r["system"], "prompt": r["prompt"],
                 "max_tokens": r.get("max_tokens", 512), "stage": "entailment_check"}
                for r in batch]
        out = h.llm(reqs)
        per[label] = [nd.entail_verdict(o.get("text", "")) for o in out]
    results[m] = per
    caught = sum(1 for v in per["neg"] if v == "unsupported")
    fp = sum(1 for v in per["pos"] if v != "supported")
    print(f"{m:18s} recall {caught}/{len(cases)}  false_alarms {fp}/{len(pos_cases)}")

# OR-ensemble: any model saying unsupported (or returning no verdict) flags the snippet.
def flags(vs):
    return [v != "supported" for v in vs]
ens_neg = [any(t) for t in zip(*[flags(results[m]["neg"]) for m in MODELS])]
ens_pos = [any(t) for t in zip(*[flags(results[m]["pos"]) for m in MODELS])]
print(f"\nOR-ensemble        recall {sum(ens_neg)}/{len(cases)}  "
      f"false_alarms {sum(ens_pos)}/{len(pos_cases)}")

print("\nper-case (n=negatives): " + "  ".join(f"{m}" for m in MODELS))
for i in range(len(cases)):
    vs = [results[m]["neg"][i] for m in MODELS]
    tag = "BOTH_MISS" if not ens_neg[i] else ("caught" if all(v == "unsupported" for v in vs) else "ONE_ONLY")
    print(f"  case{i}: " + "  ".join(f"{str(v):12s}" for v in vs) + f"  -> {tag}")

json.dump({"cases": len(cases), "models": results, "ensemble_neg": ens_neg,
           "ensemble_pos": ens_pos, "n_pos": len(pos_cases),
           "defect_artifacts": len(defect)},
          open("ensemble_probe.json", "w"), indent=1)
print("\nwrote ensemble_probe.json")
