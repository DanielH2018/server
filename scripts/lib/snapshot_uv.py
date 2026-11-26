"""The argv that runs a command from a snapshot checkout that shares its caller's venv.

A land gate renders from a detached worktree of the merge commit, with
`UV_PROJECT_ENVIRONMENT` pointed at the primary's `.venv` (`land_lib.tools.gate_snapshot`).
A plain `uv run` there syncs the project into that shared venv, and in a `package = true`
project that rewrites the caller's editable `.pth` to name the snapshot: every later import
in the caller then reads the snapshot's tree, which `git worktree remove` deletes seconds
later. The argv built here never installs the project. It syncs the dependencies alone, then
runs with the snapshot's `scripts/` first on `PYTHONPATH`, so the command imports the
snapshot's code and the caller's `.pth` stays byte-identical.

Typical usage example:

    subprocess.run(snapshot_uv.argv(snapshot, "python", "scripts/x.py"), cwd=snapshot)

Run: uv run pytest scripts/lib/tests/git_and_gh/test_snapshot_uv.py
"""

import shlex
from pathlib import Path

SYNC = ("uv", "sync", "--frozen", "--inexact", "--no-install-project", "--quiet")
RUN = ("uv", "run", "--no-sync")


def argv(snapshot: Path, *command: str, sync: bool = True) -> list[str]:
    """The argv that runs `command` under `uv run` from `snapshot` without installing it.

    Args:
      snapshot: the checkout root whose `pyproject.toml`, `uv.lock` and `scripts/` the
        command uses. The caller still passes `cwd=snapshot` when `command` names a path
        relative to it.
      command: the program and its arguments, as `uv run` takes them.
      sync: when True, the argv first syncs the snapshot's locked dependencies into the
        venv, without the project; when False, it runs against the venv as it stands.

    Returns:
      A list to pass to `subprocess.run`. With `sync`, it is an `sh -c` that stops at a
      failed sync and returns uv's exit code.
    """
    run = [
        "env",
        "UV_NO_SYNC=1",
        f"PYTHONPATH={snapshot / 'scripts'}",
        *RUN,
        "--project",
        str(snapshot),
        *command,
    ]
    if not sync:
        return run
    script = f'{shlex.join(SYNC)} --project "$0" && exec "$@"'
    return ["sh", "-c", script, str(snapshot), *run]
