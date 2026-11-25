"""Everything the command line sets, plus the budgets a test shortens.

Two of those budgets also read the environment, the way land.sh did before the port
(`land.sh:108-109` at f319437c^): `LAND_PRIMARY` names the checkout every git
call runs in, and `LAND_MERGE_POLL` is the merge wait's poll interval. A test that runs the
shim as a process has no other way to aim a landing away from the live checkout.
"""

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path

from lib.cli_help import ArgumentParser
from lib.repo_paths import PRIMARY_CHECKOUT

MERGE_POLL_S = 30
# ci.yml's longest `timeout-minutes` chain into the `prek` gate (82 min) plus 10 min of runner
# queueing; test_land_ci_budget.py holds it to the workflow.
CI_TIMEOUT_S = 5520

PRIMARY_ENV = "LAND_PRIMARY"
MERGE_POLL_ENV = "LAND_MERGE_POLL"
REQUIRE_AUTHOR_ENV = "LAND_REQUIRE_AUTHOR"
# The landing policy a lander unit sets; `policy.py` owns what each means.
REQUIRE_BRANCH_PREFIX_ENV = "LAND_REQUIRE_BRANCH_PREFIX"
APPROVAL_PATHS_ENV = "LAND_APPROVAL_PATHS"
APPROVER_ENV = "LAND_APPROVER"
# The head SHA a caller already checked, and whether every landing needs LAND_APPROVER's
# approval of it: renovate-agent's lander sets them after its own floor check.
REQUIRE_HEAD_ENV = "LAND_REQUIRE_HEAD"
REQUIRE_APPROVAL_ENV = "LAND_REQUIRE_APPROVAL"
# A user that cannot land a PR itself hands it to this lander unit (`handoff.py`).
HANDOFF_ENV = "LAND_HANDOFF_UNIT"
# Seconds after land.sh starts by which the landing must print its verdict, so a long promotion wait
# ends `deferred` rather than in a kill.
DEADLINE_ENV = "LAND_DEADLINE"
# Seconds the weekly reboot's no-promotion window may add to LAND_DEADLINE's promotion wait; set
# only when the caller's kill leaves that room.
WINDOW_GRACE_ENV = "LAND_WINDOW_GRACE"
# `0` turns off testing the drafts in the line with the turn holder's own PR.
BATCH_ENV = "LAND_BATCH"
_SHA = re.compile(r"[0-9a-f]{40}")

# Named as a set so the deploy-tools tests can clear all of them: the renovate agent's unit
# exports LAND_REQUIRE_AUTHOR, and a test that inherited it exercised a refusal rather than the
# landing it was written for.
ENV_KNOBS = (
    PRIMARY_ENV,
    MERGE_POLL_ENV,
    REQUIRE_AUTHOR_ENV,
    REQUIRE_BRANCH_PREFIX_ENV,
    APPROVAL_PATHS_ENV,
    APPROVER_ENV,
    REQUIRE_HEAD_ENV,
    REQUIRE_APPROVAL_ENV,
    HANDOFF_ENV,
    DEADLINE_ENV,
    WINDOW_GRACE_ENV,
    BATCH_ENV,
)


def _primary_from_env() -> Path:
    """`LAND_PRIMARY`, or the primary checkout when it is unset or empty."""
    return Path(os.environ.get(PRIMARY_ENV) or PRIMARY_CHECKOUT)


def _merge_poll_from_env() -> int:
    """`LAND_MERGE_POLL` in seconds, or the default when it is unset or empty."""
    return int(os.environ.get(MERGE_POLL_ENV) or MERGE_POLL_S)


def _require_author_from_env() -> str:
    """`LAND_REQUIRE_AUTHOR`: the only PR author `--arm-merge` may merge, or "" for any.

    renovate-agent-land@.service sets it, because that role's contract is "never a PR by another
    author" (`hosts/components/renovate-agent/CLAUDE.md`) and nothing checked it.
    An interactive session leaves it unset, so a person's own PR arms as before; the agent's
    session inherits it from the unit and cannot arm a human's PR without `--any-author`.
    """
    return os.environ.get(REQUIRE_AUTHOR_ENV) or ""


def _require_head_from_env(parser: argparse.ArgumentParser) -> str | None:
    """`LAND_REQUIRE_HEAD`: the head SHA the caller checked, or None when unset or empty.

    Refuses through `parser.error` a value that is not 40 lowercase hex, because the policy
    compares it whole against GitHub's `headRefOid`.
    """
    head = os.environ.get(REQUIRE_HEAD_ENV) or None
    if head is not None and not _SHA.fullmatch(head):
        parser.error(f"{REQUIRE_HEAD_ENV} must be a 40-hex commit SHA, not {head!r}")
    return head


