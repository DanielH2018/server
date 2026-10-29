"""Each host-plane rule in observability's `host-checks` group names the host it must.

`files/rules/host-checks.yaml` is plain YAML, so each rule names its host as a literal:

- one `HostReconcileFailed` per host `hosts.ini` declares, reading that host's `origin`;
- one `HostUpdateStale` per entry of `observability_expected_hosts`, equal to the rule
  `update_stale_rule` builds from the entry. `tests/rules/fixtures/host-update-stale.yaml`
  holds that rule for `FIXTURE_HOSTS`, the one-host map `host-checks.test.yaml` fires it from,
  so the shape has promtool cases while the shipped map is empty;
- one `WiredLinkDown` per host whose host_vars set `k3s_node_static_link`, reading that link.

Run: uv run pytest ansible/tests/services/test_host_plane_rule_literals.py
"""

import re

import yaml
from _helpers import K8S_ROLES, load_defaults
from lib import yaml_fast
from lib.ansible_inventory import inventory_hosts
from lib.repo_paths import ALL_VARS, HOST_VARS

ROLE = K8S_ROLES / "observability"
RULES = ROLE / "files" / "rules" / "host-checks.yaml"
FIXTURE = ROLE / "tests" / "rules" / "fixtures" / "host-update-stale.yaml"
# A host no inventory declares, so the fixture never shadows a shipped rule for a real host.
FIXTURE_HOSTS = {"fixture-host": {"job": "node-fixture", "limit_seconds": 11400}}
# The page both host-plane alerts link as their runbook.
RUNBOOK_URL = f"https://docs.local.{yaml_fast.safe_load(ALL_VARS.read_text())['domain']}/host-packages/"
# Non-vacuity: the hosts hosts.ini must keep declaring for the reconcile census to mean much.
FLEET = frozenset({"daniel-box", "daniel-server", "daniel-pi"})
# Non-vacuity: the k3s nodes that pin their address on a wired link with a WiFi fallback.
WIRED_NODES = frozenset({"daniel-box", "daniel-server"})


def _duration(seconds: int) -> str:
    """`seconds` as the annotations write a duration: minutes under 2h, else hours and minutes."""
    if seconds < 2 * 3600:
        return f"{seconds // 60} minutes"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours} hours" + (f" {minutes} minutes" if minutes else "")


def update_stale_rule(host: str, job: str, limit_seconds: int) -> dict:
    """The `HostUpdateStale` rule for one entry of `observability_expected_hosts`."""
    series = (
        f'homelab_host_update_success_timestamp_seconds{{job="{job}",origin="{host}"}}'
    )
    return {
        "alert": "HostUpdateStale",
        "expr": (
            f"( time() - {series} > {limit_seconds} or ( absent({series}) and on (job, origin)"
            f' up{{job="{job}",origin="{host}"}} == 1 ) )'
        ),
        "labels": {"severity": "critical", "service": host},
        "annotations": {
            "summary": f"{host} has had no successful homelab update for {_duration(limit_seconds)}.",
            "description": (
                f"No homelab-updater run on {host} has succeeded in {_duration(limit_seconds)}, or its "
                "exporter is up and serves no success timestamp. A failed fetch, a stopped "
                "homelab-apt-repo.service, a firewall rule, a keyring mismatch and a stopped "
                "timer each leave it stale. Read `systemctl status homelab-updater.timer` and "
                f"`journalctl -u homelab-updater` on {host}, and docs/host-packages.md, "
                '"Host-plane pages".'
            ),
            "runbook_url": RUNBOOK_URL,
        },
    }


def _squash(value: object) -> object:
    """`value` with every run of whitespace in its strings read as one space, and `(` `)` bare."""
    if isinstance(value, dict):
        return {key: _squash(item) for key, item in value.items()}
    if isinstance(value, str):
        return re.sub(r"\s*([()])\s*", r"\1", " ".join(value.split()))
    return value


def _rules(path, alert: str) -> list[dict]:
    doc = yaml_fast.safe_load(path.read_text())
    return [
        rule
        for group in doc["groups"]
        for rule in group["rules"]
        if rule.get("alert") == alert
    ]


def update_stale_problems(rules: list[dict], expected: dict[str, dict]) -> list[str]:
    """Why the `HostUpdateStale` rules differ from the ones an expected-host map needs, or nothing.

    Args:
        rules: The `HostUpdateStale` rules a rule file holds.
        expected: An `observability_expected_hosts` map.
    """
    by_host = {rule["labels"]["service"]: rule for rule in rules}
    problems = [
        f"HostUpdateStale names {host}, which observability_expected_hosts does not list"
        for host in sorted(by_host.keys() - expected.keys())
    ]
    if len(by_host) != len(rules):
        problems.append("two HostUpdateStale rules name one host")
    for host, entry in sorted(expected.items()):
        want = update_stale_rule(host, entry["job"], entry["limit_seconds"])
        if _squash(by_host.get(host)) != _squash(want):
            problems.append(
                f"observability_expected_hosts lists {host}, and host-checks.yaml needs this "
                f"rule for it:\n{yaml.safe_dump([want], sort_keys=False, width=100)}"
            )
    return problems


def reconcile_failed_problems(rules: list[dict], hosts: set[str]) -> list[str]:
    """Why the `HostReconcileFailed` rules differ from one per host, or nothing.

    Args:
        rules: The `HostReconcileFailed` rules a rule file holds.
        hosts: The hosts `hosts.ini` declares.
    """
    problems = []
    named = set()
    for rule in rules:
        host = rule["labels"]["service"]
        named.add(host)
        want = f'homelab_host_reconcile_ok{{origin="{host}"}} == 0'
        if _squash(rule["expr"]) != want or rule.get("for") != "15m":
            problems.append(
                f"HostReconcileFailed for {host} reads {rule['expr']!r} for "
                f"{rule.get('for')!r}, not {want!r} for '15m'"
            )
    problems += [
        f"{host} is in hosts.ini and no HostReconcileFailed rule names it"
        for host in sorted(hosts - named)
    ]
    problems += [
        f"HostReconcileFailed names {host}, which hosts.ini does not declare"
        for host in sorted(named - hosts)
    ]
    return problems


