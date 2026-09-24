#!/usr/bin/env python3
"""Run an interactive Ansible deploy under the locks the automated deployers take.

Invoke it as ``./scripts/deploy.sh``, which execs this file; every doc, skill, hook and
consumer names the shim. The port from bash is issue #2412, planned in
``docs/deploy-sh-python-port.md``. This module holds the FRONT half: argument parsing and
every gate that runs before the tree lock. The locked half -- tree lock, snapshot, service
locks, playbook, ``--detach`` -- is still ``deploy_locked.sh`` beside it, which this module
execs with the resolved arguments once every gate has passed.

Usage::

    deploy.sh --tags "<service>" [-e target=daniel-pi] [...]
    deploy.sh --changed [<ref>]          # derive --tags from the diff against <ref>
    deploy.sh --at <sha> --tags <svc>    # render a snapshot of <sha>, not HEAD
    deploy.sh --check | --dry-run ...    # unlocked, from the working tree
    deploy.sh --detach --tags <svc>      # background the playbook once the locks are held
    deploy.sh --list-services            # every valid --tags value

``--skip-tag-check`` deploys a tag this wrapper does not recognise, and
``--skip-staleness-check`` deploys from a tree behind origin/master. Every other argument
passes through to ansible-playbook in order.

Exit codes are ``scripts/deploy_tools/exit_codes.py``'s ``DEPLOY_*``. The ones this half
returns: 2 (a tag matched no service), 3 (``--changed`` found a broad change), 4 (the tree is
behind origin/master), 64 (arguments refused). Each means nothing was deployed.

WHICH CHECKOUT. The run deploys the checkout containing the CALLER's working directory, not
the one this file lives in, which is why the shim does not ``cd``. The helpers imported here
bind their own paths to this file's checkout, so every call that reads the tree is handed
``repo_root`` explicitly. In every real invocation (``land_lib``, ``deploy_ui``, the staging
runner, an operator typing ``./scripts/deploy.sh``) the two are the same checkout.
"""

import contextlib
import io
import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy_tools.exit_codes import (
    DEPLOY_BAD_FLAGS,
    DEPLOY_STALE,
    DEPLOY_TAG_MISS,
)
from lib.git import git
from lib.repo_paths import HOST_VARS, REPO

# The locked half, until slice 4 of #2412 ports it. This checkout's own copy, so the code that
# runs is always this checkout's release of both halves together.
DEPLOY_LOCKED = REPO / "scripts/deploy_tools/deploy_locked.sh"

# host_vars relative to a checkout root, for asking the CALLER's checkout rather than REPO.
HOST_VARS_REL = HOST_VARS.relative_to(REPO)


class Refused(Exception):
    """A gate refused the run; nothing was deployed. `code` is the exit status."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


@dataclass
class Plan:
    """Everything the argument passes resolved, for the gates and the hand-off."""

    repo_root: Path
    changed: bool = False
    changed_ref: str = "origin/master"
    at_given: bool = False
    at_ref: str = ""
    at_sha: str = ""
    skip_tag_check: bool = False
    skip_staleness_check: bool = False
    dry_run: bool = False
    detach: bool = False
    list_services: bool = False
    # The comma-split tags, in the order typed. Empty means a full run.
    tags: list[str] = field(default_factory=list)
    # What reaches ansible-playbook: every argument this wrapper does not consume.
    args: list[str] = field(default_factory=list)
    staleness_checked: bool = False

    @property
    def tags_csv(self) -> str:
        return ",".join(self.tags)

    @property
    def check(self) -> bool:
        return "--check" in self.args


def say(*lines: str) -> None:
    """Print a refusal or notice to stderr, one line per argument."""
    for line in lines:
        print(line, file=sys.stderr)


# -- the helpers, called in-process ------------------------------------------------------
#
# Each is a module-level function so a unit test can replace it. A helper that raises is
# reported the way the bash wrapper reported a helper that crashed: its traceback on stderr,
# then the refusal its non-zero exit mapped to.


def _call(fn, *args, **kwargs) -> int:
    try:
        return fn(*args, **kwargs)
    # A helper's own `sys.exit` or argparse error is its exit status, as it was in a
    # subprocess; it must not end this wrapper with the helper's number instead of the
    # refusal that number maps to.
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    # Broad on purpose: a crashed helper refuses, as a crashed subprocess did.
    except Exception:
        traceback.print_exc()
        return 1


def run_staleness(plan: Plan) -> int:
    """`deploy_staleness.main` against the caller's checkout; its exit status."""
    from deploy_tools import deploy_staleness

    argv = ["--repo", str(plan.repo_root)]
    if plan.at_sha:
        argv += ["--sha", plan.at_sha]
    if plan.tags:
        # The `=` form, so a tag that starts with `-` is a value and not a flag argparse
        # refuses -- which would read as a stale tree, not as the bad tag it is.
        argv.append(f"--tags={plan.tags_csv}")
    return _call(deploy_staleness.main, argv)


