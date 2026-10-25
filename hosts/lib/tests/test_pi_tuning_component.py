"""The optimize_pi role's subjects as packaged components on daniel-pi (H7.25).

`pi-tuning`, `pi-node-exporter`, `pi-resolver` and `pi-peer-backup-target` replace the
`optimize_pi` and `config_files` roles and `k8s_host_files`' two Pi tasks. These tests hold what
the move added: each edit adopts the line Ansible wrote, the memory and log numbers still fit the
128 MB log2ram tmpfs, the patched Log2Ram differs from upstream only by `--sparse`, and the
textfile directory exists with M3's owner contract. The unit tests of the node exporter and the
forced command stay beside the code they read: `ansible/tests/setup/` and `ansible/tests/services/`.

Run: uv run pytest hosts/lib/tests/test_pi_tuning_component.py
"""

import difflib
import re
import shutil
import subprocess

import pytest

from lib import yaml_fast
from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

COMPONENTS = REPO / "hosts" / "components"
LOG2RAM_CONF = "/etc/log2ram.conf"
JOURNALD_CAP = "/etc/systemd/journald.conf.d/60-homelab-pi.conf"
APT_DROPINS = {
    "apt-daily.timer": "/usr/lib/systemd/system/apt-daily.timer.d/50-homelab.conf",
    "apt-daily-upgrade.timer": "/usr/lib/systemd/system/apt-daily-upgrade.timer.d/50-homelab.conf",
}
QUIET_WINDOW = range(3, 6)  # 03:00-05:59 UTC, between the midnight cluster and 06:00
_MB = {"K": 1 / 1024, "M": 1, "G": 1024}


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-pi")


def _text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def _edits(package: host_mode.Package, path: str) -> dict[str, dict]:
    """The package's edits of `path`, by the line each declares."""
    return {e["line"]: e for e in package.lines if e["path"] == path}


def _mb(value: str) -> float:
    match = re.fullmatch(r"(\d+)([KMG])", value.strip())
    assert match, f"cannot read {value!r} as a size"
    return int(match.group(1)) * _MB[match.group(2)]


# Log2Ram's patched copy.


def sparse_problems(upstream: str, patched: str) -> list[str]:
    """Every way the packaged log2ram differs from upstream beyond dropping `--sparse`."""
    problems = []
    diff = [
        line
        for line in difflib.unified_diff(
            upstream.splitlines(), patched.splitlines(), lineterm="", n=0
        )
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]
    removed = [line[1:] for line in diff if line[0] == "-"]
    added = [line[1:] for line in diff if line[0] == "+"]
    if len(removed) != 2 or len(added) != 2:
        problems.append(
            f"{len(removed)} lines removed and {len(added)} added, not 2 and 2"
        )
    for old, new in zip(removed, added, strict=False):
        if old.replace(" --sparse", "", 1) != new or "--inplace" not in new:
            problems.append(f"{new.strip()!r} is not {old.strip()!r} less ' --sparse'")
    return problems


def test_the_packaged_log2ram_differs_from_upstream_only_by_the_two_sparse_words():
    upstream = (COMPONENTS / "pi-tuning/vendor/log2ram-1.7.2").read_text()
    patched = (COMPONENTS / "pi-tuning/templates/log2ram").read_text()
    assert sparse_problems(upstream, patched) == []
    assert patched.count("--inplace --no-whole-file --delete-after") == 2


def test_a_copy_that_drops_inplace_or_edits_another_line_is_flagged():
    upstream = (COMPONENTS / "pi-tuning/vendor/log2ram-1.7.2").read_text()
    no_inplace = upstream.replace(" --sparse --inplace", "")
    assert sparse_problems(upstream, no_inplace) != []
    other = upstream.replace(" --sparse", "").replace("USE_RSYNC", "USE_CP", 1)
    assert sparse_problems(upstream, other) != []


