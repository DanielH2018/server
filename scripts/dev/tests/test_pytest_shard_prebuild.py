"""The shard packer charges the render prebuilds a shard triggers, not only its test seconds.

`prebuild_seconds` is the oracle: whatever prebuild a shard's membership costs, `assign` must
pack against it, so the projected legs finish together (#8900).

Run: uv run pytest scripts/dev/tests/test_pytest_shard_prebuild.py
"""

import pytest_shard
import render_share
from lib.repo_paths import REPO

# CI's matrix in `.github/workflows/ci.yml`.
CI_SHARDS = 6
# What the packer resolves over more than a thousand modules: the test-seconds-only spread of
# the committed split is about 0.3 runner seconds.
RESOLVABLE_SPREAD_SECONDS = 4.0
CENSUS_FILES = [
    "scripts/tests/test_census_rows_test_modules.py",
    "scripts/tests/test_census_rows_test_renders.py",
]
FLEET_CONSUMER = "scripts/renderer/tests/test_renderer_standalone_map.py"
PLAIN = "scripts/dev/tests/test_pytest_shard_durations.py"


def _reads_no_render(rel: str) -> bool:
    return (
        not rel.startswith(pytest_shard.CENSUS_FILE_PREFIX)
        and not render_share.is_consumer(REPO / rel)
        and "_k8s_render" not in (REPO / rel).read_text()
    )


def _projected(members: list[str], weights: dict[str, float]) -> float:
    tests = sum(weights[f] + pytest_shard._PER_FILE_SECONDS for f in members)
    return tests + pytest_shard.prebuild_seconds(members)


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
    weights = pytest_shard.packing_table(pytest_shard.load_weights())
    files = pytest_shard.census()
    loads = [
        _projected(
            pytest_shard.shard_files(i, CI_SHARDS, files, weights),
            {f: weights.get(f, 0.0) for f in files},
        )
        for i in range(1, CI_SHARDS + 1)
    ]
    assert max(loads) - min(loads) < RESOLVABLE_SPREAD_SECONDS, loads


def test_a_shard_holding_a_render_module_is_charged_more_prebuild_than_one_holding_none():
    """A leg collecting no render reader builds no render; a fleet consumer's leg builds one."""
    assert _reads_no_render(PLAIN)
    assert render_share.is_consumer(REPO / FLEET_CONSUMER)
    assert pytest_shard.prebuild_seconds(
        [FLEET_CONSUMER]
    ) > pytest_shard.prebuild_seconds([PLAIN])
