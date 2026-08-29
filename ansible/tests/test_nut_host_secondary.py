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
HOST_VARS = ANSIBLE / "inventory" / "host_vars"
NUT_DEFAULTS = ANSIBLE / "roles" / "k8s" / "nut" / "defaults" / "main.yml"

# Measured on daniel-server 2026-08-28, `upsc apc-ups@127.0.0.1`: battery.runtime 987 at
# battery.charge 100 and ups.load 43. A floor, not a guarantee — it falls with battery age and
# with load, which is why the ceiling below reserves a large slice of it.
MEASURED_RUNTIME_S = 987
# Seconds the ONBATT timer must leave for every armed host to finish powering off. Deliberately
# generous: it also covers the runtime this UPS will not have in two years.
SHUTDOWN_RESERVE_S = 300
# Seconds an armed control-plane node must ride out before stopping. Residential outages are
# mostly shorter than this, and stopping inside that window trades a clean shutdown for a full
# cluster restart on every flicker — a worse deal than the hard cut arming was meant to fix.
BLIP_RIDE_OUT_S = 300


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


# ── The ONBATT timer, once any host beyond ups_host is armed ────────────────────────────────
#
# Arming a host makes nut_onbatt_shutdown_delay a load-bearing availability number rather than
# an agent node's private business, and it can now fail in BOTH directions. Too short stops the
# control plane during a blip the battery would have carried; too long spends the runtime the
# poweroffs themselves need. Neither shows up anywhere until a real outage, and a green deploy
# looks identical either way — so the band is asserted here instead.


def onbatt_delay_verdict(delay: int, armed_beyond_ups_host: bool) -> str | None:
    """Return why `delay` is wrong for this arming, or None if it is inside the band.

    Pure so both arms can be exercised: the repo's real values are checked below, and the
    rejecting inputs prove the check can still go red.
    """
    if not armed_beyond_ups_host:
        return None
    if delay < BLIP_RIDE_OUT_S:
        return (
            "delay %ds stops an armed control-plane node inside the %ds blip window; most "
            "outages end sooner, so this trades a clean stop for a full cluster restart"
            % (delay, BLIP_RIDE_OUT_S)
        )
    ceiling = MEASURED_RUNTIME_S - SHUTDOWN_RESERVE_S
    if delay > ceiling:
        return (
            "delay %ds leaves under the %ds reserve against %ds of measured runtime; the "
            "poweroffs would race the battery, and LOWBATT would become the real trigger"
            % (delay, SHUTDOWN_RESERVE_S, MEASURED_RUNTIME_S)
        )
    return None


def _armed_hosts_beyond_ups_host() -> list[str]:
    ups_host = yaml.safe_load(GROUP_VARS)["ups_host"]
    armed = []
    for path in sorted(HOST_VARS.glob("*.yml")):
        host = path.stem
        if host == ups_host:
            continue
        if (yaml.safe_load(path.read_text()) or {}).get("nut_host_secondary_armed"):
            armed.append(host)
    return armed


def test_the_repos_onbatt_delay_suits_its_arming():
    """The live pairing of arm flags and timer must sit inside the band."""
    delay = yaml.safe_load(NUT_DEFAULTS.read_text())["nut_onbatt_shutdown_delay"]
    reason = onbatt_delay_verdict(delay, bool(_armed_hosts_beyond_ups_host()))
    assert reason is None, reason


def test_a_delay_inside_the_band_is_clean():
    assert onbatt_delay_verdict(300, True) is None
    assert onbatt_delay_verdict(687, True) is None


def test_a_delay_below_the_blip_window_is_flagged():
    """The pre-2026-08-28 value, which is what made arming a regression rather than a fix."""
    assert onbatt_delay_verdict(120, True) is not None


def test_a_delay_that_outlasts_the_battery_is_flagged():
    assert onbatt_delay_verdict(900, True) is not None


def test_the_band_binds_only_once_a_second_host_is_armed():
    """With ups_host alone armed, an agent node stopping early is its own business."""
    assert onbatt_delay_verdict(120, False) is None


# ── the runtime watchdog on the secondary's upsd link (2026-08-29 review M-8) ────────────
#
# The `wait_for` above proves reachability once, at deploy. These guard the check that proves
# it every 10 minutes afterwards. Each asserts a property whose failure is invisible: a
# watchdog reading the wrong address, or one leaking the credential that sits on the line it
# reads, both look exactly like a working one.

WATCHDOG = (ROLE / "templates" / "ups-secondary-health.sh.j2").read_text()
WATCHDOG_ENV = (ROLE / "templates" / "kuma-push.env.j2").read_text()
STATIC_MONITORS = (
    ANSIBLE / "roles" / "k8s" / "uptime-kuma" / "templates" / "static-monitors.yaml.j2"
).read_text()
NUT_HOST_DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())

