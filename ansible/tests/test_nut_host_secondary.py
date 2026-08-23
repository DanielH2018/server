#!/usr/bin/env python3
"""Guards on the host-side secondary upsmon — the process that performs the real poweroff.

Until 2026-08-20 this role ran on `ups_host` alone, so daniel-box had no orderly shutdown at all:
no /etc/nut, no nut-monitor, no cron, no HA automation with a power-off action. Whether that
mattered turned on a question the repo had never recorded — does daniel-box draw from that UPS —
and it does: `upsc` sampled against a modulated 16-core burn moved ups.load by ~5 points ≈ 45 W,
three transitions phase-locked across two cycles.

Three properties are load-bearing here, and each one fails in a way that looks like success:

DISARMED BY DEFAULT. A secondary upsmon exists to power the machine off. Arming one on the
control-plane node without the console-attended drill risks an unplanned cluster shutdown, and
nothing about a wrong configuration is visible while mains power is up.

THE USB HALF STAYS PUT. The udev rule and the driver belong to the host the UPS is plugged into.
Rendering them elsewhere would be inert at best and misleading at worst.

THE ENDPOINT IS PROVEN, NOT ASSUMED. The nut Service does not pin its clusterIP, so a cross-node
secondary's endpoint is a deploy-time snapshot. An unreachable endpoint installs a shutdown chain
that never fires — indistinguishable from a working one until the power cut it exists for.

Run: uv run pytest ansible/tests/test_nut_host_secondary.py
"""

import yaml
from _helpers import ANSIBLE


ROLE = ANSIBLE / "roles" / "nut_host"
TASKS = (ROLE / "tasks" / "main.yml").read_text()
UPSMON = (ROLE / "templates" / "host-upsmon.conf.j2").read_text()
GROUP_VARS = (ANSIBLE / "inventory" / "group_vars" / "all.yml").read_text()
SETUP = (ANSIBLE / "initial_setup.yml").read_text()


def test_secondary_is_disarmed_by_default():
    """Arming powers a machine off; it must be an explicit, per-host decision."""
    assert "nut_host_secondary_armed: false" in GROUP_VARS, (
        "the cross-node secondary must default to disarmed — a wrong SHUTDOWNCMD or an "
        "unreachable upsd looks exactly like a working one until mains power fails"
    )


def test_the_play_gate_reads_the_arm_flag():
    """ups_host runs unconditionally; any other host needs the flag."""
    assert "nut_host_secondary_armed | bool" in SETUP, (
        "initial_setup.yml must gate the non-ups_host case on the arm flag, or deploying the "
        "role installs a shutdown chain on every host that runs the play"
    )


def test_the_usb_half_is_confined_to_the_ups_host():
    """udev rules and the driver belong to the host the UPS is physically attached to."""
    docs = list(yaml.safe_load_all(TASKS))
    tasks = [t for doc in docs if isinstance(doc, list) for t in doc]
    usb = [
        t
        for t in tasks
        if "udev" in (t.get("name") or "").lower() or "USB" in (t.get("name") or "")
    ]
    assert usb, "expected the udev tasks to still exist"
    for task in usb:
        assert "ups_host" in str(task.get("when", "")), (
            "%r must be gated on ups_host — the USB half is meaningless on a host with no "
            "UPS attached" % task.get("name")
        )


def test_the_endpoint_is_templated_not_hardcoded_to_loopback():
    """A cross-node secondary cannot use the pod's loopback hostPort."""
    assert "@{{ nut_host_upsd_host }}" in UPSMON, (
        "the MONITOR endpoint must come from a variable: 127.0.0.1 is correct only on the "
        "host whose node runs the nut pod"
    )


def test_reachability_is_asserted_before_the_chain_is_installed():
    """An unreachable endpoint must fail the deploy, not install a dead shutdown chain."""
    assert "ansible.builtin.wait_for" in TASKS and "port: 3493" in TASKS, (
        "the cross-node path must prove upsd is reachable before rendering upsmon.conf — the "
        "clusterIP is a deploy-time snapshot and nothing else would notice it going stale"
    )


def test_the_reachability_failure_names_both_causes():
    """An empty lookup and an unroutable IP need different fixes, so the message must separate them."""
    for phrase in ("Service was not found", "flannel.1"):
        assert phrase in TASKS, (
            "the failure message must distinguish 'no Service' from 'not routable / not "
            "admitted by the NetworkPolicy' — they have different remedies"
        )


def test_no_task_is_tagged_so_the_lookup_cannot_be_split_from_its_consumer():
    """A tag here can only separate the endpoint resolution from the template that reads it.

    `nut_host` runs from initial_setup.yml alone, which has no config/deploy tag split. Tagging
    the ClusterIP lookup while the set_fact and the template stay untagged means a --skip-tags run
    renders `MONITOR <ups>@` from the `| default('')`, and skips the reachability assert that
    would have caught it — a dead shutdown chain written with no error.
    """
    docs = list(yaml.safe_load_all(TASKS))
    tasks = [t for doc in docs if isinstance(doc, list) for t in doc]
    tagged = [t.get("name") for t in tasks if t.get("tags")]
    assert not tagged, (
        "tasks %r carry tags; the endpoint lookup, the set_fact that stores it and the "
        "upsmon.conf template must be selected or skipped as one unit" % tagged
    )
