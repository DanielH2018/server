"""Every process boundary a landing crosses, as one injectable object.

A test replaces one field of `Tools` and never a PATH entry. The defaults are the real
implementations, defined here so the phase modules never import subprocess.

WHICH CHECKOUT EACH HELPER COMES FROM. await_ci, land_tags, deploy_lib/detach_notify and
land_flux's promote_lib.wait are imported from land.py's checkout, so they are the same release as
it: a PR adding a flag to one and its call site must pass its own landing, though the primary
checkout holds the previous release. gitops_tick.sh is run from beside land.py for that reason.
deploy_tags.py and deploy.sh are run as subprocesses with the PRIMARY checkout as cwd,
because their question IS the primary checkout: `blockers` reads `HEAD..origin/master` and
`changed` reads `<since>...HEAD`, and deploy.sh renders from its working directory. Moving
either to this file's checkout would silently re-aim them at the worktree's HEAD. WHAT IS NOT
HERE. Five path-list decisions -- `plane_note`, `self_applied`, `remaining_setup_hosts`,
`derive`, `quiet_paths` -- are NOT on `Tools`. They are pure functions of a file list, so a
fake of them would leave no pipeline test running the real derivation. They live on the frozen
`Classifier` the Landing holds beside `Tools`, so a test takes the real ones and fake boundaries.
"""

import contextlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Protocol

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# land_tags imports `narrow_setup` bare, so its directory must be reachable. The shim puts it
# at sys.path[0] and pytest's pythonpath lists it; this covers an interpreter import.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))  # scripts/deploy_tools
from lib.repo_paths import GITOPS_DEPLOY_FILES

# The deployer's own modules import each other bare (`import deploy_locks`), so the directory
# has to be on the path for the names this module reads from it.
_sys.path.insert(2, str(GITOPS_DEPLOY_FILES))
from deployer_lib.deploy_locks import TREE_LOCK as LOCK
from gitops_hold import DeployerSnapshot, read_field
from gitops_markers import MARKERS
from deploy_tools import await_ci, shared_role_reach
from deploy_tools.land_lib import land_platform, land_reach, land_rerolls
from deploy_tools.land_lib import land_flux, land_shared, land_tags, land_tested_base
from deploy_tools.land_lib import verdict_log
from deploy_tools.deploy_lib.detach_notify import GateResult, gate as health_gate
from lib.exit_codes import CI_DISARMED
from deploy_tools.land_lib.land_tags import Derivation
from lib.gh import gh, gh_json
from lib.json_types import JsonValue
from lib.git import git

# scripts/deploy_tools -- where land.py, gitops_tick.sh and the imported helpers live.
HERE = _Path(__file__).resolve().parents[1]
# What `uv run` reads to find an existing venv instead of building one where it stands.
UV_PROJECT_ENVIRONMENT = "UV_PROJECT_ENVIRONMENT"
# `uv run` resolves the venv from cwd, PRIMARY here, which may predate `deploy_cli.py` (#4347).
DEPLOY_TAGS_ARGV = ("uv", "run", "python", "scripts/deploy_tools/deploy_tags.py")
# The parent a gate snapshot is made in, and the env var that moves it for a test.
GATE_TMP_ROOT = "/tmp"
GATE_TMP_ROOT_ENV = "LAND_GATE_TMPDIR"


