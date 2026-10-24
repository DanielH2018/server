"""The census row that holds every repo-installed systemd service to a stored log level.

journald stores notice and above (`MaxLevelStore=notice`,
`ansible/roles/setup/initial_setup/tasks/system-tuning.yml`). It forwards info to rsyslog, and
`49-homelab-info-filter.conf` then stops info for every facility except auth, authpriv, kern
and mail. A unit's stdout and stderr reach journald at its `SyslogLevel=`, which defaults to
info, so a unit left on the default loses every line it prints (#6160). The row runs on
`ansible/tests/_row_table.py`'s harness from its own module, because `test_census_rows_text.py`
is at its module-length allowlist entry.

Run: uv run pytest ansible/tests/repo/test_installed_units_log_level.py
"""

from _row_table import Census, Subject, check, proof_problems, tracked

# Levels journald stores under MaxLevelStore=notice.
STORED_LEVELS = frozenset({"emerg", "alert", "crit", "err", "warning", "notice"})


def service_syslog_level(text: str) -> str | None:
    """The `SyslogLevel=` of a unit's [Service] section, or None when it sets none."""
    section = None
    level = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line
        elif section == "[Service]" and line.startswith("SyslogLevel="):
            level = line.partition("=")[2].strip()
    return level


def _unstored_level_offence(subject: Subject) -> list[str]:
    level = service_syslog_level(subject.text)
    if level is None:
        return ["sets no SyslogLevel= in [Service], so its stdout logs at info"]
    if level not in STORED_LEVELS:
        return [f"SyslogLevel={level} is below notice"]
    return []


def _installed_units() -> list[str]:
    # Test fixtures are excluded because no host installs them.
    units = tracked(
        "ansible/*.service",
        "ansible/*.service.j2",
        "hosts/*.service",
        "hosts/*.service.j2",
    )
    return [rel for rel in units if "/tests/" not in rel]


SYSLOG_LEVEL_ROW = Census(
    name="installed-units-log-at-a-stored-level",
    reason=(
        "journald stores notice and above and rsyslog's info filter drops the rest, so a unit "
        "on the default SyslogLevel=info loses every line it prints: a traceback reaches "
        "neither `journalctl -u`, /var/log/syslog nor Loki (#6160). Add `SyslogLevel=notice` "
        "to the unit's [Service], or put the unit in `allow` with the reason it may stay at "
        "info. `k3s.service` is the installer's file, and `test_k3s_unit_logging.py` covers it."
    ),
    files=_installed_units,
    offence=_unstored_level_offence,
    red=(
        Subject("w.service.j2", "[Service]\nType=oneshot\nExecStart=/bin/true\n"),
        Subject("w.service.j2", "[Service]\nSyslogLevel=info\nExecStart=/bin/true\n"),
        Subject(
            "w.service.j2",
            "[Unit]\nSyslogLevel=notice\n[Service]\nExecStart=/bin/true\n",
        ),
    ),
    green=(
        Subject("w.service.j2", "[Service]\nSyslogLevel=notice\nExecStart=/bin/true\n"),
    ),
    min_matches=30,
    must_find=frozenset(
        {
            "ansible/roles/setup/gitops_deploy/templates/gitops-deploy.service.j2",
            "ansible/roles/setup/renovate_notify/templates/renovate-notify.service.j2",
            "hosts/packages/homelab-updater/units/homelab-updater.service",
            "hosts/components/reconcile/units/homelab-reconcile.service",
        }
    ),
)


def test_every_installed_unit_logs_at_a_stored_level():
    problems = check(SYSLOG_LEVEL_ROW)
    assert not problems, "\n".join(problems)


def test_the_row_flags_its_red_subjects_and_passes_its_green_one():
    assert not proof_problems(SYSLOG_LEVEL_ROW)
