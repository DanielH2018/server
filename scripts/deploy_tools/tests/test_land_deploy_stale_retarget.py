"""Step 5: a stale `deploy.sh --at <merge>` is retargeted to the newest green descendant.

Run: uv run pytest scripts/deploy_tools/tests/test_land_deploy_stale_retarget.py
"""

import pytest

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome

_D = "d" * 40
_D2 = "e" * 40


def _ready(landing, fakes):
    ln, calls = landing(fakes)
    ln.merge_sha = MERGE_SHA
    ln.ledger.t_ci = 2.0
    ln.ledger.t_tick = 3.0
    ln.resolved_tags = ["sonarr"]
    return ln, calls


def _ats(calls):
    """The `--at` of each deploy.sh run; "" is a run from the primary checkout."""
    return [c[2]["at"] for c in calls if c[0] == "deploy"]


def test_a_stale_at_run_deploys_the_green_descendant_without_touching_the_primary(
    landing,
):
    ln, calls = _ready(landing, Fakes(deploy=[4, 0], green_descendants=[_D]))
    deploy.deploy_phase(ln)
    assert _ats(calls) == [MERGE_SHA, _D]
    assert ln.deployed_at == _D
    # No fallback: no wait on the merge commit's CI. The one tick is the unawaited kick that
    # follows any `--at` deploy.
    assert [c[0] for c in calls].count("await_ci") == 0


def test_the_retarget_redeploys_every_host_at_the_descendant(landing):
    """One landing renders one tree: a host done at the merge commit is redone at D."""
    ln, calls = _ready(
        landing,
        Fakes(
            hosts="daniel-box\tsonarr\ndaniel-pi\talloy\n",
            deploy=[0, 4, 0, 0],
            green_descendants=[_D],
        ),
    )
    ln.resolved_tags = ["sonarr", "alloy"]
    deploy.deploy_phase(ln)
    assert _ats(calls) == [MERGE_SHA, MERGE_SHA, _D, _D]


def test_without_a_green_descendant_a_stale_at_run_falls_back_to_the_retry_loop(
    landing,
):
    ln, calls = _ready(landing, Fakes(deploy=[4, 0], green_descendants=[""]))
    deploy.deploy_phase(ln)
    assert ln.deployed_at == ""
    assert _ats(calls) == [MERGE_SHA, ""]
    assert [c[0] for c in calls].count("await_ci") == 1


def test_a_second_stale_at_run_moves_to_a_newer_descendant(landing):
    ln, calls = _ready(landing, Fakes(deploy=[4, 4, 0], green_descendants=[_D, _D2]))
    deploy.deploy_phase(ln)
    assert _ats(calls) == [MERGE_SHA, _D, _D2]
    assert ln.deployed_at == _D2


def test_a_descendant_no_newer_than_the_stale_one_falls_back(landing):
    """Offering the same SHA again would redeploy a tree that just went stale."""
    ln, calls = _ready(landing, Fakes(deploy=[4, 4, 0], green_descendants=[_D, _D]))
    deploy.deploy_phase(ln)
    assert _ats(calls) == [MERGE_SHA, _D, ""]
    assert ln.deployed_at == ""


def test_the_retargets_are_bounded_by_stale_retries(landing):
    shas = [f"{n:040x}" for n in range(1, 10)]
    ln, calls = _ready(landing, Fakes(deploy=[4], green_descendants=shas))
    with pytest.raises(Outcome) as exc:
        deploy.deploy_phase(ln)
    assert exc.value.verdict == "tip-outran-retries"
    n = ln.opts.stale_retries
    ats = _ats(calls)
    assert ats[: 1 + n] == [MERGE_SHA, *shas[:n]]
    assert set(ats[1 + n :]) == {""}
