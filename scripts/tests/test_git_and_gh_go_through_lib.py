"""Every git or gh call under `scripts/deploy_tools` and `scripts/dev` goes through `scripts/lib`.

`lib.git.git` strips every `GIT_*` variable so `cwd` alone decides which repository a call
reads; `lib.gh.gh` disables the prompt and the update notifier so a cron cannot hang on
either. A raw `subprocess.run(["git", ...])` beside them has neither property, and the
repo re-grew fourteen of them after the helpers existed (issue #2136). This refuses the
next one.

Scope is the two directories the issue migrated. `ansible/roles/*/files/*.py` stays raw on
purpose: those deploy to hosts without `scripts/lib`.

Run: uv run pytest scripts/tests/test_git_and_gh_go_through_lib.py
"""

import ast
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
_DIRS = (_SCRIPTS / "deploy_tools", _SCRIPTS / "dev")
_ROUTED = frozenset({"git", "gh", "kubectl"})
# Modules the census must contain: each carried a raw call before the migration, so an
# empty or partial scan means the walk stopped matching, not that the tree is clean.
KNOWN_MEMBERS = frozenset(
    {
        "dev/prune_worktrees.py",
        "dev/fanout_place.py",
        "dev/pytest_shard.py",
        "dev/fanout_lib/clean.py",
        "dev/fanout_lib/transport.py",
        "dev/fanout_lib/signing.py",
    }
)


def _production_modules() -> list[Path]:
    return [
        p
        for d in _DIRS
        for p in d.rglob("*.py")
        if "tests" not in p.relative_to(_SCRIPTS).parts
    ]


def raw_calls(source: str) -> list[str]:
    """The binaries `source` runs through a `subprocess.run([...])` whose argv opens with one.

    Read from the AST rather than the text, so a docstring that names the old form is prose,
    not a hit.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.args
            and isinstance(node.args[0], ast.List)
            and node.args[0].elts
        ):
            continue
        first = node.args[0].elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value in _ROUTED:
                found.append(first.value)
    return found


def test_the_census_reaches_every_migrated_module():
    found = {str(p.relative_to(_SCRIPTS)) for p in _production_modules()}
    missing = KNOWN_MEMBERS - found
    assert not missing, f"the scan no longer reaches: {sorted(missing)}"


def test_no_production_module_runs_git_or_gh_raw():
    offenders = {
        str(p.relative_to(_SCRIPTS)): calls
        for p in _production_modules()
        if (calls := raw_calls(p.read_text()))
    }
    assert offenders == {}, (
        f"raw subprocess.run of git/gh/kubectl; use lib.git / lib.gh / lib.kubectl: {offenders}"
    )


def test_the_pattern_flags_a_raw_call_and_passes_a_routed_one():
    assert raw_calls('subprocess.run(\n    ["git", "status"],\n)') == ["git"]
    assert raw_calls('subprocess.run(["gh", *args], check=True)') == ["gh"]
    assert raw_calls('git("status", cwd=repo)') == []
    assert raw_calls('subprocess.run(["ssh", host, "git rev-parse HEAD"])') == []
    # The argv of a CompletedProcess built by hand is not a call, and neither is prose.
    assert (
        raw_calls('subprocess.CompletedProcess(args=["gh", *args], returncode=1)') == []
    )
    assert (
        raw_calls(
            '"""A raw `subprocess.run(["git", ...])` here read the wrong repo."""'
        )
        == []
    )
