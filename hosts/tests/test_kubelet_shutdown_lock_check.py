"""A node with a graceful node shutdown reports whether kubelet holds its delay lock.

kubelet logs its failure to take logind's shutdown delay lock once per start and logs nothing on
success, so an alert on that line cannot resolve after a fix. `kubelet-shutdown-lock-check.sh`
in the k3s-server component reads the lock itself, and three properties hold:

THE VERDICT IS THE LOCK. A `kubelet` shutdown lock in `delay` mode writes success 1; any other
lock list, or a systemd-inhibit that cannot list, writes 0. Only the second fails the unit.

THE RULES' LITERALS EQUAL THE SCRIPT'S DEADLINE_S, on one Missing and NotReporting pair per host
that ships kubelet's graceful-shutdown config.

EVERY SUCH HOST ARMS THE TIMER, at a period that fits three runs inside the deadline.

Run: uv run pytest hosts/tests/test_kubelet_shutdown_lock_check.py
"""

import os
import re

from _aide_check_run import LOGGER_STUB
from _helpers import K8S_ROLES, REPO
from _host_packages import HOSTS, package, shipped_text
from lib import yaml_fast
from lib.repo_paths import K3S_SERVER
from lib.proc_testing import fake_bin, run, write_exec

SCRIPT = K3S_SERVER / "files" / "kubelet-shutdown-lock-check.sh"
CHECK_LIB = REPO / "hosts/components/check-lib/files/homelab-check-lib.sh"
RULES = K8S_ROLES / "observability" / "files" / "rules" / "host-checks.yaml"
TIMER = "/usr/lib/systemd/system/homelab-kubelet-shutdown-lock.timer"
GRACEFUL_SHUTDOWN = re.compile(r"/kubelet\.conf\.d/[^/]*graceful-shutdown\.conf$")
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")

# `systemd-inhibit --list --no-legend` on daniel-server with kubelet's lock held.
UNATTENDED = (
    "Unattended Upgrades Shutdown 0   root 1062    unattended-upgr shutdown "
    "Stop ongoing upgrades or perform upgrades before shutdown delay\n"
)
KUBELET = (
    "kubelet                      0   root 3464872 k3s-agent       shutdown "
    "Kubelet needs time to handle node shutdown                delay\n"
)


def _run(tmp_path, listed: str, rc: int = 0) -> tuple[int, dict[str, str]]:
    """Run the check with `systemd-inhibit --list` printing `listed` and exiting `rc`.

    Returns the exit code and every `homelab_*` series of the check by name.
    """
    script = write_exec(tmp_path / "check.sh", SCRIPT.read_text())
    textfile = tmp_path / "textfile"
    textfile.mkdir()
    (tmp_path / "listed").write_text(listed)
    stubs = fake_bin(
        tmp_path / "bin",
        logger=LOGGER_STUB,
        **{"systemd-inhibit": f'cat "{tmp_path / "listed"}"\nexit {rc}\n'},
    )
    result = run(
        [str(script)],
        env={
            **os.environ,
            "HOMELAB_LIB_DIR": str(CHECK_LIB.parent),
            "TEXTFILE_DIR": str(textfile),
            "LOGGER_OUT": str(tmp_path / "logged"),
        },
        stub_bin=stubs,
    )
    prom = (textfile / "kubelet-shutdown-lock.prom").read_text()
    series = dict(
        re.findall(
            r'^(homelab_\w+)\{check="kubelet-shutdown-lock"\} (\d+)$', prom, re.M
        )
    )
    return result.returncode, series


def deadline() -> int:
    [found] = re.findall(r"^DEADLINE_S=(\d+)$", SCRIPT.read_text(), re.M)
    return int(found)


def test_a_held_kubelet_delay_lock_writes_success_1(tmp_path):
    code, series = _run(tmp_path, UNATTENDED + KUBELET)
    assert (code, series["homelab_check_success"]) == (0, "1")
    assert series["homelab_check_deadline_seconds"] == str(deadline())


