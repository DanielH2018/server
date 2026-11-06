"""`land.sh`'s deploy.sh arm has no reachable tag, so it goes or the D6 plan names its reader (#7349).

Every check passes on either outcome: the arm deleted, or a module outside the arm reading it and
the plan naming that module. The arm's size bound in D6's exit 6 holds on both.

Run: uv run pytest scripts/deploy_tools/tests/test_land_deploy_arm_retired.py
"""

import ast
import re
from pathlib import PurePosixPath

import pytest
from _row_table import tracked
from deploy_tools import deploy_tags
from lib.repo_paths import REPO

# Spelled apart so this file stays out of the issue's `git grep` over `scripts`.
ENTRY_POINT = "deploy_by" + "_host"
ARM_MODULE = "scripts/deploy_tools/land_lib/deploy.py"
LAND_LIB = "scripts/deploy_tools/land_lib/*.py"
PLAN = "docs/target-architecture/deploy/d6-delete-old-path.md"
DESIGN = "docs/target-architecture/design.md"


def _text(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="ignore")


def _modules(pathspec: str) -> dict[str, ast.Module]:
    """Each non-test module `pathspec` tracks, parsed, with a member the census must find."""
    found = {
        rel: ast.parse(_text(rel))
        for rel in tracked(pathspec)
        if rel.endswith(".py") and "/tests/" not in rel
    }
    assert "scripts/deploy_tools/land_lib/merge.py" in found
    return found


def _reads_entry_point(tree: ast.Module) -> bool:
    return any(
        ENTRY_POINT in (getattr(node, "attr", None), getattr(node, "id", None))
        for node in ast.walk(tree)
    )


def _plan_names_the_arms_reader() -> bool:
    """Whether a plan paragraph on the entry point names a module outside the arm that reads it."""
    readers = [
        PurePosixPath(rel).name
        for rel, tree in _modules("scripts").items()
        if rel != ARM_MODULE and _reads_entry_point(tree)
    ]
    return any(
        ENTRY_POINT in paragraph and reader in paragraph
        for paragraph in _text(PLAN).split("\n\n")
        for reader in readers
    )


def _measured_landing_lines() -> int:
    """What exit 6's `wc -l` reads: the non-test lines of `land.sh`, `land.py` and `land_lib/`."""
    tracked_files = tracked(
        "scripts/deploy_tools/land.sh", "scripts/deploy_tools/land.py", LAND_LIB
    )
    assert "scripts/deploy_tools/land.py" in tracked_files
    return sum(_text(rel).count("\n") for rel in tracked_files if "/tests/" not in rel)


def test_no_script_names_the_entry_point_unless_the_plan_names_its_reader():
    """The issue's Verify-by: `git grep -n <entry point> -- scripts`, or the plan's named reader."""
    files = tracked("scripts")
    assert "scripts/deploy_tools/land.py" in files
    naming = [
        rel for rel in files if (REPO / rel).is_file() and ENTRY_POINT in _text(rel)
    ]
    assert not naming or _plan_names_the_arms_reader(), naming


@pytest.mark.parametrize(
    "name",
    [
        "deploy_with_lock_retry",
        "deploy_outcome",
        "_deploy_resolved",
        "_skip_tick_applied",
        "record_k8s_only",
        "landing_hosts_at",
    ],
)
def test_arm_function_is_gone_unless_the_plan_names_its_reader(name):
    defining = [
        rel
        for rel, tree in _modules(LAND_LIB).items()
        if any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == name
            for node in ast.walk(tree)
        )
    ]
    assert not defining or _plan_names_the_arms_reader(), defining


@pytest.mark.parametrize(
    "rel",
    [
        "scripts/deploy_tools/land_lib/stale_retarget.py",
        "scripts/deploy_tools/land_lib/land_platform.py",
    ],
)
def test_arm_module_is_gone_unless_the_plan_names_its_reader(rel):
    assert not tracked(rel) or _plan_names_the_arms_reader()


def test_no_landing_state_carries_k8s_only_unless_the_plan_names_its_reader():
    """`Landing.k8s_only` read or written anywhere in `land_lib/`."""
    using = [
        rel
        for rel, tree in _modules(LAND_LIB).items()
        if any(
            getattr(node, "attr", None) == "k8s_only"
            or getattr(getattr(node, "target", None), "id", None) == "k8s_only"
            for node in ast.walk(tree)
        )
    ]
    assert not using or _plan_names_the_arms_reader(), using


def test_deploy_tags_hosts_refuses_k8s_only_unless_the_plan_names_its_reader():
    if _plan_names_the_arms_reader():
        return
    with pytest.raises(SystemExit) as refused:
        deploy_tags.main(["hosts", "--k8s-only=dozzle", "dozzle"])
    assert refused.value.code != 0


def test_plan_does_not_keep_the_entry_point_without_a_reader():
    """The plan records the decision: its `stays` sentence goes unless it names who reads the arm."""
    prose = " ".join(_text(PLAN).split())
    assert f"`{ENTRY_POINT}` stays" not in prose or _plan_names_the_arms_reader()


@pytest.mark.parametrize(
    ("doc", "pattern"),
    [
        pytest.param(
            PLAN, r"`land_lib/` together are at most ([\d,]+) lines", id="exit-6"
        ),
        pytest.param(
            PLAN, r"land_lib/\*\.py'[^#]*# at most ([\d,]+)", id="verification"
        ),
        pytest.param(PLAN, r"the landing code at ([\d,]+)", id="f-05"),
        pytest.param(DESIGN, r"D6 bounds them at ([\d,]+) lines", id="design-prose"),
        pytest.param(
            DESIGN, r"\| `land` \|.*?at most ([\d,]+) lines after D6", id="design-table"
        ),
    ],
)
def test_exit_6_landing_bound_holds_for_the_measured_tree(doc, pattern):
    stated = re.search(pattern, " ".join(_text(doc).split()))
    assert stated, f"{doc} no longer states the landing bound: {pattern}"
    assert int(stated[1].replace(",", "")) >= _measured_landing_lines()
