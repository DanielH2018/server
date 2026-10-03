"""Censuses of the suite's own guards, one `_row_table.Census` row each.

These rows replaced `test_glob_census_non_vacuity.py` and
`test_role_tree_walks_use_role_dirs.py` (#3407). #3384 proposed retiring both once every census
they police is a row. Most of what they police is not a row and will not become one: the 22
`role_dirs` callers are mostly render-based guards, and most of the glob users in scope
glob a `tmp_path` fixture. So the checks stayed and only their files went.

Run: uv run pytest ansible/tests/repo/test_census_rows_suite.py
"""

import re

import pytest
from _row_table import (
    Census,
    Subject,
    check,
    lines_matching,
    proof_problems,
    text_lacks,
    tracked,
)

SELF = "ansible/tests/repo/test_census_rows_suite.py"

GLOB_CALL = re.compile(r"\.r?glob\(")

# A size floor, a named-member set, or a truthy assert on the census or something derived.
NON_VACUITY = re.compile(
    r">=\s*\d"
    r"|assert\s+len\([^)]*\)\s*(==|>|>=)\s*\d"
    r"|frozenset\("
    r"|KNOWN_[A-Z_]+|EXPECTED_[A-Z_]+"
    r"|assert\s+[a-zA-Z_][a-zA-Z0-9_.\[\]]*\s*(,|$)"
    r"|assert\s+not\s+"
)


def _glob_using_test_files() -> list[str]:
    """The scope measured clean when the rule shipped: repo/, scripts/tests/, scripts/*/tests/.

    A crude version over the other `ansible/tests/` directories flagged half their globbers,
    most of them `tmp_path` fixtures; telling those apart needs a person reading each file.
    """
    files = tracked(
        "ansible/tests/repo/test_*.py",
        "scripts/tests/test_*.py",
        "scripts/*/tests/test_*.py",
    )
    return [
        rel
        for rel in files
        if rel.count("/") <= 3 and GLOB_CALL.search(Subject(rel).text)
    ]


# `<anything naming a role tree>.iterdir()` on one line: the constants a guard imports and
# their local aliases, and the literal path written out or built a segment at a time. A tree
# bound on one line and walked on another is invisible to it; ROLE_DIRS_CALLERS covers that.
BARE_ROLE_WALK = re.compile(
    r"(?:K8S_ROLES|SETUP_ROLES|CONTAINER_ROLES|CONTAINERS_ROLES|DOCKER_ROLES"
    r'|roles/(?:k8s|setup|containers)"'
    r'|ROLES\s*/\s*"(?:k8s|setup|containers)"'
    r'|"roles"\s*/\s*"(?:k8s|setup|containers)")[^\n]*\.iterdir\(\)'
)

# Every module whose role census goes through `role_dirs`, pinned by name so a rewrite that
# drops the call fails even when it also drops the spelling BARE_ROLE_WALK reads.
ROLE_DIRS_CALLERS = frozenset(
    f"ansible/tests/{rel}"
    for rel in (
        "_k8s_render.py",
        "k8s/test_checksum_annotations_documented.py",
        "k8s/test_configmap_keys_not_absorbed.py",
        "k8s/test_manifest_roles_include_the_shared_render.py",
        "k8s/test_script_configmaps_apply_server_side.py",
        "longhorn/test_every_longhorn_pvc_has_a_tier.py",
        "deploy/test_containers_list_roles_exist.py",
        "deploy/test_cronjob_gate_decision.py",
        "deploy/test_cronjob_only_roles_include_the_gate.py",
        "deploy/test_inline_rollout_gates.py",
        "deploy/test_k8s_autodeploy_guard.py",
        "deploy/test_k8s_dry_run.py",
        "deploy/test_k8s_dry_run_host_writes.py",
        "deploy/test_manifests_prune.py",
        "deploy/test_renovate_automerge_follows_the_autodeploy_denylist.py",
        "deploy/test_restart_on_narrows_the_restart_signals.py",
        "deploy/test_setup_role_playbooks_agree.py",
        "repo/test_role_claude_md.py",
        "services/test_bridge_patch_boundary.py",
        "setup/test_has_flag_roles_have_both_directions.py",
        "setup/test_host_lib_sibling_copies.py",
        "setup/test_setup_cron_roles_have_a_contract.py",
    )
)


