"""Fakes for every land_lib boundary; the two fixtures that use them live in conftest.py.

Every fake appends `(name, args, kwargs)` to a shared `calls` list, which is how ordering
tests prove "blockers before the CI wait" without reading source.

`build_classifier` is separate from `build_tools` because the two answer different kinds of
question: `Tools` is the process boundaries, `Classifier` is pure path-list logic. A test
that wants the REAL derivation passes `Classifier()` and keeps the fake boundaries --
`test_land_pipeline.py` has one that does.
"""

import atexit
import contextlib
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from deploy_tools.promote_lib.wait import WaitResult
from typing import Any

from deploy_tools.deploy_lib.detach_notify import GateResult
from deploy_tools.land_lib import landing as landing_mod
from _land_host_fakes import host_fakes
from deploy_tools.land_lib.options import Options
from deploy_tools.land_lib.land_flux import FluxReads
from deploy_tools.land_lib.pin_reads import PinReads
from validate.cluster_pins import Pin
from deploy_tools.land_lib.tools import Classifier, CiVerdict, Tools
from deploy_tools.land_lib.land_tags import Derivation, DeriveSource

MERGE_SHA = "0123456789abcdef0123456789abcdef01234567"
# The commit `last-good` names unless a test says otherwise.
LAST_GOOD = "1" * 40
# Where every faked pin lives; the gate reads only the value.
PIN_FILE = "ansible/roles/setup/k3s/defaults/main.yml"


# A real directory, because the pipeline refuses a primary checkout that is not one. Made
# once per session rather than per test, so `cwd=PRIMARY` assertions stay comparable.
#
# TemporaryDirectory rather than mkdtemp: mkdtemp leaves the directory behind for good, and
# `-n auto` makes one per xdist worker on every run. The object is held at module scope so
# its finalizer runs at interpreter exit and not before.
#
# The cleanup is done via an explicit atexit hook rather than relying on TemporaryDirectory's
# own implicit finalizer. That finalizer fires from a `weakref.finalize` callback and emits a
# `ResourceWarning` first, which `filterwarnings = ["error"]` (pyproject.toml) turns into an
# exception raised inside atexit processing -- non-deterministically, since it depends on
# whether pytest's warnings plugin has already restored the original filters by the time the
# interpreter tears this module down. An explicit `.cleanup()` registered here runs first and
# removes the directory, so the implicit finalizer finds nothing left to warn about.
_PRIMARY_TMP = tempfile.TemporaryDirectory(
    prefix="land-primary-", ignore_cleanup_errors=True
)
atexit.register(_PRIMARY_TMP.cleanup)
PRIMARY = Path(_PRIMARY_TMP.name)
STATE = Path("/state")


