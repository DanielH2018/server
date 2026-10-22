"""The builder's CI cache never outlives a re-run that turns a red commit green (#5786).

GitHub's check-runs API returns the latest run per name, so `gh run rerun --failed` replaces a
failure with a success for the same SHA. The deployer's gate asks afresh every tick and sees
it; the builder must too, or it reads `no-green` on a commit the deployer has deployed. The
fixture history, the stub GitHub and the stub render are test_host_package_builder.py's.

Run: uv run pytest ansible/roles/setup/homelab_builder/tests/test_host_package_builder_ci_cache.py
"""

import json

from test_host_package_builder import (
    GREEN,
    RED,
    builder,
    lab,  # noqa: F401 (the fixture, found by name)
    last_built,
    metric,
)

T0 = 1_760_000_000.0
# The issue's window: thirty minutes after the re-run, the builder still read `no-green`.
OBSERVED_GAP = 30 * 60


def run_at(lab, when: float) -> int:  # noqa: F811
    run = builder(lab)
    run.now = lambda: when
    return run.main()


def test_a_red_tip_that_a_rerun_turns_green_is_built(lab):  # noqa: F811
    origin, state, _, github = lab
    tip = origin.commit("a")
    github.runs[tip] = RED
    assert run_at(lab, T0) == 0
    assert metric(state)[0] == "no-green"

    github.runs[tip] = GREEN
    assert run_at(lab, T0 + OBSERVED_GAP) == 0
    assert github.asked.count(tip) == 2, "the cached fail was never asked again"
    assert last_built(state) == tip
    assert metric(state) == ("ok", tip[:12], 0.0)


def test_a_fail_already_in_ci_cache_json_does_not_block_a_green_sha(lab):  # noqa: F811
    # The state the issue met on daniel-box: a `fail` written by a run before the fix.
    origin, state, _, github = lab
    tip = origin.commit("a")
    (state / "ci-cache.json").write_text(json.dumps({tip: "fail"}))
    github.runs[tip] = GREEN
    assert run_at(lab, T0 + OBSERVED_GAP) == 0
    assert tip in github.asked
    assert last_built(state) == tip
    assert metric(state) == ("ok", tip[:12], 0.0)
