"""The `static-ratchet-tests` prek hook runs the static tests that turned PRs red, and CI skips it.

Twelve static test modules caused most of the PR reds that the author's own code did not
(#3605, #4008, #4904), and eleven more cheap ones failed 15 PR runs across 13 PRs between
2026-10-15 and 2026-10-17 (#5481). The hook runs them at commit time so the failure and its
repair arrive before CI. This guard holds three things a later edit could quietly lose: every module the
hook names still exists, its `files` gate matches the edits those tests react to, and CI's two
`prek run` steps skip it, because the `pytest` job runs the same modules.

Run: uv run pytest ansible/tests/repo/test_static_ratchet_hook.py
"""

import itertools
import re
import shlex
import tomllib

import pytest
import xdist_width
from _helpers import REPO

HOOK_ID = "static-ratchet-tests"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# The four modules #3605 measured, the four #4008 added and the four #4904 added. A module
# dropped from the entry fails here by name.
EXPECTED_MODULES = {
    "ansible/tests/repo/test_module_length_ratchet.py",
    "ansible/tests/repo/test_facts_lock_matches_tree.py",
    "ansible/tests/repo/test_pytest_shards_partition_the_suite.py",
    "scripts/docs/tests/test_gen_doc_fragments.py",
    "ansible/tests/repo/test_role_claude_md.py",
    "ansible/tests/repo/test_census_rows_python.py",
    "scripts/tests/test_census_rows_test_modules.py",
    "scripts/tests/test_census_rows_test_renders.py",
    "ansible/tests/repo/test_census_rows_suite.py",
    "ansible/tests/repo/test_census_rows_roles.py",
    "ansible/tests/repo/test_census_rows_text.py",
    "ansible/tests/repo/test_census_rows_layout.py",
}

# The cheap ratchet and census modules #5481 measured failing PR CI, each with a path whose edit
# reintroduced or can reintroduce its failure. The two Renovate-rule modules are the ones whose
# recorded weights (0.66s and 0.0s) the issue quotes. `test_gen_reference_scripts` (31.09s) and
# `test_guard_tests_read_renders_not_templates` (10.12s) stay out: the equality pin below fails
# on either one.
ADDED_BY_5481 = {
    "scripts/tests/test_scripts_import_direction.py": "scripts/lib/repo_paths.py",
    "scripts/lib/tests/test_exit_codes.py": "scripts/deploy.sh",
    "ansible/tests/setup/test_lander_deadlines_cover_the_soak.py": ".github/workflows/promote.yml",
    "scripts/tests/test_prek_files_cover_entry_imports.py": "prek.toml",
    "ansible/tests/repo/test_tracked_python_hazards.py": "scripts/dev/tests/test_findings.py",
    "ansible/tests/k8s/test_checksum_annotations_documented.py": "ansible/roles/k8s/monitor-bridge/CLAUDE.md",
    "ansible/tests/repo/test_shell_entry_points_answer_help.py": ".claude/hooks/run-hook.sh",
    "ansible/tests/deploy/test_renovate_per_package_manual_rules_carry_the_denylist_marker.py": "renovate.json",
    "ansible/tests/deploy/test_renovate_denylist_marker_reaches_the_pr_title.py": "renovate.json",
    "ansible/tests/repo/test_owner_entry_points_census.py": "docs/target-architecture/deploy/d2-first-leaf.md",
    "scripts/diagnostics/tests/test_probe_health_rolled.py": "scripts/diagnostics/probe.py",
}

NUMBER_WORDS = {
    word: n
    for n, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen eighteen nineteen twenty twenty-one twenty-two twenty-three "
        "twenty-four twenty-five twenty-six twenty-seven twenty-eight twenty-nine thirty".split()
    )
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


def _modules(hook: dict) -> set[str]:
    """The test files an entry names, with any `::node` suffix dropped."""
    args = (arg.split("::", 1)[0] for arg in shlex.split(hook["entry"]))
    return {arg for arg in args if arg.endswith(".py")}


def _ratchet_hooks() -> list[dict]:
    """`static-ratchet-tests` and any sibling split off it to carry its own `files` gate."""
    config = tomllib.loads((REPO / "prek.toml").read_text())
    return [
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id", "").startswith(HOOK_ID)
    ]


