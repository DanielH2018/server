"""The aide-check.sh run harness the AIDE test modules share.

`_run_check` renders the script, repoints every absolute path it reads at `tmp_path`, stubs
aide, and runs it against a fake dpkg database and filesystem that `_fake_host` writes.
"""

import fcntl
import hashlib
import os

from lib.proc_testing import fake_bin, run, write_exec
from _helpers import ROLES
from _shell_render import render_shell_script

SCRIPT = ("setup", "initial_setup", "aide-check.sh.j2")

REAL_LIB = "/usr/local/lib/kuma-push-lib.sh"
# The real library, so the scan writes its textfile under TEXTFILE_DIR rather than the host's
# own directory, which node_exporter scrapes.
REAL_CHECK_LIB = "/usr/local/lib/homelab-check-lib.sh"
CHECK_LIB = ROLES / "setup" / "initial_setup" / "files" / "homelab-check-lib.sh"
REAL_ENV = "/etc/homelab/aide-kuma-push.env"
REAL_AIDE = "/usr/bin/nice -n19 /usr/bin/ionice -c3 /usr/bin/aide"
REAL_CAP = "REPORT_MAX_LINES=2000"
REAL_LOCK = "LOCK_FILE=/run/lock/aide-check.lock"
REAL_AIDE_DIR = "AIDE_DIR=/var/lib/aide"
REAL_DPKG_DIR = "DPKG_DIR=/var/lib/dpkg"
REAL_FS_ROOT = 'FS_ROOT=""'

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


# AIDE's 18-character summary column, as the 2026-10-20 daniel-box report printed each kind.
ADDED, REMOVED = "f+++++++++++++++++", "f-----------------"
CHANGED_CONTENT = "f =.... mc..H.. . "
# YlZbpugamcinHAXSEC: a permission change prints `p` in column 5.
CHANGED_MODE = "f =.p.. mc..H.. . "
LINK_ADDED = "l+++++++++++++++++"
LINK_CHANGED = "l =.... mc.. .. . "
# When aide.db was last advanced. The script compares it only with the dpkg status file's mtime.
BASELINE_EPOCH = 1_792_000_000
# One installed package: each path's content, and whether dpkg records its digest in md5sums
# (relative, under the pre-usrmerge name) or as a conffile in status.
PACKAGE = {
    "/usr/bin/tool": ("tool v2\n", "md5sums"),
    "/usr/lib/x86_64-linux-gnu/libtool.so": ("lib v2\n", "md5sums"),
    "/etc/tool.conf": ("conf v2\n", "conffile"),
    "/usr/share/doc/tool/README": ("readme v1\n", "md5sums"),
}
# Package-owned symlinks, which dpkg lists with no digest, and where each points.
PACKAGE_LINKS = {"/usr/bin/tool-link": "tool"}


def _dpkg_name(path: str) -> str:
    """The name dpkg records: a package built before usrmerge lists /usr/lib files as /lib."""
    return path.removeprefix("/usr") if path.startswith("/usr/lib/") else path