def test_log2ram_runs_the_packaged_copy_and_no_live_restart_follows_a_change(package):
    dropin = _text(package, "/usr/lib/systemd/system/log2ram.service.d/50-homelab.conf")
    for verb in ("start", "stop"):
        assert f"Exec{verb.title()}=/usr/lib/homelab/log2ram {verb}\n" in dropin
    assert "ExecReload=/usr/lib/homelab/log2ram write\n" in dropin
    # Each assignment resets the packaged command first, or systemd would run both.
    assert dropin.count("ExecStart=\n") == dropin.count("ExecStop=\n") == 1
    assert package.files["/usr/lib/homelab/log2ram"][1] == 0o755
    assert "/usr/local/bin/log2ram" not in package.files
    # Restarting log2ram unmounts /var/log under whatever is writing to it.
    assert package.policies["log2ram.service"] == "manual"


# The line arm: each edit adopts what Ansible wrote.

# (path, the line Ansible's task wrote or the stock line it replaces, the action)
ADOPTED = [
    ("/boot/firmware/config.txt", "gpu_mem=16", "reboot"),
    ("/boot/firmware/config.txt", "dtparam=watchdog=on", "reboot"),
    ("/etc/default/zramswap", "PERCENT=100", "reboot"),
    ("/etc/default/zramswap", "#ALGO=lz4", "reboot"),
    (LOG2RAM_CONF, "SIZE=128M", "reboot"),
    (LOG2RAM_CONF, "ZL2R=false", "reboot"),
    (LOG2RAM_CONF, "LOG_DISK_SIZE=256M", "reboot"),
    (LOG2RAM_CONF, "COMP_ALG=lz4", "reboot"),
    ("/etc/watchdog.conf", "#watchdog-device\t= /dev/watchdog", "reboot"),
    ("/etc/watchdog.conf", "max-load-1 = 24", "reboot"),
    ("/etc/sysctl.conf", "vm.swappiness=130", "sysctl"),
    ("/etc/sysctl.conf", "vm.page-cluster=0", "sysctl"),
    ("/etc/audit/auditd.conf", "max_log_file = 8", "try-restart:auditd.service"),
    ("/etc/audit/auditd.conf", "num_logs = 5", "try-restart:auditd.service"),
]


@pytest.mark.parametrize(("path", "stock", "action"), ADOPTED)
def test_each_edit_replaces_the_line_ansible_left_and_names_its_activation(
    package, path, stock, action
):
    matching = [
        e for e in package.lines if e["path"] == path and re.search(e["regexp"], stock)
    ]
    assert len(matching) == 1, f"{path}: {len(matching)} edits match {stock!r}"
    assert re.search(matching[0]["regexp"], matching[0]["line"])
    assert package.activations[path] == action


def test_the_size_anchor_leaves_log_disk_size_alone(package):
    edits = _edits(package, LOG2RAM_CONF)
    size = edits["SIZE=128M"]["regexp"]
    assert re.search(size, "#SIZE=40M") and not re.search(size, "LOG_DISK_SIZE=256M")


def test_the_zram_swappiness_prefers_swap_and_no_drop_in_overrides_it(package):
    edits = _edits(package, "/etc/sysctl.conf")
    assert int(edits["vm.swappiness=130"]["line"].split("=")[1]) > 100
    # /etc/sysctl.conf is read last, so a sysctl.d file declaring a key would be overridden.
    drop_ins = [
        dst for dst in package.files if re.match(r"/(etc|usr/lib)/sysctl\.d/", dst)
    ]
    assert not any("vm." in _text(package, dst) for dst in drop_ins), drop_ins


# The log RAM budget.


def _journal_caps(package: host_mode.Package) -> tuple[float, float]:
    """The journal's SystemMaxUse and its SD-card worst case, SystemMaxFiles x SystemMaxFileSize."""
    text = _text(package, JOURNALD_CAP)
    use = re.search(r"^SystemMaxUse=(\d+[KMG])$", text, re.M)
    size = re.search(r"^SystemMaxFileSize=(\d+[KMG])$", text, re.M)
    files = re.search(r"^SystemMaxFiles=(\d+)$", text, re.M)
    assert use and size and files, (
        "the cap needs SystemMaxUse, SystemMaxFileSize and SystemMaxFiles"
    )
    return _mb(use.group(1)), int(files.group(1)) * _mb(size.group(1))


