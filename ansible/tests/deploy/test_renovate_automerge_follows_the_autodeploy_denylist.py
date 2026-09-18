#!/usr/bin/env python3
"""Renovate automerges a k8s image bump only for a role the deployer may auto-deploy.

Issue #1886. renovate.json automerges minor/patch and digest image bumps under
`ansible/roles/k8s/**`; the GitOps deployer applies such a bump unattended only for a role
outside the denylist that `ansible/filter_plugins/k8s_autodeploy.py` derives from each role's
`k8s_autodeploy` flag. The two boundaries disagreed in one direction: Renovate merged authelia
4.39.23 (#1831) and 4.39.24 (#1862) unattended, the tick fast-forwarded each and applied nothing,
and `Release Staleness Drift` paged a person for the hand deploy both times.

The fix is one `automerge: false` rule whose `matchFileNames` is the denylist spelled as paths.
That list is typed into renovate.json, so this guard is what stops it drifting from the flags:
it derives the expected set the same way the deployer does and asserts equality, and its
failure message prints the exact line to add or drop. It also pins the rule's position, which
is load-bearing — later rules win in Renovate, so the rule must follow the two automerge rules
it overrides and precede the per-package manual rules that override its groupName.

One path in the rule is not a role's defaults: `ansible/inventory/group_vars/all.yml` (issue
#1936). `crowdsec_k8s_image` lives there because traefik's and authelia's sidecars read it too,
and the per-role rule left it automerging while the deployer routed the merged bump through its
broad plane — which applies every role reading the key with no denylist check. That path is
safe in this rule only while every `_image:` pin in group_vars belongs to denied roles, so the
census below reads each pin's Jinja readers out of the role trees and asserts they are all
denied. A pin no role reads fails too: an unmappable pin must not pass as "not eligible".

Run: uv run pytest ansible/tests/deploy/test_renovate_automerge_follows_the_autodeploy_denylist.py
"""

import json
import re
from pathlib import Path

import pytest
from lib import yaml_fast

from _autodeploy import _denylist
from _helpers import REPO

_RENOVATE = REPO / "renovate.json"

# The rule is found by the fixed prefix of its groupName: `{{depName}}` keeps Renovate's
# per-service grouping (one PR per image, the deployer's rollback unit), and the parenthetical
# is the `manual —` tell the renovate-prs skill triages by.
MANUAL_GROUP_PREFIX = "k8s image {{depName}} (manual — k8s_autodeploy: false"

# The automerge rules the manual rule must FOLLOW, and the first per-package manual rule it
# must PRECEDE — the CrowdSec bouncer plugin pin shares traefik's defaults/main.yml, and its
# own rule must keep the last word on that pin's group name.
AUTOMERGE_GROUP = "k8s image {{depName}}"
DIGEST_UPDATE_TYPES = ["digest"]
FIRST_PER_PACKAGE_MANUAL_GROUP_PREFIX = "crowdsec bouncer plugin (manual"

# The one manual path that is not a denied role's defaults. Fixed here rather than tolerated
# as "any extra": a second path added by hand has to be argued for the way this one was.
GROUP_VARS = "ansible/inventory/group_vars/all.yml"
_GROUP_VARS_PATH = REPO / GROUP_VARS
_K8S_ROLES = REPO / "ansible" / "roles" / "k8s"

# The pin the census must find, so a group_vars with no `_image:` key (renamed, moved back to
# a role) fails as a missing member rather than passing over nothing.
KNOWN_GROUP_VARS_PINS = frozenset({"crowdsec_k8s_image"})

# A Jinja reference to the var — `{{ var }}`, `{% if var %}` — not a mention in a comment:
# sonarr's defaults cite crowdsec_k8s_image in prose and read nothing.
_JINJA_REF_PREFIX = r"\{[{%][^}%]*\b"

# The floor the derived denylist must clear, mirroring test_the_real_repo_derives_a_plausible_denylist:
# below it the filter has stopped reading roles and every assertion here runs over nothing.
_DENYLIST_FLOOR = 34
_LOAD_BEARING = frozenset({"traefik", "authelia", "code-server"})


def expected_match_file_names(denylist: set[str]) -> list[str]:
    """The denylist as Renovate must spell it: group_vars, then one `defaults/main.yml` per denied role."""
    return [GROUP_VARS] + [
        f"ansible/roles/k8s/{role}/defaults/main.yml" for role in sorted(denylist)
    ]


def group_vars_image_pins(text: str) -> set[str]:
    """Every top-level `*_image:` key in a group_vars document."""
    data = yaml_fast.safe_load(text) or {}
    return {k for k in data if str(k).endswith("_image")}


