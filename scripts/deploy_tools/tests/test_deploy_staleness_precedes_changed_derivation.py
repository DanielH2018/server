#!/usr/bin/env python3
"""`scripts/deploy.sh --changed` asks whether the tree is stale BEFORE it derives its tags.

Issue #1593, one path over from #1566. The `--changed` pre-parse resolved its tag list at the
top of the script, ahead of every gate, and both of its answers reached the caller before
anything asked whether the checkout was behind origin/master:

* `deploy_tags.py changed` exits 3 on a broad change and deploy.sh propagates it.
* The derived list is EMPTY on a tree that is only BEHIND -- every commit of its own already
  merged makes the three-dot range empty by construction -- so deploy.sh exited 0 having
  deployed nothing. Exit 0 is the one code no consumer treats as a resume point, so the
  quietest possible failure wore the success code.

Both halves, per CLAUDE.md: a stale tree must refuse as STALE before the derivation runs (the
half the bug got wrong), and a current tree must still reach the derivation and deploy what it
finds (the half a naive reorder could delete by refusing everything).

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_staleness_precedes_changed_derivation.py
"""

import os
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"

_STALE_EXIT = 4
_BROAD_EXIT = 3

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
  *deploy_tags.py*changed*) echo "{derived}"; exit {changed_exit} ;;
  *deploy_tags.py*validate*) exit 0 ;;
  *ansible-playbook*) exit 0 ;;
  *) exit 0 ;;
esac
"""


def _run(tmp_path, *, stale_exit, changed_exit=0, derived="", args=("--changed",)):
    """Run the real deploy.sh with `uv` and `flock` stubbed; return (result, helper calls).

    Everything under test -- which question the wrapper asks first, and the code it returns --
    is the real script. Only the helpers' verdicts are injected: a checkout that is behind
    origin/master while carrying no unmerged commit of its own cannot be produced without
    moving the checkout.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "flock").write_text(_FLOCK_STUB)
    (bin_dir / "uv").write_text(
        _UV_STUB.format(
            stale_exit=stale_exit, changed_exit=changed_exit, derived=derived
        )
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
    result = subprocess.run(
        [str(_DEPLOY_SH), *args],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result, [ln for ln in calls.read_text().splitlines() if ln.strip()]


def test_a_stale_tree_deriving_no_tags_refuses_as_stale_rather_than_exiting_zero(
    tmp_path,
):
    """RED half: the shape issue #1593 reported -- silence wearing the success code."""
    result, calls = _run(tmp_path, stale_exit=1, derived="")
    assert result.returncode == _STALE_EXIT, (result.returncode, result.stderr)
    assert not any("deploy_tags.py changed" in c for c in calls), calls
    assert not any("ansible-playbook" in c for c in calls), calls


def test_a_stale_tree_with_a_broad_change_refuses_as_stale_not_as_broad(tmp_path):
    """The other pre-gate answer: exit 3 also reached the caller before the staleness read."""
    result, calls = _run(tmp_path, stale_exit=1, changed_exit=_BROAD_EXIT)
    assert result.returncode == _STALE_EXIT, (result.returncode, result.stderr)
    assert not any("deploy_tags.py changed" in c for c in calls), calls


def test_a_current_tree_still_derives_its_tags_and_deploys_them(tmp_path):
    """CLEAN half: hoisting the gate must not refuse a tree that is fine.

    Asserted on the call log as well as the exit code, so the ordering stays checked once
    neither gate refuses -- an exit-code-only test agrees with an implementation that answers
    0 for another reason.
    """
    result, calls = _run(tmp_path, stale_exit=0, derived="uptime-kuma")
    assert result.returncode == 0, (result.returncode, result.stderr)
    staleness = next(i for i, c in enumerate(calls) if "deploy_staleness.py" in c)
    changed = next(i for i, c in enumerate(calls) if "deploy_tags.py changed" in c)
    assert staleness < changed, calls
    assert any("ansible-playbook" in c for c in calls), calls


def test_a_current_tree_with_a_broad_change_still_refuses_as_broad(tmp_path):
    """The second CLEAN half: exit 3 must survive the reorder on a tree that is not stale."""
    result, calls = _run(tmp_path, stale_exit=0, changed_exit=_BROAD_EXIT)
    assert result.returncode == _BROAD_EXIT, (result.returncode, result.stderr)
    assert any("deploy_tags.py changed" in c for c in calls), calls
    assert not any("ansible-playbook" in c for c in calls), calls


def test_the_staleness_gate_is_asked_once_not_twice(tmp_path):
    """The hoist adds a call site; it must not add a second fetch to every --changed run."""
    _result, calls = _run(tmp_path, stale_exit=0, derived="uptime-kuma")
    assert sum(1 for c in calls if "deploy_staleness.py" in c) == 1, calls


def test_skip_staleness_check_still_bypasses_the_hoisted_gate(tmp_path):
    """The escape hatch has to reach the new call site too, or --changed can never use it."""
    result, calls = _run(
        tmp_path,
        stale_exit=1,
        derived="uptime-kuma",
        args=("--changed", "--skip-staleness-check"),
    )
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert not any("deploy_staleness.py" in c for c in calls), calls
