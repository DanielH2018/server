#!/usr/bin/env python3
"""Render observability's host-plane rule groups from their tables.

Run: uv run python scripts/dev/gen_host_rules.py [--check]

`k8s/roles/observability/files/rule-tables/<group>.yaml` holds each rule of a host-plane group
once. This script expands every rule with a `hosts:` selector into one copy per host it selects
and writes `files/rules/<group>.yaml`, which `prometheus-rules.yaml.j2` carries verbatim and the
promtool tests load by path. A rule's `service` must be a literal
(`scripts/validate/alert_conventions.py`), and a node has no workload-service join, so the
expansion happens here rather than in PromQL. `--check` exits 1 when a committed rule file
differs from the render; the `gen-host-rules` prek hook and
`scripts/dev/tests/repo_tools/test_gen_host_rules.py` run it.

THE HOST TABLE is `observability_expected_hosts` in the role's defaults: each host's scrape `job`
and its `limit_seconds` for `HostUpdateStale`. A new host is one entry there. Every rule whose
selector takes it gains a copy at the next render. The script refuses to render while an
inventory host has no entry, so every host gets its reconcile and update pages.

A SELECTOR picks hosts from the host table:

- `all`: every host.
- `[daniel-box, ...]`: the hosts named, for a producer only those hosts run.
- `{job: node}`: the hosts scraped by that job.
- `{component: aide-scan}`: the hosts whose `hosts/<host>/package.yml` lists the component.
- `{host_var: k3s_node_static_link}`: the hosts whose host_vars set the variable.

A PLACEHOLDER `${name}` in any string of an expanded rule takes the host's value: `host`, `job`,
`limit_seconds`, `limit` (that limit as the annotations write a duration), and the variable a
`host_var` selector names. Every rule, with a `hosts:` key or without, also takes the host
table's counts: `host_count`, and `count_<job>` for each scrape job (`count_node`,
`count_node_pi`), so a coverage floor follows the table. A rule with no `hosts:` key is
otherwise copied as it stands, and an unknown placeholder is an error.

`per_host: {<host>: {...}}` merges a partial rule over one host's copy, for the copies that
differ in more than their placeholders.
"""

import re
import sys
from collections.abc import Iterable
from pathlib import Path


import yaml

from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.inventory_lists import host_names
from lib.repo_paths import HOST_VARS, K8S_ROLES, REPO

SELF = "scripts/dev/gen_host_rules.py"
ROLE = K8S_ROLES / "observability"
GROUPS = ("host-checks", "hosts")
PLACEHOLDER = re.compile(r"\$\{(\w+)\}")
TABLE_KEYS = ("hosts", "per_host")


class TableError(ValueError):
    """A rule table that cannot render."""


def duration(seconds: int) -> str:
    """`seconds` as the annotations write a duration: minutes under 2h, else hours and minutes."""
    if seconds < 2 * 3600:
        return f"{seconds // 60} minutes"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours} hours" + (f" {minutes} minutes" if minutes else "")


def _load(path: Path) -> dict:
    data = yaml_fast.safe_load(path.read_text()) if path.exists() else None
    return data if isinstance(data, dict) else {}


def expected_hosts(role: Path = ROLE) -> dict[str, dict]:
    """The host table: `observability_expected_hosts` from the role's defaults."""
    return _load(role / "defaults" / "main.yml")["observability_expected_hosts"]


def untabled_hosts(table: dict[str, dict], inventory: Iterable[str]) -> list[str]:
    """Each inventory host the host table does not list, as a message, or nothing."""
    return [
        f"{host} is an inventory host and observability_expected_hosts does not list it"
        for host in sorted(set(inventory) - table.keys())
    ]


def host_facts(
    hosts: dict[str, dict], repo: Path = REPO, host_vars: Path = HOST_VARS
) -> dict[str, dict]:
    """Each host's selector facts: its table entry, package components and host_vars."""
    facts = {}
    for host, entry in hosts.items():
        package = _load(repo / "hosts" / host / "package.yml")
        facts[host] = {
            **entry,
            "components": set(package.get("components") or []),
            "host_vars": _load(host_vars / f"{host}.yml"),
        }
    return facts