def roles_reading(var: str, roles_dir: Path = _K8S_ROLES) -> set[str]:
    """The k8s roles whose files reference `var` from inside a Jinja tag."""
    pattern = re.compile(_JINJA_REF_PREFIX + re.escape(var) + r"\b")
    readers: set[str] = set()
    for role in roles_dir.iterdir():
        if not role.is_dir():
            continue
        for f in role.rglob("*"):
            if f.is_file() and f.suffix in {".j2", ".yml", ".yaml"}:
                if pattern.search(f.read_text(errors="replace")):
                    readers.add(role.name)
                    break
    return readers


def group_vars_pin_problems(
    readers_by_pin: dict[str, set[str]], denylist: set[str]
) -> list[str]:
    """Every group_vars image pin some eligible role reads, or no role reads. Empty means none.

    Pure over `{pin: reader roles}` so the red half of the pair below can hand it a pin an
    eligible role reads without editing the tree.
    """
    problems: list[str] = []
    for pin, readers in sorted(readers_by_pin.items()):
        if not readers:
            problems.append(
                f"{pin} in {GROUP_VARS} is read by no k8s role — an unmappable pin cannot be "
                "argued as denied; find its reader or move it"
            )
            continue
        eligible = sorted(readers - denylist)
        if eligible:
            problems.append(
                f"{pin} in {GROUP_VARS} is read by auto-deployable role(s) {eligible}, so the "
                "manual rule's group_vars entry would hold an eligible bump too — move the pin "
                "into the role's defaults or deny the role"
            )
    return problems


def find_rule(rules: list[dict], group_prefix: str) -> tuple[int, dict]:
    """Index and body of the single rule whose groupName starts with `group_prefix`."""
    hits = [
        (i, r)
        for i, r in enumerate(rules)
        if str(r.get("groupName", "")).startswith(group_prefix)
    ]
    assert len(hits) == 1, (
        f"expected exactly one rule with groupName starting {group_prefix!r}, found {len(hits)}"
    )
    return hits[0]


def manual_rule_problems(rules: list[dict], denylist: set[str]) -> list[str]:
    """Every way the manual rule can disagree with the denylist. Empty means they agree.

    Pure over the parsed `packageRules` so the red half of the pair below can hand it a rule
    set with a role missing, without editing renovate.json.
    """
    problems: list[str] = []
    _, rule = find_rule(rules, MANUAL_GROUP_PREFIX)
    if rule.get("automerge") is not False:
        problems.append("the manual rule does not set `automerge: false`")
    if "matchUpdateTypes" in rule:
        problems.append(
            "the manual rule sets matchUpdateTypes, so an update type it does not name "
            "(digest is the one that bit tdarr) falls back to the automerge rules above"
        )
    actual = set(rule.get("matchFileNames", []))
    expected = set(expected_match_file_names(denylist))
    for path in sorted(expected - actual):
        problems.append(
            f"denied role missing from matchFileNames — add {json.dumps(path)}"
        )
    for path in sorted(actual - expected):
        problems.append(
            f"matchFileNames names a path no denied role owns — drop {json.dumps(path)}"
        )
    return problems


def _rules() -> list[dict]:
    return json.loads(_RENOVATE.read_text())["packageRules"]


def test_the_denylist_this_guard_reads_is_not_vacuous() -> None:
    denied = _denylist()
    assert len(denied) >= _DENYLIST_FLOOR, sorted(denied)
    assert _LOAD_BEARING <= denied, sorted(_LOAD_BEARING - denied)


def test_the_manual_rule_matches_exactly_the_denied_roles() -> None:
    problems = manual_rule_problems(_rules(), _denylist())
    assert not problems, "\n".join(problems)


def test_the_manual_rule_sits_between_the_automerge_rules_and_the_per_package_manual_rules() -> (
    None
):
    rules = _rules()
    manual_at, _ = find_rule(rules, MANUAL_GROUP_PREFIX)
    automerge_at = next(
        i for i, r in enumerate(rules) if r.get("groupName") == AUTOMERGE_GROUP
    )
    digest_at = next(
        i
        for i, r in enumerate(rules)
        if r.get("matchUpdateTypes") == DIGEST_UPDATE_TYPES
    )
    first_per_package_at, _ = find_rule(rules, FIRST_PER_PACKAGE_MANUAL_GROUP_PREFIX)
    assert automerge_at < manual_at, (
        "the manual rule must follow the per-service automerge rule it overrides"
    )
    assert digest_at < manual_at, (
        "the manual rule must follow the digest automerge rule it overrides"
    )
    assert manual_at < first_per_package_at, (
        "the manual rule must precede the per-package manual rules, which override its groupName"
    )


