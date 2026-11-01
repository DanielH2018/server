"""The B4 NotReporting rules carry the deadline their producer renders, as a literal.

`files/rules/other-setup-checks.yaml` is plain YAML, so each `<Check>NotReporting` rule names its
deadline twice: the `>` threshold of the stale-file arm and the `[Ns]` window of the
absent_over_time arm. M6's batch B4 gives that pair to the UPS secondaries, the three fake-remux
heartbeats, the etcd restore drill with its fence leg, and the renovate notifier and agent. A
producer moved to a slower cadence without its rule would page on every healthy gap, and one
moved faster would leave the rule slower than the check (M1 convention 1).

Four producers render DEADLINE_S into a shell script. The two renovate units carry it in their
textfile ExecStartPost line. The Kuma tiles the deadlines once matched left in Cutover-1a, so
nothing but the producer pins a rule's literal now. Cutover-1b made every rule page, and the last
test holds it.

Run: uv run pytest ansible/tests/setup/test_b4_rule_literals.py
"""

import re

import pytest
from _helpers import K8S_ROLES
from _setup_render import render_setup_text
from _shell_render import render_shell_script
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "other-setup-checks.yaml"
# A bare `>` comparison: `>=` and `==` are not the stale-file arm.
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")
UNIT_DEADLINE = re.compile(r'write_check_textfile (\S+) 1 "\$\(date \+%%s\)" (\d+)')

OVERRIDES = {
    "domain": "example.test",
    "k3s_metallb_ingress_vip": "10.0.0.240",
    "sys_user": "ubuntu",
}
SHELL = {
    "ups": ("nut_host", "ups-secondary-health.sh.j2"),
    "fake-remux": ("fake_remux", "fake-remux-health.sh.j2"),
    "drill": ("hypervisor", "etcd-restore-drill-vm.sh.j2"),
}
UNITS = {
    "renovate-notify": ("renovate_notify", "renovate-notify.service.j2", {}),
    "renovate-agent": ("renovate_agent", "renovate-agent.service.j2", {}),
}
# (alert, origin) -> (the check label its expression reads, the producer that writes it).
PRODUCERS = {
    ("UpsSecondaryNotReporting", "daniel-box"): ("ups-secondary-health", "ups"),
    ("UpsSecondaryNotReporting", "daniel-server"): ("ups-secondary-health", "ups"),
    ("FakeRemuxScanNotReporting", "daniel-box"): ("fake-remux-scan", "fake-remux"),
    ("FakeRemuxReplaceNotReporting", "daniel-box"): (
        "fake-remux-replace",
        "fake-remux",
    ),
    ("MkvAttachmentRepairNotReporting", "daniel-box"): (
        "mkv-attachment-repair",
        "fake-remux",
    ),
    ("EtcdRestoreDrillNotReporting", "daniel-server"): (
        "etcd-restore-drill-vm",
        "drill",
    ),
    ("EtcdRestoreDrillEgressFenceNotReporting", "daniel-server"): (
        "etcd-restore-drill-egress-fence",
        "drill",
    ),
    ("RenovateNotifyNotReporting", "daniel-box"): (
        "renovate-notify",
        "renovate-notify",
    ),
    ("RenovateAgentNotReporting", "daniel-box"): ("renovate-agent", "renovate-agent"),
}


def not_reporting_rules(text: str) -> dict[tuple[str, str], str]:
    """(alert, origin) -> expr for every NotReporting rule in the group."""
    [group] = yaml_fast.safe_load(text)["groups"]
    found = {}
    for rule in group["rules"]:
        if not rule["alert"].endswith("NotReporting"):
            continue
        [origin] = re.findall(r'origin="([^"]+)"', rule["expr"].split("unless")[0])[:1]
        found[(rule["alert"], origin)] = rule["expr"]
    return found


def producer_text(producer: str) -> str:
    if producer in SHELL:
        role, template = SHELL[producer]
        return render_shell_script("setup", role, template, OVERRIDES)
    role, template, overrides = UNITS[producer]
    return render_setup_text(role, template, overrides)


def rendered_deadline(producer: str, check: str) -> int:
    """The deadline the producer renders for `check`."""
    text = producer_text(producer)
    if producer in UNITS:
        found = dict(UNIT_DEADLINE.findall(text))
        assert check in found, f"{producer} no longer writes the {check} textfile"
        return int(found[check])
    [deadline] = re.findall(r"^DEADLINE_S=(\d+)$", text, re.M)
    assert check in text, f"{producer} no longer names the {check} check"
    return int(deadline)


def literal_problems(expr: str, deadline: int) -> list[str]:
    """Why the rule's two deadline literals differ from `deadline`, or nothing."""
    problems = []
    for name, found in (
        ("the `>` threshold", STALE_ARM.findall(expr)),
        ("the absent_over_time window", WINDOW.findall(expr)),
    ):
        if len(found) != 1:
            problems.append(f"{len(found)} of {name}, not one")
        elif int(found[0]) != deadline:
            problems.append(f"{name} reads {found[0]}s, and DEADLINE_S is {deadline}")
    return problems


def test_the_group_holds_exactly_the_nine_not_reporting_rules_the_producers_table_names():
    # Non-vacuity: a rule dropped from the file, or a producer left out of PRODUCERS, fails here.
    assert sorted(not_reporting_rules(RULES.read_text())) == sorted(PRODUCERS)


@pytest.mark.parametrize("key", sorted(PRODUCERS))
def test_a_rules_literals_equal_the_deadline_its_producer_renders(key):
    check, producer = PRODUCERS[key]
    expr = not_reporting_rules(RULES.read_text())[key]
    assert f'check="{check}"' in expr
    assert literal_problems(expr, rendered_deadline(producer, check)) == []


def test_a_threshold_that_drifted_is_flagged():
    text = RULES.read_text().replace("> 129600", "> 100000")
    expr = not_reporting_rules(text)[("RenovateNotifyNotReporting", "daniel-box")]
    assert literal_problems(
        expr, rendered_deadline("renovate-notify", "renovate-notify")
    ) == ["the `>` threshold reads 100000s, and DEADLINE_S is 129600"]


def test_the_ups_deadline_follows_its_cron():
    # The UPS watchdog and the heartbeats render 2 x period + 300 from their cron variables.
    moved = render_shell_script(
        "setup",
        "nut_host",
        "ups-secondary-health.sh.j2",
        {**OVERRIDES, "nut_host_watchdog_interval_minutes": 30},
    )
    assert re.findall(r"^DEADLINE_S=(\d+)$", moved, re.M) == ["3900"]


def test_no_b4_rule_is_shadow():
    """M6 batch B4's Cutover-1b made every rule in the group page, the drill pair included.

    The drills had no `CheckWithoutPager` term, so the batch's five `[2h]` terms bounded their
    window, and those terms left with the same PR. A rule that regains `shadow` pages nobody
    while no Kuma tile pages in its place.
    """
    [group] = yaml_fast.safe_load(RULES.read_text())["groups"]
    assert {r["alert"] for r in group["rules"]} >= {
        "EtcdRestoreDrillFailed",
        "EtcdRestoreDrillNotReporting",
        "EtcdRestoreDrillEgressFenceFailed",
        "EtcdRestoreDrillEgressFenceNotReporting",
    }
    assert [r["alert"] for r in group["rules"] if "shadow" in r["labels"]] == []
