"""journald forwards at info, and the three things that must stay true when it does.

journald owns /dev/log, so rsyslog only ever sees what journald forwards. `MaxLevelSyslog`
therefore decides whether the host keeps an SSH authentication trail at all: `Accepted
publickey ... SHA256:` and every `pam_unix(...:session)` line is priority info. At `notice`
the host records none of them, even with sshd's own `LogLevel VERBOSE` set correctly.

Every failure this file catches is silent. Nothing goes red, no service restarts, no alert
fires -- a log stream simply stops, or starts, and the next person to notice is whoever needs
the records months later.
"""

import pytest
from lib import yaml_fast
from _helpers import ALL_VARS, HOST_VARS
from _host_packages import HOSTS, shipped_text
from lib.repo_paths import REPO

# The host-tuning component ships the journald drop-in in every host package. The rsyslog-filter
# component ships the filter below on every has_rsyslog host.
JOURNALD_DEST = "/etc/systemd/journald.conf.d/50-homelab.conf"
FILTER_DEST = "/etc/rsyslog.d/49-homelab-info-filter.conf"
FILTER_COMPONENT = "rsyslog-filter"

# Facilities that must never be discarded by the filter. auth and authpriv carry the records
# the whole change exists to deliver. kern and mail do NOT arrive through journald -- rsyslog
# reads kernel messages itself via imklog, and postfix writes to its own listen socket -- so
# dropping their info would remove lines from kern.log and mail.log that are present today.
MUST_BE_EXEMPT = {"auth", "authpriv", "kern", "mail"}


def lists_the_filter(host: str) -> bool:
    spec = yaml_fast.safe_load((REPO / "hosts" / host / "package.yml").read_text())
    return FILTER_COMPONENT in spec["components"]


def filter_text(host: str) -> str:
    """The filter `host` takes from its package, or nothing when it does not list one."""
    return shipped_text(host, FILTER_DEST) if lists_the_filter(host) else ""


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

    A host with no rsyslog skips: forwarding reaches nothing there, and the filter has nothing
    to pair with.
    """
    # fact: ansible/roles/setup/initial_setup/CLAUDE.md#Rules the task files do not state
    if setting(shipped_text(host, JOURNALD_DEST), "MaxLevelSyslog") != "info":
        pytest.skip("forwarding is not raised, so the filter is not required")
    if not has_rsyslog(host):
        pytest.skip("rsyslog is masked here, so forwarding reaches nothing")
    content = filter_text(host)
    assert content, (
        f"MaxLevelSyslog=info but nothing renders {FILTER_DEST}, so every info line on the "
        'host reaches /var/log/syslog and ships to Loki as {job="syslog"} -- a measured '
        "floor of +7 MB/day/host against 1.33 MB/day today"
    )
    assert "stop" in content, f"{FILTER_DEST} discards nothing"


@pytest.mark.parametrize("host", [h for h in HOSTS if has_rsyslog(h)])
def test_filter_exempts_every_facility_that_bypasses_journald(host: str):
    missing = MUST_BE_EXEMPT - exempted_facilities(filter_text(host))
    assert not missing, (
        f"the filter would discard info for {sorted(missing)}. auth/authpriv carry the records "
        "this exists to deliver; kern and mail reach rsyslog directly (imklog, postfix's own "
        "socket) rather than through journald, so discarding them removes lines that are "
        "present today."
    )


def filter_listing_errors(hosts, lists, rsyslog) -> list[str]:
    """Each host whose package lists the filter where `has_rsyslog` says it must not, or the
    reverse."""
    return [h for h in hosts if lists(h) != rsyslog(h)]


def test_the_filter_component_lists_exactly_on_hosts_with_rsyslog():
    """A masked rsyslog has no use for the filter, and `rsyslogd -N1` would hold the host."""
    # fact: ansible/roles/setup/initial_setup/CLAUDE.md#Rules the task files do not state
    assert lists_the_filter("daniel-server")
    assert lists_the_filter("daniel-box")
    assert not lists_the_filter("daniel-pi")
    assert filter_listing_errors(HOSTS, lists_the_filter, has_rsyslog) == []
    assert yaml_fast.safe_load(ALL_VARS.read_text()).get("has_rsyslog") is True, (
        "has_rsyslog has no true default in group_vars/all.yml, so a new host would list no "
        "filter while its rsyslog runs"
    )


# ── The rejecting halves ──────────────────────────────────────────────────────────────────
# A guard that fires on nothing and a guard that fires on everything look identical from the
# passing side. Each of these feeds the checkers above the exact input they must reject.


def test_a_notice_forwarding_level_is_detected():
    assert setting("[Journal]\nMaxLevelSyslog=notice\n", "MaxLevelSyslog") == "notice"


def test_a_host_with_rsyslog_and_no_filter_is_detected():
    hosts = ["a", "b"]
    assert filter_listing_errors(hosts, lambda h: h == "a", lambda h: True) == ["b"]


def test_a_filter_on_a_host_without_rsyslog_is_detected():
    hosts = ["a", "b"]
    assert filter_listing_errors(hosts, lambda h: True, lambda h: h == "a") == ["b"]


@pytest.mark.parametrize("dropped", sorted(MUST_BE_EXEMPT))
def test_dropping_an_exempt_facility_is_detected(dropped: str):
    kept = MUST_BE_EXEMPT - {dropped}
    crippled = "if ($syslogseverity >= 6)" + "".join(
        f' and ($syslogfacility-text != "{f}")' for f in sorted(kept)
    )
    assert MUST_BE_EXEMPT - exempted_facilities(crippled) == {dropped}