# A MONITOR line in the shape host-upsmon.conf.j2 renders. The fifth field is a fake stand-in
# for the credential that sits there in the real file; it exists only to be searched for in the
# extractor's output.
_FAKE_CREDENTIAL = "not-a-real-value-xyz"
_MONITOR_LINE = f"MONITOR apc-ups@10.43.171.124 1 upsmon {_FAKE_CREDENTIAL} secondary"


def _endpoint_extractor() -> str:
    """The awk program the shipped script uses, taken from the template rather than retyped.

    Retyping it would guard a copy: the script could switch to a bare grep and these tests
    would keep passing against the awk they still held.
    """
    import re

    match = re.search(r"ENDPOINT=\"\$\(awk '([^']+)'", WATCHDOG)
    assert match, (
        "no awk endpoint extraction found in the watchdog — did it change shape?"
    )
    return match.group(1)


def _extract_endpoint(conf_text: str) -> str:
    import subprocess

    return subprocess.run(
        ["awk", _endpoint_extractor()],
        input=conf_text,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_the_extractor_returns_the_endpoint():
    assert _extract_endpoint(_MONITOR_LINE) == "apc-ups@10.43.171.124"


def test_the_extractor_never_prints_the_fifth_field():
    """The rejecting half, and the reason this is awk rather than grep.

    `grep MONITOR upsmon.conf` returns the whole line — including the credential in field 5 —
    into syslog and into any transcript that ran the check. This estate has rotated three
    secrets after exactly that mistake.
    """
    assert _FAKE_CREDENTIAL not in _extract_endpoint(_MONITOR_LINE)


def test_the_extractor_is_empty_when_no_monitor_line_exists():
    """Drives the script's `no MONITOR line` DOWN branch — an empty result must not read clean."""
    assert _extract_endpoint("MINSUPPLIES 1\nPOLLFREQ 5\n") == ""


def test_the_watchdog_reads_the_deployed_conf_not_an_ansible_var():
    """A re-templated var would resolve fresh at render time.

    A tag-scoped run that touched only this cron would then bake a CURRENT ClusterIP into the
    check while upsmon kept using the stale one, so the check would pass against an address
    upsmon does not use — reproducing the blindness it exists to remove.
    """
    assert "/etc/nut/upsmon.conf" in WATCHDOG
    assert "nut_host_upsd_host" not in WATCHDOG


def test_the_watchdog_checks_both_the_unit_and_the_link():
    """Either alone is a half-check: a live nut-monitor talking to nothing still never fires."""
    assert "systemctl is-active nut-monitor" in WATCHDOG
    assert "upsc" in WATCHDOG


def test_the_watchdog_logs_above_the_journald_store_cap():
    """journald here is capped at MaxLevelStore=notice, so an info line never reaches Loki.

    That was 2026-08-29 review M-15, found in live_drift_check.py. Pinned here so this check
    cannot ship the same bug.
    """
    assert "daemon.notice" in WATCHDOG
    assert "daemon.info" not in WATCHDOG


def test_the_watchdog_renders_its_own_env_file():
    """/etc/rancher/k3s/kuma-push.env is a whole-content template owned by roles/setup/k3s.

    Adding a key to it from this role would be clobbered on that role's next run, leaving the
    cron with no token — which reads as the monitor going silent, not as a broken deploy.
    """
    # Asserted on the `source` line, not on any mention: the script's own comment names the
    # k3s file to explain why it is not used, and a bare substring check would trip on that.
    assert ". /etc/nut/kuma-push.env" in WATCHDOG
    assert ". /etc/rancher/" not in WATCHDOG
    assert "ups_secondary_push_token" in WATCHDOG_ENV


def test_the_tile_is_gated_on_its_token():
    """An ungated tile sits red from creation until the secret exists."""
    assert "{% if ups_secondary_push_token | default('') %}" in STATIC_MONITORS


def test_the_tile_deadline_is_derived_from_the_cron_cadence():
    """A hardcoded interval survives a schedule change and grants the wrong grace.

    A 24h grace against a 23h gap once cleared the DOWN it was added to make sticky.
    """
    assert "nut_host_watchdog_interval_minutes" in STATIC_MONITORS
    assert "*/{{ nut_host_watchdog_interval_minutes }}" in TASKS


def test_the_watchdog_is_armable_and_armed():
    assert NUT_HOST_DEFAULTS["nut_host_watchdog_armed"] is True
    assert NUT_HOST_DEFAULTS["nut_host_watchdog_interval_minutes"] == 10
    assert "nut_host_watchdog_armed | bool" in TASKS


def test_the_watchdog_does_not_page_on_battery_state():
    """check_ups already owns battery state; alerting on OB here double-pages one event."""
    assert "ups.status" in WATCHDOG
    assert '"OB"' not in WATCHDOG
