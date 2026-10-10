import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trend import (
    RUNS_KEY,
    _epoch_shift,
    classify,
    load_history,
    main,
    record_run,
    write_json,
)


def _entry(met, mode="hermetic"):
    return {
        "ts": 0,
        "mode": mode,
        "status": "PASS" if met else "FAIL",
        "passes": 3 if met else 1,
        "healthy": 3,
        "passRate": 1.0 if met else 0.33,
        "thresholdMet": met,
    }


def _report_case(cid, status, threshold_met, passes=3, healthy=3, **extra):
    return {
        **extra,
        "id": cid,
        "k": 3,
        "healthy": healthy,
        "passes": passes,
        "passRate": passes / healthy if healthy else 0,
        "allPass": passes == healthy,
        "thresholdMet": threshold_met,
        "status": status,
    }


def test_record_run_appends_entry_per_case():
    hist = {}
    record_run(
        hist,
        [
            _report_case("a/1", "PASS", True),
            _report_case("a/2", "FAIL", False, passes=1),
        ],
        ts=100,
        mode="hermetic",
    )
    assert hist["a/1"][0]["thresholdMet"] is True
    assert hist["a/2"][0]["thresholdMet"] is False
    assert hist["a/1"][0]["ts"] == 100 and hist["a/1"][0]["mode"] == "hermetic"


def test_record_run_inconclusive_has_no_signal():
    hist = {}
    record_run(
        hist,
        [_report_case("a/1", "INCONCLUSIVE", False, passes=0, healthy=1)],
        ts=1,
        mode="hermetic",
    )
    assert hist["a/1"][0]["thresholdMet"] is None


def test_classify_regressed():
    hist = {"a/1": [_entry(True), _entry(True), _entry(False)]}
    assert classify(hist) == {
        "regressed": ["a/1"],
        "recovered": [],
        "flaky": [],
        "stable": [],
    }


def test_classify_recovered():
    hist = {"a/1": [_entry(False), _entry(False), _entry(True)]}
    assert classify(hist)["recovered"] == ["a/1"]


def test_classify_stable():
    hist = {"a/1": [_entry(True), _entry(True), _entry(True)]}
    assert classify(hist)["stable"] == ["a/1"]


def test_classify_flaky():
    # cur True, prev True (not regressed/recovered); last 3 = [F,T,T] (not stable);
    # window has both True and False -> flaky.
    hist = {"a/1": [_entry(True), _entry(False), _entry(True), _entry(True)]}
    assert classify(hist)["flaky"] == ["a/1"]


def test_classify_needs_two_signal_runs():
    hist = {"a/1": [_entry(True)]}
    assert classify(hist) == {
        "regressed": [],
        "recovered": [],
        "flaky": [],
        "stable": [],
    }


def test_classify_ignores_other_mode():
    hist = {"a/1": [_entry(True, mode="subscription")] * 3}
    assert classify(hist, mode="hermetic")["stable"] == []


def test_classify_inconclusive_entries_skipped():
    hist = {
        "a/1": [
            _entry(True),
            {"mode": "hermetic", "thresholdMet": None},
            _entry(True),
            _entry(True),
        ]
    }
    assert classify(hist)["stable"] == ["a/1"]


def test_history_roundtrip(tmp_path):
    path = tmp_path / "history.json"
    assert load_history(path) == {}
    write_json(path, {"a/1": [_entry(True)]})
    assert load_history(path)["a/1"][0]["thresholdMet"] is True


