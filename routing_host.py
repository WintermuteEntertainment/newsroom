#!/usr/bin/env python3
"""Per-stage routing host: sends each pipeline stage to Claude or to local inference.

The routing table is a measurement, not a preference -- see routing.json for the numbers
behind each choice and `HANDOFF.md` for how they were taken. The short version:

  entailment_check   -> Claude. This is the hallucination guarantee. The best local
                        candidate (v4flash) caught 4 of 7 genuine fabricated claims;
                        routing it local saves $0.073/run and admits ~3.4 fabricated
                        claims per 100-story run, published *and stamped verified*.
  snippet_writing    -> Claude. Local scored 100% on the usability gate, but that gate
                        only asks whether the snippet survives snippet_flags. It says
                        nothing about prose quality, and this is the one stage whose
                        output a reader actually reads.
  coherence_audit    -> local (80% agreement; a miss reorders a section)
  exclusives_dedup   -> local (90%; a miss shows a story twice)
  fragment_merge     -> local (78%; a miss lists two fragments separately)

Design note: the pipeline does not tag requests with a stage -- stage identity lives in
the *system prompt object*, which is why routing keys off SYS_* identity rather than a
field the pipeline would have to learn to send. That keeps news_digest.py unchanged.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import news_digest


# Stage name for each system prompt. Keyed by the prompt text (not identity) so a
# round-trip through JSON or a subprocess still classifies correctly.
STAGE_OF_SYSTEM = {
    news_digest.SYS_COHERE: "coherence_audit",
    news_digest.SYS_MERGE: "fragment_merge",
    news_digest.SYS_SNIP: "snippet_writing",
    news_digest.SYS_ENTAIL: "entailment_check",
    news_digest.SYS_DUP: "exclusives_dedup",
}

DEFAULT_ROUTES = {
    "entailment_check": "claude",
    "snippet_writing": "claude",
    "coherence_audit": "local",
    "exclusives_dedup": "local",
    "fragment_merge": "local",
}


def resolve_profile(data: dict, profile: str | None = None) -> tuple[str, set[str]]:
    """Resolve which profile is active and which stages it lifts back to Claude.

    Returns (profile_name, claude_stages). A profile is a thin OVERLAY on the per-stage
    routes: it names stages to run on Claude, and everything else keeps the route the
    measurements chose. That way there is exactly one source of truth for each stage's
    default, and a profile cannot silently contradict the evidence recorded beside it.

    Precedence: explicit argument > routing.json's "active_profile" > "local". The explicit
    argument wins so a one-off fast run never has to edit (and risk leaving edited) the file.
    """
    profiles = data.get("profiles") or {}
    name = profile or data.get("active_profile") or "local"
    if name not in profiles:
        raise ValueError(
            f"unknown routing profile {name!r}; routing.json defines: {sorted(profiles)}"
        )
    stages = set(data.get("stages") or {})
    claude_stages = set(profiles[name].get("claude_stages") or [])
    bogus = claude_stages - stages
    if bogus:
        # A typo here would silently leave the stage local -- i.e. silently NOT do the
        # thing the user asked for. Fail loudly instead.
        raise ValueError(
            f"profile {name!r} names stages absent from routing.json: {sorted(bogus)}"
        )
    return name, claude_stages


def load_routes(path: str | Path | None, profile: str | None = None) -> dict[str, str]:
    """Read {stage: 'claude'|'local'} from routing.json, falling back to DEFAULT_ROUTES.

    `profile` (or routing.json's "active_profile") lifts selected stages back to Claude
    for a faster, metered run. See resolve_profile().
    """
    if path is None:
        return dict(DEFAULT_ROUTES)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    routes = {k: v["route"] for k, v in data.get("stages", {}).items()}
    if data.get("profiles"):
        _name, claude_stages = resolve_profile(data, profile)
        # Overlay AFTER reading the base routes, and only ever toward Claude. A profile
        # can spend money to go faster; it can never move a stage local, because that
        # decision belongs with the per-stage measurement, not a convenience toggle.
        for stage in claude_stages:
            routes[stage] = "claude"
    unknown = {v for v in routes.values()} - {"claude", "local"}
    if unknown:
        raise ValueError(f"routing.json has unknown route targets: {sorted(unknown)}")
    # A stage missing from the file is a config gap, not an invitation to guess. Default
    # it to Claude: the failure mode of over-spending is recoverable, the failure mode of
    # silently running a correctness-critical stage on an unvalidated model is not.
    for stage in DEFAULT_ROUTES:
        routes.setdefault(stage, "claude")
    return routes


def load_local_models(path: str | Path | None,
                      profile: str | None = None) -> dict[str, str]:
    """Read {stage: model} for locally-routed stages from routing.json.

    routing.json has always declared a per-stage "model", but load_routes() read only
    "route", so the field was silently ignored and every local call used whatever single
    model LocalHost was constructed with. That was harmless while one model served all
    local stages; it is wrong for a fully-local pipeline, where the strongest entailer is
    not the best choice for the cheap high-volume stages.

    Only locally-routed stages are returned. A "model" on a Claude-routed stage names a
    Claude model and must not be handed to llama-swap.
    """
    if path is None:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    # A profile that lifts a stage to Claude must also drop its local model here, or the
    # stage would carry a llama-swap model name into a Claude request. Resolve the SAME
    # profile load_routes() resolves -- both go through resolve_profile() so they cannot
    # disagree about which stages are local.
    lifted: set[str] = set()
    if data.get("profiles"):
        _name, lifted = resolve_profile(data, profile)
    return {k: v["model"] for k, v in data.get("stages", {}).items()
            if v.get("route") == "local" and v.get("model") and k not in lifted}


class RoutingHost:
    """Dispatches each request to Claude or a local endpoint based on its stage.

    Presents the same interface as ClaudeHost/LocalHost -- llm(requests) returning results
    positionally matched to the input -- so news_digest.py needs no changes.
    """

    def __init__(self, claude_host, local_host, routes: dict[str, str] | None = None,
                 unknown_stage_route: str = "claude",
                 local_models: dict[str, str] | None = None) -> None:
        self.claude = claude_host
        self.local = local_host
        self.routes = dict(routes or DEFAULT_ROUTES)
        # Per-stage local model. Empty means "use whatever LocalHost was built with", which
        # is the single-local-model behaviour every earlier run had.
        self.local_models = dict(local_models or {})
        # An unrecognised system prompt means someone added a stage without updating the
        # table. Route it to Claude and record it, rather than sending unknown work to an
        # unvalidated backend.
        self.unknown_stage_route = unknown_stage_route
        self.dispatch_log: list[dict] = []
        self.unknown_stages: set[str] = set()
        self._lock = threading.Lock()

    def reasoning_model(self) -> str:
        """The model news_digest.py stamps onto every request it builds.

        There is no single right answer under routing -- that is the point -- so this
        returns the Claude model and llm() rewrites the stamp for locally-routed requests
        (see _for_local). Claude is the safe default for the stamp because an unrecognised
        stage also routes to Claude: the stamp and the destination agree by construction.
        """
        return self.claude.reasoning_model()

    def stage_of(self, request: dict) -> str:
        return STAGE_OF_SYSTEM.get(request.get("system", ""), "other")

    def route_for(self, stage: str) -> str:
        if stage in self.routes:
            return self.routes[stage]
        with self._lock:
            self.unknown_stages.add(stage)
        return self.unknown_stage_route

    def _for_local(self, request: dict, stage: str) -> dict:
        """Adapt a request for the local backend: drop the Claude model stamp, add the stage.

        Two edits, both necessary:

        - The Claude model stamp must go. news_digest.py calls reasoning_model() ONCE and
          puts that string on every request ("model": model). Forwarded unchanged, a locally
          routed request would ask llama-swap for a Claude model id it does not serve --
          every call would fail transport and every locally-routed stage would silently fall
          back, while the run still reported success. Where routing.json names a per-stage
          local model, that model replaces the stamp; otherwise the key is dropped and
          LocalHost falls back to its constructor model (local_host.py: request.get("model")
          or self.model).
        - The stage must be added. LocalHost attributes token usage from request["stage"],
          which the pipeline never sets, so without this every local call is filed under
          "other" and the per-stage local breakdown is lost -- the exact number needed to
          decide future routing.

        Returns a copy: mutating the caller's dict would corrupt request objects
        news_digest.py still holds.
        """
        adapted = dict(request)
        model = self.local_models.get(stage)
        if model:
            adapted["model"] = model
        else:
            adapted.pop("model", None)
        adapted.setdefault("stage", stage)
        return adapted

    def llm(self, requests, max_concurrency: int = 6) -> list[dict]:
        if isinstance(requests, dict):
            requests = [requests]
        if not requests:
            return []

        # Split by destination, remembering each request's original position. A batch can
        # mix stages, and returning results out of order would silently pair a verdict with
        # the wrong snippet -- so positions are restored explicitly.
        buckets: dict[str, list[tuple[int, dict, str]]] = {}
        for i, req in enumerate(requests):
            stage = self.stage_of(req)
            dest = self.route_for(stage)
            buckets.setdefault(dest, []).append((i, req, stage))
            with self._lock:
                self.dispatch_log.append({"stage": stage, "route": dest})

        # llama-swap holds ONE model in memory at a time, so a local batch mixing models
        # would force a swap per request -- minutes of reload dominating seconds of
        # inference. Split the local bucket by model so each model is loaded once and all
        # of its calls run contiguously. Claude has no such constraint and stays one batch.
        ordered: list[tuple[str, list[tuple[int, dict, str]]]] = []
        for dest, items in buckets.items():
            if dest != "local" or not self.local_models:
                ordered.append((dest, items))
                continue
            by_model: dict[str, list[tuple[int, dict, str]]] = {}
            for it in items:
                by_model.setdefault(self.local_models.get(it[2], ""), []).append(it)
            ordered.extend((dest, grp) for grp in by_model.values())

        results: list[dict | None] = [None] * len(requests)
        for dest, items in ordered:
            host = self.claude if dest == "claude" else self.local
            if host is None:
                raise RuntimeError(
                    f"{len(items)} request(s) routed to '{dest}' but no {dest} host was "
                    "configured; check the routing table against how the host was built")
            batch = [req if dest == "claude" else self._for_local(req, stage)
                     for _, req, stage in items]
            out = host.llm(batch, max_concurrency=max_concurrency)
            if len(out) != len(batch):
                raise RuntimeError(
                    f"{dest} host returned {len(out)} results for {len(batch)} requests; "
                    "positional matching would be wrong")
            for (i, _, _), res in zip(items, out):
                results[i] = res

        missing = [i for i, r in enumerate(results) if r is None]
        if missing:
            raise RuntimeError(f"no result for request positions {missing}")
        return results

    # -- reporting ---------------------------------------------------------------------
    def routing_summary(self) -> dict:
        by = {}
        for entry in self.dispatch_log:
            k = entry["stage"]
            b = by.setdefault(k, {"claude": 0, "local": 0})
            b[entry["route"]] += 1
        return {
            "routes": self.routes,
            "calls_by_stage": by,
            "total_claude_calls": sum(b["claude"] for b in by.values()),
            "total_local_calls": sum(b["local"] for b in by.values()),
            # Non-empty means a stage was dispatched that the routing table has never been
            # measured against. Worth surfacing in the run report rather than in a log.
            "unrecognised_stages": sorted(self.unknown_stages),
        }

    def usage_summary(self) -> dict:
        """Combined usage from both backends, plus the routing breakdown."""
        out = {"routing": self.routing_summary()}
        for name, host in (("claude", self.claude), ("local", self.local)):
            if hasattr(host, "usage_summary"):
                out[name] = host.usage_summary()
            elif hasattr(host, "usage_log"):
                out[name] = {"total_calls": len(host.usage_log)}
        return out
