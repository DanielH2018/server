"""The `static-ratchet-tests` prek hook runs the eight tests that turned PRs red, and CI skips it.

Eight static test modules caused most of the PR reds that the author's own code did not (#3605,
#4008). The hook runs them at commit time so the failure and its repair arrive before CI. This
guard holds three things a later edit could quietly lose: every module the hook names still
exists, its `files` gate matches the edits those tests react to, and CI's two `prek run` steps
skip it, because the `pytest` job runs the same modules.

Run: uv run pytest ansible/tests/repo/test_static_ratchet_hook.py
"""

import re
import shlex
import tomllib

import pytest
from _helpers import REPO

HOOK_ID = "static-ratchet-tests"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# The four modules #3605 measured and the four #4008 added. A module dropped from the entry
# fails here by name.
EXPECTED_MODULES = {
    "ansible/tests/repo/test_module_length_ratchet.py",
    "ansible/tests/repo/test_facts_lock_matches_tree.py",
    "ansible/tests/repo/test_pytest_shards_partition_the_suite.py",
    "scripts/docs/tests/test_gen_doc_fragments.py",
    "ansible/tests/repo/test_role_claude_md.py",
    "ansible/tests/repo/test_census_rows_python.py",
    "scripts/tests/test_census_rows_test_modules.py",
    "scripts/tests/test_census_rows_test_renders.py",
}


def _hook() -> dict:
    config = tomllib.loads((REPO / "prek.toml").read_text())
    hooks = [
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == HOOK_ID
    ]
    assert len(hooks) == 1, f"expected exactly one {HOOK_ID} hook, found {len(hooks)}"
    return hooks[0]


def test_the_hook_runs_every_expected_module_and_each_exists():
    named = {arg for arg in shlex.split(_hook()["entry"]) if arg.endswith(".py")}
    assert named == EXPECTED_MODULES
    missing = sorted(m for m in named if not (REPO / m).is_file())
    assert not missing, f"{HOOK_ID} names modules that do not exist: {missing}"


@pytest.mark.parametrize(
    "path",
    [
        "scripts/dev/fact_status.py",
        "CLAUDE.md",
        "ansible/roles/k8s/traefik/CLAUDE.md",
        "docs/facts.lock",
        "scripts/dev/pytest_shard_weights.json",
        "ansible/tests/repo/module_length_allowlist.txt",
        "ansible/roles/k8s/traefik/defaults/main.yml",
        "ansible/inventory/hosts.ini",
        "pyproject.toml",
        ".github/workflows/ci.yml",
    ],
)
def test_the_gate_matches_an_edit_the_tests_react_to(path):
    assert re.search(_hook()["files"], path)


@pytest.mark.parametrize(
    "path",
    [
        "docs/reference/services.md",
        "docs/assets/generated/fragments/x.md",
        "evals/history.json",
    ],
)
def test_the_gate_skips_a_path_no_test_reads(path):
    """The docs-refresh and eval-run crons commit these, and a docs-only commit pays nothing."""
    assert not re.search(_hook()["files"], path)


def _sweeps(workflow: str) -> list[str]:
    """Every `prek run` line in a workflow except the ansible-lint job's own runs."""
    return [
        line.strip()
        for line in workflow.splitlines()
        if "prek run" in line
        and not line.lstrip().startswith("#")
        and "prek run ansible-lint" not in line
    ]


def _unskipped(sweeps: list[str]) -> list[str]:
    return [line for line in sweeps if f"--skip {HOOK_ID}" not in line]


def test_every_ci_prek_sweep_skipping_the_hook_is_clean():
    sweeps = _sweeps(CI_WORKFLOW.read_text())
    assert len(sweeps) == 2, f"expected the scoped and the full sweep, found {sweeps}"
    unskipped = _unskipped(sweeps)
    assert not unskipped, f"these CI prek runs would run {HOOK_ID} twice: {unskipped}"


def test_a_ci_prek_sweep_without_the_skip_is_flagged():
    workflow = "        run: prek run --all-files --skip ansible-lint --color always\n"
    assert _unskipped(_sweeps(workflow)) == [
        "run: prek run --all-files --skip ansible-lint --color always"
    ]