def test_the_journal_fits_the_log2ram_tmpfs_and_its_sd_copy_leaves_half_for_the_rest(
    package,
):
    tmpfs = _mb(_edits(package, LOG2RAM_CONF)["SIZE=128M"]["line"].split("=")[1])
    use, sd_worst = _journal_caps(package)
    assert use < tmpfs
    # log2ram's boot guard `du`s the SD copy against SIZE and refuses to start when it is over
    # (#4583). `--sparse` is patched out, so every archive lands there at its full size.
    assert sd_worst <= tmpfs / 2


def test_a_journal_cap_without_a_file_count_reads_as_unbounded():
    text = "[Journal]\nSystemMaxUse=32M\nSystemMaxFileSize=8M\n"
    assert not re.search(r"^SystemMaxFiles=\d+$", text, re.M)


# The apt timers.


def dropin_problem(content: str) -> str | None:
    """The failure message for a timer drop-in's content, else None."""
    lines = [
        ln.strip()
        for ln in content.splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    calendars = [ln for ln in lines if ln.startswith("OnCalendar=")]
    if "[Timer]" not in lines:
        return "no [Timer] section"
    if not calendars or calendars[0] != "OnCalendar=":
        return "the first OnCalendar= is not empty, so the packaged schedule stays live too"
    if len(calendars) != 2:
        return (
            f"expected exactly one schedule after the reset, found {len(calendars) - 1}"
        )
    return None


def test_both_apt_timers_reset_before_they_schedule_inside_the_quiet_window(package):
    at = {}
    for unit, dst in APT_DROPINS.items():
        text = _text(package, dst)
        assert dropin_problem(text) is None, unit
        found = re.search(r"^OnCalendar=\*-\*-\* (\d\d):(\d\d)$", text, re.M)
        assert found, unit
        hour, minute = found.groups()
        assert int(hour) in QUIET_WINDOW, f"{unit} fires at {hour}:{minute}"
        at[unit] = f"{hour}:{minute}"
    assert at["apt-daily.timer"] < at["apt-daily-upgrade.timer"], (
        "update runs before upgrade"
    )


def test_a_drop_in_that_only_adds_a_schedule_is_flagged():
    assert dropin_problem("[Timer]\nOnCalendar=*-*-* 04:20\n") is not None


# earlyoom.


def test_earlyoom_keeps_a_non_binding_swap_guard_and_protects_the_processes_that_start_containers(
    package,
):
    dropin = _text(
        package, "/usr/lib/systemd/system/earlyoom.service.d/50-homelab.conf"
    )
    assert "\nEnvironmentFile=\n" in dropin
    (args,) = re.findall(r'^Environment="EARLYOOM_ARGS=(.*)"$', dropin, re.M)
    # `-s 100,100` makes both swap thresholds always true, so memory alone decides.
    assert " -s 100,100 " in args and args.startswith("-m 10 ")
    found = re.search(r"--avoid '([^']*)'", args)
    assert found
    avoid = found.group(1)
    for name in ("dbus-daemon", "dockerd", "containerd", "sshd", "watchdog", "systemd"):
        assert name in avoid, name


# The hostname line, the resolver and the shell profile.


def test_the_hostname_line_is_anchored_on_the_loopback_address(package):
    (edit,) = [e for e in package.lines if e["path"] == "/etc/hosts"]
    assert edit["line"] == "127.0.1.1 daniel-pi"
    # A stale name on the same address is rewritten, not duplicated beside it.
    assert re.search(edit["regexp"], "127.0.1.1 old-name")
    assert not re.search(edit["regexp"], "127.0.0.1 localhost")


def test_the_profile_exports_the_sops_age_key_path(package):
    assert 'export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"' in _text(
        package, "/etc/profile.d/homelab.sh"
    )


# pi-peer-backup's key and the interim tokens.


def test_the_key_pins_the_directory_the_cronjob_pulls(package):
    user = render_context(None, "daniel-pi", inventory=REPO_INVENTORY)["sys_user"]
    (edit,) = [
        e for e in package.lines if e["path"].endswith("/.ssh/authorized_" + "keys")
    ]
    k8s = yaml_fast.safe_load(
        (REPO / "ansible/roles/k8s/pi-peer-backup/defaults/main.yml").read_text()
    )["pi_peer_backup_src_dir"]
    pinned = re.search(
        r'command="/usr/local/bin/pi-peer-backup-shell (\S+)"', edit["line"]
    )
    assert pinned and pinned.group(1) == k8s.replace("{{ sys_user }}", user)


def test_the_package_retires_the_interim_token_files_nothing_reads(package):
    paths = {e["path"] for e in package.legacy if "path" in e}
    assert {
        "/etc/homelab/interim/pi_sd_health_push_token",
        "/etc/homelab/interim/pi_recovery_push_token",
    } <= paths
    # The units read the tokens from /run/credstore instead.
    for dst, (data, _) in package.files.items():
        if dst.startswith("/usr/lib/systemd/system/homelab-pi-"):
            assert b"/etc/homelab/interim" not in data, dst


# The node exporter's textfile directory.


@pytest.mark.skipif(
    not shutil.which("systemd-tmpfiles"), reason="needs systemd-tmpfiles"
)
@pytest.mark.parametrize("line_kept", [True, False], ids=["green", "red-no-d-line"])
def test_tmpfiles_creates_the_textfile_directory_setgid_and_the_updater_can_write_there(
    package, tmp_path, line_kept
):
    import os

    config = tmp_path / "homelab-node-exporter.conf"
    root = tmp_path / "root"
    root.mkdir()
    text = _text(package, "/usr/lib/tmpfiles.d/homelab-node-exporter.conf")
    # Unprivileged run: the owner and group become the caller's, as numbers, because `--root`
    # resolves names in the root's own passwd file. The mode is the contract's own.
    owner, group = os.getuid(), os.getgid()
    config.write_text(
        re.sub(
            r"^(d\s+\S+\s+2775)\s+\S+\s+\S+", rf"\1 {owner} {group}", text, flags=re.M
        )
        if line_kept
        else "# the d line is gone\n"
    )
    subprocess.run(
        ["systemd-tmpfiles", "--create", f"--root={root}", str(config)],
        check=True,
        capture_output=True,
    )

    directory = root / "var/lib/node-exporter-textfile"
    # The updater stub writes homelab_host.prom and exits non-zero when the directory is missing.
    stub = subprocess.run(
        ["sh", "-c", f"printf 'x 1\\n' > {directory}/homelab_host.prom"],
        capture_output=True,
        check=False,
    )
    assert (stub.returncode == 0) is line_kept
    if line_kept:
        assert directory.stat().st_mode & 0o7777 == 0o2775
        assert (directory / "homelab_host.prom").is_file()


def test_the_tmpfiles_line_is_m3s_owner_contract(package):
    user = render_context(None, "daniel-pi", inventory=REPO_INVENTORY)["sys_user"]
    text = _text(package, "/usr/lib/tmpfiles.d/homelab-node-exporter.conf")
    lines = [ln.split() for ln in text.splitlines() if ln.startswith("d ")]
    assert lines == [
        ["d", "/var/lib/node-exporter-textfile", "2775", "root", user, "-"]
    ]


def test_the_component_directories_hold_only_files_the_specs_ship():
    """A vendored upstream file is the one thing a component directory holds and never ships."""
    shipped = set()
    for name in (
        "pi-tuning",
        "pi-node-exporter",
        "pi-resolver",
        "pi-peer-backup-target",
    ):
        spec = yaml_fast.safe_load((COMPONENTS / name / "component.yml").read_text())
        shipped |= {
            (COMPONENTS / name / e["src"]).resolve()
            for e in spec.get("files", [])
            if not e["src"].startswith("role:")
        }
    on_disk = {
        p.resolve()
        for name in ("pi-tuning", "pi-node-exporter", "pi-peer-backup-target")
        for p in (COMPONENTS / name).rglob("*")
        if p.is_file() and p.name != "component.yml"
    }
    assert {p.name for p in on_disk - shipped} == {
        "log2ram-1.7.2",
        "pi-peer-backup.pub",
    }