def _cp(rc: int = 0, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")


@dataclass
class Fakes:
    """What each fake answers; every field is a per-test override.

    A list is consumed one entry per call, the last entry repeating.
    """

    gh_views: dict[str, Any] = field(default_factory=dict)
    gh_merge_rc: list[int] = field(default_factory=lambda: [0])
    fetch_rc: int = 0
    pull_ref_rc: int = 0
    tip: str = MERGE_SHA
    await_ci: list[tuple[int, str]] = field(default_factory=lambda: [(0, "CI green")])
    blockers: list[int] = field(default_factory=lambda: [0])
    changed: str = ""
    changed_rc: int = 0
    gate: tuple[bool, list[str]] = field(
        default_factory=lambda: (True, ["sonarr: healthy"])
    )
    # What `tools.snapshot` yields: a directory for a snapshot that was taken, None for one
    # that could not be (the tree lock busy, the worktree add failed).
    gate_snapshot: Path | None = Path("/snap")
    plane: str = ""
    derived: tuple[list[str], str] = field(default_factory=lambda: (["sonarr"], "pr"))
    # What `containers_list` declares at the merge commit. None is the read having failed,
    # which is what every land_lib reader falls back to its own tree on.
    declared_at: set[str] | None = None
    # `git merge-base --is-ancestor <merge_sha> <recorded apply>`: 0 means the recorded broad
    # apply included this PR, non-zero means it ran at a commit that did not contain it.
    is_ancestor_rc: int = 0
    # Per-ref overrides of `is_ancestor_rc`, keyed by the ref the query asks about (the last
    # argument), for a test that needs one SHA's ancestry to differ from another's.
    is_ancestor_of: dict[str, int] = field(default_factory=dict)
    # The same query against HEAD: whether the primary checkout already carries the merge
    # commit, which `health_verdict` asks when a gate snapshot could not be taken.
    merge_applied_rc: int = 1
    # A marker's value; None is a read that failed, which `Landing.state` passes on as None.
    # Mapping, not dict: a dict is invariant in its values, so the many `dict[str, str]`
    # callers would no longer type-check.
    state: Mapping[str, str | None] = field(default_factory=dict)
    # What `tools.own_narrowing` answers on the fast path: role tag -> this PR's derivation.
    own_narrowing: dict[str, frozenset[str]] = field(default_factory=dict)
    # `git show <ref>:<path>` text by ref. An absent ref answers as a missing file.
    show_at: dict[str, str] = field(default_factory=dict)
    hostname: str = "daniel-box"
    # The forward-only pins `tools.pins.pins_at` answers, by SHA: pin key -> value. A SHA absent
    # here holds neither pin, so a PR whose base and head are both absent bumps nothing.
    pins: dict[str, dict[str, str]] = field(default_factory=dict)
    # DEPLOY_HOLD and `last-good`'s commit as the pin gate reads them; an Exception raises.
    # A list of DEPLOY_HOLD values is read one per call, the last one repeating.
    deploy_hold: str | None | Exception | list[str | None] = "none"
    last_good: str | None | Exception = LAST_GOOD
    # The first-parent commits in (last-good, tip], oldest first, as `git rev-list` prints them.
    walk: list[str] = field(default_factory=list)
    # What each `runbook_gates.py <set>` run answers: (exit code, output).
    gates: list[tuple[int, str]] = field(
        default_factory=lambda: [(0, "gate 1 ok\nall 4 gates passed")]
    )
    # What `gh api repos/{owner}/{repo}` answers: the visibility --arm-merge refuses on.
    repo: dict[str, Any] = field(default_factory=lambda: {"visibility": "public"})
    # What the REST `pulls/<n>/files` listing answers, as one page: the landing policy's input.
    pr_files: list[dict[str, Any]] = field(
        default_factory=lambda: [{"status": "modified", "filename": "docs/landing.md"}]
    )
    # `git ls-tree -r <head>`: path -> mode at the PR head the policy pinned. None answers
    # every listed path but a removed one as a regular file, so a PR that sets no tree reads
    # as having no symlink and no path missing from its head.
    head_tree: dict[str, str] | None = None
    ls_tree_rc: int = 0
    # What the REST `pulls/<n>/reviews` listing answers, as one page.
    pr_reviews: list[dict[str, Any]] = field(default_factory=list)
    # `tools.tested_base`: the master commit a head's green CI run tested it against, by head.
    # A head absent here is one whose run log cannot be read, so the merge base stands.
    tested_base: dict[str, str] = field(default_factory=dict)
    # What `gh api compare/<head>...master` answers, one entry per call: the paths master
    # changed since the PR's merge base. Empty is a PR already up to date with master, and an
    # exception is raised in place of an answer.
    compare: list[list[str] | Exception] = field(default_factory=lambda: [[]])
    # What `closing_refs`' GraphQL read answers, one entry per call.
    closing_refs: list[dict | Exception] = field(default_factory=lambda: [{}])
    # What the master `ci.yml` runs listing answers, one entry per call, as `pace.py` reads it:
    # `{status, created_at}` per run. Empty is a master with no CI backlog, and an exception is
    # raised in place of an answer.
    master_runs: list[list[dict[str, str]] | Exception] = field(
        default_factory=lambda: [[]]
    )
    # What the PR head's `ci.yml` runs listing answers on every call, as `merge._ready_run_on`
    # reads it: `{id, status, conclusion}` per run. The default is one draft run that skipped,
    # and an exception is raised in place of an answer.
    head_runs: Any = field(
        default_factory=lambda: [
            {"id": 1, "status": "completed", "conclusion": "skipped"}
        ]
    )
    # `tools.owners_at`: each tag that sets `owner` at a ref, by ref. A ref absent here has
    # none. An exception is raised in place of a map.
    owners: dict[str, dict[str, str] | Exception] = field(default_factory=dict)
    # `tools.k8s_tags_at`: every tag of a k8s entry at a ref, by ref. A ref absent here keeps
    # an entry for every tag `owners` names at any ref, so a test retires a tag only by saying
    # so. An exception is raised in place of a set.
    k8s_tags: dict[str, set[str] | Exception] = field(default_factory=dict)
    # The host wait's answers (`_land_host_fakes.py`): the HostTargets for the merge commit, and
    # the (verdict, rc, detail) `wait_for_hosts` ends with, None being every target settling.
    host_targets: list[Any] = field(default_factory=list)
    host_wait_outcome: tuple[str, int, str] | None = None
    # What `tools.land_wait` returns, one entry per call.
    land_wait: list[WaitResult] = field(
        default_factory=lambda: [
            WaitResult(
                verdict="settled", cause="", detail="1 applied", revision="", widened=""
            )
        ]
    )


def _seq(values: list, calls: list, name: str):
    it = iter(values)
    last = values[-1]

    def answer(*args, **kwargs):
        nonlocal last
        calls.append((name, args, kwargs))
        try:
            last = next(it)
        except StopIteration:
            pass
        return last

    return answer


def build_classifier(f: Fakes, calls: list | None = None) -> Classifier:
    """The pure classifiers, each answering from `Fakes` instead of the real tree.

    `calls` is the list `build_tools` returned, so a classifier call lands in the same
    ordering record as a boundary call.
    """
    return Classifier(
        plane_note=lambda paths, quiet=(), narrow_tags=None: f.plane,
        derive=lambda files, changed_files, declared=None, quiet=(): Derivation(
            list(f.derived[0]), DeriveSource(f.derived[1])
        ),
        quiet_paths=lambda paths, range_: set(),
    )


def build_tools(f: Fakes) -> tuple[Tools, list]:
    calls: list = []
    views = {
        k: (list(v) if isinstance(v, list) else [v]) for k, v in f.gh_views.items()
    }
    views.setdefault("mergeCommit", [{"mergeCommit": {"oid": MERGE_SHA}}])
    views.setdefault("commits", [{"commits": []}])
    views.setdefault(
        "files,changedFiles",
        [
            {
                "files": [{"path": "ansible/roles/k8s/sonarr/defaults/main.yml"}],
                "changedFiles": 1,
            }
        ],
    )
    view_seq = {k: _seq(v, calls, f"gh:{k}") for k, v in views.items()}
    compare_seq = _seq(f.compare, calls, "gh:compare")
    runs_seq = _seq(f.master_runs, calls, "gh:master-runs")
    closing_refs_seq = _seq(f.closing_refs, calls, "gh:closing-refs")

    def gh_json(*args, **kwargs):
        if args[:2] == ("api", "graphql"):
            answer = closing_refs_seq(*args, **kwargs)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if args[:2] == ("api", "repos/{owner}/{repo}"):
            calls.append(("gh:repo", args, kwargs))
            return f.repo
        if args[0] == "api" and args[-1].endswith("/files"):
            calls.append(("gh:files", args, kwargs))
            return [f.pr_files]
        if args[0] == "api" and args[-1].endswith("/reviews"):
            calls.append(("gh:reviews", args, kwargs))
            return [f.pr_reviews]
        if args[0] == "api" and "/compare/" in args[1]:
            answer = compare_seq(*args, **kwargs)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if args[0] == "api" and "/workflows/ci.yml/runs?head_sha=" in args[1]:
            calls.append(("gh:head-runs", args, kwargs))
            if isinstance(f.head_runs, Exception):
                raise f.head_runs
            return f.head_runs
        if args[0] == "api" and "/workflows/ci.yml/runs?branch=" in args[1]:
            answer = runs_seq(*args, **kwargs)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return view_seq[args[args.index("--json") + 1]]()

    gh_rc = _seq(f.gh_merge_rc, calls, "gh")

    def gh_run(*args, **kwargs):
        rc = gh_rc(*args, **kwargs)
        if rc:
            raise subprocess.CalledProcessError(rc, args, stderr="boom")
        return _cp()

    def git_run(*args, cwd=None, check=True, **kwargs):
        calls.append(("git", args, {"cwd": cwd}))
        if args[0] == "fetch" and "refs/pull" in args[-1]:
            return _cp(f.pull_ref_rc)
        if args[0] == "fetch":
            return _cp(f.fetch_rc)
        if args == ("rev-parse", "FETCH_HEAD"):
            return _cp(0, "prhead\n")
        if args[0] == "ls-tree":
            tree = f.head_tree
            if tree is None:
                tree = {
                    e["filename"]: "100644"
                    for e in f.pr_files
                    if isinstance(e.get("filename"), str)
                    and e.get("status") != "removed"
                }
            rows = (f"{mode} blob {'0' * 40}\t{path}\0" for path, mode in tree.items())
            return _cp(f.ls_tree_rc, "".join(rows))
        if args[0] == "show":
            ref = args[1].split(":", 1)[0]
            return _cp(0 if ref in f.show_at else 128, f.show_at.get(ref, ""))
        if args[0] == "merge-base" and "--is-ancestor" in args:
            if args[-1] == "HEAD":
                return _cp(f.merge_applied_rc)
            return _cp(f.is_ancestor_of.get(args[-1], f.is_ancestor_rc))
        if args[0] == "merge-base":
            return _cp(0, "prbase\n")
        if args[0] == "rev-list":
            return _cp(0, "".join(f"{sha}\n" for sha in f.walk))
        if args == ("rev-parse", f"origin/{landing_mod.BRANCH}"):
            return _cp(0, f.tip + "\n")
        return _cp()

    blockers = _seq(f.blockers, [], "")

    def deploy_tags(primary: Path, args: list[str]):
        calls.append(("deploy_tags", tuple(args), {"cwd": primary}))
        if args[0] == "blockers":
            return _cp(blockers())
        if args[0] == "changed":
            return _cp(f.changed_rc, f.changed)
        raise AssertionError(args)

    def gate(tags, cwd=None):
        calls.append(("gate", (tags,), {"cwd": cwd}))
        return GateResult(*f.gate)

    @contextlib.contextmanager
    def snapshot(primary, sha):
        """The real one makes a worktree; this one only records that it was asked for.

        `f.gate_snapshot` is None for a snapshot that could not be taken, which is the
        degraded path the caller must still gate from.
        """
        calls.append(("snapshot", (primary, sha), {}))
        yield f.gate_snapshot

    await_ci_seq = _seq(f.await_ci, calls, "await_ci")

    def await_ci(sha, timeout):
        return CiVerdict(*await_ci_seq(sha, timeout))

    def own_narrowing(paths, pr_range, primary):
        calls.append(("own_narrowing", (pr_range,), {}))
        return f.own_narrowing

    def owners_at(primary, ref):
        calls.append(("owners_at", (ref,), {"cwd": primary}))
        answer = f.owners.get(ref, {})
        if isinstance(answer, Exception):
            raise answer
        return answer

    def k8s_tags_at(primary, ref):
        calls.append(("k8s_tags_at", (ref,), {"cwd": primary}))
        if ref not in f.k8s_tags:
            # Every `containers_list` entry is k8s, so whatever the landing derived is one.
            owned = {t for m in f.owners.values() if isinstance(m, dict) for t in m}
            return owned | set(f.derived[0]) | {"sonarr", "radarr"}
        answer = f.k8s_tags[ref]
        if isinstance(answer, Exception):
            raise answer
        return answer

    land_wait = _seq(f.land_wait, calls, "land_wait")

    def pins_at(checkout, sha):
        calls.append(("pins_at", (sha,), {}))
        return {key: Pin(PIN_FILE, value) for key, value in f.pins.get(sha, {}).items()}

    def answer(name, values):
        seq = values if isinstance(values, list) else [values]

        def read():
            calls.append((name, (), {}))
            value = seq[min(sum(c[0] == name for c in calls), len(seq)) - 1]
            if isinstance(value, Exception):
                raise value
            return value

        return read

    pins = PinReads(
        pins_at=pins_at,
        hold=answer("hold", f.deploy_hold),
        last_good=answer("last_good", f.last_good),
        run_gates=_seq(f.gates, calls, "run_gates"),
    )

    t = [0.0]

    def clock() -> float:
        t[0] += 1.0
        return t[0]

    tools = Tools(
        gh_json=gh_json,
        gh=gh_run,
        git=git_run,
        await_ci=await_ci,
        tested_base=lambda head: f.tested_base.get(head),
        deploy_tags=deploy_tags,
        gate=gate,
        snapshot=snapshot,
        declared_at=lambda ref, primary: f.declared_at,
        read_state=lambda root, name: f.state.get(name, ""),
        flux=FluxReads(owners_at, land_wait, pins, k8s_tags_at),
        own_narrowing=own_narrowing,
        **host_fakes(f, calls),
        hostname=lambda: f.hostname,
        logger=lambda line: calls.append(("logger", (line,), {})),
        sleep=lambda s: calls.append(("sleep", (s,), {})),
        clock=clock,
        wall_clock=lambda: 1_000_000.0,
    )
    return tools, calls


def make_landing(
    fakes: Fakes | None = None, **opts
) -> tuple[landing_mod.Landing, list]:
    """A Landing over fakes, for driving one phase directly."""
    f = fakes or Fakes()
    tools, calls = build_tools(f)
    o = Options(pr="999", primary=PRIMARY, deployer_state=STATE, **opts)
    return landing_mod.Landing(o, tools, build_classifier(f, calls)), calls
