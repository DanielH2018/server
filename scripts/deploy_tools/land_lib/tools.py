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
import fcntl
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
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
from deploy_tools import await_ci, land_reach, land_tags
from deploy_tools.deploy_detach_notify import GateResult
from deploy_tools.deploy_detach_notify import gate as health_gate
from deploy_tools.exit_codes import CI_DISARMED
from deploy_tools.land_tags import Derivation
from lib.gh import gh, gh_json
from lib.git import git

# scripts/deploy_tools -- where land.py, gitops_tick.sh and the imported helpers live.
HERE = _Path(__file__).resolve().parents[1]
LOCK = "/var/lock/server-git-tree.lock"
# What `uv run` reads to find an existing venv instead of building one where it stands.
UV_PROJECT_ENVIRONMENT = "UV_PROJECT_ENVIRONMENT"
# `uv run` here resolves the venv from cwd, which is PRIMARY at every call site.
DEPLOY_TAGS_ARGV = ("uv", "run", "python", "scripts/deploy_tools/deploy_tags.py")


# The two wrapper lines that report a wait ending in an ACQUIRE or a JOIN -- the waits
# `retry_while_locked` structurally cannot see, because it books only an attempt that exits
# 75 and both of these exit 0. deploy.sh waits inside `flock -w`; gitops_tick.sh watches a
# tick another actor already started. `gitops_tick: waited <M>s` (the tick this landing
# started itself) is deliberately absent: those seconds are the tick's own work, and the
# path that makes them long ends at exit 3, which `retry_while_locked` already books.
_ACQUIRED = re.compile(
    r"deploy: lock acquired after (\d+)s(?: \(holder was: (.*)\))?\s*$"
)
# `[^;]*` rather than `\d+` for the in-flight seconds: that number is NOT booked, so requiring
# it to parse would throw away the one that is. gitops_tick.sh derives it from /proc/uptime and
# renders `already s in flight` if that read ever comes back empty, which under `\d+` stopped
# the line matching at all and silently restored `lock=0` -- the exact failure this parser
# exists to end. Bounded at the `;` so it cannot run into the seconds that ARE booked.
_JOINED = re.compile(
    r"gitops_tick: joined a tick already [^;]*in flight; waited (\d+)s for it\s*$"
)
# deploy.sh holds the tree lock only for its snapshot and then queues on one lock per service
# (ADR-0017), so most of what a landing waits for now arrives on THIS line rather than the one
# above. It names no holder: a service lock is taken on a descriptor the way the tree lock is,
# but nothing samples `fuser` for it — the holder is by construction another deploy of the same
# service, which the line's own tag already says. A run can print several of these and
# `note_in_flock_wait` sums them, which is the right total: they are taken in sequence.
_SERVICE_ACQUIRED = re.compile(r"deploy: service lock \S+ acquired after (\d+)s\s*$")
# `annotation_line` writes the holder as `holder="..."`, so an unstripped quote splits one
# Loki row into fields the board reads as something else. `lock_holder` sanitises its own
# return the same way; this is the choke point for the wrapper-reported source.
HOLDER_MAX = 200


def in_flock_wait(line: str) -> tuple[int, str] | None:
    """The seconds and holder a wrapper's own wait line reports, or None for any other line.

    Three lines qualify: the tree lock's acquire, a per-service lock's acquire, and a landing
    that joined a tick already in flight. A deploy prints the tree line at most once and a
    service line per lock it queued on, and the caller sums them all into `lock`.

    The joined-tick line carries two numbers and only the second is booked: the first is how
    long the tick had run BEFORE this landing arrived, which is time that elapsed outside the
    landing. Booking it would push `lock` above `tick`, and `lock` is a sub-part of `tick`
    and `deploy` rather than a fifth phase.
    """
    if m := _ACQUIRED.search(line):
        return int(m[1]), (m[2] or "").replace('"', "")[:HOLDER_MAX]
    if m := _SERVICE_ACQUIRED.search(line):
        return int(m[1]), ""
    if m := _JOINED.search(line):
        return int(m[1]), ""
    return None


def stream_stderr(
    argv: list[str], cwd: Path | None, observe: Callable[[int, str], None] | None
) -> int:
    """Run `argv`, echo its stderr through line by line, report waits; its exit code.

    `cwd` is None to INHERIT this process's working directory, which is not the same as
    passing any particular path: deploy.sh renders from its working directory and deploy_tags
    reads ranges relative to it, so re-aiming either is a silent change of which checkout was
    deployed (this module's own docstring). A caller that does not need a specific cwd must
    pass None rather than a plausible-looking one.

    stdout stays this process's own handle and stderr becomes a pipe. Both are BLOCKING file
    handles, which is what Ansible requires; `land.py` clears O_NONBLOCK on the handle this
    echo writes to, and deploy.sh clears it again for the playbook.

    Every line is written out exactly as it arrived, so the landing log reads as it did when
    the child owned the handle. The pipe is read as BYTES and decoded here rather than through
    `text=True`, which turns on universal-newline translation: ansible writes bare `\r`
    progress output, and translating it would rewrite the log this echo exists to preserve.
    """
    proc = subprocess.Popen(argv, cwd=cwd, stderr=subprocess.PIPE)
    with proc:
        for chunk in proc.stderr or ():
            line = chunk.decode("utf-8", "replace")
            sys.stderr.write(line)
            sys.stderr.flush()
            if observe and (wait := in_flock_wait(line)):
                observe(*wait)
    return proc.returncode


