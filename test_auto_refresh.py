"""Tests for the built-in scheduled refresh.

Added 2026-08-05. There was no scheduled refresh at all before this: the digest only
updated when somebody pressed the button, so the site could sit on yesterday's stories
indefinitely.

The behaviour these pin down, in order of how badly getting it wrong would hurt:

  1. The schedule must never spend money on its own. A metered profile behind an
     unattended timer would bill every couple of hours with nobody watching.
  2. It must decide from the digest file on disk, not from a timer this process has been
     counting -- nothing supervises this server, so it WILL be restarted, and an in-memory
     interval would reset to zero each time and could postpone a refresh forever.
  3. It must never start a second run on top of a live one. There is one GPU.

Run: python3 -m pytest -q test_auto_refresh.py
"""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


HOUR = 3600.0


# --- the rule itself, with no clock or filesystem involved -------------------

def test_fresh_digest_is_not_due():
    assert server.auto_refresh_due(30 * 60, 2) is False


def test_digest_exactly_at_the_interval_is_due():
    """Boundary is inclusive: at 2h00m00s the digest is 2 hours old, which is the point."""
    assert server.auto_refresh_due(2 * HOUR, 2) is True


def test_stale_digest_is_due():
    assert server.auto_refresh_due(6 * HOUR, 2) is True


def test_no_digest_at_all_is_due():
    """None means the site has nothing to show -- the most due state there is, not an
    unknown to skip. A fresh install would otherwise never refresh itself."""
    assert server.auto_refresh_due(None, 2) is True


def test_zero_interval_disables_the_schedule_even_with_no_digest():
    assert server.auto_refresh_due(None, 0) is False
    assert server.auto_refresh_due(99 * HOUR, 0) is False


# --- age comes off the disk, which is what survives a restart ----------------

def test_age_is_read_from_the_digest_file_not_remembered(tmp_path, monkeypatch):
    """The whole reason this survives the server being down for hours.

    A timer held in memory would restart at zero here and report a brand-new digest.
    """
    csv_path = tmp_path / "news_digest_2026-08-05.csv"
    csv_path.write_text("headline\n", encoding="utf-8")
    three_hours_ago = time.time() - 3 * HOUR
    import os
    os.utime(csv_path, (three_hours_ago, three_hours_ago))
    monkeypatch.setattr(server, "newest_digest", lambda: csv_path)

    age = server.digest_age_seconds()
    assert age is not None
    assert 3 * HOUR - 60 < age < 3 * HOUR + 60
    assert server.auto_refresh_due(age, 2) is True


def test_no_digest_file_reports_unknown_age(monkeypatch):
    monkeypatch.setattr(server, "newest_digest", lambda: None)
    assert server.digest_age_seconds() is None


def test_unreadable_digest_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "newest_digest", lambda: tmp_path / "gone.csv")
    assert server.digest_age_seconds() is None


# --- the tick, which is where money and the GPU are on the line --------------

@pytest.fixture
def fake_run(monkeypatch):
    """Replace the pipeline with something that records and releases the lock."""
    started = []

    def _fake(command=None):
        started.append(command)
        server.refresh_state["running"] = False
        server.refresh_lock.release()

    monkeypatch.setattr(server, "run_refresh", _fake)
    return started


@pytest.fixture(autouse=True)
def clean_state():
    yield
    if server.refresh_lock.locked():
        server.refresh_lock.release()
    server.refresh_state.update(running=False, error=None, profile=None,
                                contract={}, log=None, trigger=None)


def test_metered_profile_is_never_started_by_the_schedule(monkeypatch, fake_run):
    """The one that would cost real money unattended.

    The manual path requires a password before a metered run. A timer cannot supply one,
    so the only honest options are "skip" or "bill the user without being asked" -- and
    running it would silently convert a $0.00/run setup into a recurring charge.
    """
    monkeypatch.setattr(server, "REFRESH_COMMAND", "echo run")
    monkeypatch.setattr(server, "digest_age_seconds", lambda: 6 * HOUR)
    monkeypatch.setattr(server, "effective_profile", lambda _requested: "claude")
    monkeypatch.setattr(server, "profile_is_metered", lambda _name: True)

    assert server.auto_refresh_tick() == "skipped_metered"
    assert fake_run == []
    assert server.refresh_state["running"] is False


def test_due_and_free_starts_a_run_marked_scheduled(monkeypatch, fake_run):
    monkeypatch.setattr(server, "REFRESH_COMMAND", "echo run")
    monkeypatch.setattr(server, "digest_age_seconds", lambda: 6 * HOUR)
    monkeypatch.setattr(server, "profile_is_metered", lambda _name: False)

    assert server.auto_refresh_tick() == "started"
    assert fake_run == ["echo run"]
    # Provenance: a run appearing while nobody is at the keyboard must be identifiable as
    # the schedule working rather than as a fault.
    assert server.refresh_state["trigger"] == "scheduled"


def test_fresh_digest_starts_nothing(monkeypatch, fake_run):
    monkeypatch.setattr(server, "REFRESH_COMMAND", "echo run")
    monkeypatch.setattr(server, "digest_age_seconds", lambda: 10 * 60)
    monkeypatch.setattr(server, "profile_is_metered", lambda _name: False)

    assert server.auto_refresh_tick() == "not_due"
    assert fake_run == []


def test_schedule_never_stacks_on_a_live_run(monkeypatch, fake_run):
    """There is one GPU. A second run on top of a live one would fight it for the model."""
    monkeypatch.setattr(server, "REFRESH_COMMAND", "echo run")
    monkeypatch.setattr(server, "digest_age_seconds", lambda: 6 * HOUR)
    monkeypatch.setattr(server, "profile_is_metered", lambda _name: False)
    server.refresh_lock.acquire()          # stand in for a run already in flight

    assert server.auto_refresh_tick() == "already_running"
    assert fake_run == []


def test_unconfigured_refresh_command_is_a_no_op(monkeypatch, fake_run):
    monkeypatch.setattr(server, "REFRESH_COMMAND", "")
    monkeypatch.setattr(server, "digest_age_seconds", lambda: 99 * HOUR)

    assert server.auto_refresh_tick() == "not_configured"
    assert fake_run == []


# --- start_refresh is the single door both callers go through ----------------

def test_start_refresh_reports_false_without_clobbering_a_live_run(fake_run):
    server.refresh_lock.acquire()
    server.refresh_state.update(running=True, trigger="manual", log="in_flight.log")

    assert server.start_refresh("echo other", trigger="scheduled") is False
    # The live run's state must be untouched -- a failed start that reset `log` would
    # detach the running pipeline from its own log file in the UI.
    assert server.refresh_state["trigger"] == "manual"
    assert server.refresh_state["log"] == "in_flight.log"


def test_start_refresh_defaults_to_manual(fake_run):
    assert server.start_refresh("echo run") is True
    assert server.refresh_state["trigger"] == "manual"


def test_start_refresh_clears_the_previous_runs_report(fake_run):
    server.refresh_state.update(contract={"total": {"parsed": 1, "calls": 1}},
                                log="old.log", error="previous failure")

    assert server.start_refresh("echo run", trigger="scheduled") is True
    assert server.refresh_state["contract"] == {}
    assert server.refresh_state["log"] is None
    assert server.refresh_state["error"] is None