def run_validate(plan: Plan) -> int:
    """`deploy_tags.validate` against the caller's host_vars; its exit status."""
    from deploy_tools import deploy_tags

    return _call(
        deploy_tags.validate,
        plan.tags,
        at=plan.at_sha,
        host_vars=plan.repo_root / HOST_VARS_REL,
    )


def run_changed(plan: Plan) -> tuple[int, str]:
    """`deploy_tags.changed` against the caller's checkout: (status, derived --tags)."""
    from deploy_tools import deploy_tags

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status = _call(deploy_tags.changed, plan.changed_ref, cwd=plan.repo_root)
    return status, out.getvalue().strip()


def clear_fact_cache(plan: Plan) -> None:
    """`fact_cache_guard --clear` for the caller's checkout. Never raises."""
    # DECIDED: this preflight fails OPEN. It is a remediation, not a verdict -- if it cannot
    # clear the cache, the deploy proceeds and dies at Gathering Facts exactly as it does
    # today, except now with this script's stderr naming the cache directly above the
    # misleading module error. Blocking every deploy on a bug in a cache-cleaner would be a
    # worse failure than the one it prevents.
    try:
        from deploy_tools import fact_cache_guard

        fact_cache_guard.main(["--clear", "--repo-root", str(plan.repo_root)])
    # Broad on purpose, SystemExit included: the preflight fails open, per the DECIDED note.
    except Exception, SystemExit:
        traceback.print_exc()


