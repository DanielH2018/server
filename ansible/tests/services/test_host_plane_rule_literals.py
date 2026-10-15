"""Each host-plane rule in observability's `host-checks` group names the host it must.

`files/rules/host-checks.yaml` is plain YAML, so each rule names its host as a literal:

- one `HostReconcileFailed` per host `hosts.ini` declares, reading that host's `origin`;
- one `HostUpdateStale` per entry of `observability_expected_hosts`, equal to the rule
  `update_stale_rule` builds from the entry. `tests/rules/fixtures/host-update-stale.yaml`
  holds that rule for `FIXTURE_HOSTS`, the one-host map `host-checks.test.yaml` fires it from,
  so the shape has promtool cases while the shipped map is empty.

Run: uv run pytest ansible/tests/services/test_host_plane_rule_literals.py
"""

import re

import yaml
from _helpers import K8S_ROLES, load_defaults
from lib import yaml_fast
from lib.ansible_inventory import inventory_hosts

ROLE = K8S_ROLES / "observability"
RULES = ROLE / "files" / "rules" / "host-checks.yaml"
FIXTURE = ROLE / "tests" / "rules" / "fixtures" / "host-update-stale.yaml"
FIXTURE_HOSTS = {"daniel-pi": {"job": "node-pi", "limit_seconds": 11400}}
# Non-vacuity: the hosts hosts.ini must keep declaring for the reconcile census to mean much.
FLEET = frozenset({"daniel-box", "daniel-server", "daniel-pi"})


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
            "summary": f"{host} has had no successful homelab update for {limit_seconds}s.",
            "description": (
                f"No homelab-updater run on {host} has succeeded in {limit_seconds}s, or its "
                "exporter is up and serves no success timestamp. A failed fetch, a stopped "
                "homelab-apt-repo.service, a firewall rule, a keyring mismatch and a stopped "
                "timer each leave it stale. Read `systemctl status homelab-updater.timer` and "
                f"`journalctl -u homelab-updater` on {host}, and docs/host-packages.md, "
                '"Host-plane pages".'
            ),
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


def _hosts() -> set[str]:
    return {host.name for host in inventory_hosts()}


def test_the_shipped_update_stale_rules_match_the_expected_host_map_is_clean():
    expected = load_defaults(ROLE)["observability_expected_hosts"]
    assert update_stale_problems(_rules(RULES, "HostUpdateStale"), expected) == []


def test_the_fixture_rule_matches_the_one_host_fixture_map_is_clean():
    assert (
        update_stale_problems(_rules(FIXTURE, "HostUpdateStale"), FIXTURE_HOSTS) == []
    )


def test_a_listed_host_with_no_rule_is_flagged():
    [problem] = update_stale_problems([], FIXTURE_HOSTS)
    assert "lists daniel-pi" in problem
    assert "> 11400" in problem


def test_a_rule_whose_limit_differs_from_the_map_is_flagged():
    moved = {"daniel-pi": {"job": "node-pi", "limit_seconds": 12000}}
    assert update_stale_problems(_rules(FIXTURE, "HostUpdateStale"), moved)


def test_a_rule_for_a_host_the_map_does_not_list_is_flagged():
    [problem] = update_stale_problems(_rules(FIXTURE, "HostUpdateStale"), {})
    assert "names daniel-pi" in problem


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
