"""The metered-refresh password gate, exercised through the HTTP endpoint.

Moving the routing dropdown into the password-locked settings panel is UX only: anyone
who reads the page source can POST /api/refresh directly. The gate that actually stops a
stranger spending money is server-side, and these tests drive it over a real socket
rather than calling the handler's helpers, because the thing being protected is the
endpoint.

Free (all-local) refreshes stay open on purpose -- they cost nothing and the site's main
button should not need a password.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import server


@pytest.fixture
def base(tmp_path, monkeypatch):
    """A live server whose routing.json we control and whose runs never execute."""
    routing = {
        "active_profile": "local",
        "stages": {},
        "profiles": {
            "local": {"claude_stages": [], "projected_cost_usd": 0},
            "safe-verify": {"claude_stages": ["entailment_check"],
                            "projected_cost_usd": 0.0309},
        },
    }
    (tmp_path / "routing.json").write_text(json.dumps(routing), encoding="utf-8")
    monkeypatch.setattr(server, "ROOT", tmp_path)
    # Set a password for the test rather than reading server.CONFIG_PASSWORD: that is empty
    # unless one is configured, and a test that reads the live value would only pass while a
    # real password sits in source -- which is exactly what was removed.
    monkeypatch.setattr(server, "CONFIG_PASSWORD", "test-password")
    monkeypatch.setattr(server, "REFRESH_COMMAND", "claude_pipeline_runner.py --routing routing.json")
    # Never actually start the pipeline; record what would have run.
    started = []
    monkeypatch.setattr(server, "run_refresh", lambda command: started.append(command))
    server.refresh_state.update({"running": False, "error": None, "profile": None})
    if server.refresh_lock.locked():
        server.refresh_lock.release()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.NewsroomHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", started
    httpd.shutdown()


def post(base_url, body):
    req = urllib.request.Request(
        f"{base_url}/api/refresh", method="POST",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def release(base_url=None):
    """A started run holds the lock; drop it so the next request is not a 409."""
    server.refresh_state["running"] = False
    if server.refresh_lock.locked():
        server.refresh_lock.release()


def test_metered_profile_without_password_is_rejected(base):
    url, started = base
    status, body = post(url, {"profile": "safe-verify"})
    assert status == 401, body
    assert not started, "a metered run started without a password"


def test_metered_profile_with_wrong_password_is_rejected(base):
    url, started = base
    status, body = post(url, {"profile": "safe-verify", "password": "0000"})
    assert status == 401, body
    assert not started


def test_metered_profile_with_password_runs(base):
    url, started = base
    status, body = post(url, {"profile": "safe-verify", "password": "test-password"})
    assert status == 202, body
    assert started and "--profile safe-verify" in started[0]
    release()


def test_free_profile_needs_no_password(base):
    """The whole point of gating only metered profiles."""
    url, started = base
    status, body = post(url, {"profile": "local"})
    assert status == 202, body
    assert started and "--profile local" in started[0]
    release()


def test_empty_body_runs_the_free_default(base):
    url, started = base
    status, body = post(url, {})
    assert status == 202, body
    assert started
    release()


def test_metered_active_profile_gates_an_empty_body(base, tmp_path):
    """Omitting a profile does NOT mean free: the runner falls back to active_profile.

    A gate that inspected only the requested value would wave this through, and every
    unauthenticated click of the page's Refresh button would bill the API.
    """
    url, started = base
    routing = json.loads((tmp_path / "routing.json").read_text())
    routing["active_profile"] = "safe-verify"
    (tmp_path / "routing.json").write_text(json.dumps(routing), encoding="utf-8")
    status, body = post(url, {})
    assert status == 401, body
    assert not started, "an empty body started a metered run via active_profile"


def test_unknown_profile_is_rejected_before_the_password_check(base):
    """Order matters: an unknown name must not be reported as a password problem, and
    must never reach the shell."""
    url, started = base
    status, body = post(url, {"profile": "; rm -rf /"})
    assert status == 400, body
    assert "; rm -rf /" not in json.dumps(body), "echoed the injected value back"
    assert not started


def test_api_exposes_metered_flag_per_profile(base):
    url, _ = base
    with urllib.request.urlopen(f"{url}/api/digest", timeout=10) as resp:
        routing = json.loads(resp.read())["routing"]
    flags = {p["name"]: p["metered"] for p in routing["profiles"]}
    assert flags == {"local": False, "safe-verify": True}


def test_metered_is_derived_from_stages_not_cost(base, tmp_path):
    """A stale or missing projected_cost_usd must not make a billing profile look free."""
    routing = json.loads((tmp_path / "routing.json").read_text())
    routing["profiles"]["safe-verify"].pop("projected_cost_usd")
    (tmp_path / "routing.json").write_text(json.dumps(routing), encoding="utf-8")
    assert server.profile_is_metered("safe-verify") is True


def test_unknown_profile_is_treated_as_metered(base):
    """Fail closed: if we cannot tell what a name costs, require the password."""
    assert server.profile_is_metered("not-a-profile") is True


def test_no_routing_config_leaves_refresh_open(base, tmp_path):
    """Without routing.json there is no profile to pass, so the gate adds nothing and
    must not lock out a plain refresh."""
    (tmp_path / "routing.json").unlink()
    url, started = base
    status, body = post(url, {})
    assert status == 202, body
    release()
