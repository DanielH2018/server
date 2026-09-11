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

import os
import subprocess
from pathlib import Path

import pytest
from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# The step is found by its id, which the job's later `if:` expressions read as
# `steps.changed.outputs.mode`, so it cannot be renamed without those breaking too.
JOB = "hooks"
STEP_ID = "changed"

# A literal the extracted script must carry, so a step that stopped being the scoping step
# (or an extractor that returned some other step's script) fails here rather than passing on
# whatever `bash` made of the wrong text.
SCRIPT_MARKER = 'echo "mode=scoped" >> "$GITHUB_OUTPUT"'


def scoping_script(workflow_text: str) -> str:
    steps = yaml_fast.safe_load(workflow_text)["jobs"][JOB]["steps"]
    matches = [step for step in steps if step.get("id") == STEP_ID]
    assert len(matches) == 1, f"expected one step with id={STEP_ID!r} in job {JOB!r}"
    return matches[0]["run"]


def _scrubbed_env() -> dict[str, str]:
    """The real environment minus every GIT_* variable, plus a scratch identity.

    `prek`'s pytest hook runs with `GIT_DIR` set, and git resolves that before `cwd`, so an
    unscrubbed call reads and writes the real checkout. `GIT_CONFIG_GLOBAL` goes to /dev/null
    because this host configures SSH commit signing globally, which a scratch commit cannot
    satisfy."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.invalid"
    env["GIT_CONFIG_GLOBAL"] = env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_scrubbed_env(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str, **files: str | None) -> str:
    """Write (or delete, for None) each file, commit, return the SHA."""
    for name, content in files.items():
        path = repo / name
        if content is None:
            path.unlink()
        else:
            path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone of a bare `origin` whose master holds one commit, checked out on master."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    repo = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    _git(repo, "checkout", "-q", "-b", "master")
    _commit(repo, "base", **{"README.md": "base\n", "keep.txt": "keep\n"})
    _git(repo, "push", "-q", "origin", "master")
    return repo


def _checkout_merge_ref(repo: Path, branch: str) -> None:
    """Stand where the runner stands: a merge of the PR branch into master, detached."""
    _git(repo, "checkout", "-q", "--detach", "master")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", branch)


def run_step(
    repo: Path, tmp_path: Path
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the extracted script as the runner would; return the process and $GITHUB_OUTPUT."""
    script = scoping_script(CI_WORKFLOW.read_text())
    assert SCRIPT_MARKER in script
    runner_tmp = tmp_path / "runner_tmp"
    runner_tmp.mkdir()
    output = tmp_path / "github_output"
    output.touch()
    env = _scrubbed_env()
    env["BASE_REF"] = "master"
    env["RUNNER_TEMP"] = str(runner_tmp)
    env["GITHUB_OUTPUT"] = str(output)
    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    return proc, output.read_text()


def test_a_pr_that_changes_a_file_is_scoped(clone: Path, tmp_path: Path) -> None:
    _git(clone, "checkout", "-q", "-b", "pr")
    _commit(clone, "bump", **{"README.md": "bumped\n"})
    _checkout_merge_ref(clone, "pr")

    proc, output = run_step(clone, tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "mode=scoped" in output


def test_a_pr_that_only_deletes_runs_the_full_sweep(
    clone: Path, tmp_path: Path
) -> None:
    _git(clone, "checkout", "-q", "-b", "pr")
    _commit(clone, "drop", **{"keep.txt": None})
    _checkout_merge_ref(clone, "pr")

    proc, output = run_step(clone, tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "mode=full" in output


def test_a_pr_whose_content_master_already_holds_fails(
    clone: Path, tmp_path: Path
) -> None:
    # The #1741 shape: the PR branch carries the bump from an older master ...
    _git(clone, "checkout", "-q", "-b", "pr")
    _commit(clone, "bump to 4.39.21", **{"README.md": "4.39.21\n"})
    # ... and master landed the same content by another commit (the earlier PR's squash).
    _git(clone, "checkout", "-q", "master")
    _commit(clone, "Update image to 4.39.21 (#1540)", **{"README.md": "4.39.21\n"})
    _git(clone, "push", "-q", "origin", "master")
    _checkout_merge_ref(clone, "pr")

    proc, output = run_step(clone, tmp_path)

    assert proc.returncode != 0
    assert "::error::" in proc.stdout
    assert "mode=" not in output
