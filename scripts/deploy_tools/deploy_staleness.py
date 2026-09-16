#!/usr/bin/env python3
"""Refuse a deploy from a git tree that is behind origin/master.

THE PROBLEM. scripts/deploy.sh renders Ansible templates from whatever git tree it runs
in. A worktree behind master therefore deploys *stale* templates, reverting live config
for exactly the roles it targets — and every repo-side check still reads green, because
the stale tree is internally self-consistent. Tests pass, prek passes, `--dry-run`
validates: they all measure the old tree against itself. Nothing in the deploy path
compares HEAD to origin/master.

Measured 2026-08-19. A worktree 48 commits behind master ran `--tags claude-otel`. That
reverted the role for ~9 minutes: Prometheus lost the endpoint-based scrape discovery
that reaches both longhorn-manager pods (falling back to a single Service target), and
telemetry-health.sh went back to its branch-point version. It surfaced only because a
live scrape-target count moved for no reason the diff explained.

WHY ONLY "BEHIND". Being *ahead* of master is normal and expected — every slice deploy
runs from a worktree carrying unmerged commits, which is the whole point of the workflow.
Only the behind direction can revert live config, so only it refuses. The 2026-08-19 tree
was both (48 behind, 15 ahead), so being ahead must never mask being behind.

WHICH BEHIND-NESS REFUSES. A tree behind on a role this deploy does not render cannot
revert that role's live config: the refusal is about what the tags render, not about the
commit count. `--tags` therefore narrows the question to the paths in HEAD..<ref> that reach
one of those tags, a broad plane, the SOPS secrets file, or a shared k8s role every k8s
deploy runs — `refusing_paths` carries each rule and why it is not about the tags. With no
tags, with a tag that names a BLOCK of tasks rather than a service (`config`, `deploy`,
`cron`, `always`), or with host_vars unreadable, the deploy is unscoped and any tail refuses,
which is the rule this guard has always had. The narrowing matters because the GitOps
deployer fast-forwards to the newest GREEN commit in its range rather than to the tip, so the
primary checkout is legitimately behind a pending tip while every landing deploys from it.

WHICH COMMIT IS ASKED ABOUT. HEAD, unless `--sha` names another one. `deploy.sh --at <sha>`
renders a snapshot of `<sha>` rather than of its own working tree, so `<sha>` is what can be
behind and the checkout the run was launched from is irrelevant — a landing deploys the PR's
merge commit from a primary checkout the tick has not fast-forwarded yet, and asking about
HEAD there refuses a deploy of a commit that is not behind at all.

NOT THE AUTOMATED PIPELINE. gitops_deploy.py invokes ansible-playbook directly
(roles/setup/gitops_deploy/files/gitops_deploy.py:572), not this wrapper, and it pulls
before deploying. This guard covers the interactive and agent path, where the failure was.

Used by scripts/deploy.sh, which maps a non-zero exit here to its own refusal.
"""

import argparse
import subprocess
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.deployer_park import (
    GITOPS_STATE_DIR,
    park_note,
    read_behind_marker,
)
from lib.git import git
from lib.repo_paths import GITOPS_DEPLOY_FILES

# The deployer's own path→service mapper, reached the way deploy_tags.py reaches it: the role's
# files/ is on no path by default, and a copy of the mapping here would drift from the one the
# tick decides with. The path entry is constant and free; the IMPORT is lazy, so `--help` and
# every un-tagged run pay nothing for it.
sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

# Distinct from deploy.sh's other refusals: 2 = tag matched nothing, 3 = broad --changed,
# 75 = lock busy.
STALE_EXIT = 4

FETCH_TIMEOUT_S = 20


def _git(
    repo: str, *args: str, timeout: float | None = None
) -> subprocess.CompletedProcess:
    """Run git in `repo`, never raising on a non-zero exit.

    `git -C` does not override GIT_DIR, so an inherited one (a pre-commit hook, a
    rebase in progress) would silently redirect these reads at another repository;
    lib.git strips it.
    """
    return git(*args, cwd=repo, check=False, timeout=timeout)


