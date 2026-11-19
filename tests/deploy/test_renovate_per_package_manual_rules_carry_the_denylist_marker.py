#!/usr/bin/env python3
"""A per-package manual Renovate rule whose pin a denied role owns carries `k8s_autodeploy: false`.

The unattended `renovate_agent` keys its exclusion on one phrase in the PR title,
`k8s_autodeploy: false`, which the denylist rule's groupName carries. The per-package
manual rules below that rule override its groupName for the pins they own, and a
github-releases or pypi pin never reaches it at all. Either way the title loses the phrase, so
the crowdsec bouncer plugin bump — a traefik redeploy whose failure the health gate cannot
see — would ship through `land.sh --arm-merge` like a jellyfin plugin work order.

The rule is the class, not the instance: every per-package manual rule whose pin a denied
role owns ends its parenthetical with the marker, and one whose pin no denied role owns does
not, so a role promoted out of the denylist drops the marker with it. Who owns a pin is read
from `scripts/validate/renovate_expected_deps.tsv`, the table CI holds against Renovate's own
extract: the role whose file Renovate extracts a dep from owns it, matched by depName or by
packageName where the two differ. The n8n Dockerfiles' FROM lines are the built-in
`dockerfile` manager's rows, so n8n owns its base image. A pin no role owns fails rather than
passing as "not denied".
`test_renovate_agent_unit.py` reads the marker out of every such groupName and pins that the
prompt names the same phrase.

Run: uv run pytest tests/deploy/test_renovate_per_package_manual_rules_carry_the_denylist_marker.py
"""

import json
import re
from pathlib import Path, PurePosixPath

import pytest

from _autodeploy import _denylist
from _helpers import REPO
from validate.renovate_extract import TABLE, Row, read_table
from test_renovate_automerge_follows_the_autodeploy_denylist import (
    MANUAL_GROUP_PREFIX,
    find_rule,
)

_RENOVATE = REPO / "renovate.json"

DENYLIST_MARKER = "k8s_autodeploy: false"

# The per-package groups the census must find owning a denied role's pin, so a rename that
# drops one out of the census fails as a missing member rather than passing over nothing.
KNOWN_DENIED_PER_PACKAGE_GROUPS = frozenset(
    {"crowdsec bouncer plugin", "meilisearch", "karakeep time-tagger pip deps", "n8n"}
)


_ROLES = REPO / "k8s" / "roles"

# Built-in managers that read no file a role deploys, each with its default
# `managerFilePatterns` (upstream `lib/modules/manager/<name>/index.ts`). A rule whose
# `matchManagers` names only these reads its pin from a workflow or a local action, so no role
# owns the pin and no deploy follows its merge. Such a rule is outside this census rather than
# unmappable: the cosign binary in the promote and prune workflows is one.
# `test_no_role_file_is_read_by_a_non_role_manager` checks the "no role file" half.
NON_ROLE_MANAGERS = {
    "github-actions": (
        "/(^|/)(workflow-templates|\\.(?:github|gitea|forgejo)/(?:workflows|actions))/.+\\.ya?ml$/",
        "/(^|/)action\\.ya?ml$/",
    ),
}


def _role_relative_files(roles_dir: Path = _ROLES) -> list[str]:
    """Every file under `k8s/roles/`, as the repo-relative path Renovate matches on.

    Every plane, not just k8s: a setup-plane pin (sops, k3s, coredns) must map to its own
    role so the guard can tell "eligible" from "unmappable".
    """
    return [str(f.relative_to(REPO)) for f in roles_dir.rglob("*") if f.is_file()]


def _file_pattern(pattern: str) -> re.Pattern[str]:
    """A `managerFilePatterns` entry as a regex. Every entry here is the `/regex/` form."""
    assert pattern.startswith("/") and pattern.endswith("/"), pattern
    return re.compile(pattern[1:-1])


def _in_file_scope(rel: str, file_names: list[str]) -> bool:
    """Whether a rule's `matchFileNames` (minimatch globs or `/regex/`) selects `rel`.

    A brace glob, a `!` negation or a flagged `/regex/i` is refused rather than read as
    matching nothing, which would drop the rule from the census unseen.
    """
    for entry in file_names:
        assert not entry.startswith("!") and "{" not in entry, entry
        assert not re.fullmatch(r"/.*/[a-z]+", entry), entry
        if len(entry) > 1 and entry.startswith("/") and entry.endswith("/"):
            if re.search(entry[1:-1], rel):
                return True
        elif PurePosixPath(rel).full_match(entry):
            return True
    return False


def pin_owner_roles(
    package: str,
    deps: list[Row],
    datasources: set[str] | None = None,
    file_names: list[str] | None = None,
) -> set[str]:
    """The k8s roles whose files Renovate's extract reads `package` from.

    `datasources` is the rule's `matchDatasources`: a dep in a datasource the rule never
    matches is not its pin, whatever its name. Without it, a github-releases rule for
    `astral-sh/uv` read as karakeep's, whose `_image:` names `ghcr.io/astral-sh/uv`, a
    different package in a different datasource. None means the rule matches every
    datasource.

    `file_names` is the rule's `matchFileNames`, which narrows the files the same way: a
    Pi-only wg-easy rule does not own `k8s/roles/wg-easy`'s pin. None means every file.
    """
    return {
        Path(dep.file).parts[2]
        for dep in deps
        if dep.file.startswith("k8s/roles/")
        and package in dep.names()
        and not (datasources and dep.datasource not in datasources)
        and not (file_names and not _in_file_scope(dep.file, file_names))
    }


