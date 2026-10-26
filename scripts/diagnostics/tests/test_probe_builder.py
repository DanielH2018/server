"""Tests for `probe.py builder`.

The textfiles and journal lines are the ones daniel-box held on 2026-10-25, in the shape
`host_reader.READER` returns them.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from diagnostics.probe_lib import builder, core
from lib.exit_codes import PROBE_FAILED, PROBE_OK

NOW = 1792959100.0
DEADLINE = builder.fetch_deadline_s()

BUILDER_PROM = """\
# HELP homelab_builder_fetch_timestamp_seconds End of the last run whose git fetch succeeded.
# TYPE homelab_builder_fetch_timestamp_seconds gauge
homelab_builder_fetch_timestamp_seconds {fetched}
# TYPE homelab_builder_unbuilt_since_timestamp_seconds gauge
homelab_builder_unbuilt_since_timestamp_seconds{{reason="{reason}",sha="4a1f8caa904c"}} 0.0
"""
REPO_PROM = """\
# TYPE homelab_repo_published_info gauge
homelab_repo_published_info{package="homelab-daniel-box",sha="341de76568f3",version="0.6267+g341de76568f3"} 1
homelab_repo_published_info{package="homelab-daniel-pi",sha="382df818e145",version="0.6283+g382df818e145"} 1
"""
ENTRIES = [
    [1792893422.0, 3, "f1", "systemd", "Failed to start homelab-builder.service - Homelab"],
    [1792893422.0, 3, "f1", "systemd", "homelab-builder.service: Failed with result 'exit-code'."],
    [1792958780.0, 5, "r1", "env", "homelab-builder: staged hosts/daniel-pi at 0.6283+g382df818e145"],
    [1792959001.0, 5, "r2", "env", "homelab-builder: 4a1f8caa904c changes no package"],
]  # fmt: skip


def _doc(reason="ok", fetched=NOW - 60, entries=ENTRIES, builder_prom=None):
    files = {
        builder.BUILDER_PROM: builder_prom
        or {
            "status": "ok",
            "text": BUILDER_PROM.format(reason=reason, fetched=fetched),
        },
        builder.REPO_PROM: {"status": "ok", "text": REPO_PROM},
    }
    return {
        "host": "daniel-box",
        "user": "ubuntu",
        "files": files,
        "journal": {"builder": {"rc": 0, "stderr": "", "entries": entries}},
    }


def _report(doc):
    return builder.report(doc, now=NOW, last=5, deadline_s=DEADLINE)


def test_a_caught_up_builder_is_publishing():
    text, code = _report(_doc())
    assert code == PROBE_OK
    assert "reason=ok at 4a1f8caa904c, caught up" in text
    assert "homelab-daniel-pi       0.6283+g382df818e145" in text
    assert "hosts/daniel-pi at 0.6283+g382df818e145" in text
    assert text.endswith("verdict: publishing")


def test_a_refusing_reason_is_the_fail_verdict():
    text, code = _report(_doc(reason="ci"))
    assert code == PROBE_FAILED
    assert "verdict: reason ci" in text


def test_the_canary_wait_is_not_a_failure():
    assert _report(_doc(reason="canary"))[1] == PROBE_OK


def test_a_fetch_older_than_the_deadline_is_stale():
    text, code = _report(_doc(fetched=NOW - DEADLINE - 1))
    assert code == PROBE_FAILED
    assert f"past {DEADLINE}s" in text


def test_the_deadline_is_two_timer_periods_plus_the_run_ceiling():
    """2min timer, 20min TimeoutStartSec in hosts/components/builder/units/."""
    assert DEADLINE == 2 * 120 + 1200


def test_a_failed_run_counts_once_and_a_quiet_line_not_at_all():
    text, _ = _report(_doc())
    assert "failures in the window: 1" in text
    assert "Failed with result 'exit-code'" in text


def test_a_builder_error_line_counts_as_a_failure():
    err = [1792959001.0, 3, "r3", "env", "homelab-builder: ci: 4a1f8caa904c is red"]
    text, _ = _report(_doc(entries=[*ENTRIES, err]))
    assert "failures in the window: 2" in text
    assert "homelab-builder: ci: 4a1f8caa904c is red" in text


def test_a_missing_textfile_is_unreadable_not_a_verdict():
    with pytest.raises(core.Unreadable, match=r"homelab_builder\.prom"):
        _report(_doc(builder_prom={"status": "absent"}))


def test_the_dry_run_names_the_read_and_makes_none(capsys):
    def no_read(*_a, **_k):
        raise AssertionError("a dry run must not read")

    ns = SimpleNamespace(since="6h", last=5, dry_run=True)
    assert builder.run_builder(ns, read=no_read) == PROBE_OK
    out = capsys.readouterr().out
    assert "homelab-builder.service" in out and builder.BUILDER_PROM in out
