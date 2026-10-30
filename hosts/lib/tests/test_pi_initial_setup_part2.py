"""The Pi's last three `initial_setup` subjects as packaged components (H7.30).

`fail2ban`, `integrity-scans`, `postfix`, `accounting`, `host-maintenance` and `ssh-firewall`, with
the Pi package's own ufw rules, replace initial_setup's fail2ban group, maintenance crons and ufw
tasks on daniel-pi. These tests hold what the move must keep: the role skips every task of the
three subjects on the Pi, each cron it stops writing is retired and replaced by a timer at the same
slot, the scans read the Pi's own tokens, the weekly reboot refuses after a failed validation, and
reconcile adopts the Pi's Ansible-era firewall rules in place.

Run: uv run pytest hosts/lib/tests/test_pi_initial_setup_part2.py
"""

import json
import os
import re
from pathlib import Path

import jinja2
import pytest
from test_pi_initial_setup_part1 import seeded_pi

from lib import yaml_fast
from lib.estate import REPO_INVENTORY
from lib.proc_testing import fake_bin, run, write_exec
from lib.render_context import render_context
from lib.repo_paths import ANSIBLE
from renderer.renderer_lib import host as host_mode

ROLE = ANSIBLE / "roles" / "setup" / "initial_setup"
# The block tags of each subject's tasks (the role's `CLAUDE.md`, *Granular tags*).
SUBJECT_TAGS = {
    "fail2ban": "fail2ban",
    "rkhunter": "fail2ban",
    "aide": "fail2ban",
    "postfix": "fail2ban",
    "accounting": "fail2ban",
    "apt-hygiene": "maintenance",
    "weekly-restart": "maintenance",
    "firmware": "maintenance",
    "prune": "maintenance",
    "firewall": "ufw",
}
# Each cron the subjects install, and the packaged timer that replaces it on the Pi. The Pi has no
# fwupd, so the role never installed "Weekly firmware update" there.
TIMERS = {
    "Weekly apt autoremove": "homelab-apt-autoremove.timer",
    "Weekly dpkg purge orphaned configs": "homelab-dpkg-purge.timer",
    "Weekly system restart": "homelab-reboot.timer",
    "Clean unused Docker images": "homelab-docker-prune.timer",
    "Weekly rkhunter malware scan": "homelab-rkhunter-check.timer",
    "Weekly AIDE file integrity check": "homelab-aide-check.timer",
}
NOT_ON_THE_PI = {"Weekly firmware update"}
DAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
LAN = "allow from {lan} to any port 22 proto tcp"


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-pi")


@pytest.fixture(scope="module")
def manifest(package) -> dict:
    return json.loads(package.files["/usr/lib/homelab/manifest.json"][0])


@pytest.fixture(scope="module")
def context() -> dict:
    return render_context(ROLE, "daniel-pi", inventory=REPO_INVENTORY)


def _text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def _tasks() -> list[dict]:
    """Every task of the role's task files, an import's own tasks included."""
    found = []
    for path in sorted((ROLE / "tasks").glob("*.yml")):
        found += [
            t
            for t in yaml_fast.safe_load(path.read_text()) or []
            if isinstance(t, dict)
        ]
    return found


def _whens(task: dict) -> list[str]:
    when = task.get("when", [])
    return [str(w) for w in (when if isinstance(when, list) else [when])]


def _subject_tasks() -> dict[str, str]:
    """Task name -> its subject, for every task carrying a subject's block tag.

    A task also tagged `sysctl` is part 1's, which the `sysctl` subject already guards.
    """
    found = {}
    for task in _tasks():
        tags = set(task.get("tags") or [])
        subjects = {SUBJECT_TAGS[t] for t in tags & SUBJECT_TAGS.keys()}
        if subjects and "sysctl" not in tags:
            [subject] = subjects
            found[str(task["name"])] = subject
    return found


