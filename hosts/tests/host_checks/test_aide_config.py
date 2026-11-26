"""Guards on what AIDE scans and which scan owns account changes.

The aide-scan component's aide.conf selects only the trees aide-check pages on, so a sample of
paged paths must be selected and a sample of churning, never-paged paths must not. A host that
runs AIDE turns off rkhunter's passwd and group tests; daniel-pi, with no AIDE, keeps them.

Run: uv run pytest hosts/tests/host_checks/test_aide_config.py
"""

import re

from _host_packages import HOSTS, package, shipped_text

AIDE_CONF = "/usr/lib/homelab/aide.conf"
AIDE_CHECK = "/usr/lib/homelab/aide-check"
RKHUNTER_DROP_IN = "/etc/rkhunter.d/homelab-aide-owns-accounts.conf"

PAGED = (
    "/etc/passwd",
    "/etc/sudoers.d/90-cloud-init-users",
    "/etc/rancher/k3s/registries.yaml",
    "/etc/systemd/system/sockets.target.wants/homelab-rootq.socket",
    "/usr/bin/ls",
    "/usr/local/sbin/homelab-takeover-check",
    "/usr/local/bin/kubectl",
    "/bin",
    "/lib",
    "/boot/vmlinuz",
    "/var/spool/cron/crontabs/root",
    "/root/.bashrc",
    "/root/.ssh/authorized_keys",
    "/home/ubuntu/.ssh/authorized_keys",
    "/var/lib/claude/.ssh/authorized_keys",
)
NOT_PAGED = (
    "/home/ubuntu/server/.claude/worktrees/fix-5785/.venv/bin/python",
    "/home/ubuntu/.cache/uv/archive-v0/x/lib.py",
    "/home/ubuntu/.ssh/known_hosts",
    "/var/lib/claude/.cache/prek/repoabc/hook.py",
    "/var/lib/longhorn/replicas/pvc-1/volume-head-000.img",
    "/var/lib/rancher/k3s/agent/containerd/io.containerd.content.v1.content/blobs/x",
    "/var/lib/kubelet/pods/abc/volumes/x",
    "/var/log/syslog",
    "/run/k3s/containerd/io.containerd.runtime.v2.task/k8s.io/d98e30b5/rootfs/app/index.js",
    "/srv/media/media/movies/Film (2026)/Film.mkv",
    "/swap.img",
    "/root/.cache/x",
    "/usrx/bin/ls",
    "/etcetera",
)


def aide_hosts() -> list[str]:
    """The hosts whose package ships the AIDE config."""
    return [host for host in HOSTS if AIDE_CONF in package(host).files]


def rules(conf: str) -> list[tuple[str, str]]:
    """Each selection line as (`!` or `+`, Python regex), with the macros expanded.

    AIDE reads `\\\\` in its config as one backslash and anchors a rule at the path's start.
    """
    defines = dict(re.findall(r"^@@define (\w+) (.+)$", conf, re.M))
    out = []
    for line in conf.splitlines():
        if not line.startswith(("/", "!/")):
            continue
        regex = line.split()[0].replace("\\\\", "\\")
        regex = re.sub(r"@@\{(\w+)\}", lambda m: defines[m.group(1)], regex)
        regex = regex.replace("[:xdigit:]", "0-9a-fA-F")
        out.append(("!", regex[1:]) if regex.startswith("!") else ("+", regex))
    return out


def selected(path: str, conf: str) -> bool:
    """Whether a selection line in `conf` matches `path` and no `!` line does."""
    hits = {sign for sign, regex in rules(conf) if re.match(regex, path)}
    return "+" in hits and "!" not in hits


def paged_by_the_check(path: str, script: str) -> bool:
    """Whether aide-check's PERSIST, SYSTEM or /etc scope covers `path`."""
    persist, system = re.findall(r"^(?:PERSIST|SYSTEM)='(.+)'$", script, re.M)
    return bool(
        re.match(persist, path) or re.match(system, path) or path.startswith("/etc/")
    )


def test_every_paged_path_is_scanned_and_no_other_is():
    for host in aide_hosts():
        conf, script = shipped_text(host, AIDE_CONF), shipped_text(host, AIDE_CHECK)
        assert [p for p in PAGED if not paged_by_the_check(p, script)] == []
        assert [p for p in PAGED if not selected(p, conf)] == [], host
        assert [p for p in NOT_PAGED if selected(p, conf)] == [], host


VFAT_PATHS = (
    "/boot/efi/EFI/ubuntu",
    "/boot/efi/EFI/ubuntu/grubx64.efi",
    "/boot/firmware",
    "/boot/firmware/README",
    "/boot/firmware/overlays/pwm.dtbo",
)
# ext4 neighbours whose inode number stays a real signal.
INODE_CHECKED = ("/boot/vmlinuz", "/boot/firmware-old", "/boot/initrd.img")


def _inode_skipped(path: str, conf: str) -> bool:
    """Whether a selection line in `conf` that matches `path` gives it a rule ending in `-i`."""
    lines = [line for line in conf.splitlines() if line.startswith("/")]
    return any(
        re.match(line.split()[0].replace("\\\\", "\\"), path)
        and line.split()[-1].endswith("-i")
        for line in lines
    )


def test_every_vfat_path_skips_its_inode_and_its_ext4_neighbours_do_not():
    conf = shipped_text("daniel-box", AIDE_CONF)
    assert [p for p in VFAT_PATHS if not _inode_skipped(p, conf)] == []
    assert [p for p in INODE_CHECKED if _inode_skipped(p, conf)] == []


def test_rkhunter_skips_account_changes_only_where_aide_judges_them():
    for host in HOSTS:
        files = package(host).files
        if host in aide_hosts():
            disabled = re.findall(
                r"^DISABLE_TESTS=(.+)$", shipped_text(host, RKHUNTER_DROP_IN), re.M
            )
            assert disabled == ["passwd_changes group_changes"], host
        else:
            assert RKHUNTER_DROP_IN not in files, host