def run_tick(
    observe: Callable[[int, str], None] | None = None, wait: bool = True
) -> int:
    """Run gitops_tick.sh from beside land.py; its exit code.

    `observe` is given the seconds and holder of a wait the wrapper reports on its own
    stderr. Without one the child simply inherits stdio: the pipe is the more fragile
    arrangement, so it is taken only when a caller is booking what it reads.

    `wait=False` passes `--no-wait`: the tick is started and this returns as soon as systemd
    has the request. A landing that deploys its own merge commit (`deploy.sh --at`) needs the
    primary checkout to converge eventually, not before it deploys, and the tick's own 10-min
    timer converges it regardless.

    The SCRIPT comes from beside land.py (issue #851) but the working directory is inherited
    either way. Pinning it to `HERE` would have aimed the tick at this checkout's
    scripts/deploy_tools, which is the re-aiming this module's docstring warns about.
    """
    argv = [str(HERE / "gitops_tick.sh")] + ([] if wait else ["--no-wait"])
    if observe is None:
        return subprocess.run(argv, check=False).returncode
    return stream_stderr(argv, None, observe)


def run_deploy(
    primary: Path,
    tags: list[str],
    target: str | None,
    observe: Callable[[int, str], None] | None = None,
    at: str = "",
) -> int:
    """Run deploy.sh in the primary checkout; its exit code.

    The tag list is joined HERE and nowhere earlier: `--tags` is an argv element, so this is
    the one place a landing needs a comma string rather than a list.

    `at` is the commit to render, passed as `--at`. It is what lets a landing deploy its PR's
    merge commit from a primary checkout the tick has not fast-forwarded onto it yet; empty
    keeps deploy.sh rendering that checkout's HEAD.

    stdio is inherited unless `observe` is given, for the reason `run_tick` states: Ansible
    refuses a non-blocking handle, and deploy.sh clears O_NONBLOCK on the handles it is
    given. Both call sites pass `observe` BY KEYWORD, which keeps the positional tuple a
    fake records three elements long.
    """
    argv = ["./scripts/deploy.sh", "--tags", ",".join(tags)]
    if at:
        argv += ["--at", at]
    if target:
        argv += ["-e", f"target={target}"]
    if observe is None:
        return subprocess.run(argv, cwd=primary, check=False).returncode
    return stream_stderr(argv, primary, observe)


def run_deploy_tags(primary: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run deploy_tags.py in the primary checkout; stdout captured, stderr inherited."""
    return subprocess.run(
        [*DEPLOY_TAGS_ARGV, *args],
        cwd=primary,
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )


@contextlib.contextmanager
def _tree_lock_held(path: str = LOCK) -> Iterator[bool]:
    """Hold the git-tree lock for the block, NON-BLOCKING; False when it could not be taken.

    `path` is the lock file, a parameter for the reason deploy.sh makes its own overridable:
    a test that took the production lock would queue behind a live gitops tick and hold one
    up behind itself.

    DECIDED: non-blocking, and a refusal degrades the caller rather than queueing. deploy.sh
    waits up to LOCK_WAIT (3000s) for this lock because it is about to deploy; the health
    gate's snapshot is worth a few seconds at most, and blocking here would put back at the
    LAST step of a landing exactly the wait this path removes from the middle of it.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o666)
    except OSError:
        yield False
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def gate_snapshot(primary: Path, sha: str, lock: str = LOCK) -> Iterator[Path | None]:
    """A detached worktree of `sha` for the health gate to render from; None when there is none.

    WHY. `probe.py health <tag>` renders the role's manifests to enumerate the workloads it
    must gate, from the checkout the probe.py it ran came from. A landing that deployed a
    snapshot of its merge commit has not moved the primary checkout at all, so a role whose
    workloads that commit ADDS enumerates nothing there and the gate reads `skipped` -- green,
    on the first landing of every new service, which is the landing that most needs gating.

    UV_PROJECT_ENVIRONMENT is pointed at the primary's `.venv` for the life of the snapshot,
    for the reason `deploy.sh`'s `run_playbook_in_snapshot` sets it: `uv run` resolves its
    project from the working directory, and a snapshot carries no `.venv`, so the probe would
    otherwise build a fresh environment inside a directory removed seconds later.

    Not created under HOMELAB_DEPLOY_SNAPSHOT_ROOT: deploy.sh's reaper collects a directory
    there whose owner lock nobody holds, and this snapshot holds none -- a concurrent deploy
    would delete it mid-gate.

    Yields None, never raises, when the tree lock is busy or the worktree could not be
    created. The caller then gates from the primary, which is what it did before this existed.
    """
    tmp = Path(tempfile.mkdtemp(prefix="land-gate-"))
    snap = tmp / "tree"
    made = False
    previous = os.environ.get(UV_PROJECT_ENVIRONMENT)
    try:
        with _tree_lock_held(lock) as locked:
            made = locked and (
                git(
                    "worktree",
                    "add",
                    "--detach",
                    str(snap),
                    sha,
                    cwd=primary,
                    check=False,
                ).returncode
                == 0
            )
        if made:
            os.environ[UV_PROJECT_ENVIRONMENT] = str(primary / ".venv")
        yield snap if made else None
    finally:
        if made:
            git(
                "worktree",
                "remove",
                "--force",
                str(snap),
                cwd=primary,
                check=False,
            )
        if previous is None:
            os.environ.pop(UV_PROJECT_ENVIRONMENT, None)
        else:
            os.environ[UV_PROJECT_ENVIRONMENT] = previous
        shutil.rmtree(tmp, ignore_errors=True)


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


