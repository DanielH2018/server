"""The Pi's host baseline as the `host-hardening` component.

The component replaces initial_setup's access, accounting, audit-and-kernel and integrity tasks
and the package tasks of host-basics on daniel-pi. Those tasks are gone, so `WRITTEN` and the
constants beside it carry what they wrote. These tests hold the shipped bytes to them, and the
line-arm edits that adopt them.

Run: uv run pytest hosts/lib/tests/test_pi_host_hardening.py
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from reconcile_lib import lines
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.host_manifest import MANIFEST

SUDOERS = "/etc/sudoers.d/10-timestamp"
BLACKLIST = "/etc/modprobe.d/blacklist-rare-network.conf"
TMPFILES = "/usr/lib/tmpfiles.d/homelab-hardening.conf"
KEEP_CONFFILES = "/etc/apt/apt.conf.d/53homelab-keep-conffiles"
APPORT_DROPIN = "/usr/lib/systemd/system/apport.service.d/50-homelab.conf"
BANNER = (
    "WARNING: Unauthorized access to this system is prohibited.\n"
    "All connections are monitored and recorded.\n"
    "Disconnect immediately if you are not an authorized user.\n"
)
# The bytes initial_setup's tasks wrote to each path.
WRITTEN = {
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
# The one directive in SUDOERS; the file's comment header is the component's own. It sets no
# timestamp type, because sudo-rs, 26.04's default sudo, refuses that setting.
SUDOERS_DIRECTIVE = "Defaults timestamp_timeout=60"
# Where each visudo lives: classic sudo's on 24.04, classic beside sudo-rs on 26.04, sudo-rs's.
VISUDO_PATHS = ("/usr/sbin/visudo", "/usr/sbin/visudo.ws", "/usr/lib/cargo/bin/visudo")
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
        ("/etc/audit/rules.d/hardening.rules", 0o640),
        ("/etc/modules-load.d/wireguard.conf", 0o644),
        ("/etc/systemd/coredump.conf.d/disable.conf", 0o644),
    ],
)
def test_each_shipped_file_holds_the_bytes_and_mode_its_task_wrote(package, dst, mode):
    data, shipped_mode = package.files[dst]
    assert data.decode() == WRITTEN[dst]
    assert shipped_mode == mode


def sudoers_directives(data: bytes) -> list[str]:
    return [ln for ln in data.decode().splitlines() if ln and not ln.startswith("#")]


def test_the_sudoers_file_holds_only_the_timeout_directive(package):
    data, shipped_mode = package.files[SUDOERS]
    assert sudoers_directives(data) == [SUDOERS_DIRECTIVE]
    assert shipped_mode == 0o440


def _visudo(implementation: str) -> str | None:
    """The path of the visudo whose `--version` opens with `implementation`, if one is here."""
    for path in VISUDO_PATHS:
        if not Path(path).exists():
            continue
        version = subprocess.run(
            [path, "--version"], capture_output=True, text=True, check=False, timeout=60
        ).stdout
        if version.split(" ", 1)[0] == implementation:
            return path
    return None


# Classic sudo prints `visudo version` on 24.04 and `visudo.ws version` beside sudo-rs on 26.04.
@pytest.mark.parametrize("implementation", ["visudo", "visudo.ws", "visudo-rs"])
def test_visudo_accepts_the_sudoers_file_and_rejects_a_broken_copy(
    package, tmp_path, implementation
):
    visudo = _visudo(implementation)
    if visudo is None:
        pytest.skip(f"no {implementation} here")
    good = tmp_path / "10-timestamp"
    good.write_bytes(package.files[SUDOERS][0])
    good.chmod(0o440)
    check = [visudo, "-cf"]
    assert subprocess.run([*check, str(good)], check=False, timeout=60).returncode == 0
    broken = tmp_path / "broken"
    broken.write_bytes(package.files[SUDOERS][0] + b"Defaults =\n")
    broken.chmod(0o440)
    assert (
        subprocess.run([*check, str(broken)], check=False, timeout=60).returncode != 0
    )


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
        timeout=60,
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


@pytest.mark.parametrize("host", ["daniel-box", "daniel-server", "daniel-pi"])
def test_every_host_drops_in_on_apport_and_reconcile_restarts_it(host):
    # A unit with a drop-in and no policy is restarted when the drop-in lands, so apport's stop
    # writes fs.suid_dumpable 0 without waiting for a reboot.
    rendered = host_mode.render_host(host)
    assert APPORT_DROPIN in rendered.files
    units = json.loads(rendered.files[MANIFEST][0])["units"]
    assert units["apport.service"] == {}


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="needs systemd-analyze")
def test_the_apport_dropin_condition_never_holds(package):
    conditions = [
        line
        for line in package.files[APPORT_DROPIN][0].decode().splitlines()
        if line.startswith("Condition")
    ]
    result = subprocess.run(
        ["systemd-analyze", "condition", *conditions],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert conditions
    # A line systemd cannot parse also exits 1, so each condition must be reported as failed.
    for condition in conditions:
        assert f"{condition} failed." in result.stderr, result.stderr