ROWS = (
    Census(
        name="glob-census-carries-a-non-vacuity-assertion",
        reason=(
            "A census that globs for its subject returns an empty set the moment the subject "
            "renames or moves, and `all(...)` over nothing passes (failure class 4, "
            "docs/failure-classes.md). Write it as a Census row, or add `assert len(found) >= N` "
            "or a named-member set. This cannot see whether a census covers the right SET."
        ),
        files=_glob_using_test_files,
        offence=lambda s: (
            []
            if NON_VACUITY.search(s.text)
            else ["globs with no non-vacuity assertion"]
        ),
        red=(
            Subject(
                "a.py",
                "found = sorted(ROLES.glob('*.py'))\nfor f in found:\n    check(f)\n",
            ),
        ),
        green=(
            Subject(
                "a.py", "found = sorted(ROLES.glob('*.py'))\nassert len(found) >= 5\n"
            ),
        ),
        min_matches=10,
        must_find=frozenset({"scripts/diagnostics/tests/test_probe_boundaries.py"}),
    ),
    Census(
        name="role-tree-walks-use-role-dirs",
        reason=(
            "A retired role's gitignored `__pycache__/` keeps `roles/<plane>/<role>/` on disk "
            "after the deployer removes its tracked files, so a bare `iterdir()` reads the shell "
            "as a role. That fails or miscensuses only in the long-lived primary checkout where "
            "the crons run the suite, never in CI (#2952, #2964). Use `_role_census.role_dirs`."
        ),
        files=lambda: tracked("ansible/tests/*.py"),
        offence=lines_matching(BARE_ROLE_WALK),
        red=(
            Subject("a.py", "for p in sorted(_K8S_ROLES.iterdir()):"),
            Subject("b.py", 'for p in (REPO / "ansible/roles/k8s").iterdir():'),
            Subject("c.py", 'for p in (ROLES / "setup").iterdir():'),
            Subject("d.py", 'for p in (REPO / "ansible" / "roles" / "k8s").iterdir():'),
            Subject("e.py", "for p in DOCKER_ROLES.iterdir():"),
            Subject("f.py", "for p in sorted(_SETUP_ROLES_DIR.iterdir()):"),
        ),
        green=(
            Subject("a.py", "for p in role_dirs(K8S_ROLES):"),
            Subject("b.py", "for p in (role / 'templates').iterdir():"),
        ),
        min_matches=100,
        must_find=ROLE_DIRS_CALLERS,
        allow={
            SELF: "the red fixtures above hold the offending spelling as text",
            "ansible/tests/deploy/_autodeploy.py": (
                "filters with is_leftover_dir inline, the predicate role_dirs uses"
            ),
            "ansible/tests/deploy/test_denylist_parsers_agree.py": (
                "mirrors the denylist filter's own skips inline, as its comment argues"
            ),
        },
    ),
    Census(
        name="role-dirs-callers-still-call-it",
        reason=(
            "role-tree-walks-use-role-dirs reads one line at a time, so a tree bound on one line "
            "and walked on another escapes it. These modules census roles and must keep calling "
            "`role_dirs`; one that genuinely stopped censusing roles leaves ROLE_DIRS_CALLERS."
        ),
        files=lambda: sorted(ROLE_DIRS_CALLERS),
        offence=text_lacks("role_dirs(", "calls role_dirs()"),
        red=(Subject("a.py", "for p in K8S.iterdir():\n"),),
        green=(Subject("a.py", "for p in role_dirs(K8S):\n"),),
        min_matches=len(ROLE_DIRS_CALLERS),
        must_find=ROLE_DIRS_CALLERS,
    ),
)

_IDS = [row.name for row in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_holds_on_the_tree(row: Census):
    problems = check(row)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_flags_its_red_subjects_and_passes_its_green_ones(row: Census):
    assert not proof_problems(row)
