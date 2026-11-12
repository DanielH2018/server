"""Every process boundary a landing crosses, as one injectable object.

A test replaces one field of `Tools` and never a PATH entry. The defaults are the real
implementations, defined here so the phase modules never import subprocess.

WHICH CHECKOUT EACH HELPER COMES FROM. await_ci, land_tags, health_gate and land_flux's
promote_lib.wait are imported from land.py's checkout, so they are the same release as it: a PR
adding a flag to one and its call site must pass its own landing, though the primary checkout
holds the previous release.

WHAT IS NOT HERE. Three path-list decisions -- `plane_note`, `derive`, `quiet_paths` -- are NOT on
`Tools`. They are pure functions of a file list, so a fake of them would leave no pipeline
test running the real derivation. They live on the frozen `Classifier` the Landing holds
beside `Tools`, so a test takes the real ones and fake boundaries.
"""

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Protocol

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `land_flux` imports `forge` bare from scripts/deploy_tools. The shim puts it at sys.path[0]
# and pytest's pythonpath lists it; this covers an interpreter import.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))  # scripts/deploy_tools
from deploy_tools import await_ci
from deploy_tools.land_lib import land_flux, land_tags, land_tested_base
from deploy_tools.land_lib import fresh, own_checkout, verdict_log
from deploy_tools.land_lib.health_gate import GateResult, gate as health_gate
from lib.exit_codes import CI_DISARMED
from deploy_tools.land_lib.land_tags import Derivation
from lib.gh import gh, gh_json
from lib.json_types import JsonValue
from lib.git import git

# scripts/deploy_tools -- where land.py and the imported helpers live.
HERE = _Path(__file__).resolve().parents[1]
# What `uv run` reads to find an existing venv instead of building one where it stands.
UV_PROJECT_ENVIRONMENT = "UV_PROJECT_ENVIRONMENT"
# The parent a gate snapshot is made in, and the env var that moves it for a test.
GATE_TMP_ROOT = "/tmp"
GATE_TMP_ROOT_ENV = "LAND_GATE_TMPDIR"


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
    because a snapshot carries no `.venv`, so the probe would otherwise build a fresh environment in a directory removed seconds later.

    DECIDED: NO tree lock is taken. `git worktree add --detach <tmp> <pinned sha>` writes the
    new directory and one file under `.git/worktrees/`, nothing in the primary's working tree,
    so there is nothing for the lock that guards that tree to serialise. Taking it
    non-blocking and degrading on a refusal is what the first cut did, and it reopened the
    gap this whole path closes: a checkout-sync run holding that lock is exactly when the
    gate would have fallen back to the primary and read `skipped` on a role the primary has
    not pulled.

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
    interval = await_ci.poll_interval(20, authenticated, timeout_s)
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


def declared_tags_at(ref: str, primary: Path) -> set[str] | None:
    """The service tags `containers_list` declares at `ref`, or None when unreadable.

    Read at the MERGE COMMIT rather than from a checkout, because a PR that adds a role and its
    `containers_list` entry together is the case a checkout answers wrongly: the entry is absent
    from every tree until checkout-sync fast-forwards, so the new role reads as one somebody forgot
    to register. None makes `land_tags` read the tree it lives in, so a ref this checkout cannot
    resolve costs nothing extra.

    AN EMPTY READ IS DAMAGE, NEVER EVIDENCE. `set()` says no service exists anywhere, which
    would make every changed role read as unregistered and every landing print
    `needs-manual-apply` with a full-`deploy.yml` remedy — fleet-wide, silently, and green in
    the suite. It is `None` here, so the fallback to this checkout takes over.
    """
    try:
        return land_tags.service_tags_at(ref, primary) or None
    except subprocess.SubprocessError, OSError, ValueError:
        return None


# Every `files` parameter below is positional-only. The real functions and the fakes name it
# differently (`files` against `paths`), and a Protocol matches a keyword-capable parameter by
# NAME -- so without the `/` a fake with an equally valid signature is rejected.
class PlaneNote(Protocol):
    """`land_tags.plane_note`: what a PR still needs a HUMAN to apply, or ""."""

    def __call__(self, files: list[str], /) -> str: ...


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
    await_ci: Callable[[str, int], CiVerdict] = await_ci_verdict
    tested_base: Callable[[str], str | None] = land_tested_base.tested_base
    # `cwd` is the checkout the probe renders the deployed role's manifests from; None is the
    # one this file lives in. `health_gate.check_one` carries the argument.
    gate: Callable[..., GateResult] = health_gate
    snapshot: Callable[[Path, str], contextlib.AbstractContextManager[Path | None]] = (
        gate_snapshot
    )
    declared_at: Callable[[str, Path], set[str] | None] = declared_tags_at
    flux: land_flux.FluxReads = field(default_factory=land_flux.FluxReads)
    # `handoff.land_through_unit`'s one boundary: starts the lander unit, reads its exit code.
    # The landing's own modules that lag master at a base commit; None when git could not say.
    stale_code: Callable[[str], list[str] | None] = own_checkout.stale_landing_code
    # `--fresh`: runs `land.sh <argv>` from a new checkout of origin/master; its exit code.
    fresh_landing: Callable[[Path, list[str]], int] = fresh.land_fresh
    systemctl: Callable[..., subprocess.CompletedProcess[str]] = systemctl
    # Its other boundary: follows the unit's journal, so its phase lines reach the handoff's log.
    follow_journal: Callable[[str, float], subprocess.Popen[str]] = follow_journal
    hostname: Callable[[], str] = socket.gethostname
    logger: Callable[[str], None] = syslog
    # Appends `(pr, rc, line)` to the per-user record `probe.py landing-verdicts` reads.
    record_verdict: Callable[[str, int, str], None] = verdict_log.record
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    # Wall-clock seconds, for the pace check.
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
    derive: Derive = land_tags.derive
    quiet_paths: Callable[[list[str], str], set[str]] = land_tags.quiet_paths
