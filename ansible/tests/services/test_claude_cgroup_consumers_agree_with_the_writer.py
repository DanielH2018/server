#!/usr/bin/env python3
"""Every consumer of `claude_cgroup_*` must name a metric the writer actually emits.

Issue #1258 added the two consumers: monitor-bridge's `with_claude_cgroups` arm and the
`AI/claude-code-host-cgroups` Grafana board. Both select by metric name across a tree boundary —
the names are written by `roles/setup/claude_code/files/claude-cgroup-metrics.sh` — and a
name-based selector that stops matching returns an EMPTY vector rather than an error. The board
then renders "No data" behind a healthy Grafana pod, and the alert arm's third verdict is the only
thing standing between that and a silent green. `Apps/backups-b2-usage.json` sat in exactly that
state for two weeks after kopia retired: the datasource resolved perfectly the whole time.

THIS IS THE NON-VACUITY HALF of the red-proof pair in
`roles/k8s/monitor-bridge/tests/test_check_claude_cgroups.py`. That file proves the verdict can go
red on a fixture; a fixture cannot notice that the real series were renamed out from under it.
Both halves are needed, and this one is the half that goes empty: `EMITTED` is asserted against a
named frozenset rather than a count, so a writer that stops emitting fails with the missing name
instead of passing over nothing.

Run: uv run pytest ansible/tests/services/test_claude_cgroup_consumers_agree_with_the_writer.py
"""

import json
import re

from _helpers import ANSIBLE


WRITER = (
    ANSIBLE / "roles" / "setup" / "claude_code" / "files" / "claude-cgroup-metrics.sh"
)
ARM = ANSIBLE / "roles" / "k8s" / "monitor-bridge" / "files" / "checks" / "host.py"
BOARD = (
    ANSIBLE
    / "roles"
    / "k8s"
    / "claude-otel"
    / "files"
    / "dashboards"
    / "AI"
    / "claude-code-host-cgroups.json"
)

# The metric families PR #1251 shipped. Named rather than counted, so a writer that drops one
# fails with the name it dropped. Any consumer selector outside this set is a typo or a rename.
EMITTED = frozenset(
    {
        "claude_cgroup_memory_current_bytes",
        "claude_cgroup_memory_swap_current_bytes",
        "claude_cgroup_memory_events_total",
        "claude_cgroup_memory_pressure_stalled_usec_total",
        "claude_cgroup_cpu_usage_usec_total",
        "claude_cgroup_pids_current",
    }
)

# The two `cgroup` label values the writer labels its series with. `claude-rc` is the one whose
# absence the alert arm treats as a fault; `user-1000-slice` exists only once somebody has logged
# in since boot, so it is watched-when-present rather than required.
LABELLED = frozenset({"claude-rc", "user-1000-slice"})

METRIC_RE = re.compile(r"claude_cgroup_[a-z0-9_]+")
# The arm's module also names `claude_cgroup_verdict`, a function rather than a series, in both its
# code and its prose. Matching only a name carrying a label selector picks out the two PromQL
# selectors and nothing else — and a MISTYPED metric name still carries its selector, so this
# narrowing costs no detection.
ARM_SELECTOR_RE = re.compile(r"claude_cgroup_[a-z0-9_]+(?=\{)")


def _writer_metrics() -> set[str]:
    return set(METRIC_RE.findall(WRITER.read_text()))


def _writer_cgroup_labels() -> set[str]:
    """The keys of the script's `declare -A CGROUPS=(...)` map."""
    body = WRITER.read_text().split("declare -A CGROUPS=(", 1)[1].split(")", 1)[0]
    return set(re.findall(r"\[([A-Za-z0-9_-]+)\]=", body))


def _board_exprs() -> list[str]:
    board = json.loads(BOARD.read_text())
    return [
        t["expr"]
        for panel in board["panels"]
        for t in panel.get("targets", [])
        if "expr" in t
    ]


def test_the_writer_still_emits_every_metric_family_the_consumers_were_built_on():
    """The named-member assertion. An empty or narrowed writer fails here, not silently."""
    assert EMITTED <= _writer_metrics()


def test_the_writer_still_labels_both_cgroups():
    assert LABELLED == _writer_cgroup_labels()


def test_the_alert_arm_selects_only_emitted_metrics():
    """And selects at least the two families the alert is built on, so it cannot go vacuous."""
    used = set(ARM_SELECTOR_RE.findall(ARM.read_text()))
    assert used, "with_claude_cgroups selects no claude_cgroup_* metric at all"
    assert used <= EMITTED, sorted(used - EMITTED)
    assert {
        "claude_cgroup_memory_pressure_stalled_usec_total",
        "claude_cgroup_memory_events_total",
    } <= used


def test_the_board_selects_only_emitted_metrics_and_graphs_every_family():
    """Every family is graphed somewhere on the board — that is the point of the board.

    The board exists because #1258 found six scraped families with no panel. Asserting the whole
    set rather than "at least one panel" is what stops the board drifting back toward the state
    the issue was filed about.
    """
    exprs = _board_exprs()
    assert exprs, "the board declares no panel targets"
    used = set(METRIC_RE.findall(" ".join(exprs)))
    assert used <= EMITTED, sorted(used - EMITTED)
    assert used == EMITTED, sorted(EMITTED - used)


def test_the_board_uid_is_unique_across_every_provisioned_folder():
    """A duplicate dashboard uid freezes ALL Grafana provisioning, not just the offender."""
    dashboards = sorted((BOARD.parent.parent).rglob("*.json"))
    assert len(dashboards) >= 20, len(dashboards)
    owners = [
        p
        for p in dashboards
        if json.loads(p.read_text()).get("uid") == "claude-code-host-cgroups"
    ]
    assert owners == [BOARD], owners
