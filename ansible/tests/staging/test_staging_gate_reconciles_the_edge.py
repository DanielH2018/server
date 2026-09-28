"""The gate deploys the stage edge before it measures a verdict -- issue #2797.

WHAT WENT WRONG. `scripts/deploy_tools/staging_gate_remote.sh` deployed only the tags under
test, so stage's copy of a service no gated change had touched drifted from prod. Measured
2026-09-27 (#2789): stage's live `traefik-static` ConfigMap lacked the
`providers.kubernetesCRD.allowEmptyServices` key #2765 added, and stage's traefik ran a
different image digest from the one the repo pins. Both were five days stale. Every gated
service sits behind that edge, so each verdict in those five days described the change under an
edge config prod no longer had.

THE HALF THAT MAKES THIS MORE THAN A SPELLING TEST is the failure semantics. Folding the edge
tags into `$TAGS` would make a stage-edge fault exit non-zero from the verdict-bearing command,
which `staging_gate.classify` reads as REJECTED -- and `gitops_deploy_staging_gate_blocking` is
true on daniel-box, so a stage-edge fault would hold prod over a change that has nothing to do
with the edge. The edge is its own leg, and its failure is `fail_prep`: PREP_FAILED, which
classifies as NO_VERDICT and blocks nothing.

Run: uv run pytest ansible/tests/staging/test_staging_gate_reconciles_the_edge.py
"""

import re
import subprocess

import pytest

import staging_gate
from lib import yaml_fast
from _helpers import REPO

_REMOTE = REPO / "scripts" / "deploy_tools" / "staging_gate_remote.sh"
_INVENTORY = REPO / "ansible" / "inventory" / "host_vars" / "daniel-stage.yml"


def edge_tags() -> list[str]:
    """`EDGE_TAGS` as the shipped script assigns it, read rather than restated here."""
    match = re.search(r"^EDGE_TAGS=(\S+)$", _REMOTE.read_text(), re.MULTILINE)
    assert match, "EDGE_TAGS is gone from the gate script, so this guard checks nothing"
    return match.group(1).split(",")


def stage_services() -> set[str]:
    """What daniel-stage actually runs, the one copy true by construction."""
    entries = yaml_fast.safe_load(_INVENTORY.read_text())["containers_list"]
    return {entry["name"] for entry in entries}


def edge_tags_outside(tags: str) -> str:
    """The shipped `edge_tags_outside` helper, run against `tags`.

    Extracted and eval'd rather than reimplemented: a test that restates the filter proves
    nothing about the copy that runs. The script cannot be sourced whole -- its last line calls
    `main` -- so the two definitions it needs are lifted out by name.
    """
    source = _REMOTE.read_text()
    assignment = re.search(r"^EDGE_TAGS=\S+$", source, re.MULTILINE)
    body = re.search(
        r"^edge_tags_outside\(\) \{.*?^\}$", source, re.MULTILINE | re.DOTALL
    )
    assert assignment and body, "the edge helper moved; this test drives nothing"
    script = f'{assignment.group(0)}\n{body.group(0)}\nedge_tags_outside "$1"\n'
    return subprocess.run(
        ["bash", "-uo", "pipefail", "-c", script, "bash", tags],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_every_edge_tag_is_a_service_stage_runs() -> None:
    """Non-vacuity, and the failure it prevents is silent.

    A tag daniel-stage does not run exits `deploy.sh` 2, which the edge leg turns into
    PREP_FAILED -- so every gated tick would answer NO_VERDICT and prod would deploy unguarded.
    """
    tags = edge_tags()
    assert tags, "EDGE_TAGS is empty, so the gate reconciles nothing"
    assert set(tags) <= stage_services(), (
        f"{sorted(set(tags) - stage_services())} are in EDGE_TAGS but not in daniel-stage's "
        f"containers_list"
    )


def test_the_edge_is_deployed_before_the_verdict_command() -> None:
    """An edge deployed after the verdict would describe the next tick's stage, not this one's."""
    source = _REMOTE.read_text()
    edge = source.index('--tags "$EDGE"')
    verdict = source.index('--tags "$TAGS"')
    assert edge < verdict, (
        "the edge leg runs after the verdict-bearing deploy, so the verdict is still measured "
        "against the stale edge"
    )


def test_a_failed_edge_leg_is_a_prep_failure_not_a_rejection() -> None:
    """The line that keeps a stage-edge fault from holding prod over an unrelated change."""
    source = _REMOTE.read_text()
    leg = source[source.index('--tags "$EDGE"') :]
    assert leg.index("fail_prep") < leg.index('--tags "$TAGS"'), (
        "the edge leg no longer routes its failure through fail_prep, so a stage-edge fault "
        "reaches staging_gate.classify as a rejection"
    )
    assert staging_gate.classify(staging_gate.PREP_FAILED) == staging_gate.NO_VERDICT


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ("freshrss", "traefik,authelia"),
        ("traefik", "authelia"),
        ("traefik,authelia", ""),
    ],
)
def test_the_edge_leg_covers_only_what_the_tags_do_not(
    tags: str, expected: str
) -> None:
    """Accept and reject in one: a tag already under test is deployed once, not twice."""
    assert edge_tags_outside(tags) == expected
