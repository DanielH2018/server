"""Every exit-code contract this repo's entry points share, named once.

WHY ONE MODULE. A contract decoded in two places drifts: a bare integer in one consumer and
a frozenset in another are tied together by nothing, so a change to one cannot fail the other.
Every consumer imports the names from here.

WHY IT SITS IN `scripts/lib/` RATHER THAN `scripts/deploy_tools/`. `lib/` is the directory
every other script can already import. A scheme kept in `deploy_tools` reads as one tool
family's private business, and the rest of `scripts/` grows codings of its own. A new entry
point takes the spine below instead of inventing another coding.

THE SYSEXITS SPINE. `OK`, `FAILED`, `USAGE_ERROR` and `TEMP_FAIL` are the four values every
family reuses, taken from `sysexits.h` so an operator reading a bare number gets the same
answer from any script here: 64 is always "you typed the command wrong", 75 is always "nothing
happened, try again later", 0 and 1 are the verdicts. Each family defines its own names in
terms of them rather than restating the integer.

THE FAMILIES ARE STILL DISJOINT VOCABULARIES OVER THE SAME SMALL INTEGERS, outside the spine.
2 means "a tag matched no service" to deploy.sh and "the branch is on origin with no PR" to
publish_pr.py; 3 means "the change is broad" to deploy.sh and "the tree lock was held" to the
tick. The prefixes are what let a reader tell which contract a value belongs to, so they are
not decoration: `publish_pr.py` in particular defines two groups of its own that both reuse 0
to 3 with different meanings, and they are `PUBLISH_*` and `UNLANDED_*` here for that reason.

`CONTRACTS` AT THE BOTTOM IS THE ONE RENDERABLE COPY. `deploy_run.py` prints a failing code's
name and meaning from it, `docs/reference/scripts.md` renders its Exit-codes column from it,
and `test_exit_codes.py` asserts it covers every constant defined above. A table in a skill,
a hook or a CLAUDE.md paragraph is a copy nothing reads; this one is read by the thing that
exits.

Typical usage example:

    from lib.exit_codes import DEPLOY_LOCK_BUSY, DEPLOY_SH_NO_VERDICT

    if rc == DEPLOY_LOCK_BUSY:
        ...
"""

import dataclasses

# -- the spine every family below reuses ------------------------------------------------
# sysexits.h's EX_USAGE and EX_TEMPFAIL, plus the two verdict codes. A family names these
# rather than the integer, so "64 is a usage error" holds for every entry point in the repo.
OK = 0
FAILED = 1
USAGE_ERROR = 64
TEMP_FAIL = 75

# -- scripts/deploy.sh ------------------------------------------------------------------ The
# wrapper's own contract: 2, 3, 4 and 64 are refused by its front half `deploy_run.py`, 20 and
# 75-79 by its locked half `deploy_under_locks.py` (and `deploy_detach.py`).
# `DEPLOY_SH_NO_VERDICT` below is the set that means NOTHING was deployed, and every member is a
# resume point. Read the frozenset rather than a list in prose. 20 is the inverse -- the
# playbook RAN and a task failed, so whatever applied before it is live. ansible-playbook's own
# 2/3/4 are collapsed onto 20 by the wrapper for exactly that reason;
# `tests/test_deploy_exit_codes.py` pins the disjointness. One case never reaches 20:
# ansible-playbook exits 2 on a USAGE error too, and `deploy_flags.check_passthrough` asks its
# parser before the lock and refuses with 64, because there no play ran and nothing is live.
DEPLOY_OK = OK
DEPLOY_TAG_MISS = 2
DEPLOY_BROAD = 3
DEPLOY_STALE = 4
DEPLOY_PLAYBOOK_FAILED = 20
DEPLOY_BAD_FLAGS = USAGE_ERROR
DEPLOY_LOCK_BUSY = TEMP_FAIL
# flock failed for a reason that is not contention: a bad descriptor, a lock file the deploy
# user cannot open. Nothing was deployed either way, so it is a refusal — but retrying clears
# 75 and never clears this one, which is why it is its own code.
DEPLOY_LOCK_UNAVAILABLE = 76
# The snapshot worktree could not be created, so the wrapper had no tree to render from. Its
# own code rather than 76's: 76 points at the lock file, this points at the snapshot root or
# the git object store, and the two are fixed in different places (ADR-0017).
DEPLOY_SNAPSHOT_FAILED = 77
# The playbook reached PLAY RECAP naming no host. ansible exits 0 for that -- no play matched,
# so no task failed -- and the wrapper reads the recap to tell it apart from a deploy. Nothing
# was deployed, and unlike 77 the fault is in the inventory or the host pattern, not the
# snapshot.
DEPLOY_NO_HOSTS = 78
# `deploy_locks.py plan` did not print the service locks -- it exited non-zero, timed out, or
# printed nothing -- so the wrapper had nothing to take and deployed nothing. It refuses rather
# than fall back to a lock order of its own: the plan is the ONE implementation of the lock
# names and their order, and a second one is what a deadlock between a hand deploy and a tick
# is made of. Its own code because the remedy is the helper itself.
DEPLOY_LOCK_PLAN_FAILED = 79

