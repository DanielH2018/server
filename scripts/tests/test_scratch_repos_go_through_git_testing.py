"""A test builds its scratch git repository through `lib.git_testing`, never by hand.

`lib.git_testing` exists because twenty-five test modules each re-derived the same `GIT_*`
scrub and disagreed on what went in it. A module that forgets one line still passes on a
workstation and writes the primary repository under `prek`'s pytest hook, where `git commit`
exports `GIT_DIR` and `GIT_INDEX_FILE` and git resolves both before `cwd`.

This refuses the next one. It is the test-side counterpart of
the `git-and-gh-go-through-lib` row of `ansible/tests/repo/test_census_rows_python.py`, which holds production modules under
`scripts/deploy_tools` and `scripts/dev` to `lib.git`.

TWO RULES, each a clean/flagged pair below.

1. A test module does not run a git WRITE verb through `subprocess`. Read from the AST, so a
   docstring naming the old form is prose rather than a hit. A guard reading the real
   checkout with `git ls-files` or `git rev-parse HEAD` cannot damage anything, and
   twenty-two of them do exactly that; the damage comes from a verb that builds or edits a
   repository under a redirected `GIT_DIR`.
2. A test module does not re-derive the scrub — the `startswith("GIT_")` comprehension —
   for itself.

Run: uv run pytest scripts/tests/test_scratch_repos_go_through_git_testing.py
"""

import ast
import re
from pathlib import Path

from lib.repo_paths import ANSIBLE, REPO, SCRIPTS

# Every `testpaths` entry, so the rule covers the same files pytest collects. All of them can
# import `lib.git_testing`: `pyproject.toml` puts `scripts/` on `pythonpath` for the whole
# session, so depth does not matter.
_TEST_ROOTS = (
    SCRIPTS,
    ANSIBLE / "tests",
    ANSIBLE / "roles",
    REPO / ".claude" / "hooks",
    REPO / ".claude" / "tests",
)

# Modules whose SUBJECT is the environment handling itself, or a subject's own environment
# rather than a git call. Each keeps its raw form, and says why here.
EXEMPT = {
    # The module under test IS the scrub.
    "scripts/lib/tests/test_git.py",
    "scripts/lib/tests/test_git_testing.py",
    # Pins GIT_CONFIG_NOSYSTEM and GIT_TEST_ASSUME_DIFFERENT_OWNER and asserts why each is
    # there; routing it through the shared helper would test the helper instead.
    "ansible/tests/deploy/test_setup_drift_check.py",
    # Both run the production Bash functions under a deliberately minimal environment
    # (a pinned PATH and a redirected HOME). The environment is the subject's, not a git
    # runner's, and inheriting the real one would change what the script sees.
    "ansible/tests/setup/test_docs_refresh_failure_path.py",
    "ansible/tests/setup/test_eval_run_failure_path.py",
    # `git_free_env` builds the whole environment a `deploy.sh` child runs under. Its own
    # docstring says why it is the shared scrub WITHOUT the scratch commit identity.
    "scripts/deploy_tools/tests/_deploy_sh_fakes.py",
    # This module: both rules are held here as fixture text, which is what a red-proof pair is.
    "scripts/tests/test_scratch_repos_go_through_git_testing.py",
}

# Modules the census must reach, so an empty or partial scan means the walk stopped matching
# rather than that the tree is clean.
KNOWN_MEMBERS = frozenset(
    {
        "ansible/tests/_ci_scoping.py",
        "ansible/tests/longhorn/test_volume_snapshot_register.py",
        "ansible/tests/repo/test_facts_lock_matches_tree.py",
        "ansible/roles/setup/renovate_agent/tests/test_prepare_worktree.py",
        "scripts/deploy_tools/tests/_narrow_fixtures.py",
        "scripts/deploy_tools/tests/test_deploy_staleness.py",
        "scripts/deploy_tools/tests/test_land_tools.py",
        "scripts/dev/tests/test_prune_worktrees.py",
        "scripts/diagnostics/tests/_release_fixtures.py",
        "scripts/lib/tests/test_facts_lock.py",
        "scripts/lib/tests/test_render_guard.py",
    }
)

_SCRUB = re.compile(r'startswith\(\s*["\']GIT_["\']\s*\)')

# The verbs that build or edit a repository. A read verb against the real checkout -- the
# `git ls-files` twenty-two guards run -- is harmless however the environment is set.
WRITE_VERBS = frozenset(
    {
        "add",
        "am",
        "apply",
        "branch",
        "checkout",
        "cherry-pick",
        "clone",
        "commit",
        "config",
        "fetch",
        "gc",
        "init",
        "merge",
        "mv",
        "prune",
        "pull",
        "push",
        "rebase",
        "remote",
        "reset",
        "restore",
        "rm",
        "stash",
        "switch",
        "tag",
        "update-ref",
        "worktree",
    }
)