def test_every_group_vars_image_pin_belongs_to_denied_roles() -> None:
    pins = group_vars_image_pins(_GROUP_VARS_PATH.read_text())
    assert KNOWN_GROUP_VARS_PINS <= pins, sorted(KNOWN_GROUP_VARS_PINS - pins)
    problems = group_vars_pin_problems({p: roles_reading(p) for p in pins}, _denylist())
    assert not problems, "\n".join(problems)


def test_the_manual_rule_is_scoped_to_container_images() -> None:
    """The bouncer plugin pin shares traefik's defaults/main.yml; this rule must not claim it."""
    _, rule = find_rule(_rules(), MANUAL_GROUP_PREFIX)
    assert rule.get("matchDatasources") == ["docker"]
    assert rule.get("matchManagers") == ["custom.regex"]


# --- the red-proof pair: the checker must accept a matching rule and reject a drifted one ---

_DENYLIST = {"authelia", "traefik"}


def _rule_set(paths: list[str], **overrides) -> list[dict]:
    rule = {
        "groupName": MANUAL_GROUP_PREFIX + ", so the tick applies nothing)",
        "automerge": False,
        "matchFileNames": paths,
    }
    rule.update(overrides)
    return [{"groupName": AUTOMERGE_GROUP, "automerge": True}, rule]


def test_a_rule_spelling_the_denylist_exactly_is_clean() -> None:
    assert (
        manual_rule_problems(_rule_set(expected_match_file_names(_DENYLIST)), _DENYLIST)
        == []
    )


def test_a_denied_role_missing_from_the_rule_is_flagged() -> None:
    problems = manual_rule_problems(
        _rule_set(expected_match_file_names({"authelia"})), _DENYLIST
    )
    assert problems == [
        'denied role missing from matchFileNames — add "ansible/roles/k8s/traefik/defaults/main.yml"'
    ]


def test_a_promoted_role_still_in_the_rule_is_flagged() -> None:
    problems = manual_rule_problems(
        _rule_set(expected_match_file_names(_DENYLIST | {"sonarr"})), _DENYLIST
    )
    assert problems == [
        'matchFileNames names a path no denied role owns — drop "ansible/roles/k8s/sonarr/defaults/main.yml"'
    ]


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"automerge": True}, "automerge: false"),
        ({"matchUpdateTypes": ["minor", "patch"]}, "matchUpdateTypes"),
    ],
)
def test_a_rule_that_still_automerges_some_way_is_flagged(
    overrides: dict, fragment: str
) -> None:
    problems = manual_rule_problems(
        _rule_set(expected_match_file_names(_DENYLIST), **overrides), _DENYLIST
    )
    assert any(fragment in p for p in problems), problems


def test_a_group_vars_pin_read_only_by_denied_roles_is_clean() -> None:
    assert (
        group_vars_pin_problems({"x_image": {"authelia", "traefik"}}, _DENYLIST) == []
    )


def test_a_group_vars_pin_an_eligible_role_reads_is_flagged() -> None:
    problems = group_vars_pin_problems({"x_image": {"authelia", "sonarr"}}, _DENYLIST)
    assert len(problems) == 1 and "['sonarr']" in problems[0], problems


def test_a_group_vars_pin_no_role_reads_is_flagged() -> None:
    problems = group_vars_pin_problems({"x_image": set()}, _DENYLIST)
    assert len(problems) == 1 and "read by no k8s role" in problems[0], problems


def test_the_jinja_reference_census_ignores_a_comment(tmp_path: Path) -> None:
    """sonarr's defaults cite crowdsec_k8s_image in a comment and must not count as a reader."""
    (tmp_path / "reader" / "templates").mkdir(parents=True)
    (tmp_path / "reader" / "templates" / "d.yaml.j2").write_text(
        "image: {{ x_image }}\n"
    )
    (tmp_path / "citer" / "defaults").mkdir(parents=True)
    (tmp_path / "citer" / "defaults" / "main.yml").write_text(
        "# see x_image in group_vars\n"
    )
    assert roles_reading("x_image", tmp_path) == {"reader"}


def test_a_missing_manual_rule_fails_rather_than_passing_over_nothing() -> None:
    with pytest.raises(AssertionError, match="expected exactly one rule"):
        manual_rule_problems(
            [{"groupName": AUTOMERGE_GROUP, "automerge": True}], _DENYLIST
        )
