"""The Pi's last three `initial_setup` subjects as packaged components (H7.30).

`fail2ban`, `integrity-scans`, `postfix`, `accounting`, `host-maintenance` and `ssh-firewall`, with
the Pi package's own ufw rules, replace initial_setup's fail2ban group, maintenance crons and ufw
tasks on daniel-pi. These tests hold what the move must keep: each cron the role once wrote is
retired and replaced by a timer, the scans read the Pi's own tokens, the weekly reboot refuses
after a failed validation, and reconcile adopts the Pi's Ansible-era firewall rules in place.

Run: uv run pytest hosts/lib/tests/test_pi_initial_setup_part2.py
"""

import json
import os
from pathlib import Path

import pytest
from test_pi_initial_setup_part1 import seeded_pi

from lib.estate import REPO_INVENTORY
from lib.proc_testing import fake_bin, run, write_exec
from lib.render_context import render_context
from renderer.renderer_lib import host as host_mode

# Each cron the role once installed on the Pi, and the packaged timer that replaces it. The Pi has
# no fwupd, so the role never installed "Weekly firmware update" there.
TIMERS = {
    "Weekly apt autoremove": "homelab-apt-autoremove.timer",
    "Weekly dpkg purge orphaned configs": "homelab-dpkg-purge.timer",
    "Weekly system restart": "homelab-reboot.timer",
    "Clean unused Docker images": "homelab-docker-prune.timer",
    "Weekly rkhunter malware scan": "homelab-rkhunter-check.timer",
}
# Retired with no replacement: the Pi's AIDE scan outgrew its memory and reset the board, so its
# package lists no aide-scan (#8084).
UNSCHEDULED = {"Weekly AIDE file integrity check": "homelab-aide-check.timer"}
LAN = "allow from {lan} to any port 22 proto tcp"


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-pi")


@pytest.fixture(scope="module")
def manifest(package) -> dict:
    return json.loads(package.files["/usr/lib/homelab/manifest.json"][0])


@pytest.fixture(scope="module")
def context() -> dict:
    return render_context(None, "daniel-pi", inventory=REPO_INVENTORY)


def _text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def test_each_cron_the_role_once_wrote_is_retired_and_a_timer_replaces_it(
    package, manifest
):
    retired = {e["cron"]: e["user"] for e in manifest["legacy"] if "cron" in e}
    # Every job sat in root's crontab: the live Pi's sys_user crontab holds no Ansible entry
    # (2026-10-30), and the prune's `ansible_facts.user_id` was root under the play's become.
    jobs = TIMERS | UNSCHEDULED
    assert {name: retired.get(name) for name in jobs} == dict.fromkeys(jobs, "root")
    for timer in TIMERS.values():
        assert timer in manifest["enable_links"]
        assert f"/usr/lib/systemd/system/{timer}" in package.files
    for timer in UNSCHEDULED.values():
        assert timer not in manifest["enable_links"]
        assert f"/usr/lib/systemd/system/{timer}" not in package.files


def test_the_package_ships_rkhunter_and_no_aide(package):
    """The package lists no aide-scan, so apt autoremove removes aide and aide-common."""
    assert "rkhunter" in package.depends
    assert {"aide", "aide-common"}.isdisjoint(package.depends)


def test_the_scan_sources_the_check_library_and_loads_no_credential(package):
    # M6 batch B9's Cutover-2 dropped the pushes and its token PR retired the keys.
    for scan in ("rkhunter",):
        script = _text(package, f"/usr/lib/homelab/{scan}-check")
        assert "source /usr/lib/homelab/homelab-check-lib.sh ||" in script
        assert "kuma-push-lib.sh" not in script
        assert "CREDENTIALS_DIRECTORY" not in script
        unit = _text(package, f"/usr/lib/systemd/system/homelab-{scan}-check.service")
        assert "LoadCredential" not in unit and "homelab-secrets" not in unit
        assert f"{scan}_daniel_pi_push_token" not in package.credentials
    assert "healthchecks_ping_key" in package.credentials
    # The Pi's main sshd_config sets no PermitRootLogin, so rkhunter must accept an absent line.
    assert "\nALLOW_SSH_ROOT_USER=unset\n" in _text(package, "/etc/rkhunter.conf.local")


def test_the_rkhunter_scan_downloads_no_data_files(package):
    # Debian's WEB_CMD="/bin/false" makes `rkhunter --update` fail on every run (#6964).
    unit = _text(package, "/usr/lib/systemd/system/homelab-rkhunter-check.service")
    assert "ExecStart=/usr/lib/homelab/rkhunter-check\n" in unit
    assert "--update" not in unit
    assert "WEB_CMD" not in _text(package, "/etc/rkhunter.conf.local")


