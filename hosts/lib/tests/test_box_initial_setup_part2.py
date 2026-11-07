"""daniel-box ships the same last three `initial_setup` subjects as the Pi and daniel-server (H7.32).

The components are host-neutral and `test_pi_initial_setup_part2.py` holds their content. These
tests hold what is specific to this host, the last one to move: the
crons the role once wrote here are retired and replaced by timers, the firmware update ships
only where fwupd is, the scans read this host's own tokens, and
reconcile adopts the two SSH rules while it leaves every rule another role writes alone.

Run: uv run pytest hosts/lib/tests/test_box_initial_setup_part2.py
"""

import json

import pytest
from test_pi_initial_setup_part1 import seeded_pi
from test_pi_initial_setup_part2 import (
    LAN,
    TIMERS,
    _reboot,
    _text,
)

from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import ANSIBLE
from renderer.renderer_lib import host as host_mode

HOST = "daniel-box"
ROLE = ANSIBLE / "roles" / "setup" / "initial_setup"
FIRMWARE = "homelab-firmware-update.timer"
# daniel-box has fwupd, so its firmware cron becomes a timer here and nowhere else.
BOX_TIMERS = TIMERS | {"Weekly firmware update": FIRMWARE}


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host(HOST)


@pytest.fixture(scope="module")
def manifest(package) -> dict:
    return json.loads(package.files["/usr/lib/homelab/manifest.json"][0])


@pytest.fixture(scope="module")
def context() -> dict:
    return render_context(ROLE, HOST, inventory=REPO_INVENTORY)


def test_each_cron_the_role_once_wrote_here_is_retired_and_a_timer_replaces_it(
    package, manifest
):
    retired = {e["cron"]: e["user"] for e in manifest["legacy"] if "cron" in e}
    for name, timer in BOX_TIMERS.items():
        if name == "Clean unused Docker images":
            continue
        assert retired.get(name) == "root", name
        assert timer in manifest["enable_links"]
        assert f"/usr/lib/systemd/system/{timer}" in package.files
    assert "homelab-docker-prune.timer" not in manifest["enable_links"]


def test_the_firmware_update_runs_the_wrapper_and_ships_only_on_a_fwupd_host(package):
    unit = _text(package, "/usr/lib/systemd/system/homelab-firmware-update.service")
    assert "ExecStart=/usr/lib/homelab/host-maintenance firmware-update\n" in unit
    assert "SyslogIdentifier=fwupd-auto\n" in unit
    for other in ("daniel-pi", "daniel-server"):
        files = host_mode.render_host(other).files
        assert f"/usr/lib/systemd/system/{FIRMWARE}" not in files, other


def test_the_scans_load_no_credential_and_the_package_lists_no_scan_token(package):
    # M6 batch B9's Cutover-2 dropped the pushes and its token PR retired the keys.
    for scan in ("rkhunter", "aide"):
        unit = _text(package, f"/usr/lib/systemd/system/homelab-{scan}-check.service")
        assert "LoadCredential" not in unit and "homelab-secrets" not in unit
        assert f"{scan}_daniel_box_push_token" not in package.credentials
    assert "healthchecks_ping_key" in package.credentials


def test_the_maintenance_wrapper_retires(manifest):
    paths = {e["path"] for e in manifest["legacy"] if "path" in e}
    assert "/usr/local/bin/host-maintenance.sh" in paths


@pytest.mark.parametrize(
    ("host", "firmware", "key_file"),
    [
        ("daniel-box", True, False),
        ("daniel-server", False, True),
        ("daniel-pi", False, True),
    ],
)
def test_the_wrapper_and_key_file_stay_with_the_crons_that_use_them(
    host, firmware, key_file
):
    handoff = next(
        group
        for group in host_mode.render_host(host).handoffs
        if {"cron": "Weekly system restart", "user": "root"} in group["legacy"]
    )
    assert "homelab-reboot.timer" in handoff["units"]
    assert {"path": "/usr/local/bin/host-maintenance.sh"} in handoff["legacy"]
    assert (
        {"cron": "Weekly firmware update", "user": "root"} in handoff["legacy"]
    ) == firmware
    assert ({"path": "/etc/healthchecks"} in handoff["legacy"]) == key_file


def test_the_weekly_reboot_pings_this_hosts_check(package, tmp_path, context):
    result, calls = _reboot(package, tmp_path, validate_failed=False)

    assert result.returncode == 0, result.stderr
    delay = context["weekly_reboot_shutdown_delay_minutes"]
    assert calls == [
        f"curl -fs -m 10 --retry 3 https://hc-ping.com/fixture-key/weekly-reboot-{HOST}",
        f"shutdown -r +{delay}",
    ]


def test_reconcile_adopts_the_ssh_rules_and_leaves_every_other_roles_rules(
    package, manifest, tmp_path, context
):
    node = seeded_pi(package, tmp_path)
    node.state["host"] = HOST
    node.state["installed"][f"homelab-{HOST}"] = node.state["installed"].pop(
        "homelab-daniel-pi"
    )
    node.state["users"] = {u["name"]: u["uid"] for u in manifest["users"]["users"]}
    lan = context["lan_subnet"]
    agent = "192.0.2.10"
    deploy_ui = "deploy-ui from the k3s cluster"
    k3s = [
        r
        for r in manifest["ufw"]["rules"]
        if r["id"].startswith(("k3s-join-", "coredns-host-"))
    ]
    builder = [
        [r["spec"], f"homelab:{r['id']}"]
        for r in manifest["ufw"]["rules"]
        if r["id"].startswith("apt-repo-")
    ]
    # The live rules in `ufw status` order: the k3s join rules and coredns-host metrics allows
    # carry no comment, and setup/deploy_ui labels its allows with its own comment.
    others = [
        ["allow from 10.42.0.0/16 to any port 8790 proto tcp", deploy_ui],
        [f"allow from {agent} to any port 8790 proto tcp", deploy_ui],
        ["allow from 192.0.2.20 to any port 8790 proto tcp", deploy_ui],
    ] + builder
    node.state["ufw"] = {
        "active": True,
        "rules": [[LAN.format(lan=lan), ""], ["limit 22/tcp", ""]]
        + [[r["spec"], ""] for r in k3s]
        + others,
        "policy": {"incoming": "deny", "outgoing": "allow", "routed": "deny"},
        "fail": [],
    }

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert [tuple(r) for r in node.state["ufw"]["rules"]] == [
        (LAN.format(lan=lan), "homelab:keep:lan-ssh"),
        ("limit 22/tcp", "homelab:keep:ssh-limit"),
    ] + [
        (
            r["spec"],
            f"homelab:keep:{r['id']}" if r.get("keep") else f"homelab:{r['id']}",
        )
        for r in k3s
    ] + [tuple(r) for r in others]
    # A `ufw default` or an enable on an active k3s node reloads the ruleset under kube-router,
    # which wiped the before chains here on 2026-10-25.
    ufw = [c["argv"][1:] for c in node.calls("ufw")]
    assert not [a for a in ufw if a[:1] in (["--force"], ["default"], ["delete"])]
