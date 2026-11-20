"""Censuses of the suite's own guards, one `_row_table.Census` row each.

These rows replace `test_glob_census_non_vacuity.py` and
`test_role_tree_walks_use_role_dirs.py`. Not every census they police can be a row: the
non-vacuity row finds 40 glob-using test files, and only two of them are row files. 23 of
the 40 mention a `tmp_path` fixture, which no `git ls-files` selector can stand in for. The 22 `role_dirs` callers are
mostly render-based guards, which are not textual censuses. So the checks stay as rows.

Run: uv run pytest tests/repo/test_census_rows_suite.py
"""

import ast
import functools
import re

import pytest
from _row_table import (
    REPO,
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
# walked directly, even from a `tmp_path` test. It needs the `roles` plural, so a single role's
# `(role / "templates").iterdir()` stays clean.
BARE_ROLE_WALK = re.compile(
    r"(?:K8S_ROLES|SETUP_ROLES|CONTAINERS_ROLES|DOCKER_ROLES"
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


# A module that reads tracked content: a walk a Census selector stands in for, or one file
# read by its path, which a selector naming that file stands in for.
READS_THE_TREE = re.compile(
    r"\.r?glob\(|\bls-files\b|\btracked\(|\bos\.walk\(|\brole_dirs\("
    r"|\.read_text\(|\.read_bytes\(|\bopen\(|\bsafe_load\(|\bload_\w+\(|\b_load\("
    r"|^from test_\w+ import",
    re.MULTILINE,
)
# A module that runs code rather than reading text: a subprocess other than `git ls-files`,
# a render, a scratch tree or a patched callable. A row holds none of these.
RUNS_CODE = re.compile(
    r"\bsubprocess\b|\brun\(\[(?!\"git\", \"ls-files\")|\brender\w*\("
    r"|\btmp_path\b|\bmonkeypatch\b|\burlopen\("
)
# A helper function that renders. Its `subprocess` stays out of this: `git_out` shells out
# to `git ls-files`, which a row may do.
RENDERS = re.compile(r"\brender\w*\(")
TEST_FUNCTION = re.compile(r"^(?:async )?def test_", re.MULTILINE)
BUILDS_A_ROW = re.compile(r"\b(?:Census|RunbookRule|Property)\(")
# A module with this many test functions or fewer reads like a selector and a few
# predicates with their red proofs: the shape of one row or of a handful of rows.
ROW_SIZED = 8


@functools.cache
def _helper_function_renders(module: str, name: str) -> bool:
    """Whether tracked module `module` defines function `name`, and it renders.

    The function renders when its own source does, or when it calls a function of the same
    module that does, at any depth.
    """
    for rel in tracked(f"*/{module.rpartition('.')[2]}.py"):
        text = (REPO / rel).read_text(errors="replace")
        defs = {
            node.name: node
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.FunctionDef)
        }
        pending, seen = [name], set()
        while pending:
            current = pending.pop()
            if current in seen or current not in defs:
                continue
            seen.add(current)
            if RENDERS.search(ast.get_source_segment(text, defs[current]) or ""):
                return True
            pending += [
                call.func.id
                for call in ast.walk(defs[current])
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            ]
    return False


def _calls_a_rendering_helper(text: str) -> bool:
    """Whether the module calls a function it imports, and that function renders.

    A test that renders through `route_tree()` has no `render(` of its own to match.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    imported = {
        (alias.asname or alias.name): (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in imported
        and _helper_function_renders(*imported[node.func.id])
        for node in ast.walk(tree)
    )


def _standalone_check_offence(subject: Subject) -> list[str]:
    text = subject.text
    tests = len(TEST_FUNCTION.findall(text))
    if (
        tests > ROW_SIZED
        or BUILDS_A_ROW.search(text)
        or not READS_THE_TREE.search(text)
        or RUNS_CODE.search(text)
        or _calls_a_rendering_helper(text)
    ):
        return []
    return [
        f"a standalone check of {tests} test function(s) over tracked text; make it a row"
    ]


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
            "after checkout-sync's fast-forward removes its tracked files, so a bare `iterdir()` "
            "reads the shell as a role. That fails or miscensuses only in the long-lived primary checkout where "
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
    Census(
        name="a-single-predicate-check-is-a-row",
        reason=(
            "A one-selector, one-predicate check is a row, not a file "
            "(.claude/rules/python-layout.md). A row fails `subject gone: delete this row` "
            "once its selector matches nothing, so it retires with its subject; a file "
            "passes over an empty census until someone notices. Write the check as a "
            "`Census` row, a row of the workflow table, a rendered-property row or a "
            "`RunbookRule` row. A check that needs "
            "a second render context, a cross-document join or a long exemption list keeps "
            "its file and goes in `allow` with that reason."
        ),
        files=lambda: tracked("tests/repo/test_*.py", "tests/services/test_*.py"),
        offence=_standalone_check_offence,
        red=(
            Subject(
                "tests/repo/test_x.py",
                "def test_ci_pins_its_runner():\n"
                "    ci = safe_load((REPO / '.github/workflows/ci.yml').read_text())\n"
                "    assert ci['jobs']['pytest']['runs-on'] != 'ubuntu-latest'\n",
            ),
            Subject(
                "tests/repo/test_x.py",
                "def test_every_doc_is_short():\n"
                "    for p in DOCS.glob('*.md'):\n"
                "        assert len(p.read_text()) < 9000\n",
            ),
            Subject(
                "tests/repo/test_x.py",
                "from lib.git_testing import git_out\n"
                "def test_every_doc_is_short():\n"
                "    for p in git_out(REPO, 'ls-files', 'docs').splitlines():\n"
                "        assert len((REPO / p).read_text()) < 9000\n",
            ),
        ),
        green=(
            Subject(
                "tests/repo/test_x.py",
                "ROWS = (Census(files=lambda: tracked('docs/*.md')),)\n"
                "def test_row_holds(): ...\n",
            ),
            Subject(
                "tests/repo/test_x.py", "def test_parse(): assert parse('a') == 1\n"
            ),
            Subject(
                "tests/repo/test_x.py",
                "def test_the_render_names_a_port():\n"
                "    assert 'port' in render_role_template('a', 'b.j2')\n",
            ),
            Subject(
                "tests/repo/test_x.py",
                "from test_alertmanager_routing import route_tree\n"
                "def test_the_tree_names_a_receiver():\n"
                "    assert 'blackhole' in route_tree()['receiver']\n"
                "    assert (REPO / 'a.yml').read_text()\n",
            ),
        ),
        min_matches=100,
        must_find=frozenset({SELF, "tests/repo/test_runbook_rules.py"}),
        allow={
            "tests/repo/test_ci_lint_group.py": (
                "a join of pyproject.toml's lint group and default groups against every "
                "workflow that installs the project"
            ),
            "tests/repo/test_claude_md_paragraphs_have_one_home.py": (
                "a join of each long CLAUDE.md paragraph against every other operator doc"
            ),
            "tests/repo/test_doc_values_hook.py": (
                "a join of the prek hook's `files` pattern against the paths the doc_values "
                "rows name and the paths the docs-refresh cron stages"
            ),
            "tests/repo/test_failure_class_detectors.py": (
                "a join of docs/failure-classes.md against the detector tests it cites"
            ),
            "tests/repo/test_host_package_case_inputs_census.py": (
                "a join of the render step's CASE_INPUTS against the modules the cases "
                "script imports and the tracked tree"
            ),
            "tests/repo/test_kind_node_image_follows_k3s.py": (
                "a join of CI's kind node image against the k3s version in the "
                "system-upgrade role's defaults"
            ),
            "tests/repo/test_renovate_canary_is_scheduled_not_per_merge.py": (
                "a join of ci.yml and renovate-config-canary.yml, which run the one "
                "retrying validator script"
            ),
            "tests/repo/test_vale_matches_the_ci_pin.py": (
                "a join of the Vale version and sha256 CI installs against the "
                "homelab-tools package pin"
            ),
            "tests/services/test_ha_trusted_proxies_cover_cloudflare.py": (
                "a join of Home Assistant's trusted_proxies against the pod CIDR and the "
                "Cloudflare ranges in the group vars"
            ),
            "tests/services/test_scrape_target_flap_rule.py": (
                "a join of the flap rules against the promtool cases that exercise them"
            ),
            "tests/services/test_host_check_rule_literals.py": (
                "a join of the rule file against the inventory, which #9019 rewrites"
            ),
            "tests/services/test_claude_cgroup_consumers_agree_with_the_writer.py": (
                "a join of the writer script's metrics against the alert rules and the "
                "Grafana board that read them"
            ),
            "tests/services/test_pi_peer_backup_schedule_clears_the_longhorn_slots.py": (
                "a join of pi-peer-backup's schedule against two other roles' Longhorn "
                "slots and the group vars' time zone"
            ),
        },
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
