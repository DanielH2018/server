"""The `hooks` job's scoping step fails a PR whose merge would change no files.

On a `pull_request` event the checkout is the merge ref, so the step's three-dot diff against
the base branch is exactly what merging the PR would land. An empty result used to fall through
to a green full sweep. That is how #1741 and #1743 automerged on 2026-09-11 with empty squash
commits: Renovate reused the previous bump's branch under the next version's title, and master
already held its content (#1755). The step now exits non-zero there, which fails the `prek`
gate the ruleset requires.

This runs the step's own `run:` script, extracted from ci.yml, against a scratch repository
built into each of the three shapes the step distinguishes. The empty fixture reproduces the
#1741 geometry rather than a branch with no commits: a branch off an older master carrying a
commit, master carrying the same content from a separate commit, and a merge commit of the two
checked out detached with `origin/master` set.

Run: uv run pytest ansible/tests/repo/test_ci_empty_pr_merge_fails.py
"""

from pathlib import Path

import pytest
from _ci_scoping import checkout_merge_ref, commit, git, make_clone, run_step


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    return make_clone(tmp_path)


def test_a_pr_that_changes_a_file_is_scoped(clone: Path, tmp_path: Path) -> None:
    git(clone, "checkout", "-q", "-b", "pr")
    commit(clone, "bump", **{"README.md": "bumped\n"})
    checkout_merge_ref(clone, "pr")

    proc, output = run_step(clone, tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "mode=scoped" in output


def test_a_pr_that_only_deletes_runs_the_full_sweep(
    clone: Path, tmp_path: Path
) -> None:
    git(clone, "checkout", "-q", "-b", "pr")
    commit(clone, "drop", **{"keep.txt": None})
    checkout_merge_ref(clone, "pr")

    proc, output = run_step(clone, tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "mode=full" in output


def test_a_pr_whose_content_master_already_holds_fails(
    clone: Path, tmp_path: Path
) -> None:
    # The #1741 shape: the PR branch carries the bump from an older master ...
    git(clone, "checkout", "-q", "-b", "pr")
    commit(clone, "bump to 4.39.21", **{"README.md": "4.39.21\n"})
    # ... and master landed the same content by another commit (the earlier PR's squash).
    git(clone, "checkout", "-q", "master")
    commit(clone, "Update image to 4.39.21 (#1540)", **{"README.md": "4.39.21\n"})
    git(clone, "push", "-q", "origin", "master")
    checkout_merge_ref(clone, "pr")

    proc, output = run_step(clone, tmp_path)

    assert proc.returncode != 0
    assert "::error::" in proc.stdout
    assert "mode=" not in output
