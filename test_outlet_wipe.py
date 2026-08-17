"""Guards against the settings panel silently wiping the outlet list.

The failure this file exists to prevent: activeOutlets/removedDefaults are posted to
/api/config as the COMPLETE new outlet set -- removed_outlets and added_outlets replace
the stored config rather than patching it. They start as [] and are only filled by a
successful GET /api/config. So if that read fails, or never ran, a Save posts {removed:
[], added: []}, which restores every removed default and deletes every custom outlet.

The server cannot catch this: that payload is byte-identical to a legitimate "reset to
defaults", and it passes validate_config. The guard has to be client-side, which is why
these tests read app.js as text rather than exercising the server.
"""
import pathlib
import re

APP = pathlib.Path(__file__).with_name("app.js").read_text(encoding="utf-8")


def _save_handler() -> str:
    """The body of the #settings-save click handler, to its real end.

    Sliced to the handler's closing `});` rather than a fixed character count -- a
    fixed window silently truncates as the handler grows, which turns a real guard
    into a passing-looking test that checks nothing.
    """
    i = APP.index("document.querySelector('#settings-save')")
    end = APP.index("\n});", i)
    return APP[i:end]


def _load_fn() -> str:
    i = APP.index("async function loadSettingsForm()")
    return APP[i:APP.index("\n}", i)]


def test_save_is_gated_on_a_successful_load():
    """Save must refuse outright when the panel never loaded."""
    body = _save_handler()
    guard = body.index("if (!settingsLoaded)")
    posted = body.index("added_outlets:")
    assert guard < posted, "the settingsLoaded gate must run BEFORE the payload is built"
    assert "return;" in body[guard:posted], "the gate must return, not merely warn"


def test_the_gate_tells_the_user_why():
    """A silent refusal would read as a broken button."""
    body = _save_handler()
    msg = body[body.index("if (!settingsLoaded)"):body.index("const body")]
    assert "settingsSaveError" in msg and "hidden = false" in msg
    assert "erase" in msg or "wipe" in msg, "the message must name the consequence"


def test_flag_starts_false():
    """The initial value is the dangerous one, so it must not be assumed loaded."""
    decl = re.search(r"^let settingsLoaded = (\w+);", APP, re.M)
    assert decl, "settingsLoaded must be declared at module scope"
    assert decl.group(1) == "false"


def test_load_clears_the_flag_before_it_can_throw():
    """A read that fails midway must not leave the panel marked loaded."""
    fn = _load_fn()
    cleared = fn.index("settingsLoaded = false")
    first_await = fn.index("await")
    assert cleared < first_await, "clear the flag before the first await, not after"


def test_load_sets_the_flag_only_at_the_very_end():
    """Anything that throws on the way must leave the flag false."""
    fn = _load_fn()
    assert fn.rstrip().endswith("settingsLoaded = true;"), \
        "the flag must be set last, after every step that can throw"


def test_a_200_with_no_outlet_array_is_rejected():
    """`|| []` on a malformed payload manufactures a convincing empty panel."""
    fn = _load_fn()
    assert "Array.isArray(data.active_outlets)" in fn
    assert "throw" in fn[fn.index("Array.isArray(data.active_outlets)"):]


def test_a_non_ok_response_throws():
    """A 500 body parsed as JSON would otherwise yield undefined, then []."""
    fn = _load_fn()
    assert "response.ok" in fn and "throw" in fn


def test_reopening_the_panel_awaits_and_catches():
    """The original bug: fire-and-forget meant a failed reload was invisible."""
    i = APP.index("document.querySelector('#settings-btn')")
    handler = APP[i:i + 700]
    assert "async" in handler[:handler.index("{")], "the handler must be async to await"
    assert "await loadSettingsForm()" in handler
    assert "catch" in handler, "a failed reload must be caught and surfaced"


def test_save_response_is_validated_like_the_load():
    """The POST reply refills the same globals, so it needs the same check."""
    body = _save_handler()
    after = body[body.index("await response.json()"):]
    assert "Array.isArray(data.active_outlets)" in after, \
        "the save response must be validated before it is adopted"
    assert "settingsLoaded = false" in after, \
        "a malformed save response must disarm the panel"
