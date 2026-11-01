"""daniel-server ships the same last three `initial_setup` subjects as the Pi (H7.31).

The components are host-neutral and `test_pi_initial_setup_part2.py` holds their content. These
tests hold what is specific to this host: the role skips the three subjects here, the crons it
stops writing are retired and replaced at their slot without the Pi's Docker prune, the scans
read this host's own tokens, and reconcile adopts the two SSH rules while it leaves the k3s
agent's rules and the hand-added rule alone. Reconcile also removes the `eval-run` textfile a test
run leaked here, which no daniel-server component writes (#6735).

Run: uv run pytest hosts/lib/tests/test_server_initial_setup_part2.py
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

HOST = "daniel-server"
ROLE = ANSIBLE / "roles" / "setup" / "initial_setup"
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
    return render_context(ROLE, HOST, inventory=REPO_INVENTORY)


def test_the_role_skips_the_three_subjects_on_this_host(context):
    for subject in set(_subject_tasks().values()):
        assert HOST in context["initial_setup_packaged_hosts"][subject], subject


def test_each_cron_this_host_stops_writing_is_retired_and_a_timer_runs_at_its_slot(
    package, manifest, context
):
    retired = {e["cron"]: e["user"] for e in manifest["legacy"] if "cron" in e}
    for name, cron in _crons(context).items():
        if name in NOT_HERE:
            continue
        assert retired.get(name) == cron["user"], name
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


def test_the_scans_read_this_hosts_own_tokens(package):
    for scan in ("rkhunter", "aide"):
        unit = _text(package, f"/usr/lib/systemd/system/homelab-{scan}-check.service")
        key = f"{scan}_daniel_server_push_token"
        assert f"LoadCredential={scan}_push_token:/run/credstore/{key}\n" in unit
        assert key in package.credentials
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


def test_reconcile_adopts_the_ssh_rules_and_leaves_the_k3s_and_hand_added_rules(
    package, tmp_path, context
):
    node = seeded_pi(package, tmp_path)
    node.state["host"] = HOST
    node.state["installed"][f"homelab-{HOST}"] = node.state["installed"].pop(
        "homelab-daniel-pi"
    )
    node.state["users"] = {"claude": 995}
    lan = context["lan_subnet"]
    server = "192.0.2.10"
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

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert [tuple(r) for r in node.state["ufw"]["rules"]] == [
        (LAN.format(lan=lan), "homelab:keep:lan-ssh"),
        tuple(others[0]),
        ("limit 22/tcp", "homelab:keep:ssh-limit"),
    ] + [tuple(r) for r in others[1:]]
    ufw = [c["argv"][1:] for c in node.calls("ufw")]
    assert not [a for a in ufw if a[:1] in (["--force"], ["default"], ["delete"])]
