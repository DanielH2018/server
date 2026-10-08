#!/usr/bin/env python3
"""Guards on the host-side secondary upsmon — the process that performs the real poweroff.

daniel-box draws from the UPS: `upsc` sampled against a modulated 16-core burn moves
ups.load by ~5 points ≈ 45 W, three transitions phase-locked across two cycles. Without a
secondary upsmon it has no orderly shutdown at all: no /etc/nut, no nut-monitor, no cron,
no HA automation with a power-off action.

Three properties are load-bearing here, and each one fails in a way that looks like success:

DISARMED BY DEFAULT. A secondary upsmon exists to power the machine off. Arming one on the
control-plane node without the console-attended drill risks an unplanned cluster shutdown, and
nothing about a wrong configuration is visible while mains power is up.

THE USB HALF STAYS PUT. The udev rule and the driver belong to the host the UPS is plugged into.
Rendering them elsewhere would be inert at best and misleading at worst.

THE ENDPOINT IS PROVEN, NOT ASSUMED. The nut Service does not pin its clusterIP, so a cross-node
secondary's endpoint is a deploy-time snapshot. An unreachable endpoint installs a shutdown chain
that never fires — indistinguishable from a working one until the power cut it exists for.

Run: uv run pytest ansible/tests/setup/test_nut_host_secondary.py
"""

from lib import yaml_fast
from _helpers import ANSIBLE
import json
import re

from _kuma_monitors import entity, monitors_text, tile_is_gated_on
from _setup_render import render_setup_text
from _shell_render import rendered_shell_text
from lib.proc_testing import run


ROLE = ANSIBLE / "roles" / "setup" / "nut_host"
TASKS = (ROLE / "tasks" / "main.yml").read_text()
GROUP_VARS = (ANSIBLE / "inventory" / "group_vars" / "all.yml").read_text()
SETUP = (ANSIBLE / "initial_setup.yml").read_text()
HOST_VARS = ANSIBLE / "inventory" / "host_vars"
NUT_DEFAULTS = ANSIBLE / "roles" / "k8s" / "nut" / "defaults" / "main.yml"

# Measured on daniel-server, `upsc apc-ups@127.0.0.1`: battery.runtime 987 at
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
    docs = list(yaml_fast.safe_load_all(TASKS))
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
    """A cross-node secondary cannot use the pod's loopback hostPort.

    Asserted by rendering the conf at an address the inventory does not hold. Matching
    `@{{ nut_host_upsd_host }}` in the source instead holds only while the endpoint is spelled
    that way, and a spelling the pattern misses leaves the guard matching nothing (#3202).
    """
    rendered = render_setup_text(
        "nut_host", "host-upsmon.conf.j2", {"nut_host_upsd_host": "198.51.100.7"}
    )
    monitor = [ln for ln in rendered.splitlines() if ln.startswith("MONITOR ")]
    assert len(monitor) == 1, f"expected one MONITOR line, got {monitor}"
    assert "@198.51.100.7 " in monitor[0], (
        "the MONITOR endpoint must come from nut_host_upsd_host: 127.0.0.1 is correct only on "
        f"the host whose node runs the nut pod. Rendered: {monitor[0].split()[1]!r}"
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
    docs = list(yaml_fast.safe_load_all(TASKS))
    tasks = [t for doc in docs if isinstance(doc, list) for t in doc]
    tagged = [t.get("name") for t in tasks if t.get("tags")]
    assert not tagged, (
        "tasks %r carry tags; the endpoint lookup, the set_fact that stores it and the "
        "upsmon.conf template must be selected or skipped as one unit" % tagged
    )


# ── The ONBATT timer, once any host beyond ups_host is armed ────────────────────────────────
#
# Arming a host makes nut_onbatt_shutdown_delay a load-bearing availability number rather than
# an agent node's private business, and it can fail in BOTH directions. Too short stops the
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
    ups_host = yaml_fast.safe_load(GROUP_VARS)["ups_host"]
    armed = []
    for path in sorted(HOST_VARS.glob("*.yml")):
        host = path.stem
        if host == ups_host:
            continue
        if (yaml_fast.safe_load(path.read_text()) or {}).get(
            "nut_host_secondary_armed"
        ):
            armed.append(host)
    return armed


def test_the_repos_onbatt_delay_suits_its_arming():
    """The live pairing of arm flags and timer must sit inside the band."""
    delay = yaml_fast.safe_load(NUT_DEFAULTS.read_text())["nut_onbatt_shutdown_delay"]
    reason = onbatt_delay_verdict(delay, bool(_armed_hosts_beyond_ups_host()))
    assert reason is None, reason


def test_a_delay_inside_the_band_is_clean():
    assert onbatt_delay_verdict(300, True) is None
    assert onbatt_delay_verdict(687, True) is None


def test_a_delay_below_the_blip_window_is_flagged():
    """The original short delay, which is what made arming a regression rather than a fix."""
    assert onbatt_delay_verdict(120, True) is not None


def test_a_delay_that_outlasts_the_battery_is_flagged():
    assert onbatt_delay_verdict(900, True) is not None


def test_the_band_binds_only_once_a_second_host_is_armed():
    """With ups_host alone armed, an agent node stopping early is its own business."""
    assert onbatt_delay_verdict(120, False) is None


# ── the runtime watchdog on the secondary's upsd link ────────────
#
# The `wait_for` above proves reachability once, at deploy. These guard the check that proves
# it every 10 minutes afterwards. Each asserts a property whose failure is invisible: a
# watchdog reading the wrong address, or one leaking the credential that sits on the line it
# reads, both look exactly like a working one.

WATCHDOG = rendered_shell_text("setup", "nut_host", "ups-secondary-health.sh.j2")
WATCHDOG_ENV = (ROLE / "templates" / "kuma-push.env.j2").read_text()
NUT_HOST_DEFAULTS = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())

