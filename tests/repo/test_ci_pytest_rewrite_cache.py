"""Each CI pytest leg restores the assertion rewrites an earlier run of its shard wrote (#8693).

Every xdist worker collects its whole shard, and on a cold checkout each one rewrites every
test module's assertions itself. pytest validates a rewritten `.pyc` by the source's hash, so a
cached one stays valid across checkouts. The cache helps only while its glob matches the name
pytest writes, a name that carries pytest's version, so the glob is checked against
`_pytest.assertion.rewrite.PYC_TAIL` rather than against a literal.

Run: uv run pytest tests/repo/test_ci_pytest_rewrite_cache.py
"""

from pathlib import PurePosixPath

from _helpers import REPO
from _pytest.assertion.rewrite import PYC_TAIL
from lib import yaml_fast

CI = REPO / ".github" / "workflows" / "ci.yml"
TEST_STEP = "Run the test suite"


def _steps() -> list[dict]:
    return yaml_fast.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["pytest"][
        "steps"
    ]


def _cache_step(steps: list[dict]) -> dict | None:
    """The pytest job's cache step whose path glob matches a rewritten module, if one does."""
    rewritten = PurePosixPath("tests/repo/__pycache__/test_x" + PYC_TAIL)
    for step in steps:
        if str(step.get("uses", "")).startswith("actions/cache@"):
            path = str((step.get("with") or {}).get("path", ""))
            if any(rewritten.full_match(glob) for glob in path.split()):
                return step
    return None


def test_the_pytest_job_caches_its_rewritten_modules_before_the_tests_run():
    steps = _steps()
    step = _cache_step(steps)
    assert step is not None, (
        f"no actions/cache step in ci.yml's pytest job matches the `*{PYC_TAIL}` files "
        "pytest writes, so every worker rewrites every module's assertions from cold"
    )
    names = [s.get("name") for s in steps]
    assert names.index(step.get("name")) < names.index(TEST_STEP)


def test_each_shard_saves_under_its_own_key_and_restores_its_latest():
    """A per-run key saves every run's rewrites; the prefix restores the shard's newest."""
    step = _cache_step(_steps())
    assert step is not None
    key, restore = step["with"]["key"], step["with"]["restore-keys"]
    assert "${{ matrix.shard }}" in key, key
    assert key.endswith("-${{ github.sha }}"), key
    assert restore == key.removesuffix("${{ github.sha }}"), (key, restore)


def test_a_glob_missing_the_pytest_tag_matches_no_rewritten_module():
    """The rejecting half: the normal bytecode glob does not reach pytest's files."""
    step = {
        "uses": "actions/cache@v6",
        "with": {"path": "**/__pycache__/*.cpython-315.pyc"},
    }
    assert _cache_step([step]) is None
