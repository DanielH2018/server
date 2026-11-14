"""The rkhunter baseline refresh after every package change, outside apt's lock (#7680).

rkhunter's own APT_AUTOGEN hook ran `rkhunter --propupd` inside apt's DPkg::Post-Invoke, so apt
held the dpkg lock for about 2 minutes on the Pi after every package change. The integrity-scans
component turns APT_AUTOGEN off and ships a hook that starts homelab-rkhunter-propupd.service
without blocking. The unit's script waits for apt to release the lock, refreshes, and refreshes
again when dpkg changed packages during the refresh, because a start while the unit is active
does nothing. A needrestart override keeps apt's own needrestart hook from restarting the unit
while it waits, which deadlocked daniel-box on 2026-11-14. A dpkg run that changed only
homelab-* packages keeps the baseline (#8650), which is safe only while no host package ships a
file rkhunter records; the census below holds that.

Run: uv run pytest hosts/tests/test_rkhunter_propupd.py
"""

import json
import os
from pathlib import Path

import pytest

from lib.proc_testing import fake_bin, run, write_exec
from lib.repo_paths import REPO
from _host_packages import HOSTS, package, shipped_text

SCRIPT = (
    REPO / "hosts" / "components" / "integrity-scans" / "files" / "rkhunter-propupd"
)
HOOK = "/etc/apt/apt.conf.d/91homelab-rkhunter-propupd"
UNIT = "homelab-rkhunter-propupd.service"
NEEDRESTART_OVERRIDE = "/etc/needrestart/conf.d/homelab-rkhunter-propupd.conf"


@pytest.mark.parametrize("host", HOSTS)
def test_every_host_refreshes_from_its_own_hook_and_not_from_apt_autogen(host):
    manifest = json.loads(package(host).files["/usr/lib/homelab/manifest.json"][0])
    autogen = [
        e["line"]
        for e in manifest["lines"]["edits"]
        if e["path"] == "/etc/default/rkhunter"
    ]
    assert autogen == ['APT_AUTOGEN="false"']
    assert f"systemctl start --no-block {UNIT}" in shipped_text(host, HOOK)
    unit = shipped_text(host, f"/usr/lib/systemd/system/{UNIT}")
    assert "ExecStart=/usr/lib/homelab/rkhunter-propupd\n" in unit


@pytest.mark.parametrize("host", HOSTS)
def test_needrestart_never_restarts_the_refresh_unit_that_waits_on_apts_lock(host):
    override = shipped_text(host, NEEDRESTART_OVERRIDE)
    assert (
        r"$nrconf{override_rc}{qr(^homelab-rkhunter-propupd\.service$)} = 0;"
        in override
    )


RKHUNTER_DEFAULT_BINDIR = frozenset(
    "/bin /usr/bin /sbin /usr/sbin /usr/local/bin /usr/local/sbin /usr/libexec "
    "/usr/local/libexec".split()
)
# rkhunter 1.4.6's PROP_FILE_LIST with its Linux additions, from /usr/bin/rkhunter.
RKHUNTER_PROP_FILE_LIST = frozenset(
    """adduser amd awk basename bash bget cat chattr checkproc chkconfig chmod chown chroot cp
    cron csh curl cut date depmod df diff dirname dmesg dpkg dpkg-query du echo ed egrep elinks
    env fgrep file find fsck fuser GET grep groupadd groupdel groupmod groups grpck head id
    ifconfig ifdown ifstatus ifup inetd init insmod ip ipcs kallsyms kill killall ksyms kudzu
    last lastlog ldd less links locate logger login ls lsattr lsmod lsof lynx mail md5 md5sum
    mktemp mlocate modinfo modload modprobe modstat modunload more mount mv netstat newgrp
    newsyslog nologin passwd perl pgrep ping pkg pkgdb pkg_info pkill prelink ps pstree pwck
    pwd readlink rkhunter rmmod route rpm rsyslogd runcon runlevel sed sestatus sh sha1
    sha1sum sha224 sha224sum sha256 sha256sum sha384 sha384sum sha512 sha512sum size skdet
    slocate sockstat sort ssh sshd stat strace strings su sudo sulogin sysctl syslogd systat
    tail tcpd telnet test top touch tr uname uniq useradd userdel usermod users vipw vmstat w
    watch wc wget whatis whereis which who whoami xinetd unhide unhide-linux unhide-posix
    unhide-tcp numfmt""".split()
)