# The two tiles and the variable each is gated on, one per watching host.
TILES = {
    "daniel-box": ("ups-secondary.json", "ups_secondary_push_token"),
    "daniel-server": (
        "ups-secondary-daniel-server.json",
        "ups_secondary_daniel_server_push_token",
    ),
}
# Two values no secret holds, so a render carrying one carries THAT host's value.
SENTINELS = {
    "ups_secondary_push_token": "ups-secondary-box-sentinel",
    "ups_secondary_daniel_server_push_token": "ups-secondary-server-sentinel",
}

# A MONITOR line in the shape host-upsmon.conf.j2 renders. The fifth field is a fake stand-in
# for the credential that sits there in the real file; it exists only to be searched for in the
# extractor's output.
_FAKE_CREDENTIAL = "not-a-real-value-xyz"
_MONITOR_LINE = f"MONITOR apc-ups@10.43.171.124 1 upsmon {_FAKE_CREDENTIAL} secondary"


def _endpoint_extractor() -> str:
    """The awk program the shipped script uses, taken from the render rather than retyped.

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

    return run(
        ["awk", _endpoint_extractor()], input=conf_text, check=True
    ).stdout.strip()


def test_the_extractor_returns_the_endpoint():
    assert _extract_endpoint(_MONITOR_LINE) == "apc-ups@10.43.171.124"


def test_the_extractor_never_prints_the_fifth_field():
    """The rejecting half, and the reason this is awk rather than grep.

    `grep MONITOR upsmon.conf` returns the whole line — including the credential in field 5 —
    into syslog and into any transcript that ran the check, so the credential needs rotating.
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
    """journald here is capped at MaxLevelStore=notice, so an info line never reaches Loki."""
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


def test_the_env_file_selects_the_token_by_host():
    """daniel-box and ups_host (daniel-server) must render DIFFERENT values.

    Two hosts pushing the same token would let either host's `up` satisfy Kuma's push
    deadline, masking the other's `down`. Read from two renders with a different sentinel
    behind each variable: the per-host SELECTION is the claim, and at inventory values both
    resolve to the same STUB, which cannot show it (#3202).
    """
    rendered = {
        host: render_setup_text(
            "nut_host",
            "kuma-push.env.j2",
            {"inventory_hostname": host, "ups_host": "daniel-server", **SENTINELS},
        )
        for host in TILES
    }
    box, server = (SENTINELS[v] for _, v in TILES.values())
    assert box in rendered["daniel-box"] and server not in rendered["daniel-box"], (
        f"daniel-box renders {rendered['daniel-box'].splitlines()[-1]!r}"
    )
    assert (
        server in rendered["daniel-server"] and box not in rendered["daniel-server"]
    ), (
        "the env template must select by host, or both hosts push one monitor; daniel-server "
        f"renders {rendered['daniel-server'].splitlines()[-1]!r}"
    )