def behind_ahead(
    repo, ref: str = "origin/master", base: str = "HEAD"
) -> tuple[int, int]:
    """Return (behind, ahead) for `base` relative to `ref`.

    `base` is HEAD for a deploy that renders the working tree, and the commit named by
    `deploy.sh --at` for one that renders a snapshot of something else: the question is
    always about the bytes being deployed, and with `--at` those are not HEAD's.

    Raises LookupError if `ref` or `base` cannot be resolved.
    """
    proc = _git(str(repo), "rev-list", "--left-right", "--count", f"{ref}...{base}")
    if proc.returncode != 0:
        raise LookupError(f"cannot resolve {ref}...{base}")
    left, right = proc.stdout.split()
    return int(left), int(right)


def what_is_behind(base: str) -> str:
    """How the refusals name the thing that is behind: the tree, or the named commit."""
    return "this tree is" if base == "HEAD" else f"{base[:12]} is"


def format_refusal(behind: int, ahead: int, ref: str, base: str = "HEAD") -> str:
    """The stderr message printed when `base` is behind `ref`, with the fixes to try."""
    ahead_note = f" (and {ahead} ahead)" if ahead else ""
    return (
        f"deploy: {what_is_behind(base)} {behind} commit(s) behind {ref}{ahead_note} "
        f"-- nothing was deployed.\n"
        f"  Deploying now would render stale templates and revert live config for the\n"
        f"  roles you target, while every repo-side check still reads green.\n"
        f"  Fix: git fetch origin && git rebase {ref}\n"
        f"  Deliberately deploying an older tree? Re-run with --skip-staleness-check."
    )


def incoming(repo: str, ref: str, *args: str, base: str = "HEAD") -> list[str] | None:
    """One `git log`/`git diff` read over <base>..<ref> as lines, or None when it failed.

    Two dots and this direction: the question is what the deployed commit has yet to receive,
    not what it has changed. None is distinct from an empty list on purpose — a range that
    could not be read is not a range shown to be unrelated, and the caller refuses on it.
    """
    proc = _git(repo, *args, f"{base}..{ref}")
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line.strip()]


def service_tags_or_none() -> set[str] | None:
    """Every tag that names a service in `containers_list`, or None when they can't be read.

    Lazy for the reason `refusing_paths`'s import is: this parses every host_vars file, and
    `--help` and every un-tagged run must not pay for it.
    """
    try:
        from deploy_tags import service_tags

        return service_tags()
    except Exception:
        return None


def unscoped_reason(tags: set[str], declared: set[str] | None) -> str:
    """Why these `--tags` cannot narrow the question, or "" when they can.

    A tag that names no service names a BLOCK of tasks instead — `config`, `deploy`, `cron`,
    `always` — and those run across every role the play selects, so no path list bounds what
    such a run renders. The unscoped rule (any commit behind refuses) is the only correct
    answer there. Unreadable host_vars is the same answer for the same reason: a narrowing
    that cannot name the services is not a narrowing.
    """
    if declared is None:
        return "the declared service tags could not be read"
    unknown = sorted(tags - declared)
    if unknown:
        return f"{', '.join(unknown)} names no single service"
    return ""


