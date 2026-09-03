"""Every process boundary a landing crosses, as one injectable object.

A test replaces one field of `Tools` and never a PATH entry. The defaults are the real
implementations, defined here so the phase modules never import subprocess.

WHICH CHECKOUT EACH HELPER COMES FROM. await_ci, land_tags and deploy_detach_notify are
imported from beside land.py, so they are always the same release as it -- a PR adding a flag
to one and its call site used to fail on its own landing, because the primary checkout still
held the previous release (PR #850, issue #851). gitops_tick.sh is run from beside land.py
for the same reason. deploy_tags.py and deploy.sh are run as subprocesses with the PRIMARY
checkout as cwd, because their question IS the primary checkout: `blockers` reads
`HEAD..origin/master` and `changed` reads `<since>...HEAD`, and deploy.sh renders from its
working directory. Moving either to this file's checkout would silently re-aim them at the
worktree's HEAD.
"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# land_tags and deploy_detach_notify `import deploy_tags` bare, so their own directory has
# to be reachable too. It is under the shim (land.py's dir is sys.path[0]) and under pytest
# (pythonpath lists it); this insert makes an interpreter-only import work as well.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))  # scripts/deploy_tools
from deploy_tools import await_ci, land_tags
from deploy_tools.deploy_detach_notify import gate as health_gate
from lib.gh import gh, gh_json
from lib.git import git

# scripts/deploy_tools -- where land.py, gitops_tick.sh and the imported helpers live.
HERE = _Path(__file__).resolve().parents[1]
LOCK = "/var/lock/server-git-tree.lock"
# `uv run` here resolves the venv from cwd, which is PRIMARY at every call site.
DEPLOY_TAGS_ARGV = ("uv", "run", "python", "scripts/deploy_tools/deploy_tags.py")


def run_tick() -> int:
    """Run gitops_tick.sh from beside land.py, stdio inherited; its exit code."""
    return subprocess.run([str(HERE / "gitops_tick.sh")], check=False).returncode


def run_deploy(primary: Path, tags: str, target: str | None) -> int:
    """Run deploy.sh in the primary checkout, stdio inherited; its exit code.

    stdio is inherited on purpose: Ansible refuses a non-blocking handle, and deploy.sh
    clears O_NONBLOCK on the handles it is given.
    """
    argv = ["./scripts/deploy.sh", "--tags", tags]
    if target:
        argv += ["-e", f"target={target}"]
    return subprocess.run(argv, cwd=primary, check=False).returncode


def run_deploy_tags(primary: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run deploy_tags.py in the primary checkout; stdout captured, stderr inherited."""
    return subprocess.run(
        [*DEPLOY_TAGS_ARGV, *args],
        cwd=primary,
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )


def await_ci_verdict(sha: str, timeout_s: int) -> tuple[int, str]:
    """await_ci.wait with its CLI's exit contract: 0 green, 1 red, 75 pending, 2 disarmed."""
    try:
        return await_ci.wait(sha, timeout_s, 20)
    except await_ci.DisarmedGateError as exc:
        return 2, f"await_ci: {exc}"


def syslog(line: str) -> None:
    """One logfmt line into syslog, which Alloy ships to Loki for the Landings board."""
    subprocess.run(
        ["logger", "-t", "landing-annotation", line],
        check=True,
        capture_output=True,
        timeout=10,
    )


def lock_holder() -> str:
    """The tree lock's holder as `<etimes> <command>`, or '' when nobody holds it.

    fuser prints the PIDs on stdout and the path on stderr; the lowest PID is the flock
    parent, its children inherit the descriptor. 200 characters rather than 120: an
    `ansible-playbook` command line is long enough that the tags -- the part that says which
    landing holds the lock -- fell off the end (issue #1031).
    """
    with contextlib.suppress(
        OSError, subprocess.SubprocessError, ValueError, StopIteration
    ):
        out = subprocess.run(
            ["fuser", LOCK], capture_output=True, text=True, timeout=5, check=False
        ).stdout
        pid = next(tok for tok in out.split() if tok.isdigit())
        ps = subprocess.run(
            ["ps", "-o", "etimes=,args=", "-p", pid],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
        return " ".join(ps.split()).replace('"', "")[:200]
    return ""


def read_state(deployer_state: Path, name: str) -> str:
    """The deployer's `<name>` marker, stripped; '' when the file is missing or empty."""
    with contextlib.suppress(OSError):
        return (deployer_state / name).read_text().strip()
    return ""


@dataclass
class Tools:
    """Every process boundary, so a test replaces one field and never a PATH entry."""

    gh_json: Callable[..., Any] = gh_json
    gh: Callable[..., subprocess.CompletedProcess[str]] = gh
    git: Callable[..., subprocess.CompletedProcess[str]] = git
    await_ci: Callable[[str, int], tuple[int, str]] = await_ci_verdict
    tick: Callable[[], int] = run_tick
    deploy: Callable[[Path, str, str | None], int] = run_deploy
    deploy_tags: Callable[[Path, list[str]], subprocess.CompletedProcess[str]] = (
        run_deploy_tags
    )
    gate: Callable[[list[str]], tuple[bool, list[str]]] = field(
        default=lambda tags: health_gate(tags, True)
    )
    plane_note: Callable[..., str] = land_tags.plane_note
    self_applied: Callable[..., bool] = land_tags.self_applied
    remaining_setup_hosts: Callable[..., str] = land_tags.remaining_setup_hosts_note
    derive: Callable[..., tuple[list[str], str]] = land_tags.derive
    quiet_paths: Callable[[list[str], str], set[str]] = land_tags.quiet_paths
    read_state: Callable[[Path, str], str] = read_state
    lock_holder: Callable[[], str] = lock_holder
    hostname: Callable[[], str] = socket.gethostname
    logger: Callable[[str], None] = syslog
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
