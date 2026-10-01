#!/usr/bin/env python3
"""A deploy-run-only change to `manifests` deploys ONE caller, not 57 (#3124).

WHAT WENT WRONG. PR #3117 changed `roles/k8s/manifests/tasks/` alone -- the condition the
config rollout-restart reads. `land_tags.shared_caller_tags` fanned that out to 57 tags and
about 20 minutes, ended `DEPLOY-VERDICT: deployed` then `VERDICT: settled`, and 0 of 60 release
records showed a restart. The operator ruled on 2026-10-01 that such a landing deploys one
representative caller as a smoke test.

WHY THE PICK IS NOT PINNED BY NAME. `smoke_caller` chooses the cheapest render target from the
tree, so adding one template to today's winner moves the answer. The live-tree tests here
assert the SHAPE -- one tag, a real caller, the same tag twice -- and the cost rule itself is
asserted against a synthetic tree, where the costs are the fixture.

Run: uv run pytest scripts/deploy_tools/tests/test_shared_role_smoke_caller.py
"""

import land_shared
import land_tags
import shared_role_callers
from shared_role_callers import SMOKE_TESTABLE_SHARED_ROLES, smoke_caller

# PR #3117's real five-path file list, read from `gh pr view 3117 --json files` on 2026-10-01.
# The CLAUDE.md and the two `ansible/tests/` paths decide nothing: `role_for` drops a `.md` and
# neither test path sits under the role, so `tasks/` is the whole of what this PR changed there.
_PR_3117_FILES = [
    "ansible/roles/k8s/manifests/CLAUDE.md",
    "ansible/roles/k8s/manifests/tasks/main.yml",
    "ansible/roles/k8s/manifests/tasks/release_stamp.yml",
    "ansible/tests/_release_expectation.py",
    "ansible/tests/k8s/test_release_stamp_rollout_expectation.py",
]

_TEMPLATE = "ansible/roles/k8s/manifests/templates/ingressroute.yml.j2"


def _every_caller(role: str) -> set[str]:
    """The unnarrowed caller walk, which the narrowing must stay a subset of."""
    return land_shared.caller_tags(
        role, land_shared.declared_tags(), land_shared.role_callers()
    )


def test_pr_3117_deploys_one_caller_rather_than_the_fleet():
    """The issue's Verify-by, on the measured file list."""
    every = _every_caller("manifests")
    assert len(every) > 40, "the caller walk itself still reaches the fleet"
    reached = land_tags.shared_caller_tags(_PR_3117_FILES)["manifests"]
    assert len(reached) == 1 and reached <= every
    # Still nothing owed to a hand: a non-empty caller set is what keeps the role out of the
    # note, and a narrowing to the empty set would have put it back in.
    assert land_tags.plane_note(_PR_3117_FILES) == ""


def test_the_operator_line_knows_which_role_was_narrowed():
    """`classify` reads `smoke_narrowed_roles` so its line cannot claim the fleet.

    Not derivable from the tag count: `arr-notification` has two callers and is not narrowed,
    while a role with one caller would read as narrowed on a count alone.
    """
    assert land_shared.narrowed_roles(_PR_3117_FILES) == frozenset({"manifests"})
    assert land_shared.narrowed_roles([*_PR_3117_FILES, _TEMPLATE]) == frozenset()
    assert (
        land_shared.narrowed_roles(
            ["ansible/roles/k8s/arr-notification/tasks/main.yml"]
        )
        == frozenset()
    )


def test_the_same_pick_twice_over_one_tree():
    """A landing that re-derives its tags must not deploy a second service."""
    first = land_tags.shared_caller_tags(_PR_3117_FILES)["manifests"]
    assert first == land_tags.shared_caller_tags(_PR_3117_FILES)["manifests"]


def test_a_template_beside_the_tasks_keeps_every_caller():
    """The reject half by path: `templates/` is bytes a deploy applies to each caller."""
    files = [*_PR_3117_FILES, _TEMPLATE]
    assert land_shared.deploy_run_only("manifests", files) is False
    assert land_tags.shared_caller_tags(files)["manifests"] == _every_caller(
        "manifests"
    )


