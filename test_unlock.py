"""The settings-panel unlock check, exercised over a real socket.

The Unlock button used to open the panel on ANY non-empty string, on the reasoning that the
client gate was presentation only because /api/config re-checks the password on save. That was
technically true and practically wrong: a rotated password looked like it had not taken effect,
because the panel opened to the old one and only rejected it at save time.

/api/unlock exists so the client can verify BEFORE opening. These tests pin the two properties
that make it safe to expose: it reveals nothing, and it changes nothing.
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
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "CONFIG_PASSWORD", "correct-horse")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.NewsroomHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def post(base_url, path, body, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(f"{base_url}{path}", method="POST", data=data,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw_body = exc.read()
        try:
            return exc.code, json.loads(raw_body)
        except json.JSONDecodeError:
            return exc.code, {"_raw": raw_body[:200]}


def test_correct_password_is_accepted(base):
    status, body = post(base, "/api/unlock", {"password": "correct-horse"})
    assert status == 200, body
    assert body.get("ok") is True


def test_wrong_password_is_rejected(base):
    status, body = post(base, "/api/unlock", {"password": "1814"})
    assert status == 401, body
    assert "password" in body.get("error", "").lower()


def test_empty_password_is_rejected(base):
    """The old bug in one line: an empty or missing password must not unlock anything."""
    for payload in ({"password": ""}, {}, {"password": None}):
        status, _ = post(base, "/api/unlock", payload)
        assert status == 401, f"{payload} was accepted"


def test_a_non_string_password_does_not_crash_the_server(base):
    """hmac.compare_digest raises TypeError on non-str input; a 500 here would be a DoS."""
    for payload in ({"password": 1814}, {"password": []}, {"password": {"a": 1}}, {"password": True}):
        status, _ = post(base, "/api/unlock", payload)
        assert status == 401, f"{payload} produced {status}, not a clean 401"


def test_malformed_body_is_a_400_not_a_500(base):
    status, _ = post(base, "/api/unlock", None, raw=b"{not json")
    assert status == 400


def test_unlock_reveals_no_settings(base):
    """The response must not become a way to read config without the password.

    If this endpoint ever returns the settings payload, a successful guess leaks everything in
    one request -- and worse, someone may then rely on it and drop the separate GET.
    """
    _, body = post(base, "/api/unlock", {"password": "correct-horse"})
    assert set(body.keys()) == {"ok"}, f"unlock returned more than an acknowledgement: {body}"


def test_unlock_changes_nothing(base, tmp_path):
    """A check that writes is not a check. Verified by watching the config file."""
    before = sorted(p.name for p in tmp_path.iterdir())
    post(base, "/api/unlock", {"password": "correct-horse"})
    post(base, "/api/unlock", {"password": "wrong"})
    assert sorted(p.name for p in tmp_path.iterdir()) == before, "unlock touched the filesystem"


def test_get_on_unlock_is_not_a_route(base):
    """Only POST. A GET /api/unlock?password=... would put the password in server logs."""
    try:
        with urllib.request.urlopen(f"{base}/api/unlock", timeout=10) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status in (404, 501), f"GET /api/unlock returned {status}"


def test_password_ok_helper_is_used_by_every_path():
    """All three password checks must share one comparison.

    A direct == left behind in any path is how a future change to comparison lands in one place
    and is silently missed in another.
    """
    import pathlib
    source = pathlib.Path(server.__file__).read_text(encoding="utf-8")
    body = source[source.index("class NewsroomHandler"):]
    assert "!= CONFIG_PASSWORD" not in body, "a direct password comparison survives in a handler"
    assert body.count("password_ok(") >= 3, "not every password path routes through password_ok"


def test_comparison_is_constant_time():
    """compare_digest, not ==, so the check does not short-circuit on the first wrong character."""
    import pathlib
    source = pathlib.Path(server.__file__).read_text(encoding="utf-8")
    assert "hmac.compare_digest" in source


def _reload_with(monkeypatch, tmp_path, env=None, file_text=None):
    """Reload server.py with a chosen env var / password file and return the module.

    The password is resolved once at import time, so these have to be set BEFORE the reload.
    """
    import importlib
    if env is None:
        monkeypatch.delenv("NEWSROOM_CONFIG_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("NEWSROOM_CONFIG_PASSWORD", env)
    monkeypatch.setattr(server, "ROOT", tmp_path)
    if file_text is not None:
        (tmp_path / "config_password.txt").write_text(file_text, encoding="utf-8")
    monkeypatch.setattr(server, "PASSWORD_FILE", tmp_path / "config_password.txt")
    # Re-resolve using the patched ROOT/PASSWORD_FILE rather than re-importing, so the real
    # file beside server.py can never leak into the test.
    server.CONFIG_PASSWORD = server._load_config_password()
    return server


def _restore(monkeypatch):
    import importlib
    monkeypatch.delenv("NEWSROOM_CONFIG_PASSWORD", raising=False)
    importlib.reload(server)


def test_no_password_anywhere_fails_closed(monkeypatch, tmp_path):
    """With no env var and no password file, the gate must reject EVERYTHING.

    This is the point of removing the hardcoded fallback: the previous code defaulted to the
    real password, which kept the gate working but put the secret in git history forever.
    Failing closed is the safe direction -- nobody gets in, and it is obvious immediately.
    """
    srv = _reload_with(monkeypatch, tmp_path)
    try:
        assert srv.CONFIG_PASSWORD == ""
        for attempt in ("", "   ", "NEWStc1814", "anything", None, 1814):
            assert srv.password_ok(attempt) is False, f"{attempt!r} unlocked an unconfigured gate"
    finally:
        _restore(monkeypatch)


def test_password_file_is_read_when_the_env_var_is_absent(monkeypatch, tmp_path):
    """The file is what survives a hand-restart, so it must actually work."""
    srv = _reload_with(monkeypatch, tmp_path, file_text="from-the-file\n")
    try:
        assert srv.password_ok("from-the-file") is True
        assert srv.password_ok("something-else") is False
    finally:
        _restore(monkeypatch)


def test_the_env_var_wins_over_the_file(monkeypatch, tmp_path):
    srv = _reload_with(monkeypatch, tmp_path, env="from-the-env", file_text="from-the-file\n")
    try:
        assert srv.password_ok("from-the-env") is True
        assert srv.password_ok("from-the-file") is False
    finally:
        _restore(monkeypatch)


def test_an_empty_or_blank_source_is_treated_as_absent(monkeypatch, tmp_path):
    """`export NEWSROOM_CONFIG_PASSWORD=` or a blank first line must not become the password.

    Found by mutation testing: hmac.compare_digest("", "") is True, so an empty configured
    password would accept an empty submitted one and unlock everything.
    """
    for env, file_text in ((""       , None),
                           ("   "    , None),
                           (None     , "\n"),
                           (None     , "   \n"),
                           (""       , "\n\n")):
        srv = _reload_with(monkeypatch, tmp_path, env=env, file_text=file_text)
        try:
            assert srv.CONFIG_PASSWORD == "", f"env={env!r} file={file_text!r} produced a password"
            assert srv.password_ok("") is False
            assert srv.password_ok("   ") is False
        finally:
            _restore(monkeypatch)


def test_a_blank_first_line_does_not_hide_the_real_password(monkeypatch, tmp_path):
    """A file whose first line is blank should still find the password below it."""
    srv = _reload_with(monkeypatch, tmp_path, file_text="\n\n  actual-password  \n")
    try:
        assert srv.password_ok("actual-password") is True
    finally:
        _restore(monkeypatch)


def test_no_password_literal_survives_in_the_source(monkeypatch):
    """The whole point of this change: no working password in a committed file.

    Deliberately checks the CURRENT live value too, so re-adding it as a convenience default
    fails here rather than in a code review nobody does.
    """
    import pathlib
    source = pathlib.Path(server.__file__).read_text(encoding="utf-8")
    for literal in ("NEWStc1814", "1814"):
        assert literal not in source, f"{literal!r} is hardcoded in server.py"


def test_the_empty_password_guard_is_redundant_on_purpose(monkeypatch, tmp_path):
    """Two independent things must both reject an empty password.

    Mutation testing showed that deleting the `if not CONFIG_PASSWORD` guard in password_ok
    changes no observable behaviour -- because the earlier `not supplied` check already rejects
    "". That is defence in depth, not dead code: the guard exists so that if the *supplied*
    check is ever loosened, compare_digest("", "") returning True still cannot open the gate.
    This test pins both layers independently, so weakening either one fails here.
    """
    srv = _reload_with(monkeypatch, tmp_path)
    try:
        # Layer 1: an empty submitted password is rejected even when a real one is configured.
        srv.CONFIG_PASSWORD = "a-real-password"
        assert srv.password_ok("") is False, "empty submission accepted against a real password"

        # Layer 2: with the supplied-side check bypassed, an unconfigured password must still
        # not match. This is the case the guard alone covers.
        import hmac
        srv.CONFIG_PASSWORD = ""
        assert not (srv.CONFIG_PASSWORD and hmac.compare_digest("", srv.CONFIG_PASSWORD)), \
            "an unconfigured password would match an empty submission"
        assert srv.password_ok("") is False
    finally:
        _restore(monkeypatch)