def test_load_history_tolerates_corrupt(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("{not json")
    assert load_history(path) == {}


def test_main_records_and_flags_regression(tmp_path, capsys):
    hist = tmp_path / "history.json"
    write_json(hist, {"a/1": [_entry(True), _entry(True)]})
    report = tmp_path / "report.json"
    report.write_text(json.dumps([_report_case("a/1", "FAIL", False, passes=1)]))
    rc = main(["--history", str(hist), str(report)])
    assert rc == 1
    saved = load_history(hist)
    assert len(saved["a/1"]) == 3 and saved["a/1"][-1]["thresholdMet"] is False
    assert "REGRESSED" in capsys.readouterr().out


def test_main_subscription_does_not_write(tmp_path):
    hist = tmp_path / "history.json"
    report = tmp_path / "report.json"
    report.write_text(json.dumps([_report_case("a/1", "FAIL", False)]))
    rc = main(["--history", str(hist), "--mode", "subscription", str(report)])
    assert rc == 0
    assert not hist.exists()


def test_record_run_stamps_epoch():
    hist = {}
    record_run(
        hist,
        [_report_case("a/1", "PASS", True)],
        ts=1,
        mode="hermetic",
        epoch="opus-4.8/cc-2.0",
    )
    assert hist["a/1"][0]["epoch"] == "opus-4.8/cc-2.0"


def test_record_run_epoch_defaults_to_unknown():
    hist = {}
    record_run(hist, [_report_case("a/1", "PASS", True)], ts=1, mode="hermetic")
    assert hist["a/1"][0]["epoch"] == "unknown"


def test_epoch_shift_detected_and_none_when_same():
    same = [{**_entry(True), "epoch": "e1"}, {**_entry(False), "epoch": "e1"}]
    assert _epoch_shift(same, "hermetic") is None
    shifted = [{**_entry(True), "epoch": "e1"}, {**_entry(False), "epoch": "e2"}]
    assert _epoch_shift(shifted, "hermetic") == ("e1", "e2")


def test_regression_across_epoch_is_annotated(tmp_path, capsys):
    hist = tmp_path / "history.json"
    write_json(
        hist,
        {"a/1": [{**_entry(True), "epoch": "e1"}, {**_entry(True), "epoch": "e1"}]},
    )
    report = tmp_path / "report.json"
    report.write_text(json.dumps([_report_case("a/1", "FAIL", False, passes=1)]))
    rc = main(["--history", str(hist), "--epoch", "e2", str(report)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "REGRESSED" in out and "e1 -> e2" in out


def test_record_run_stores_the_summed_sweep_cost_on_the_run_entry():
    hist = {}
    record_run(
        hist,
        [
            _report_case("a/1", "PASS", True, costUsd=0.1),
            _report_case("a/2", "FAIL", False, passes=1, costUsd=0.2),
        ],
        ts=7,
        mode="hermetic",
        epoch="e1",
    )
    # 0.1 + 0.2 is 0.30000000000000004 in floats; the stored total matches the engine's
    # 4-decimal `sweep cost:` line instead.
    assert hist[RUNS_KEY] == [
        {"ts": 7, "mode": "hermetic", "epoch": "e1", "costUsd": 0.3}
    ]
    assert "costUsd" not in hist["a/1"][0]


def test_record_run_without_cost_fields_records_no_cost():
    hist = {}
    record_run(hist, [_report_case("a/1", "PASS", True)], ts=1, mode="hermetic")
    assert hist[RUNS_KEY][0]["costUsd"] is None


def test_record_run_partly_priced_report_records_no_cost():
    hist = {}
    record_run(
        hist,
        [
            _report_case("a/1", "PASS", True, costUsd=0.5),
            _report_case("a/2", "PASS", True),
        ],
        ts=1,
        mode="hermetic",
    )
    assert hist[RUNS_KEY][0]["costUsd"] is None


def test_main_records_one_cost_entry_per_run(tmp_path):
    hist = tmp_path / "history.json"
    write_json(hist, {"a/1": [_entry(True), _entry(True)]})
    report = tmp_path / "report.json"
    report.write_text(json.dumps([_report_case("a/1", "PASS", True, costUsd=0.25)]))
    assert main(["--history", str(hist), str(report)]) == 0
    assert main(["--history", str(hist), str(report)]) == 0
    saved = load_history(hist)
    assert [r["costUsd"] for r in saved[RUNS_KEY]] == [0.25, 0.25]
    assert classify(saved)["stable"] == ["a/1"]
