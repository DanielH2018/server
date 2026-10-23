#!/usr/bin/env python3
"""The Pi's two token heartbeats write a homelab_check_* textfile beside their Kuma push (M6 B5).

docs/target-architecture/monitoring/m6-retire-kuma.md, batch B5: pi-sd-health and
pi-recovery-health each write `<check>.prom` through homelab-check-lib.sh, so the rules in
`pi-checks.yaml` can shadow the "Daniel Pi SD Health" and "Daniel Pi Recovery" tiles. The Kuma
pushes are unchanged. Each case runs the rendered script through `_pi_health` against stubs, with
the role's check library and `TEXTFILE_DIR` pointed at a temp directory.

Three properties:

- the file carries success 1 for an up verdict and 0 for a down one, so a cycle that restarted a
  container reads 0 as the Kuma push does;
- the file exists before the push is attempted, because a push retries for over a minute;
- a failed push never costs the textfile.

The rules' deadline literals equal the DEADLINE_S each script renders (M1 convention 1).

Run: uv run pytest ansible/tests/setup/test_pi_heartbeat_textfiles.py
"""

import re

import pytest
from _helpers import K8S_ROLES
from _pi_health import PI_HOST_VARS, render, run, textfile
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "pi-checks.yaml"
# A bare `>` comparison: `>=` and `==` are not the stale-file arm.
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")
# Alert prefix -> the script, and the `check` label, whose textfile its rules read.
PRODUCERS = {"PiSdHealth": "pi-sd-health", "PiRecovery": "pi-recovery-health"}
# Every container the recovery heartbeat watches, so a run with all of them up is a clean cycle.
ALL = [c["name"] for c in PI_HOST_VARS["containers_list"]] + ["docker-proxy-lifecycle"]


def _textfile_came_first(tmp_path) -> bool:
    """The push stub drops this marker when the .prom file already existed at push time."""
    return (tmp_path / "push.textfile-first").exists()


def test_a_healthy_sd_cycle_writes_success_one_before_the_push(tmp_path):
    status, _, _, _ = run("pi-sd-health", tmp_path, counter="0")

    assert status == "up"
    assert textfile(tmp_path, "pi-sd-health")["homelab_check_success"] == 1
    assert _textfile_came_first(tmp_path)


def test_a_climbing_error_count_writes_success_zero(tmp_path):
    status, _, _, _ = run("pi-sd-health", tmp_path, counter="3")

    assert status == "down"
    assert textfile(tmp_path, "pi-sd-health")["homelab_check_success"] == 0


def test_a_healthy_recovery_cycle_writes_success_one_before_the_push(tmp_path):
    status, _, _, _ = run("pi-recovery-health", tmp_path, running=ALL)

    assert status == "up"
    assert textfile(tmp_path, "pi-recovery-health")["homelab_check_success"] == 1
    assert _textfile_came_first(tmp_path)


def test_a_cycle_that_restarted_a_container_writes_success_zero(tmp_path):
    status, msg, _, _ = run(
        "pi-recovery-health", tmp_path, running=[c for c in ALL if c != "autoheal"]
    )

    assert status == "down" and "restarted: autoheal" in msg
    assert textfile(tmp_path, "pi-recovery-health")["homelab_check_success"] == 0


@pytest.mark.parametrize("script", sorted(PRODUCERS.values()))
def test_a_failed_push_still_leaves_the_textfile(tmp_path, script):
    kwargs = {"counter": "0"} if script == "pi-sd-health" else {"running": ALL}
    run(script, tmp_path, push_ok="0", **kwargs)

    series = textfile(tmp_path, script)
    assert series["homelab_check_success"] == 1
    assert series["homelab_check_deadline_seconds"] == 900


def rendered_deadline(script: str, tmp_path) -> int:
    """DEADLINE_S as the producer renders it."""
    path, _ = render(script, tmp_path)
    [deadline] = re.findall(r"^DEADLINE_S=(\d+)$", path.read_text(), re.M)
    return int(deadline)


def not_reporting_rules(text: str) -> dict[str, str]:
    """alert prefix -> expr for every NotReporting rule in the group."""
    [group] = yaml_fast.safe_load(text)["groups"]
    return {
        rule["alert"].removesuffix("NotReporting"): rule["expr"]
        for rule in group["rules"]
        if rule["alert"].endswith("NotReporting")
    }


def literal_problems(rules: dict[str, str], deadlines: dict[str, int]) -> list[str]:
    """Why a rule's deadline literals differ from its producer's DEADLINE_S, or nothing."""
    problems = []
    for prefix, check in PRODUCERS.items():
        expr = rules.get(prefix)
        if expr is None:
            problems.append(f"{prefix}NotReporting has no rule")
            continue
        if f'check="{check}"' not in expr:
            problems.append(f"{prefix}NotReporting does not read {check}")
        for name, found in (
            ("the `>` threshold", STALE_ARM.findall(expr)),
            ("the absent_over_time window", WINDOW.findall(expr)),
        ):
            if len(found) != 1:
                problems.append(
                    f"{prefix}NotReporting has {len(found)} of {name}, not one"
                )
            elif int(found[0]) != deadlines[prefix]:
                problems.append(
                    f"{prefix}NotReporting: {name} reads {found[0]}s, "
                    f"and DEADLINE_S renders {deadlines[prefix]}"
                )
    return problems


@pytest.fixture
def deadlines(tmp_path_factory) -> dict[str, int]:
    return {
        prefix: rendered_deadline(script, tmp_path_factory.mktemp(script))
        for prefix, script in PRODUCERS.items()
    }


def test_literal_problems_is_clean_on_the_shipped_rules(deadlines):
    rules = not_reporting_rules(RULES.read_text())
    # Non-vacuity: both producers are found, and each has its rule.
    assert set(deadlines) == set(rules) == set(PRODUCERS)
    assert literal_problems(rules, deadlines) == []


def test_literal_problems_is_flagged_on_a_threshold_that_drifted(deadlines):
    text = RULES.read_text().replace("> 900", "> 1200", 1)
    problems = literal_problems(not_reporting_rules(text), deadlines)
    assert len(problems) == 1 and "the `>` threshold reads 1200s" in problems[0]