def refusing_paths(
    paths: list[str], tags: set[str], repo: str, declared: set[str]
) -> list[tuple[str, str]]:
    """The incoming paths this deploy must not be behind on, each with the reason, in order.

    Each path is classified on its own so the refusal can name the ones responsible; the
    verdict is the same as classifying them together, because the ChangeSet fields this reads
    are unions over the path list. `cs.broad` covers the manual planes too (a bring-up
    playbook sets both), so no separate check is needed for those.

    Three of the four rules refuse on something OTHER than the requested tags, because the
    narrowing is only sound where a path's reach can be named:

    - A broad plane renders for every role the play selects, so no tag set bounds it.
    - `ansible/vars/secrets.yml` is read by every template that takes a SOPS value, and
      `services_from_changed_paths` maps it to `ChangeSet.secrets` and NO service. Deploying
      any service from a tree behind a rotation commit renders the old credential and pushes
      it live — the reversion this guard exists to refuse, arriving through a field the
      narrowing does not consult (issue #1785).
    - An `ansible/roles/k8s/<role>` with no `containers_list` entry is a SHARED role,
      included by literal name from other roles rather than selected by a tag — which is the
      derivation this rule uses, rather than a list that would go stale as roles are added.
      Every k8s deploy runs `k8s/manifests`, so being behind on one is being behind on
      whatever this deploy renders, whichever service it names.

    Build couplings widen the fourth on purpose: a build role in the tail renders the image
    its coupled workload runs, so deploying that workload from a tree missing the build role's
    commit is exactly the same reversion.

    Args:
        paths: the incoming paths, as `git diff --name-only HEAD..<ref>` gives them.
        tags: the service tags this deploy targets.
        repo: the checkout to read shared-module callers from.
        declared: every tag naming a `containers_list` entry, which is how a shared k8s role
            is told from a service one.
    """
    from deploy_logic import (
        expand_build_couplings,
        services_from_changed_paths,
        shared_module_consumers,
    )

    flagged = []
    for path in paths:
        cs = services_from_changed_paths([path])
        if cs.broad:
            flagged.append((path, "a broad plane"))
            continue
        if cs.secrets:
            flagged.append((path, "a SOPS value every template can render"))
            continue
        shared = sorted(cs.k8s - declared)
        if shared:
            flagged.append(
                (path, f"the shared k8s role {shared[0]}, which every k8s deploy runs")
            )
            continue
        reached = expand_build_couplings(
            cs.services
            | cs.k8s
            | cs.k8s_deploy
            | cs.tasks
            | cs.meta
            | shared_module_consumers([path], repo)
        )
        hit = sorted(reached & tags)
        if hit:
            flagged.append((path, ", ".join(hit)))
    return flagged


def format_tag_refusal(
    behind: int,
    ref: str,
    tags: list[str],
    paths: list[tuple[str, str]],
    commits: list[str],
    base: str = "HEAD",
) -> str:
    """The stderr message for a tree behind on something this deploy DOES render.

    Each path carries the reason it was flagged, because three of the four rules refuse on
    something other than the requested tags — an operator reading `ansible/vars/secrets.yml`
    in a list headed "reach sonarr" would read it as a bug in the guard.
    """
    listed = "\n".join(f"    {p}  ({reason})" for p, reason in paths[:10])
    if len(paths) > 10:
        listed += f"\n    +{len(paths) - 10} more"
    log = "\n".join(f"    {c}" for c in commits[:10])
    if len(commits) > 10:
        log += f"\n    +{len(commits) - 10} more"
    return (
        f"deploy: {what_is_behind(base)} {behind} commit(s) behind {ref}, and {len(paths)} "
        f"of the path(s) in that range reach what a deploy of {', '.join(tags)} renders "
        f"-- nothing was deployed.\n"
        f"  Deploying now would render stale templates and revert live config for those\n"
        f"  roles, while every repo-side check still reads green.\n"
        f"  Commits:\n{log}\n"
        f"  Paths:\n{listed}\n"
        f"  Fix: git fetch origin && git rebase {ref}\n"
        f"  Deliberately deploying an older tree? Re-run with --skip-staleness-check."
    )


