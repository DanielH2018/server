#!/usr/bin/env python3
"""The edge and DNS images keep one Renovate PR per image while every other denied image batches.

The denylist rule in renovate.json groups a Monday's bumps across the denied roles into one PR.
A later rule gives traefik, authelia, crowdsec, Pi-hole and unbound back their per-image
`{{depName}}` group, because a regression in one of them takes down every route or LAN DNS.
Later rules win in Renovate, so that rule must follow the denylist rule. Its files must stay
inside the denylist, or the `k8s_autodeploy: false` marker lands on an eligible role's PR. Each
package it names must be pinned in one of its files, or a moved pin silently rejoins the batch.

Run: uv run pytest tests/deploy/test_renovate_edge_images_stay_one_per_pr.py
"""

import json
import re

from _helpers import REPO
from test_renovate_automerge_follows_the_autodeploy_denylist import (
    MANUAL_GROUP_PREFIX,
    find_rule,
)

_RENOVATE = REPO / "renovate.json"

EDGE_GROUP_PREFIX = "k8s image {{depName}} (manual — k8s_autodeploy: false"

KNOWN_EDGE_PACKAGES = frozenset(
    {
        "authelia/authelia",
        "crowdsecurity/crowdsec",
        "klutchell/unbound",
        "pihole/pihole",
        "traefik",
    }
)


def is_pinned(package: str, text: str) -> bool:
    """True when `text` holds an `_image:` key pinning `package` by tag or digest."""
    return re.search(rf"_image:\s*{re.escape(package)}[:@]", text) is not None


def edge_rule_problems(rules: list[dict], file_texts: dict[str, str]) -> list[str]:
    """Every way the edge rule fails to keep its images out of the batch. Empty means none.

    Args:
      rules: the parsed `packageRules`.
      file_texts: the content of each path the edge rule lists, keyed by repo-relative path.
    """
    manual_at, manual = find_rule(rules, MANUAL_GROUP_PREFIX)
    edge_at, edge = find_rule(rules, EDGE_GROUP_PREFIX)
    problems = []
    if edge_at < manual_at:
        problems.append(
            "the edge rule precedes the denylist rule, so the batch groupName overrides it"
        )
    outside = sorted(
        set(edge.get("matchFileNames", [])) - set(manual["matchFileNames"])
    )
    if outside:
        problems.append(f"the edge rule lists files outside the denylist: {outside}")
    for package in edge.get("matchPackageNames", []):
        if not any(
            is_pinned(package, file_texts.get(p, "")) for p in edge["matchFileNames"]
        ):
            problems.append(f"{package} is pinned in none of the edge rule's files")
    return problems


def _rules() -> list[dict]:
    return json.loads(_RENOVATE.read_text())["packageRules"]


def test_the_edge_rule_names_every_known_edge_package() -> None:
    _, edge = find_rule(_rules(), EDGE_GROUP_PREFIX)
    missing = KNOWN_EDGE_PACKAGES - set(edge["matchPackageNames"])
    assert not missing, sorted(missing)


def test_the_edge_rule_keeps_its_images_out_of_the_batch() -> None:
    rules = _rules()
    _, edge = find_rule(rules, EDGE_GROUP_PREFIX)
    texts = {p: (REPO / p).read_text() for p in edge["matchFileNames"]}
    problems = edge_rule_problems(rules, texts)
    assert not problems, "\n".join(problems)


# --- the red-proof pair ---

_MANUAL = {
    "groupName": MANUAL_GROUP_PREFIX + ", so a person merges it)",
    "matchFileNames": [
        "roles/traefik/defaults/main.yml",
        "roles/nut/defaults/main.yml",
    ],
}
_EDGE = {
    "groupName": EDGE_GROUP_PREFIX + ", so a person merges it)",
    "matchFileNames": ["roles/traefik/defaults/main.yml"],
    "matchPackageNames": ["traefik"],
    "groupSingleUpdates": True,
}
_TEXTS = {
    "roles/traefik/defaults/main.yml": "traefik_k8s_image: traefik:v3.7.13@sha256:ab\n"
}


def test_an_edge_rule_after_the_denylist_inside_its_files_is_clean() -> None:
    assert edge_rule_problems([_MANUAL, _EDGE], _TEXTS) == []


def test_an_edge_rule_before_the_denylist_or_outside_its_files_is_flagged() -> None:
    stray = _EDGE | {
        "matchFileNames": [
            "roles/traefik/defaults/main.yml",
            "roles/docs/defaults/main.yml",
        ],
        "matchPackageNames": ["traefik", "pihole/pihole"],
    }
    problems = edge_rule_problems([stray, _MANUAL], _TEXTS)
    assert len(problems) == 3, problems
    assert "precedes" in problems[0]
    assert "outside the denylist" in problems[1]
    assert "pihole/pihole is pinned in none" in problems[2]
