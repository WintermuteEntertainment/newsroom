"""Tests for the auto-refresh interval being configurable from the settings panel.

Added 2026-09-22. Before this the interval was read from the environment once at import
and the schedule could only be turned off by restarting the server with a different
variable set. There was no way to change it, or switch it off, from the page.

The behaviour these pin down, in order of how badly getting it wrong would hurt:

  1. A saved 0 must stay 0. It is the off switch. If anything along the path treats it as
     "unset" and falls back to the default, the schedule turns itself back on -- the worst
     possible failure here, because it spends the GPU on a schedule the user switched off
     and the page would still show it as off.
  2. The interval must be read at each check, not captured at import. Captured once, a
     change saved from the panel would not take effect until a restart, which is exactly
     the limitation this work removes.
  3. An unset value must fall back to the environment default, so existing installs that
     have never opened the panel keep the behaviour they already had.

Run: python3 -m pytest -q test_auto_refresh_config.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_digest
import server


HOUR = 3600.0


@pytest.fixture
def saved_config(monkeypatch):
    """Drive load_config() from a dict instead of the real file on disk."""
    store = {}

    def _set(**values):
        store.clear()
        store.update(values)

    monkeypatch.setattr(news_digest, "load_config", lambda: dict(store))
    return _set


# --- 0 is the off switch, and must survive the whole round trip -------------

def test_zero_in_the_config_turns_the_schedule_off(saved_config, monkeypatch):
    """The failure that would cost real GPU time against the user's wishes."""
    monkeypatch.setattr(server, "AUTO_REFRESH_HOURS", 2.0)
    saved_config(auto_refresh_hours=0)

    assert server.effective_auto_refresh_hours() == 0
    # and the rule built on it agrees, even with no digest at all -- the most "due" state
    assert server.auto_refresh_due(None, server.effective_auto_refresh_hours()) is False


def test_zero_is_not_confused_with_unset(saved_config, monkeypatch):
    """`cfg.get(key) or default` would collapse these two into the default.

    Unset means "nobody has chosen, use the environment default". Zero means "switched
    off on purpose". A falsy check cannot tell them apart, and the cost of getting it
    wrong is a schedule that silently reactivates itself.
    """
    monkeypatch.setattr(server, "AUTO_REFRESH_HOURS", 2.0)

    saved_config()                                  # nothing saved
    assert server.effective_auto_refresh_hours() == 2.0

    saved_config(auto_refresh_hours=0)              # saved as off
    assert server.effective_auto_refresh_hours() == 0


def test_a_tick_with_the_schedule_off_starts_nothing(saved_config, monkeypatch):
    monkeypatch.setattr(server, "REFRESH_COMMAND", "echo run")
    monkeypatch.setattr(server, "digest_age_seconds", lambda: 99 * HOUR)
    monkeypatch.setattr(server, "profile_is_metered", lambda _name: False)
    saved_config(auto_refresh_hours=0)

    assert server.auto_refresh_tick() == "not_due"


# --- read per check, which is what removes the restart ----------------------

def test_the_interval_is_read_at_each_check(saved_config, monkeypatch):
    """Captured at import, a change saved from the panel would need a restart to matter."""
    monkeypatch.setattr(server, "REFRESH_COMMAND", "echo run")
    monkeypatch.setattr(server, "digest_age_seconds", lambda: 3 * HOUR)
    monkeypatch.setattr(server, "profile_is_metered", lambda _name: False)

    saved_config(auto_refresh_hours=6)              # 3h old, 6h interval -> not yet
    assert server.auto_refresh_tick() == "not_due"

    saved_config(auto_refresh_hours=2)              # same digest, shorter interval -> due
    assert server.effective_auto_refresh_hours() == 2


def test_unset_falls_back_to_the_environment_default(saved_config, monkeypatch):
    """Installs that never open the settings panel keep the behaviour they already had."""
    monkeypatch.setattr(server, "AUTO_REFRESH_HOURS", 4.0)
    saved_config()

    assert server.effective_auto_refresh_hours() == 4.0


# --- a bad value must not take the schedule down ----------------------------

def test_a_corrupt_value_falls_back_instead_of_crashing(saved_config, monkeypatch):
    """The loop must survive a hand-edited config.

    Disabling the schedule on a bad value would be the quiet failure: the site would go
    stale and nothing would say why. Falling back keeps it refreshing while the bad value
    is noticed.
    """
    monkeypatch.setattr(server, "AUTO_REFRESH_HOURS", 2.0)

    saved_config(auto_refresh_hours="every other tuesday")
    assert server.effective_auto_refresh_hours() == 2.0

    saved_config(auto_refresh_hours=None)
    assert server.effective_auto_refresh_hours() == 2.0


def test_a_negative_value_is_clamped_to_off_not_to_always(saved_config, monkeypatch):
    """A negative interval would make `age >= interval * 3600` true forever, starting a
    run on every check. Clamping to 0 turns it off instead, which is the safe reading."""
    monkeypatch.setattr(server, "AUTO_REFRESH_HOURS", 2.0)
    saved_config(auto_refresh_hours=-5)

    assert server.effective_auto_refresh_hours() == 0
    assert server.auto_refresh_due(None, server.effective_auto_refresh_hours()) is False


# --- validation, so the panel cannot save something the loop must defend against ---

def test_validation_accepts_zero_but_rejects_out_of_range():
    base = {"removed_outlets": [], "added_outlets": []}

    assert news_digest.validate_config({**base, "auto_refresh_hours": 0}) is None
    assert news_digest.validate_config({**base, "auto_refresh_hours": 0.5}) is None
    assert news_digest.validate_config({**base, "auto_refresh_hours": 168}) is None

    assert news_digest.validate_config({**base, "auto_refresh_hours": -1}) is not None
    assert news_digest.validate_config({**base, "auto_refresh_hours": 169}) is not None
    assert news_digest.validate_config({**base, "auto_refresh_hours": "soon"}) is not None


def test_validation_still_lets_the_key_be_absent():
    """Absent is legal and means "use the default" -- saving other settings must not
    require sending an interval."""
    assert news_digest.validate_config({"removed_outlets": [], "added_outlets": []}) is None
