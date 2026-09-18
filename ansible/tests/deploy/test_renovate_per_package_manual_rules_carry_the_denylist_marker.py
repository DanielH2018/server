#!/usr/bin/env python3
"""A per-package manual Renovate rule whose pin a denied role owns carries `k8s_autodeploy: false`.

Issue #1963. The unattended `renovate_agent` keys its exclusion on one phrase in the PR title,
`k8s_autodeploy: false`, which the denylist rule's groupName carries (#1939). The per-package
manual rules below that rule override its groupName for the pins they own, and a
github-releases or pypi pin never reached it at all. Either way the title loses the phrase, so
the crowdsec bouncer plugin bump — a traefik redeploy whose failure the health gate cannot
see — shipped through `land.sh --arm-merge` like a jellyfin plugin work order.

The rule is the class, not the instance: every per-package manual rule whose pin a denied
role owns ends its parenthetical with the marker, and one whose pin no denied role owns does
not, so a role promoted out of the denylist drops the marker with it. Who owns a pin is read
from `customManagers`: a manager with a `depNameTemplate` names its package outright, so every
file it matches owns the pin; one without derives the name from the file, so the file must
carry the package literally (the `_image:` pins). `test_renovate_agent_unit.py` reads the
marker out of every such groupName and pins that the prompt names the same phrase.

Run: uv run pytest ansible/tests/deploy/test_renovate_per_package_manual_rules_carry_the_denylist_marker.py
"""

import json
import re
from pathlib import Path

from _autodeploy import _K8S_ROLES, _denylist
from _helpers import REPO
from test_renovate_automerge_follows_the_autodeploy_denylist import (
    MANUAL_GROUP_PREFIX,
    find_rule,
)

_RENOVATE = REPO / "renovate.json"

DENYLIST_MARKER = "k8s_autodeploy: false"

# The per-package groups the census must find owning a denied role's pin, so a rename that
# drops one out of the census fails as a missing member rather than passing over nothing.
KNOWN_DENIED_PER_PACKAGE_GROUPS = frozenset(
    {"crowdsec bouncer plugin", "meilisearch", "karakeep time-tagger pip deps"}
)


def _k8s_relative_files(roles_dir: Path = _K8S_ROLES) -> list[str]:
    """Every file under the k8s roles tree, as the repo-relative path Renovate matches on."""
    return [str(f.relative_to(REPO)) for f in roles_dir.rglob("*") if f.is_file()]


def _file_pattern(pattern: str) -> re.Pattern[str]:
    """A `managerFilePatterns` entry as a regex. Every entry here is the `/regex/` form."""
    assert pattern.startswith("/") and pattern.endswith("/"), pattern
    return re.compile(pattern[1:-1])


def _read(rel: str) -> str:
    return (REPO / rel).read_text(errors="replace")


def pin_owner_roles(
    package: str, managers: list[dict], files: list[str], read=_read
) -> set[str]:
    """The k8s roles whose files a custom manager reads `package` from."""
    owners: set[str] = set()
    for manager in managers:
        named = manager.get("depNameTemplate")
        if named is not None and named != package:
            continue
        for pattern in map(_file_pattern, manager.get("managerFilePatterns", [])):
            for rel in files:
                if not pattern.search(rel):
                    continue
                if named is None and package not in read(rel):
                    continue
                owners.add(Path(rel).parts[3])
    return owners


def per_package_manual_rules(rules: list[dict]) -> list[dict]:
    """The `manual` rules after the denylist rule that match by package name."""
    manual_at, _ = find_rule(rules, MANUAL_GROUP_PREFIX)
    return [
        r
        for r in rules[manual_at + 1 :]
        if r.get("matchPackageNames") and "(manual" in str(r.get("groupName", ""))
    ]


def per_package_marker_problems(
    rules: list[dict], owners_by_package: dict[str, set[str]], denylist: set[str]
) -> list[str]:
    """Every per-package manual rule whose marker disagrees with who owns its pin. Empty means none.

    Pure over the parsed rules and a `{package: owner roles}` map, so the red half of the pair
    below can hand it a denied owner without editing the tree.
    """
    problems: list[str] = []
    for rule in per_package_manual_rules(rules):
        group = str(rule["groupName"])
        owners: set[str] = set()
        for package in rule["matchPackageNames"]:
            owners |= owners_by_package.get(package, set())
        denied = sorted(owners & denylist)
        carries = DENYLIST_MARKER in group
        if denied and not carries:
            problems.append(
                f"{group!r} owns a pin in denied role(s) {denied} but its groupName omits "
                f"{DENYLIST_MARKER!r}, so the renovate_agent lands it unattended — append "
                f"'; {DENYLIST_MARKER}' inside the parenthetical"
            )
        if carries and not denied:
            problems.append(
                f"{group!r} carries {DENYLIST_MARKER!r} but no denied role owns its pin "
                f"(owners: {sorted(owners)}) — drop the marker, a person is not needed there"
            )
    return problems


