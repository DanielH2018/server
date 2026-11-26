"""What fills daniel-pi's 128 MB log2ram tmpfs, and what rotates it.

The caps on the journal, pacct, auditd and sysstat must keep the SD copy under log2ram's boot
guard, and logrotate must read and run each stanza that keeps pacct and log2ram.log bounded.

Run: uv run pytest hosts/lib/tests/host_baselines/test_pi_log_tmpfs.py
"""

import re
from pathlib import PurePosixPath

import pytest

from reconcile_lib import lines
from renderer.renderer_lib import host as host_mode
from test_pi_tuning_component import (
    ACCT_LOGROTATE,
    AUDITD_CONF,
    LOG2RAM_CONF,
    _edits,
    _text,
    dropin_problem,
)
from test_pi_tuning_component import package  # noqa: F401 (the fixture, found by name)

JOURNALD_CAP = "/etc/systemd/journald.conf.d/60-homelab-pi.conf"
_MB = {"K": 1 / 1024, "M": 1, "G": 1024}


def _mb(value: str) -> float:
    match = re.fullmatch(r"(\d+)([KMG])", value.strip())
    assert match, f"cannot read {value!r} as a size"
    return int(match.group(1)) * _MB[match.group(2)]


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


def _edit_value(package: host_mode.Package, path: str, key: str) -> str:  # noqa: F811
    """The value the package's edit of `path` sets for `key`."""
    for line in _edits(package, path):
        match = re.fullmatch(rf"\s*{re.escape(key)}(?:\s*=\s*|\s+)\"?([^\"]+)\"?", line)
        if match:
            return match.group(1)
    raise AssertionError(f"no edit of {path} sets {key}")


def sd_copy_worst_mb(package: host_mode.Package) -> float:  # noqa: F811
    """The largest the log2ram SD copy grows to under the package's caps, in MB."""
    _, journal_archives, file_mb = journal_caps(_text(package, JOURNALD_CAP))
    journal = journal_archives + JOURNAL_ACTIVE_FILES * file_mb
    # `start 0` and delaycompress keep pacct.0 uncompressed and gzip the generations after it. A
    # file passes `size` by up to an hour's growth before the hourly run rotates it.
    generations = int(_edit_value(package, ACCT_LOGROTATE, "rotate"))
    pacct_file = (
        _mb(_edit_value(package, ACCT_LOGROTATE, "size")) + PACCT_MB_PER_DAY / 24
    )
    pacct = pacct_file * (2 + (generations - 1) * PACCT_GZIP_RATIO)
    audit = int(_edit_value(package, AUDITD_CONF, "max_log_file")) * int(
        _edit_value(package, AUDITD_CONF, "num_logs")
    )
    days = int(_edit_value(package, "/etc/sysstat/sysstat", "HISTORY")) + 1
    return journal + pacct + audit + days * SYSSTAT_MB_PER_DAY + UNCAPPED_REST_MB


def test_the_whole_sd_copy_fits_under_the_log2ram_boot_guard(package):  # noqa: F811
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


# logrotate.

# acct 6.6.4's /etc/logrotate.d/acct on daniel-pi's Ubuntu 26.04.
STOCK_ACCT_LOGROTATE = (
    "/var/log/account/pacct {\n\tdaily\n\tcompress\n\tmissingok\n\tnotifempty\n"
    "\tdelaycompress\n\trotate 30\n\tstart 0\n\tcreate\n\tpostrotate\n"
    '\t\trun-parts --regex \'.*\\.hook$\' --arg "$1" --arg "$2"'
    " /etc/acct/hooks.d /usr/share/acct/hooks.d\n\tendscript\n}\n"
)


def test_pacct_rotates_by_size_on_an_hourly_logrotate_run(package):  # noqa: F811
    """A `daily` check reads the state file on the card, which a tmpfs restore leaves ahead."""
    stanza = STOCK_ACCT_LOGROTATE
    for edit in [e for e in package.lines if e["path"] == ACCT_LOGROTATE]:
        stanza = lines.with_line(stanza, edit)
    body = stanza[: stanza.index("}")]
    assert re.search(r"^\tsize \d+M$", body, re.M), stanza
    assert not re.search(r"^\s*(daily|weekly|monthly)\s*$", stanza, re.M), stanza
    timer = _text(package, "/usr/lib/systemd/system/logrotate.timer.d/50-homelab.conf")
    assert dropin_problem(timer) is None
    assert re.search(r"^OnCalendar=hourly$", timer, re.M)


def test_log2ram_log_has_one_stanza_and_it_sits_directly_under_logrotate_d(package):  # noqa: F811
    """logrotate's `include /etc/logrotate.d` reads no subdirectory."""
    stanzas = [
        PurePosixPath(dst)
        for dst, (body, _) in package.files.items()
        if dst.startswith("/etc/logrotate.d/")
        and re.search(rb"^/var/log/log2ram\.log\b", body, re.M)
    ]
    assert [path.parent for path in stanzas] == [PurePosixPath("/etc/logrotate.d")]