@dataclass(frozen=True)
class Options:
    """The landing's parameters. Budgets are fields so a test sets them without an env knob.

    Frozen: nothing writes an Option after `parse_args` built it, and `conftest.py` already
    treats it as immutable by going through `dataclasses.replace`.
    """

    pr: str
    since: str = ""
    tags: str = ""
    ci_timeout: int = CI_TIMEOUT_S
    await_merge: bool = False
    arm_merge: bool = False
    subject: str = ""
    # The PR author `--arm-merge` insists on; "" arms any author's PR.
    require_author: str = ""
    # The landing policy (`policy.py`); both "" leave it off.
    require_branch_prefix: str = ""
    approval_paths: str = ""
    # The login whose approval of the head lifts the approval-path refusal; "" never does.
    approver: str = ""
    # The head SHA the landing must find, or None for whatever the PR's head is.
    require_head: str | None = None
    # Whether the landing needs the approver's approval of the head whatever paths it changes.
    require_approval: bool = False
    # `LAND_DEADLINE`, in seconds after the landing starts, or 0 for none.
    deadline: int = 0
    # `LAND_WINDOW_GRACE`, the seconds the reboot window may add to the promotion wait.
    window_grace: int = 0
    # Where to write PENDING, then the landing's one-line result, for a unit to hand on.
    verdict_file: str = ""
    # A PR's CI runs the same `ci.yml` chain as master's, so a shorter wait gives up while CI can
    # still go green.
    merge_timeout: int = CI_TIMEOUT_S
    merge_poll: int = MERGE_POLL_S
    # `LAND_BATCH`: form a landing batch from the drafts in the line for the turn.
    batch: bool = True
    primary: Path = PRIMARY_CHECKOUT
    # `--detach` forks the landing into a logfile and returns; `cc-wait land <pr>` is the wait.
    detach: bool = False
    # Empty means `$CLAUDE_JOB_DIR/tmp`, or `detach.FALLBACK_LOG_DIR` outside a Claude session. The
    # fan-out passes `.fanout/`, which `fanout_lib/status.py` greps.
    log_dir: str = ""
    # `--regate <pr>`: re-run a merged pin bump's gates and re-post its status, no landing.
    regate: bool = False
    # `--fresh`: land from a checkout of origin/master made for this run (`land_lib/fresh.py`).
    fresh: bool = False

    @property
    def policy_active(self) -> bool:
        """Whether a lander unit set the landing policy, which also pins the merge."""
        return bool(
            self.require_branch_prefix
            or self.approval_paths
            or self.require_head is not None
            or self.require_approval
        )


def parse_args(argv: list[str] | None, description: str) -> Options:
    """The command line as Options; argparse exits 2 on a bad one, and prints `description` for --help."""
    parser = ArgumentParser(
        prog="land.sh",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # `allow_abbrev=False`: argparse would otherwise resolve `--su` to `--subject` instead of
        # rejecting it.
        allow_abbrev=False,
    )
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--pr", help="the PR number")
    which.add_argument(
        "--regate",
        metavar="PR",
        help="re-run a merged pin bump's upgrade gates and re-post homelab/upgrade-gates; "
        "never merges or deploys",
    )
    parser.add_argument(
        "--since", default="", help="pre-merge SHA, for the truncated-list fallback"
    )
    parser.add_argument(
        "--tags", default="", help="skip derivation; comma-separated deploy tags"
    )
    parser.add_argument(
        "--ci-timeout",
        type=int,
        default=CI_TIMEOUT_S,
        help="master CI wait budget, seconds",
    )
    parser.add_argument(
        "--await-merge", action="store_true", help="poll until the PR is merged first"
    )
    parser.add_argument(
        "--arm-merge",
        action="store_true",
        help="check the PR, then merge it directly once CI is green (needs --await-merge)",
    )
    parser.add_argument(
        "--subject",
        default="",
        help="the squash commit's subject (default: the PR title)",
    )
    parser.add_argument(
        "--any-author",
        action="store_true",
        help="arm a PR by any author, overriding LAND_REQUIRE_AUTHOR",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="fork the landing into a logfile this script names, and return",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="land from a detached checkout of origin/master this run makes and removes, "
        "whatever this checkout holds; a landing whose own code is older than master's "
        "does so without the flag",
    )
    parser.add_argument(
        "--log-dir",
        default="",
        help="with --detach: where to write the log (default: $CLAUDE_JOB_DIR/tmp)",
    )
    parser.add_argument(
        "--verdict-file",
        default="",
        help="write PENDING here, then the landing's one-line result",
    )
    ns = parser.parse_args(argv)
    if ns.regate and (
        ns.arm_merge or ns.await_merge or ns.tags or ns.detach or ns.fresh
    ):
        parser.error(
            "--regate re-runs the gates for a merged PR and nothing else; drop "
            "--arm-merge, --await-merge, --tags, --detach and --fresh"
        )
    return Options(
        pr=ns.regate or ns.pr,
        regate=bool(ns.regate),
        since=ns.since,
        tags=ns.tags,
        ci_timeout=ns.ci_timeout,
        await_merge=ns.await_merge,
        arm_merge=ns.arm_merge,
        subject=ns.subject,
        require_author="" if ns.any_author else _require_author_from_env(),
        # Not lifted by --any-author: the policy is the unit's, not the caller's.
        require_branch_prefix=os.environ.get(REQUIRE_BRANCH_PREFIX_ENV) or "",
        approval_paths=os.environ.get(APPROVAL_PATHS_ENV) or "",
        approver=os.environ.get(APPROVER_ENV) or "",
        require_head=_require_head_from_env(parser),
        require_approval=bool(os.environ.get(REQUIRE_APPROVAL_ENV)),
        deadline=int(os.environ.get(DEADLINE_ENV) or 0),
        window_grace=int(os.environ.get(WINDOW_GRACE_ENV) or 0),
        verdict_file=ns.verdict_file,
        merge_poll=_merge_poll_from_env(),
        batch=os.environ.get(BATCH_ENV) != "0",
        primary=_primary_from_env(),
        detach=ns.detach,
        log_dir=ns.log_dir,
        fresh=ns.fresh,
    )