def test_the_jail_postfix_and_sysstat_edits_restart_their_daemon(package):
    assert package.activations["/etc/fail2ban/jail.d/homelab.conf"] == (
        "try-restart:fail2ban.service"
    )
    assert "banaction = ufw" in _text(package, "/etc/fail2ban/jail.d/homelab.conf")
    assert package.activations["/etc/postfix/main.cf"] == "try-restart:postfix.service"
    assert package.activations["/etc/default/sysstat"] == "try-restart:sysstat.service"
    postfix = {e["line"] for e in package.lines if e["path"] == "/etc/postfix/main.cf"}
    assert "inet_interfaces = loopback-only" in postfix


def _reboot(package, tmp_path: Path, *, validate_failed: bool):
    root = tmp_path / "root"
    (root / "run/homelab").mkdir(parents=True)
    if validate_failed:
        (root / "run/homelab/validate-failed").touch()
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "healthchecks_ping_key").write_text("fixture-key")
    calls = tmp_path / "calls"
    stubs = fake_bin(
        tmp_path / "bin",
        curl=f'echo "curl $*" >> {calls}\n',
        shutdown=f'echo "shutdown $*" >> {calls}\n',
    )
    script = _text(package, "/usr/lib/homelab/homelab-reboot").replace(
        "/sbin/shutdown", "shutdown"
    )
    result = run(
        [str(write_exec(tmp_path / "homelab-reboot", script))],
        env={
            **os.environ,
            "HOMELAB_ROOT": str(root),
            "CREDENTIALS_DIRECTORY": str(creds),
        },
        stub_bin=stubs,
    )
    return result, calls.read_text().splitlines() if calls.exists() else []


def test_the_weekly_reboot_refuses_while_reconcile_validation_failed(package, tmp_path):
    result, calls = _reboot(package, tmp_path, validate_failed=True)

    assert result.returncode == 1
    assert calls == []


def test_the_weekly_reboot_pings_its_check_then_schedules_the_reboot(
    package, tmp_path, context
):
    result, calls = _reboot(package, tmp_path, validate_failed=False)

    assert result.returncode == 0, result.stderr
    delay = context["weekly_reboot_shutdown_delay_minutes"]
    assert calls == [
        "curl -fs -m 10 --retry 3 https://hc-ping.com/fixture-key/weekly-reboot-daniel-pi",
        f"shutdown -r +{delay}",
    ]


def test_reconcile_adopts_the_pis_ansible_era_rules_and_deletes_only_the_stale_allow(
    package, tmp_path, context
):
    node = seeded_pi(package, tmp_path)
    lan = context["lan_subnet"]
    metrics = f"allow from {lan} to any port {{}} proto tcp"
    # The Pi's rules as the Ansible tasks wrote them, plus the WireGuard allow added by hand.
    node.state["ufw"] = {
        "active": True,
        "rules": [
            [LAN.format(lan=lan), ""],
            ["limit 22/tcp", ""],
            [metrics.format(9100), "optimize_pi: node_exporter"],
            [metrics.format(9323), "docker_install: dockerd metrics"],
            [metrics.format(1338), "docker_install: containerd metrics"],
            ["allow 51820/udp", ""],
            ["allow 51822/udp", ""],
        ],
        "fail": [],
    }

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert [tuple(r) for r in node.state["ufw"]["rules"]] == [
        (LAN.format(lan=lan), "homelab:keep:lan-ssh"),
        ("limit 22/tcp", "homelab:keep:ssh-limit"),
        (metrics.format(9100), "homelab:node-exporter"),
        (metrics.format(9323), "homelab:dockerd-metrics"),
        (metrics.format(1338), "homelab:containerd-metrics"),
        ("allow 51822/udp", ""),
    ]
    assert not [c for c in node.calls("ufw") if c["argv"][1:2] == ["--force"]]
    # A job's service is neither enabled nor active, so installing it never runs it: the reboot
    # waits for its timer.
    calls = [c["argv"] for c in node.calls("systemctl")]
    for job in ("homelab-reboot", "homelab-aide-check", "homelab-docker-prune"):
        assert not [c for c in calls if f"{job}.service" in c and "start" in c[1]], job
    # The jail file changed, so fail2ban restarts, and only after its config test.
    calls = [c["argv"] for c in node.calls()]
    test = calls.index(["fail2ban-client", "-t"])
    assert ["systemctl", "try-restart", "fail2ban.service"] in calls[test:]