# The two wrapper lines that report a wait ending in an ACQUIRE or a JOIN -- the waits
# `retry_while_locked` structurally cannot see, because it books only an attempt that exits
# 75 and both of these exit 0. deploy.sh waits inside `flock -w`; gitops_tick.sh watches a
# tick another actor already started. `gitops_tick.sh: waited <M>s` (a tick this landing
# started) is absent: those seconds are the tick's own work, and the path that makes them
# long ends at exit 3, which `retry_while_locked` books. The deploy patterns also take the
# pre-#4478 `deploy:`, as PRIMARY can predate this file; `run_tick` runs the tick beside it.
_ACQUIRED = re.compile(
    r"deploy(?:\.sh)?: lock acquired after (\d+)s(?: \(holder was: (.*)\))?\s*$"
)
# `[^;]*` rather than `\d+` for the in-flight seconds: that number is NOT booked, and
# gitops_tick.sh renders `already s in flight` when its /proc/uptime read comes back empty,
# which `\d+` would refuse, silently restoring `lock=0` -- the failure this parser exists to
# end. Bounded at the `;` so it cannot run into the seconds that ARE booked.
_JOINED = re.compile(
    r"gitops_tick\.sh: joined a tick already [^;]*in flight; waited (\d+)s for it\s*$"
)
# deploy.sh holds the tree lock only for its snapshot, then queues on one lock per service
# (ADR-0017), so most of a landing's wait arrives on THIS line. It names no holder: nothing
# samples `fuser` for a service lock, and the holder is by construction another deploy of the
# same service. A run can print several, taken in sequence, and `note_in_flock_wait` sums them.
_SERVICE_ACQUIRED = re.compile(
    r"deploy(?:\.sh)?: service lock \S+ acquired after (\d+)s\s*$"
)
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
    has the request -- or exits `TICK_JOINED` having started none, because a run was already
    in flight. A landing that deploys its own merge commit (`deploy.sh --at`) needs the
    primary checkout to converge eventually, not before it deploys, and the tick's own 10-min
    timer converges it regardless.

    The SCRIPT comes from beside land.py but the working directory is inherited
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
    # DECIDED: deploy.sh stays a subprocess; land.sh does not call `deploy_run` in process
    # (#3664). An import runs THIS checkout's deploy_run, not the primary's, which is the
    # re-aiming the module docstring rules out. deploy_run also chdirs into the repo, execs
    # ansible-playbook on the unlocked paths and clears O_NONBLOCK on fds 0-2 -- all
    # process-wide, so each would land on the landing itself.
    # DECIDED: no structured result comes back either; the exit code is the result (#3687).
    # Each field such a result would carry is already known here. The verdict is one of
    # `lib.exit_codes`' named DEPLOY_* constants, which both sides import from one module.
    # The tags applied are the `--tags` this function passes: land.sh resolves `--changed`
    # itself (`deploy.derive_from_diff`) and never hands it to deploy.sh. The resume point is
    # per host, and `Landing.deployed_hosts` records it. A JSON line would be a second channel
    # that must agree with the exit code and adds no fact a landing acts on.
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
def _dies_on_a_signal() -> Generator[None]:
    """SIGTERM and SIGINT raise inside the block, so an enclosing `finally` still runs.

    Default SIGTERM disposition terminates the interpreter outright, skipping every `finally`
    -- which for `gate_snapshot` leaves a worktree registered in the primary that nothing
    collects. `KeyboardInterrupt` rather than a new exception type: `land.py` catches neither
    (its own `finally` suppresses `Exception` only, so this propagates through it after the
    ledger row is written), and reusing it keeps a SIGTERM'd landing and a Ctrl-C'd one on one
    path. The previous handlers are restored on the way out, because the test suite runs this
    in pytest's main thread.
    """

    def raise_it(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    for sig in previous:
        signal.signal(sig, raise_it)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextlib.contextmanager
def gate_snapshot(primary: Path, sha: str) -> Generator[Path | None]:
    """A detached worktree of `sha` for the health gate to render from; None when there is none.

    WHY. `probe.py health <tag>` renders the role's manifests to enumerate the workloads it
    must gate, from the checkout the probe.py it ran came from. A landing that deployed a
    snapshot of its merge commit has not moved the primary checkout at all, so a role whose
    workloads that commit ADDS enumerates nothing there and the gate reads `skipped` -- green,
    on the first landing of every new service, which is the landing that most needs gating.

    UV_PROJECT_ENVIRONMENT is pointed at the primary's `.venv` for the life of the snapshot,
    for the reason `deploy_playbook.run_playbook` sets it: a snapshot carries no `.venv`, so
    the probe would otherwise build a fresh environment in a directory removed seconds later.

    DECIDED: NO tree lock is taken. `git worktree add --detach <tmp> <pinned sha>` writes the
    new directory and one file under `.git/worktrees/`, nothing in the primary's working tree,
    so there is nothing for the lock that guards that tree to serialise. Taking it
    non-blocking and degrading on a refusal is what the first cut did, and it reopened the
    gap this whole path closes: `gitops-deploy.service` holds that lock for its entire unit
    run, so the tick most landings race is exactly when the gate would have fallen back to
    the primary and read `skipped` on a role the primary has not pulled.

    DECIDED: under `tempfile.mkdtemp`, NOT under HOMELAB_DEPLOY_SNAPSHOT_ROOT. deploy.sh's
    reaper collects any directory there whose `.deploy-owner.lock` nobody holds -- and it
    creates that lock file itself to ask, so an unowned live gate snapshot would be reaped
    mid-gate by a concurrent deploy. Outside the root the reaper never looks, and the prune at
    `deploy_under_locks.reap_dead_snapshots` only deregisters worktrees already gone.

    Yields None, never raises, when the worktree could not be created. The caller must NOT
    assume the primary can answer instead (see `health_verdict.gate_from_the_deployed_tree`).

    DECIDED: the parent directory is PINNED to GATE_TMP_ROOT rather than left to `mkdtemp`'s
    own default, which honours TMPDIR -- on this host that is `/tmp/user/1000`, so a leaked
    snapshot would sit where the recovery below does not look. GATE_TMP_ROOT_ENV moves it.

    A SIGKILL or a host reboot still leaks the registration: the directory survives, so
    nothing prunes it. Recover by hand with `rm -rf /tmp/land-gate-*` then
    `git -C <primary> worktree prune`.
    """
    # The signal handlers wrap the try, not the reverse: they are restored only after the
    # worktree is gone, so a second signal arriving during the removal cannot skip it.
    with _dies_on_a_signal():
        root = os.environ.get(GATE_TMP_ROOT_ENV) or GATE_TMP_ROOT
        tmp = Path(tempfile.mkdtemp(prefix="land-gate-", dir=root))
        snap = tmp / "tree"
        made = False
        previous = os.environ.get(UV_PROJECT_ENVIRONMENT)
        try:
            made = (
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


def await_ci_verdict(
    sha: str, timeout_s: int, wait=await_ci.wait, token=await_ci.lookup_token
) -> CiVerdict:
    """await_ci.wait with its CLI's exit contract: 0 green, 1 red, 75 pending, 2 disarmed.

    An anonymous wait polls every 90s, not 20s, and walks no descendants
    (`await_ci.ANONYMOUS_INTERVAL_S`, #6148). A zero budget is one poll that never sleeps or
    reaches the dropped-push grace, so the merge loop's per-poll call skips the lookup.
    """
    authenticated = bool(token()) if timeout_s else True
    interval = await_ci.poll_interval(20, authenticated)
    try:
        return CiVerdict(
            *wait(sha, timeout_s, interval, walk_descendants=authenticated)
        )
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


def systemctl(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    """`systemctl <args>`, its output captured; never raises on a non-zero exit."""
    return subprocess.run(
        ["systemctl", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def follow_journal(unit: str, since: float) -> subprocess.Popen[str]:
    """`journalctl -f` on `unit` from `since` (epoch seconds), one message per line on stdout.

    `-o cat` prints the message alone, so a relayed `== ` phase line reads as the unit wrote it.
    `-f` reads only the current boot, which holds every unit this landing starts.
    """
    return subprocess.Popen(
        [
            "journalctl",
            "-u",
            unit,
            "-f",
            "-o",
            "cat",
            "--no-pager",
            f"--since=@{int(since)}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def lock_holder() -> str:
    """The tree lock's holder as `pid <pid> (etimes, command): <etimes> <command>`, or ''.

    fuser prints the PIDs on stdout and the path on stderr; the lowest PID is the flock
    parent, its children inherit the descriptor. The pid is folded into this string, which
    also feeds the `holder="..."` annotation field, because this single return value is the
    only thing callers have. 200 characters rather than 120: an `ansible-playbook` command
    line is long enough that the tags -- the part that says which landing holds the lock --
    would fall off the end.
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

    Read at the MERGE COMMIT rather than from a checkout, because a PR that adds a role and its
    `containers_list` entry together is the case a checkout answers wrongly: the entry is absent
    from every tree until the tick fast-forwards, so the new role reads as one somebody forgot
    to register. None makes `land_tags` read the tree it lives in, so a ref this checkout cannot
    resolve costs nothing extra.

    AN EMPTY READ IS DAMAGE, NEVER EVIDENCE. `set()` says no service exists anywhere, which
    would make every changed role read as unregistered and every landing print
    `needs-manual-apply` with a full-`deploy.yml` remedy — fleet-wide, silently, and green in
    the suite. `deploy_phases.reconcile_denylist` carries the same guard and the longer
    argument. It is `None` here, so the fallback to this checkout takes over.
    """
    try:
        return land_tags.service_tags_at(ref, primary) or None
    except subprocess.SubprocessError, OSError, ValueError:
        return None


# Each basename `read_state` accepts, mapped to the `DeployerSnapshot` field that holds it.
_SNAPSHOT_FIELDS = {MARKERS[f]: f for f in DeployerSnapshot._fields}


def read_state(deployer_state: Path, name: str) -> str | None:
    """The deployer's `<name>` marker, stripped; '' when absent, None when unreadable.

    A basename `gitops_hold.DeployerSnapshot` carries (`hold_sha`, `behind_since`) is read
    through `gitops_hold.read_field` (#3972), one marker only, so a torn sibling cannot turn
    a readable hold into None. Any other basename (`receipts.jsonl`) is read raw.

    ABSENT AND UNREADABLE ARE DIFFERENT ANSWERS. A missing marker means the deployer is not
    holding and is not behind, the ordinary case. Collapsing the two made
    `Landing.tick_state` report `converged` for a state directory it never saw. Callers must
    fail closed on None; `tick_state` does.
    """
    field_name = _SNAPSHOT_FIELDS.get(name)
    try:
        if field_name is None:
            return (deployer_state / name).read_text().strip()
        return read_field(deployer_state, field_name) or ""
    except FileNotFoundError:
        return ""
    except OSError, UnicodeDecodeError:
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
        narrow_tags: dict[str, frozenset[str]] | None = None,
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
        self,
        files: list[str],
        local_host: str,
        /,
        *,
        quiet: Iterable[str] = (),
        pr_range: str = "",
        ref: str = "",
        repo: Path = ...,
    ) -> str: ...


class Derive(Protocol):
    """`land_tags.derive`: a PR's deploy tags. `declared` pins the tag set, for a test."""

    def __call__(
        self,
        files: list[str],
        changed_files: int,
        declared: set[str] | None = None,
        quiet: Iterable[str] = (),
    ) -> Derivation: ...


@dataclass
class Tools:
    """Every process boundary, so a test replaces one field and never a PATH entry."""

    gh_json: Callable[..., JsonValue] = gh_json
    gh: Callable[..., subprocess.CompletedProcess[str]] = gh
    git: Callable[..., subprocess.CompletedProcess[str]] = git
    # Above `await_ci:`, whose field name shadows the module for the rest of this class body.
    newest_green_descendant: Callable[[str], str] = await_ci.newest_green_descendant
    await_ci: Callable[[str, int], CiVerdict] = await_ci_verdict
    tested_base: Callable[[str], str | None] = land_tested_base.tested_base
    # `...`, not the argument list: both take an optional `observe` callback, passed by keyword.
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
    # The tags another deploy re-rolled after this landing's own, each with the commit that
    # deploy rendered. Asked only after a failed gate; it waits out any such deploy first.
    later_deploys: Callable[..., dict[str, str]] = land_rerolls.later_deploys
    declared_at: Callable[[str, Path], set[str] | None] = declared_tags_at
    # `deploy_tags.py hosts` reads the checkout; this reads `containers_list` at the merge
    # commit a fast-path landing deploys. None sends the caller to the subprocess.
    landing_hosts_at: Callable[[Any, str, Path, Any], dict[str, list[str]] | None] = (
        land_tags.landing_hosts_at
    )
    read_state: Callable[[Path, str], str | None] = read_state
    flux: land_flux.FluxReads = field(default_factory=land_flux.FluxReads)
    # The PR's own narrowing, for a landing that never awaits the tick and so has no receipt
    # to read. It runs git over the PR's range, so it is a boundary; `classify.narrow_plane` calls it.
    own_narrowing: Callable[[list[str], str, Path], dict[str, frozenset[str]]] = (
        land_tags.own_narrow_tags
    )
    # Also runs git over the PR's range -- the key diff of a shared role's `defaults/main.yml`
    # and a grep of every template for those keys. `classify.classify` is its caller.
    paths_a_hand_must_apply: Callable[
        [list[str], str, Path, set[str] | None], list[str]
    ] = shared_role_reach.paths_a_hand_must_apply
    lock_holder: Callable[[], str] = lock_holder
    # `handoff.land_through_unit`'s one boundary: starts the lander unit, reads its exit code.
    systemctl: Callable[..., subprocess.CompletedProcess[str]] = systemctl
    # Its other boundary: follows the unit's journal, so its phase lines reach the handoff's log.
    follow_journal: Callable[[str, float], subprocess.Popen[str]] = follow_journal
    hostname: Callable[[], str] = socket.gethostname
    logger: Callable[[str], None] = syslog
    # Appends `(pr, rc, line)` to the per-user record `probe.py landing-verdicts` reads.
    record_verdict: Callable[[str, int, str], None] = verdict_log.record
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    # Wall-clock seconds, for comparing with a release record's `applied_at`.
    wall_clock: Callable[[], float] = time.time
    # None means `host_wait`'s own, which imports this module and so cannot be named here.
    host_targets: Callable[[Path, str], list[Any]] | None = None
    wait_for_hosts: Callable[..., None] | None = None


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
    # Reads the role-caller graph from this checkout's tree, as `plane_note` always has.
    shared_caller_tags: Callable[[list[str], set[str] | None], dict[str, set[str]]] = (
        land_tags.shared_caller_tags
    )
    # Which of those roles was narrowed to one smoke caller, so the operator line says so
    # rather than claiming every caller was deployed.
    smoke_narrowed_roles: Callable[[list[str], set[str] | None], frozenset[str]] = (
        land_shared.narrowed_roles
    )
    # Which derived tags the PR's own paths prove are a k3s change. Beside
    # `shared_caller_tags` because `deploy_by_host` routes the union of the two the same way.
    k8s_only_tags: Callable[[list[str], set[str] | None], list[str]] = (
        land_platform.k8s_only_tags
    )
    quiet_paths: Callable[[list[str], str], set[str]] = land_tags.quiet_paths
