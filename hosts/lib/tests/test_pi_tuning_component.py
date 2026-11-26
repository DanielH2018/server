"""The optimize_pi role's subjects as packaged components on daniel-pi.

`pi-tuning`, `pi-node-exporter`, `pi-resolver` and `pi-peer-backup-target` replace the
`optimize_pi` and `config_files` roles and `k8s_host_files`' two Pi tasks. These tests hold what
the move added: each edit adopts the line Ansible wrote, the memory and log numbers still fit the
128 MB log2ram tmpfs, the patched Log2Ram differs from upstream only by `--sparse`, and the
textfile directory exists with M3's owner contract. The unit tests of the node exporter and the
forced command stay beside the code they read: `hosts/tests/` and `tests/services/`.

Run: uv run pytest hosts/lib/tests/test_pi_tuning_component.py
"""

import difflib
import os
import re
import shutil
import subprocess

import pytest

from lib import yaml_fast
from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO
from reconcile_lib import lines
from renderer.renderer_lib import host as host_mode

COMPONENTS = REPO / "hosts" / "components"
LOG2RAM_CONF = "/etc/log2ram.conf"
JOURNALD_CAP = "/etc/systemd/journald.conf.d/60-homelab-pi.conf"
AUDITD_CONF = "/etc/audit/auditd.conf"
ACCT_LOGROTATE = "/etc/logrotate.d/acct"
WATCHDOG_RESTART = "try-restart:watchdog.service"
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
    assert "/usr/bin/log2ram" not in package.files
    # Restarting log2ram unmounts /var/log under whatever is writing to it.
    assert package.policies["log2ram.service"] == "manual"


def test_the_package_depends_on_ubuntu_s_log2ram_and_reconcile_removes_azlux_s_source(
    package,
):
    assert "log2ram" in package.depends
    legacy = {entry["path"] for entry in package.legacy if "path" in entry}
    assert {
        "/etc/apt/sources.list.d/azlux.sources",
        "/etc/apt/sources.list.d/azlux.sources.distUpgrade",
        "/usr/share/keyrings/azlux-archive-keyring.gpg",
    } <= legacy


def test_log2ram_is_held_at_every_boot_and_reconcile_once_it_is_installed(package):
    unit = "homelab-log2ram-hold.service"
    text = _text(package, f"/usr/lib/systemd/system/{unit}")
    assert "/usr/lib/systemd/system/multi-user.target.wants/" + unit in package.links
    # Reconcile starts an enabled unit only while it is inactive and not manual.
    assert unit not in package.policies
    assert not re.search(r"^RemainAfterExit=", text, re.M)
    assert "Type=oneshot\n" in text
    assert "ConditionPathExists=/var/lib/dpkg/info/log2ram.list\n" in text
    assert re.search(r"^ExecStart=/usr/bin/apt-mark .*\bhold log2ram$", text, re.M)


def test_the_log2ram_hold_is_reapplied_daily_before_the_unattended_upgrade(package):
    """The updater runs reconcile only when a package changes, so a timer re-applies the hold."""
    timer = _text(package, "/usr/lib/systemd/system/homelab-log2ram-hold.timer")
    assert "homelab-log2ram-hold.timer" in package.enable_links()

    def minute(text: str) -> int:
        found = re.findall(r"^OnCalendar=\*-\*-\* (\d\d):(\d\d)", text, re.M)
        assert len(found) == 1, text
        return int(found[0][0]) * 60 + int(found[0][1])

    hold = minute(timer)
    update = minute(_text(package, APT_DROPINS["apt-daily.timer"]))
    upgrade = minute(_text(package, APT_DROPINS["apt-daily-upgrade.timer"]))
    # apt-daily's RandomizedDelaySec=10min plus its four-minute run.
    assert update + 14 < hold < upgrade
    assert re.search(r"^RandomizedDelaySec=0$", timer, re.M)


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
    ("/etc/watchdog.conf", "#watchdog-device\t= /dev/watchdog", WATCHDOG_RESTART),
    ("/etc/watchdog.conf", "max-load-1 = 24", WATCHDOG_RESTART),
    (AUDITD_CONF, "max_log_file = 8", "try-restart:auditd.service"),
    (AUDITD_CONF, "num_logs = 5", "try-restart:auditd.service"),
    (AUDITD_CONF, "space_left = 75", "try-restart:auditd.service"),
    (AUDITD_CONF, "admin_space_left = 50", "try-restart:auditd.service"),
    (AUDITD_CONF, "admin_space_left_action = SUSPEND", "try-restart:auditd.service"),
    ("/etc/locale.gen", "# en_US.UTF-8 UTF-8", "locale-gen"),
    (ACCT_LOGROTATE, "\trotate 30", None),  # 26.04's acct stanza, tab-indented
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
    assert package.activations.get(path) == action


