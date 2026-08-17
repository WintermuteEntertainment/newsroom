#!/usr/bin/env python3
"""Local web viewer for the news digest CSV.

Run:  python server.py
Open: http://127.0.0.1:8767

Set NEWSROOM_REFRESH_COMMAND to a command that writes a new
news_digest_YYYY-MM-DD.csv into this folder to enable browser refreshes.
"""
from __future__ import annotations

import csv
import hmac
import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

import sys

import news_digest

ROOT = Path(__file__).resolve().parent
# Default to a ROUTED run. Deploying a fully-local routing.json is not enough on its own:
# without --routing the runner ignores the config entirely and sends every stage to the API.
# That is exactly what happened on 2026-07-30 -- NEWSROOM_REFRESH_COMMAND was set to a bare
# `claude_pipeline_runner.py`, so refreshes billed $0.1712 across all five stages while
# routing.json said fully local. Defaulting to --routing means the cheap path is what you get
# by not thinking about it, and an explicit env override is what costs money.
DEFAULT_REFRESH_COMMAND = ".venv/bin/python claude_pipeline_runner.py --routing routing.json"
ROUTING_PATH = "routing.json"

# --- Access log --------------------------------------------------------------------------
# [2026-08-13] The server binds 127.0.0.1 behind a Cloudflare tunnel, so the socket peer is
# ALWAYS localhost -- self.client_address can never identify a visitor. The real client
# exists only in the tunnel's CF-Connecting-IP header. Before this, log_message() also
# dropped the address entirely and stdout went to whichever console launched the server, so
# "how many people use this?" was unanswerable: the only surviving log covered 16 hours of
# 2026-08-03 and could not tell one person reloading from fourteen visitors.
#
# Rotating file, not stdout, so history survives a restart. NOTE: this records visitor IP
# and user-agent -- personal data. access.log is gitignored; keep it that way.
ACCESS_LOG_PATH = ROOT / "access.log"
_access_log = logging.getLogger("newsroom.access")
_access_log.setLevel(logging.INFO)
_access_log.propagate = False          # don't duplicate into any root handler
if not _access_log.handlers:           # module can be re-imported by tests
    _rot = RotatingFileHandler(ACCESS_LOG_PATH, maxBytes=5_000_000,
                               backupCount=5, encoding="utf-8")
    _rot.setFormatter(logging.Formatter("%(asctime)s\t%(message)s"))
    _access_log.addHandler(_rot)