def _fake_host(root):
    """Writes PACKAGE as installed, with dpkg's records of it, under `root`.

    Returns (dpkg dir, filesystem root). dpkg also records one diversion of the package's README,
    and the filesystem also holds an alternatives link to the package's binary, a unit masked by
    a maintainer script and one file in each tree nothing packages.
    """
    dpkg, fs = root / "dpkg", root / "fs"
    (dpkg / "info").mkdir(parents=True)
    md5sums, conffiles = [], []
    for path, (content, record) in PACKAGE.items():
        target = fs / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        digest = hashlib.md5(content.encode()).hexdigest()
        if record == "md5sums":
            md5sums.append(f"{digest}  {_dpkg_name(path).lstrip('/')}")
        else:
            conffiles.append(f" {path} {digest}")
    for link, target in PACKAGE_LINKS.items():
        (fs / link.lstrip("/")).symlink_to(target)
    (dpkg / "info" / "tool.list").write_text(
        "".join(f"{_dpkg_name(p)}\n" for p in [*PACKAGE, *PACKAGE_LINKS])
    )
    (dpkg / "info" / "tool.md5sums").write_text("\n".join(md5sums) + "\n")
    # A third-party package, whose md5sums file names each path as ./usr/...
    (fs / "usr/bin/vendor").write_text("vendor v1\n")
    (dpkg / "info" / "vendor.list").write_text("/usr/bin/vendor\n")
    vendor_digest = hashlib.md5(b"vendor v1\n").hexdigest()
    (dpkg / "info" / "vendor.md5sums").write_text(
        f"{vendor_digest}  ./usr/bin/vendor\n"
    )
    (dpkg / "status").write_text(
        "Package: tool\nStatus: install ok installed\nConffiles:\n"
        + "\n".join(conffiles)
        + "\nDescription: a tool\n"
    )
    # dpkg's diversions file: diverted path, its new name, the diverting package. tool's README
    # now sits at the new name.
    (dpkg / "diversions").write_text(
        "/usr/share/doc/tool/README\n/usr/share/doc/tool/README.distrib\ntool-extra\n"
    )
    (fs / "usr/share/doc/tool/README").rename(fs / "usr/share/doc/tool/README.distrib")
    (fs / "etc" / "alternatives").mkdir(parents=True)
    (fs / "etc" / "alternatives" / "tool").symlink_to(fs / "usr" / "bin" / "tool")
    units = fs / "usr" / "lib" / "systemd" / "system"
    units.mkdir(parents=True)
    (units / "tool-cleanup.service").symlink_to("/dev/null")
    for path in (
        "/usr/sbin/implant",
        "/etc/homelab/deployed.env",
        "/etc/sudoers.d/extra",
    ):
        (fs / path.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (fs / path.lstrip("/")).write_text("unpackaged\n")
    return dpkg, fs


# The line every host logged each Monday until #4591, and the exit it came with.
ERROR_REPORT = (
    "  ERROR: missing configuration (use '--config' '--before' or '--after' "
    "command line parameter)\n"
)

# Records each call as `tag: message`: the message argument when one is given, else every line
# on stdin, which is how the script hands over the report.
# systemd's IgnoreSIGPIPE=yes default. An ignored signal stays ignored across exec, and only then
# does a write to a closed pipe print bash's `write error: Broken pipe` instead of dying silently.
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


def _run_check(
    tmp_path,
    report: str,
    rc: int,
    token: str = "stubtoken",
    cap: int = 2000,
    lock_held: bool = False,
    args: tuple[str, ...] = (),
    baseline: bool = True,
    rewrite: dict[str, str] | None = None,
    relink: dict[str, str] | None = None,
    dpkg_ran: bool = True,
    pending: bool = False,
    stubs: dict[str, str] | None = None,
    ignore_sigpipe: bool = False,
):
    """Render aide-check.sh and run it with aide stubbed to print `report` and exit `rc`.

    Only absolute paths are repointed: the push lib (a recording stub), the token env file, the
    lock file, the nice/ionice/aide invocation, the AIDE database directory, and the dpkg
    database and filesystem root `_fake_host` writes. `lock_held` holds the lock from this
    process for the run, as a scan still running from the previous week would. The aide stub
    writes aide.db.new as `--update` does; `baseline` says whether aide.db exists beforehand.
    `rewrite` replaces a path on the fake host with a regular file holding the given content, and
    `relink` with a symlink to the given target, before the run. `dpkg_ran` dates dpkg's status
    file after the baseline, as any package run does, and `pending` leaves an aide.db.new from an
    earlier run. `stubs` adds fake binaries in front of PATH.
    `ignore_sigpipe` starts the script with SIGPIPE ignored, as systemd starts a service.
    The script's stderr lands in `tmp_path / "stderr"`. Returns (exit code, pushed lines or
    None, logged lines, whether aide ran).
    """
    body = render_shell_script(
        *SCRIPT,
        overrides={"domain": "example.test", "k3s_metallb_ingress_vip": "10.0.0.240"},
    )
    for needle in (
        REAL_LIB,
        REAL_ENV,
        REAL_AIDE,
        REAL_CAP,
        REAL_LOCK,
        REAL_CHECK_LIB,
        REAL_AIDE_DIR,
        REAL_DPKG_DIR,
        REAL_FS_ROOT,
    ):
        assert needle in body, (
            f"{needle} is no longer in aide-check.sh; this harness repoints it"
        )
    dpkg, fs = _fake_host(tmp_path)
    for path, content in (rewrite or {}).items():
        (fs / path.lstrip("/")).unlink(missing_ok=True)
        (fs / path.lstrip("/")).write_text(content)
    for path, target in (relink or {}).items():
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
    status_time = baseline_time + (60 if dpkg_ran else -60)
    os.utime(dpkg / "status", (status_time, status_time))
    if pending:
        (aide_dir / "aide.db.new").write_text("reviewed\n")

    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(
        'kuma_push() { printf \'%s\\n%s\\n\' "$1" "$2" > "$KUMA_PUSH_OUT"; }\n'
    )
    env_file = tmp_path / "push.env"
    env_file.write_text(f"AIDE_PUSH_TOKEN={token}\n")
    lock_file = tmp_path / "aide-check.lock"
    report_file = tmp_path / "report"
    report_file.write_text(report)
    aide = write_exec(
        tmp_path / "aide",
        f'touch "$AIDE_RAN"\ncat {report_file}\n'
        f'[ {rc} -le 7 ] && echo "this week" > {aide_dir}/aide.db.new\nexit {rc}\n',
    )
    body = (
        body.replace(REAL_LIB, str(lib))
        .replace(REAL_CHECK_LIB, str(CHECK_LIB))
        .replace(REAL_ENV, str(env_file))
        .replace(REAL_AIDE, str(aide))
        .replace(REAL_CAP, f"REPORT_MAX_LINES={cap}")
        .replace(REAL_LOCK, f"LOCK_FILE={lock_file}")
        .replace(REAL_AIDE_DIR, f"AIDE_DIR={aide_dir}")
        .replace(REAL_DPKG_DIR, f"DPKG_DIR={dpkg}")
        .replace(REAL_FS_ROOT, f"FS_ROOT={fs}")
    )
    script = write_exec(tmp_path / "aide-check.sh", body)
    pushed, logged, ran = (tmp_path / n for n in ("pushed", "logged", "ran"))
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
                "KUMA_PUSH_OUT": str(pushed),
                "TEXTFILE_DIR": str(textfile_dir),
                "LOGGER_OUT": str(logged),
                "AIDE_RAN": str(ran),
            },
            stub_bin=fake_bin(tmp_path / "bin", logger=LOGGER_STUB, **(stubs or {})),
        )
    (tmp_path / "stderr").write_text(result.stderr)
    return (
        result.returncode,
        pushed.read_text().splitlines() if pushed.exists() else None,
        logged.read_text().splitlines() if logged.exists() else [],
        ran.exists(),
    )
