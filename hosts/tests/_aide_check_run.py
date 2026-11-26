"""The aide-check.sh run harness the AIDE test modules share.

`_run_check` renders the script, repoints every absolute path it reads at `tmp_path`, stubs
aide, and runs it against a fake dpkg database and filesystem that `_fake_host` writes.
"""

import fcntl
import os
import shlex
import sys
import time

from _aide_fake_host import ACCOUNT_FILES, DEFS, PACKAGE, TOOL_COPY, _fake_host
from _helpers import REPO
from _shell_render import render_shell_script
from lib.proc_testing import fake_bin, run, write_exec

SCRIPT = ("components", "aide-scan", "aide-check.sh.j2")

# The real library, so the scan writes its textfile under TEXTFILE_DIR, not the directory
# node_exporter scrapes.
REAL_CHECK_LIB = "/usr/lib/homelab/homelab-check-lib.sh"
CHECK_LIB = (
    REPO / "hosts" / "components" / "check-lib" / "files" / "homelab-check-lib.sh"
)
REAL_AIDE = "/usr/bin/nice -n19 /usr/bin/ionice -c3 /usr/bin/aide"
REAL_CAP = "REPORT_MAX_LINES=2000"
REAL_LOCK = "LOCK_FILE=/run/lock/aide-check.lock"
REAL_AIDE_DIR = "AIDE_DIR=/var/lib/aide"
REAL_DPKG_DIR = "DPKG_DIR=/var/lib/dpkg"
REAL_DPKG_LOG = "DPKG_LOG=/var/log/dpkg.log"
REAL_FS_ROOT = 'FS_ROOT=""'
REAL_AIDE_ORIGINALS = "AIDE_ORIGINALS=/usr/lib/homelab/aide-originals"
REAL_ORIGINALS = "ORIGINALS=/var/lib/homelab-aide-originals"
AIDE_ORIGINALS = (
    REPO / "hosts" / "components" / "aide-scan" / "files" / "aide_originals.py"
)

CLEAN_REPORT = """\
Start timestamp: 2026-10-19 03:00:01 +0000 (AIDE 0.18.6)
AIDE found NO differences between database and filesystem. Looks okay!!
"""
DIFF_REPORT = """\
Start timestamp: 2026-10-19 03:00:01 +0000 (AIDE 0.18.6)
AIDE found differences between database and filesystem!!

Summary:
  Total number of entries:\t266585
  Added entries:\t\t2
  Removed entries:\t\t0
  Changed entries:\t\t3

---------------------------------------------------
Added entries:
---------------------------------------------------

f+++++++++++++++++: /etc/new-one
f+++++++++++++++++: /etc/new-two
"""


def _report(*entries: str) -> str:
    """A completed report listing `entries`, each a summary column and path as AIDE prints them."""
    return f"{DIFF_REPORT.split('---')[0]}Changed entries:\n\n" + "".join(
        f"{e}\n" for e in entries
    )


ADDED, REMOVED = "f+++++++++++++++++", "f-----------------"
CHANGED_CONTENT = "f =.... mc..H.. . "
# YlZbpugamcinHAXSEC: a permission change prints `p` in column 5.
CHANGED_MODE = "f =.p.. mc..H.. . "
LINK_ADDED = "l+++++++++++++++++"
LINK_CHANGED = "l =.... mc.. .. . "
# A regular file replaced by a link: its type, so its permissions, changed.
LINK_CHANGED_MODE = "l =.p.. mc.. .. . "
# When aide.db was last advanced. The script opens its dpkg.log window there.
BASELINE_EPOCH = 1_792_000_000
DAY_S = 86_400
# The line a bare `aide --check` logs, and the exit it comes with.
ERROR_REPORT = (
    "  ERROR: missing configuration (use '--config' '--before' or '--after' "
    "command line parameter)\n"
)

# systemd's IgnoreSIGPIPE=yes default: an ignored signal stays ignored across exec, so a write to a
# closed pipe prints bash's `write error: Broken pipe` rather than dying silently.
IGNORING_SIGPIPE = ("sh", "-c", 'trap "" PIPE; exec "$0" "$@"')

LOGGER_STUB = """\
#!/bin/sh
tag=""
while [ $# -gt 0 ]; do
  case "$1" in
    -t) tag=$2; shift 2 ;;
    -p) shift 2 ;;
    *) break ;;
  esac
done
if [ $# -gt 0 ]; then
  printf '%s: %s\\n' "$tag" "$*" >> "$LOGGER_OUT"
else
  while IFS= read -r line; do printf '%s: %s\\n' "$tag" "$line" >> "$LOGGER_OUT"; done
fi
"""