def select(selector: object, facts: dict[str, dict]) -> list[tuple[str, dict]]:
    """The hosts `selector` picks, in host-table order, each with its placeholder values."""

    def values(host: str, extra: dict | None = None) -> dict:
        fact = facts[host]
        limit = fact.get("limit_seconds")
        found = {"host": host, "job": fact["job"], "limit_seconds": limit}
        if limit is not None:
            found["limit"] = duration(limit)
        return {**found, **(extra or {})}

    if selector == "all":
        return [(host, values(host)) for host in facts]
    if isinstance(selector, list):
        unknown = sorted(set(selector) - facts.keys())
        if unknown:
            raise TableError(
                f"hosts: {unknown} are not in observability_expected_hosts"
            )
        return [(host, values(host)) for host in facts if host in selector]
    if isinstance(selector, dict) and len(selector) == 1:
        [(kind, want)] = selector.items()
        if kind == "job":
            return [(h, values(h)) for h, f in facts.items() if f["job"] == want]
        if kind == "component":
            return [(h, values(h)) for h, f in facts.items() if want in f["components"]]
        if kind == "host_var":
            return [
                (h, values(h, {want: f["host_vars"][want]}))
                for h, f in facts.items()
                if f["host_vars"].get(want)
            ]
    raise TableError(f"hosts: {selector!r} is no selector")


def _fill(value: object, found: dict, alert: str) -> object:
    if isinstance(value, dict):
        return {key: _fill(item, found, alert) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, found, alert) for item in value]
    if not isinstance(value, str):
        return value

    def one(match: re.Match) -> str:
        name = match.group(1)
        if found.get(name) is None:
            raise TableError(f"{alert}: ${{{name}}} has no value for this rule")
        return str(found[name])

    return PLACEHOLDER.sub(one, value)


def _filled(rule: dict, found: dict, alert: str) -> dict:
    return {key: _fill(value, found, alert) for key, value in rule.items()}


def _merge(base: dict, over: dict) -> dict:
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = _merge(base[key], value)
        else:
            merged[key] = value
    return merged


def counts(facts: dict[str, dict]) -> dict[str, int]:
    """The host table's counts every rule can read: `host_count` and `count_<job>`."""
    found = {"host_count": len(facts)}
    for fact in facts.values():
        key = "count_" + fact["job"].replace("-", "_")
        found[key] = found.get(key, 0) + 1
    return found


def expand(rules: list[dict], facts: dict[str, dict]) -> list[dict]:
    """The rule list a table's `rules` render to: one copy per selected host."""
    totals = counts(facts)
    out = []
    for rule in rules:
        alert = rule.get("alert", rule.get("record"))
        body = {key: value for key, value in rule.items() if key not in TABLE_KEYS}
        if "hosts" not in rule:
            out.append(_filled(body, totals, alert))
            continue
        picked = select(rule["hosts"], facts)
        overrides = rule.get("per_host") or {}
        stray = sorted(overrides.keys() - {host for host, _ in picked})
        if stray:
            raise TableError(
                f"{alert}: per_host names {stray}, which its selector skips"
            )
        for host, found in picked:
            out.append(
                _filled(
                    _merge(body, overrides.get(host) or {}), {**totals, **found}, alert
                )
            )
    return out


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else (">" if len(value) > 80 else None)
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


def banner(group: str) -> str:
    return (
        f"# GENERATED by {SELF} from files/rule-tables/{group}.yaml.\n"
        f"# Do not edit: change the table, then run `uv run python {SELF}`.\n"
    )


def render(group: str, role: Path = ROLE, facts: dict[str, dict] | None = None) -> str:
    """The text of `files/rules/<group>.yaml` for the table as it stands."""
    if facts is None:
        facts = host_facts(expected_hosts(role))
    table = _load(role / "files" / "rule-tables" / f"{group}.yaml")
    doc = {
        "groups": [
            {**entry, "rules": expand(entry["rules"], facts)}
            for entry in table["groups"]
        ]
    }
    body = yaml.dump(
        doc, Dumper=_Dumper, sort_keys=False, width=100, allow_unicode=True
    )
    return banner(group) + body


def main(argv: list[str] | None = None, role: Path = ROLE) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if a committed rule file differs from what this renders now",
    )
    args = parser.parse_args(argv)
    table = expected_hosts(role)
    inventory = host_names()
    missing = untabled_hosts(table, inventory)
    if missing:
        print("\n".join(missing), file=sys.stderr)
        return 1
    try:
        facts = host_facts(table)
        rendered = {group: render(group, role, facts) for group in GROUPS}
    except TableError as exc:
        print(exc, file=sys.stderr)
        return 1
    stale = []
    for group, text in rendered.items():
        path = role / "files" / "rules" / f"{group}.yaml"
        if path.exists() and path.read_text() == text:
            continue
        if args.check:
            stale.append(path)
        else:
            path.write_text(text)
            print(
                f"wrote {path.relative_to(REPO) if path.is_relative_to(REPO) else path}"
            )
    for path in stale:
        print(
            f"{path.name} differs from its table; run `uv run python {SELF}`",
            file=sys.stderr,
        )
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
