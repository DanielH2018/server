"""daniel-box ships the same last three `initial_setup` subjects as the Pi and daniel-server (H7.32).

The components are host-neutral and `test_pi_initial_setup_part2.py` holds their content. These
tests hold what is specific to this host, the last one to move: the role keeps no task of the
three subjects for any host, the crons it stops writing here are retired and replaced at their
slot, the firmware update ships only where fwupd is, the scans read this host's own tokens, and
reconcile adopts the two SSH rules while it leaves every rule another role writes alone.

Run: uv run pytest hosts/lib/tests/test_box_initial_setup_part2.py
"""

import json
import re

import pytest
from test_pi_initial_setup_part1 import seeded_pi
from test_pi_initial_setup_part2 import (
    DAYS,
    LAN,
    TIMERS,
    _crons,
    _reboot,
    _subject_tasks,
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


def test_the_role_skips_the_three_subjects_on_every_host(context):
    subjects = set(_subject_tasks().values())
    assert subjects == {"fail2ban", "maintenance"}
    for subject in subjects | {"ufw"}:
        assert set(context["initial_setup_packaged_hosts"][subject]) == {
            "daniel-pi",
            "daniel-server",
            HOST,
        }, subject


def test_each_cron_this_host_stops_writing_is_retired_and_a_timer_runs_at_its_slot(
    package, manifest, context
):
    retired = {e["cron"]: e["user"] for e in manifest["legacy"] if "cron" in e}
    for name, cron in _crons(context).items():
        if name == "Clean unused Docker images":
            continue
        assert retired.get(name) == cron["user"], name
        timer = BOX_TIMERS[name]
        assert timer in manifest["enable_links"]
        [calendar] = re.findall(
            r"^OnCalendar=(.*)$",
            _text(package, f"/usr/lib/systemd/system/{timer}"),
            re.M,
        )
        day = "" if cron["weekday"] == "*" else f"{DAYS[int(cron['weekday'])]} "
        clock = f"{int(cron['hour']):02d}:{int(cron['minute']):02d}:00"
        assert calendar.removesuffix(" UTC") == f"{day}*-*-* {clock}", name
    assert "homelab-docker-prune.timer" not in manifest["enable_links"]


def test_the_firmware_update_runs_the_wrapper_and_ships_only_on_a_fwupd_host(package):
    unit = _text(package, "/usr/lib/systemd/system/homelab-firmware-update.service")
    assert "ExecStart=/usr/lib/homelab/host-maintenance firmware-update\n" in unit
    assert "SyslogIdentifier=fwupd-auto\n" in unit
    for other in ("daniel-pi", "daniel-server"):
        files = host_mode.render_host(other).files
        assert f"/usr/lib/systemd/system/{FIRMWARE}" not in files, other


def test_the_scans_read_this_hosts_own_tokens(package):
    for scan in ("rkhunter", "aide"):
        unit = _text(package, f"/usr/lib/systemd/system/homelab-{scan}-check.service")
        key = f"{scan}_daniel_box_push_token"
        assert f"LoadCredential={scan}_push_token:/run/credstore/{key}\n" in unit
        assert key in package.credentials
    assert "healthchecks_ping_key" in package.credentials


def test_the_k3s_health_crons_keep_their_ping_key_file(manifest):
    paths = {e["path"] for e in manifest["legacy"] if "path" in e}
    assert "/usr/local/bin/host-maintenance.sh" in paths
    assert not {p for p in paths if p.startswith("/etc/healthchecks")}


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
    builder = [
        [r["spec"], f"homelab:{r['id']}"]
        for r in manifest["ufw"]["rules"]
        if r["id"].startswith("apt-repo-")
    ]
    # The live rules in `ufw status` order (2026-10-31): setup/k3s's join rules and coredns-host
    # metrics allows carry no comment, and setup/deploy_ui labels its allows with its own comment.
    others = [
        [f"allow from {agent} to any port 6443 proto tcp", ""],
        [f"allow from {agent} to any port 8472 proto udp", ""],
        [f"allow from {agent} to any port 10250 proto tcp", ""],
        [f"allow from {agent} to any port 9100 proto tcp", ""],
        [f"allow from {agent} to any port 7946 proto tcp", ""],
        [f"allow from {agent} to any port 7946 proto udp", ""],
        ["allow from 10.42.0.0/16 to any port 9253 proto tcp", ""],
        [f"allow from {agent} to any port 9253 proto tcp", ""],
        ["allow from 10.42.0.0/16 to any port 8790 proto tcp", deploy_ui],
        [f"allow from {agent} to any port 8790 proto tcp", deploy_ui],
        ["allow from 192.0.2.20 to any port 8790 proto tcp", deploy_ui],
    ] + builder
    node.state["ufw"] = {
        "active": True,
        "rules": [[LAN.format(lan=lan), ""], ["limit 22/tcp", ""]] + others,
        "policy": {"incoming": "deny", "outgoing": "allow", "routed": "deny"},
        "fail": [],
    }

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert [tuple(r) for r in node.state["ufw"]["rules"]] == [
        (LAN.format(lan=lan), "homelab:keep:lan-ssh"),
        ("limit 22/tcp", "homelab:keep:ssh-limit"),
    ] + [tuple(r) for r in others]
    # A `ufw default` or an enable on an active k3s node reloads the ruleset under kube-router,
    # which wiped the before chains here on 2026-10-25.
    ufw = [c["argv"][1:] for c in node.calls("ufw")]
    assert not [a for a in ufw if a[:1] in (["--force"], ["default"], ["delete"])]
