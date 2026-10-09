"""Step 5 skips the deploy of a tag the tick's own apply already deployed here.

When the tick's own apply already covered the merge commit, step 5's second deploy would queue for
the tree lock and could fail its snapshot, grading live work `deploy-failed`. The step exists for
what the tick deferred, and that tick deferred nothing.

The skip is per HOST: the tick's broad apply names no `-e target=`, so its receipt speaks only to
the host it was written on, and a Pi-declared tag still gets its deploy. Step 6 is untouched --
the gate still runs over every tag.

Run: uv run pytest scripts/deploy_tools/tests/test_land_skips_a_deploy_the_tick_already_applied.py
"""

import pytest

from _land_fakes import MERGE_SHA, PRIMARY, Fakes, receipt
from deploy_tools.land_lib import deploy

from lib.repo_paths import REPO as _REPO
from deploy_tools.land_lib.landing import Classification

_DEPLOY_NARROW = _REPO / "ansible/roles/setup/gitops_deploy/files/deploy_narrow.py"

WHOLE_PLAY = {"ansible/deploy.yml": []}
# What `deploy_tags.py hosts` answers; the fake returns it whatever the tags asked about.
LOCAL_ONLY = "daniel-box\tsonarr\n"
WITH_THE_PI = "daniel-box\tsonarr\ndaniel-pi\talloy\n"


def _fakes(
    applied: dict[str, list[str]],
    hosts: str = LOCAL_ONLY,
    is_ancestor_rc: int = 0,
    merge_applied_rc: int = 0,
    self_applied: bool = False,
) -> Fakes:
    """A tick whose receipt covers the PR and records `applied`, carried by the primary."""
    return Fakes(
        hosts=hosts,
        state=receipt(applied),
        is_ancestor_rc=is_ancestor_rc,
        merge_applied_rc=merge_applied_rc,
        self_applied=self_applied,
    )


def _deploys(calls) -> list[tuple]:
    return [c[1] for c in calls if c[0] == "deploy"]


def _run(landing, fakes: Fakes, tags: list[str]):
    ln, calls = landing(fakes)
    ln.merge_sha = MERGE_SHA
    ln.resolved_tags = tags
    return deploy.deploy_by_host(ln), calls


@pytest.mark.parametrize(
    "tags", [[], ["radarr", "sonarr"]], ids=["whole-play", "narrowed-and-covering"]
)
def test_a_local_tag_the_tick_applied_is_not_deployed_again(landing, tags, capsys):
    rc, calls = _run(landing, _fakes({"ansible/deploy.yml": tags}), ["sonarr"])
    assert rc == 0
    assert _deploys(calls) == []
    assert "the tick already applied these on daniel-box" in capsys.readouterr().out


def test_a_pi_tag_still_deploys_while_the_local_one_is_skipped(landing):
    """The rejecting half by host: the receipt says nothing about what daniel-pi runs."""
    rc, calls = _run(
        landing,
        _fakes(WHOLE_PLAY, hosts=WITH_THE_PI),
        ["sonarr", "alloy"],
    )
    assert rc == 0
    assert _deploys(calls) == [(PRIMARY, ["alloy"], "daniel-pi")]


@pytest.mark.parametrize(
    "applied, overrides",
    [
        ({"ansible/deploy.yml": ["radarr"]}, {}),
        ({"ansible/deploy.yml": ["narrowed-to-nothing"]}, {}),
        ({"ansible/initial_setup.yml": []}, {}),
        ({}, {}),
        (WHOLE_PLAY, {"is_ancestor_rc": 1}),
        (WHOLE_PLAY, {"merge_applied_rc": 1}),
    ],
    ids=[
        "narrowed-past-the-tag",
        "narrowed-to-nothing",
        "setup-plane",
        "nothing-applied",
        "an-earlier-commit",
        "primary-behind-the-merge",
    ],
)
def test_a_receipt_that_does_not_cover_the_tag_here_deploys_it(
    landing, applied, overrides
):
    """The rejecting half by receipt: every part has to say the tag was applied, or it deploys."""
    rc, calls = _run(landing, _fakes(applied, **overrides), ["sonarr"])
    assert rc == 0
    assert _deploys(calls) == [(PRIMARY, ["sonarr"], None)]


def test_a_tag_under_no_host_is_skipped_on_the_same_terms(landing):
    """The no-host fallthrough is a local deploy, so the local skip applies to it too."""
    rc, calls = _run(landing, _fakes(WHOLE_PLAY, hosts=""), ["k8s-manifests"])
    assert rc == 0
    assert _deploys(calls) == []


def test_the_skip_holds_across_the_whole_deploy_phase(landing):
    """A deploy-plane PR the tick applied leaves step 5 with nothing to run, and step 6 to gate."""
    ln, calls = landing(_fakes(WHOLE_PLAY, self_applied=True))
    ln.merge_sha = MERGE_SHA
    ln.resolved_tags = ["sonarr"]
    ln.classification = Classification(
        self_applied=True,
    )
    ln.ledger.t_ci = 2.0
    ln.ledger.t_tick = 3.0
    deploy.deploy_phase(ln)
    assert _deploys(calls) == []
    assert ln.resolved_tags == ["sonarr"]
    assert ln.ledger.t_deploy is not None


def test_the_predicate_reads_the_marker_the_deployer_writes():
    """The two literals `tick_already_deployed` compares against are the deployer's own.

    land_lib does not import across the deployer's `files/` boundary, and the markers the
    tests above build use the same spellings, so a rename on the deployer's side would leave
    both halves green while the skip silently stopped firing. This is what breaks instead.
    """
    src = _DEPLOY_NARROW.read_text()
    assert 'playbook = "ansible/deploy.yml"' in src
    assert 'NARROWED_TO_NOTHING = "narrowed-to-nothing"' in src
