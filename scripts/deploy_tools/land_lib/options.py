"""Everything the command line sets, plus the budgets a test shortens.

Two of those budgets also read the environment, the way land.sh did before the port
(`land.sh:108-109` at f319437c^): `LAND_PRIMARY` names the checkout every git and deploy.sh
call runs in, and `LAND_MERGE_POLL` is the merge wait's poll interval. A test that runs the
shim as a process has no other way to aim a landing away from the live checkout.
"""

import argparse
import os
import sys as _sys
from dataclasses import dataclass
from pathlib import Path

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
from lib.cli_help import ArgumentParser
from lib.repo_paths import GITOPS_DEPLOY_FILES, PRIMARY_CHECKOUT

# The deployer's own marker module, read from its role's files/ rather than a copy (#3275).
_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from gitops_markers import STATE_DIR

MERGE_POLL_S = 30

PRIMARY_ENV = "LAND_PRIMARY"
MERGE_POLL_ENV = "LAND_MERGE_POLL"
REQUIRE_AUTHOR_ENV = "LAND_REQUIRE_AUTHOR"
# The landing policy a lander unit sets; `policy.py` owns what each means.
REQUIRE_BRANCH_PREFIX_ENV = "LAND_REQUIRE_BRANCH_PREFIX"
APPROVAL_PATHS_ENV = "LAND_APPROVAL_PATHS"
APPROVER_ENV = "LAND_APPROVER"
# A user that cannot land a PR itself hands it to this lander unit (`handoff.py`).
HANDOFF_ENV = "LAND_HANDOFF_UNIT"
# Seconds after land.sh starts by which the landing must print its verdict. A lander whose
# caller kills it at a fixed time sets it, so a long promotion wait ends `deferred` rather than
# in a kill that writes no verdict (#5199).
DEADLINE_ENV = "LAND_DEADLINE"
# Seconds the weekly reboot's no-promotion window may add to LAND_DEADLINE's promotion wait,
# so a landing that merges into the window waits out the deferral (#5457). A lander sets it
# only when its caller's kill leaves that much room past LAND_DEADLINE.
WINDOW_GRACE_ENV = "LAND_WINDOW_GRACE"
# The unfinished master CI runs at which `pace.hold` holds a green merge; 0 turns pacing off.
PACE_ENV = "LAND_PACE_MASTER_RUNS"
# DECIDED: hold the merge while two master runs are unfinished. A full sweep is about 18 jobs
# against a 20-job runner cap, so a second unfinished sweep already queues (#5526). A cap of 1
# was tried from 2026-10-25 (#6308), on the theory that a re-tested PR merged into an idle
# master more often merges the tree it tested, so `prove` skips the push sweep. It did not
# raise that share (#6309). Before the change, 74 of 155 master push runs ran only `prove` and
# `prek`; from 2026-10-26 to 2026-10-29, 46 of 89 did. With three or more merges in the hour
# before, the share was 61 of 140 before and 27 of 64 after. The cap of 1 did move merges onto
# an idle master: 11 of 89 merged while a master run was unfinished, against 67 of 155 before.
# Yet only 46 of 78 idle-master merges skipped the sweep, against 65 of 88 before, so the
# untested tree comes from master moving during the PR's re-test, which no merge-time read
# prevents. Batching the merges is the remaining route (#6521).
PACE_MASTER_RUNS = 2
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
    HANDOFF_ENV,
    DEADLINE_ENV,
    WINDOW_GRACE_ENV,
    PACE_ENV,
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
    author" (`ansible/roles/setup/renovate_agent/CLAUDE.md`) and nothing checked it.
    An interactive session leaves it unset, so a person's own PR arms as before; the agent's
    session inherits it from the unit and cannot arm a human's PR without `--any-author`.
    """
    return os.environ.get(REQUIRE_AUTHOR_ENV) or ""


@dataclass(frozen=True)
class Options:
    """The landing's parameters. Budgets are fields so a test sets them without an env knob.

    Frozen: nothing writes an Option after `parse_args` built it, and `conftest.py` already
    treats it as immutable by going through `dataclasses.replace`.
    """

    pr: str
    since: str = ""
    tags: str = ""
    ci_timeout: int = 1200
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
    # `LAND_DEADLINE`, in seconds after the landing starts, or 0 for none.
    deadline: int = 0
    # `LAND_WINDOW_GRACE`, the seconds the reboot window may add to the promotion wait.
    window_grace: int = 0
    # Where to write PENDING, then the landing's one-line result, for a unit to hand on.
    verdict_file: str = ""
    # Sized for a PR run plus queueing behind other PRs' runs; a PR still open after this is
    # not being merged, and the session should look at why.
    merge_timeout: int = 2700
    merge_poll: int = MERGE_POLL_S
    pace_master_runs: int = PACE_MASTER_RUNS
    lock_retries: int = 5
    lock_backoff: int = 60
    # A single retry covered one merge landing during the wait; a third merge landing during
    # the tip wait moved the tip again and the retry's own deploy exited 4.
    stale_retries: int = 3
    primary: Path = PRIMARY_CHECKOUT
    deployer_state: Path = Path(STATE_DIR)
    # `--detach` forks the landing into a logfile this script names and returns; `cc-wait land
    # <pr>` is the wait. It defaults off, so an invocation that omits it reaches the same code
    # path as a plain landing.
    detach: bool = False
    # Where `--detach` writes. Empty means `$CLAUDE_JOB_DIR/tmp`, or `detach.FALLBACK_LOG_DIR`
    # outside a Claude session. The fan-out passes its worktree's `.fanout/`, which is where
    # `fanout_lib/status.py` greps for the verdict.
    log_dir: str = ""
    # `--regate <pr>`: re-run a merged pin bump's gates and re-post its status, no landing.
    regate: bool = False

    @property
    def policy_active(self) -> bool:
        """Whether a lander unit set the landing policy, which also pins the merge."""
        return bool(self.require_branch_prefix or self.approval_paths)


def parse_args(argv: list[str] | None, description: str) -> Options:
    """The command line as Options; argparse exits 2 on a bad one, and prints `description` for --help."""
    parser = ArgumentParser(
        prog="land.sh",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # Bash's `case` rejected any argument it didn't name outright, so `--su "text"`
        # was `land: unknown argument '--su'`. argparse's default abbreviation matching
        # would instead silently resolve it to `--subject` (or `--sin` to `--since`).
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
        "--ci-timeout", type=int, default=1200, help="master CI wait budget, seconds"
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
    # Retired, and kept only so a caller still passing it hears what replaced it rather than
    # argparse's bare "unrecognized arguments".
    parser.add_argument("--await-verdict", action="store_true", help=argparse.SUPPRESS)
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
    if ns.regate and (ns.arm_merge or ns.await_merge or ns.tags or ns.detach):
        parser.error(
            "--regate re-runs the gates for a merged PR and nothing else; drop "
            "--arm-merge, --await-merge, --tags and --detach"
        )
    if ns.await_verdict:
        parser.error(
            "--await-verdict is retired; wait with cc-wait instead: "
            f"land.sh --pr {ns.pr} ... --detach && cc-wait land {ns.pr}"
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
        deadline=int(os.environ.get(DEADLINE_ENV) or 0),
        window_grace=int(os.environ.get(WINDOW_GRACE_ENV) or 0),
        verdict_file=ns.verdict_file,
        merge_poll=_merge_poll_from_env(),
        pace_master_runs=int(os.environ.get(PACE_ENV) or PACE_MASTER_RUNS),
        primary=_primary_from_env(),
        detach=ns.detach,
        log_dir=ns.log_dir,
    )