def _renovate() -> dict:
    return json.loads(_RENOVATE.read_text())


def _owners_by_package(config: dict) -> dict[str, set[str]]:
    files = _k8s_relative_files()
    return {
        package: pin_owner_roles(package, config["customManagers"], files)
        for rule in per_package_manual_rules(config["packageRules"])
        for package in rule["matchPackageNames"]
    }


def test_every_per_package_manual_rule_on_a_denied_role_carries_the_marker() -> None:
    config = _renovate()
    problems = per_package_marker_problems(
        config["packageRules"], _owners_by_package(config), _denylist()
    )
    assert not problems, "\n".join(problems)


def test_the_per_package_census_finds_the_known_denied_groups() -> None:
    config = _renovate()
    owners_by_package = _owners_by_package(config)
    denylist = _denylist()
    denied_groups = {
        str(rule["groupName"]).split(" (manual")[0]
        for rule in per_package_manual_rules(config["packageRules"])
        if any(owners_by_package[p] & denylist for p in rule["matchPackageNames"])
    }
    assert KNOWN_DENIED_PER_PACKAGE_GROUPS <= denied_groups, sorted(
        KNOWN_DENIED_PER_PACKAGE_GROUPS - denied_groups
    )


# --- the pin-owner census: a named package maps by pattern, a derived one by file content ---

_DEFAULTS_PATTERN = "/^ansible/roles/k8s/[^/]+/defaults/main\\.yml$/"


def test_a_named_package_manager_maps_the_pin_to_every_file_it_matches() -> None:
    managers = [
        {"managerFilePatterns": [_DEFAULTS_PATTERN], "depNameTemplate": "vendor/plugin"}
    ]
    files = [
        "ansible/roles/k8s/traefik/defaults/main.yml",
        "ansible/roles/k8s/traefik/templates/deployment.yaml.j2",
    ]
    assert pin_owner_roles("vendor/plugin", managers, files, read=lambda _: "") == {
        "traefik"
    }
    assert pin_owner_roles("vendor/other", managers, files, read=lambda _: "") == set()


def test_a_content_derived_manager_maps_the_pin_only_where_the_file_names_it() -> None:
    managers = [{"managerFilePatterns": [_DEFAULTS_PATTERN]}]
    files = [
        "ansible/roles/k8s/karakeep/defaults/main.yml",
        "ansible/roles/k8s/sonarr/defaults/main.yml",
    ]
    text = {
        files[0]: "meili_image: getmeili/meilisearch:v1.0\n",
        files[1]: "x_image: a/b:1\n",
    }
    assert pin_owner_roles("getmeili/meilisearch", managers, files, read=text.get) == {
        "karakeep"
    }


# --- the red-proof pair: the checker must accept an agreeing rule and reject a drifted one ---

_DENYLIST = {"authelia", "traefik"}


def _rule_set(group: str, package: str = "vendor/plugin") -> list[dict]:
    return [
        {"groupName": MANUAL_GROUP_PREFIX + ", so the tick applies nothing)"},
        {"groupName": group, "matchPackageNames": [package], "automerge": False},
    ]


def test_a_denied_owner_whose_rule_carries_the_marker_is_clean() -> None:
    rules = _rule_set("plugin (manual — finish it; k8s_autodeploy: false)")
    assert (
        per_package_marker_problems(rules, {"vendor/plugin": {"traefik"}}, _DENYLIST)
        == []
    )


def test_an_eligible_owner_whose_rule_omits_the_marker_is_clean() -> None:
    rules = _rule_set("plugin (manual — finish it)")
    assert (
        per_package_marker_problems(rules, {"vendor/plugin": {"jellyfin"}}, _DENYLIST)
        == []
    )


def test_a_denied_owner_whose_rule_omits_the_marker_is_flagged() -> None:
    rules = _rule_set("plugin (manual — finish it)")
    problems = per_package_marker_problems(
        rules, {"vendor/plugin": {"traefik"}}, _DENYLIST
    )
    assert len(problems) == 1 and "['traefik']" in problems[0], problems


def test_a_promoted_owner_whose_rule_still_carries_the_marker_is_flagged() -> None:
    rules = _rule_set("plugin (manual — finish it; k8s_autodeploy: false)")
    problems = per_package_marker_problems(
        rules, {"vendor/plugin": {"jellyfin"}}, _DENYLIST
    )
    assert len(problems) == 1 and "drop the marker" in problems[0], problems
