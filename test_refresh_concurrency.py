"""Concurrent refresh requests must be refused, not queued.

A pipeline run takes about an hour and writes one digest CSV. Two runs at once would
compete over that file and double the GPU load, so the second request has to fail rather
than wait. These tests drive a real socket because the thing being protected is the
endpoint -- anyone can POST to it, and the site's own button is only one of several
callers (the browser extension is another).

The failure has to be a *graceful* one: a 409 with a message a client can show, not a
hang, a 500, or a silent second run.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import server


@pytest.fixture
def base(tmp_path, monkeypatch):
    routing = {
        "active_profile": "local",
        "stages": {},
        "profiles": {"local": {"claude_stages": [], "projected_cost_usd": 0}},
    }
    (tmp_path / "routing.json").write_text(json.dumps(routing), encoding="utf-8")
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "REFRESH_COMMAND", "claude_pipeline_runner.py --routing routing.json")
    started = []
    # Stands in for a run that is still going: records the call and, like the real thing,
    # leaves the lock held until something releases it.
    monkeypatch.setattr(server, "run_refresh", lambda command: started.append(command))
    server.refresh_state.update({"running": False, "error": None, "profile": None})
    if server.refresh_lock.locked():
        server.refresh_lock.release()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.NewsroomHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", started
    httpd.shutdown()
    if server.refresh_lock.locked():
        server.refresh_lock.release()


def post(base_url, body, content_type="application/json"):
    req = urllib.request.Request(
        f"{base_url}/api/refresh", method="POST",
        data=json.dumps(body).encode(), headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def get_state(base_url):
    with urllib.request.urlopen(f"{base_url}/api/refresh", timeout=10) as resp:
        return json.loads(resp.read())


def test_second_request_is_refused_with_409(base):
    url, started = base
    first, _ = post(url, {"profile": "local"})
    assert first == 202

    status, body = post(url, {"profile": "local"})
    assert status == 409, body
    assert len(started) == 1, "a second run started while the first was in progress"


def test_the_refusal_carries_a_message_a_client_can_show(base):
    """A bare 409 with no body would leave a UI with nothing to display."""
    url, _ = base
    post(url, {"profile": "local"})
    _, body = post(url, {"profile": "local"})
    assert body.get("error"), "409 came back with no error message"
    assert "running" in body["error"].lower()


def test_refusal_does_not_disturb_the_running_state(base):
    """A rejected request must not clear `running` or overwrite the active profile."""
    url, _ = base
    post(url, {"profile": "local"})
    before = get_state(url)
    post(url, {"profile": "local"})
    after = get_state(url)
    assert after["running"] is True
    assert after == before, "a refused request mutated the run state"


def test_many_simultaneous_requests_start_exactly_one_run(base):
    """The real hazard: a user double-clicking, or several clients firing at once."""
    url, started = base
    codes = []
    lock = threading.Lock()

    def fire():
        status, _ = post(url, {"profile": "local"})
        with lock:
            codes.append(status)

    threads = [threading.Thread(target=fire) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(started) == 1, f"{len(started)} runs started from 8 concurrent requests"
    assert codes.count(202) == 1, f"expected one acceptance, got {codes}"
    assert codes.count(409) == 7, f"expected seven refusals, got {codes}"


def test_a_new_run_is_accepted_once_the_previous_one_finishes(base):
    """The lock must not wedge: refusing forever would be as broken as stacking."""
    url, started = base
    post(url, {"profile": "local"})
    assert post(url, {"profile": "local"})[0] == 409

    server.refresh_state["running"] = False
    server.refresh_lock.release()

    status, body = post(url, {"profile": "local"})
    assert status == 202, body
    assert len(started) == 2


def test_lock_is_released_when_the_pipeline_fails(base, monkeypatch):
    """Exercises the real run_refresh: a failing command must not hold the lock forever.

    Without the `finally` in run_refresh, one crashed run would refuse every later request
    until someone restarted the server by hand -- and this server is unsupervised.
    """
    url, _ = base
    monkeypatch.undo()  # restore the real run_refresh
    monkeypatch.setattr(server, "ROOT", server.ROOT)
    monkeypatch.setattr(server, "REFRESH_COMMAND", "exit 1")

    status, _ = post(url, {"profile": "local"})
    assert status == 202

    for _ in range(50):
        if not server.refresh_lock.locked():
            break
        time.sleep(0.1)
    assert not server.refresh_lock.locked(), "a failed run left the lock held"
    assert server.refresh_state["running"] is False
    assert server.refresh_state["error"], "a failed run recorded no error"


def test_text_plain_body_is_accepted(base):
    """The browser extension sends text/plain to avoid a CORS preflight.

    The server answers OPTIONS with 501, so a preflighted request would fail outright.
    text/plain is CORS-safelisted; read_json_body ignores content-type, which is what
    makes this work. If someone later makes the parser strict, this test fails loudly
    rather than the extension silently losing its refresh button.
    """
    url, started = base
    status, body = post(url, {"profile": "local"}, content_type="text/plain")
    assert status == 202, body
    assert started and "--profile local" in started[0]
