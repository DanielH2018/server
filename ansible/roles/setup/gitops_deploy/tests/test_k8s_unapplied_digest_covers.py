"""A matching render discharges a role that is not digest-provable when its range is covered.

Before #4781 a render proved nothing for such a role, so a deploy-plane change that moved no
rendered byte left its line until the role was deployed for some other reason: 35 lines after
#4753. `deploy_k8s_owed.discharge_k8s_unapplied` now asks `digest_covers` whether the range
from the role's release record to the render's commit changed anything outside the digest.
`scripts/deploy_tools/tests/test_digest_covers.py` holds the case table for that answer; this
module holds what the discharge does with it.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_k8s_unapplied_digest_covers.py
"""

import pytest

from deployer_lib import deploy_k8s_owed
from deployer_lib.deploy_toolbox import DeployTools
from gitops_ledger import OWED_K8S_UNAPPLIED

ORIGIN = "a" * 40
APPLIED = "b" * 40
RENDER = "c" * 40


def _none(_items) -> set[str]:
    return set()


@pytest.fixture
def asked():
    """The item sets each `digest_covers` call was asked about, in call order."""
    return []


def _discharge(state, settings, asked, covers, render=RENDER, record=APPLIED):
    """authelia, not provable, has a record predating ORIGIN and a render at `render`."""

    def digest_covers(_repo, items):
        asked.append(set(items))
        return covers(items)

    tools = DeployTools(
        discord_post=lambda _webhook, _content: True,
        release_commit=lambda _svc: record,
        is_ancestor=lambda _repo, _origin, commit: commit == RENDER,
        render_proof=lambda _svc: render,
        digest_provable=lambda _repo, _roles: set(),
        digest_covers=digest_covers,
    )
    return deploy_k8s_owed.discharge_k8s_unapplied(tools, state, settings)


@pytest.fixture
def pending(gitops_deploy, state_dir, settings, state):
    state.record_owed(OWED_K8S_UNAPPLIED, ORIGIN, {"authelia"}, 1000.0)
    return state


def test_a_covered_range_discharges_on_the_render(pending, settings, asked):
    """FLAGGED half, the #4753 shape: asserts in the play moved no byte and no task."""
    assert _discharge(pending, settings, asked, lambda items: set(items)) == [
        "authelia"
    ]
    assert pending.owed_pending(OWED_K8S_UNAPPLIED) == []


def test_the_range_runs_from_the_release_record_to_the_render(pending, settings, asked):
    """Not from the line's origin, which `record_owed` moves past an earlier change."""
    _discharge(pending, settings, asked, _none)
    assert asked == [{f"authelia:{APPLIED}:{RENDER}"}]


@pytest.mark.parametrize(
    "covers",
    [
        pytest.param(_none, id="not-covered"),
        pytest.param(
            lambda _items: (_ for _ in ()).throw(ValueError("bad json")),
            id="derivation-fails",
        ),
    ],
)
def test_an_uncovered_or_unjudged_range_keeps_the_line(
    pending, settings, asked, covers
):
    """CLEAN half: a range that reached the role's own tasks, or no answer at all."""
    assert _discharge(pending, settings, asked, covers) == []
    assert [e.service for e in pending.owed_pending(OWED_K8S_UNAPPLIED)] == ["authelia"]


@pytest.mark.parametrize(
    "render, record",
    [
        pytest.param(None, APPLIED, id="no-matching-render"),
        pytest.param(RENDER, None, id="no-release-record"),
    ],
)
def test_no_range_is_asked_without_both_ends(pending, settings, asked, render, record):
    """The render is the proof and the record is the range's start; either missing keeps it."""
    assert _discharge(pending, settings, asked, _none, render, record) == []
    assert asked == []


def test_a_shared_role_judges_each_callers_own_range(gitops_deploy, settings, state):
    """`image-builder` acts outside the digest, so a caller's render needs its range covered."""
    state.record_owed(OWED_K8S_UNAPPLIED, ORIGIN, {"image-builder"}, 1000.0)
    asked = []
    tools = DeployTools(
        discord_post=lambda _webhook, _content: True,
        release_commit=lambda svc: None if svc == "image-builder" else APPLIED + svc,
        is_ancestor=lambda _repo, _origin, commit: commit.startswith(RENDER),
        render_proof=lambda svc: RENDER + svc,
        shared_role_callers=lambda _repo, roles: {
            r: {"n8n", "terraria"} for r in roles
        },
        digest_provable=lambda _repo, _roles: set(),
        digest_covers=lambda _repo, items: asked.append(set(items)) or set(items),
    )
    assert deploy_k8s_owed.discharge_k8s_unapplied(tools, state, settings) == [
        "image-builder"
    ]
    assert asked == [
        {f"image-builder:{APPLIED}{svc}:{RENDER}{svc}" for svc in ("n8n", "terraria")}
    ]