def wired_link_expr(host: str, link: str) -> str:
    """The `WiredLinkDown` expression for `host`'s static link, whitespace squashed."""
    match = f'device="{link}",origin="{host}"'
    return f"node_network_carrier{{{match}}} == 0 or node_network_up{{{match}}} == 0"


def wired_link_problems(rules: list[dict], links: dict[str, str]) -> list[str]:
    """Why the `WiredLinkDown` rules differ from one per static link, or nothing.

    Args:
        rules: The `WiredLinkDown` rules a rule file holds.
        links: Each host's `k3s_node_static_link`, for the hosts that set one.
    """
    problems = []
    named = set()
    for rule in rules:
        host = rule["labels"]["service"]
        named.add(host)
        if host not in links:
            continue
        want = wired_link_expr(host, links[host])
        if _squash(rule["expr"]) != want or rule.get("for") != "5m":
            problems.append(
                f"WiredLinkDown for {host} reads {rule['expr']!r} for "
                f"{rule.get('for')!r}, not {want!r} for '5m'"
            )
    problems += [
        f"{host} sets k3s_node_static_link {links[host]} and no WiredLinkDown rule names it"
        for host in sorted(links.keys() - named)
    ]
    problems += [
        f"WiredLinkDown names {host}, whose host_vars set no k3s_node_static_link"
        for host in sorted(named - links.keys())
    ]
    return problems


def _hosts() -> set[str]:
    return {host.name for host in inventory_hosts()}


def _static_links() -> dict[str, str]:
    """Each inventory host's `k3s_node_static_link`, for the hosts that set one."""
    links = {}
    for host in sorted(_hosts()):
        path = HOST_VARS / f"{host}.yml"
        hv = (yaml_fast.safe_load(path.read_text()) if path.exists() else None) or {}
        if hv.get("k3s_node_static_link"):
            links[host] = hv["k3s_node_static_link"]
    return links


def test_the_shipped_update_stale_rules_match_the_expected_host_map_is_clean():
    expected = load_defaults(ROLE)["observability_expected_hosts"]
    assert update_stale_problems(_rules(RULES, "HostUpdateStale"), expected) == []


def test_the_fixture_rule_matches_the_one_host_fixture_map_is_clean():
    assert (
        update_stale_problems(_rules(FIXTURE, "HostUpdateStale"), FIXTURE_HOSTS) == []
    )


def test_a_listed_host_with_no_rule_is_flagged():
    [problem] = update_stale_problems([], FIXTURE_HOSTS)
    assert "lists fixture-host" in problem
    assert "> 11400" in problem


def test_a_rule_whose_limit_differs_from_the_map_is_flagged():
    moved = {"fixture-host": {"job": "node-fixture", "limit_seconds": 12000}}
    assert update_stale_problems(_rules(FIXTURE, "HostUpdateStale"), moved)


def test_a_rule_for_a_host_the_map_does_not_list_is_flagged():
    [problem] = update_stale_problems(_rules(FIXTURE, "HostUpdateStale"), {})
    assert "names fixture-host" in problem


def test_every_inventory_host_has_its_reconcile_rule_is_clean():
    hosts = _hosts()
    assert FLEET <= hosts
    assert reconcile_failed_problems(_rules(RULES, "HostReconcileFailed"), hosts) == []


def test_an_inventory_host_with_no_reconcile_rule_is_flagged():
    problems = reconcile_failed_problems(
        _rules(RULES, "HostReconcileFailed"), _hosts() | {"daniel-new"}
    )
    assert problems == [
        "daniel-new is in hosts.ini and no HostReconcileFailed rule names it"
    ]


def test_a_reconcile_rule_reading_another_host_is_flagged():
    [rule, *_] = _rules(RULES, "HostReconcileFailed")
    crossed = {**rule, "expr": 'homelab_host_reconcile_ok{origin="elsewhere"} == 0'}
    assert reconcile_failed_problems([crossed], {rule["labels"]["service"]})


def test_every_static_link_has_its_wired_link_rule_is_clean():
    links = _static_links()
    assert WIRED_NODES <= links.keys()
    assert wired_link_problems(_rules(RULES, "WiredLinkDown"), links) == []


def test_a_static_link_with_no_rule_is_flagged():
    links = {**_static_links(), "daniel-new": "eth0"}
    assert wired_link_problems(_rules(RULES, "WiredLinkDown"), links) == [
        "daniel-new sets k3s_node_static_link eth0 and no WiredLinkDown rule names it"
    ]


def test_a_rule_reading_a_link_the_host_no_longer_pins_is_flagged():
    links = {**_static_links(), "daniel-box": "enp1s0"}
    [problem] = wired_link_problems(_rules(RULES, "WiredLinkDown"), links)
    assert problem.startswith("WiredLinkDown for daniel-box reads")
    assert 'device="enp1s0"' in problem


def test_a_rule_for_a_host_with_no_static_link_is_flagged():
    links = {k: v for k, v in _static_links().items() if k != "daniel-server"}
    assert wired_link_problems(_rules(RULES, "WiredLinkDown"), links) == [
        "WiredLinkDown names daniel-server, whose host_vars set no k3s_node_static_link"
    ]
