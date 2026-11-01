#!/usr/bin/env python3
"""The Pi's two heartbeats write a homelab_check_* textfile and push nothing (M6 B5).

docs/target-architecture/monitoring/m6-retire-kuma.md, batch B5: pi-sd-health and
pi-recovery-health each write `<check>.prom` through homelab-check-lib.sh, so the rules in
`pi-checks.yaml` page for the "Daniel Pi SD Health" and "Daniel Pi Recovery" tiles that Cutover-1a
deleted. Cutover-2 dropped the Kuma pushes. Each case runs the rendered script through
`_pi_health` against stubs, with the role's check library and `TEXTFILE_DIR` pointed at a temp
directory.

Three properties:

- the file carries success 1 for an up verdict and 0 for a down one, so a cycle that restarted a
  container reads 0;
- the rendered script holds no Kuma push, token read or push-library source;
- the rules' deadline literals equal the DEADLINE_S each script renders (M1 convention 1).

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
# Both of the Pi's lists, since the recovery heartbeat watches the union (H5.1), plus the
# sub-proxy the docker-proxy compose file declares.
ALL = [
    c["name"]
    for c in PI_HOST_VARS["containers_list"] + PI_HOST_VARS["compose_services"]
] + ["docker-proxy-lifecycle"]


def test_a_healthy_sd_cycle_writes_success_one(tmp_path):
    status, _, _, _ = run("pi-sd-health", tmp_path, counter="0")

    assert status == "up"
    assert textfile(tmp_path, "pi-sd-health")["homelab_check_success"] == 1


def test_a_climbing_error_count_writes_success_zero(tmp_path):
    status, _, _, _ = run("pi-sd-health", tmp_path, counter="3")

    assert status == "down"
    assert textfile(tmp_path, "pi-sd-health")["homelab_check_success"] == 0


def test_a_healthy_recovery_cycle_writes_success_one(tmp_path):
    status, _, _, _ = run("pi-recovery-health", tmp_path, running=ALL)

    assert status == "up"
    assert textfile(tmp_path, "pi-recovery-health")["homelab_check_success"] == 1


def test_a_cycle_that_restarted_a_container_writes_success_zero(tmp_path):
    status, msg, _, _ = run(
        "pi-recovery-health", tmp_path, running=[c for c in ALL if c != "autoheal"]
    )

    assert status == "down" and "restarted: autoheal" in msg
    assert textfile(tmp_path, "pi-recovery-health")["homelab_check_success"] == 0


# What a push leaves in a rendered script: the Kuma helper, its push call, the success flag it
# sets, the push URL, or the credential the unit used to load for it.
PUSH_MARKERS = ("kuma-push-lib", "kuma_push", "KUMA_PUSH_OK", "api/push", "push_token")


def push_problems(text: str) -> list[str]:
    """Each push marker a rendered script still carries, or nothing."""
    return [marker for marker in PUSH_MARKERS if marker in text]


@pytest.mark.parametrize("script", sorted(PRODUCERS.values()))
def test_a_rendered_heartbeat_holds_no_kuma_push(tmp_path, script):
    path, _ = render(script, tmp_path)
    assert push_problems(path.read_text()) == []


def test_push_problems_is_flagged_on_a_script_that_still_pushes(tmp_path):
    # The red proof: the push call and the success flag it sets are each found.
    pushing = 'kuma_push "$STATUS" "$MSG" "$PUSH_URL"\n[ "${KUMA_PUSH_OK:-1}" = 1 ]\n'
    assert push_problems(pushing) == ["kuma_push", "KUMA_PUSH_OK"]


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
