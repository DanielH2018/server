"""Everything the command line sets, plus the budgets a test shortens."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Options:
    """The landing's parameters. Budgets are fields so a test sets them without an env knob."""

    pr: str
    since: str = ""
    tags: str = ""
    ci_timeout: int = 900
    await_merge: bool = False
    arm_merge: bool = False
    subject: str = ""
    # Sized for a PR run plus queueing behind other PRs' runs; a PR still open after this is
    # not being merged, and the session should look at why.
    merge_timeout: int = 2700
    merge_poll: int = 30
    lock_retries: int = 5
    lock_backoff: int = 60
    # A single retry covered one merge landing during the wait; a third merge landing during
    # the tip wait moved the tip again and the retry's own deploy exited 4.
    stale_retries: int = 3
    primary: Path = Path("/home/ubuntu/server")
    deployer_state: Path = Path("/var/lib/gitops-deploy")


def parse_args(argv: list[str] | None, description: str) -> Options:
    """The command line as Options; argparse exits 2 on a bad one, and prints `description` for --help."""
    parser = argparse.ArgumentParser(
        prog="land.sh",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--pr", required=True, help="the PR number")
    parser.add_argument(
        "--since", default="", help="pre-merge SHA, for the truncated-list fallback"
    )
    parser.add_argument(
        "--tags", default="", help="skip derivation; comma-separated deploy tags"
    )
    parser.add_argument(
        "--ci-timeout", type=int, default=900, help="master CI wait budget, seconds"
    )
    parser.add_argument(
        "--await-merge", action="store_true", help="poll until the PR is merged first"
    )
    parser.add_argument(
        "--arm-merge",
        action="store_true",
        help="run `gh pr merge --squash --auto` first",
    )
    parser.add_argument(
        "--subject",
        default="",
        help="the squash commit's subject (default: the PR title)",
    )
    ns = parser.parse_args(argv)
    return Options(
        pr=ns.pr,
        since=ns.since,
        tags=ns.tags,
        ci_timeout=ns.ci_timeout,
        await_merge=ns.await_merge,
        arm_merge=ns.arm_merge,
        subject=ns.subject,
    )
