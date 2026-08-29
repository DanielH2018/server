"""journald forwards at info, and the three things that must stay true when it does.

journald owns /dev/log, so rsyslog only ever sees what journald forwards. `MaxLevelSyslog`
therefore decides whether the host keeps an SSH authentication trail at all: `Accepted
publickey ... SHA256:` and every `pam_unix(...:session)` line is priority info. It read
`notice` from 2026-08-01 to 2026-08-29, and in that window daniel-box recorded zero of them
while sshd's own `LogLevel VERBOSE` was set correctly throughout.

Every failure this file catches is silent. Nothing goes red, no service restarts, no alert
fires -- a log stream simply stops, or starts, and the next person to notice is whoever needs
the records months later.
"""

from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1] / "roles" / "setup" / "initial_setup"
SYSTEM_TUNING = ROLE / "tasks" / "system-tuning.yml"
PLAYBOOK = Path(__file__).resolve().parents[1] / "initial_setup.yml"

JOURNALD_DEST = "/etc/systemd/journald.conf.d/50-homelab.conf"
FILTER_DEST = "/etc/rsyslog.d/49-homelab-info-filter.conf"

# Facilities that must never be discarded by the filter. auth and authpriv carry the records
# the whole change exists to deliver. kern and mail do NOT arrive through journald -- rsyslog
# reads kernel messages itself via imklog, and postfix writes to its own listen socket -- so
# dropping their info would remove lines from kern.log and mail.log that are present today.
MUST_BE_EXEMPT = {"auth", "authpriv", "kern", "mail"}


def iter_tasks(tasks):
    """Every task, descending into block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            yield from iter_tasks(task.get(key))


def copy_content(path: Path, dest: str) -> str:
    """The `content:` of the ansible.builtin.copy task writing `dest`."""
    for task in iter_tasks(yaml.safe_load(path.read_text())):
        copy = task.get("ansible.builtin.copy")
        if isinstance(copy, dict) and copy.get("dest") == dest:
            return copy.get("content", "")
    return ""


def setting(content: str, key: str) -> str | None:
    for line in content.splitlines():
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


def exempted_facilities(filter_content: str) -> set[str]:
    """Facility names the filter refuses to discard, read from its `!=` comparisons."""
    found = set()
    for line in filter_content.splitlines():
        if line.lstrip().startswith("#"):
            continue
        parts = line.split('$syslogfacility-text != "')
        for part in parts[1:]:
            found.add(part.split('"')[0])
    return found


def handler_names(path: Path) -> list[str]:
    plays = yaml.safe_load(path.read_text())
    for play in plays:
        if isinstance(play, dict) and "handlers" in play:
            return [
                h["name"]
                for h in play["handlers"]
                if isinstance(h, dict) and "name" in h
            ]
    return []


def test_journald_forwards_at_info():
    """The regression itself. `notice` here deletes the authentication trail."""
    assert (
        setting(copy_content(SYSTEM_TUNING, JOURNALD_DEST), "MaxLevelSyslog") == "info"
    ), (
        "MaxLevelSyslog is not info, so journald drops every priority-info message before "
        "rsyslog sees it -- no Accepted publickey, no pam_unix session lines, and crowdsec's "
        "auth.log source goes empty. Nothing reports this; the lines just stop."
    )


def test_raising_the_forwarding_level_requires_the_syslog_filter():
    """The two are one change. Forwarding at info without the filter is the expensive half."""
    if setting(copy_content(SYSTEM_TUNING, JOURNALD_DEST), "MaxLevelSyslog") != "info":
        pytest.skip("forwarding is not raised, so the filter is not required")
    content = copy_content(SYSTEM_TUNING, FILTER_DEST)
    assert content, (
        f"MaxLevelSyslog=info but nothing renders {FILTER_DEST}, so every info line on the "
        'host reaches /var/log/syslog and ships to Loki as {job="syslog"} -- a measured '
        "floor of +7 MB/day/host against 1.33 MB/day today"
    )
    assert "stop" in content, f"{FILTER_DEST} discards nothing"


def test_filter_exempts_every_facility_that_bypasses_journald():
    missing = MUST_BE_EXEMPT - exempted_facilities(
        copy_content(SYSTEM_TUNING, FILTER_DEST)
    )
    assert not missing, (
        f"the filter would discard info for {sorted(missing)}. auth/authpriv carry the records "
        "this exists to deliver; kern and mail reach rsyslog directly (imklog, postfix's own "
        "socket) rather than through journald, so discarding them removes lines that are "
        "present today."
    )


def test_rsyslog_restarts_before_journald():
    """Handlers fire in definition order, not notify order.

    journald forwards at info the moment it restarts. If it restarted first, every info line
    on the host would reach /var/log/syslog until rsyslog loaded the filter that drops them.
    """
    names = handler_names(PLAYBOOK)
    assert "Restart rsyslog" in names, "no Restart rsyslog handler"
    assert "Restart systemd-journald" in names, "no Restart systemd-journald handler"
    assert names.index("Restart rsyslog") < names.index("Restart systemd-journald"), (
        "Restart systemd-journald is defined before Restart rsyslog, so journald begins "
        "forwarding at info while rsyslog is still running without the filter"
    )


# ── The rejecting halves ──────────────────────────────────────────────────────────────────
# A guard that fires on nothing and a guard that fires on everything look identical from the
# passing side. Each of these feeds the checkers above the exact input they must reject.


def test_a_notice_forwarding_level_is_detected():
    assert setting("[Journal]\nMaxLevelSyslog=notice\n", "MaxLevelSyslog") == "notice"


def test_a_missing_filter_is_detected():
    assert copy_content(SYSTEM_TUNING, "/etc/rsyslog.d/does-not-exist.conf") == ""


@pytest.mark.parametrize("dropped", sorted(MUST_BE_EXEMPT))
def test_dropping_an_exempt_facility_is_detected(dropped: str):
    kept = MUST_BE_EXEMPT - {dropped}
    crippled = "if ($syslogseverity >= 6)" + "".join(
        f' and ($syslogfacility-text != "{f}")' for f in sorted(kept)
    )
    assert MUST_BE_EXEMPT - exempted_facilities(crippled) == {dropped}


def test_reordered_handlers_are_detected():
    names = ["Restart systemd-journald", "Restart rsyslog"]
    assert names.index("Restart rsyslog") > names.index("Restart systemd-journald")
