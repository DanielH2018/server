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
from lib.proc_testing import run
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
