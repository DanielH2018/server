"""The weekly host-maintenance crons write a textfile verdict their rules read (#6179).

The apt autoremove, dpkg purge and firmware update crons used to pipe their command into
`logger`, so the pipeline's status was logger's and nothing wrote a textfile. A job that failed
every week, or stopped running, was silent. They now run through `files/host-maintenance.sh`,
and three properties hold:

THE VERDICT IS THE JOB'S OWN EXIT CODE. fwupdmgr exits 2 when there is nothing to update, the
normal weekly case, so 2 is a success there and nowhere else. A failed purge inside the dpkg
pipeline reaches the verdict through pipefail.

THE RULES' LITERALS EQUAL THE SCRIPT'S DEADLINE_S. `host-checks.yaml` is plain YAML, so each
NotReporting rule names the deadline twice, and a deadline moved without its rules would page
on every healthy week (M1 convention 1).

EACH CRON NAMES THE CHECK ITS RULES READ, on exactly the hosts that run it.

Run: uv run pytest ansible/tests/setup/test_host_maintenance.py
"""

import os
import re

import pytest
from _helpers import K8S_ROLES, ROLES, load_defaults, load_tasks, walk_tasks
from _role_census import role_task_files
from lib import yaml_fast
from lib.proc_testing import fake_bin, run, write_exec
from test_aide_check import LOGGER_STUB, _textfile

ROLE = ROLES / "setup" / "initial_setup"
SCRIPT = ROLE / "files" / "host-maintenance.sh"
CHECK_LIB = ROLE / "files" / "homelab-check-lib.sh"
REAL_CHECK_LIB = "/usr/local/lib/homelab-check-lib.sh"
RULES = K8S_ROLES / "observability" / "files" / "rules" / "host-checks.yaml"
STALE_ARM = re.compile(r"(?<![<>=!])>(?!=)\s*(\d+)")
# The guard on the absent arm: the series existed in this window, so a never-written file is quiet.
HISTORY = re.compile(r"present_over_time\([^)]*\[(\d+)s\]\)")
WINDOW = re.compile(r"absent_over_time\([^)]*\[(\d+)s\]\)")

EVERY_HOST = {"daniel-box", "daniel-server", "daniel-pi"}
# Check -> (the cron that runs it, the alert prefix, the hosts whose rules read it). The firmware
# cron is gated on a stat of /usr/bin/fwupdmgr, which no inventory var carries; daniel-box is
# the one host with fwupd (crons.yml, "Detect whether fwupd is installed on this host").
JOBS = {
    "apt-autoremove": ("Weekly apt autoremove", "AptAutoremove", EVERY_HOST),
    "dpkg-purge": ("Weekly dpkg purge orphaned configs", "DpkgPurge", EVERY_HOST),
    "firmware-update": ("Weekly firmware update", "FirmwareUpdate", {"daniel-box"}),
}

# `dpkg -l` lists one removed package with its config left behind; `--purge` exits $PURGE_RC.
DPKG_STUB = """\
if [ "$1" = "-l" ]; then
  echo "rc  oldpkg  1.0  amd64  removed package"
  exit 0
fi
echo "purging $*"
exit "${PURGE_RC:-0}"
"""


def _run(tmp_path, job: str, rc: int):
    """Run host-maintenance.sh JOB with its command stubbed to exit `rc`.

    Returns (exit code, the check's textfile series).
    """
    body = SCRIPT.read_text()
    assert REAL_CHECK_LIB in body, "this harness repoints the check library"
    script = write_exec(
        tmp_path / "host-maintenance.sh", body.replace(REAL_CHECK_LIB, str(CHECK_LIB))
    )
    textfile = tmp_path / "textfile"
    textfile.mkdir()
    stubs = fake_bin(
        tmp_path / "bin",
        logger=LOGGER_STUB,
        **{"apt-get": f"exit {rc}\n"},
        fwupdmgr=f'[ "$1" = refresh ] && exit 0\nexit {rc}\n',
        dpkg=DPKG_STUB,
    )
    result = run(
        [str(script), job],
        env={
            **os.environ,
            "TEXTFILE_DIR": str(textfile),
            "LOGGER_OUT": str(tmp_path / "logged"),
            "PURGE_RC": str(rc),
        },
        stub_bin=stubs,
    )
    return result.returncode, _textfile(tmp_path, job)


def deadline() -> int:
    [found] = re.findall(r"^DEADLINE_S=(\d+)$", SCRIPT.read_text(), re.M)
    return int(found)


@pytest.mark.parametrize(
    ("job", "rc", "success"),
    [
        ("firmware-update", 2, "1"),
        ("firmware-update", 0, "1"),
        ("firmware-update", 1, "0"),
        ("apt-autoremove", 0, "1"),
        ("apt-autoremove", 2, "0"),
        ("dpkg-purge", 0, "1"),
        ("dpkg-purge", 1, "0"),
    ],
)
def test_the_verdict_follows_the_jobs_exit_code(tmp_path, job, rc, success):
    code, series = _run(tmp_path, job, rc)
    assert series["homelab_check_success"] == success
    assert series["homelab_check_deadline_seconds"] == str(deadline())
    assert (code == 0) == (success == "1")