def test_the_env_file_token_selection_is_mandatory():
    """A missed host selection must fail the deploy, not render an empty token.

    The script's own empty-token branch is a silent `logger …; exit 0`, so a token that
    resolves empty pushes nothing and logs nothing anyone watches — the failure has to happen
    here, at render time, instead.
    """
    assert "ups_secondary_push_token | mandatory" in WATCHDOG_ENV
    assert "ups_secondary_daniel_server_push_token | mandatory" in WATCHDOG_ENV


def test_the_tile_is_gated_on_its_token():
    """An ungated tile sits red from creation until the secret exists.

    `entity` fails when the tile is absent with its gate armed, `tile_is_gated_on` when it is
    present with the gate open. Together they pin each tile to THAT variable, which a substring
    match on the `{% if %}` line cannot do.
    """
    for host, (tile, gate_var) in TILES.items():
        assert entity(tile, gate_var)["name"] == f"UPS Secondary ({host})", (
            f"{tile} must be the tile named for {host}"
        )
        assert tile_is_gated_on(tile, gate_var), (
            f"{tile} renders with {gate_var} empty, so it sits red from creation"
        )


def test_the_tile_deadline_is_derived_from_the_cron_cadence():
    """A hardcoded interval survives a schedule change and grants the wrong grace.

    A 24h grace against a 23h gap clears the DOWN it was added to make sticky.
    """
    # Two renders at a cadence the inventory does not hold: the deadline must follow the
    # cadence variable, which a substring match on the variable's name cannot show.
    minutes = 7
    for tile, gate_var in TILES.values():
        armed = entity(tile, gate_var)
        assert (
            armed["interval"]
            == NUT_HOST_DEFAULTS["nut_host_watchdog_interval_minutes"] * 120
        )
        rerendered = re.search(
            rf"^  {re.escape(tile)}: \|\n\s+(\{{.*\}})$",
            monitors_text(
                {gate_var: "x", "nut_host_watchdog_interval_minutes": minutes}
            ),
            re.M,
        )
        assert rerendered, f"{tile} is not rendered at a {minutes}-minute cadence"
        moved = json.loads(rerendered.group(1))
        assert moved["interval"] == minutes * 120, (
            f"{tile}'s deadline does not follow nut_host_watchdog_interval_minutes — a hardcoded "
            "interval survives a schedule change and grants the wrong grace"
        )
    assert "*/{{ nut_host_watchdog_interval_minutes }}" in TASKS


def test_the_watchdog_is_armable_and_armed():
    assert NUT_HOST_DEFAULTS["nut_host_watchdog_armed"] is True
    assert NUT_HOST_DEFAULTS["nut_host_watchdog_interval_minutes"] == 10
    assert "nut_host_watchdog_armed | bool" in TASKS


def test_the_watchdog_does_not_page_on_battery_state():
    """check_ups already owns battery state; alerting on OB here double-pages one event."""
    assert "ups.status" in WATCHDOG
    assert '"OB"' not in WATCHDOG


def test_the_watchdog_keeps_upsc_stdout_and_stderr_apart():
    """`2>&1` on the upsc call puts its diagnostics inside the VALUE.

    upsc writes "Init SSL without certificate database" to stderr on every successful call
    against this endpoint. Folding the streams makes the pushed message multi-line and makes the
    empty-status branch unreachable, since the noise is never empty.
    """
    assert 'upsc "$ENDPOINT" ups.status 2>&1' not in WATCHDOG
    assert 'ups.status 2>"$UPSC_STDERR"' in WATCHDOG


def test_the_watchdog_collapses_newlines_out_of_its_message():
    """A newline in either half breaks the syslog line and the Kuma push message."""
    assert WATCHDOG.count("tr '\\n' ' '") >= 2