def main(argv: list[str] | None = None) -> int:
    """Fetch origin, compare `--sha` (default HEAD) to `--ref`; STALE_EXIT (4) when behind."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".")
    parser.add_argument("--ref", default="origin/master")
    parser.add_argument(
        "--state-dir",
        default=GITOPS_STATE_DIR,
        help="the GitOps deployer's marker directory (a test points this at a tmp_path)",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="skip refreshing the remote ref (tests, and offline runs)",
    )
    parser.add_argument(
        "--sha",
        default="",
        help=(
            "the commit this deploy renders, when it is not HEAD (`deploy.sh --at`). The "
            "range asked about becomes <sha>..<ref> rather than HEAD..<ref>, because a "
            "deploy from a snapshot of <sha> is behind on exactly what <sha> has yet to "
            "receive — the working tree it was launched from renders nothing."
        ),
    )
    parser.add_argument(
        "--tags",
        action="append",
        default=[],
        help=(
            "the service tags this deploy targets, comma-separated and repeatable. Given "
            "them, only a commit reaching one of those tags (or a broad path) refuses; with "
            "none the deploy is unscoped and any commit behind refuses."
        ),
    )
    args = parser.parse_args(argv)
    tags = {t.strip() for arg in args.tags for t in arg.split(",") if t.strip()}
    base = args.sha or "HEAD"

    if not args.no_fetch:
        # Best-effort. Offline is not a reason to block a deploy, but comparing against a
        # stale origin/master has the same blind spot this guard exists to close, so say
        # which ref the answer came from.
        try:
            failed = _git(
                args.repo, "fetch", "--quiet", "origin", timeout=FETCH_TIMEOUT_S
            ).returncode
        except subprocess.TimeoutExpired:
            failed = True
        if failed:
            print(
                f"deploy: could not fetch origin; comparing against the local "
                f"{args.ref}, which may itself be stale.",
                file=sys.stderr,
            )

    try:
        behind, ahead = behind_ahead(args.repo, args.ref, base)
    except LookupError:
        # No such ref: a fresh init, a detached CI checkout, a fork with another remote.
        # This is a staleness check, not a git-topology check — do not block the deploy.
        return 0

    declared = service_tags_or_none() if behind and tags else None
    blocked = unscoped_reason(tags, declared) if behind and tags else ""
    if blocked:
        print(
            f"deploy: not narrowing the staleness check -- {blocked}.", file=sys.stderr
        )
    paths = (
        incoming(args.repo, args.ref, "diff", "--name-only", base=base)
        if behind and tags and not blocked
        else None
    )
    # `paths is None` means the range could not be read or these tags cannot narrow it, so it
    # falls through to the unscoped refusal below rather than to the narrow one — a range
    # nothing could classify is not a range shown to touch nothing this deploy renders.
    if paths is not None:
        assert declared is not None  # unscoped_reason refuses the None case above.
        flagged = refusing_paths(paths, tags, args.repo, declared)
        if not flagged:
            # Not silent: the tree IS behind, and an operator reading a deploy log has to be
            # able to tell this from a tree that was current.
            print(
                f"deploy: {what_is_behind(base)} {behind} commit(s) behind {args.ref}, but "
                f"none of those commits reach {', '.join(sorted(tags))} -- deploying anyway.",
                file=sys.stderr,
            )
            return 0
        print(
            format_tag_refusal(
                behind,
                args.ref,
                sorted(tags),
                flagged,
                # `or []` because the refusal stands either way: an unreadable log costs the
                # message its commit list, never the verdict.
                incoming(
                    args.repo, args.ref, "log", "--oneline", "--no-decorate", base=base
                )
                or [],
                base,
            ),
            file=sys.stderr,
        )
        note = park_note(read_behind_marker(args.state_dir))
        if note:
            print(note, file=sys.stderr)
        return STALE_EXIT

    if behind:
        print(format_refusal(behind, ahead, args.ref, base), file=sys.stderr)
        # Exit 4 names a rebase of THIS tree, which is the wrong repair when the deployer is
        # parked: the primary checkout is what has to converge, and rebasing a worktree onto an
        # origin the fleet is not running deploys nothing. The SessionStart banner already says
        # this to a session as it OPENS; a session running for an hour that hits exit 4
        # mid-landing never saw it (issue #1429). Additive and best-effort — the refusal above
        # prints either way, so a host with no state directory changes nothing.
        note = park_note(read_behind_marker(args.state_dir))
        if note:
            print(note, file=sys.stderr)
        return STALE_EXIT
    return 0


if __name__ == "__main__":
    sys.exit(main())
