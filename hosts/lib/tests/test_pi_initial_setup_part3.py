"""The Pi's host baseline as the `host-hardening` component (H7.58).

The component replaces initial_setup's access, accounting, audit-and-kernel and integrity tasks
and the package tasks of host-basics on daniel-pi. H7.60 deleted those tasks, so `WRITTEN` and the
constants beside it carry what they wrote. These tests hold the shipped bytes to them, and the
line-arm edits that adopt them.

Run: uv run pytest hosts/lib/tests/test_pi_initial_setup_part3.py
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from reconcile_lib import lines
from renderer.renderer_lib import host as host_mode

SUDOERS = "/etc/sudoers.d/10-timestamp"
BLACKLIST = "/etc/modprobe.d/blacklist-rare-network.conf"
TMPFILES = "/usr/lib/tmpfiles.d/homelab-hardening.conf"
KEEP_CONFFILES = "/etc/apt/apt.conf.d/53homelab-keep-conffiles"
BANNER = (
    "WARNING: Unauthorized access to this system is prohibited.\n"
    "All connections are monitored and recorded.\n"
    "Disconnect immediately if you are not an authorized user.\n"
)
# The bytes initial_setup's tasks wrote to each path before H7.60 deleted them.
WRITTEN = {
    SUDOERS: (
        "# Managed by Ansible (roles/setup/initial_setup) — edits here are overwritten.\n"
        "Defaults timestamp_type=global, timestamp_timeout=60\n"
    ),
    "/etc/audit/rules.d/hardening.rules": (
        "# Identity file changes\n"
        "-w /etc/passwd -p wa -k identity\n"
        "-w /etc/group -p wa -k identity\n"
        "-w /etc/shadow -p wa -k identity\n"
        "# SSH configuration changes\n"
        "-w /etc/ssh/sshd_config -p wa -k sshd\n"
        "# Privilege escalation changes\n"
        "-w /etc/sudoers -p wa -k actions\n"
        "-w /etc/sudoers.d -p wa -k actions\n"
    ),
    "/etc/modules-load.d/wireguard.conf": "wireguard\n",
    "/etc/systemd/coredump.conf.d/disable.conf": (
        "[Coredump]\nStorage=none\nProcessSizeMax=0\n"
    ),
}
BLACKLIST_WRITTEN = (
    "install dccp /bin/true\n"
    "install sctp /bin/true\n"
    "install rds /bin/true\n"
    "install tipc /bin/true\n"
)
# `Restrict sensitive file permissions`, path to mode.
RESTRICTED = {
    "/etc/ssh/sshd_config": "0600",
    "/etc/crontab": "0600",
    "/boot/grub/grub.cfg": "0600",
    "/etc/cron.d": "0700",
    "/etc/cron.daily": "0700",
    "/etc/cron.hourly": "0700",
    "/etc/cron.weekly": "0700",
    "/etc/cron.monthly": "0700",
}
# `Install required packages`, `Install Raspberry Pi-specific packages` and `Install auditd`.
INSTALLED = {
    *("unzip", "unattended-upgrades", "update-notifier-common", "fail2ban"),
    *("libpam-tmpdir", "debsums", "jq", "linux-raspi", "wireguard", "auditd"),
}


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-pi")


def _edits(package: host_mode.Package, path: str) -> list[dict]:
    return [edit for edit in package.lines if edit["path"] == path]


@pytest.mark.parametrize(
    "dst, mode",
    [
        (SUDOERS, 0o440),
        ("/etc/audit/rules.d/hardening.rules", 0o640),
        ("/etc/modules-load.d/wireguard.conf", 0o644),
        ("/etc/systemd/coredump.conf.d/disable.conf", 0o644),
    ],
)
def test_each_shipped_file_holds_the_bytes_and_mode_its_task_wrote(package, dst, mode):
    data, shipped_mode = package.files[dst]
    assert data.decode() == WRITTEN[dst]
    assert shipped_mode == mode


def test_visudo_accepts_the_sudoers_file_and_rejects_a_broken_copy(package, tmp_path):
    visudo = shutil.which("visudo") or "/usr/sbin/visudo"
    if not Path(visudo).exists():
        pytest.skip("needs a visudo")
    version = subprocess.run(
        [visudo, "--version"], capture_output=True, text=True, check=False
    ).stdout
    # The hosts run classic sudo. The ubuntu-26.04 runner's visudo rejects `timestamp_type`.
    if not version.startswith("visudo version"):
        pytest.skip(f"the hosts' classic visudo is not here: {version.strip()!r}")
    good = tmp_path / "10-timestamp"
    good.write_bytes(package.files[SUDOERS][0])
    good.chmod(0o440)
    check = [visudo, "-cf"]
    assert subprocess.run([*check, str(good)], check=False).returncode == 0
    broken = tmp_path / "broken"
    broken.write_bytes(package.files[SUDOERS][0] + b"Defaults =\n")
    broken.chmod(0o440)
    assert subprocess.run([*check, str(broken)], check=False).returncode != 0


def test_a_live_banner_is_left_alone_and_a_stock_one_is_replaced(package):
    edits = _edits(package, "/etc/issue")
    live, stock = BANNER, "Ubuntu 24.04.3 LTS \\n \\l\n\n"
    for edit in edits:
        live = lines.with_line(live, edit)
        stock = lines.with_line(stock, edit)
    assert live == BANNER
    assert stock == BANNER.replace("\n", "\n\n", 1)
    assert not _edits(package, "/etc/issue.net")


def test_the_blacklist_edits_adopt_the_lines_the_task_wrote(package):
    edits = _edits(package, BLACKLIST)
    written = BLACKLIST_WRITTEN
    assert [edit["line"] + "\n" for edit in edits] == written.splitlines(keepends=True)
    adopted = written
    for edit in edits:
        adopted = lines.with_line(adopted, edit)
    assert adopted == written
    # kmod owns this path as a conffile, so a file entry would fail dpkg's unpack.
    assert BLACKLIST not in package.files


@pytest.mark.skipif(not shutil.which("apt-config"), reason="needs apt's apt-config")
def test_apt_parses_the_conffile_option_unattended_upgrade_requires(package, tmp_path):
    # Without --force-confold or --force-confnew, unattended-upgrade skips a package whose conffile
    # would prompt, so base-files and kmod would wait for a hand run.
    conf = tmp_path / "keep.conf"
    conf.write_bytes(package.files[KEEP_CONFFILES][0])
    dump = subprocess.run(
        ["apt-config", "-c", str(conf), "dump"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert 'DPkg::Options:: "--force-confold";' in dump


def test_the_tmpfiles_lines_cover_the_ssh_directory_and_every_restricted_path(package):
    rows = [
        line.split()
        for line in package.files[TMPFILES][0].decode().splitlines()
        if line and not line.startswith("#")
    ]
    assert rows[0] == ["Z", "/home/ubuntu/.ssh", "~0700", "ubuntu", "ubuntu", "-"]
    assert {(row[1], row[2]) for row in rows if row[0] == "z"} == set(
        RESTRICTED.items()
    )


def test_the_package_depends_on_what_the_package_tasks_installed(package):
    assert INSTALLED <= set(package.depends)
