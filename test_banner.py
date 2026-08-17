"""Freshness-banner logic tests.

The banner claimed "Refreshed just now" over two-hour-old content whenever a run was in
flight, because raw_headlines_<date>.json is REWRITTEN at fetch time with a new fetched_utc
and no completed_utc. Observed on BBB 2026-07-30 14:40 (fetched_utc 18:40 UTC,
completed_utc null, digest files from 12:46). These tests encode the states that produced
that bug, as a Python model of app.js's setRefreshed/renderRefreshed.

app.js is the implementation; test_banner_source_excludes_fetched_utc below fails if the two
drift apart, which is the only thing keeping this model honest.
"""
import datetime as dt
import pathlib
import re

APP_JS = pathlib.Path(__file__).with_name("app.js")
NOW = dt.datetime(2026, 7, 30, 18, 44, 52, tzinfo=dt.timezone.utc)


def ago_text(then, now=NOW):
    mins = int((now - then).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} {'hour' if hours == 1 else 'hours'} ago"
    days = hours // 24
    return f"{days} {'day' if days == 1 else 'days'} ago"


def _parse(stamp):
    if not stamp:
        return None
    try:
        return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def banner(data, now=NOW):
    """Mirror of setRefreshed + renderRefreshed. Returns (text, title)."""
    scan = data.get("scan") or {}
    refreshed_at = _parse(scan.get("completed_utc")) or _parse(data.get("updated"))
    fetched = _parse(scan.get("fetched_utc"))
    in_progress = bool(fetched and (refreshed_at is None or fetched > refreshed_at))
    if refreshed_at is None:
        return ("Refreshing now\u2026" if in_progress else "",
                "A refresh is running; no completed report exists yet." if in_progress else "")
    age = f"Refreshed {ago_text(refreshed_at, now)}"
    text = f"{age} \u00b7 refreshing now\u2026" if in_progress else age
    return text, f"Report last completed {refreshed_at.isoformat()}"


# The exact state observed on BBB while a run was in flight.
MID_RUN = {"scan": {"fetched_utc": "2026-07-30T18:40:18+00:00", "completed_utc": None},
           "updated": "2026-07-30T16:46:00+00:00"}


def test_mid_run_does_not_claim_just_now():
    text, _ = banner(MID_RUN)
    assert "just now" not in text, text


def test_mid_run_age_describes_the_old_content():
    """16:46 -> 18:44 UTC is 1h58m, which floors to "1 hour ago" -- the point is that the age
    tracks the OLD digest, not the just-started fetch (which would read "just now")."""
    text, _ = banner(MID_RUN)
    assert "1 hour ago" in text, text
    assert "min ago" not in text, text


def test_mid_run_announces_the_run_underway():
    text, _ = banner(MID_RUN)
    assert "refreshing now" in text, text


def test_completed_run_has_no_progress_marker():
    text, _ = banner({"scan": {"fetched_utc": "2026-07-30T18:40:18+00:00",
                               "completed_utc": "2026-07-30T18:44:00+00:00"},
                      "updated": "2026-07-30T18:44:00+00:00"})
    assert "refreshing" not in text and "Refreshed" in text, text


def test_missing_scan_falls_back_to_mtime_without_claiming_progress():
    text, _ = banner({"scan": {}, "updated": "2026-07-30T16:46:00+00:00"})
    assert "ago" in text and "refreshing" not in text, text


def test_first_ever_run_shows_progress_and_no_bogus_age():
    text, title = banner({"scan": {"fetched_utc": "2026-07-30T18:40:18+00:00"}, "updated": None})
    assert text == "Refreshing now\u2026" and "ago" not in text, text
    assert "no completed report" in title, title


def test_invalid_stamp_does_not_leak_nan():
    text, title = banner({"scan": {"completed_utc": "not-a-date"}, "updated": None})
    assert text == "" and "NaN" not in text + title, (text, title)


def test_clock_skew_future_stamp_reads_just_now():
    text, _ = banner({"scan": {"completed_utc": "2026-07-30T19:00:00+00:00"}, "updated": None})
    assert "just now" in text, text


def test_banner_source_excludes_fetched_utc():
    """The bug was fetched_utc in the stamp chain; keep it out of app.js itself."""
    src = APP_JS.read_text(encoding="utf-8")
    i = src.index("function setRefreshed(data)")
    chain = src[i:i + 1400]
    assert "scan.completed_utc || data.updated" in chain
    assert "scan.completed_utc || scan.fetched_utc" not in src


# ---------------------------------------------------------------------------
# The server's own running flag outranks the timestamp proxy.
#
# Observed 2026-08-04: a run started 08:55 UTC and was killed ~34 minutes in (a server
# restart). raw_headlines kept fetched_utc with completed_utc null, so the stamp proxy said
# "refreshing now" for the next two hours while /api/refresh reported running: false. The
# proxy cannot tell "in flight" from "died mid-run"; the process holding the refresh lock
# can, so /api/digest now reports refreshRunning and both front ends prefer it.

DIED_MID_RUN = {"scan": {"fetched_utc": "2026-08-04T08:55:16+00:00", "completed_utc": None},
                "updated": "2026-08-04T04:14:18+00:00",
                "refreshRunning": False}


def _run_in_progress(data):
    """Python model of setRefreshed's runInProgress decision in app.js."""
    scan = data.get("scan") or {}
    if isinstance(data.get("refreshRunning"), bool):
        return data["refreshRunning"]
    refreshed = _parse(scan.get("completed_utc")) or _parse(data.get("updated"))
    fetched = _parse(scan.get("fetched_utc"))
    return bool(fetched and (not refreshed or fetched > refreshed))


def test_a_dead_run_does_not_claim_to_be_refreshing():
    """The whole point: stamps say in-flight, the server says no, and the server wins."""
    assert _run_in_progress(DIED_MID_RUN) is False
    # ...and the proxy alone would have got it wrong, which is why the flag exists.
    proxy_only = {k: v for k, v in DIED_MID_RUN.items() if k != "refreshRunning"}
    assert _run_in_progress(proxy_only) is True


def test_a_genuinely_live_run_still_says_refreshing():
    assert _run_in_progress({**DIED_MID_RUN, "refreshRunning": True}) is True


def test_an_older_server_without_the_flag_falls_back_to_stamps():
    """Omitting the field must not report every run as idle -- degrade to the old proxy."""
    assert _run_in_progress(MID_RUN) is True
    assert _run_in_progress({"scan": {"fetched_utc": "2026-07-30T18:40:18+00:00",
                                      "completed_utc": "2026-07-30T18:44:00+00:00"},
                             "updated": "2026-07-30T18:44:00+00:00"}) is False


def test_both_front_ends_prefer_the_server_flag():
    """app.js and extension/digest.js are separate copies; the extension missed the last fix."""
    for name in ("app.js", "extension/digest.js"):
        src = (pathlib.Path(__file__).parent / name).read_text(encoding="utf-8")
        assert "refreshRunning" in src, f"{name} ignores the server's running flag"
        assert "typeof data.refreshRunning === 'boolean'" in src, \
            f"{name} must fall back to the stamp proxy when the field is absent"


def test_the_digest_endpoint_actually_sends_the_flag():
    """A front end preferring a field the server never sends is worse than the proxy."""
    src = (pathlib.Path(__file__).parent / "server.py").read_text(encoding="utf-8")
    assert '"refreshRunning": bool(refresh_state.get("running"))' in src
