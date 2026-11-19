"""The t2-egress component: its ruleset, its squid proxy and the sessions that use it.

nft itself checks the syntax on the host, in the loader's ExecStartPre=, because a check here
would need CAP_NET_ADMIN. squid parses its config on the host the same way.
"""

import re
import shlex
import shutil

import pytest
import yaml

from lib import yaml_fast
from lib.estate import Inventory
from lib.proc_testing import run, write_exec
from lib.repo_paths import HOST_VARS, REPO
from renderer.renderer_lib import host as host_mode

RULESET = "/usr/lib/homelab/t2-egress/t2-egress.nft"
EGRESS_UNIT = "/usr/lib/systemd/system/homelab-t2-egress.service"
SQUID_CONF = "/usr/lib/homelab/t2-egress/t2-squid.conf"
SQUID_DROPIN = "/usr/lib/systemd/system/squid.service.d/50-homelab-t2.conf"
SESSION_UNITS = (
    "/usr/lib/systemd/system/homelab-claude-triage-session.service",
    "/usr/lib/systemd/system/homelab-renovate-agent-session.service",
)
TRIAGE_SETUP = "/usr/lib/homelab/claude-triage-setup"
SESSION_USERS = {
    "renovate-agent": REPO / "hosts/components/renovate-agent/component.yml",
    "claude-triage": REPO / "hosts/daniel-box/package.yml",
}
OWN_TABLE = "inet t2_egress"


