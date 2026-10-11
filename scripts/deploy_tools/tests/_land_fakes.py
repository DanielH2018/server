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
import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from deploy_tools.deploy_detach_notify import GateResult
from lib.exit_codes import DEPLOY_BROAD
from deploy_tools.land_lib import landing as landing_mod
from deploy_tools.land_lib.options import Options
from deploy_tools.land_lib.tools import Classifier, CiVerdict, Tools
from deploy_tools.land_lib.land_tags import Derivation, DeriveSource
from gitops_markers import MARKERS

MERGE_SHA = "0123456789abcdef0123456789abcdef01234567"
# The receipt marker's basename on disk, which is the name `read_state` is asked for.
RECEIPTS = MARKERS["receipts"]
# The base of the range a `receipt()` covers. `Fakes.is_ancestor_of` answers non-zero for it
# by default, so the merge commit reads as inside `base..origin` unless a test says otherwise.
RECEIPT_BASE = "b" * 40


def receipt(applied: dict[str, list[str]], manual=None, origin: str = "f" * 40) -> dict:
    """A `state` holding the receipt of the tick that crossed the merge commit.

    Args:
        applied: playbook -> the tags the tick applied it with, `[]` for the whole play.
        manual: setup role tag -> its narrowest tags, for each role left to a hand.
        origin: the SHA the tick crossed to; `Fakes.is_ancestor_rc` decides whether it
            contains the merge commit.
    """
    line = {"origin": origin, "base": RECEIPT_BASE, "applied": applied}
    return {RECEIPTS: json.dumps({**line, "manual": manual or {}})}


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
    tick: list[int] = field(default_factory=lambda: [0])
    deploy: list[int] = field(default_factory=lambda: [0])
    blockers: list[int] = field(default_factory=lambda: [0])
    hosts: str = ""
    hosts_rc: int = 0
    # What `containers_list` routes each tag to at the merge commit (`tools.landing_hosts_at`).
    # None is the read having failed, so a landing falls back to `deploy_tags.py hosts`
    # against the primary -- the `hosts` string above.
    hosts_at: dict[str, list[str]] | None = None
    changed: str = ""
    changed_rc: int = 0
    # `deploy_tags.py narrow`, the second derivation a broad `changed` refusal falls back to.
    # It refuses by default, which is what every range written before it did.
    narrowed: str = ""
    narrowed_rc: int = DEPLOY_BROAD
    # `git diff --name-only <since>...HEAD`: the paths the fallback derivation proves a tag's
    # platform from, and the return code of the read.
    diff_paths: list[str] = field(default_factory=list)
    diff_rc: int = 0
    gate: tuple[bool, list[str]] = field(
        default_factory=lambda: (True, ["sonarr: healthy"])
    )
    # What the gate returns from its second call onward -- the re-gate after a later deploy.
    # None repeats `gate`.
    regate: tuple[bool, list[str]] | None = None
    # `tools.later_deploys`: each tag another deploy re-rolled after this landing's own, to
    # the commit that deploy rendered.
    later_deploys: dict[str, str] = field(default_factory=dict)
    # What `tools.snapshot` yields: a directory for a snapshot that was taken, None for one
    # that could not be (the tree lock busy, the worktree add failed).
    gate_snapshot: Path | None = Path("/snap")
    plane: str = ""
    self_applied: bool = False
    self_applied_command: str = "`ansible-playbook ansible/initial_setup.yml --tags x`"
    remaining_setup: str = ""
    derived: tuple[list[str], str] = field(default_factory=lambda: (["sonarr"], "pr"))
    # `land_tags.shared_caller_tags`: each shared role the PR changes, to the tags that run it.
    shared_callers: dict[str, set[str]] = field(default_factory=dict)
    # `land_platform.k8s_only_tags`: the derived tags whose every changed path sits under the
    # k8s role tree, so the landing may route them to a `platform: k8s` entry alone.
    path_k8s_only: list[str] = field(default_factory=list)
    # What `containers_list` declares at the merge commit. None is the read having failed,
    # which is what every land_lib reader falls back to its own tree on.
    declared_at: set[str] | None = None
    # `git merge-base --is-ancestor <merge_sha> <recorded apply>`: 0 means the recorded broad
    # apply included this PR, non-zero means it ran at a commit that did not contain it.
    is_ancestor_rc: int = 0
    # Per-ref overrides of `is_ancestor_rc`, keyed by the ref the query asks about (the last
    # argument), for a test that needs one SHA's ancestry to differ from another's.
    is_ancestor_of: dict[str, int] = field(default_factory=lambda: {RECEIPT_BASE: 1})
    # The same query against HEAD — `Landing.merge_applied`, which asks whether the primary
    # checkout already carries this PR. Non-zero by default so `behind_since` alone still
    # answers BEHIND unless a test says the tick crossed the merge commit.
    merge_applied_rc: int = 1
    # A marker's value; None is a read that failed, which `Landing.state` passes on as None.
    # Mapping, not dict: a dict is invariant in its values, so the many `dict[str, str]`
    # callers would no longer type-check.
    state: Mapping[str, str | None] = field(default_factory=dict)
    # What `tools.own_narrowing` answers on the fast path: role tag -> this PR's derivation.
    own_narrowing: dict[str, frozenset[str]] = field(default_factory=dict)
    # The paths `tools.paths_a_hand_must_apply` drops: a shared role's change that moves no
    # rendered manifest. None leaves the list as the PR's own.
    plane_paths_dropped: frozenset[str] = frozenset()
    lock_holder: list[str] = field(default_factory=lambda: ["42 flock deploy"])
    hostname: str = "daniel-box"
    # What `gh api repos/{owner}/{repo}` answers: the visibility --arm-merge refuses on.
    repo: dict[str, Any] = field(default_factory=lambda: {"visibility": "public"})
    # What the REST `pulls/<n>/files` listing answers, as one page: the landing policy's input.
    pr_files: list[dict[str, Any]] = field(
        default_factory=lambda: [{"filename": "docs/landing.md"}]
    )
    # What the REST `pulls/<n>/reviews` listing answers, as one page.
    pr_reviews: list[dict[str, Any]] = field(default_factory=list)
    # What `gh api compare/<head>...master` answers, one entry per call: the paths master
    # changed since the PR's merge base. Empty is a PR already up to date with master, and an
    # exception is raised in place of an answer.
    compare: list[list[str] | Exception] = field(default_factory=lambda: [[]])


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
    record = calls if calls is not None else []

    def remaining_setup_hosts(
        paths, local_host, quiet=(), pr_range="", ref="", repo=None
    ):
        # local_host is recorded: the phase must pass `tools.hostname()`, not a constant.
        # ref and repo are recorded: without them the note reads this checkout's own tree,
        # the pre-merge-checkout bug #4080 fixed.
        record.append(
            ("remaining_setup_hosts", (local_host,), {"ref": ref, "repo": repo})
        )
        return f.remaining_setup

    def k8s_only_tags(paths, declared=None):
        # The paths are recorded: the fallback derivation must hand the DIFF's paths rather
        # than the PR's file list, which `gh` truncated.
        record.append(("k8s_only_tags", tuple(paths), {}))
        return list(f.path_k8s_only)

    return Classifier(
        plane_note=lambda paths, declared=None, quiet=(), narrow_tags=None: f.plane,
        self_applied=lambda paths, quiet=(): f.self_applied,
        self_applied_command=lambda paths, quiet=(): f.self_applied_command,
        remaining_setup_hosts=remaining_setup_hosts,
        derive=lambda paths, changed, declared=None: Derivation(
            list(f.derived[0]), DeriveSource(f.derived[1])
        ),
        quiet_paths=lambda paths, range_: set(),
        shared_caller_tags=lambda paths, declared=None: f.shared_callers,
        k8s_only_tags=k8s_only_tags,
    )