UPDATE_NOTIFIER = "/etc/apt/apt.conf.d/99update-notifier"
# Ubuntu 26.04's stock conffile, md5 8c8636b1123964162e9a1127f237ba7b, per dpkg on daniel-pi.
STOCK_UPDATE_NOTIFIER = (
    'DPkg::Post-Invoke {"if [ -d /var/lib/update-notifier ]; then touch'
    " /var/lib/update-notifier/dpkg-run-stamp; fi;"
    ' /usr/lib/update-notifier/update-motd-updates-available 2>/dev/null || true";};\n'
    'APT::Update::Post-Invoke-Success {"/usr/lib/update-notifier/update-motd-updates-available'
    ' 2>/dev/null || true";};\n'
)


def test_no_apt_hook_runs_apt_check_and_a_second_run_changes_nothing(package):
    edits = [e for e in package.lines if e["path"] == UPDATE_NOTIFIER]
    once = STOCK_UPDATE_NOTIFIER
    for edit in edits:
        once = lines.with_line(once, edit)
    assert "update-motd-updates-available" not in once
    assert once.count("\n") == STOCK_UPDATE_NOTIFIER.count("\n")
    twice = once
    for edit in edits:
        twice = lines.with_line(twice, edit)
    assert twice == once


# The en_US block of the stock /etc/locale.gen on daniel-pi's Ubuntu 26.04.
STOCK_LOCALE_GEN = (
    "# en_SG.UTF-8 UTF-8\n"
    "# en_US ISO-8859-1\n"
    "# en_US.ISO-8859-15 ISO-8859-15\n"
    "# en_US.UTF-8 UTF-8\n"
    "# en_ZA ISO-8859-1\n"
)


def test_locale_gen_enables_only_en_us_utf8_and_a_second_run_changes_nothing(package):
    (edit,) = [e for e in package.lines if e["path"] == "/etc/locale.gen"]
    once = lines.with_line(STOCK_LOCALE_GEN, edit)
    assert once == STOCK_LOCALE_GEN.replace("# en_US.UTF-8 UTF-8", "en_US.UTF-8 UTF-8")
    assert lines.with_line(once, edit) == once
    assert "locales" in package.depends


def test_needrestart_skips_its_kernel_check_and_keeps_its_service_restarts(package):
    settings = dict(
        re.findall(
            r"^\$nrconf\{(\w+)\}\s*=\s*(.+?);",
            _text(package, "/etc/needrestart/conf.d/50-homelab.conf"),
            re.M,
        )
    )
    assert settings == {"kernelhints": "0"}


def test_apt_keeps_no_installed_deb_and_empties_its_archive_cache_daily(package):
    settings = dict(
        re.findall(
            r'^([\w:-]+)\s+"([^"]*)";',
            _text(package, "/etc/apt/apt.conf.d/60homelab-pi-archives"),
            re.M,
        )
    )
    assert settings == {
        "APT::Keep-Downloaded-Packages": "false",
        "APT::Periodic::CleanInterval": "1",
    }


def test_the_size_anchor_leaves_log_disk_size_alone(package):
    edits = _edits(package, LOG2RAM_CONF)
    size = edits["SIZE=128M"]["regexp"]
    assert re.search(size, "#SIZE=40M") and not re.search(size, "LOG_DISK_SIZE=256M")


def test_the_zram_swappiness_prefers_swap(package):
    text = _text(package, "/etc/sysctl.d/90-homelab-pi-tuning.conf")
    swappiness = re.search(r"^vm\.swappiness=(\d+)$", text, re.M)
    assert swappiness and int(swappiness[1]) > 100


# The log RAM budget.


def journal_caps(text: str) -> tuple[float, float, float]:
    """The journal's SystemMaxUse, SystemMaxFiles x SystemMaxFileSize, and SystemMaxFileSize.

    `text` is a journald drop-in. Raises AssertionError when it lacks one of the three keys.
    """
    use = re.search(r"^SystemMaxUse=(\d+[KMG])$", text, re.M)
    size = re.search(r"^SystemMaxFileSize=(\d+[KMG])$", text, re.M)
    files = re.search(r"^SystemMaxFiles=(\d+)$", text, re.M)
    assert use and size and files, (
        "the cap needs SystemMaxUse, SystemMaxFileSize and SystemMaxFiles"
    )
    file_mb = _mb(size.group(1))
    return _mb(use.group(1)), int(files.group(1)) * file_mb, file_mb


# Each budget counts today's file beside the kept ones.
PACCT_MB_PER_DAY = 28
PACCT_GZIP_RATIO = 0.2
SYSSTAT_MB_PER_DAY = 1.4
UNCAPPED_REST_MB = 3
JOURNAL_ACTIVE_FILES = 2  # system.journal and user-1000.journal


def _edit_value(package: host_mode.Package, path: str, key: str) -> str:
    """The value the package's edit of `path` sets for `key`."""
    for line in _edits(package, path):
        match = re.fullmatch(rf"\s*{re.escape(key)}(?:\s*=\s*|\s+)\"?([^\"]+)\"?", line)
        if match:
            return match.group(1)
    raise AssertionError(f"no edit of {path} sets {key}")


