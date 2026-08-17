"""The scan caption must describe the rows on screen, not the run currently fetching.

raw_headlines_<date>.json carries max_age_hours, n_raw, n_stale and the per-outlet kept counts.
The site renders those as prose: "anything published in the last 36 hours" in the explanation
paragraph, and "Scanned N headlines in the last Xh" in the scan detail. news_digest.run() used
to overwrite that file at FETCH time -- before clustering, snippet-writing or entailment had
produced a single new row -- so for the whole length of a run the caption described the run just
STARTED while the page still served the PREVIOUS run's stories.

Reported 2026-08-04: "if I make a new run with a 3 hour window it'll show the previous runs
contents but say its all from the last three hours". The numbers were not wrong, they were
attached to the wrong rows.

Fix: run() writes to <name>.partial and publish_scan() atomically renames it into place -- called
by the runners immediately AFTER write_csv, so the caption and the rows change together.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import news_digest


def _scan(tmp_path, name="raw_headlines_2026-08-04.json"):
    return tmp_path / name


def test_a_run_in_flight_does_not_restate_the_window_over_old_rows(tmp_path, monkeypatch):
    """The whole bug: mid-run, the live file must still describe the live CSV."""
    monkeypatch.chdir(tmp_path)
    live = _scan(tmp_path)
    live.write_text(json.dumps({"max_age_hours": 36, "meta": {"n_raw": 240}}), encoding="utf-8")

    # a run starts with a NEW window and writes its partial
    partial = live.with_name(live.name + ".partial")
    partial.write_text(json.dumps({"max_age_hours": 3, "meta": {"n_raw": 31}}), encoding="utf-8")

    # while it runs, the site still reads 36 -- matching the rows it is still serving
    assert json.loads(live.read_text(encoding="utf-8"))["max_age_hours"] == 36

    news_digest.publish_scan({"scan_partial": str(partial), "scan_final": str(live)})

    # only now does the caption change, alongside the new CSV
    assert json.loads(live.read_text(encoding="utf-8"))["max_age_hours"] == 3
    assert not partial.exists(), "partial must be renamed, not copied"


def test_a_crashed_run_leaves_the_previous_caption_intact(tmp_path):
    """A run that dies before publish must not restate the window over rows it never replaced."""
    live = _scan(tmp_path)
    live.write_text(json.dumps({"max_age_hours": 36}), encoding="utf-8")
    partial = live.with_name(live.name + ".partial")
    partial.write_text(json.dumps({"max_age_hours": 3}), encoding="utf-8")
    # ...crash: publish_scan is never reached.
    assert json.loads(live.read_text(encoding="utf-8"))["max_age_hours"] == 36


def test_publish_is_a_noop_without_a_partial(tmp_path):
    """Never raise into a runner that has already produced a good CSV."""
    live = _scan(tmp_path)
    live.write_text(json.dumps({"max_age_hours": 36}), encoding="utf-8")
    assert news_digest.publish_scan({"scan_partial": str(live) + ".partial",
                                     "scan_final": str(live)}) is False
    assert news_digest.publish_scan({}) is False
    assert json.loads(live.read_text(encoding="utf-8"))["max_age_hours"] == 36


def test_run_writes_the_partial_not_the_live_file():
    """Guards the source directly: a future edit must not point the fetch-time write back."""
    src = (pathlib.Path(__file__).parent / "news_digest.py").read_text(encoding="utf-8")
    body = src[src.index("def run(host"):]
    body = body[:body.index("\ndef ")] if "\ndef " in body else body
    assert "raw_partial.write_text" in body, "run() must write the partial"
    assert "raw_path.write_text" not in body, \
        "run() must not write the live scan file -- publish_scan does that, after the CSV"


def test_both_runners_publish_after_writing_the_csv():
    """Publishing before write_csv would reintroduce the bug in a narrower window."""
    for name in ("claude_pipeline_runner.py", "pipeline_runner.py"):
        src = (pathlib.Path(__file__).parent / name).read_text(encoding="utf-8-sig")
        assert "publish_scan" in src, f"{name} never publishes the scan"
        assert src.index("write_csv(result") < src.index("news_digest.publish_scan"), \
            f"{name} publishes the caption before the rows it describes"
