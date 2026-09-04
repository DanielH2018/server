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
WHAT IS NOT HERE. Five path-list decisions used to sit on `Tools` beside the process
boundaries -- `plane_note`, `self_applied`, `remaining_setup_hosts`, `derive`, `quiet_paths`.
They are pure functions of a file list, so the fakes replaced them with constant lambdas and
no pipeline test ever ran real tag derivation. They are `Classifier` now, a separate frozen
dataclass the Landing holds beside `Tools`, so a test can take the real ones and the fake
boundaries.
"""

import contextlib
import socket
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Protocol

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# land_tags and deploy_detach_notify `import deploy_tags` bare, so their own directory has
# to be reachable too. It is under the shim (land.py's dir is sys.path[0]) and under pytest
# (pythonpath lists it); this insert makes an interpreter-only import work as well.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))  # scripts/deploy_tools
from deploy_tools import await_ci, land_tags
from deploy_tools.deploy_detach_notify import GateResult
from deploy_tools.deploy_detach_notify import gate as health_gate
from deploy_tools.exit_codes import CI_DISARMED
from deploy_tools.land_tags import Derivation
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


def run_deploy(primary: Path, tags: list[str], target: str | None) -> int:
    """Run deploy.sh in the primary checkout, stdio inherited; its exit code.

    The tag list is joined HERE and nowhere earlier: `--tags` is an argv element, so this is
    the one place a landing needs a comma string rather than a list.

    stdio is inherited on purpose: Ansible refuses a non-blocking handle, and deploy.sh
    clears O_NONBLOCK on the handles it is given.
    """
    argv = ["./scripts/deploy.sh", "--tags", ",".join(tags)]
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


class CiVerdict(NamedTuple):
    """await_ci's exit code and the one line it printed to explain it."""

    rc: int
    line: str


def await_ci_verdict(sha: str, timeout_s: int) -> CiVerdict:
    """await_ci.wait with its CLI's exit contract: 0 green, 1 red, 75 pending, 2 disarmed."""
    try:
        return CiVerdict(*await_ci.wait(sha, timeout_s, 20))
    except await_ci.DisarmedGateError as exc:
        return CiVerdict(CI_DISARMED, f"await_ci: {exc}")


def syslog(line: str) -> None:
    """One logfmt line into syslog, which Alloy ships to Loki for the Landings board."""
    subprocess.run(
        ["logger", "-t", "landing-annotation", line],
        check=True,
        capture_output=True,
        timeout=10,
    )


def lock_holder() -> str:
    """The tree lock's holder as `pid <pid> (etimes, command): <etimes> <command>`, or ''.

    fuser prints the PIDs on stdout and the path on stderr; the lowest PID is the flock
    parent, its children inherit the descriptor. bash's `note_lock_contention` kept the pid
    in a separate local and only folded it into the printed `say` line, leaving this
    string (which also feeds the `holder="..."` annotation field) pid-less; this single
    return value is the only thing callers have, so the pid is folded in here instead.
    200 characters rather than 120: an `ansible-playbook` command line is long enough that
    the tags -- the part that says which landing holds the lock -- fell off the end
    (issue #1031).
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
        etimes_command = " ".join(ps.split()).replace('"', "")
        return f"pid {pid} (etimes, command): {etimes_command}"[:200]
    return ""


def read_state(deployer_state: Path, name: str) -> str | None:
    """The deployer's `<name>` marker, stripped; '' when absent, None when unreadable.

    ABSENT AND UNREADABLE ARE DIFFERENT ANSWERS. A missing marker means the deployer is not
    holding and is not behind, which is the ordinary case on every healthy tick. A directory
    this process cannot read answers nothing at all, and collapsing the two made
    `Landing.tick_state` report `converged` -- "the tick applied it" -- for a state directory
    it never saw. Callers must fail closed on None; `tick_state` does.
    """
    try:
        return (deployer_state / name).read_text().strip()
    except FileNotFoundError:
        return ""
    except OSError:
        return None


# Every `files` parameter below is positional-only. The real functions and the fakes name it
# differently (`files` against `paths`), and a Protocol matches a keyword-capable parameter by
# NAME -- so without the `/` a fake with an equally valid signature is rejected.
class PlaneNote(Protocol):
    """`land_tags.plane_note`: what a PR still needs a HUMAN to apply, or ""."""

    def __call__(self, files: list[str], /, *, quiet: Iterable[str] = ()) -> str: ...


class SelfApplied(Protocol):
    """`land_tags.self_applied`: whether the tick applies part of this PR itself."""

    def __call__(self, files: list[str], /, *, quiet: Iterable[str] = ()) -> bool: ...


class RemainingSetupHosts(Protocol):
    """`land_tags.remaining_setup_hosts_note`: the hosts a self-applied role still owes."""

    def __call__(
        self, files: list[str], local_host: str, /, *, quiet: Iterable[str] = ()
    ) -> str: ...


class Derive(Protocol):
    """`land_tags.derive`: the deploy tags a PR's own file list maps to.

    `declared` pins the set of tags that exist, for a test; production passes none and
    `land_tags` reads the inventory.
    """

    def __call__(
        self, files: list[str], changed_files: int, /, declared: set[str] | None = None
    ) -> Derivation: ...


@dataclass
class Tools:
    """Every process boundary, so a test replaces one field and never a PATH entry."""

    gh_json: Callable[..., Any] = gh_json
    gh: Callable[..., subprocess.CompletedProcess[str]] = gh
    git: Callable[..., subprocess.CompletedProcess[str]] = git
    await_ci: Callable[[str, int], CiVerdict] = await_ci_verdict
    tick: Callable[[], int] = run_tick
    deploy: Callable[[Path, list[str], str | None], int] = run_deploy
    deploy_tags: Callable[[Path, list[str]], subprocess.CompletedProcess[str]] = (
        run_deploy_tags
    )
    gate: Callable[[list[str]], GateResult] = field(
        default=lambda tags: health_gate(tags, True)
    )
    read_state: Callable[[Path, str], str | None] = read_state
    lock_holder: Callable[[], str] = lock_holder
    hostname: Callable[[], str] = socket.gethostname
    logger: Callable[[str], None] = syslog
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class Classifier:
    """The pure path-list decisions, held beside `Tools` rather than inside it.

    Every one is a function of a changed-file list and nothing else: no subprocess, no
    network, no clock. Keeping them here is what lets a pipeline test drive the REAL
    derivation over a fixed path list while every boundary in `Tools` stays fake.
    """

    plane_note: PlaneNote = land_tags.plane_note
    self_applied: SelfApplied = land_tags.self_applied
    remaining_setup_hosts: RemainingSetupHosts = land_tags.remaining_setup_hosts_note
    derive: Derive = land_tags.derive
    quiet_paths: Callable[[list[str], str], set[str]] = land_tags.quiet_paths
