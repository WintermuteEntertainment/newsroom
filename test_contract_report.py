"""Parse-rate reporting survives a run, and an absent report is never read as success.

The pipeline prints an output-contract report -- per-stage "N/M parsed" plus samples of the
replies that did not parse. Before this, the server ran the pipeline with capture_output=True
and read only .stderr, only on a non-zero exit, so every refresh triggered from the web UI
threw that report away. Parse rates survived only in hand-run terminal logs, which is why a
run that "worked" could not be distinguished from one where a stage silently fell back.

These tests pin the two properties that made the loss dangerous rather than merely annoying:
the report is recovered from the log even when the run TIMES OUT (the case where the old
buffered output died with the process), and a missing report parses to a falsy value instead
of an empty-but-successful-looking one.
"""
import server


REAL_REPORT = """\
Scanning 5 outlets...
Output contract: 220/224 responses parsed (4 FAILED)
  fragment_merge: 4/7 parsed (57.1%)  <-- silent fallback fired
  coherence_audit: 133/134 parsed (99.2%)  <-- silent fallback fired
  snippet_writing: 20/20 parsed (100.0%)
  entailment_check: 23/23 parsed (100.0%)
  exclusives_dedup: 40/40 parsed (100.0%)
    [fragment_merge] unparsed: 'intuitionDIFFERENT'
    [fragment_merge] unparsed: 'ghiSAME'
"""

CLEAN_REPORT = """\
Output contract: 381/381 responses parsed (clean)
  coherence_audit: 132/132 parsed (100.0%)
  fragment_merge: 7/7 parsed (100.0%)
"""


def test_stage_rates_are_recovered():
    got = server.parse_contract_report(REAL_REPORT)
    assert got["stages"]["fragment_merge"] == {"parsed": 4, "calls": 7, "rate": 57.1}
    assert got["total"] == {"parsed": 220, "calls": 224}


def test_trailing_fallback_marker_does_not_break_the_match():
    """The runner appends '<-- silent fallback fired' to any stage under 100%.

    An end-anchored regex would reject exactly the lines worth reading.
    """
    got = server.parse_contract_report(REAL_REPORT)["stages"]
    assert got["fragment_merge"]["rate"] == 57.1
    assert got["coherence_audit"]["rate"] == 99.2


def test_failing_stages_are_identifiable():
    stages = server.parse_contract_report(REAL_REPORT)["stages"]
    failing = sorted(k for k, v in stages.items() if v["parsed"] < v["calls"])
    assert failing == ["coherence_audit", "fragment_merge"]


def test_unparsed_samples_are_kept():
    """The rate says a stage failed; the samples say why. The 2026-07-30 fragment_merge
    failures were run-together verdicts ('ghiSAME'), undiagnosable from 57% alone."""
    assert server.parse_contract_report(REAL_REPORT)["samples"]["fragment_merge"] == [
        "'intuitionDIFFERENT'", "'ghiSAME'"]


def test_no_report_is_falsy_not_empty_success():
    """A run that dies before printing its report must not look like a clean run.

    This is the assertion that matters: {} is falsy, so a caller writing
    `if contract:` cannot mistake "no data" for "no failures".
    """
    got = server.parse_contract_report("Traceback (most recent call last):\nRuntimeError")
    assert got == {}
    assert not got


def test_clean_run_reports_totals():
    got = server.parse_contract_report(CLEAN_REPORT)
    assert got["total"] == {"parsed": 381, "calls": 381}
    assert got["samples"] == {}


def test_later_report_supersedes_earlier():
    """The rewrite loop re-runs stages, so a log can hold several reports.
    The last one printed is the run's real tally."""
    got = server.parse_contract_report(CLEAN_REPORT + REAL_REPORT)
    assert got["stages"]["fragment_merge"]["rate"] == 57.1
    assert got["total"] == {"parsed": 220, "calls": 224}


def test_refresh_state_exposes_contract_and_log_keys():
    """/api/refresh returns dict(refresh_state); a key that only appears after the first
    run forces every client into presence checks."""
    assert "contract" in server.refresh_state
    assert "log" in server.refresh_state


def test_report_survives_a_timeout(tmp_path, monkeypatch):
    """The regression that motivated streaming to a file.

    With capture_output=True, TimeoutExpired means no CompletedProcess ever exists and the
    buffered output dies with the killed process -- losing the report in the one case where
    a partial report is most valuable. Streaming to a file leaves everything written up to
    the kill on disk. This drives the real run_refresh with a command that prints a report
    and then hangs past the timeout.
    """
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "REFRESH_TIMEOUT", 3)
    server.refresh_state.update({"contract": {}, "error": None, "log": None})
    server.refresh_lock.acquire()

    server.run_refresh(
        "printf 'Output contract: 5/7 responses parsed (2 FAILED)\\n"
        "  fragment_merge: 5/7 parsed (71.4%%)\\n'; sleep 30")

    assert "timed out" in server.refresh_state["error"]
    got = server.refresh_state["contract"]
    assert got, "the report must survive the kill"
    assert got["stages"]["fragment_merge"] == {"parsed": 5, "calls": 7, "rate": 71.4}
    assert server.refresh_state["log"], "the log filename must be reported"
    assert (tmp_path / server.refresh_state["log"]).exists()


def test_log_is_tailable_while_the_run_is_in_flight(tmp_path, monkeypatch):
    """A buffered pipe is unreadable until exit; a 50-minute run needs to be observable.
    Output written early must be on disk before the process finishes."""
    import threading, time
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "REFRESH_TIMEOUT", 30)
    server.refresh_state.update({"contract": {}, "error": None, "log": None})
    server.refresh_lock.acquire()

    t = threading.Thread(target=server.run_refresh,
                         args=("echo EARLY_MARKER; sleep 6",), daemon=True)
    t.start()
    deadline = time.time() + 5
    seen = False
    while time.time() < deadline and not seen:
        name = server.refresh_state.get("log")
        if name and (tmp_path / name).exists():
            seen = "EARLY_MARKER" in (tmp_path / name).read_text(errors="replace")
        time.sleep(0.2)
    assert seen, "early output must be readable mid-run"
    t.join(timeout=20)