# The subset that means staging (or a landing) never formed an opinion, because deploy.sh
# refused before it applied anything.
DEPLOY_SH_NO_VERDICT = frozenset(
    {
        DEPLOY_TAG_MISS,
        DEPLOY_BROAD,
        DEPLOY_STALE,
        DEPLOY_LOCK_BUSY,
        DEPLOY_LOCK_UNAVAILABLE,
        DEPLOY_SNAPSHOT_FAILED,
        DEPLOY_NO_HOSTS,
        DEPLOY_LOCK_PLAN_FAILED,
    }
)

# -- scripts/deploy_tools/gitops_tick.sh ------------------------------------------------
# 3 = the tick was skipped because the tree lock was held, so it fast-forwarded NOTHING.
# 4 = `--no-wait` joined a tick already in flight and started none. That tick fetched before
#     the request, so a commit merged since is not in it. Never seen with a wait
#     budget: there the wrapper watches the joined run and, when it ends cleanly, starts a
#     fresh run on the same budget and exits by THAT run's outcome.
# 75 = the wrapper stopped watching a run still in flight, which is not a failure.
TICK_OK = OK
TICK_FAILED = FAILED
# The unit is not installed on this host -- the deployer runs only where `has_gitops` is true.
# Its own code because the remedy is a different host, not a different command line.
TICK_NOT_INSTALLED = 2
TICK_LOCK_CONTENTION = 3
TICK_JOINED = 4
TICK_BAD_ARGS = USAGE_ERROR
TICK_STILL_RUNNING = TEMP_FAIL

# -- scripts/deploy_tools/await_ci.py ---------------------------------------------------
# `land_lib.tools.await_ci_verdict` maps await_ci's own CLI contract onto these.
CI_GREEN = OK
CI_RED = FAILED
CI_DISARMED = 2
CI_PENDING = TEMP_FAIL

# -- scripts/deploy_tools/land.sh -------------------------------------------------------
# Documented in land.py's module docstring, which is what `--help` prints.
# LAND_BAD_ARGS is the spine's 64. argparse's own SystemExit(2) would leave a caller unable
# to tell "you typed the command wrong" from `CI_DISARMED` or `DEPLOY_TAG_MISS` arriving
# through the same pipeline, so `land.py` remaps argparse's 2 onto 64.
LAND_SETTLED = OK
LAND_FAILED = FAILED
LAND_BAD_ARGS = USAGE_ERROR
LAND_GAVE_UP = TEMP_FAIL

# -- scripts/deploy_tools/publish_pr.py, `publish` --------------------------------------
# What state the tree is in afterwards. 1 promises the commit is still local and there is
# nothing to clean up on origin; 2 promises the opposite.
PUBLISH_PUBLISHED = OK
PUBLISH_STILL_LOCAL = FAILED
PUBLISH_PUSHED_NO_PR = 2