def test_every_task_of_the_three_subjects_skips_the_pi(context):
    subjects = _subject_tasks()
    assert {
        "Deploy fail2ban jail configuration": "fail2ban",
        "Schedule weekly AIDE integrity check": "fail2ban",
        "Schedule sudo crontab for weekly restart": "maintenance",
        "Enable UFW firewall": "ufw",
    }.items() <= subjects.items()
    tasks = {str(t["name"]): t for t in _tasks()}
    unguarded = [
        name
        for name, subject in subjects.items()
        if f"inventory_hostname not in initial_setup_packaged_hosts['{subject}']"
        not in _whens(tasks[name])
    ]
    assert not unguarded
    for subject in set(subjects.values()):
        assert "daniel-pi" in context["initial_setup_packaged_hosts"][subject]


def _crons(context: dict) -> dict[str, dict]:
    """Cron name -> the cron task's fields rendered for the Pi, for each subject cron it adds."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    subjects = _subject_tasks()
    found = {}
    for task in _tasks():
        cron = task.get("ansible.builtin.cron")
        if task.get("name") in subjects and cron and cron.get("state") != "absent":
            found[cron["name"]] = {
                key: env.from_string(str(cron.get(key, "*"))).render(context)
                for key in ("minute", "hour", "weekday")
            } | {"user": cron["user"]}
    return found


def test_each_cron_the_pi_stops_writing_is_retired_and_a_timer_runs_at_its_slot(
    package, manifest, context
):
    crons = _crons(context)
    assert crons.keys() == TIMERS.keys() | NOT_ON_THE_PI
    retired = {e["cron"]: e["user"] for e in manifest["legacy"] if "cron" in e}
    for name, cron in crons.items():
        if name in NOT_ON_THE_PI:
            continue
        # The prune's `ansible_facts.user_id` is root, because initial_setup gathers facts with the
        # play's become (ansible/pre_tasks/load_secrets.yml); the live Pi's sys_user crontab holds
        # no Ansible entry (2026-10-30). So every job sat in root's crontab.
        user = "root" if "ansible_facts" in cron["user"] else cron["user"]
        assert retired.get(name) == user, name
        timer = TIMERS[name]
        assert timer in manifest["enable_links"]
        [calendar] = re.findall(
            r"^OnCalendar=(.*)$",
            _text(package, f"/usr/lib/systemd/system/{timer}"),
            re.M,
        )
        day = "" if cron["weekday"] == "*" else f"{DAYS[int(cron['weekday'])]} "
        clock = f"{int(cron['hour']):02d}:{int(cron['minute']):02d}:00"
        assert calendar.removesuffix(" UTC") == f"{day}*-*-* {clock}", name


def test_the_scan_packages_install_with_their_config_under_no_install_recommends(
    package,
):
    # The updater installs with --no-install-recommends, and aide only Recommends aide-common,
    # which ships aide.conf, the aide.conf.d directory the exclusions land in, and aideinit.
    assert {"rkhunter", "aide", "aide-common"} <= set(package.depends)


def test_the_scans_read_the_pis_own_tokens_and_the_packaged_libraries(package):
    for scan in ("rkhunter", "aide"):
        script = _text(package, f"/usr/lib/homelab/{scan}-check")
        assert "source /usr/lib/homelab/kuma-push-lib.sh ||" in script
        assert "source /usr/lib/homelab/homelab-check-lib.sh ||" in script
        assert f'"$CREDENTIALS_DIRECTORY/{scan}_push_token"' in script
        unit = _text(package, f"/usr/lib/systemd/system/homelab-{scan}-check.service")
        key = f"{scan}_daniel_pi_push_token"
        assert f"LoadCredential={scan}_push_token:/run/credstore/{key}\n" in unit
        assert key in package.credentials
    assert "healthchecks_ping_key" in package.credentials
    # The Pi's main sshd_config sets no PermitRootLogin, so rkhunter must accept an absent line.
    assert "\nALLOW_SSH_ROOT_USER=unset\n" in _text(package, "/etc/rkhunter.conf.local")


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