def build_tools(f: Fakes) -> tuple[Tools, list]:
    calls: list = []
    views = {
        k: (list(v) if isinstance(v, list) else [v]) for k, v in f.gh_views.items()
    }
    views.setdefault("mergeCommit", [{"mergeCommit": {"oid": MERGE_SHA}}])
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

    def gh_json(*args, **kwargs):
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
        if args[0] == "diff":
            return _cp(f.diff_rc, "\n".join(f.diff_paths) + "\n")
        if args[0] == "merge-base" and "--is-ancestor" in args:
            if args[-1] == "HEAD":
                return _cp(f.merge_applied_rc)
            return _cp(f.is_ancestor_of.get(args[-1], f.is_ancestor_rc))
        if args[0] == "merge-base":
            return _cp(0, "prbase\n")
        if args == ("rev-parse", f"origin/{landing_mod.BRANCH}"):
            return _cp(0, f.tip + "\n")
        return _cp()

    blockers = _seq(f.blockers, [], "")

    def deploy_tags(primary: Path, args: list[str]):
        calls.append(("deploy_tags", tuple(args), {"cwd": primary}))
        if args[0] == "blockers":
            return _cp(blockers())
        if args[0] == "hosts":
            return _cp(f.hosts_rc, f.hosts)
        if args[0] == "changed":
            return _cp(f.changed_rc, f.changed)
        if args[0] == "narrow":
            return _cp(f.narrowed_rc, f.narrowed)
        raise AssertionError(args)

    def landing_hosts_at(tags, ref, primary, k8s_only=()):
        calls.append(
            (
                "landing_hosts_at",
                (list(tags), ref),
                {"cwd": primary, "k8s_only": list(k8s_only)},
            )
        )
        return f.hosts_at

    def gate(tags, cwd=None):
        again = any(c[0] == "gate" for c in calls)
        calls.append(("gate", (tags,), {"cwd": cwd}))
        return GateResult(*(f.regate if again and f.regate else f.gate))

    def later_deploys(tags, since):
        calls.append(("later_deploys", (list(tags), since), {}))
        return f.later_deploys

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

    t = [0.0]

    def clock() -> float:
        t[0] += 1.0
        return t[0]

    tools = Tools(
        gh_json=gh_json,
        gh=gh_run,
        git=git_run,
        await_ci=await_ci,
        tick=_seq(f.tick, calls, "tick"),
        deploy=_seq(f.deploy, calls, "deploy"),
        deploy_tags=deploy_tags,
        gate=gate,
        snapshot=snapshot,
        later_deploys=later_deploys,
        declared_at=lambda ref, primary: f.declared_at,
        landing_hosts_at=landing_hosts_at,
        read_state=lambda root, name: f.state.get(name, ""),
        own_narrowing=own_narrowing,
        paths_a_hand_must_apply=lambda paths, pr_range, primary, declared: [
            p for p in paths if p not in f.plane_paths_dropped
        ],
        lock_holder=_seq(f.lock_holder, calls, "lock_holder"),
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