def _test_modules() -> list[Path]:
    """Every module pytest collects under a `tests/` directory, plus `ansible/tests/`."""
    found = []
    for root in _TEST_ROOTS:
        for path in root.rglob("*.py"):
            parts = path.relative_to(REPO).parts
            if "tests" in parts and "collections" not in parts:
                found.append(path)
    return sorted(set(found))


def raw_git_calls(source: str) -> list[str]:
    """The WRITE verbs `source` runs through `subprocess`, in either argv spelling.

    A list or a tuple opening with the literal `"git"`. Read from the AST, so a docstring
    naming the old form is prose rather than a hit.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"run", "Popen", "check_output", "check_call"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.args
        ):
            continue
        argv = node.args[0]
        if not (isinstance(argv, ast.List | ast.Tuple) and len(argv.elts) >= 2):
            continue
        head, verb = argv.elts[0], argv.elts[1]
        if not (isinstance(head, ast.Constant) and head.value == "git"):
            continue
        if not isinstance(verb, ast.Constant) or verb.value not in WRITE_VERBS:
            continue
        # `worktree list` is a read; `worktree add` and `worktree remove` are not.
        third = argv.elts[2] if len(argv.elts) > 2 else None
        if (
            verb.value == "worktree"
            and isinstance(third, ast.Constant)
            and third.value == "list"
        ):
            continue
        found.append(str(verb.value))
    return found


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def test_the_census_reaches_every_migrated_module():
    found = {_rel(p) for p in _test_modules()}
    missing = KNOWN_MEMBERS - found
    assert not missing, f"the scan no longer reaches: {sorted(missing)}"


def test_every_exemption_still_names_a_file_that_exists():
    """A stale exemption silently lets a whole module back out of the rule."""
    gone = sorted(name for name in EXEMPT if not (REPO / name).exists())
    assert gone == [], f"exempted modules that no longer exist: {gone}"


def test_no_test_module_runs_git_through_subprocess():
    offenders = {
        _rel(p): calls
        for p in _test_modules()
        if _rel(p) not in EXEMPT and (calls := raw_git_calls(p.read_text()))
    }
    assert offenders == {}, (
        "these tests drive git themselves instead of through `lib.git_testing`, which is "
        f"how a scratch commit reaches the primary repository under a prek hook: {offenders}"
    )


def test_no_test_module_re_derives_the_git_scrub():
    offenders = sorted(
        _rel(p)
        for p in _test_modules()
        if _rel(p) not in EXEMPT and _SCRUB.search(p.read_text())
    )
    assert offenders == [], (
        "these tests re-derive the GIT_* scrub; import `scrubbed_env` from "
        f"`lib.git_testing` instead: {offenders}"
    )


def test_a_module_using_the_helper_is_clean():
    assert raw_git_calls('from lib.git_testing import git\ngit(repo, "status")\n') == []
    assert _SCRUB.search("from lib.git_testing import scrubbed_env\n") is None


def test_a_raw_git_write_is_flagged_in_each_argv_spelling():
    assert raw_git_calls('subprocess.run(["git", "init"], cwd=p)') == ["init"]
    assert raw_git_calls('subprocess.run(("git", "commit"), cwd=p)') == ["commit"]
    assert raw_git_calls('subprocess.check_output(["git", "worktree", "add", p])') == [
        "worktree"
    ]


def test_a_read_only_git_call_against_the_real_tree_is_not_flagged():
    """Twenty-two guards read the checkout this way, and none of them can damage it."""
    assert raw_git_calls('subprocess.run(["git", "ls-files", "-z"], cwd=REPO)') == []
    assert raw_git_calls('subprocess.run(["git", "rev-parse", "HEAD"])') == []
    assert raw_git_calls('subprocess.run(("git", "worktree", "list"))') == []


def test_a_hand_rolled_scrub_is_flagged():
    assert _SCRUB.search(
        'env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}'
    )
    assert _SCRUB.search("for v in [n for n in os.environ if n.startswith('GIT_')]:")


def test_a_non_git_subprocess_call_is_not_flagged():
    """The rule is about git, not about running a subprocess."""
    assert raw_git_calls('subprocess.run(["bash", str(script)], cwd=p)') == []
    assert raw_git_calls("subprocess.run([sys.executable, str(tool)])") == []
    assert (
        raw_git_calls('subprocess.CompletedProcess(args=["git", "x"], returncode=0)')
        == []
    )
    assert (
        raw_git_calls(
            '"""A raw subprocess.run(["git", ...]) here wrote the wrong repo."""'
        )
        == []
    )