@pytest.mark.parametrize("host", HOSTS)
def test_no_host_package_ships_a_file_rkhunter_records(host):
    files = package(host).files
    assert "/usr/bin/sops" in files
    recorded = [
        path
        for path in files
        if path.rsplit("/", 1)[0] in RKHUNTER_DEFAULT_BINDIR
        and path.rsplit("/", 1)[1] in RKHUNTER_PROP_FILE_LIST
    ]
    assert recorded == []
    assert "USER_FILEPROP_FILES_DIRS" not in shipped_text(
        host, "/etc/rkhunter.conf.local"
    )
    unit = shipped_text(host, f"/usr/lib/systemd/system/{UNIT}")
    assert "StateDirectory=homelab-rkhunter-propupd\n" in unit


STATUS = "Package: homelab-daniel-pi\nVersion: 1\n\nPackage: libc6\nVersion: 2.39-8.9\n"
REFRESH = ["wait --wait-for-apt", "rkhunter --propupd --nolog --nocolors"]


def _refresh(
    tmp_path: Path, changes_during_refresh: int = 0, status_text: str = STATUS
) -> tuple[list[str], str]:
    """Run the script against stubs; the first `changes_during_refresh` refreshes rewrite status.

    The stamp, rkhunter's config and its database persist in `tmp_path`, so a second call
    sees what the first left.

    Returns:
        The calls the stubs logged during this run, in order, and the script's stdout.
    """
    calls = tmp_path / "calls"
    calls.unlink(missing_ok=True)
    calls.touch()
    status = tmp_path / "status"
    status.write_text(status_text)
    if not (tmp_path / "state").exists():
        (tmp_path / "state").mkdir()
        (tmp_path / "rkhunter.dat").touch()
        (tmp_path / "rkhunter.conf.local").write_text("ALLOW_SSH_ROOT_USER=unset\n")
    countdown = tmp_path / "countdown"
    countdown.write_text(str(changes_during_refresh))
    write_exec(tmp_path / "lib" / "homelab-reconcile", f'echo "wait $1" >> {calls}\n')
    # A rename, as dpkg writes its status file, so the inode changes.
    stub_bin = fake_bin(
        tmp_path / "bin",
        rkhunter=(
            f'echo "rkhunter $*" >> {calls}\n'
            f"n=$(cat {countdown})\n"
            f'if [ "$n" -gt 0 ]; then\n'
            f"  echo $((n - 1)) > {countdown}\n"
            f'  echo "Package: b$n" > {status}.new && mv {status}.new {status}\n'
            "fi\n"
        ),
    )
    result = run(
        ["bash", str(SCRIPT)],
        env={
            **os.environ,
            "HOMELAB_LIB_DIR": str(tmp_path / "lib"),
            "DPKG_STATUS": str(status),
            "RKHUNTER_CONF_DIR": str(tmp_path),
            "RKHUNTER_DATABASE": str(tmp_path / "rkhunter.dat"),
            "STATE_DIRECTORY": str(tmp_path / "state"),
        },
        stub_bin=stub_bin,
        check=True,
    )
    return calls.read_text().splitlines(), result.stdout


def test_a_quiet_dpkg_gets_one_refresh_after_the_wait_for_apt(tmp_path):
    calls, stdout = _refresh(tmp_path, changes_during_refresh=0)
    assert calls == REFRESH
    assert stdout.endswith("rkhunter baseline refreshed\n")


def test_a_dpkg_run_during_the_refresh_gets_a_second_refresh(tmp_path):
    calls, _ = _refresh(tmp_path, changes_during_refresh=1)
    assert calls == REFRESH * 2


def test_a_dpkg_run_that_changed_only_homelab_packages_keeps_the_baseline(tmp_path):
    _refresh(tmp_path)
    calls, stdout = _refresh(tmp_path, status_text=STATUS.replace("1\n", "2\n", 1))
    assert calls == ["wait --wait-for-apt"]
    assert stdout.endswith("only homelab-* packages changed; rkhunter baseline kept\n")


def test_a_dpkg_run_that_changed_another_package_refreshes(tmp_path):
    _refresh(tmp_path)
    calls, _ = _refresh(tmp_path, status_text=STATUS.replace("8.9", "8.12"))
    assert calls == REFRESH


def test_a_changed_rkhunter_config_refreshes(tmp_path):
    _refresh(tmp_path)
    (tmp_path / "rkhunter.conf.local").write_text("ALLOW_SSH_ROOT_USER=no\n")
    calls, _ = _refresh(tmp_path)
    assert calls == REFRESH


def test_a_missing_database_refreshes(tmp_path):
    _refresh(tmp_path)
    (tmp_path / "rkhunter.dat").unlink()
    calls, _ = _refresh(tmp_path)
    assert calls == REFRESH