def _reads_no_role(rule: dict, role_files: list[str] | None) -> bool:
    managers = set(rule.get("matchManagers", []))
    if managers and managers <= NON_ROLE_MANAGERS.keys():
        return True
    file_names = rule.get("matchFileNames")
    if not file_names:
        return False
    if role_files is None:
        role_files = _role_relative_files()
    return not any(_in_file_scope(rel, file_names) for rel in role_files)


def per_package_manual_rules(
    rules: list[dict], role_files: list[str] | None = None
) -> list[dict]:
    """The `manual` rules after the denylist rule that match by package name.

    A rule scoped to `NON_ROLE_MANAGERS` alone, or whose `matchFileNames` select no file in
    `role_files` (default: every file under `k8s/roles/`), is left out, because no role
    owns its pin.
    """
    manual_at, _ = find_rule(rules, MANUAL_GROUP_PREFIX)
    return [
        r
        for r in rules[manual_at + 1 :]
        if r.get("matchPackageNames")
        and "(manual" in str(r.get("groupName", ""))
        and not _reads_no_role(r, role_files)
    ]


def per_package_marker_problems(
    rules: list[dict],
    owners_by_package: dict[str, set[str]],
    denylist: set[str],
    role_files: list[str] | None = None,
) -> list[str]:
    """Every per-package manual rule whose marker disagrees with who owns its pin. Empty means none.

    Pure over the parsed rules and a `{package: owner roles}` map, so the red half of the pair
    below can hand it a denied owner without editing the tree.
    """
    problems: list[str] = []
    for rule in per_package_manual_rules(rules, role_files):
        group = str(rule["groupName"])
        owners: set[str] = set()
        for package in rule["matchPackageNames"]:
            owners |= owners_by_package.get(package, set())
        denied = sorted(owners & denylist)
        carries = DENYLIST_MARKER in group
        if not owners:
            problems.append(
                f"{group!r} matches {rule['matchPackageNames']} but no manager reads that "
                "pin from a role — an unmappable pin cannot be argued as eligible; find its "
                "reader or drop the rule"
            )
            continue
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
    deps = read_table(TABLE.read_text())
    return {
        package: pin_owner_roles(
            package,
            deps,
            datasources=set(rule.get("matchDatasources", [])) or None,
            file_names=rule.get("matchFileNames"),
        )
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


# --- the pin-owner census: the role whose file the extract reads a dep from owns it ---


def _dep(
    file: str, dep_name: str, datasource: str = "docker", package_name: str = "-"
) -> Row:
    return Row("regex", file, dep_name, datasource, "-", package_name)


def test_a_dep_is_owned_by_the_role_whose_file_holds_it() -> None:
    deps = [_dep("k8s/roles/traefik/defaults/main.yml", "vendor/plugin")]
    assert pin_owner_roles("vendor/plugin", deps) == {"traefik"}
    assert pin_owner_roles("vendor/other", deps) == set()


def test_a_package_name_maps_the_pin_like_a_dep_name() -> None:
    deps = [
        _dep(
            "k8s/roles/headlamp/defaults/main.yml",
            "vendor-plugin/flux",
            "github-releases",
            "vendor/plugins",
        )
    ]
    assert pin_owner_roles("vendor/plugins", deps) == {"headlamp"}
    assert pin_owner_roles("vendor-plugin/flux", deps) == {"headlamp"}


def test_a_dockerfile_pin_is_owned_by_the_role_that_builds_and_runs_it() -> None:
    """n8n builds the image it runs, so the Dockerfile pin is n8n's alone."""
    deps = [
        Row(
            "dockerfile",
            "k8s/roles/n8n/templates/Dockerfile.j2",
            "n8nio/n8n",
            "docker",
            "digest",
            "-",
        )
    ]
    assert pin_owner_roles("n8nio/n8n", deps) == {"n8n"}


def test_a_dep_outside_the_roles_has_no_owner() -> None:
    deps = [
        _dep(
            ".github/workflows/promote.yml",
            "sigstore/cosign",
            "github-release-attachments",
        )
    ]
    assert pin_owner_roles("sigstore/cosign", deps) == set()


def test_a_dep_in_another_datasource_does_not_own_the_pin() -> None:
    """karakeep's `_image:` names ghcr.io/astral-sh/uv; a github-releases rule for
    astral-sh/uv is initial-setup's alone."""
    deps = [
        _dep("k8s/roles/karakeep/defaults/main.yml", "astral-sh/uv"),
        _dep(
            "k8s/roles/initial-setup/tasks/host-basics.yml",
            "astral-sh/uv",
            "github-releases",
        ),
    ]
    assert pin_owner_roles("astral-sh/uv", deps) == {"karakeep", "initial-setup"}
    assert pin_owner_roles("astral-sh/uv", deps, datasources={"github-releases"}) == {
        "initial-setup"
    }


# --- the red-proof pair: the checker must accept an agreeing rule and reject a drifted one ---

_DENYLIST = {"authelia", "traefik"}


def _rule_set(group: str, package: str = "vendor/plugin") -> list[dict]:
    return [
        {"groupName": MANUAL_GROUP_PREFIX + ", so a person merges it)"},
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


def test_a_pin_no_role_owns_is_flagged() -> None:
    rules = _rule_set("plugin (manual — finish it)")
    problems = per_package_marker_problems(rules, {}, _DENYLIST)
    assert len(problems) == 1 and "no manager reads" in problems[0], problems


def test_a_pin_only_a_workflow_manager_reads_is_outside_the_census() -> None:
    rules = _rule_set("plugin (manual — finish it)")
    rules[1]["matchManagers"] = ["github-actions"]
    assert per_package_marker_problems(rules, {}, _DENYLIST) == []


def test_a_rule_that_also_names_a_role_manager_stays_in_the_census() -> None:
    rules = _rule_set("plugin (manual — finish it)")
    rules[1]["matchManagers"] = ["github-actions", "custom.regex"]
    problems = per_package_marker_problems(rules, {}, _DENYLIST)
    assert len(problems) == 1 and "no manager reads" in problems[0], problems


def test_no_role_file_is_read_by_a_non_role_manager() -> None:
    """The exemption holds only while these managers' default patterns select no role file."""
    for name, patterns in NON_ROLE_MANAGERS.items():
        regexes = [_file_pattern(p) for p in patterns]
        assert any(r.search(".github/workflows/promote.yml") for r in regexes), name
        read = [f for f in _role_relative_files() if any(r.search(f) for r in regexes)]
        assert not read, f"{name} reads role files {read}; their pins have an owner"


def test_the_census_leaves_out_the_cosign_workflow_rule() -> None:
    rules = _renovate()["packageRules"]
    cosign = [
        r for r in rules if str(r.get("groupName", "")).startswith("cosign (manual")
    ]
    assert len(cosign) == 1
    assert cosign[0] not in per_package_manual_rules(rules)


def test_a_promoted_owner_whose_rule_still_carries_the_marker_is_flagged() -> None:
    rules = _rule_set("plugin (manual — finish it; k8s_autodeploy: false)")
    problems = per_package_marker_problems(
        rules, {"vendor/plugin": {"jellyfin"}}, _DENYLIST
    )
    assert len(problems) == 1 and "drop the marker" in problems[0], problems


# --- a rule's matchFileNames narrows the files its pin is read from ---

_WG_EASY = "ghcr.io/wg-easy/wg-easy"
_WG_EASY_ROLE_FILES = ["k8s/roles/wg-easy/defaults/main.yml"]


def _wg_easy_problems(rule_extra: dict) -> list[str]:
    rules = _rule_set("pi image wg-easy (manual — finish it)", package=_WG_EASY)
    rules[1].update(rule_extra)
    owners = pin_owner_roles(
        _WG_EASY,
        [_dep(_WG_EASY_ROLE_FILES[0], _WG_EASY)],
        file_names=rules[1].get("matchFileNames"),
    )
    return per_package_marker_problems(
        rules, {_WG_EASY: owners}, {"wg-easy"}, role_files=_WG_EASY_ROLE_FILES
    )


def test_a_rule_scoped_to_a_file_outside_the_roles_tree_is_clean() -> None:
    scope = ["hosts/components/pi-compose/services/wg-easy/compose.yaml.j2"]
    assert _wg_easy_problems({"matchFileNames": scope}) == []


def test_the_same_rule_without_its_file_scope_is_flagged() -> None:
    problems = _wg_easy_problems({})
    assert len(problems) == 1 and "['wg-easy']" in problems[0], problems


@pytest.mark.parametrize(
    "entry",
    [
        "k8s/roles/{traefik,authelia}/defaults/main.yml",
        "!hosts/**",
        "/^k8s/roles/traefik/defaults/main\\.yml$/i",
    ],
)
def test_a_file_scope_entry_the_census_cannot_read_is_refused(entry: str) -> None:
    with pytest.raises(AssertionError):
        _in_file_scope("k8s/roles/traefik/defaults/main.yml", [entry])


def test_a_file_scope_keeps_only_the_roles_its_globs_select() -> None:
    deps = [
        _dep("k8s/roles/loki-homelab/defaults/main.yml", "grafana/alloy"),
        _dep("k8s/roles/alloy-edge/defaults/main.yml", "grafana/alloy"),
    ]
    assert pin_owner_roles("grafana/alloy", deps) == {"loki-homelab", "alloy-edge"}
    for scope in (
        ["k8s/roles/loki-homelab/**"],
        ["/^k8s/roles/loki-homelab/defaults/main\\.yml$/"],
    ):
        assert pin_owner_roles("grafana/alloy", deps, file_names=scope) == {
            "loki-homelab"
        }, scope
