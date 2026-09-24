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

The gates run in process in `deploy_run.py` (#2412), so `run_front_half` injects their
verdicts; the order they are asked in is the real code.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_staleness_precedes_changed_derivation.py
"""

from _deploy_sh_fakes import make_snapshot_repo, run_front_half

_STALE_EXIT = 4
_BROAD_EXIT = 3


def _run(tmp_path, monkeypatch, *, stale, changed=(0, ""), args=("--changed",)):
    repo = make_snapshot_repo(tmp_path / "repo")
    code, calls = run_front_half(
        monkeypatch, repo, list(args), stale=stale, changed=changed
    )
    return code, [c[0] for c in calls], calls


def test_a_stale_tree_deriving_no_tags_refuses_as_stale_rather_than_exiting_zero(
    tmp_path, monkeypatch
):
    """RED half: the shape issue #1593 reported -- silence wearing the success code."""
    code, names, _ = _run(tmp_path, monkeypatch, stale=1)
    assert code == _STALE_EXIT
    assert "changed" not in names and "deploy" not in names, names


def test_a_stale_tree_with_a_broad_change_refuses_as_stale_not_as_broad(
    tmp_path, monkeypatch
):
    """The other pre-gate answer: exit 3 also reached the caller before the staleness read."""
    code, names, _ = _run(tmp_path, monkeypatch, stale=1, changed=(_BROAD_EXIT, ""))
    assert code == _STALE_EXIT
    assert "changed" not in names, names


def test_a_current_tree_still_derives_its_tags_and_deploys_them(tmp_path, monkeypatch):
    """CLEAN half: hoisting the gate must not refuse a tree that is fine.

    Asserted on the call log as well as the exit code, so the ordering stays checked once
    neither gate refuses -- an exit-code-only test agrees with an implementation that answers
    0 for another reason.
    """
    code, names, calls = _run(
        tmp_path, monkeypatch, stale=0, changed=(0, "uptime-kuma")
    )
    assert code is None, names
    assert names.index("staleness") < names.index("changed") < names.index("deploy")
    # The derived list became the run's --tags, which the locked half receives.
    assert calls[-1][1] == ["in-process", "uptime-kuma"], calls[-1]


def test_a_current_tree_with_a_broad_change_still_refuses_as_broad(
    tmp_path, monkeypatch
):
    """The second CLEAN half: exit 3 must survive the reorder on a tree that is not stale."""
    code, names, _ = _run(tmp_path, monkeypatch, stale=0, changed=(_BROAD_EXIT, ""))
    assert code == _BROAD_EXIT
    assert "changed" in names and "deploy" not in names, names


def test_a_current_tree_deriving_no_tags_exits_zero_having_run_nothing(
    tmp_path, monkeypatch
):
    """Nothing changed and nothing is behind: the one case where 0 with no deploy is true."""
    code, names, _ = _run(tmp_path, monkeypatch, stale=0, changed=(0, ""))
    assert code == 0
    assert "deploy" not in names, names


def test_the_staleness_gate_is_asked_once_not_twice(tmp_path, monkeypatch):
    """The hoist adds a call site; it must not add a second fetch to every --changed run."""
    _code, names, _ = _run(tmp_path, monkeypatch, stale=0, changed=(0, "uptime-kuma"))
    assert names.count("staleness") == 1, names


def test_skip_staleness_check_still_bypasses_the_hoisted_gate(tmp_path, monkeypatch):
    """The escape hatch has to reach the new call site too, or --changed can never use it."""
    code, names, _ = _run(
        tmp_path,
        monkeypatch,
        stale=1,
        changed=(0, "uptime-kuma"),
        args=("--changed", "--skip-staleness-check"),
    )
    assert code is None, names
    assert "staleness" not in names, names
