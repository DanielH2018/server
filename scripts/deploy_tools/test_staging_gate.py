"""The staging gate must say "staging rejected this" and "staging could not be asked" apart.

Decision 4 of docs/staging-phase-c.md turns on that distinction: a guest that will not boot, a
dirty tree on daniel-server, an expired ssh key and a genuine bad manifest all look identical if
they collapse into one non-zero exit, and an operator who cannot tell them apart learns to
override the gate on reflex.

Every case below drives `classify`, the same function the runner drives, so a classifier that
stopped discriminating fails here rather than in an alert nobody trusts.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from staging_gate import (  # noqa: E402 — needs the path insert above
    DEPLOY_SH_NO_VERDICT,
    NO_VERDICT,
    PASS,
    PREP_FAILED,
    REJECTED,
    REMOTE_SCRIPT,
    SSH_FAILURE,
    STAGING_SERVICES,
    classify,
    verdict_name,
)

_REPO = Path(__file__).resolve().parents[2]


def test_a_clean_run_passes() -> None:
    assert classify(0) == PASS


@pytest.mark.parametrize("rc", sorted(DEPLOY_SH_NO_VERDICT))
def test_deploy_sh_refusals_are_not_rejections(rc: int) -> None:
    """2, 3, 4 and 75 all mean deploy.sh deployed NOTHING.

    Reading any of them as a rejection would fail a merge for a reason that has nothing to do
    with the merge — a busy lock, a broad change, a stale tree, a mistyped tag.
    """
    assert classify(rc) == NO_VERDICT, (
        f"deploy.sh exit {rc} means nothing was deployed, so staging has no opinion; "
        f"classifying it as a rejection blocks a merge for an unrelated reason."
    )


def test_prep_failure_is_not_a_rejection() -> None:
    """The remote could not be put at the SHA — a dirty tree, a failed fetch, a missing commit.
    Staging never saw the change."""
    assert classify(PREP_FAILED) == NO_VERDICT


def test_ssh_failure_is_not_a_rejection() -> None:
    assert classify(SSH_FAILURE) == NO_VERDICT


@pytest.mark.parametrize("rc", [1, 5, 99, 130])
def test_a_playbook_failure_is_a_rejection(rc: int) -> None:
    """The rejecting half. A classifier that returned NO_VERDICT for everything would satisfy
    every test above and make the gate incapable of ever failing a merge."""
    assert classify(rc) == REJECTED, (
        f"exit {rc} is the play itself failing, which is the one outcome the gate exists to "
        f"act on — classifying it as NO_VERDICT makes the gate inert."
    )


def test_the_three_verdicts_are_distinct() -> None:
    """Pins the premise. If two verdicts ever collapse to the same value, every test above still
    passes while the alert Decision 4 depends on can no longer be written."""
    assert len({PASS, REJECTED, NO_VERDICT}) == 3
    assert {verdict_name(v) for v in (PASS, REJECTED, NO_VERDICT)} == {
        "PASS",
        "REJECTED",
        "NO_VERDICT",
    }


def test_the_prep_code_matches_the_remote_script() -> None:
    """The two halves agree on 70 by assertion, not by hope.

    `PREP_FAILED` is duplicated across a Python module and a shell script that cannot import it.
    If they drift, prep failures classify as REJECTED and the gate starts blocking merges for
    daniel-server's housekeeping.
    """
    text = REMOTE_SCRIPT.read_text()
    match = re.search(r"^PREP_FAILED=(\d+)", text, re.M)
    assert match, "staging_gate_remote.sh no longer defines PREP_FAILED"
    assert int(match.group(1)) == PREP_FAILED, (
        f"the remote script exits {match.group(1)} on prep failure but staging_gate.py expects "
        f"{PREP_FAILED}, so a prep failure would read as a rejection."
    )


def test_the_prep_code_cannot_collide_with_a_deploy_sh_code() -> None:
    """70 is chosen to sit outside deploy.sh's vocabulary. If deploy.sh ever grows an exit that
    collides, the two meanings become indistinguishable at the far end of an ssh pipe."""
    assert PREP_FAILED not in DEPLOY_SH_NO_VERDICT
    assert PREP_FAILED not in (PASS, REJECTED, SSH_FAILURE)


def test_the_default_tags_are_services_staging_actually_runs() -> None:
    """A tag naming a service outside the subset exits 2 on the far side — a NO_VERDICT that
    reads like a broken gate rather than a typo. Checked against the host's own inventory."""
    host_vars = (
        _REPO / "ansible" / "inventory" / "host_vars" / "daniel-stage.yml"
    ).read_text()
    declared = set(re.findall(r"^\s+- name:\s*(\S+)", host_vars, re.M))
    missing = sorted(set(STAGING_SERVICES) - declared)
    assert not missing, (
        f"{missing} are in STAGING_SERVICES but not in daniel-stage's containers_list, so the "
        f"default deploy would exit 2 on a tag that matches nothing."
    )


