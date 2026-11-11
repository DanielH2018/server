"""The aide-check.sh run harness the AIDE test modules share.

`_run_check` renders the script, repoints every absolute path it reads at `tmp_path`, stubs
aide, and runs it against a fake dpkg database and filesystem that `_fake_host` writes.
"""

import fcntl
import hashlib
import os
import time

from lib.proc_testing import fake_bin, run, write_exec
from _helpers import ROLES
from _shell_render import render_shell_script

SCRIPT = ("setup", "initial_setup", "aide-check.sh.j2")

# The real library, so the scan writes its textfile under TEXTFILE_DIR rather than the host's
# own directory, which node_exporter scrapes.
REAL_CHECK_LIB = "/usr/lib/homelab/homelab-check-lib.sh"
CHECK_LIB = ROLES / "setup" / "initial_setup" / "files" / "homelab-check-lib.sh"
REAL_AIDE = "/usr/bin/nice -n19 /usr/bin/ionice -c3 /usr/bin/aide"
REAL_CAP = "REPORT_MAX_LINES=2000"
REAL_LOCK = "LOCK_FILE=/run/lock/aide-check.lock"
REAL_AIDE_DIR = "AIDE_DIR=/var/lib/aide"
REAL_DPKG_DIR = "DPKG_DIR=/var/lib/dpkg"
REAL_DPKG_LOG = "DPKG_LOG=/var/log/dpkg.log"
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
# A regular file replaced by a link: its type, so its permissions, changed.
LINK_CHANGED_MODE = "l =.p.. mc.. .. . "
# When aide.db was last advanced. The script opens its dpkg.log window there.
BASELINE_EPOCH = 1_792_000_000
DAY_S = 86_400
# One installed package: each path's content, and whether dpkg records its digest in md5sums
# (relative, under the pre-usrmerge name) or as a conffile in status.
PACKAGE = {
    "/usr/bin/tool": ("tool v2\n", "md5sums"),
    "/usr/lib/x86_64-linux-gnu/libtool.so": ("lib v2\n", "md5sums"),
    "/etc/tool.conf": ("conf v2\n", "conffile"),
    "/usr/share/doc/tool/README": ("readme v1\n", "md5sums"),
    "/usr/lib/systemd/system/tool.service": ("[Unit]\n", "md5sums"),
    "/usr/lib/python3/dist-packages/toolpkg/mod.py": ("x = 1\n", "md5sums"),
}
# Package-owned symlinks, which dpkg lists with no digest, and where each points. The headers
# link points into a package that is not installed, as the kernel headers' rust link does.
PACKAGE_LINKS = {
    "/usr/bin/tool-link": "tool",
    "/usr/src/tool-headers/rust": "../tool-lib-rust/rust",
}
# The host package's link to a secret in the tmpfs its secrets unit fills.
SECRET_LINK = "/etc/svc/config.env"
SECRET_TARGET = "/run/homelab-secrets/etc/svc/config.env"
# The host package's apt hook and a script root runs, which dpkg lists with no digest here.
HOMELAB_FILES = ("/etc/apt/apt.conf.d/91homelab-hook", "/usr/lib/homelab/check")


def _dpkg_name(path: str) -> str:
    """The name dpkg records: a package built before usrmerge lists /usr/lib files as /lib."""
    return path.removeprefix("/usr") if path.startswith("/usr/lib/") else path


def _fake_host(root):
    """Writes PACKAGE as installed, with dpkg's records of it, under `root`.

    Returns (dpkg dir, filesystem root). dpkg also records one diversion of the package's README,
    and the filesystem also holds an alternatives link to the package's binary, a unit masked by
    a maintainer script, the usr-merge links, the boot links and GRUB files, a compiled module,
    a unit with its enable link, and one file in each tree nothing packages. A `homelab-host`
    package owns SECRET_LINK.
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
        (fs / link.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (fs / link.lstrip("/")).symlink_to(target)
    (fs / SECRET_LINK.lstrip("/")).parent.mkdir(parents=True)
    (fs / SECRET_LINK.lstrip("/")).symlink_to(SECRET_TARGET)
    (dpkg / "info" / "homelab-host.list").write_text(
        "".join(f"{p}\n" for p in (SECRET_LINK, *HOMELAB_FILES))
    )
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
    units.mkdir(parents=True, exist_ok=True)
    (units / "tool-cleanup.service").symlink_to("/dev/null")
    # The usr-merge links, as a merged host has them: relative, to the usr counterpart.
    for name in ("bin", "sbin", "lib"):
        (fs / name).symlink_to(f"usr/{name}")
    boot = fs / "boot"
    (boot / "grub").mkdir(parents=True)
    (boot / "initrd.img-7.0.0-42-generic").write_text("initrd\n")
    (boot / "vmlinuz-7.0.0-42-generic").write_text("kernel\n")
    (boot / "initrd.img").symlink_to("initrd.img-7.0.0-42-generic")
    (boot / "vmlinuz").symlink_to("vmlinuz-7.0.0-42-generic")
    (boot / "grub" / "grub.cfg").write_text("menuentry\n")
    (boot / "grub" / "grubenv").write_text("saved_entry=0\n")
    pycache = fs / "usr/lib/python3/dist-packages/toolpkg/__pycache__"
    pycache.mkdir()
    (pycache / "mod.cpython-312.pyc").write_bytes(b"pyc")
    # The k3s installer's unit, and the link `systemctl enable` makes to it and to a package's.
    system = fs / "etc/systemd/system"
    (system / "multi-user.target.wants").mkdir(parents=True)
    (system / "k3s.service").write_text("[Service]\n")
    (system / "multi-user.target.wants" / "k3s.service").symlink_to("../k3s.service")
    (system / "multi-user.target.wants" / "tool.service").symlink_to(
        "../../../../usr/lib/systemd/system/tool.service"
    )
    for path in (
        "/usr/sbin/implant",
        "/etc/homelab/deployed.env",
        "/etc/sudoers.d/extra",
        "/usr/local/bin/aide-check.sh",
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
):
    """Render aide-check.sh and run it with aide stubbed to print `report` and exit `rc`.

    Only absolute paths are repointed: the lock file, the nice/ionice/aide invocation, the AIDE database directory, and the dpkg
    database and filesystem root `_fake_host` writes. `lock_held` holds the lock from this
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

    lock_file = tmp_path / "aide-check.lock"
    report_file = tmp_path / "report"
    report_file.write_text(report)
    aide = write_exec(
        tmp_path / "aide",
        f'touch "$AIDE_RAN"\ncat {report_file}\n'
        f'[ {rc} -le 7 ] && echo "this week" > {aide_dir}/aide.db.new\nexit {rc}\n',
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
            stub_bin=fake_bin(tmp_path / "bin", logger=LOGGER_STUB, **(stubs or {})),
        )
    (tmp_path / "stderr").write_text(result.stderr)
    lines = logged.read_text().splitlines() if logged.exists() else []
    return result.returncode, verdict_of(lines, "aide-check"), lines, ran.exists()
