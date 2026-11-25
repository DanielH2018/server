"""The shard packer charges the render prebuilds a shard triggers, not only its test seconds.

`prebuild_seconds` is the oracle: whatever prebuild a shard's membership costs, `assign` must
pack against it, so the projected legs finish together.

Run: uv run pytest scripts/dev/tests/ci/test_pytest_shard_prebuild.py
"""

from dev import pytest_shard
from dev import shard_prebuild
import render_share
from lib.git import git
from lib.repo_paths import REPO

# CI's matrix in `.github/workflows/ci.yml`.
CI_SHARDS = 6
RESOLVABLE_SPREAD_SECONDS = 4.0
CENSUS_FILES = [
    "scripts/tests/test_census_rows_test_modules.py",
    "scripts/tests/test_census_rows_test_renders.py",
]
FLEET_CONSUMER = "scripts/renderer/tests/test_renderer_standalone_map.py"
PLAIN = "scripts/dev/tests/ci/test_pytest_shard_durations.py"


def _reads_no_render(rel: str) -> bool:
    return (
        not rel.startswith(shard_prebuild.CENSUS_FILE_PREFIX)
        and not render_share.is_consumer(REPO / rel)
        and "_k8s_render" not in (REPO / rel).read_text()
    )


def _projected(members: list[str], weights: dict[str, float]) -> float:
    tests = sum(weights[f] + pytest_shard._PER_FILE_SECONDS for f in members)
    return tests + shard_prebuild.prebuild_seconds(members)


def test_the_packing_balances_the_census_pass_a_shard_triggers():
    """Equal test weights put a census file in two of three shards and the pass in neither's
    weight, so those two legs run the 16s pass on every worker while the third idles."""
    plain = [f for f in pytest_shard.census() if _reads_no_render(f)][:200]
    assert len(plain) == 200
    files = CENSUS_FILES + plain
    weights = dict.fromkeys(files, 1.0)
    placed = pytest_shard.assign(files, 3, weights)
    loads = [
        _projected([f for f, s in placed.items() if s == i], weights) for i in range(3)
    ]
    assert max(loads) - min(loads) < RESOLVABLE_SPREAD_SECONDS, loads


def test_the_ci_split_balances_test_seconds_and_prebuilds_together():
    """The Verify-by's leg gap, projected: the legs holding the census files carried 64 runner
    seconds more prebuild than the split projected, which is the 14-17s gap CI measured."""
    files = pytest_shard.census()
    packed = pytest_shard.packing_seconds(files, pytest_shard.load_runner_weights())
    loads = [
        _projected(pytest_shard.shard_files(i, CI_SHARDS, files), packed)
        for i in range(1, CI_SHARDS + 1)
    ]
    assert max(loads) - min(loads) < RESOLVABLE_SPREAD_SECONDS, loads


def test_a_shard_holding_a_render_module_is_charged_more_prebuild_than_one_holding_none():
    """A leg collecting no render reader builds no render; a fleet consumer's leg builds one."""
    assert _reads_no_render(PLAIN)
    assert render_share.is_consumer(REPO / FLEET_CONSUMER)
    assert shard_prebuild.prebuild_seconds(
        [FLEET_CONSUMER]
    ) > shard_prebuild.prebuild_seconds([PLAIN])


def test_a_module_reaching_the_k8s_render_through_a_helper_or_a_conftest_is_charged_it(
    tmp_path,
):
    """`render_share` builds the k8s render when `_k8s_render` is in `sys.modules`, however
    it got there, so a helper's import and an ancestor conftest's import both count."""
    (tmp_path / "_k8s_render.py").write_text("SHARE_KEY = 'k8s'\n")
    (tmp_path / "_helper.py").write_text("from _k8s_render import SHARE_KEY\n")
    (tmp_path / "test_via_helper.py").write_text("from _helper import SHARE_KEY\n")
    (tmp_path / "role" / "tests").mkdir(parents=True)
    (tmp_path / "role" / "conftest.py").write_text("import _helper\n")
    (tmp_path / "role" / "tests" / "test_under_conftest.py").write_text(
        "def test_x(): pass\n"
    )
    (tmp_path / "test_plain.py").write_text("import json\n")
    git("init", "-q", cwd=tmp_path)
    git("add", ".", cwd=tmp_path)
    k8s = shard_prebuild.K8S_RENDER_WALL_SECONDS * shard_prebuild.CI_WORKERS
    assert shard_prebuild.prebuild_seconds(["test_via_helper.py"], tmp_path) == k8s
    assert (
        shard_prebuild.prebuild_seconds(["role/tests/test_under_conftest.py"], tmp_path)
        == k8s
    )
    assert shard_prebuild.prebuild_seconds(["test_plain.py"], tmp_path) == 0.0


def test_the_tree_s_indirect_k8s_render_readers_are_charged_the_render():
    """Non-vacuity over the real tree: one module per path that loads `_k8s_render` without
    naming it, as a collection with an import trace on `_k8s_render` showed."""
    for indirect in (
        "tests/k8s/test_container_port_name_property.py",
        "tests/deploy/test_k8s_autodeploy_guard.py",
        "k8s/roles/monitor-bridge/tests/test_b2_storage.py",
    ):
        assert "k8s" in shard_prebuild.prebuild_kinds(indirect), indirect


def test_the_census_files_share_one_leg_so_the_census_pass_runs_once():
    """Placed one at a time, the second census file costs less on an empty shard than beside
    the first, which ran the 16s pass on two legs."""
    placed = pytest_shard.assign(pytest_shard.census(), CI_SHARDS)
    assert len({placed[f] for f in CENSUS_FILES}) == 1, {
        f: placed[f] for f in CENSUS_FILES
    }
