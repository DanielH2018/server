"""The Pi's host baseline as the `host-hardening` component (H7.58).

The component replaces initial_setup's access, accounting, audit-and-kernel and integrity tasks
and the package tasks of host-basics on daniel-pi. These tests hold the bytes the tasks wrote,
the line-arm edits that adopt what they wrote, and the guards that stop the role writing them.

Run: uv run pytest hosts/lib/tests/test_pi_initial_setup_part3.py
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from lib import yaml_fast
from lib.repo_paths import ANSIBLE

from deploy_tools.setup_lib.setup_gates import packaged_guard_hosts
from reconcile_lib import lines
from renderer.renderer_lib import host as host_mode

ROLE = ANSIBLE / "roles" / "setup" / "initial_setup"
SUDOERS = "/etc/sudoers.d/10-timestamp"
BLACKLIST = "/etc/modprobe.d/blacklist-rare-network.conf"
TMPFILES = "/usr/lib/tmpfiles.d/homelab-hardening.conf"
KEEP_CONFFILES = "/etc/apt/apt.conf.d/53homelab-keep-conffiles"
BANNER = (
    "WARNING: Unauthorized access to this system is prohibited.\n"
    "All connections are monitored and recorded.\n"
    "Disconnect immediately if you are not an authorized user.\n"
)


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-pi")


def _task(file: str, name: str) -> dict:
    tasks = yaml_fast.safe_load((ROLE / "tasks" / file).read_text())
    return next(task for task in tasks if task["name"] == name)


def _edits(package: host_mode.Package, path: str) -> list[dict]:
    return [edit for edit in package.lines if edit["path"] == path]


@pytest.mark.parametrize(
    "file, name, module, dst, mode",
    [
        (
            "access.yml",
            "Scope the sudo credential cache to the user rather than the terminal",
            "ansible.builtin.copy",
            SUDOERS,
            0o440,
        ),
        (
            "audit-and-kernel.yml",
            "Configure audit rules",
            "ansible.builtin.copy",
            "/etc/audit/rules.d/hardening.rules",
            0o640,
        ),
        (
            "audit-and-kernel.yml",
            "Persist the WireGuard kernel module across reboots",
            "ansible.builtin.copy",
            "/etc/modules-load.d/wireguard.conf",
            0o644,
        ),
        (
            "integrity.yml",
            "Disable core dumps via systemd coredump",
            "ansible.builtin.copy",
            "/etc/systemd/coredump.conf.d/disable.conf",
            0o644,
        ),
    ],
)
def test_each_shipped_file_holds_the_bytes_and_mode_its_task_wrote(
    package, file, name, module, dst, mode
):
    data, shipped_mode = package.files[dst]
    assert data.decode() == _task(file, name)[module]["content"]
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
    written = _task(
        "audit-and-kernel.yml", "Blacklist unused rare network kernel modules"
    )["ansible.builtin.copy"]["content"]
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
    task = _task("audit-and-kernel.yml", "Restrict sensitive file permissions")
    rows = [
        line.split()
        for line in package.files[TMPFILES][0].decode().splitlines()
        if line and not line.startswith("#")
    ]
    assert rows[0] == ["Z", "/home/ubuntu/.ssh", "~0700", "ubuntu", "ubuntu", "-"]
    assert {(row[1], row[2]) for row in rows if row[0] == "z"} == {
        (item["path"], item["mode"]) for item in task["loop"]
    }


def test_the_package_depends_on_what_the_package_tasks_installed(package):
    required = _task("host-basics.yml", "Install required packages")[
        "ansible.builtin.apt"
    ]
    raspi = _task("host-basics.yml", "Install Raspberry Pi-specific packages")[
        "ansible.builtin.apt"
    ]
    assert {*required["name"], *raspi["name"], "auditd"} <= set(package.depends)


@pytest.mark.parametrize("host", ["daniel-box", "daniel-server"])
def test_a_cluster_node_stays_on_the_role_until_its_own_pr(host):
    assert "/etc/audit/rules.d/hardening.rules" not in host_mode.render_host(host).files


@pytest.mark.parametrize(
    "import_file, subject",
    [
        ("access.yml", "access"),
        ("accounting.yml", "accounting"),
        ("audit-and-kernel.yml", "audit_and_kernel"),
        ("integrity.yml", "integrity"),
    ],
)
def test_each_whole_file_subject_skips_the_pi_at_its_import(import_file, subject):
    imports = yaml_fast.safe_load((ROLE / "tasks" / "main.yml").read_text())
    entry = next(
        task for task in imports if task["ansible.builtin.import_tasks"] == import_file
    )
    assert subject in entry["when"]
    hosts = ("daniel-pi", "daniel-box", "daniel-server")
    assert packaged_guard_hosts([entry["when"]], hosts, ROLE) == {"daniel-pi"}


def test_the_setup_apply_runs_no_apt_upgrade_on_the_pi():
    upgrade = _task("host-basics.yml", "Update and upgrade system packages")
    hosts = ("daniel-pi", "daniel-box", "daniel-server")
    assert packaged_guard_hosts([upgrade["when"]], hosts, ROLE) == {"daniel-pi"}
    tooling = _task("host-basics.yml", "Install the pinned host Python")
    assert "packaged_hosts" not in str(tooling["when"])