def test_the_remote_script_returns_deploy_sh_untouched() -> None:
    """The verdict-bearing command must be the last thing the script runs.

    Anything after it — a cleanup, an echo, a `|| true` — replaces deploy.sh's exit code with
    that command's, and every rejection silently becomes a pass.
    """
    lines = [
        line.strip()
        for line in REMOTE_SCRIPT.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # The body lives in a `main` function -- bash reads a script by byte offset, and the
    # dispatcher execs this file from the very checkout it fast-forwards, so the whole body has
    # to be parsed before any of it runs. The property below is unchanged by that: what must be
    # last is still the deploy.sh call, now followed only by the closing brace and the call.
    assert lines[-1] == 'main "$@"', (
        f"the last executable line of staging_gate_remote.sh is {lines[-1]!r}, not the "
        f'`main "$@"` that runs the body.'
    )
    assert lines[-2] == "}", (
        f"expected the `main` function to close immediately before it is called, found "
        f"{lines[-2]!r}"
    )
    assert lines[-3].startswith("./scripts/deploy.sh"), (
        f"the last command inside main() is {lines[-3]!r}, not the deploy.sh call — its exit "
        f"code, not deploy.sh's, is what the gate would classify."
    )


def deploy_invocation(source: str) -> str:
    """The gate's verdict-bearing deploy.sh command line, as the remote shell will run it."""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("./scripts/deploy.sh"):
            return stripped
    return ""


def test_the_staging_deploy_does_not_refuse_a_tree_behind_the_tip() -> None:
    """The staging clone is pinned to the SHA under test, so it is behind origin/master
    whenever anything merges during the run — by construction, not by accident.

    deploy.sh's staleness guard is right for a production host and wrong here: the tick
    resolves `origin` once, ff-merges to it, asks staging about it, and deploys THAT to prod
    (gitops_deploy.py, `if cs.k8s_deploy:`). A merge landing mid-run moves the tip without
    changing what this tick ships, so exit 4 -> NO_VERDICT is a false failure. Two of four
    hand-runs on 2026-08-29 died that way, and NO_VERDICT is silent by design.
    """
    invocation = deploy_invocation(REMOTE_SCRIPT.read_text())
    assert invocation, "no ./scripts/deploy.sh call found in staging_gate_remote.sh"
    assert "--skip-staleness-check" in invocation, (
        f"the staging deploy must pass --skip-staleness-check; got {invocation!r}. Without it "
        f"any merge inside the run's window makes deploy.sh exit 4, which classify() maps to "
        f"NO_VERDICT — a false failure against the rate slice 4 gates on."
    )


def test_a_staleness_refusing_invocation_is_caught() -> None:
    # Red proof, on the same extractor. The rejected input is what the script said until
    # 2026-08-29, and the accepted one is what it says now.
    now = deploy_invocation(
        '# a comment\n./scripts/deploy.sh --tags "$TAGS" -e target=daniel-stage --skip-staleness-check\n'
    )
    assert "--skip-staleness-check" in now

    before = deploy_invocation(
        './scripts/deploy.sh --tags "$TAGS" -e target=daniel-stage\n'
    )
    assert before and "--skip-staleness-check" not in before, (
        "the extractor must read the actual command line, or dropping the flag reads as "
        "still carrying it"
    )
    assert deploy_invocation("# ./scripts/deploy.sh in a comment only\n") == "", (
        "a commented-out call must not count as the invocation"
    )