# -- scripts/deploy_tools/publish_pr.py, `unlanded` -------------------------------------
# A different question, and deliberately a different vocabulary over the same integers.
UNLANDED_NOTHING = OK
UNLANDED_ORIGIN_UNREADABLE = FAILED
UNLANDED_PR_OPEN = 2
UNLANDED_NO_PR = 3


# -- the renderable copy ----------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Code:
    """One row of a script's exit contract.

    `meaning` is the one line `docs/reference/scripts.md` renders. `remedy` is the operator's
    next step, printed by the entry point itself on a non-zero exit, so it reaches a human at
    a terminal as well as Claude. `verdict` is the
    one-token name of this outcome for the verdict line below, empty where the entry point
    prints none.
    """

    value: int
    const: str
    meaning: str
    remedy: str = ""
    verdict: str = ""


def _c(
    value: int, const: str, meaning: str, remedy: str = "", verdict: str = ""
) -> Code:
    return Code(value, const, meaning, remedy, verdict)


# Keyed by the entry point's path relative to the repo root — the name every doc, skill and
# caller uses. Ordered by code so the rendered table reads in one direction.
CONTRACTS: dict[str, tuple[Code, ...]] = {
    "scripts/deploy.sh": (
        _c(
            DEPLOY_OK,
            "DEPLOY_OK",
            "deployed, and the play reached PLAY RECAP naming a host.",
            verdict="deployed",
        ),
        _c(
            DEPLOY_TAG_MISS,
            "DEPLOY_TAG_MISS",
            "a --tags value matched no service in containers_list, so NOTHING was deployed.",
            "--list-services prints every valid value.",
            verdict="tag-miss",
        ),
        _c(
            DEPLOY_BROAD,
            "DEPLOY_BROAD",
            "the change is broad (shared templates, inventory, the setup plane) and maps to no "
            "single service, so NOTHING was deployed.",
            "--changed refuses it by design; apply the plane by hand. `deploy_tags.py narrow "
            "<old> <new>` prints, read-only, the services the tick would map the range to.",
            verdict="broad",
        ),
        _c(
            DEPLOY_STALE,
            "DEPLOY_STALE",
            "the tree is behind origin/master, so NOTHING was deployed.",
            "A stale tree renders stale templates and reverts live config while every "
            "repo-side check still reads green. Pull first; never --skip-staleness-check. "
            "Staleness is decided before --tags is validated, so a stale tree carrying a tag "
            "it does not know yet (a new role's first landing) reports this, not a tag miss.",
            verdict="stale",
        ),
        _c(
            DEPLOY_PLAYBOOK_FAILED,
            "DEPLOY_PLAYBOOK_FAILED",
            "the playbook RAN and a task failed, so this is the one deploy exit where changes "
            "ARE live — everything applied before the failing task took effect.",
            "Read the PLAY RECAP and the failing TASK. Do not treat it as a tag, staleness or "
            "lock refusal, and do not assume a re-run is safe.",
            verdict="playbook-failed",
        ),
        _c(
            DEPLOY_BAD_FLAGS,
            "DEPLOY_BAD_FLAGS",
            "the command line is wrong, so NOTHING was deployed and a retry changes nothing.",
            "Read the usage above; fix the flags rather than re-running.",
            verdict="bad-flags",
        ),
        _c(
            DEPLOY_LOCK_BUSY,
            "DEPLOY_LOCK_BUSY",
            "a deploy lock stayed busy, so NOTHING was deployed — either "
            "the git-tree lock (deploy_locks.TREE_LOCK) or one of this run's own "
            "/var/lock/server-deploy-<tag>.lock files.",
            "The GitOps timer or another session holds it. This is a resume point, not a "
            "playbook failure — re-run the same command shortly.",
            verdict="lock-busy",
        ),
        _c(
            DEPLOY_LOCK_UNAVAILABLE,
            "DEPLOY_LOCK_UNAVAILABLE",
            "flock failed on the lock file ITSELF, so NOTHING was deployed. This is not "
            "contention — no deploy holds the lock.",
            "Check that the git-tree lock file (deploy_locks.TREE_LOCK) exists and is writable "
            "by this user; "
            "retrying alone changes nothing.",
            verdict="lock-unavailable",
        ),
        _c(
            DEPLOY_SNAPSHOT_FAILED,
            "DEPLOY_SNAPSHOT_FAILED",
            "the snapshot worktree could not be created, so NOTHING was deployed.",
            "The playbook renders from a detached worktree of HEAD under "
            "/tmp/homelab-deploy-snapshots; the message above carries the failing command's "
            "own stderr (the `fatal:` line), so fix what it names — retrying changes nothing.",
            verdict="snapshot-failed",
        ),
        _c(
            DEPLOY_NO_HOSTS,
            "DEPLOY_NO_HOSTS",
            "the playbook matched NO host, so NOTHING was deployed.",
            "ansible exits 0 for a run where no play matched, so the wrapper reads the PLAY "
            "RECAP itself. Read the [WARNING] lines above — an inventory that failed to "
            "parse, or a host pattern that matched nothing — fix that, then re-run.",
            verdict="no-hosts",
        ),
        _c(
            DEPLOY_LOCK_PLAN_FAILED,
            "DEPLOY_LOCK_PLAN_FAILED",
            "`deploy_locks.py plan` did not print this run's service locks, so the wrapper had "
            "nothing to take and NOTHING was deployed.",
            "It never falls back to a lock order of its own. Run `uv run python "
            "ansible/roles/setup/gitops_deploy/files/deploy_locks.py plan <tag>` by hand to "
            "see why, fix that, then re-run; nothing was held while it ran.",
            verdict="lock-plan-failed",
        ),
    ),
    "scripts/deploy_tools/land.sh": (
        _c(
            LAND_SETTLED,
            "LAND_SETTLED",
            "deployed and settled, or there was nothing to deploy.",
        ),
        _c(
            LAND_FAILED,
            "LAND_FAILED",
            "CI red, blocked by a change needing a hand, the deploy failed, the health gate "
            "failed, or the PR was closed without merging, conflicts with master, or its own CI is red.",
        ),
        _c(LAND_BAD_ARGS, "LAND_BAD_ARGS", "the command line is wrong."),
        _c(
            LAND_GAVE_UP,
            "LAND_GAVE_UP",
            "gave up waiting — a budget elapsed, the deploy lock stayed busy, the tick was "
            "skipped for lock contention every time, master merged faster than one "
            "tick-and-deploy cycle, or the tick has not yet crossed origin.",
        ),
    ),
    "scripts/deploy_tools/gitops_tick.sh": (
        _c(
            TICK_OK,
            "TICK_OK",
            "the tick ran to completion; read its journal for what it did. A noop, a deferral "
            "and a real deploy all complete successfully.",
            verdict="ticked",
        ),
        _c(
            TICK_FAILED,
            "TICK_FAILED",
            "the unit failed, or it could not be started at all.",
            "gitops-deploy-alert.service has already posted to Discord via OnFailure. An "
            "`Interactive authentication required` on the start means the polkit rule is "
            "missing: apply it with `initial_setup.yml --tags gitops_deploy`.",
            verdict="failed",
        ),
        _c(
            TICK_NOT_INSTALLED,
            "TICK_NOT_INSTALLED",
            "gitops-deploy.service is not installed on this host.",
            "The deployer runs only where `has_gitops` is true (daniel-box). Run it there.",
            verdict="not-installed",
        ),
        _c(
            TICK_LOCK_CONTENTION,
            "TICK_LOCK_CONTENTION",
            "the tick was skipped for lock contention, so nothing deployed and nothing alerted.",
            "Re-run once the tree-lock holder finishes (docs/deploying.md lists them); "
            "`last_run` is untouched, and no alert fires for this.",
            verdict="contention",
        ),
        _c(
            TICK_JOINED,
            "TICK_JOINED",
            "--no-wait joined a run already in flight and started none. That run fetched "
            "before this request, so a commit merged since is not in it.",
            "Re-run once it ends, or wait for the timer.",
            verdict="joined",
        ),
        _c(
            TICK_BAD_ARGS,
            "TICK_BAD_ARGS",
            "the command line is wrong.",
            "`--wait <seconds>` and `--no-wait` are the only flags.",
            verdict="bad-args",
        ),
        _c(
            TICK_STILL_RUNNING,
            "TICK_STILL_RUNNING",
            "the wait budget elapsed and the wrapper stopped watching a run still in flight.",
            "The run itself is fine. Follow it with `journalctl -u gitops-deploy.service`.",
            verdict="still-running",
        ),
    ),
    "scripts/deploy_tools/await_ci.py": (
        _c(CI_GREEN, "CI_GREEN", "every required check on the SHA concluded green."),
        _c(CI_RED, "CI_RED", "a required check concluded red."),
        _c(
            CI_DISARMED,
            "CI_DISARMED",
            "the CI gate is disarmed, so no verdict was formed.",
        ),
        _c(CI_PENDING, "CI_PENDING", "the budget elapsed with checks still running."),
    ),
    "scripts/deploy_tools/publish_pr.py": (
        _c(
            PUBLISH_PUBLISHED,
            "PUBLISH_PUBLISHED",
            "`publish`: pushed and a PR is open.",
        ),
        _c(
            PUBLISH_STILL_LOCAL,
            "PUBLISH_STILL_LOCAL",
            "`publish`: the commit is still local and there is nothing to clean up on origin. "
            "`unlanded`: origin was unreadable.",
        ),
        _c(
            PUBLISH_PUSHED_NO_PR,
            "PUBLISH_PUSHED_NO_PR",
            "`publish`: pushed but no PR was opened. `unlanded`: a PR is open.",
        ),
        _c(
            UNLANDED_NO_PR,
            "UNLANDED_NO_PR",
            "`unlanded`: the branch is on origin with no PR.",
        ),
    ),
}


