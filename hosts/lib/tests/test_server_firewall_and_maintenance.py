"""daniel-server ships the same last three `initial_setup` subjects as the Pi (H7.31).

The components are host-neutral and `test_pi_firewall_and_maintenance.py` holds their content. These
tests hold what is specific to this host: the crons the role once wrote here are retired and
replaced by timers without the Pi's Docker prune, the scans
read this host's own tokens, and reconcile adopts the two SSH rules while it leaves the k3s
agent's rules and the hand-added rule alone. Reconcile also removes the `eval-run` textfile a test
run leaked here, which no daniel-server component writes (#6735).

Run: uv run pytest hosts/lib/tests/test_server_firewall_and_maintenance.py
"""

import json

import pytest
from _reconcile_arms import live_unit
from test_pi_sshd_apt_and_tuning import seeded_pi
from test_pi_firewall_and_maintenance import (
    LAN,
    TIMERS,
    _reboot,
    _text,
)

from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from renderer.renderer_lib import host as host_mode

HOST = "daniel-server"
# daniel-server has neither fwupd nor Docker, so neither job is packaged here.
NOT_HERE = {"Weekly firmware update", "Clean unused Docker images"}


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host(HOST)


@pytest.fixture(scope="module")
def manifest(package) -> dict:
    return json.loads(package.files["/usr/lib/homelab/manifest.json"][0])


@pytest.fixture(scope="module")
def context() -> dict:
    return render_context(None, HOST, inventory=REPO_INVENTORY)


def test_each_cron_the_role_once_wrote_here_is_retired_and_a_timer_replaces_it(
    package, manifest
):
    retired = {e["cron"]: e["user"] for e in manifest["legacy"] if "cron" in e}
    for name, timer in TIMERS.items():
        if name in NOT_HERE:
            continue
        assert retired.get(name) == "root", name
        assert timer in manifest["enable_links"]
        assert f"/usr/lib/systemd/system/{timer}" in package.files
    assert "homelab-docker-prune.timer" not in manifest["enable_links"]
    assert "/usr/lib/homelab/docker-prune" not in package.files


def test_reconcile_retires_the_compose_era_crowdsec_hub_cron(manifest):
    retired = {e["cron"]: e["user"] for e in manifest["legacy"] if "cron" in e}
    assert retired.get("Update Crowdsec Hub") == "root"


def _eval_run_writers(package: host_mode.Package) -> list[str]:
    return [
        dst
        for dst, (body, *_) in package.files.items()
        if b"write_check_textfile eval-run " in body
    ]


def test_reconcile_retires_the_leaked_eval_run_textfile(package, manifest):
    leaked = "/var/lib/node-exporter-textfile/eval-run.prom"
    assert {"path": leaked} in manifest["legacy"]
    # The entry deletes a live producer's file once anything on this host writes the check.
    # daniel-box ships the producer, so the pattern must still find it there.
    assert _eval_run_writers(host_mode.render_host("daniel-box")) == [
        "/usr/lib/homelab/eval-run"
    ]
    assert _eval_run_writers(package) == []


def test_the_scans_load_no_credential_and_the_package_lists_no_scan_token(package):
    # M6 batch B9's Cutover-2 dropped the pushes and its token PR retired the keys.
    for scan in ("rkhunter", "aide"):
        unit = _text(package, f"/usr/lib/systemd/system/homelab-{scan}-check.service")
        assert "LoadCredential" not in unit and "homelab-secrets" not in unit
        assert f"{scan}_daniel_server_push_token" not in package.credentials
    assert "healthchecks_ping_key" in package.credentials
    # The main sshd_config here carries PermitRootLogin, as the live rkhunter.conf.local reads.
    assert "\nALLOW_SSH_ROOT_USER=no\n" in _text(package, "/etc/rkhunter.conf.local")
    # The scans and the maintenance wrapper source the packaged textfile library.
    assert "/usr/lib/homelab/homelab-check-lib.sh" in package.files


def test_the_weekly_reboot_pings_this_hosts_check(package, tmp_path, context):
    result, calls = _reboot(package, tmp_path, validate_failed=False)

    assert result.returncode == 0, result.stderr
    delay = context["weekly_reboot_shutdown_delay_minutes"]
    assert calls == [
        f"curl -fs -m 10 --retry 3 https://hc-ping.com/fixture-key/weekly-reboot-{HOST}",
        f"shutdown -r +{delay}",
    ]


def test_reconcile_adopts_the_ssh_and_join_rules_and_leaves_the_hand_added_rule(
    package, tmp_path, context
):
    node = seeded_pi(package, tmp_path)
    node.state["host"] = HOST
    node.state["installed"][f"homelab-{HOST}"] = node.state["installed"].pop(
        "homelab-daniel-pi"
    )
    node.state["users"] = {"claude": 995}
    # H7.59's filter restarts rsyslog, which the Pi fixture masks.
    live_unit(node, "rsyslog.service")
    lan = context["lan_subnet"]
    server = "10.0.0.215"
    # The live rules in `ufw status` order (2026-10-31): the k3s agent's five join rules carry no
    # comment, and the 45876 allow from the pod network has no writer in the repo.
    others = [
        ["allow from 10.42.0.0/16 to any port 45876", ""],
        [f"allow from {server} to any port 8472 proto udp", ""],
        [f"allow from {server} to any port 10250 proto tcp", ""],
        [f"allow from {server} to any port 9100 proto tcp", ""],
        [f"allow from {server} to any port 7946 proto tcp", ""],
        [f"allow from {server} to any port 7946 proto udp", ""],
    ]
    node.state["ufw"] = {
        "active": True,
        "rules": [[LAN.format(lan=lan), ""], others[0], ["limit 22/tcp", ""]]
        + others[1:],
        # The live defaults already match, so the arm must not reload the ruleset on a k3s node.
        "policy": {"incoming": "deny", "outgoing": "allow", "routed": "deny"},
        "fail": [],
    }

    for objects in package.libvirt.values():
        for declared in objects:
            xml = node.root / declared["path"].lstrip("/")
            xml.parent.mkdir(parents=True, exist_ok=True)
            xml.write_bytes(package.files[declared["path"]][0])

    result = node.run()

    assert result.returncode == 0, result.stderr
    join = [
        "k3s-join-vxlan",
        "k3s-join-kubelet",
        "k3s-join-node-exporter",
        "k3s-join-memberlist-tcp",
        "k3s-join-memberlist-udp",
    ]
    assert [tuple(r) for r in node.state["ufw"]["rules"]] == [
        (LAN.format(lan=lan), "homelab:keep:lan-ssh"),
        tuple(others[0]),
        ("limit 22/tcp", "homelab:keep:ssh-limit"),
    ] + [
        (rule[0], f"homelab:keep:{name}")
        for rule, name in zip(others[1:], join, strict=True)
    ]
    ufw = [c["argv"][1:] for c in node.calls("ufw")]
    assert not [a for a in ufw if a[:1] in (["--force"], ["default"], ["delete"])]
