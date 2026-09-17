"""`probe.py health` tells a workload that ROLLED from one that was merely healthy — issue #1867.

THE GAP. The gate reads rollout-complete plus no restart in 180s, and the pods that were
already running satisfy both. A deploy that matched a host, changed the rendered manifests,
and rolled no pod read `settled`. The recap check (#1814, exit 78) catches only the no-host
case.

THE PREDICATE. Not the clock: "a pod newer than the deploy's start" fails every idempotent
re-run, which is every `land.sh` of a change that leaves the rendered manifests alone. The
release record (`release_stamp.yml`) says which workloads the apply QUEUED a restart of,
decided from the same facts the restart tasks read, and `kubectl rollout restart` stamps
`restartedAt` on the workload when one reaches it. The gate fails a workload the record
expected to roll whose `restartedAt` is absent or older than the record's `applied_at`.

Every narrowing has a reject half, and the accept halves are the shapes the issue names:
a docs-only change (no restart queued), a standalone run with no record, and a record from
before the field shipped all keep today's verdict.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_rolled.py
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _probe_health_fixtures import NOW, pods
from diagnostics.probe_lib import health, health_rollout

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy_tools"))
import deploy_detach_notify as notify_mod

APPLIED = NOW - timedelta(minutes=5)
APPLIED_STR = APPLIED.strftime("%Y-%m-%dT%H:%M:%SZ")


def _deploy(restarted_at=None, **annotations):
    """A rolled-out, quiet Deployment, optionally carrying a `restartedAt` annotation."""
    if restarted_at is not None:
        annotations[health_rollout.RESTARTED_AT_ANNOTATION] = restarted_at
    return {
        "kind": "Deployment",
        "metadata": {"generation": 1},
        "spec": {"replicas": 1, "template": {"metadata": {"annotations": annotations}}},
        "status": {
            "observedGeneration": 1,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
        },
    }


def _record(rollouts, applied_at=APPLIED_STR):
    return {"service": "freshrss", "applied_at": applied_at, "rollouts": rollouts}


def _checked(workload, name="freshrss"):
    return [("homelab", "Deployment", name, workload, pods(("app", 0, None)))]


# --- restarted_at / unrolled_reason -------------------------------------------------------


def test_restarted_at_reads_the_annotation_in_kubectls_utc_form():
    """`kubectl rollout restart` on the nodes writes second-precision UTC with a Z."""
    when = health_rollout.restarted_at(_deploy(restarted_at="2026-09-17T14:03:06Z"))
    assert when == datetime(2026, 9, 17, 14, 3, 6, tzinfo=timezone.utc)


def test_restarted_at_is_none_without_the_annotation_or_when_unreadable():
    assert health_rollout.restarted_at(_deploy()) is None
    assert health_rollout.restarted_at(_deploy(restarted_at="yesterday")) is None
    assert health_rollout.restarted_at({"spec": {}}) is None


def test_a_restart_after_the_apply_is_rolled():
    later = (APPLIED + timedelta(seconds=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert health_rollout.unrolled_reason(_deploy(restarted_at=later), APPLIED) is None


def test_a_restart_before_the_apply_is_not_rolled():
    earlier = (APPLIED - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reason = health_rollout.unrolled_reason(_deploy(restarted_at=earlier), APPLIED)
    assert reason and "NOT ROLLED" in reason and "older" in reason


def test_no_annotation_at_all_is_not_rolled():
    reason = health_rollout.unrolled_reason(_deploy(), APPLIED)
    assert reason and "NOT ROLLED" in reason and "no restartedAt" in reason


# --- release_expected_restarts ------------------------------------------------------------


def test_expected_restarts_names_only_the_workloads_the_apply_queued():
    """A rollout '' role records no entry; an extra that did not change records restart false."""
    expected = health.release_expected_restarts(
        _record(
            [
                {"name": "prowlarr", "kind": "deploy", "restart": True},
                {"name": "flaresolverr", "kind": "deploy", "restart": False},
            ]
        )
    )
    assert expected == {"prowlarr": APPLIED}


def test_expected_restarts_is_empty_when_the_record_cannot_answer():
    """The safe direction here is NO expectation: the two existing gate halves stay in force."""
    assert health.release_expected_restarts(None) == {}
    assert health.release_expected_restarts({"applied_at": APPLIED_STR}) == {}
    assert (
        health.release_expected_restarts(_record([{"name": "x", "restart": True}], "?"))
        == {}
    )
    assert health.release_expected_restarts(_record("not a list")) == {}
    assert health.release_expected_restarts(_record([{"restart": True}])) == {}


def test_a_record_written_by_the_stamp_on_daniel_box_parses(tmp_path):
    """`_release_record` reads the file the stamp writes, and tolerates its absence."""
    assert health._release_record("freshrss", tmp_path) is None
    (tmp_path / "freshrss.json").write_text(
        json.dumps(_record([{"name": "freshrss", "kind": "deploy", "restart": True}]))
    )
    assert health.release_expected_restarts(
        health._release_record("freshrss", tmp_path)
    ) == {"freshrss": APPLIED}
    (tmp_path / "freshrss.json").write_text("{trunc")
    assert health._release_record("freshrss", tmp_path) is None


# --- format_role_health, end to end -------------------------------------------------------


def test_a_queued_restart_that_never_reached_the_workload_fails_the_gate():
    """The issue's shape: healthy pods, complete rollout, and nothing rolled."""
    stale = (APPLIED - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    text, code = health.format_role_health(
        "freshrss", _checked(_deploy(restarted_at=stale)), NOW, {"freshrss": APPLIED}
    )
    assert code == 1
    assert "1 of 1 workloads FAILED" in text.splitlines()[0]
    assert "NOT ROLLED" in text


def test_a_queued_restart_that_rolled_passes():
    fresh = (APPLIED + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    text, code = health.format_role_health(
        "freshrss", _checked(_deploy(restarted_at=fresh)), NOW, {"freshrss": APPLIED}
    )
    assert code == 0, text
    assert "NOT ROLLED" not in text


def test_no_expectation_keeps_the_old_verdict_for_a_stale_workload():
    """A docs-only change, an idempotent re-run, a standalone run, a pre-field record: all
    hand the gate an empty expectation, and a workload last rolled a day ago stays green."""
    stale = (APPLIED - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for expected in (None, {}):
        text, code = health.format_role_health(
            "freshrss", _checked(_deploy(restarted_at=stale)), NOW, expected
        )
        assert code == 0, text


def test_the_expectation_is_matched_by_workload_name_not_role():
    """claude-otel's tag names no workload; the record names grafana, and only grafana is held."""
    stale = (APPLIED - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    checked = _checked(_deploy(restarted_at=stale), "grafana") + _checked(
        _deploy(restarted_at=stale), "loki"
    )
    text, code = health.format_role_health(
        "claude-otel", checked, NOW, {"grafana": APPLIED}
    )
    assert code == 1
    assert text.splitlines()[0].endswith("— homelab/grafana")


def test_the_not_rolled_line_carries_no_skip_marker():
    """`deploy_detach_notify` turns a first line carrying one of its markers into a `skipped`
    that does not fail the verdict; NOT ROLLED must never read that way."""
    text, _ = health.format_role_health(
        "freshrss", _checked(_deploy()), NOW, {"freshrss": APPLIED}
    )
    for line in text.splitlines():
        assert not any(m in line for m in notify_mod.NOT_APPLICABLE_MARKERS), line