# -- the verdict line an entry point ends with ------------------------------------------
#
# `land.sh` prints `VERDICT: <verdict> (<detail>)` as its last line. `fanout_status.py` and the
# `land-after-merge` wait both find a landing's outcome by grepping `^VERDICT:`.
#
# DECIDED: deploy.sh and the tick print that same shape under their OWN prefix rather than
# under `VERDICT:`. A landing runs both as subprocesses with stdout INHERITED
# (`land_lib/tools.py:run_deploy`), so their lines reach the landing's own logfile ABOVE its
# verdict -- and `grep -m1 '^VERDICT:'` takes the FIRST match, which would report the deploy's
# outcome as the landing's on every landing, with nothing failing. One format, three prefixes,
# one renderer. Issue #2853 asked for a bare `VERDICT:` from all three; the PR that closed it
# carries the measurement.
VERDICT_PREFIXES = {
    "scripts/deploy.sh": "DEPLOY-VERDICT",
    "scripts/deploy_tools/gitops_tick.sh": "TICK-VERDICT",
    "scripts/deploy_tools/land.sh": "VERDICT",
}


def verdict_line(entry_point: str, rc: int, detail: str) -> str | None:
    """`<PREFIX>: <verdict> (<detail>)` for `rc`, or None when the code names no verdict.

    The outermost tool an operator ran is the one whose prefix is bare `VERDICT`; see the
    DECIDED note above for why the inner two are not.
    """
    prefix = VERDICT_PREFIXES.get(entry_point)
    for code in contract(entry_point):
        if code.value == rc and prefix and code.verdict:
            return f"{prefix}: {code.verdict} ({detail})"
    return None


def contract(entry_point: str) -> tuple[Code, ...]:
    """`CONTRACTS[entry_point]`, or an empty tuple for an entry point that declares none."""
    return CONTRACTS.get(entry_point, ())


def describe(entry_point: str, rc: int) -> str | None:
    """`<const> (<value>): <meaning> <remedy>` for `rc`, or None when it is not in the contract.

    What an entry point prints on its own non-zero exit, so an operator reading a bare
    `Exit code N` does not have to look the number up. The wrapper prints this; nothing
    downstream re-decodes it.
    """
    for code in contract(entry_point):
        if code.value == rc:
            tail = f" {code.remedy}" if code.remedy else ""
            return f"{code.const} ({code.value}): {code.meaning}{tail}"
    return None
