#!/usr/bin/env python3
"""Run the digest on a remote host over SSH and fetch only its published CSV."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Host and path are machine-specific: override with env vars rather than editing this
# file, so a clone on another machine works without a source change.
REMOTE = os.environ.get("NEWSROOM_REMOTE", "newsroom-host")
REMOTE_DIR = os.environ.get("NEWSROOM_REMOTE_DIR", "~/newsroom")
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def run(command: list[str]) -> None:
    result = subprocess.run(command, text=True, capture_output=True, timeout=900)
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        raise SystemExit(details[-2000:] or "Remote refresh failed.")


def main() -> None:
    run(["ssh", *SSH_OPTIONS, REMOTE,
         f"source ~/.profile && cd {REMOTE_DIR} && .venv/bin/python claude_pipeline_runner.py"])
    run(["scp", *SSH_OPTIONS, f"{REMOTE}:{REMOTE_DIR}/current.csv",
         str(ROOT / "news_digest_latest.csv")])
    print(f"Fetched the updated digest from {REMOTE}.")


if __name__ == "__main__":
    main()