def test_the_hook_runs_every_expected_module_and_each_exists():
    named = set().union(*(_modules(hook) for hook in _ratchet_hooks()))
    assert named == EXPECTED_MODULES | set(ADDED_BY_5481)
    missing = sorted(m for m in named if not (REPO / m).is_file())
    assert not missing, f"{HOOK_ID} names modules that do not exist: {missing}"


@pytest.mark.parametrize(
    "path",
    [
        "scripts/dev/fact_status.py",
        "CLAUDE.md",
        "ansible/roles/k8s/traefik/CLAUDE.md",
        "docs/facts.lock/0123456789abcdef.json",
        "scripts/dev/pytest_shard_weights.json",
        "ansible/tests/repo/module_length_allowlist.txt",
        "ansible/roles/k8s/traefik/defaults/main.yml",
        "ansible/inventory/hosts.ini",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        ".github/workflows/image-smoke.yml",
        "ansible/roles/k8s/monitor-bridge/files/notify.sh",
        ".claude/agents/skeptic.md",
        ".claude/skills/homelab-review/SKILL.md",
        "evals/cases/review-standing.json",
        "scripts/lib/__pycache__/gh.cpython-314.pyc",
    ],
)
def test_the_gate_matches_an_edit_the_tests_react_to(path):
    assert re.search(_hook()["files"], path)


@pytest.mark.parametrize(("module", "trigger"), sorted(ADDED_BY_5481.items()))
def test_a_commit_reintroducing_a_listed_failure_runs_its_test(module, trigger):
    """The edit that turned a PR red now selects a ratchet hook whose entry runs the test."""
    running = [hook for hook in _ratchet_hooks() if module in _modules(hook)]
    assert running, f"no {HOOK_ID} hook runs {module}"
    assert any(re.search(hook["files"], trigger) for hook in running), (
        f"an edit to {trigger} skips every {HOOK_ID} hook that runs {module}"
    )


def test_claude_md_states_how_many_files_the_hook_runs():
    paragraph = (REPO / "CLAUDE.md").read_text().split("## Pre-commit Hooks", 1)[1]
    match = re.search(
        r"commit time it runs ([\w-]+) static", " ".join(paragraph.split())
    )
    assert match, (
        "CLAUDE.md's Pre-commit Hooks paragraph no longer states the hook's file count"
    )
    word = match.group(1).lower()
    stated = int(word) if word.isdigit() else NUMBER_WORDS.get(word)
    assert stated == len(EXPECTED_MODULES | set(ADDED_BY_5481))


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
    assert len(sweeps) == 1, f"expected the one full sweep, found {sweeps}"
    unskipped = _unskipped(sweeps)
    assert not unskipped, f"these CI prek runs would run {HOOK_ID} twice: {unskipped}"


def test_a_ci_prek_sweep_without_the_skip_is_flagged():
    workflow = "        run: prek run --all-files --skip ansible-lint --color always\n"
    assert _unskipped(_sweeps(workflow)) == [
        "run: prek run --all-files --skip ansible-lint --color always"
    ]


def _unskipped_ids(sweeps: list[str], ids: list[str]) -> list[tuple[str, str]]:
    """(sweep, id) for each ratchet hook id a sweep does not name as a `--skip` argument."""
    out = []
    for line in sweeps:
        tokens = shlex.split(line)
        skipped = {
            after for flag, after in itertools.pairwise(tokens) if flag == "--skip"
        }
        out += [(line, hook_id) for hook_id in ids if hook_id not in skipped]
    return out


def test_every_ci_prek_sweep_skips_every_sibling_by_its_exact_id():
    """prek's `--skip` takes exact ids, so `--skip static-ratchet-tests` leaves the siblings on."""
    ids = [hook["id"] for hook in _ratchet_hooks()]
    assert len(ids) > 1, ids
    assert _unskipped_ids(_sweeps(CI_WORKFLOW.read_text()), ids) == []


