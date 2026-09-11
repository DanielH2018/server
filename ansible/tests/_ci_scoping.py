"""Run a ci.yml scoping step's `run:` script against a scratch repository.

Both the `hooks` and the `ansible_lint` job decide `mode=full|scoped|none` in a step with
`id: changed`, and two test modules exercise those decisions: `test_ci_empty_pr_merge_fails.py`
(an empty merge fails) and `test_hook_config_changes_run_the_hook.py` (a hook-config edit forces
the full sweep). The harness lives here, at the `pythonpath` root, so neither imports the other.

The script is extracted from the workflow and run with the step's own `env:` block, so a
variable the step declares (`FULL_SWEEP_PATHS`) reaches it the way the runner supplies it, and
a `${{ }}` expression in that block is replaced by the caller's value rather than passed raw.
"""

import os
import subprocess
from pathlib import Path

from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# The step is found by its id, which the job's later `if:` expressions read as
# `steps.changed.outputs.mode`, so it cannot be renamed without those breaking too.
STEP_ID = "changed"

# A literal the extracted script must carry, so a step that stopped being the scoping step
# (or an extractor that returned some other step's script) fails here rather than passing on
# whatever `bash` made of the wrong text.
SCRIPT_MARKER = 'echo "mode=scoped" >> "$GITHUB_OUTPUT"'


def scoping_step(workflow_text: str, job: str) -> dict:
    steps = yaml_fast.safe_load(workflow_text)["jobs"][job]["steps"]
    matches = [step for step in steps if step.get("id") == STEP_ID]
    assert len(matches) == 1, f"expected one step with id={STEP_ID!r} in job {job!r}"
    return matches[0]


def scoping_script(workflow_text: str, job: str = "hooks") -> str:
    return scoping_step(workflow_text, job)["run"]


def full_sweep_paths(workflow_text: str, job: str) -> list[str]:
    """The step's `FULL_SWEEP_PATHS` block, one path per line, as the script reads it."""
    block = scoping_step(workflow_text, job).get("env", {}).get("FULL_SWEEP_PATHS", "")
    return [line for line in block.splitlines() if line.strip()]


def scrubbed_env() -> dict[str, str]:
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


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=scrubbed_env(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repo: Path, message: str, **files: str | None) -> str:
    """Write (or delete, for None) each file, commit, return the SHA."""
    for name, content in files.items():
        path = repo / name
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_clone(tmp_path: Path) -> Path:
    """A clone of a bare `origin` whose master holds one commit, checked out on master.

    A plain function rather than a fixture: a fixture imported into a test module is a
    redefinition to ruff (F811) once a test names it as a parameter, so each module wraps
    this in its own three-line `clone` fixture instead."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    repo = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    git(repo, "checkout", "-q", "-b", "master")
    commit(repo, "base", **{"README.md": "base\n", "keep.txt": "keep\n"})
    git(repo, "push", "-q", "origin", "master")
    return repo


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
    # The step's own env block, with the one expression it carries resolved the way the
    # runner would resolve it for a PR against master.
    for key, value in (step.get("env") or {}).items():
        env[key] = "master" if "${{" in str(value) else str(value)
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