def test_no_kubelet_lock_writes_success_0(tmp_path):
    code, series = _run(tmp_path, UNATTENDED)
    assert (code, series["homelab_check_success"]) == (0, "0")


def test_a_kubelet_lock_in_block_mode_writes_success_0(tmp_path):
    code, series = _run(tmp_path, KUBELET.replace("delay\n", "block\n"))
    assert (code, series["homelab_check_success"]) == (0, "0")


def test_a_systemd_inhibit_that_cannot_list_writes_success_0(tmp_path):
    code, series = _run(tmp_path, "Failed to connect to bus", rc=1)
    assert (code, series["homelab_check_success"]) == (1, "0")


def graceful_hosts() -> set[str]:
    """The hosts whose package ships kubelet's graceful-shutdown config."""
    return {
        host
        for host in HOSTS
        if any(GRACEFUL_SHUTDOWN.search(path) for path in package(host).files)
    }


def rules(text: str) -> dict[tuple[str, str], str]:
    """(alert, origin) -> expr for every KubeletShutdownLock* rule."""
    [group] = yaml_fast.safe_load(text)["groups"]
    found = {}
    for rule in group["rules"]:
        if rule.get("alert", "").startswith("KubeletShutdownLock"):
            [origin] = re.findall(r'origin="([^"]+)"', rule["expr"])[:1]
            found[(rule["alert"], origin)] = rule["expr"]
    return found


def literal_problems(
    found: dict[tuple[str, str], str], hosts: set[str], deadline_s: int
) -> list[str]:
    """Why the rule set or a NotReporting rule's literals differ from the script, or nothing."""
    expected = {
        (alert, host)
        for alert in ("KubeletShutdownLockMissing", "KubeletShutdownLockNotReporting")
        for host in hosts
    }
    problems = [f"no rule {key}" for key in sorted(expected - set(found))]
    problems += [f"an unexpected rule {key}" for key in sorted(set(found) - expected)]
    for (alert, origin), expr in sorted(found.items()):
        if alert != "KubeletShutdownLockNotReporting":
            continue
        for name, values in (
            ("threshold", STALE_ARM.findall(expr)),
            ("window", WINDOW.findall(expr)),
        ):
            if values != [str(deadline_s)]:
                problems.append(
                    f"{origin}: {name} reads {values}, DEADLINE_S is {deadline_s}"
                )
    return problems


def test_graceful_hosts_names_both_k3s_nodes():
    assert graceful_hosts() == {"daniel-box", "daniel-server"}


def test_literal_problems_is_clean_on_the_shipped_rules():
    assert (
        literal_problems(rules(RULES.read_text()), graceful_hosts(), deadline()) == []
    )


def test_literal_problems_is_flagged_on_a_window_that_drifted():
    text = RULES.read_text().replace(
        'check="kubelet-shutdown-lock",origin="daniel-server"}[900s]',
        'check="kubelet-shutdown-lock",origin="daniel-server"}[600s]',
    )
    assert literal_problems(rules(text), graceful_hosts(), deadline()) == [
        "daniel-server: window reads ['600'], DEADLINE_S is 900"
    ]


def test_literal_problems_is_flagged_on_a_graceful_host_with_no_rules():
    problems = literal_problems(
        rules(RULES.read_text()), graceful_hosts() | {"daniel-pi"}, deadline()
    )
    assert problems == [
        "no rule ('KubeletShutdownLockMissing', 'daniel-pi')",
        "no rule ('KubeletShutdownLockNotReporting', 'daniel-pi')",
    ]


def test_every_graceful_host_arms_a_timer_that_runs_three_times_per_deadline():
    for host in sorted(graceful_hosts()):
        assert any(
            path.endswith("/homelab-kubelet-shutdown-lock.timer")
            for path in package(host).links
        ), host
        [minutes] = re.findall(
            r"^OnCalendar=\*:0/(\d+)$", shipped_text(host, TIMER), re.M
        )
        assert int(minutes) * 60 * 3 == deadline(), host
