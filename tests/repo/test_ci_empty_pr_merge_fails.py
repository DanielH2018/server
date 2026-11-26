"""The `hooks` job's empty-merge step fails a PR whose merge would change no files.

On a `pull_request` event the checkout is the merge ref, so the step's three-dot diff against
the base branch is exactly what merging the PR would land. An empty result would fall through
to a green full sweep and let an empty squash commit automerge, as when Renovate reuses the
previous bump's branch under the next version's title and master already holds its content.
The step exits non-zero there, which fails the `prek` gate the ruleset requires.

This runs the step's own `run:` script, extracted from ci.yml, against a scratch repository
built into each of the shapes the step distinguishes. The empty fixture reproduces that
geometry rather than a branch with no commits: a branch off an older master carrying a
commit, master carrying the same content from a separate commit, and a merge commit of the two
checked out detached with `origin/master` set.

The script runs with the step's own `env:` block, so a variable the step declares reaches it
the way the runner supplies it. A `${{ }}` expression in that block is replaced by the value
the runner resolves for a PR against master, rather than passed raw. The step calls
`scripts/dev/pr_changed_files.sh` through `$GITHUB_WORKSPACE`, which on the runner is the
checkout it stands in. Here it is this repository, so the scratch clone runs the live script
without carrying a copy of it.

Run: uv run pytest tests/repo/test_ci_empty_pr_merge_fails.py
"""

import subprocess
from pathlib import Path

import pytest
from _helpers import REPO
from lib import yaml_fast
from lib.git_testing import commit, git, init_repo, scrubbed_env
from lib.proc_testing import run

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

STEP_ID = "changed"

SCRIPT_MARKER = "would change no files"


def scoping_step(workflow_text: str, job: str) -> dict:
    steps = yaml_fast.safe_load(workflow_text)["jobs"][job]["steps"]
    matches = [step for step in steps if step.get("id") == STEP_ID]
    assert len(matches) == 1, f"expected one step with id={STEP_ID!r} in job {job!r}"
    return matches[0]


def checkout_merge_ref(repo: Path, branch: str) -> None:
    """Stand where the runner stands: a merge of the PR branch into master, detached."""
    git(repo, "checkout", "-q", "--detach", "master")
    git(repo, "merge", "-q", "--no-ff", "--no-edit", branch)


def run_step(
    repo: Path, tmp_path: Path, job: str = "hooks"
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the extracted script as the runner would; return the process and $GITHUB_OUTPUT."""
    step = scoping_step(CI_WORKFLOW.read_text(), job)
    script = step["run"]
    assert SCRIPT_MARKER in script
    runner_tmp = tmp_path / "runner_tmp"
    runner_tmp.mkdir(exist_ok=True)
    output = tmp_path / "github_output"
    output.write_text("")
    env = scrubbed_env()
    for key, value in (step.get("env") or {}).items():
        env[key] = "master" if "${{" in str(value) else str(value)
    env["BASE_REF"] = "master"
    env["RUNNER_TEMP"] = str(runner_tmp)
    env["GITHUB_WORKSPACE"] = str(REPO)
    env["GITHUB_OUTPUT"] = str(output)
    proc = run(["bash", "-c", script], cwd=repo, env=env)
    return proc, output.read_text()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone of a bare `origin` whose master holds one commit, checked out on master."""
    origin = init_repo(tmp_path / "origin.git", bare=True)
    repo = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    git(repo, "checkout", "-q", "-b", "master")
    commit(repo, "base", **{"README.md": "base\n", "keep.txt": "keep\n"})
    git(repo, "push", "-q", "origin", "master")
    return repo


def test_a_pr_that_changes_a_file_passes(clone: Path, tmp_path: Path) -> None:
    git(clone, "checkout", "-q", "-b", "pr")
    commit(clone, "bump", **{"README.md": "bumped\n"})
    checkout_merge_ref(clone, "pr")

    proc, _ = run_step(clone, tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "changes 1 file(s)" in proc.stdout


def test_a_pr_that_only_deletes_passes(clone: Path, tmp_path: Path) -> None:
    git(clone, "checkout", "-q", "-b", "pr")
    commit(clone, "drop", **{"keep.txt": None})
    checkout_merge_ref(clone, "pr")

    proc, _ = run_step(clone, tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "deletes 1" in proc.stdout


def test_a_pr_whose_content_master_already_holds_fails(
    clone: Path, tmp_path: Path
) -> None:
    # The PR branch carries the bump from an older master ...
    git(clone, "checkout", "-q", "-b", "pr")
    commit(clone, "bump to 4.39.21", **{"README.md": "4.39.21\n"})
    # ... and master landed the same content by another commit (the earlier PR's squash).
    git(clone, "checkout", "-q", "master")
    commit(clone, "Update image to 4.39.21 (#1540)", **{"README.md": "4.39.21\n"})
    git(clone, "push", "-q", "origin", "master")
    checkout_merge_ref(clone, "pr")

    proc, _ = run_step(clone, tmp_path)

    assert proc.returncode != 0
    assert "::error::" in proc.stdout
