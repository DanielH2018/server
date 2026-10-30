"""The B4 NotReporting rules carry the deadline their producer renders, as a literal.

`files/rules/other-setup-checks.yaml` is plain YAML, so each `<Check>NotReporting` rule names its
deadline twice: the `>` threshold of the stale-file arm and the `[Ns]` window of the
absent_over_time arm. M6's batch B4 gives that pair to the UPS secondaries, the three fake-remux
heartbeats, the etcd restore drill with its fence leg, and the renovate notifier and agent. A
producer moved to a slower cadence without its rule would page on every healthy gap, and one
moved faster would leave the rule slower than the check (M1 convention 1).

Four producers render DEADLINE_S into a shell script. The two renovate units carry it in their
textfile ExecStartPost line. The drill and the two renovate tiles share their deadline with the
Kuma tile they shadow, so those three are also pinned to the tile's interval.

Run: uv run pytest ansible/tests/setup/test_b4_rule_literals.py
"""

import re

import pytest
from _helpers import K8S_ROLES
from _kuma_monitors import entity
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
    "renovate-agent": (
        "renovate_agent",
        "renovate-agent.service.j2",
        {"renovate_agent_kuma_push_token": "tok"},
    ),
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


def test_the_drill_deadline_is_the_tiles_interval_and_the_ups_deadline_follows_its_cron():
    # The drill shares etcd_drill_full_kuma_interval_s with both of its Kuma tiles.
    full = entity("etcd-restore-drill-full.json", "etcd_drill_full_push_token")
    fence = entity("etcd-restore-drill-fence.json", "etcd_drill_fence_push_token")
    assert (
        full["interval"]
        == fence["interval"]
        == rendered_deadline("drill", "etcd-restore-drill-vm")
    )
    # The UPS watchdog and the heartbeats render 2 x period + 300 from their cron variables.
    moved = render_shell_script(
        "setup",
        "nut_host",
        "ups-secondary-health.sh.j2",
        {**OVERRIDES, "nut_host_watchdog_interval_minutes": 30},
    )
    assert re.findall(r"^DEADLINE_S=(\d+)$", moved, re.M) == ["3900"]


@pytest.mark.parametrize(
    ("producer", "token_var", "tile", "overrides"),
    [
        (
            "renovate-notify",
            "monitor_bridge_renovate_alive_push_token",
            "renovate-notify-alive.json",
            {},
        ),
        (
            "renovate-agent",
            "renovate_agent_kuma_push_token",
            "renovate-agent-alive.json",
            {"renovate_agent_enabled": True},
        ),
    ],
)
def test_a_renovate_deadline_is_the_interval_of_the_tile_it_shadows(
    producer, token_var, tile, overrides
):
    assert entity(tile, token_var, overrides)["interval"] == rendered_deadline(
        producer, producer
    )


CUTOVER = K8S_ROLES / "observability" / "files" / "rules" / "kuma-cutover.yaml"
DRILL_RULES = (
    "EtcdRestoreDrillFailed",
    "EtcdRestoreDrillNotReporting",
    "EtcdRestoreDrillEgressFenceFailed",
    "EtcdRestoreDrillEgressFenceNotReporting",
)
# B4's tiles whose CheckWithoutPager term fits Prometheus's retention. The drill tiles retire in
# the same Cutover-1a and their rules leave shadow in the same Cutover-1b, so a term here bounds
# the drills' no-pager window too.
DRILL_WINDOW_SIBLINGS = (
    "UPS Secondary (daniel-box)",
    "UPS Secondary (daniel-server)",
    "Fake Remux Scan",
    "Fake Remux Replace",
    "MKV Attachment Repair",
)


def drill_window_problems(rules_text: str, cutover_text: str) -> list[str]:
    """Why a shadow drill rule has no sibling CheckWithoutPager term to bound its window."""
    [group] = yaml_fast.safe_load(rules_text)["groups"]
    shadow = [
        r["alert"]
        for r in group["rules"]
        if r["alert"] in DRILL_RULES and r.get("labels", {}).get("shadow") == "true"
    ]
    if not shadow:
        return []
    terms = set(re.findall(r'monitor_name="([^"]+)"\}\[2h\]', cutover_text))
    if terms & set(DRILL_WINDOW_SIBLINGS):
        return []
    return [f"{', '.join(shadow)} shadow with no B4 sibling term in kuma-cutover"]


def test_the_shadow_drill_rules_have_a_sibling_term_bounding_their_window_is_clean():
    [group] = yaml_fast.safe_load(RULES.read_text())["groups"]
    assert {r["alert"] for r in group["rules"]} >= set(DRILL_RULES)
    assert drill_window_problems(RULES.read_text(), CUTOVER.read_text()) == []


def test_a_cutover_that_drops_the_siblings_but_leaves_the_drills_shadow_is_flagged():
    cutover = CUTOVER.read_text()
    for tile in DRILL_WINDOW_SIBLINGS:
        assert f'monitor_name="{tile}"}}[2h]' in cutover, f"{tile} has no [2h] term"
        cutover = cutover.replace(f'monitor_name="{tile}"}}[2h]', "")
    assert drill_window_problems(RULES.read_text(), cutover) == [
        f"{', '.join(DRILL_RULES)} shadow with no B4 sibling term in kuma-cutover"
    ]
