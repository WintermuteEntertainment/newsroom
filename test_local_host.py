"""Tests for LocalHost: server-slot diagnostic, cold-model warmup gate, loss accounting.

Recovered and extended 2026-07-30. The slot checks previously existed only as a throwaway
script in /tmp, so "12 tests pass" was unverifiable an hour later -- committing them.
Run: python3 -m pytest -q test_local_host.py
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from local_host import LocalHost

def running_entry(model="m", parallel=4):
    """One llama-swap /running entry, shaped like the real thing.

    `parallel=None` omits the flag entirely, which is how v4flash actually runs.
    """
    flag = "" if parallel is None else f" --parallel {parallel}"
    return {"model": model, "state": "ready",
            "cmd": f"A:\\llama\\llama-server.exe --port 5800 --ctx-size 8192{flag}",
            "proxy": "http://localhost:5800"}


# `running=None` models the endpoint being absent -- the fake server then 404s every GET,
# which is exactly what llama-swap's own /props did in production and why the old
# implementation returned None on every real run.
STATE = {"running": [running_entry()], "chat_delay": 0.0, "chat_status": 200,
         "chat_content": "ok", "chat_calls": 0, "fail_first_n": 0}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Deliberately serves ONLY /running. /props 404s here just as it does through the
        # real swap proxy, so an implementation that reverted to asking /props would fail
        # these tests rather than quietly returning None again.
        if self.path == "/running" and STATE["running"] is not None:
            self._json({"running": STATE["running"]})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        STATE["chat_calls"] += 1
        if STATE["chat_delay"]:
            time.sleep(STATE["chat_delay"])
        if STATE["fail_first_n"] > 0:
            # Fail exactly N times then recover -- models the eviction case, where the
            # request dies mid-flight but the next attempt lands on a healthy model.
            # Independent of chat_status, which models a PERSISTENT fault.
            STATE["fail_first_n"] -= 1
            self.send_response(502)
            self.end_headers()
            return
        if STATE["chat_status"] != 200:
            self.send_response(STATE["chat_status"])
            self.end_headers()
            return
        self._json({"choices": [{"message": {"content": STATE["chat_content"]},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1}})

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def base():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


@pytest.fixture(autouse=True)
def reset():
    STATE.update(running=[running_entry()], chat_delay=0.0, chat_status=200,
                 chat_content="ok", chat_calls=0, fail_first_n=0)


# --- server-slot diagnostic -------------------------------------------------

def test_reads_parallel_from_launch_cmd(base):
    """Passing at all proves it reads /running: the fake server 404s /props."""
    assert LocalHost("m", base_url=base).server_slots() == 4


def test_absent_parallel_flag_means_llama_cpp_default_not_one(base):
    """v4flash sets no --parallel and genuinely has 4 slots.

    Reading an absent flag as 1 would invent a permanent warning about the one model that
    never had a concurrency ceiling.
    """
    STATE["running"] = [running_entry(parallel=None)]
    host = LocalHost("m", base_url=base, max_concurrency=4)
    assert host.server_slots() == 4
    assert host.concurrency_warning() is None


def test_slots_read_for_this_hosts_model_not_whatever_is_loaded(base):
    """A routed run alternates models; the loaded one is often not this host's.

    v4flash's 4 slots say nothing about the qwen batch this host is about to send, so
    reporting them would be worse than reporting nothing.
    """
    STATE["running"] = [running_entry(model="v4flash", parallel=4)]
    assert LocalHost("qwen-finetune-q3", base_url=base).server_slots() is None


def test_model_not_loaded_yet_is_unknown_not_a_guess(base):
    STATE["running"] = []
    host = LocalHost("m", base_url=base, max_concurrency=4)
    assert host.server_slots() is None and host.concurrency_warning() is None


def test_cap_equal_to_slots_is_silent(base):
    assert LocalHost("m", base_url=base, max_concurrency=4).concurrency_warning() is None


def test_under_using_slots_is_not_an_error(base):
    assert LocalHost("m", base_url=base, max_concurrency=2).concurrency_warning() is None


def test_cap_above_slots_warns_with_shortfall_and_remedy(base):
    w = LocalHost("m", base_url=base, max_concurrency=6).concurrency_warning()
    assert w and "2 request(s) will queue" in w
    assert "--parallel" in w and "--local-concurrency to 4" in w


def test_single_slot_case(base):
    """The real BBB bug: qwen ran --parallel 1 while the client sent 4.

    This is the warning that never fired for the whole project, because the old
    implementation asked an endpoint the swap proxy does not serve.
    """
    STATE["running"] = [running_entry(parallel=1)]
    w = LocalHost("m", base_url=base, max_concurrency=4).concurrency_warning()
    assert w and "3 request(s) will queue" in w


def test_missing_running_endpoint_degrades_silently(base):
    STATE["running"] = None
    h = LocalHost("m", base_url=base, max_concurrency=4)
    assert h.server_slots() is None
    assert h.concurrency_warning() is None


def test_malformed_running_body_does_not_raise(base):
    """A shape change upstream must degrade to "unknown", not abort a run."""
    STATE["running"] = "not-a-list"
    h = LocalHost("m", base_url=base, max_concurrency=4)
    assert h.server_slots() is None and h.concurrency_warning() is None


def test_entry_without_cmd_is_unknown(base):
    STATE["running"] = [{"model": "m", "state": "ready"}]
    assert LocalHost("m", base_url=base).server_slots() is None


def test_dead_endpoint_does_not_raise():
    h = LocalHost("m", base_url="http://127.0.0.1:1/v1", max_concurrency=4)
    assert h.server_slots() is None and h.concurrency_warning() is None


def test_default_concurrency_is_four(base):
    assert LocalHost("m", base_url=base).max_concurrency == 4


# --- measured timeout default ----------------------------------------------

def test_timeout_default_is_above_slowest_observed_success(base):
    """206.8s was the slowest call that ever returned content; 300s only bought silence."""
    assert LocalHost("m", base_url=base).timeout == 210.0


# --- cold-model warmup gate -------------------------------------------------

def test_warmup_probes_once_then_caches(base):
    h = LocalHost("m", base_url=base)
    first = h.ensure_warm("m")
    assert first["status"] == "warmed"
    assert STATE["chat_calls"] == 1
    second = h.ensure_warm("m")
    assert second["status"] == "already_warm"
    assert STATE["chat_calls"] == 1, "second call must not re-probe"


def test_failed_warmup_is_reported_not_raised():
    h = LocalHost("m", base_url="http://127.0.0.1:1/v1")
    out = h.ensure_warm("m", timeout=1.0)
    assert out["status"] == "failed" and "error" in out
    assert h.errors["warmup_failed"] == 1
    assert "m" not in h._warmed, "a failed probe must not mark the model warm"
    assert not h.lost_by_stage, "a probe failure is not pipeline work lost"


def test_llm_warms_before_dispatching_batch(base):
    h = LocalHost("m", base_url=base)
    h.llm([{"prompt": "a", "stage": "snippet_writing"},
           {"prompt": "b", "stage": "snippet_writing"}])
    # 1 warmup probe + 2 real calls
    assert STATE["chat_calls"] == 3


# --- silent-loss accounting -------------------------------------------------

def test_http_error_records_the_stage(base):
    h = LocalHost("m", base_url=base, retry_backoff=0.0)
    h.ensure_warm("m")
    STATE["chat_status"] = 502
    out = h.llm([{"prompt": "x", "stage": "fragment_merge"}])
    assert out[0]["text"] == ""
    assert h.lost_by_stage["fragment_merge"] == 1
    assert h.errors["http_502"] == 1


def test_empty_content_counts_as_loss(base):
    """A 200 carrying no content is the same silent loss as a timeout."""
    h = LocalHost("m", base_url=base, retry_backoff=0.0)
    h.ensure_warm("m")
    STATE["chat_content"] = ""
    out = h.llm([{"prompt": "x", "stage": "entailment_check"}])
    assert out[0]["text"] == ""
    assert h.lost_by_stage["entailment_check"] == 1
    assert h.errors["empty_content"] == 1


def test_stageless_call_is_attributed_to_other(base):
    h = LocalHost("m", base_url=base, retry_backoff=0.0)
    h.ensure_warm("m")
    STATE["chat_status"] = 502
    h.llm([{"prompt": "x"}])
    assert h.lost_by_stage["other"] == 1


def test_successful_call_records_no_loss(base):
    h = LocalHost("m", base_url=base)
    out = h.llm([{"prompt": "x", "stage": "snippet_writing"}])
    assert out[0]["text"] == "ok"
    assert not h.lost_by_stage
    assert not h.errors


def test_loss_appears_in_usage_summary(base):
    h = LocalHost("m", base_url=base, retry_backoff=0.0)
    h.ensure_warm("m")
    STATE["chat_status"] = 502
    h.llm([{"prompt": "x", "stage": "fragment_merge"}])
    summary = h.usage_summary() if hasattr(h, "usage_summary") else None
    if summary is None:
        pytest.skip("usage summary is built by the runner, not LocalHost")
    assert summary["lost_by_stage"]["fragment_merge"] == 1

# --- upstream retry (two distinct fault classes) -----------------------------

def test_transient_failure_is_retried_and_succeeds(base):
    """The eviction case: model dies mid-request, second attempt works."""
    h = LocalHost("m", base_url=base, retry_backoff=0.0)
    h.ensure_warm("m")
    STATE["chat_calls"] = 0
    STATE["fail_first_n"] = 1          # transient: fails once, then healthy
    out = h.llm([{"prompt": "x", "stage": "entailment_check"}])
    assert out[0]["text"] == "ok", "retry should have recovered the call"
    assert not h.lost_by_stage, "a recovered call is not a lost call"
    assert h.errors["retried_http_502"] == 1
    assert h.errors["http_502"] == 0


def test_persistent_failure_is_counted_once_after_retry(base):
    h = LocalHost("m", base_url=base, retry_backoff=0.0)
    h.ensure_warm("m")
    STATE["chat_status"] = 502
    out = h.llm([{"prompt": "x", "stage": "fragment_merge"}])
    assert out[0]["text"] == ""
    assert h.lost_by_stage["fragment_merge"] == 1, "counted once, not once per attempt"
    assert h.errors["http_502"] == 1
    assert h.errors["retried_http_502"] == 1


def test_retry_can_be_disabled(base):
    h = LocalHost("m", base_url=base, retry_upstream=False, retry_backoff=0.0)
    h.ensure_warm("m")
    STATE["fail_first_n"] = 1          # transient, but retry is off -> stays lost
    out = h.llm([{"prompt": "x", "stage": "fragment_merge"}])
    assert out[0]["text"] == ""
    assert h.lost_by_stage["fragment_merge"] == 1
    assert h.errors["retried_http_502"] == 0


def test_retry_clears_the_warm_flag_without_reprobing(base):
    """An eviction leaves the model cold, so the warm flag must be dropped -- but the retry
    must NOT probe, or the probe absorbs the recovery and the real request fails anyway."""
    h = LocalHost("m", base_url=base, retry_backoff=0.0)
    h.ensure_warm("m")
    assert "m" in h._warmed
    STATE["fail_first_n"] = 1          # transient
    calls_before = STATE["chat_calls"]
    out = h.llm([{"prompt": "x", "stage": "entailment_check"}])
    assert out[0]["text"] == "ok", "the retry itself must get the recovery, not a probe"
    assert "m" not in h._warmed, "next batch should re-warm serially"
    assert STATE["chat_calls"] - calls_before == 2, "exactly one retry, no extra probe"