def test_a_sweep_skipping_only_the_main_hook_is_flagged():
    line = "run: prek run --all-files --skip static-ratchet-tests --color always"
    assert _unskipped_ids(
        [line], ["static-ratchet-tests", "static-ratchet-tests-python"]
    ) == [(line, "static-ratchet-tests-python")]


@pytest.mark.parametrize(
    "path",
    [
        "ansible/roles/k8s/bento-pdf/tasks/main.yml",
        "ansible/roles/k8s/bento-pdf/templates/deployment.yaml.j2",
        "ansible/roles/k8s/flux/defaults/main.yml",
        "ansible/roles/k8s/observability/files/rules/x.yml",
    ],
)
def test_the_lander_gate_matches_every_k8s_input_its_render_reads(path):
    """The soak fixture renders every flux-owned role, and the image check reads every role's tasks."""
    lander = next(h for h in _ratchet_hooks() if h["id"] == f"{HOOK_ID}-lander")
    assert re.search(lander["files"], path)


def test_the_lander_gate_skips_a_k8s_role_doc():
    lander = next(h for h in _ratchet_hooks() if h["id"] == f"{HOOK_ID}-lander")
    assert not re.search(lander["files"], "ansible/roles/k8s/bento-pdf/CLAUDE.md")


# The session ceiling `PYTEST_XDIST_AUTO_NUM_WORKERS` sets for every Claude session.
SESSION_CEILING = 4

# A multi-module pytest hook that still takes `-n auto`, with the PR that pins it. #5919 pins
# `static-ratchet-tests` at `-n 4`; delete its row once that lands.
UNPINNED = {"static-ratchet-tests": "#5919"}


def _pytest_hooks() -> list[dict]:
    """Every prek hook whose entry runs pytest."""
    config = tomllib.loads((REPO / "prek.toml").read_text())
    return [
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if "pytest" in shlex.split(hook.get("entry", ""))
    ]


def _pinned_width(entry: str) -> int | None:
    """The `-n` an entry passes itself, or None when it leaves the width to `-n auto`."""
    tokens = shlex.split(entry)
    pinned = [after for flag, after in itertools.pairwise(tokens) if flag == "-n"]
    pinned += [t[2:] for t in tokens if t.startswith("-n") and t[2:].isdigit()]
    if pinned and pinned[-1] != "auto":
        return int(pinned[-1])
    return None


def _left_to_auto(hooks: list[dict]) -> list[str]:
    """The ids of multi-module hooks whose width `-n auto` sizes at one worker."""
    return [
        h["id"]
        for h in hooks
        if len(_modules(h)) > 1
        and _pinned_width(h["entry"]) is None
        and xdist_width.workers_for(len(_modules(h)), SESSION_CEILING) <= 1
    ]


def test_no_multi_module_pytest_hook_leaves_its_width_to_auto():
    """xdist_width gives `-n auto` one worker per 15 modules, which serialised five (#5917).

    An explicit `-n`, `-n0` included, is a measured choice; the defect is the silent fallback.
    """
    hooks = _pytest_hooks()
    assert "static-ratchet-tests-python" in {h["id"] for h in hooks}
    serial = [i for i in _left_to_auto(hooks) if i not in UNPINNED]
    assert not serial, (
        f"`-n auto` runs these on one worker; pin an explicit `-n`: {serial}"
    )


def test_every_unpinned_exemption_still_applies():
    """Once the named PR pins its hook, this fails so the exemption row goes with it."""
    stale = sorted(set(UNPINNED) - set(_left_to_auto(_pytest_hooks())))
    assert not stale, f"these hooks are pinned now; delete their UNPINNED rows: {stale}"


@pytest.mark.parametrize(
    ("entry", "flagged"),
    [
        ("uv run pytest -q -p no:cacheprovider a.py b.py c.py", True),
        ("uv run pytest -q -n auto a.py b.py", True),
        ("uv run pytest -q -n 4 a.py b.py", False),
        ("uv run pytest -q -n0 a.py b.py", False),
        ("uv run pytest -q a.py", False),
    ],
)
def test_the_guard_flags_only_an_unpinned_multi_module_entry(entry, flagged):
    assert _left_to_auto([{"id": "h", "entry": entry}]) == (["h"] if flagged else [])
