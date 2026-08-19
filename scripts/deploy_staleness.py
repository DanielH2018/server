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

NOT THE AUTOMATED PIPELINE. gitops_deploy.py invokes ansible-playbook directly
(roles/setup/gitops_deploy/files/gitops_deploy.py:572), not this wrapper, and it pulls
before deploying. This guard covers the interactive and agent path, where the failure was.

Used by scripts/deploy.sh, which maps a non-zero exit here to its own refusal.
"""

import argparse
import os
import subprocess
import sys

# Distinct from deploy.sh's other refusals: 2 = tag matched nothing, 3 = broad --changed,
# 75 = lock busy.
STALE_EXIT = 4

FETCH_TIMEOUT_S = 20


def _git(
    repo: str, *args: str, timeout: float | None = None
) -> subprocess.CompletedProcess:
    """Run git in `repo` with inherited GIT_* variables removed.

    `git -C` does not override GIT_DIR, so an inherited one (a pre-commit hook, a
    rebase in progress) would silently redirect these reads at another repository.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", repo, *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def behind_ahead(repo, ref: str = "origin/master") -> tuple[int, int]:
    """Return (behind, ahead) for HEAD relative to `ref`.

    Raises LookupError if `ref` cannot be resolved.
    """
    proc = _git(str(repo), "rev-list", "--left-right", "--count", f"{ref}...HEAD")
    if proc.returncode != 0:
        raise LookupError(f"cannot resolve {ref}")
    left, right = proc.stdout.split()
    return int(left), int(right)


def format_refusal(behind: int, ahead: int, ref: str) -> str:
    ahead_note = f" (and {ahead} ahead)" if ahead else ""
    return (
        f"deploy: this tree is {behind} commit(s) behind {ref}{ahead_note} "
        f"-- nothing was deployed.\n"
        f"  Deploying now would render stale templates and revert live config for the\n"
        f"  roles you target, while every repo-side check still reads green.\n"
        f"  Fix: git fetch origin && git rebase {ref}\n"
        f"  Deliberately deploying an older tree? Re-run with --skip-staleness-check."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".")
    parser.add_argument("--ref", default="origin/master")
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="skip refreshing the remote ref (tests, and offline runs)",
    )
    args = parser.parse_args(argv)

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
        behind, ahead = behind_ahead(args.repo, args.ref)
    except LookupError:
        # No such ref: a fresh init, a detached CI checkout, a fork with another remote.
        # This is a staleness check, not a git-topology check — do not block the deploy.
        return 0

    if behind:
        print(format_refusal(behind, ahead, args.ref), file=sys.stderr)
        return STALE_EXIT
    return 0


if __name__ == "__main__":
    sys.exit(main())
