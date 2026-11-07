"""journald forwards at info, and the three things that must stay true when it does.

journald owns /dev/log, so rsyslog only ever sees what journald forwards. `MaxLevelSyslog`
therefore decides whether the host keeps an SSH authentication trail at all: `Accepted
publickey ... SHA256:` and every `pam_unix(...:session)` line is priority info. At `notice`
the host records none of them, even with sshd's own `LogLevel VERBOSE` set correctly.

Every failure this file catches is silent. Nothing goes red, no service restarts, no alert
fires -- a log stream simply stops, or starts, and the next person to notice is whoever needs
the records months later.
"""

from pathlib import Path

import pytest
from lib import yaml_fast
from _helpers import ALL_VARS, HOST_VARS, SETUP_ROLES
from _host_packages import HOSTS, shipped_text
from lib.repo_paths import REPO

ROLE = SETUP_ROLES / "initial_setup"
SYSTEM_TUNING = ROLE / "tasks" / "system-tuning.yml"

# The host-tuning component ships the journald drop-in in every host package. The rsyslog-filter
# component ships the filter below on the hosts that list it, and initial_setup writes it on the
# other has_rsyslog hosts.
JOURNALD_DEST = "/etc/systemd/journald.conf.d/50-homelab.conf"
FILTER_DEST = "/etc/rsyslog.d/49-homelab-info-filter.conf"
FILTER_COMPONENT = "rsyslog-filter"

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
    for task in iter_tasks(yaml_fast.safe_load(path.read_text())):
        copy = task.get("ansible.builtin.copy")
        if isinstance(copy, dict) and copy.get("dest") == dest:
            return copy.get("content", "")
    return ""


def lists_the_filter(host: str) -> bool:
    spec = yaml_fast.safe_load((REPO / "hosts" / host / "package.yml").read_text())
    return FILTER_COMPONENT in spec["components"]


def filter_text(host: str) -> str:
    """The filter `host` takes from its package, or else the role's copy."""
    if lists_the_filter(host):
        return shipped_text(host, FILTER_DEST)
    return copy_content(SYSTEM_TUNING, FILTER_DEST)


def has_rsyslog(host: str) -> bool:
    flag = yaml_fast.safe_load((HOST_VARS / f"{host}.yml").read_text()).get(
        "has_rsyslog"
    )
    return (
        yaml_fast.safe_load(ALL_VARS.read_text())["has_rsyslog"]
        if flag is None
        else flag
    )


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


@pytest.mark.parametrize("host", HOSTS)
def test_journald_forwards_at_info(host: str):
    """The regression itself. `notice` here deletes the authentication trail."""
    assert setting(shipped_text(host, JOURNALD_DEST), "MaxLevelSyslog") == "info", (
        "MaxLevelSyslog is not info, so journald drops every priority-info message before "
        "rsyslog sees it -- no Accepted publickey, no pam_unix session lines, and crowdsec's "
        "auth.log source goes empty. Nothing reports this; the lines just stop."
    )


@pytest.mark.parametrize("host", HOSTS)
def test_raising_the_forwarding_level_requires_the_syslog_filter(host: str):
    """The two are one change. Forwarding at info without the filter is the expensive half.

    Read statically, so it holds wherever the filter block RUNS; the block skips on a host
    with no rsyslog (`test_the_filter_is_gated_on_has_rsyslog`), where forwarding reaches
    nothing and the filter has nothing to pair with.
    """
    # fact: ansible/roles/setup/initial_setup/CLAUDE.md#Rules the task files do not state
    if setting(shipped_text(host, JOURNALD_DEST), "MaxLevelSyslog") != "info":
        pytest.skip("forwarding is not raised, so the filter is not required")
    content = filter_text(host)
    assert content, (
        f"MaxLevelSyslog=info but nothing renders {FILTER_DEST}, so every info line on the "
        'host reaches /var/log/syslog and ships to Loki as {job="syslog"} -- a measured '
        "floor of +7 MB/day/host against 1.33 MB/day today"
    )
    assert "stop" in content, f"{FILTER_DEST} discards nothing"


@pytest.mark.parametrize("host", HOSTS)
def test_filter_exempts_every_facility_that_bypasses_journald(host: str):
    missing = MUST_BE_EXEMPT - exempted_facilities(filter_text(host))
    assert not missing, (
        f"the filter would discard info for {sorted(missing)}. auth/authpriv carry the records "
        "this exists to deliver; kern and mail reach rsyslog directly (imklog, postfix's own "
        "socket) rather than through journald, so discarding them removes lines that are "
        "present today."
    )


def filter_block_when(path: Path):
    """The `when` of the task or block that renders the rsyslog filter."""
    for task in yaml_fast.safe_load(path.read_text()) or []:
        if not isinstance(task, dict):
            continue
        for inner in iter_tasks(task.get("block")):
            copy = inner.get("ansible.builtin.copy")
            if isinstance(copy, dict) and copy.get("dest") == FILTER_DEST:
                return task.get("when")
    return None


def test_the_filter_is_gated_on_has_rsyslog():
    """rsyslog is masked on daniel-pi, and a masked unit fails the restart handler.

    Without the gate, `initial_setup.yml -e target=daniel-pi` applies every task and then fails
    at `Restart rsyslog`, so the recap reads failed=1 for a run whose changes are all live. The
    gate is a declared host flag, not a live check of
    the unit, so the play never renders config for a logger the host has retired.
    """
    assert "has_rsyslog" in filter_block_when(SYSTEM_TUNING), (
        "the block rendering the rsyslog filter is not `when: has_rsyslog`, so it runs on "
        "daniel-pi, notifies Restart rsyslog, and fails the play on the masked unit"
    )
    assert yaml_fast.safe_load(ALL_VARS.read_text()).get("has_rsyslog") is True, (
        "has_rsyslog has no true default in group_vars/all.yml -- an undefined flag fails "
        "the when on every host rather than skipping only the Pi"
    )
    pi = yaml_fast.safe_load((HOST_VARS / "daniel-pi.yml").read_text())
    assert pi.get("has_rsyslog") is False, (
        "daniel-pi does not set has_rsyslog: false, so the filter block runs on the one host "
        "whose rsyslog is masked"
    )


def test_the_filter_component_lists_only_on_hosts_with_rsyslog():
    """A masked rsyslog has no use for the filter, and `rsyslogd -N1` would hold the host."""
    assert lists_the_filter("daniel-server")
    assert not lists_the_filter("daniel-pi")
    assert not [h for h in HOSTS if lists_the_filter(h) and not has_rsyslog(h)]


def test_the_component_ships_the_bytes_the_role_writes():
    assert shipped_text("daniel-server", FILTER_DEST) == copy_content(
        SYSTEM_TUNING, FILTER_DEST
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


def test_an_ungated_filter_block_is_detected(tmp_path: Path):
    ungated = tmp_path / "system-tuning.yml"
    ungated.write_text(
        "- name: filter\n"
        "  block:\n"
        "    - name: render\n"
        "      ansible.builtin.copy:\n"
        f"        dest: {FILTER_DEST}\n"
        "        content: stop\n"
    )
    assert filter_block_when(ungated) is None
