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

THE HOST HALF IS PACKAGE-OWNED. Since H4.3c the homelab-daniel-server package installs the udev
rule, and since H7.33 both cluster nodes' packages install the secondary upsmon. The role is one
unconditional `end_role`, so one path never has two writers.

THE ENDPOINT IS PINNED. The nut Service pins its clusterIP, and daniel-box's package renders that
literal address. An address nothing re-resolves would install a shutdown chain that never fires,
indistinguishable from a working one until the power cut it exists for.

Run: uv run pytest ansible/tests/setup/test_nut_host_secondary.py
"""

from lib import yaml_fast
from _helpers import ALL_VARS, ANSIBLE, HOST_VARS
import json
import re

from _kuma_monitors import entity, monitors_text, tile_is_gated_on
from _shell_render import rendered_shell_text
from deploy_tools.setup_lib import setup_gates
from lib.proc_testing import run
from lib.repo_paths import REPO


ROLE = ANSIBLE / "roles" / "setup" / "nut_host"
TASKS = (ROLE / "tasks" / "main.yml").read_text()
ALL_VARS_TEXT = ALL_VARS.read_text()
SETUP = (ANSIBLE / "initial_setup.yml").read_text()
NUT_DEFAULTS = ANSIBLE / "roles" / "k8s" / "nut" / "defaults" / "main.yml"
NUT_SERVICE = ANSIBLE / "roles" / "k8s" / "nut" / "templates" / "service.yaml.j2"
NUT_COMPONENT = REPO / "hosts" / "components" / "nut-secondary" / "component.yml"
NUT_TIMER = NUT_COMPONENT.parent / "units" / "homelab-ups-watchdog.timer.j2"

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
    assert "nut_host_secondary_armed: false" in ALL_VARS_TEXT, (
        "the cross-node secondary must default to disarmed — a wrong SHUTDOWNCMD or an "
        "unreachable upsd looks exactly like a working one until mains power fails"
    )


def test_the_play_gate_reads_the_arm_flag():
    """ups_host runs unconditionally; any other host needs the flag."""
    assert "nut_host_secondary_armed | bool" in SETUP, (
        "initial_setup.yml must gate the non-ups_host case on the arm flag, or deploying the "
        "role installs a shutdown chain on every host that runs the play"
    )


def test_the_role_is_one_unconditional_end_role():
    """H7.33 emptied the role: both cluster nodes' secondary is package-owned.

    A guard that names a host would let the tick write the old files again on that host, and a
    second task would give the role work the packages do not own.
    """
    assert setup_gates.role_end_guard(ROLE) == "True"
    [task] = yaml_fast.safe_load(TASKS)
    assert task["ansible.builtin.meta"] == "end_role"


def daniel_box_package(tmp_path):
    """daniel-box's package rendered against a stand-in ciphertext file.

    The real `ansible/vars/hosts/daniel-box.sops.yaml` is operator-written, so the render
    reads a placeholder here; only the rendered templates are under test.
    """
    from renderer.renderer_lib import host as host_mode

    (tmp_path / "daniel-box.sops.yaml").write_text("placeholder: ENC[AES256_GCM,x]\n")
    return host_mode.render_host("daniel-box", sops_dir=tmp_path)


def test_daniel_boxs_package_names_the_pinned_clusterip(tmp_path):
    """A cross-node secondary cannot use the pod's loopback hostPort; it names the pinned Service.

    The pin sits in two files, the nut role's defaults and the component's defaults, because
    the host render cannot read a k8s role. This pins them together, and pins the rendered
    MONITOR line to the value, so a spelling the extractor misses cannot empty the guard.
    """
    pin = yaml_fast.safe_load(NUT_DEFAULTS.read_text())["nut_k8s_cluster_ip"]
    component = yaml_fast.safe_load(NUT_COMPONENT.read_text())
    assert component["defaults"]["nut_host_upsd_clusterip"] == pin
    assert "clusterIP: {{ nut_k8s_cluster_ip }}" in NUT_SERVICE.read_text()
    package = daniel_box_package(tmp_path)
    template = "/usr/lib/homelab/secret-templates/etc/nut/upsmon.conf"
    monitor = [
        ln
        for ln in package.files[template][0].decode().splitlines()
        if ln.startswith("MONITOR ")
    ]
    assert len(monitor) == 1, f"expected one MONITOR line, got {monitor}"
    assert monitor[0].split()[1] == f"apc-ups@{pin}"


def test_daniel_boxs_watchdog_unit_loads_its_own_tiles_token(tmp_path):
    """The package must list the credential the unit loads, and it must not be daniel-server's."""
    package = daniel_box_package(tmp_path)
    unit = package.files["/usr/lib/systemd/system/homelab-ups-watchdog.service"][0]
    assert b"/run/credstore/ups_secondary_push_token" in unit
    assert b"ups_secondary_daniel_server_push_token" not in unit
    assert "ups_secondary_push_token" in package.credentials


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
    ups_host = yaml_fast.safe_load(ALL_VARS_TEXT)["ups_host"]
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
NUT_HOST_DEFAULTS = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())

# The two tiles and the variable each is gated on, one per watching host.
TILES = {
    "daniel-box": ("ups-secondary.json", "ups_secondary_push_token"),
    "daniel-server": (
        "ups-secondary-daniel-server.json",
        "ups_secondary_daniel_server_push_token",
    ),
}


def test_the_packaged_token_key_names_each_hosts_own_tile_token():
    """The packaged watchdog unit loads this key (H4.3b3); the wrong host's key reads another tile."""
    from lib.render_context import render_context

    for host, (_, token) in TILES.items():
        ctx = render_context(ROLE, host, overrides={"inventory_hostname": host})
        assert ctx["nut_host_watchdog_token_key"] == token, host


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
    assert "*:0/{{ nut_host_watchdog_interval_minutes }}" in NUT_TIMER.read_text()


def test_the_watchdog_is_armable_and_armed():
    assert NUT_HOST_DEFAULTS["nut_host_watchdog_armed"] is True
    assert NUT_HOST_DEFAULTS["nut_host_watchdog_interval_minutes"] == 10
    enable = yaml_fast.safe_load(NUT_COMPONENT.read_text())["enable"]
    assert enable["homelab-ups-watchdog.timer"]["when"] == "nut_host_watchdog_armed"


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