def verdict_of(logged: list[str], tag: str) -> list[str] | None:
    """The [status, message] pair of the `status=` line a script logged under `tag`, or None.

    The scans push nothing since M6 batch B9's Cutover-2, so the logged verdict is the one place
    a run's status and message are readable.
    """
    prefix = f"{tag}: status="
    for line in logged:
        if line.startswith(prefix):
            status, _, message = line.removeprefix(prefix).partition(" ")
            return [status, message]
    return None


def _dpkg_log_lines(
    packages: tuple[str, ...], epoch: int, upgraded: tuple[str, ...] = ()
) -> str:
    """dpkg.log as a package run writes it, stamped in local time as dpkg does.

    Each package gets a `configure` line, and one `status installed` line that the script must
    not read as an event of its own. Each `upgraded` package gets an `upgrade` line first.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
    return (
        f"{stamp} startup packages configure\n"
        + "".join(f"{stamp} upgrade {pkg}:amd64 0.9 1.0\n" for pkg in upgraded)
        + "".join(
            f"{stamp} configure {pkg}:amd64 1.0 1.0\n{stamp} status installed {pkg}:amd64 1.0\n"
            for pkg in packages
        )
    )


def _run_check(
    tmp_path,
    report: str,
    rc: int,
    cap: int = 2000,
    lock_held: bool = False,
    args: tuple[str, ...] = (),
    baseline: bool = True,
    rewrite: dict[str, str] | None = None,
    relink: dict[str, str] | None = None,
    unreadable: tuple[str, ...] = (),
    packages_ran: tuple[str, ...] = ("tool",),
    packages_ran_before: tuple[str, ...] = (),
    packages_upgraded: tuple[str, ...] = (),
    pending: bool = False,
    stubs: dict[str, str] | None = None,
    ignore_sigpipe: bool = False,
    snapshot: dict[str, str | None] | None = None,
    pending_snapshot: dict[str, str] | None = None,
    during_scan: dict[str, str] | None = None,
    originals: dict[str, str] | None = None,
):
    """Render aide-check.sh and run it with aide stubbed to print `report` and exit `rc`.

    Only absolute paths are repointed: the lock file, the nice/ionice/aide invocation, the AIDE database directory, and the dpkg
    database and filesystem root `_fake_host` writes. The aide stub writes `fd8` to `tmp_path / "ran"` when
    it inherits the lock's descriptor. `lock_held` holds the lock from this
    process for the run, as a scan still running from the previous week would. The aide stub
    writes aide.db.new as `--update` does; `baseline` says whether aide.db exists beforehand.
    `rewrite` replaces a path on the fake host with a regular file holding the given content, and
    `relink` with a symlink to the given target, before the run. `unreadable` sets each named
    path to mode 000. `dpkg_ran` dates dpkg's status
    log after the baseline, one `configure` line per `packages_ran` entry, as a package run does,
    and `packages_ran_before` writes the same lines a month before it, in the rotated log.
    `packages_upgraded` adds an `upgrade` line after the baseline for each package it names.
    `pending` leaves an aide.db.new from an earlier run. `stubs` adds fake binaries in front of PATH.
    `ignore_sigpipe` starts the script with SIGPIPE ignored, as systemd starts a service.
    The last accepted copies of the account files hold ACCOUNT_FILES, with `snapshot` replacing
    a file's copy, or removing it for None. `pending_snapshot` leaves an earlier run's copies
    beside its aide.db.new, ACCOUNT_FILES with those files replaced. `during_scan` has the aide
    stub append the given text to each named path while it runs. `originals` keeps the given
    content at each path under aide-originals' state directory, such as TOOL_COPY, in place of
    DEFS as the installed tool version ships it at TOOL_COPY. The real aide-originals runs,
    with an `apt-get` stub that fails, so a copy no test keeps is never fetched.
    The script's stderr lands in `tmp_path / "stderr"`. Returns (exit code, the verdict as a
    [status, message] pair read from the `status=` line the script logs, or None when it logged
    none, logged lines, whether aide ran).
    """
    body = render_shell_script(
        *SCRIPT,
        overrides={"domain": "example.test", "k3s_metallb_ingress_vip": "10.0.0.240"},
    )
    for needle in (
        REAL_AIDE,
        REAL_CAP,
        REAL_LOCK,
        REAL_CHECK_LIB,
        REAL_AIDE_DIR,
        REAL_DPKG_DIR,
        REAL_DPKG_LOG,
        REAL_FS_ROOT,
        REAL_AIDE_ORIGINALS,
        REAL_ORIGINALS,
    ):
        assert needle in body, (
            f"{needle} is no longer in aide-check.sh; this harness repoints it"
        )
    dpkg, fs = _fake_host(tmp_path)
    for path, content in (rewrite or {}).items():
        (fs / path.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (fs / path.lstrip("/")).unlink(missing_ok=True)
        (fs / path.lstrip("/")).write_text(content)
    for path in unreadable:
        (fs / path.lstrip("/")).chmod(0)
    for path, target in (relink or {}).items():
        (fs / path.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (fs / path.lstrip("/")).unlink(missing_ok=True)
        (fs / path.lstrip("/")).symlink_to(target)
    aide_dir = tmp_path / "aide-db"
    aide_dir.mkdir()
    if baseline:
        (aide_dir / "aide.db").write_text("last week\n")
    baseline_time = BASELINE_EPOCH
    os.utime(
        aide_dir / "aide.db" if baseline else aide_dir, (baseline_time, baseline_time)
    )
    dpkg_log = tmp_path / "dpkg.log"
    (tmp_path / "dpkg.log.1").write_text(
        _dpkg_log_lines(packages_ran_before, baseline_time - 30 * DAY_S)
    )
    dpkg_log.write_text(
        _dpkg_log_lines(packages_ran, baseline_time + 60, packages_upgraded)
    )
    if pending:
        (aide_dir / "aide.db.new").write_text("reviewed\n")
    copies = {"accounts": {**ACCOUNT_FILES, **(snapshot or {})}}
    if pending_snapshot is not None:
        copies["accounts.new"] = {**ACCOUNT_FILES, **pending_snapshot}
    for name, files in copies.items():
        (aide_dir / name).mkdir(mode=0o700)
        for path, content in files.items():
            if content is not None:
                (aide_dir / name / path.rsplit("/", 1)[1]).write_text(content)

    lock_file = tmp_path / "aide-check.lock"
    copies = tmp_path / "originals"
    for kept, content in (
        {TOOL_COPY: PACKAGE[DEFS][0]} if originals is None else originals
    ).items():
        copy = copies / kept
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(content)
    originals_bin = write_exec(
        tmp_path / "aide-originals",
        f'PYTHONPATH={REPO / "hosts" / "lib"} exec {sys.executable} {AIDE_ORIGINALS} "$@"\n',
    )
    report_file = tmp_path / "report"
    report_file.write_text(report)
    aide = write_exec(
        tmp_path / "aide",
        f'{{ [ -e /dev/fd/8 ] && echo fd8; true; }} > "$AIDE_RAN"\ncat {report_file}\n'
        + "".join(
            f"printf '%s' {shlex.quote(text)} >> {fs / path.lstrip('/')}\n"
            for path, text in (during_scan or {}).items()
        )
        + f'[ {rc} -le 7 ] && echo "this week" > {aide_dir}/aide.db.new\nexit {rc}\n',
    )
    body = (
        body.replace(REAL_CHECK_LIB, str(CHECK_LIB))
        .replace(REAL_AIDE, str(aide))
        .replace(REAL_CAP, f"REPORT_MAX_LINES={cap}")
        .replace(REAL_LOCK, f"LOCK_FILE={lock_file}")
        .replace(REAL_AIDE_DIR, f"AIDE_DIR={aide_dir}")
        .replace(REAL_DPKG_DIR, f"DPKG_DIR={dpkg}")
        .replace(REAL_DPKG_LOG, f"DPKG_LOG={dpkg_log}")
        .replace(REAL_FS_ROOT, f"FS_ROOT={fs}")
        .replace(REAL_AIDE_ORIGINALS, f"AIDE_ORIGINALS={originals_bin}")
        .replace(REAL_ORIGINALS, f"ORIGINALS={copies}")
    )
    script = write_exec(tmp_path / "aide-check.sh", body)
    logged, ran = (tmp_path / n for n in ("logged", "ran"))
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir(exist_ok=True)
    with open(lock_file, "w") as holder:
        if lock_held:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(
            [*IGNORING_SIGPIPE, str(script), *args]
            if ignore_sigpipe
            else [str(script), *args],
            env={
                **os.environ,
                "TEXTFILE_DIR": str(textfile_dir),
                "LOGGER_OUT": str(logged),
                "AIDE_RAN": str(ran),
            },
            stub_bin=fake_bin(
                tmp_path / "bin",
                logger=LOGGER_STUB,
                **{
                    "apt-get": "echo 'E: no network in the suite' >&2\nexit 100\n",
                    **(stubs or {}),
                },
            ),
        )
    (tmp_path / "stderr").write_text(result.stderr)
    lines = logged.read_text().splitlines() if logged.exists() else []
    return result.returncode, verdict_of(lines, "aide-check"), lines, ran.exists()
