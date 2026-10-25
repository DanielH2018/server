"""The NotReporting rules' deadline literals equal the DEADLINE_S each B2 producer renders.

`files/rules/initial-setup-checks.yaml` is plain YAML, so each NotReporting rule carries its
deadline as a literal twice: the `>` of the stale-file arm and the `[<n>s]` window of the
absent_over_time arm. The producer renders DEADLINE_S, and a producer moved to a slower cadence
without its rule would page on every healthy gap, while one moved faster would leave the rule
slower than the Kuma tile it shadows (M1 convention 1).

The Loki witness renders one script that both nodes run, so one DEADLINE_S pins both of its rules.

Run: uv run pytest ansible/tests/services/test_initial_setup_check_rule_literals.py
"""

import re

import pytest
from _helpers import K8S_ROLES
from _shell_render import render_shell_script
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "initial-setup-checks.yaml"
# A bare `>` comparison: `>=` and `==` are not the stale-file arm.
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")
# Alert prefix -> (template, the `check` label its textfile carries, the hosts that run it).
PRODUCERS = {
    "DocsRefresh": (
        ("components", "docs-refresh", "docs-refresh.sh.j2"),
        "docs-refresh",
        ["daniel-box"],
    ),
    "EvalSweep": (
        ("components", "eval-sweep", "eval-run.sh.j2"),
        "eval-run",
        ["daniel-box"],
    ),
    "SecretRotationAudit": (
        ("components", "secret-rotation", "secret-rotation-audit.sh.j2"),
        "secret-rotation-audit",
        ["daniel-box"],
    ),
    "SetupDrift": (
        ("setup", "initial_setup", "setup-drift-check.sh.j2"),
        "setup-drift-check",
        ["daniel-server"],
    ),
    "LokiReadRoute": (
        ("setup", "initial_setup", "loki-read-route-health.sh.j2"),
        "loki-read-route-health",
        ["daniel-box", "daniel-server"],
    ),
}


def rendered_deadline(template: tuple[str, str, str]) -> int:
    """DEADLINE_S as the producer renders it."""
    body = render_shell_script(template[0], template[1], template[2])
    [deadline] = re.findall(r"^\s*DEADLINE_S=(\d+)$", body, re.M)
    return int(deadline)


def not_reporting_rules(text: str) -> dict[tuple[str, str], str]:
    """(alert prefix, origin) -> expr for every NotReporting rule in the group."""
    [group] = yaml_fast.safe_load(text)["groups"]
    found = {}
    for rule in group["rules"]:
        alert = rule["alert"]
        if not alert.endswith("NotReporting"):
            continue
        [origin] = re.findall(r'origin="([^"]+)"', rule["expr"].split("unless")[0])[:1]
        found[(alert.removesuffix("NotReporting"), origin)] = rule["expr"]
    return found


def literal_problems(rules: dict[tuple[str, str], str], deadlines: dict[str, int]):
    """Why a rule's deadline literals differ from its producer's DEADLINE_S, or nothing."""
    problems = []
    for prefix, (_, check, hosts) in PRODUCERS.items():
        for host in hosts:
            expr = rules.get((prefix, host))
            if expr is None:
                problems.append(f"{prefix}NotReporting has no rule for {host}")
                continue
            if f'check="{check}"' not in expr:
                problems.append(f"{prefix}NotReporting on {host} does not read {check}")
            for name, found in (
                ("the `>` threshold", STALE_ARM.findall(expr)),
                ("the absent_over_time window", WINDOW.findall(expr)),
            ):
                if len(found) != 1:
                    problems.append(
                        f"{prefix}NotReporting on {host} has {len(found)} of {name}, not one"
                    )
                elif int(found[0]) != deadlines[prefix]:
                    problems.append(
                        f"{prefix}NotReporting on {host}: {name} reads {found[0]}s, "
                        f"and DEADLINE_S renders {deadlines[prefix]}"
                    )
    return problems


def shipped_deadlines() -> dict[str, int]:
    return {prefix: rendered_deadline(spec[0]) for prefix, spec in PRODUCERS.items()}


def test_literal_problems_is_clean_on_the_shipped_rules():
    deadlines = shipped_deadlines()
    # Non-vacuity: the pin found all five producers, and the rules cover six host/check pairs.
    assert set(deadlines) == set(PRODUCERS)
    rules = not_reporting_rules(RULES.read_text())
    assert len(rules) == 6
    assert literal_problems(rules, deadlines) == []


def test_literal_problems_is_flagged_on_a_threshold_that_drifted():
    text = RULES.read_text().replace("> 626400", "> 600000")
    assert literal_problems(not_reporting_rules(text), shipped_deadlines()) == [
        "EvalSweepNotReporting on daniel-box: the `>` threshold reads 600000s, "
        "and DEADLINE_S renders 626400"
    ]


def test_literal_problems_is_flagged_when_a_producer_deadline_moves_without_its_rule():
    deadlines = shipped_deadlines() | {"LokiReadRoute": 3900}
    problems = literal_problems(not_reporting_rules(RULES.read_text()), deadlines)
    assert len(problems) == 4  # two literals on each of the two witness hosts
    assert all("DEADLINE_S renders 3900" in p for p in problems)


@pytest.mark.parametrize("prefix", sorted(PRODUCERS))
def test_every_producer_names_the_check_label_its_rule_reads(prefix):
    template, check, _ = PRODUCERS[prefix]
    body = render_shell_script(template[0], template[1], template[2])
    assert re.search(rf"write_check_textfile {re.escape(check)} ", body), (
        f"{template[-1]} does not write the {check} textfile its rules read"
    )


def test_the_packaged_loki_witness_renders_the_deadline_the_rules_pin():
    """daniel-box's witness is a component since H7.36 and daniel-server's stays the role's
    template, so the shared rule's literals have to hold for both copies."""
    packaged = rendered_deadline(
        ("components", "loki-route-witness", "loki-read-route-health.sh.j2")
    )
    assert packaged == rendered_deadline(PRODUCERS["LokiReadRoute"][0])
