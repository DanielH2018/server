"""Each CI pytest leg restores the assertion rewrites an earlier run of its shard wrote (#8693).

Every xdist worker collects its whole shard, and on a cold checkout each one rewrites every
test module's assertions itself. pytest validates a rewritten `.pyc` by the source's hash, so a
cached one stays valid across checkouts. The cache helps only while its glob matches the name
pytest writes, a name that carries pytest's version, so the glob is checked against
`_pytest.assertion.rewrite.PYC_TAIL` rather than against a literal. Only master saves, once per
shard per day, because a per-run entry would evict the pinned-tool caches the workflow reads.

Run: uv run pytest tests/repo/test_ci_pytest_rewrite_cache.py
"""

from pathlib import PurePosixPath

from _helpers import REPO
from _pytest.assertion.rewrite import PYC_TAIL
from lib import yaml_fast

CI = REPO / ".github" / "workflows" / "ci.yml"
TEST_STEP = "Run the test suite"
RESTORE = "actions/cache/restore"
SAVE = "actions/cache/save"
DAY = "${{ steps.rewrite-day.outputs.day }}"


def _steps() -> list[dict]:
    return yaml_fast.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["pytest"][
        "steps"
    ]


def _cache_step(steps: list[dict], action: str) -> dict | None:
    """The first `action` step whose path glob matches a rewritten module, if one does."""
    rewritten = PurePosixPath("tests/repo/__pycache__/test_x" + PYC_TAIL)
    for step in steps:
        if str(step.get("uses", "")).startswith(action + "@"):
            path = str((step.get("with") or {}).get("path", ""))
            if any(rewritten.full_match(glob) for glob in path.split()):
                return step
    return None


def test_the_pytest_job_restores_its_rewritten_modules_before_the_tests_run():
    steps = _steps()
    step = _cache_step(steps, RESTORE)
    assert step is not None, (
        f"no {RESTORE} step in ci.yml's pytest job matches the `*{PYC_TAIL}` files "
        "pytest writes, so every worker rewrites every module's assertions from cold"
    )
    names = [s.get("name") for s in steps]
    assert names.index(step.get("name")) < names.index(TEST_STEP)


def test_each_shard_restores_its_newest_entry_and_names_one_per_day():
    step = _cache_step(_steps(), RESTORE)
    assert step is not None
    key, restore = step["with"]["key"], step["with"]["restore-keys"]
    assert "${{ matrix.shard }}" in key, key
    assert key.endswith(f"-{DAY}"), key
    assert restore == key.removesuffix(DAY), (key, restore)


def test_only_master_saves_and_only_a_key_it_did_not_restore():
    """A per-run or per-branch save would churn a cache budget already over 10 GB."""
    steps = _steps()
    restore, save = _cache_step(steps, RESTORE), _cache_step(steps, SAVE)
    assert restore is not None and save is not None
    assert save["with"]["path"] == restore["with"]["path"]
    assert save["with"]["key"] == (
        f"${{{{ steps.{restore['id']}.outputs.cache-primary-key }}}}"
    )
    gate = save.get("if", "")
    assert "github.ref == 'refs/heads/master'" in gate, gate
    assert f"steps.{restore['id']}.outputs.cache-hit != 'true'" in gate, gate
    names = [s.get("name") for s in steps]
    assert names.index(save.get("name")) > names.index(TEST_STEP)


def test_a_glob_missing_the_pytest_tag_matches_no_rewritten_module():
    """The rejecting half: the normal bytecode glob does not reach pytest's files."""
    step = {
        "uses": f"{RESTORE}@v6",
        "with": {"path": "**/__pycache__/*.cpython-315.pyc"},
    }
    assert _cache_step([step], RESTORE) is None