def test_a_per_caller_helper_keeps_every_caller():
    """The reject half by role: `arr-notification` acts on each caller separately (#1397).

    `tasks/`-only and a shared role, so only `SMOKE_TESTABLE_SHARED_ROLES` separates it from
    the accepted case above.
    """
    files = ["ansible/roles/k8s/arr-notification/tasks/main.yml"]
    assert land_shared.deploy_run_only("arr-notification", files) is True
    assert "arr-notification" not in SMOKE_TESTABLE_SHARED_ROLES
    reached = land_tags.shared_caller_tags(files)["arr-notification"]
    assert reached == _every_caller("arr-notification")
    assert len(reached) > 1


def test_the_smoke_set_cannot_widen_past_the_digest_provable_roles():
    """Both sets mean "this role acts only through the bytes it renders", so pin them.

    `deploy_defer.DIGEST_PROVABLE_ROLES` carries the argument per role. A role added here but
    not there would be smoke-tested on one caller while the deployer still demands a record
    from all of them.
    """
    from deploy_defer import DIGEST_PROVABLE_ROLES

    assert SMOKE_TESTABLE_SHARED_ROLES <= DIGEST_PROVABLE_ROLES


def test_a_hand_typed_tags_run_still_expands_to_every_caller():
    """The asymmetry #2717 decided: `deploy.sh --tags manifests` is the operator asking."""
    declared = land_shared.declared_tags()
    tags, replaced = shared_role_callers.expand_shared_tags(
        ["manifests"], declared, land_shared.role_callers()
    )
    assert set(tags) == _every_caller("manifests")
    assert len(replaced["manifests"]) > 1


def _tree(tmp_path, entries, templates):
    """A host_vars + roles pair `smoke_caller` can read.

    Args:
        tmp_path: the directory to build both trees under.
        entries: host name to the service names its `containers_list` declares.
        templates: service name to how many `templates/*.j2` its role renders.

    Returns:
        The `(host_vars, roles)` pair, in `smoke_caller`'s argument order.
    """
    host_vars, roles = tmp_path / "host_vars", tmp_path / "roles"
    host_vars.mkdir()
    for host, names in entries.items():
        lines = ["containers_list:"]
        for name in names:
            lines.append(f"  - name: {name}")
            lines.append("    platform: k8s")
        (host_vars / f"{host}.yml").write_text("\n".join(lines) + "\n")
    for name, count in templates.items():
        (roles / name / "tasks").mkdir(parents=True)
        (roles / name / "tasks" / "main.yml").write_text(
            "- name: Render\n  include_role:\n    name: k8s/manifests\n"
        )
        (roles / name / "templates").mkdir()
        for i in range(count):
            (roles / name / "templates" / f"{i}.yaml.j2").write_text("{}\n")
    return host_vars, roles


def test_the_cheapest_candidate_wins_on_hosts_then_templates_then_name(tmp_path):
    """One host beats two, then fewer manifests, then the name as a stable tie-break."""
    host_vars, roles = _tree(
        tmp_path,
        {"box": ["wide", "cheap", "aardvark", "costly"], "server": ["wide"]},
        {"wide": 1, "cheap": 2, "aardvark": 2, "costly": 9},
    )
    tags = {"wide", "cheap", "aardvark", "costly"}
    assert smoke_caller(tags, host_vars, roles) == "aardvark"
    assert smoke_caller(tags - {"aardvark"}, host_vars, roles) == "cheap"
    assert smoke_caller({"wide", "costly"}, host_vars, roles) == "costly"


def test_a_caller_that_renders_nothing_is_no_candidate(tmp_path):
    """A role that applies its objects some other way proves nothing about the render path."""
    host_vars, roles = _tree(tmp_path, {"box": ["plain"]}, {})
    (roles / "plain" / "tasks").mkdir(parents=True)
    (roles / "plain" / "tasks" / "main.yml").write_text(
        "- name: Call an API\n  debug:\n"
    )
    assert smoke_caller({"plain"}, host_vars, roles) is None


def test_no_candidate_keeps_every_caller(tmp_path):
    """The RED proof for the empty answer.

    `plane_note` reads an empty caller set as a role nothing deploys and asks a hand for a full
    `ansible/deploy.yml`, so `None` must widen back to the whole set rather than narrow to it.
    """
    host_vars, roles = _tree(tmp_path, {"box": []}, {})
    assert smoke_caller({"sonarr", "radarr"}, host_vars, roles) is None
    assert land_shared.narrowed("manifests", {"sonarr"}, _PR_3117_FILES) == {
        "sonarr"
    }, "a single caller is already as narrow as it goes"