def sd_copy_worst_mb(package: host_mode.Package) -> float:
    """The largest the log2ram SD copy grows to under the package's caps, in MB."""
    _, journal_archives, file_mb = journal_caps(_text(package, JOURNALD_CAP))
    journal = journal_archives + JOURNAL_ACTIVE_FILES * file_mb
    # `start 0` and delaycompress keep pacct.0 uncompressed and gzip the generations after it.
    generations = int(_edit_value(package, ACCT_LOGROTATE, "rotate"))
    pacct = PACCT_MB_PER_DAY * (2 + (generations - 1) * PACCT_GZIP_RATIO)
    audit = int(_edit_value(package, AUDITD_CONF, "max_log_file")) * int(
        _edit_value(package, AUDITD_CONF, "num_logs")
    )
    days = int(_edit_value(package, "/etc/sysstat/sysstat", "HISTORY")) + 1
    return journal + pacct + audit + days * SYSSTAT_MB_PER_DAY + UNCAPPED_REST_MB


def test_the_whole_sd_copy_fits_under_the_log2ram_boot_guard(package):
    tmpfs = _mb(_edits(package, LOG2RAM_CONF)["SIZE=128M"]["line"].split("=")[1])
    use, _, _ = journal_caps(_text(package, JOURNALD_CAP))
    assert use < tmpfs
    # log2ram's boot guard `du`s the SD copy against SIZE and refuses to start when over; `--sparse`
    # is patched out.
    assert sd_copy_worst_mb(package) <= tmpfs


def test_a_journal_cap_without_system_max_files_is_flagged():
    """Without SystemMaxFiles the archive count, and so the SD copy, has no bound."""
    with pytest.raises(AssertionError, match="SystemMaxFiles"):
        journal_caps("[Journal]\nSystemMaxUse=48M\nSystemMaxFileSize=8M\n")


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


# auditd's free-space thresholds.

LOG_TMPFS_LOW_WATER_MB = 13


def audit_space_problems(settings: dict[str, str]) -> list[str]:
    """Each way auditd.conf's free-space settings stop auditd logging on the log tmpfs."""
    problems = []
    space_left, admin = int(settings["space_left"]), int(settings["admin_space_left"])
    if admin >= space_left:
        problems.append(
            "admin_space_left is not below space_left: auditd will not start"
        )
    if space_left > LOG_TMPFS_LOW_WATER_MB:
        problems.append(
            f"space_left = {space_left} MB is more than the tmpfs keeps free"
        )
    if settings["admin_space_left_action"].upper() in {"SUSPEND", "SINGLE", "HALT"}:
        problems.append("a write below admin_space_left stops logging until a restart")
    return problems


def test_auditd_free_space_settings_fit_the_log_tmpfs_is_clean(package):
    edits = _edits(package, AUDITD_CONF)
    settings = dict(line.split(" = ") for line in edits)
    assert audit_space_problems(settings) == []
    # An unanchored `space_left` would also rewrite the action line beside it.
    assert not re.search(
        edits["space_left = 10"]["regexp"], "space_left_action = SYSLOG"
    )


def test_auditd_packaged_free_space_settings_are_flagged():
    stock = {
        "space_left": "75",
        "admin_space_left": "50",
        "admin_space_left_action": "SUSPEND",
    }
    assert audit_space_problems(stock) == [
        "space_left = 75 MB is more than the tmpfs keeps free",
        "a write below admin_space_left stops logging until a restart",
    ]
    swapped = {**stock, "space_left": "5", "admin_space_left": "10"}
    assert "auditd will not start" in audit_space_problems(swapped)[0]


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
        (REPO / "k8s/roles/pi-peer-backup/defaults/main.yml").read_text()
    )["pi_peer_backup_src_dir"]
    pinned = re.search(
        r'command="/usr/local/bin/pi-peer-backup-shell (\S+)"', edit["line"]
    )
    assert pinned and pinned.group(1) == k8s.replace("{{ sys_user }}", user)


# The node exporter's textfile directory.


@pytest.mark.skipif(
    not shutil.which("systemd-tmpfiles"), reason="needs systemd-tmpfiles"
)
@pytest.mark.parametrize("line_kept", [True, False], ids=["green", "red-no-d-line"])
def test_tmpfiles_creates_the_textfile_directory_setgid_and_the_updater_can_write_there(
    package, tmp_path, line_kept
):
    config = tmp_path / "homelab-node-exporter.conf"
    root = tmp_path / "root"
    root.mkdir()
    text = _text(package, "/usr/lib/tmpfiles.d/homelab-node-exporter.conf")
    # Unprivileged run: owner and group become the caller's as numbers, because `--root` resolves
    # names in the root's passwd.
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
        timeout=60,
    )

    directory = root / "var/lib/node-exporter-textfile"
    # The updater stub writes homelab_host.prom and exits non-zero when the directory is missing.
    stub = subprocess.run(
        ["sh", "-c", f"printf 'x 1\\n' > {directory}/homelab_host.prom"],
        capture_output=True,
        check=False,
        timeout=60,
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
        if p.is_file() and p.name not in ("component.yml", "CLAUDE.md")
    }
    assert {p.name for p in on_disk - shipped} == {
        "log2ram-1.7.2",
        "pi-peer-backup.pub",
    }