def exec_argv(argv: list[str]) -> None:
    """Replace this process with `argv`. Flushes first: exec discards Python's buffers."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(argv[0], argv)


@dataclass(frozen=True)
class Tools:
    """The boundaries a run crosses: the four helpers and the final exec.

    A test passes its own to inject the helpers' verdicts and observe the order the gates
    ask in, as `land_lib/tools.py` does for a landing.
    """

    staleness: Callable[[Plan], int] = run_staleness
    validate: Callable[[Plan], int] = run_validate
    changed: Callable[[Plan], tuple[int, str]] = run_changed
    clear_fact_cache: Callable[[Plan], None] = clear_fact_cache
    exec_argv: Callable[[list[str]], None] = exec_argv


REAL_TOOLS = Tools()


# -- the argument passes -----------------------------------------------------------------


def resolve_at_and_changed(raw: list[str], plan: Plan) -> list[str]:
    """Strip `--changed [<ref>]` and `--at <sha>` from `raw`; returns what is left.

    Its own pass because both take a separate argument, and `--changed`'s is OPTIONAL: the
    main pass cannot tell "no ref given" from "the next flag" without look-ahead.
    `--skip-staleness-check` is noted here and left in place, because the `--changed`
    derivation below asks the staleness gate before the main pass has run.
    """
    left: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        a = raw[i]
        if a == "--changed":
            plan.changed = True
            i += 1
            if i < n and not raw[i].startswith("-"):
                plan.changed_ref = raw[i]
                i += 1
            continue
        if a == "--at":
            plan.at_given = True
            i += 1
            # A missing value, and a next argument that is itself a flag, both leave at_ref
            # empty and are refused below. Reading past the end used to leave it empty
            # SILENTLY, and `--at "$sha"` with an unset variable deployed the checkout's tip.
            if i < n and not raw[i].startswith("-"):
                plan.at_ref = raw[i]
                i += 1
            continue
        if a.startswith("--at="):
            # Accepted because --tags= is: otherwise `--at=abc` would reach ansible-playbook
            # as an unknown flag.
            plan.at_given = True
            plan.at_ref = a[len("--at=") :]
            i += 1
            continue
        if a == "--skip-staleness-check":
            plan.skip_staleness_check = True
        left.append(a)
        i += 1
    return left


def check_at(plan: Plan) -> None:
    """Resolve `--at` to one full SHA, or refuse with 64.

    Before any helper runs: an argument error must not cost a subprocess, and everything after
    uses the ONE SHA this produces rather than re-resolving a ref another session can move.
    64 rather than 2, because `land_lib/deploy.py` reads 2 as "a derived tag matched no
    service", the wrong sentence for a committish that did not resolve.
    """
    if not plan.at_given:
        return
    if not plan.at_ref:
        say(
            "deploy: --at needs a commit -- nothing was deployed.",
            "  It was given none (or an empty one), and a run that asked for another",
            "  commit must not fall back to deploying this checkout's HEAD in silence.",
        )
        raise Refused(DEPLOY_BAD_FLAGS)
    if plan.changed:
        say(
            "deploy: --at <sha> with --changed is contradictory -- nothing was deployed.",
            "  --changed derives its tags from the WORKING TREE's diff, while --at renders",
            "  a snapshot of another commit, so the derived tags would describe a tree this",
            "  run never deploys. Pass --tags explicitly with --at.",
        )
        raise Refused(DEPLOY_BAD_FLAGS)
    resolved = git(
        "rev-parse",
        "--verify",
        "--quiet",
        f"{plan.at_ref}^{{commit}}",
        cwd=plan.repo_root,
        check=False,
    )
    if resolved.returncode != 0:
        say(
            f"deploy: --at '{plan.at_ref}' does not resolve to a commit here -- nothing was",
            "  deployed. The snapshot is cut from this checkout's object store, so the",
            "  commit has to be in it: 'git fetch origin' first if it was merged elsewhere.",
        )
        raise Refused(DEPLOY_BAD_FLAGS)
    plan.at_sha = resolved.stdout.strip()


def staleness_gate(plan: Plan, tools: Tools) -> None:
    """Refuse with 4 when the rendered commit is behind origin/master. Asked once per run.

    `--changed` reaches this before any tag is derived and so asks the unscoped question,
    which is right there: the derivation reads the same stale tree. With tags, the gate asks
    the narrower question -- a commit reaching none of them and no broad path cannot revert
    what this deploy renders, and the GitOps deployer fast-forwards to the newest GREEN
    commit, so the primary checkout is legitimately behind a pending tip.
    """
    if plan.staleness_checked:
        return
    plan.staleness_checked = True
    if tools.staleness(plan) != 0:
        raise Refused(DEPLOY_STALE)


def derive_changed_tags(plan: Plan, args: list[str], tools: Tools) -> list[str]:
    """Resolve `--changed` into `--tags <derived>`, or end the run.

    The staleness gate runs FIRST (issue #1593). On a checkout that is only behind, the
    three-dot range the derivation reads is empty by construction, so it derives no tags and
    the run would exit 0 having deployed nothing -- the one code no consumer treats as a
    resume point. Exit 4 is the honest answer, and land.sh already retries it.
    """
    if not plan.changed:
        return args
    if not plan.skip_staleness_check:
        staleness_gate(plan, tools)
    status, derived = tools.changed(plan)
    if status != 0:
        raise Refused(status)
    if not derived:
        raise Refused(0)
    return [*args, "--tags", derived]


def parse_wrapper_flags(args: list[str], plan: Plan) -> None:
    """Consume the wrapper's own flags into `plan`; everything else goes to `plan.args`.

    `--list-services` ends the pass at once, before any flag after it is read, as it did in
    bash, where the loop exec'd on reaching it.
    """
    raw_tags: list[str] = []
    next_is_tags = False
    for arg in args:
        if next_is_tags:
            next_is_tags = False
            raw_tags.append(arg)
            plan.args.append(arg)
            continue
        if arg == "--skip-tag-check":
            plan.skip_tag_check = True
        elif arg == "--skip-staleness-check":
            plan.skip_staleness_check = True
        elif arg == "--detach":
            plan.detach = True
        elif arg == "--dry-run":
            # Translated, not passed through: ansible-playbook has no --dry-run of its own
            # (--check is the Ansible-level one, and it is a different mode entirely).
            plan.dry_run = True
            plan.args += ["-e", "k8s_dry_run=true"]
        elif arg == "--list-services":
            plan.list_services = True
            return
        elif arg in ("--tags", "-t"):
            next_is_tags = True
            plan.args.append(arg)
        elif arg.startswith("--tags="):
            raw_tags.append(arg[len("--tags=") :])
            plan.args.append(arg)
        else:
            plan.args.append(arg)
    # Ansible accepts comma-separated tags in one argument, so split each into the single
    # tags the staleness gate and the tag validation both ask about.
    plan.tags = [t for raw in raw_tags for t in raw.split(",") if t]


def check_mode_conflicts(plan: Plan) -> None:
    """Refuse `--detach` with `--check`/`--dry-run`, and say what `--at` means for them.

    Checked before the (comparatively slow) staleness check, so a nonsensical combination
    fails fast. `--changed` is the one path where staleness already ran, so there a stale
    tree is reported first, at exit 4.
    """
    if plan.detach and (plan.check or plan.dry_run):
        say(
            "deploy: --detach with --check or --dry-run is meaningless -- both already return",
            "  immediately without touching the lock, so there is nothing to background.",
        )
        raise Refused(DEPLOY_BAD_FLAGS)
    # Both modes render the WORKING TREE, so --at scopes the gates and nothing else. Said out
    # loud rather than refused: rehearsing a landing's `--at <sha>` with --check is a
    # reasonable thing to type, and rendering a different tree in silence is the surprise.
    if plan.at_sha and (plan.check or plan.dry_run):
        say(
            f"deploy: --check/--dry-run render THIS working tree; --at {plan.at_sha[:12]} scoped the",
            "  staleness and tag checks to that commit and nothing else.",
        )


def validate_tags(plan: Plan, tools: Tools) -> None:
    """Refuse with 2 when a tag names no service. Runs AFTER staleness (issue #1566).

    A tag check against a stale tree answers about the wrong tree: the first landing of a NEW
    role reads as a tag miss whenever the tick has not fast-forwarded the merge commit yet,
    where exit 4 names the real cause and land.sh retries it. Ansible itself exits 0 on a tag
    that matches nothing, so without this a typo deploys nothing and reports success. Under
    `--at` the list is validated against `containers_list` at that commit.
    """
    if plan.skip_tag_check or not plan.tags:
        return
    if tools.validate(plan) != 0:
        raise Refused(DEPLOY_TAG_MISS)


def exec_target(plan: Plan) -> list[str]:
    """The command a run that passed every gate becomes.

    `--check` and `--dry-run` run ansible-playbook unlocked, from the WORKING TREE: a dry run
    renders to a temp dir and applies with --dry-run=server, so it writes neither the cluster
    nor the staging tree, and there is nothing for a lock to serialize. Every other run is
    handed to the locked half.
    """
    if plan.check or plan.dry_run:
        return ["uv", "run", "ansible-playbook", "ansible/deploy.yml", *plan.args]
    return [
        str(DEPLOY_LOCKED),
        plan.at_sha,
        "1" if plan.detach else "0",
        plan.tags_csv,
        "--",
        *plan.args,
    ]


def prepare_stdio() -> None:
    """Clear O_NONBLOCK on 0/1/2 so the ansible-playbook this run starts can use them.

    Ansible refuses to start on a non-blocking stdout or stderr ("Ansible requires blocking IO
    on stdin/stdout/stderr"), and Claude Code's Bash tool hands its child a file with
    O_NONBLOCK set (#834). The flag lives on the open file description that exec and fork
    share, so clearing it here clears it for every process this run starts.
    """
    for fd in (0, 1, 2):
        with contextlib.suppress(OSError):
            os.set_blocking(fd, True)


def run(argv: list[str], tools: Tools = REAL_TOOLS) -> int:
    """Resolve and gate one invocation; exec the locked half, or return a refusal's code."""
    top = git("rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0:
        sys.stderr.write(top.stderr)
        return 1
    plan = Plan(repo_root=Path(top.stdout.strip()))
    os.chdir(plan.repo_root)
    try:
        args = resolve_at_and_changed(argv, plan)
        check_at(plan)
        args = derive_changed_tags(plan, args, tools)
        parse_wrapper_flags(args, plan)
        if plan.list_services:
            tools.exec_argv(
                ["uv", "run", "python", "scripts/deploy_tools/deploy_tags.py", "list"]
            )
        check_mode_conflicts(plan)
        # The fact cache is shared by host across every worktree on this machine and pins
        # the interpreter of whichever session gathered facts first. A cache naming a gone
        # worktree fails EVERY deploy at Gathering Facts for the full TTL, AFTER the lock
        # wait. Runs before --check and --dry-run too: a dry run gathers facts like any
        # other, which is how the cache gets re-poisoned in the first place.
        tools.clear_fact_cache(plan)
        # A tree behind origin/master renders stale templates and reverts live config while
        # every repo-side check reads green. Before --check and --dry-run too: a green dry run
        # against a stale tree is the misleading signal itself.
        if not plan.skip_staleness_check:
            staleness_gate(plan, tools)
        validate_tags(plan, tools)
    except Refused as refused:
        return refused.code
    tools.exec_argv(exec_target(plan))
    return 1  # the exec does not return; reached only when a test replaces it


def main() -> int:
    prepare_stdio()
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
