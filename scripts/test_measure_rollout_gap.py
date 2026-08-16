"""Gap summarisation: the arithmetic that turns poll samples into a downtime verdict.

The polling loop itself is I/O and is not unit-tested; this covers the part that decides
whether a rollout was zero-downtime, which is the part a wrong answer would mislead on.
"""

from __future__ import annotations

from measure_rollout_gap import GapReport, ready_count, summarize


def test_all_ok_reports_no_gap():
    samples = [(0.0, True), (0.5, True), (1.0, True)]
    assert summarize(samples) == GapReport(
        total=3, failures=0, longest_gap_s=0.0, gaps=[]
    )


def test_failure_window_runs_from_first_failure_to_recovery():
    samples = [(0.0, True), (0.5, False), (1.0, False), (1.5, True)]
    report = summarize(samples)
    assert report.failures == 2
    assert report.gaps == [(0.5, 1.5)]
    assert report.longest_gap_s == 1.0


def test_two_windows_report_the_longer_one():
    samples = [
        (0.0, True),
        (1.0, False),
        (2.0, True),
        (3.0, False),
        (4.0, False),
        (5.0, True),
    ]
    report = summarize(samples)
    assert report.gaps == [(1.0, 2.0), (3.0, 5.0)]
    assert report.longest_gap_s == 2.0


def test_trailing_failures_are_measured_to_the_last_sample():
    """A rollout that never recovers must not read as a zero-length gap."""
    samples = [(0.0, True), (1.0, False), (2.0, False)]
    report = summarize(samples)
    assert report.gaps == [(1.0, 2.0)]
    assert report.longest_gap_s == 1.0


def test_leading_failures_are_measured_from_the_first_sample():
    samples = [(0.0, False), (1.0, False), (2.0, True)]
    report = summarize(samples)
    assert report.gaps == [(0.0, 2.0)]


def test_empty_samples_is_not_a_pass():
    """Zero requests is a broken run, not a clean one."""
    report = summarize([])
    assert report.total == 0
    assert report.failures == 0


def test_ready_count_counts_only_ready_endpoints():
    assert ready_count("true\nfalse\ntrue\n") == 2


def test_ready_count_of_no_endpoints_is_zero():
    """An empty EndpointSlice is the outage this mode exists to catch, not a parse error."""
    assert ready_count("") == 0


def test_ready_count_ignores_blank_lines_and_padding():
    """kubectl's jsonpath output carries stray newlines between slices."""
    assert ready_count("\n true \n\nfalse\n\n true\n") == 2


def test_ready_count_treats_unready_as_not_ready():
    assert ready_count("false\nfalse\n") == 0
