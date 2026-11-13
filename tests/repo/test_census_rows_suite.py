"""Censuses of the suite's own guards, one `_row_table.Census` row each.

These rows replaced `test_glob_census_non_vacuity.py` and
`test_role_tree_walks_use_role_dirs.py` (#3407). #3384 proposed retiring both once every census
they police is a row. That precondition does not hold, and #3430 measured why it will not.
After #3430 folded the textual censuses it named, except three whose docstrings say why, the
non-vacuity row still finds 40 glob-using test files, and only two of them are row files. 23 of
the 40 mention a `tmp_path` fixture, which no `git ls-files` selector can stand in for. The 22 `role_dirs` callers are
mostly render-based guards, which are not textual censuses. So the checks stay as rows.

Run: uv run pytest tests/repo/test_census_rows_suite.py
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

SELF = "tests/repo/test_census_rows_suite.py"

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

    A crude version over the other `tests/` directories flagged half their globbers,
    most of them `tmp_path` fixtures; telling those apart needs a person reading each file.
    """
    files = tracked(
        "tests/repo/test_*.py",
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
# The last branch is a parameter or local named for the tree (`roles_root`, `k8s_roles_dir`)
# walked directly. A test that passes a `tmp_path` tree in still walks a real one in
# production, and such a walk escaped every other branch (#3769). It needs the `roles` plural,
# so a single role's `(role / "templates").iterdir()` stays clean.
BARE_ROLE_WALK = re.compile(
    r"(?:K8S_ROLES|SETUP_ROLES|CONTAINER_ROLES|CONTAINERS_ROLES|DOCKER_ROLES"
    r'|k8s/roles"'
    r'|ROLES\s*/\s*"(?:k8s|setup|containers)"'
    r'|"k8s"\s*/\s*"roles")[^\n]*\.iterdir\(\)'
    r"|\b\w*roles\w*\.iterdir\(\)"
)

# Every module whose role census goes through `role_dirs`, pinned by name so a rewrite that
# drops the call fails even when it also drops the spelling BARE_ROLE_WALK reads.
ROLE_DIRS_CALLERS = frozenset(
    f"tests/{rel}"
    for rel in (
        "_k8s_render.py",
        "k8s/test_built_images_name_the_content_tag.py",
        "k8s/test_checksum_annotations_documented.py",
        "k8s/test_configmap_keys_not_absorbed.py",
        "deploy/test_cronjob_only_roles_include_the_gate.py",
        "deploy/test_k8s_autodeploy_guard.py",
        "deploy/test_renovate_automerge_follows_the_autodeploy_denylist.py",
        "repo/test_role_claude_md.py",
        "services/test_bridge_patch_boundary.py",
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
        files=lambda: tracked("tests/*.py", "hosts/tests/*.py"),
        offence=lines_matching(BARE_ROLE_WALK),
        red=(
            Subject("a.py", "for p in sorted(_K8S_ROLES.iterdir()):"),
            Subject("b.py", 'for p in (REPO / "k8s/roles").iterdir():'),
            Subject("c.py", 'for p in (ROLES / "setup").iterdir():'),
            Subject("d.py", 'for p in (REPO / "k8s" / "roles").iterdir():'),
            Subject("e.py", "for p in DOCKER_ROLES.iterdir():"),
            Subject("f.py", "for p in sorted(_SETUP_ROLES_DIR.iterdir()):"),
            Subject("g.py", "for p in roles_root.iterdir():"),
            Subject("h.py", "for p in sorted(roles_dir.iterdir()):"),
            Subject("i.py", "for p in sorted(k8s_roles_dir.iterdir())"),
        ),
        green=(
            Subject("a.py", "for p in role_dirs(K8S_ROLES):"),
            Subject("b.py", "for p in (role / 'templates').iterdir():"),
        ),
        min_matches=100,
        must_find=ROLE_DIRS_CALLERS,
        allow={
            SELF: "the red fixtures above hold the offending spelling as text",
            "tests/deploy/_autodeploy.py": (
                "filters with is_leftover_dir inline, the predicate role_dirs uses"
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