def not_reporting(text: str) -> dict[tuple[str, str], str]:
    """(check, origin) -> expr for every host-maintenance NotReporting rule."""
    [group] = yaml_fast.safe_load(text)["groups"]
    prefixes = {spec[1]: check for check, spec in JOBS.items()}
    found = {}
    for rule in group["rules"]:
        check = prefixes.get(rule["alert"].removesuffix("NotReporting"))
        if check and rule["alert"].endswith("NotReporting"):
            [origin] = re.findall(r'origin="([^"]+)"', rule["expr"])[:1]
            found[(check, origin)] = rule["expr"]
    return found


def literal_problems(rules: dict[tuple[str, str], str], deadline_s: int) -> list[str]:
    """Why the rule set or a rule's deadline literals differ from the script, or nothing.

    The absent arm's history guard must outlast the deadline, or the arm can never fire, and
    must fit inside Prometheus's retention, or the history it reads is already gone.
    """
    expected = {(check, host) for check, spec in JOBS.items() for host in spec[2]}
    problems = [
        f"no NotReporting rule for {key}" for key in sorted(expected - set(rules))
    ]
    problems += [
        f"a NotReporting rule nothing runs: {key}"
        for key in sorted(set(rules) - expected)
    ]
    for (check, origin), expr in sorted(rules.items()):
        if f'check="{check}"' not in expr.split("absent_over_time")[1]:
            problems.append(f"{check} on {origin}: the absent arm reads another check")
        for name, found in (
            ("threshold", STALE_ARM.findall(expr)),
            ("window", WINDOW.findall(expr)),
        ):
            if found != [str(deadline_s)]:
                problems.append(
                    f"{check} on {origin}: {name} reads {found}, DEADLINE_S is {deadline_s}"
                )
        history = [int(s) for s in HISTORY.findall(expr)]
        if len(history) != 1 or not deadline_s < history[0] <= retention_s():
            problems.append(
                f"{check} on {origin}: the history guard reads {history}, not one window "
                f"between DEADLINE_S {deadline_s} and the {retention_s()}s retention"
            )
    return problems


def retention_s() -> int:
    """Prometheus's time retention, `observability_prometheus_retention`, in seconds."""
    defaults = load_defaults(K8S_ROLES / "observability")
    days = re.fullmatch(r"(\d+)d", defaults["observability_prometheus_retention"])
    assert days, (
        "observability_prometheus_retention is no longer a whole number of days"
    )
    return int(days[1]) * 86400


def test_literal_problems_is_clean_on_the_shipped_rules():
    rules = not_reporting(RULES.read_text())
    assert len(rules) == 7
    assert literal_problems(rules, deadline()) == []


def test_literal_problems_is_flagged_on_a_window_that_drifted():
    text = RULES.read_text().replace(
        'check="dpkg-purge",origin="daniel-pi"}[612000s]',
        'check="dpkg-purge",origin="daniel-pi"}[604800s]',
    )
    assert literal_problems(not_reporting(text), deadline()) == [
        "dpkg-purge on daniel-pi: window reads ['604800'], DEADLINE_S is 612000"
    ]


def test_literal_problems_is_flagged_on_a_history_guard_shorter_than_the_deadline():
    text = RULES.read_text().replace(
        'check="firmware-update",origin="daniel-box"}[2419200s]',
        'check="firmware-update",origin="daniel-box"}[600000s]',
    )
    assert literal_problems(not_reporting(text), deadline()) == [
        "firmware-update on daniel-box: the history guard reads [600000], not one window "
        "between DEADLINE_S 612000 and the 2592000s retention"
    ]


def test_literal_problems_is_flagged_on_a_host_with_no_rule():
    rules = not_reporting(RULES.read_text())
    del rules[("apt-autoremove", "daniel-server")]
    assert literal_problems(rules, deadline()) == [
        "no NotReporting rule for ('apt-autoremove', 'daniel-server')"
    ]


def test_each_cron_runs_the_wrapper_with_the_check_its_rules_read():
    jobs = {}
    for tasks_file in role_task_files(ROLE):
        for task in walk_tasks(load_tasks(tasks_file)):
            cron = task.get("ansible.builtin.cron")
            if isinstance(cron, dict) and cron.get("state") != "absent":
                jobs[cron.get("name")] = str(cron.get("job"))
    for check, (cron, _, _) in JOBS.items():
        assert jobs[cron].startswith(f"/usr/local/bin/host-maintenance.sh {check} "), (
            f"`{cron}` does not run host-maintenance.sh {check}: {jobs[cron]}"
        )
