"""Every host reports the *.ucf-dist and *.dpkg-dist copies an upgrade left pending under /etc.

ucf and dpkg keep a locally edited config file across an upgrade and leave the package's new
version beside it as *.ucf-dist or *.dpkg-dist. `ucf-dist-check.sh` in the host-maintenance component counts them
every hour, and three properties hold:

THE VERDICT IS THE COUNT OF COPIES OLDER THAN A DAY, aged by ctime. Only a find that cannot
read the directory fails the check's own unit.

THE RULES' LITERALS EQUAL THE SCRIPT'S DEADLINE_S, on one Pending and NotReporting pair per host.

EVERY HOST SHIPS AND ARMS THE TIMER.

Run: uv run pytest hosts/tests/test_ucf_dist_check.py
"""

import os
import re

from _aide_check_run import LOGGER_STUB
from _helpers import K8S_ROLES, REPO
from _host_packages import HOSTS, package, shipped_text
from lib import yaml_fast
from lib.proc_testing import fake_bin, run, write_exec

COMPONENT = REPO / "hosts" / "components" / "host-maintenance"
SCRIPT = COMPONENT / "files" / "ucf-dist-check.sh"
CHECK_LIB = (
    REPO / "hosts" / "components" / "check-lib" / "files" / "homelab-check-lib.sh"
)
RULES = K8S_ROLES / "observability" / "files" / "rules" / "host-checks.yaml"
TIMER = "/usr/lib/systemd/system/homelab-ucf-dist.timer"
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")
DAY = 86400


def _run(tmp_path, clock_offset_s: int = 0):
    """Run the check over `tmp_path/etc`, with `date +%s` reading `clock_offset_s` past its ctime.

    A file's ctime cannot be set from a test, so the stub moves the clock instead. Returns
    (exit code, stdout, every `homelab_*` series of check ucf-dist by name).
    """
    script = write_exec(tmp_path / "ucf-dist-check.sh", SCRIPT.read_text())
    textfile = tmp_path / "textfile"
    textfile.mkdir(exist_ok=True)
    now = int(script.stat().st_ctime) + clock_offset_s
    stubs = fake_bin(tmp_path / "bin", logger=LOGGER_STUB, date=f"echo {now}\n")
    result = run(
        [str(script)],
        env={
            **os.environ,
            "HOMELAB_LIB_DIR": str(CHECK_LIB.parent),
            "TEXTFILE_DIR": str(textfile),
            "ETC_DIR": str(tmp_path / "etc"),
            "LOGGER_OUT": str(tmp_path / "logged"),
        },
        stub_bin=stubs,
    )
    prom = (textfile / "ucf-dist.prom").read_text()
    series = dict(re.findall(r'^(homelab_\w+)\{check="ucf-dist"\} (\d+)$', prom, re.M))
    return result.returncode, result.stdout, series


def _plant(tmp_path, *names: str) -> None:
    for name in names:
        path = tmp_path / "etc" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")


def deadline() -> int:
    [found] = re.findall(r"^DEADLINE_S=(\d+)$", SCRIPT.read_text(), re.M)
    return int(found)


def test_an_etc_with_no_dist_copy_writes_success_1(tmp_path):
    _plant(tmp_path, "ssh/sshd_config", "ca-certificates.conf.dpkg-old")
    code, _, series = _run(tmp_path, clock_offset_s=2 * DAY)
    assert (code, series["homelab_check_success"]) == (0, "1")
    assert series["homelab_ucf_dist_pending_files"] == "0"
    assert series["homelab_check_deadline_seconds"] == str(deadline())


def test_a_dist_copy_older_than_a_day_writes_success_0_naming_it(tmp_path):
    _plant(tmp_path, "ssh/sshd_config.ucf-dist", "login.defs.dpkg-dist")
    code, out, series = _run(tmp_path, clock_offset_s=DAY + 60)
    assert (code, series["homelab_check_success"]) == (0, "0")
    assert series["homelab_ucf_dist_pending_files"] == "2"
    assert f"{tmp_path}/etc/ssh/sshd_config.ucf-dist" in out
    assert f"{tmp_path}/etc/login.defs.dpkg-dist" in out


def test_only_dpkg_copies_from_before_the_cutoff_are_passed_over(tmp_path):
    [since] = re.findall(r"^DPKG_SINCE=(\d+)$", SCRIPT.read_text(), re.M)
    before, at = int(since) - 1, int(since)
    fake_bin(
        tmp_path / "bin",
        find=f"echo {before}.5 /etc/old.dpkg-dist\n"
        f"echo {before}.5 /etc/old.ucf-dist\n"
        f"echo {at}.5 /etc/new.dpkg-dist\n",
    )
    _, out, series = _run(tmp_path, clock_offset_s=2 * DAY)
    assert series["homelab_ucf_dist_pending_files"] == "2"
    assert "/etc/old.dpkg-dist" not in out


def test_a_dist_copy_younger_than_a_day_is_not_pending(tmp_path):
    _plant(tmp_path, "default/grub.ucf-dist")
    code, _, series = _run(tmp_path, clock_offset_s=DAY - 60)
    assert (code, series["homelab_check_success"]) == (0, "1")


def test_an_unreadable_etc_writes_success_0_and_fails_the_unit(tmp_path):
    code, _, series = _run(tmp_path)
    assert (code, series["homelab_check_success"]) == (1, "0")


def rules(text: str) -> dict[tuple[str, str], str]:
    """(alert, origin) -> expr for every UcfDist* rule."""
    [group] = yaml_fast.safe_load(text)["groups"]
    return {
        (rule["alert"], re.findall(r'origin="([^"]+)"', rule["expr"])[0]): rule["expr"]
        for rule in group["rules"]
        if rule["alert"].startswith("UcfDist")
    }


def literal_problems(found: dict[tuple[str, str], str], deadline_s: int) -> list[str]:
    """Why the rule set or a NotReporting rule's literals differ from the script, or nothing."""
    expected = {
        (alert, host)
        for alert in ("UcfDistPending", "UcfDistNotReporting")
        for host in HOSTS
    }
    problems = [f"no rule {key}" for key in sorted(expected - set(found))]
    problems += [f"an unexpected rule {key}" for key in sorted(set(found) - expected)]
    for (alert, origin), expr in sorted(found.items()):
        if alert != "UcfDistNotReporting":
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


def test_literal_problems_is_clean_on_the_shipped_rules():
    assert literal_problems(rules(RULES.read_text()), deadline()) == []


def test_literal_problems_is_flagged_on_a_window_that_drifted():
    text = RULES.read_text().replace(
        'check="ucf-dist",origin="daniel-pi"}[10800s]',
        'check="ucf-dist",origin="daniel-pi"}[3600s]',
    )
    assert literal_problems(rules(text), deadline()) == [
        "daniel-pi: window reads ['3600'], DEADLINE_S is 10800"
    ]


def test_every_host_arms_an_hourly_timer_that_runs_three_times_per_deadline():
    for host in HOSTS:
        assert any(
            path.endswith("/homelab-ucf-dist.timer") for path in package(host).links
        ), host
        assert re.search(r"^OnCalendar=hourly$", shipped_text(host, TIMER), re.M), host
    assert 3600 * 3 == deadline()
