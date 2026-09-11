#!/usr/bin/env python3
"""`scripts/deploy.sh` asks whether the tree is stale BEFORE it validates --tags (issue #1566).

Both refusals mean nothing was deployed, but they name different causes and consumers act on
them differently: `land.sh` retries exit 4 (stale tree) after the tick fast-forwards, and
reports exit 2 (tag miss) as `deploy-failed cause=tag-miss` -- "your change broke something".

`deploy_tags.py validate` reads the CHECKOUT's own containers_list, so on a tree that is behind
origin/master it answers about the wrong tree. The first landing of a new k8s role therefore
read as a tag miss whenever the tick had not yet fast-forwarded the merge commit, because
PR #1555 derives `land.sh`'s tags from the merge commit while deploy.sh validated them against
the primary checkout. Ordering staleness first makes the stale tree the reported cause.

Both halves, per CLAUDE.md: a stale tree carrying an unknown tag must refuse as STALE (the
half the bug got wrong), and an unknown tag on a current tree must still refuse as a TAG MISS
(the half a naive reorder could delete by never reaching the tag check at all).

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_staleness_precedes_tag_validation.py
"""

import os
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"

_STALE_EXIT = 4
_TAG_MISS_EXIT = 2

_FLOCK_STUB = """#!/bin/bash
# Drop flock's own flags and its lock-file argument, then run the rest: no real
# /var/lock/server-git-tree.lock is taken, so this can never interleave with a live deploy.
while [[ $# -gt 0 ]]; do
  case "$1" in
    -w|-E) shift 2 ;;
    -n|-u) shift ;;
    *) break ;;
  esac
done
shift
exec "$@"
"""

# One `uv` for every helper deploy.sh shells out to. Each call is appended to $DEPLOY_SH_CALLS
# first, so the ORDER the wrapper asks its questions in is readable even when an early refusal
# means later helpers never run.
_UV_STUB = """#!/bin/bash
echo "$*" >> "$DEPLOY_SH_CALLS"
case "$*" in
  *deploy_staleness.py*) exit {stale_exit} ;;
  *deploy_tags.py*validate*) exit {validate_exit} ;;
  *ansible-playbook*) exit 0 ;;
  *) exit 0 ;;
esac
"""


def _run(tmp_path, *, stale_exit, validate_exit, tag="definitely-not-a-real-service"):
    """Run the real deploy.sh with `uv` and `flock` stubbed; return (result, helper calls).

    Everything under test -- the order of the two gates and the code each returns -- is the real
    script. Only the helpers' verdicts are injected, because the two states this pins (a tree
    behind origin/master, a tag absent from containers_list) cannot both be produced in a
    checkout without moving the checkout.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "flock").write_text(_FLOCK_STUB)
    (bin_dir / "uv").write_text(
        _UV_STUB.format(stale_exit=stale_exit, validate_exit=validate_exit)
    )
    for stub in ("flock", "uv"):
        (bin_dir / stub).chmod(0o755)

    calls = tmp_path / "calls.log"
    calls.write_text("")
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        DEPLOY_SH_CALLS=str(calls),
    )
    argv = [str(_DEPLOY_SH)] + (["--tags", tag] if tag else [])
    result = subprocess.run(
        argv,
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result, [ln for ln in calls.read_text().splitlines() if ln.strip()]


def test_a_stale_tree_with_an_unknown_tag_refuses_as_stale(tmp_path):
    """RED half: the shape issue #1566 reported -- a new role's tag on a not-yet-pulled tree."""
    result, calls = _run(tmp_path, stale_exit=1, validate_exit=1)
    assert result.returncode == _STALE_EXIT, result.stderr
    # The tag was never judged against the wrong tree, and nothing was deployed.
    assert not any("deploy_tags.py validate" in c for c in calls), calls
    assert not any("ansible-playbook" in c for c in calls), calls


def test_an_unknown_tag_on_a_current_tree_is_still_a_tag_miss(tmp_path):
    """CLEAN half: reordering must not swallow the tag check it moved past."""
    result, calls = _run(tmp_path, stale_exit=0, validate_exit=1)
    assert result.returncode == _TAG_MISS_EXIT, result.stderr
    assert any("deploy_tags.py validate" in c for c in calls), calls
    assert not any("ansible-playbook" in c for c in calls), calls


def test_the_staleness_gate_is_told_which_tags_are_being_deployed(tmp_path):
    """The gate refuses only on a commit reaching what this run renders, so it needs the tags.

    Both halves: a tagged run hands them over, and an untagged run (the wrapper's own
    --changed path reaches the gate before deriving any) asks the unscoped question.
    """
    tagged_dir, untagged_dir = tmp_path / "tagged", tmp_path / "untagged"
    tagged_dir.mkdir()
    untagged_dir.mkdir()
    _, tagged = _run(tagged_dir, stale_exit=0, validate_exit=0, tag="uptime-kuma")
    gate = next(c for c in tagged if "deploy_staleness.py" in c)
    assert "--tags uptime-kuma" in gate, tagged
    _, untagged = _run(untagged_dir, stale_exit=0, validate_exit=0, tag=None)
    assert "--tags" not in next(c for c in untagged if "deploy_staleness.py" in c)


def test_the_staleness_question_is_asked_before_the_tag_question(tmp_path):
    """Both gates pass: the order they were asked in is what this pins.

    Asserted on the call log rather than on an exit code, so the ordering stays checked even
    once neither gate refuses -- an exit-code-only test agrees with an implementation that
    happens to answer 4 for another reason.
    """
    result, calls = _run(tmp_path, stale_exit=0, validate_exit=0, tag="uptime-kuma")
    assert result.returncode == 0, result.stderr
    staleness = next(i for i, c in enumerate(calls) if "deploy_staleness.py" in c)
    validate = next(i for i, c in enumerate(calls) if "deploy_tags.py validate" in c)
    assert staleness < validate, calls