def available_profiles() -> dict:
    """Read the routing profiles for the UI dropdown.

    Returns {} when routing.json is missing or malformed rather than raising: a broken
    config should degrade the UI to "no dropdown", not take down the whole server.
    """
    try:
        data = json.loads((ROOT / ROUTING_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    profiles = data.get("profiles") or {}
    if not profiles:
        return {}
    return {
        "active": data.get("active_profile") or "local",
        "profiles": [
            {"name": name,
             "cost": p.get("projected_cost_usd"),
             "claude_stages": p.get("claude_stages") or [],
             # A profile is metered if it sends ANY stage to the API. Derived from
             # claude_stages, never from projected_cost_usd: the cost figure is a
             # measurement that can be stale or absent, while claude_stages is the
             # thing that actually bills. The client uses this to decide whether to
             # ask for the password; the server re-derives it and does not trust it.
             "metered": bool(p.get("claude_stages")),
             "why": p.get("why") or ""}
            for name, p in profiles.items()
        ],
    }


def effective_profile(requested: str | None) -> str | None:
    """The profile a run will ACTUALLY use if `requested` is passed to the runner.

    Omitting --profile does not mean "free": the runner falls back to routing.json's
    active_profile. A gate that only inspected the requested value would therefore wave
    through an unauthenticated refresh whenever active_profile happened to be metered.
    """
    if requested:
        return requested
    return available_profiles().get("active")


def profile_is_metered(name: str | None) -> bool:
    """Whether running `name` bills the API.

    Unknown names are treated as metered. This is only reached for a name that passed
    the refresh_command_for() allowlist, so it should not happen -- but the safe default
    for "I cannot tell what this costs" is to require the password, not to spend.
    """
    profiles = available_profiles().get("profiles") or []
    if not profiles:
        # No routing config: the runner cannot be given a profile at all, so the run is
        # whatever REFRESH_COMMAND already does. Nothing for this gate to add.
        return False
    for entry in profiles:
        if entry["name"] == name:
            return entry["metered"]
    return True


def refresh_command_for(profile: str | None) -> str:
    """Build the refresh command for a requested profile.

    SECURITY: REFRESH_COMMAND is run with shell=True, so a profile name coming from a
    browser is untrusted input on a shell command line. The name is therefore checked
    against the profiles actually defined in routing.json -- an allowlist of known-good
    literals -- and anything else is rejected before it can reach the shell. Never
    interpolate the raw request value, even "just to build an error message".
    """
    if not profile:
        return REFRESH_COMMAND
    known = {p["name"] for p in available_profiles().get("profiles", [])}
    if profile not in known:
        raise ValueError("unknown profile")
    if "claude_pipeline_runner" not in REFRESH_COMMAND:
        # A custom refresh command may not accept --profile at all; appending it would
        # break the run. Honour the command as configured and ignore the request.
        return REFRESH_COMMAND
    return f"{REFRESH_COMMAND} --profile {profile}"
REFRESH_COMMAND = os.environ.get("NEWSROOM_REFRESH_COMMAND", DEFAULT_REFRESH_COMMAND).strip()
if REFRESH_COMMAND and "claude_pipeline_runner" in REFRESH_COMMAND and "--routing" not in REFRESH_COMMAND:
    print("WARNING: NEWSROOM_REFRESH_COMMAND runs the pipeline WITHOUT --routing, so every "
          "stage will bill the API and routing.json is ignored. Add: --routing routing.json",
          file=sys.stderr, flush=True)
# A routed run sends three stages to local inference, which is far slower per call than the
# API: a measured 100-story routed run took ~27 minutes against ~4 all-Claude. The old
# hardcoded 900s (15 min) killed it well before completion. 3600s gives that run better than
# 2x headroom; override with NEWSROOM_REFRESH_TIMEOUT if the local backend gets slower.
REFRESH_TIMEOUT = float(os.environ.get("NEWSROOM_REFRESH_TIMEOUT", "3600"))

# --- scheduled refresh -----------------------------------------------------------------
# Keep the digest fresh without anything outside this process: no cron, no Task Scheduler
# entry, no assistant. Added 2026-08-05 because there was no scheduled refresh at all and
# the page could sit on yesterday's stories until somebody pressed the button.
#
# The decision is made from the newest digest CSV's mtime ON DISK, never from a timer this
# process has been counting. That is the whole point: nothing supervises this server, so it
# WILL be down sometimes, and an in-memory interval would restart its count from zero on
# every restart and could postpone a refresh indefinitely. Reading the file means a server
# that was off for six hours notices immediately that the digest is six hours old.
AUTO_REFRESH_HOURS = float(os.environ.get("NEWSROOM_AUTO_REFRESH_HOURS", "2"))
# How often to look. Cheap (one stat call), so the resolution costs nothing; 5 minutes means
# a due refresh starts within 5 minutes of becoming due rather than up to 2 hours late.
AUTO_REFRESH_CHECK_SECONDS = float(os.environ.get("NEWSROOM_AUTO_REFRESH_CHECK_SECONDS", "300"))
# Wait before the FIRST check. A restart is usually a deploy, and a run takes ~90 minutes on
# the local GPU -- kicking one off the instant the server comes back would seize the GPU
# before whoever just deployed had a chance to look at anything. Five minutes is enough to
# restart, verify, and get out of the way.
AUTO_REFRESH_GRACE_SECONDS = float(os.environ.get("NEWSROOM_AUTO_REFRESH_GRACE_SECONDS", "300"))

refresh_lock = threading.Lock()
# "contract" holds the parsed output-contract report of the LAST completed run (see
# parse_contract_report) and "log" its log filename. Declared here so /api/refresh has
# one stable shape -- a key that only appears after the first run makes every client do
# presence checks. {} means "no report", NOT "everything parsed".
# "trigger" says who started the current/last run -- "manual" for the web button, "scheduled"
# for the timer below. Without it a run that appears while nobody is at the keyboard looks
# like a fault rather than the schedule working.
refresh_state = {"running": False, "error": None, "profile": None,
                 "contract": {}, "log": None, "trigger": None}

# Gate for /api/config (what the pipeline scans) and for /api/refresh when the run would bill
# the API. Not real auth -- it stops a casual visitor to the public site, not anyone who reads
# the source. Rotated 2026-08-02 after the previous value was briefly exposed in a public repo.
#
# The live password is NOT in this file. It is read, in order, from:
#   1. NEWSROOM_CONFIG_PASSWORD, if set and non-empty
#   2. config_password.txt beside this file (gitignored) -- first non-blank line
# and if neither supplies one, the gate REFUSES EVERYTHING rather than falling back to a
# literal. That is a deliberate reversal of the previous behaviour: the old code defaulted to
# the working password so a hand-restart could not weaken the gate, but the cost was that the
# real password lived in source and therefore in git history forever. Failing closed keeps the
# secret out of the repo, and the failure is loud and immediate (nobody can unlock) rather than
# silent, so a missing file gets noticed and fixed in seconds.
#
# The file exists because this server is started by hand with no service manager, so an
# environment variable set in one shell is gone at the next restart; a file on disk is not.
#
# A value that is set but EMPTY is treated as absent, not as an empty password. Without this,
# `export NEWSROOM_CONFIG_PASSWORD=$SOMETHING_UNSET`, or a blank first line in the file, would
# make the password "" -- and an empty submitted password would then compare equal to it.
PASSWORD_FILE = ROOT / "config_password.txt"


def _load_config_password() -> str:
    """Read the config password from the environment, else the gitignored file, else "" ."""
    from_env = os.environ.get("NEWSROOM_CONFIG_PASSWORD", "").strip()
    if from_env:
        return from_env
    try:
        for line in PASSWORD_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                return line.strip()
    except OSError:
        pass
    return ""


CONFIG_PASSWORD = _load_config_password()
if not CONFIG_PASSWORD:
    # Loud on startup: the settings panel and metered refresh are unusable until this is fixed.
    print(f"WARNING: no config password found (set NEWSROOM_CONFIG_PASSWORD or write "
          f"{PASSWORD_FILE.name}); the settings gate will reject every password.", flush=True)


def password_ok(supplied: object) -> bool:
    """The single place the config password is checked.

    Three paths need this -- unlocking the settings panel, saving settings, and starting a
    metered run -- and having one helper means a change to how passwords are compared cannot
    land in one path and be silently missed in the others.

    compare_digest rather than == so the comparison does not short-circuit on the first wrong
    character. A timing attack on a short password behind a tunnel is not the realistic threat
    here; the point is that this is the pattern worth having in the file that gets copied.
    """
    if not isinstance(supplied, str) or not supplied:
        return False
    # Belt and braces on the empty case: if CONFIG_PASSWORD were ever empty, compare_digest("",
    # "") is True and the gate would be open. The definition above prevents that, and this
    # prevents a future edit there from reopening it.
    if not CONFIG_PASSWORD:
        return False
    return hmac.compare_digest(supplied, CONFIG_PASSWORD)


def newest_digest() -> Path | None:
    files = list(ROOT.glob("news_digest_*.csv"))
    return max(files, key=lambda path: path.stat().st_mtime) if files else None


def scan_meta(date: str) -> dict | None:
    """The pipeline's per-run diagnostics (headlines scanned, stale/undated drops, per-outlet
    counts, any feed that failed) live in raw_headlines_<date>.json alongside the CSV, but that
    file also carries every raw headline it fetched -- too big to ship to the browser whole, so
    only the small 'meta' section is pulled out here."""
    path = ROOT / f"raw_headlines_{date}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {"fetched_utc": data.get("fetched_utc"), "completed_utc": data.get("completed_utc"),
            "max_age_hours": data.get("max_age_hours"),
            **(data.get("meta") or {})}


def config_payload() -> dict:
    cfg = news_digest.load_config()
    resolved = news_digest.resolve_outlets(cfg)
    return {
        "active_outlets": resolved["active_outlets"],
        "removed_defaults": resolved["removed_defaults"],
        "model": cfg.get("model") or os.environ.get("NEWSROOM_MODEL") or "claude-haiku-4-5-20251001",
        "top_n": cfg.get("top_n") or 18,
        "max_age_hours": cfg.get("max_age_hours") or news_digest.DEFAULT_MAX_AGE_HOURS,
    }


def digest_payload() -> dict:
    digest = newest_digest()
    if not digest:
        # Still report routing and whether refresh is wired up. Without these the
        # settings panel loses its routing control exactly when it is most needed --
        # a fresh install, or after a failed first run, has no CSV yet and starting
        # a run is the only useful action on the page.
        return {"error": "No news_digest_YYYY-MM-DD.csv file was found.", "rows": [],
                "refreshConfigured": bool(REFRESH_COMMAND),
                "routing": available_profiles()}
    with digest.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    date = digest.stem.removeprefix("news_digest_")
    return {
        "date": date,
        "updated": datetime.fromtimestamp(digest.stat().st_mtime, timezone.utc).isoformat(),
        "rows": rows,
        "refreshConfigured": bool(REFRESH_COMMAND),
        "routing": available_profiles(),
        "scan": scan_meta(date),
        # The AUTHORITATIVE answer to "is a run in flight". The front end used to infer this by
        # comparing scan.fetched_utc against scan.completed_utc, which is only a proxy: a run
        # writes fetched_utc when it starts reading feeds and completed_utc when it finishes, so
        # a run that DIES in between leaves fetched_utc set and completed_utc null forever, and
        # the banner reads "refreshing now" indefinitely with nothing running. Observed
        # 2026-08-04: fetched_utc 08:55, completed_utc null, no pipeline process alive, banner
        # stuck for 6 hours. This process holds the refresh lock, so it knows the truth.
        "refreshRunning": bool(refresh_state.get("running")),
    }


# Matched against the pipeline's real printed report (claude_pipeline_runner.py:269-277):
#     Output contract: 220/224 responses parsed (4 FAILED)
#       fragment_merge: 4/7 parsed (57.1%)  <-- silent fallback fired
#         [fragment_merge] unparsed: 'ghiSAME'
# Deliberately not anchored at the end: the runner appends a "<-- silent fallback fired" marker
# to any stage below 100%, and anchoring would reject exactly the lines that matter most.
CONTRACT_TOTAL = re.compile(
    r"^Output contract:\s*(?P<parsed>\d+)/(?P<calls>\d+)\s+responses parsed")
CONTRACT_STAGE = re.compile(
    r"^\s+(?P<stage>[a-z_]+):\s*(?P<parsed>\d+)/(?P<calls>\d+)\s+parsed\s*\((?P<rate>[\d.]+)%\)")
CONTRACT_SAMPLE = re.compile(r"^\s+\[(?P<stage>[a-z_]+)\]\s+unparsed:\s*(?P<sample>.+)$")


def parse_contract_report(text: str) -> dict:
    """Per-stage parse rates lifted from the pipeline's printed output-contract report.

    Reads the log rather than importing the pipeline: the pipeline runs as a separate process
    (its own interpreter, possibly its own working copy), so its in-memory counters are
    unreachable from here. The printed report is the only shared surface.

    A stage can appear more than once -- the entailment/rewrite loop re-runs stages -- so later
    lines overwrite earlier ones: the final report printed is the run's real tally.

    Returns {} when the text carries no report, which is itself the signal that the run died
    before printing one. An empty result must never be rendered as "everything parsed"; callers
    check for presence, not for a zero failure count.
    """
    stages: dict[str, dict] = {}
    total: dict = {}
    samples: dict[str, list] = {}
    for line in text.splitlines():
        m = CONTRACT_STAGE.match(line)
        if m:
            stages[m.group("stage")] = {"parsed": int(m.group("parsed")),
                                        "calls": int(m.group("calls")),
                                        "rate": float(m.group("rate"))}
            continue
        m = CONTRACT_TOTAL.match(line)
        if m:
            total = {"parsed": int(m.group("parsed")), "calls": int(m.group("calls"))}
            continue
        m = CONTRACT_SAMPLE.match(line)
        if m:
            # Keep a few per stage. These are what turn "57% parsed" into a diagnosable fault:
            # the 2026-07-30 fragment_merge failures were a model emitting a joke and two
            # run-together verdicts, which the rate alone could never have revealed.
            got = samples.setdefault(m.group("stage"), [])
            if len(got) < 3:
                got.append(m.group("sample").strip()[:300])
    if not stages and not total:
        return {}
    return {"total": total, "stages": stages, "samples": samples}


def run_refresh(command: str | None = None) -> None:
    """Runs the pipeline in the background so the triggering HTTP request can
    return immediately -- Cloudflare (and most reverse proxies) kill origin
    responses that take longer than ~100s, well short of a full pipeline run."""
    before = newest_digest()
    before_mtime = before.stat().st_mtime if before else None
    # Stream the pipeline's output to a file rather than capture_output=True. Three reasons,
    # all of them things that bit us:
    #   1. The pipeline PRINTS its output-contract report to stdout (per-stage "N/M responses
    #      parsed" plus samples of unparsed replies). capture_output=True buffered that into a
    #      pipe this function then dropped on the floor -- only .stderr was ever read, and only
    #      on failure. So every refresh triggered from the web UI silently discarded the one
    #      artifact that says whether the models honoured their contracts.
    #   2. A buffered pipe is unreadable until the process exits. A file can be tailed while a
    #      50-minute run is in progress, which is the difference between "is it stuck?" being
    #      answerable and being a guess.
    #   3. On TimeoutExpired the CompletedProcess never exists, so the buffered output died with
    #      it -- output was lost in exactly the case where it was most wanted. A file already
    #      holds everything written up to the kill.
    # stderr is folded into the same file (stderr=STDOUT) so interleaved ordering is preserved;
    # the error message reads the tail back, which is what .stderr used to supply.
    log_path = ROOT / f"pipeline_run_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.log"
    refresh_state["log"] = log_path.name

    def log_tail(n: int = 2000) -> str:
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")[-n:]
        except OSError:
            return "(no output captured)"

    try:
        with log_path.open("w", encoding="utf-8") as sink:
            sink.write(f"$ {command or REFRESH_COMMAND}\n\n")
            sink.flush()
            result = subprocess.run(
                command or REFRESH_COMMAND, cwd=ROOT, shell=True, text=True,
                stdout=sink, stderr=subprocess.STDOUT, timeout=REFRESH_TIMEOUT,
            )
        refresh_state["contract"] = parse_contract_report(log_tail(20000))
        after = newest_digest()
        if result.returncode:
            refresh_state["error"] = f"Pipeline failed. {log_tail()}"
        elif after is None or after.stat().st_mtime == before_mtime:
            refresh_state["error"] = "The command finished but did not write a newer digest CSV."
        else:
            refresh_state["error"] = None
    except subprocess.TimeoutExpired:
        refresh_state["contract"] = parse_contract_report(log_tail(20000))
        refresh_state["error"] = (
            f"Pipeline timed out after {REFRESH_TIMEOUT / 60:.0f} minutes. "
            f"Last output: {log_tail(600)}")
    finally:
        refresh_state["running"] = False
        refresh_lock.release()


def start_refresh(command: str, profile: str | None = None, trigger: str = "manual") -> bool:
    """Start a pipeline run unless one is already going. True if this call started it.

    The ONE place that takes refresh_lock and seeds refresh_state. Two callers now need that
    sequence -- the web button and the scheduled timer -- and duplicating it is how they
    drift: a field added to the state in one path and missed in the other would show the
    front end a fresh run wearing the previous run's contract report.

    Non-blocking on purpose. A scheduled tick that queued behind a 90-minute run would fire
    the instant that run finished, which is the opposite of "every 2 hours".
    """
    if not refresh_lock.acquire(blocking=False):
        return False
    refresh_state["running"] = True
    refresh_state["error"] = None
    refresh_state["profile"] = profile
    refresh_state["trigger"] = trigger
    # Drop the previous run's report. Leaving it would show the new run's "running" state
    # beside the old run's parse rates, which reads as live data about the wrong run.
    refresh_state["contract"] = {}
    refresh_state["log"] = None
    threading.Thread(target=run_refresh, args=(command,), daemon=True).start()
    return True


def digest_age_seconds() -> float | None:
    """Seconds since the newest digest CSV was written, or None if there is no digest.

    Read off the filesystem on every call rather than remembered in a variable. That is the
    whole reason the schedule survives this server being down: an in-memory interval would
    restart its count at zero on every restart, so a server that bounced every 90 minutes
    would never reach a 2-hour mark and would never refresh at all.
    """
    digest = newest_digest()
    if digest is None:
        return None
    try:
        return max(0.0, time.time() - digest.stat().st_mtime)
    except OSError:
        return None


def auto_refresh_due(age_seconds: float | None, interval_hours: float) -> bool:
    """Whether a scheduled refresh should start now.

    Kept pure so the rule can be tested without a clock, a filesystem or a GPU.

    `age_seconds is None` means there is no digest at all -- the site has nothing to show,
    which is the most due state there is, not an unknown to be skipped.
    """
    if interval_hours <= 0:               # 0 disables the schedule entirely
        return False
    if age_seconds is None:
        return True
    return age_seconds >= interval_hours * 3600


def auto_refresh_tick() -> str:
    """One scheduled check. Returns the decision it made, for logging and for tests."""
    if not REFRESH_COMMAND:
        return "not_configured"
    if not auto_refresh_due(digest_age_seconds(), AUTO_REFRESH_HOURS):
        return "not_due"
    # NEVER let the schedule spend money. An unattended timer on a metered profile would
    # bill every couple of hours with nobody watching. The manual path requires a password
    # for exactly this reason, and a timer cannot supply one -- so the honest behaviour is
    # to skip and say why, not to run it as though someone had authorised it.
    if profile_is_metered(effective_profile(None)):
        return "skipped_metered"
    if not start_refresh(REFRESH_COMMAND, profile=None, trigger="scheduled"):
        return "already_running"
    return "started"


def auto_refresh_loop() -> None:
    """Background scheduler thread. Never raises out: a bad tick must not end the schedule."""
    time.sleep(AUTO_REFRESH_GRACE_SECONDS)
    while True:
        try:
            outcome = auto_refresh_tick()
            # Only the interesting outcomes. Logging "not_due" every 5 minutes would bury
            # the two lines anyone actually needs to see in the server log.
            if outcome in ("started", "skipped_metered"):
                print(f"[auto-refresh] {outcome}", flush=True)
        except Exception as exc:
            # A scheduler that dies on one bad tick is worse than no scheduler, because it
            # looks like it is still there. Report and keep the loop alive.
            print(f"[auto-refresh] check failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(AUTO_REFRESH_CHECK_SECONDS)


class NewsroomHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        """Tell caches not to hold onto anything this server produces.

        Everything here is either live data (the API) or a small file that must match the
        deployed commit (index.html, app.js, styles.css) -- nothing benefits from being
        cached, and stale copies are actively wrong.

        This exists because of a real incident: SimpleHTTPRequestHandler sends no
        Cache-Control at all, so the Cloudflare tunnel in front of this server fell back to
        its own default for static extensions and cached app.js/styles.css for 4 hours
        (observed: cf-cache-status HIT, age 1869, max-age 14400). A deploy updated
        index.html at the edge but kept serving the PREVIOUS app.js, so the page had markup
        referencing an element that the stale script never populated. Declaring no-store
        here makes the edge revalidate every time, so a deploy is live the moment the
        origin files change.
        """
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/digest":
            return self.send_json(digest_payload())
        if path == "/api/refresh":
            return self.send_json(dict(refresh_state))
        if path == "/api/config":
            return self.send_json(config_payload())
        return super().do_GET()

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/refresh":
            return self.handle_refresh()
        if path == "/api/config":
            return self.handle_config_save()
        if path == "/api/unlock":
            return self.handle_unlock()
        return self.send_error(HTTPStatus.NOT_FOUND)

    def handle_unlock(self):
        """Checks a password WITHOUT doing anything else.

        The settings panel needs to know whether a password is right before it shows the panel.
        It cannot learn that from /api/config, because that endpoint's POST also SAVES -- so
        using it as a check would either write settings the user has not chosen yet, or require
        sending a no-op save, which is fragile.

        This endpoint deliberately does nothing on success but say yes. It reveals no settings
        and changes no state, so it is safe to expose: an attacker learns exactly what they
        already knew from POSTing to /api/config, which is whether a guess was right.
        """
        try:
            body = self.read_json_body()
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "Malformed request body."}, HTTPStatus.BAD_REQUEST)
        if not password_ok(body.get("password")):
            return self.send_json({"error": "Incorrect password."}, HTTPStatus.UNAUTHORIZED)
        return self.send_json({"ok": True})

    def handle_refresh(self):
        if not REFRESH_COMMAND:
            return self.send_json({
                "error": "Refresh is not configured. Set NEWSROOM_REFRESH_COMMAND to the command that runs your pipeline and writes a CSV here."
            }, HTTPStatus.NOT_IMPLEMENTED)
        # Profile is optional: no body, or a body without one, keeps the configured
        # default. Parse BEFORE taking the lock so a bad request cannot leave it held.
        try:
            body = self.read_json_body()
        except (ValueError, json.JSONDecodeError):
            body = {}
        requested = (body or {}).get("profile")
        try:
            command = refresh_command_for(requested)
        except ValueError:
            # Deliberately does not echo the requested value back.
            return self.send_json(
                {"error": "Unknown routing profile."}, HTTPStatus.BAD_REQUEST)
        # A metered run spends real money, so it needs the password -- hiding the
        # dropdown in the locked settings panel is UX only, and anyone reading the page
        # source can POST here directly. A free (all-local) refresh stays open: it costs
        # nothing, and requiring a password to press the site's own main button would be
        # friction with no benefit. The effective profile is resolved first because
        # omitting one falls back to routing.json's active_profile, which may be metered.
        if profile_is_metered(effective_profile(requested)):
            if not password_ok((body or {}).get("password")):
                return self.send_json(
                    {"error": "This routing profile bills the API and is "
                              "password-protected."}, HTTPStatus.UNAUTHORIZED)
        if not start_refresh(command, profile=requested or None, trigger="manual"):
            return self.send_json({"error": "A refresh is already running."}, HTTPStatus.CONFLICT)
        return self.send_json({"running": True, "profile": requested or None},
                              HTTPStatus.ACCEPTED)

    def handle_config_save(self):
        try:
            body = self.read_json_body()
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "Malformed request body."}, HTTPStatus.BAD_REQUEST)
        if not password_ok(body.get("password")):
            return self.send_json({"error": "Incorrect password."}, HTTPStatus.UNAUTHORIZED)
        cfg = {"removed_outlets": body.get("removed_outlets") or [],
               "added_outlets": body.get("added_outlets") or [],
               "model": (body.get("model") or "").strip() or None,
               "top_n": body.get("top_n"), "max_age_hours": body.get("max_age_hours")}
        error = news_digest.validate_config(cfg)
        if error:
            return self.send_json({"error": error}, HTTPStatus.BAD_REQUEST)
        news_digest.save_config(cfg)
        return self.send_json(config_payload())

    def log_message(self, format, *args):
        line = format % args
        print(f"[{self.log_date_time_string()}] {line}")
        # Identify the visitor from tunnel headers. log_message() is also reached via
        # log_error() before/without a parsed request, so tolerate missing headers rather
        # than lose the line -- and never let logging break request handling.
        try:
            headers = getattr(self, "headers", None)
            if headers is None:
                _access_log.info(f'-\t-\t"{line}"\t"-"\t"-"')
                return
            xff = (headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            ip = headers.get("CF-Connecting-IP") or xff or self.client_address[0]
            _access_log.info(
                f'{ip}\t{headers.get("CF-IPCountry", "-")}\t"{line}"'
                f'\t"{headers.get("User-Agent", "-")}"\t"{headers.get("Referer", "-")}"'
            )
        except Exception:                      # logging must never take the server down
            pass


if __name__ == "__main__":
    print("Newsroom running at http://127.0.0.1:8767")
    if not REFRESH_COMMAND:
        print("Refresh is disabled until NEWSROOM_REFRESH_COMMAND is set.")
    elif AUTO_REFRESH_HOURS > 0:
        print(f"Auto-refresh: every {AUTO_REFRESH_HOURS:g}h if the digest is older than that "
              f"(first check in {AUTO_REFRESH_GRACE_SECONDS / 60:g} min). "
              f"Set NEWSROOM_AUTO_REFRESH_HOURS=0 to disable.")
        # Daemon so it cannot keep a shutting-down server alive.
        threading.Thread(target=auto_refresh_loop, daemon=True).start()
    else:
        print("Auto-refresh disabled (NEWSROOM_AUTO_REFRESH_HOURS=0).")
    ThreadingHTTPServer(("127.0.0.1", 8767), NewsroomHandler).serve_forever()