def statements(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def foreign_tables(text: str) -> list[str]:
    """Each statement that names a table other than `inet t2_egress`, or names none."""
    found = []
    for statement in statements(text):
        match = re.match(
            r"(?:add|flush|delete) (?:table|chain|set|rule) (\S+ \S+)", statement
        )
        if not match or match.group(1) != OWN_TABLE:
            found.append(statement)
    return found


@pytest.fixture(scope="module")
def box() -> host_mode.Package:
    return host_mode.render_host("daniel-box")


@pytest.fixture(scope="module")
def box_off(tmp_path_factory) -> host_mode.Package:
    """daniel-box with the egress table and the session proxy both switched off."""
    host_vars = tmp_path_factory.mktemp("off") / "host_vars"
    shutil.copytree(HOST_VARS, host_vars)
    path = host_vars / "daniel-box.yml"
    values = yaml.safe_load(path.read_text())
    del values["t2_egress_proxy"]
    values["t2_egress_enabled"] = False
    path.write_text(yaml.safe_dump(values))
    return host_mode.render_host("daniel-box", inventory=Inventory(host_vars=host_vars))


def rendered(package: host_mode.Package, path: str) -> str:
    return package.files[path][0].decode()


def table_stops(unit: str) -> list[str]:
    """Each ExecStop or ExecStopPost line of `unit` that runs nft."""
    return [
        line
        for line in unit.splitlines()
        if re.match(r"ExecStop(?:Post)?=.*\bnft\b", line)
    ]


def test_the_ruleset_names_only_its_own_table_is_clean(box, box_off):
    """I4: loading it, in either flag state, changes no ufw, kube-proxy, flannel or portmap rule."""
    for package in (box, box_off):
        assert statements(rendered(package, RULESET))
        assert foreign_tables(rendered(package, RULESET)) == []


def test_a_statement_on_another_table_is_flagged(box):
    planted = rendered(box, RULESET) + "\nadd rule ip filter OUTPUT counter\n"
    assert foreign_tables(planted) == ["add rule ip filter OUTPUT counter"]


def test_the_switched_off_ruleset_deletes_the_table_and_the_unit_still_ships(
    box, box_off
):
    """The way out is the flag: the package keeps the unit, and its reload deletes the table."""
    on, off = statements(rendered(box, RULESET)), statements(rendered(box_off, RULESET))
    assert "delete table inet t2_egress" not in on
    assert [s for s in on if s.startswith("add rule")]
    assert off == ["add table inet t2_egress", "delete table inet t2_egress"]
    assert EGRESS_UNIT in box_off.files


def test_no_stop_of_the_egress_unit_touches_the_table_is_clean(box):
    """A restart for a changed unit file keeps the sets, and once B3-2 enforces, the fence."""
    assert table_stops(rendered(box, EGRESS_UNIT)) == []


def test_a_stop_that_deletes_the_table_is_flagged(box):
    planted = (
        rendered(box, EGRESS_UNIT)
        + "ExecStop=/usr/sbin/nft delete table inet t2_egress\n"
    )
    assert table_stops(planted) == [
        "ExecStop=/usr/sbin/nft delete table inet t2_egress"
    ]


def test_the_rules_cover_exactly_the_session_users_the_users_arm_declares(box):
    declared = {
        user["uid"]
        for name, spec in SESSION_USERS.items()
        for user in yaml_fast.safe_load(spec.read_text())["users"]
        if user["name"] == name
    }
    assert len(declared) == len(SESSION_USERS), "a session user is no longer declared"
    rule_uids = {
        frozenset(int(uid) for uid in group.split(","))
        for group in re.findall(r"meta skuid \{([^}]*)\}", rendered(box, RULESET))
    }
    assert rule_uids == {frozenset(declared)}


def test_squid_binds_the_proxy_address_logs_to_a_file_and_never_to_syslog(box):
    """I6: the request log stays in /var/log/squid, which Alloy does not tail."""
    proxy = yaml_fast.safe_load((HOST_VARS / "daniel-box.yml").read_text())[
        "t2_egress_proxy"
    ]
    conf = rendered(box, SQUID_CONF)
    assert f"http_port {proxy}\n" in conf
    assert "access_log daemon:/var/log/squid/t2-access.log t2" in conf
    assert "squid" in box.depends
    starts = [
        line
        for line in rendered(box, SQUID_DROPIN).splitlines()
        if line.startswith("ExecStart=/")
    ]
    assert starts == [f"ExecStart=/usr/sbin/squid --foreground -YC -f {SQUID_CONF}"]


def test_both_sessions_send_their_traffic_through_the_proxy(box):
    proxy = yaml_fast.safe_load((HOST_VARS / "daniel-box.yml").read_text())[
        "t2_egress_proxy"
    ]
    for unit in SESSION_UNITS:
        text = rendered(box, unit)
        assert f"HTTPS_PROXY=http://{proxy} https_proxy=http://{proxy}" in text, unit
        assert f"HTTP_PROXY=http://{proxy} http_proxy=http://{proxy}" in text, unit
        assert "Wants=squid.service" in text, unit


def test_no_session_sends_claude_codes_nonessential_traffic(box):
    """Error reports go to an intake the egress allowlist leaves out, so the session never tries."""
    for unit in SESSION_UNITS:
        assert "Environment=CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1\n" in rendered(
            box, unit
        ), unit


def test_without_a_proxy_address_no_session_names_one(box_off):
    for unit in SESSION_UNITS:
        text = rendered(box_off, unit)
        assert "PROXY" not in text and "squid" not in text, unit


def test_the_triage_kubeconfig_names_the_api_by_the_hosts_lan_address(box, tmp_path):
    """B3-1: no loopback port stays open to the session; the API goes through the proxy."""
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    server_ip = yaml_fast.safe_load((HOST_VARS / "daniel-box.yml").read_text())[
        "server_ip"
    ]
    setup = rendered(box, TRIAGE_SETUP)
    (command,) = re.findall(r"^\s*(sed -i '[^']*') \S+$", setup, re.MULTILINE)
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(
        "clusters:\n- cluster:\n    server: https://127.0.0.1:6443\n  name: default\n"
    )
    run([*shlex.split(command), str(kubeconfig)], check=True)
    assert f"    server: https://{server_ip}:6443\n" in kubeconfig.read_text()


CHECK = REPO / "hosts/components/t2-egress/files/t2-egress-check"
SESSION_UIDS = {SESSION_UNITS[0]: 907, SESSION_UNITS[1]: 901}
ALLOWED_HOSTS = frozenset(
    {
        "api.anthropic.com",
        "api.github.com",
        "prometheus.local.daniel-hunter.com",
        "alertmanager.local.daniel-hunter.com",
        "results-receiver.actions.githubusercontent.com",
        "cafe.github.com",
    }
)


def declared_uids() -> set[int]:
    return {
        user["uid"]
        for name, spec in SESSION_USERS.items()
        for user in yaml_fast.safe_load(spec.read_text())["users"]
        if user["name"] == name
    }


def first(lines: list[str], needle: str) -> int:
    return next(i for i, line in enumerate(lines) if needle in line)


def test_each_session_uid_is_refused_everything_after_its_trace_and_the_proxy(box):
    """The trace sees refused attempts too, and only squid's address and port get past."""
    proxy_host, proxy_port = yaml_fast.safe_load(
        (HOST_VARS / "daniel-box.yml").read_text()
    )["t2_egress_proxy"].split(":")
    rules = [s for s in statements(rendered(box, RULESET)) if s.startswith("add rule")]
    accept = first(rules, " accept")
    assert (
        f"ip daddr {proxy_host} tcp dport {proxy_port} counter accept" in rules[accept]
    )
    assert (
        first(rules, "update @seen4") < accept
        and first(rules, "update @seen6") < accept
    )
    for uid in declared_uids():
        tcp = first(rules, f'comment "t2-fence {uid} tcp"')
        other = first(rules, f'comment "t2-fence {uid} other"')
        assert accept < tcp < other
        assert "reject with tcp reset" in rules[tcp]
        assert "reject with icmpx admin-prohibited" in rules[other]
    assert not [r for r in rules if re.search(r"\blog\b", r)], "a rule logs (I6)"


def test_squid_admits_exactly_the_ruled_hosts_and_refuses_the_rest(box):
    server_ip = yaml_fast.safe_load((HOST_VARS / "daniel-box.yml").read_text())[
        "server_ip"
    ]
    conf = statements(rendered(box, SQUID_CONF))
    hosts = {line.split()[-1] for line in conf if line.startswith("acl t2_https ")}
    assert hosts == ALLOWED_HOSTS
    assert f"acl t2_k3s dst {server_ip}" in conf
    access = [line for line in conf if line.startswith("http_access ")]
    assert access == [
        "http_access deny to_localhost",
        "http_access deny to_linklocal",
        "http_access allow loopback CONNECT https_port t2_https",
        "http_access allow loopback CONNECT k3s_port t2_k3s",
        "http_access deny all",
    ]


def test_each_session_unit_checks_its_own_uid_is_fenced_before_it_starts(box, box_off):
    """I4: the check runs as root before the session, and goes with the flag."""
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    for unit, uid in SESSION_UIDS.items():
        line = f"ExecStartPre=+/usr/lib/homelab/t2-egress/t2-egress-check {uid}\n"
        assert line in rendered(box, unit), unit
        assert "t2-egress-check" not in rendered(box_off, unit), unit
    assert set(SESSION_UIDS.values()) == declared_uids()


def checked(tmp_path, nft_body: str, uid: str = "907") -> int:
    """t2-egress-check's exit status against a stub nft whose script is `nft_body`."""
    nft = write_exec(tmp_path / "nft", f"#!/bin/sh\n{nft_body}\n")
    script = write_exec(
        tmp_path / "t2-egress-check",
        CHECK.read_text().replace("nft=/usr/sbin/nft", f"nft={nft}"),
    )
    assert f"nft={nft}" in script.read_text()
    return run([str(script), uid]).returncode


def fence_listing(box) -> str:
    """What `nft list chain` prints for the rendered fence, close enough for the check."""
    return "\n".join(
        "\t\t" + s.split("output ", 1)[1]
        for s in statements(rendered(box, RULESET))
        if s.startswith("add rule")
    )


def test_the_check_passes_a_loaded_fence_for_the_uid_is_clean(box, tmp_path):
    listing = tmp_path / "listing"
    listing.write_text(fence_listing(box))
    assert checked(tmp_path, f"cat {listing}") == 0


def test_the_check_refuses_when_the_table_is_missing_is_flagged(tmp_path):
    assert checked(tmp_path, "echo 'No such file or directory' >&2; exit 1") == 1


def test_the_check_refuses_a_table_without_the_uids_rules_is_flagged(box, tmp_path):
    listing = tmp_path / "listing"
    listing.write_text(
        "\n".join(
            line
            for line in fence_listing(box).splitlines()
            if "t2-fence 907" not in line
        )
    )
    assert checked(tmp_path, f"cat {listing}") == 1
    assert checked(tmp_path, f"cat {listing}", uid="901") == 0
