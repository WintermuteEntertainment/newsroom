#!/usr/bin/env python3
"""Standalone OpenAI runner for the news digest pipeline."""
from __future__ import annotations

import argparse
import csv
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import OpenAI
import news_digest


class OpenAIHost:
    """Compatibility layer for the pipeline's former Claude host object."""

    def __init__(self, model: str, effort: str) -> None:
        self.client = OpenAI()
        self.model = model
        self.effort = effort

    def reasoning_model(self) -> str:
        return self.model

    def llm(self, requests: list[dict], max_concurrency: int = 6) -> list[dict]:
        def call(request: dict) -> dict:
            response = self.client.responses.create(
                model=request.get("model", self.model),
                instructions=request.get("system", ""),
                input=request["prompt"],
                max_output_tokens=request.get("max_tokens", 300),
                reasoning={"effort": self.effort},
                text={"verbosity": "low"},
            )
            return {"text": response.output_text}

        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            return list(executor.map(call, requests))


CSV_FIELDS = ["rank", "headline", "n_panel_outlets", "panel_size", "n_articles",
              "covered_closest_by", "also_carried_by", "snippet", "entailment",
              "snippet_form", "link", "outlet_links"]


def write_csv(rows: list[dict], path: Path) -> None:
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the OpenAI-backed news digest.")
    parser.add_argument("--model", default=os.environ.get("NEWSROOM_MODEL", "gpt-5.6-terra"))
    parser.add_argument("--effort", default=os.environ.get("NEWSROOM_REASONING_EFFORT", "medium"),
                        choices=["none", "low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--top-n", type=int, default=18)
    parser.add_argument("--max-age-hours", type=float, default=news_digest.MAX_AGE_HOURS)
    parser.add_argument("--output-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set in this environment.")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(args.output_dir)
    result = news_digest.run(OpenAIHost(args.model, args.effort), args.top_n, args.max_age_hours)
    write_csv(result["rows"], args.output_dir / f"news_digest_{result['date']}.csv")
    write_csv(result["rows"], args.output_dir / "current.csv")
    # The scan metadata captions the rows just written, so it goes live only now.
    # Before this, /api/digest still reports the previous run's window and counts --
    # correct, because those describe the rows the site is still serving.
    news_digest.publish_scan(result)
    print(f"Wrote {len(result['rows'])} stories for {result['date']}.")


if __name__ == "__main__":
    main()