def declared_tags_at(ref: str, primary: Path) -> set[str] | None:
    """The service tags `containers_list` declares at `ref`, or None when unreadable.

    Read at the MERGE COMMIT rather than from a checkout, because a PR that adds a role and
    its `containers_list` entry together is the case a checkout answers wrongly: the entry is
    absent from every tree until the tick fast-forwards, so the new role reads as one somebody
    forgot to register (issue #1544). None restores exactly the previous answer — `land_tags`
    then reads the tree it lives in — so a ref this checkout cannot resolve costs nothing more
    than it used to.

    AN EMPTY READ IS DAMAGE, NEVER EVIDENCE. `set()` says no service exists anywhere, which
    would make every changed role read as unregistered and every landing print
    `needs-manual-apply` with a full-`deploy.yml` remedy — fleet-wide, silently, and green in
    the suite. `deploy_phases.reconcile_denylist` carries the same guard and the longer argument
    (issue #1331). It is `None` here, so the fallback to this checkout takes over.
    """
    try:
        return land_tags.service_tags_at(ref, primary) or None
    except subprocess.SubprocessError, OSError, ValueError:
        return None


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
    """`land_tags.plane_note`: what a PR still needs a HUMAN to apply, or "".

    `declared` pins the set of tags that exist, the same way `Derive` does — production passes
    the set read at the merge commit, and None falls back to the tree `land_tags` lives in.
    """

    def __call__(
        self,
        files: list[str],
        /,
        declared: set[str] | None = None,
        *,
        quiet: Iterable[str] = (),
    ) -> str: ...


class SelfApplied(Protocol):
    """`land_tags.self_applied`: whether the tick applies part of this PR itself."""

    def __call__(self, files: list[str], /, *, quiet: Iterable[str] = ()) -> bool: ...


class SelfAppliedCommand(Protocol):
    """`land_tags.self_applied_command`: what applies the tick's own half by hand, or ""."""

    def __call__(self, files: list[str], /, *, quiet: Iterable[str] = ()) -> str: ...


class RemainingSetupHosts(Protocol):
    """`land_reach.remaining_setup_hosts_note`: the hosts a self-applied role still owes."""

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
    # `...` rather than the argument list: both take an optional `observe` callback, which a
    # fake absorbs through **kwargs and a call site passes by keyword.
    tick: Callable[..., int] = run_tick
    deploy: Callable[..., int] = run_deploy
    deploy_tags: Callable[[Path, list[str]], subprocess.CompletedProcess[str]] = (
        run_deploy_tags
    )
    # `cwd` is the checkout the probe renders the deployed role's manifests from; None is the
    # one this file lives in. `deploy_detach_notify.check_one` carries the argument.
    gate: Callable[..., GateResult] = field(
        default=lambda tags, cwd=None: health_gate(tags, True, cwd=cwd)
    )
    snapshot: Callable[[Path, str], contextlib.AbstractContextManager[Path | None]] = (
        gate_snapshot
    )
    declared_at: Callable[[str, Path], set[str] | None] = declared_tags_at
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
    self_applied_command: SelfAppliedCommand = land_tags.self_applied_command
    remaining_setup_hosts: RemainingSetupHosts = land_reach.remaining_setup_hosts_note
    derive: Derive = land_tags.derive
    quiet_paths: Callable[[list[str], str], set[str]] = land_tags.quiet_paths
