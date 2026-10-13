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

`CONTRACTS` AT THE BOTTOM IS THE ONE RENDERABLE COPY. `lib/exit_messages.py` prints a failing
code's name and meaning from it, `docs/reference/scripts.md` renders its Exit-codes column from
it, and `test_exit_codes.py` asserts it covers every constant defined above. A table in a
skill, a hook or a CLAUDE.md paragraph is a copy nothing reads; this one is read by the thing
that exits.

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
# wrapper's own contract: 2, 3, 4 and 64 are refused by its front half `deploy_lib/run.py`, 20 and
# 75-79 by its locked half `deploy_lib/under_locks.py` (and `deploy_lib/detach_run.py`).
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
CI_UNPROVEN = 4
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

# -- scripts/deploy_tools/narrow_setup.py -----------------------------------------------
# Which `--tags` value a setup-role range needs. 0 prints the tags; 1 is a refusal, after which
# the caller keeps the whole-role tag. `NARROW_SETUP_NOTHING` says the role needs no tag at all,
# printed beside `narrowed-to-nothing` on stdout. It is 5, not 3, because `narrow_cmd` exits
# `DEPLOY_BROAD` (3) for the opposite answer, "cannot narrow" (#4453), and not 2, because
# argparse exits 2 on a bad command line. The deployer's copy of the value is
# `deployer_lib/deploy_narrow.py:NARROW_SETUP_NOTHING_EXIT`, which cannot import this module.
NARROW_SETUP_NARROWED = OK
NARROW_SETUP_REFUSED = FAILED
NARROW_SETUP_NOTHING = 5

# -- scripts/diagnostics/probe.py -------------------------------------------------------
# Every verdict subcommand: 0 and 1 are what the probe observed, and 75 means it observed
# nothing because it could not read its source (a curl, an ssh or a kubectl read that failed,
# or a read that came back empty). A caller retries on 75 and investigates on 1 (#4444). 64 is
# an argument the probe cannot act on, such as `--since 3x`: no retry can succeed, and nothing
# was observed, so it names no verdict (#4471).
PROBE_OK = OK
PROBE_FAILED = FAILED
PROBE_USAGE_ERROR = USAGE_ERROR
PROBE_UNREADABLE = TEMP_FAIL

# -- scripts/dev/fanout_lib/place.py (`fanout.py place`) --------------------------------
# `launch` and `status` share one vocabulary. 3 is "nothing was placed" and nothing else, so a
# launch that started batches while dropping a refused claim exits 7 instead (#4452). A
# `status` read that timed out is 75, but only when no batch reports something worse: a failed
# batch (5) or one that needs a hand (1) outranks "read again later" (`status.exit_tier`).
FANOUT_PLACE_OK = OK
FANOUT_PLACE_FAILED = FAILED
FANOUT_PLACE_NOTHING_PLACED = 3
FANOUT_PLACE_NO_HOST_READABLE = 4
FANOUT_PLACE_BATCH_FAILED = 5
FANOUT_PLACE_NO_SIGNING_HOST = 6
FANOUT_PLACE_PARTLY_LAUNCHED = 7
FANOUT_PLACE_BAD_ARGS = USAGE_ERROR
FANOUT_PLACE_UNREAD = TEMP_FAIL

# -- scripts/docs/build_docs.py ---------------------------------------------------------
# 2 and 3 name which half failed, because `docs-refresh.sh` answers them oppositely: stale
# pages behind a fresh site against a stale site. 1 is an uncaught crash.
BUILD_DOCS_OK = OK
BUILD_DOCS_CRASHED = FAILED
BUILD_DOCS_GENERATOR_FAILED = 2
BUILD_DOCS_SITE_FAILED = 3
BUILD_DOCS_BAD_ARGS = USAGE_ERROR

# -- scripts/dev/findings.py ------------------------------------------------------------
# 3 is the register refusing the write, which `fanout_lib.abandon` reads as "the claim was
# already gone". A bad command line was 2 until #4443.
FINDINGS_OK = OK
FINDINGS_FAILED = FAILED
FINDINGS_REFUSED = 3
FINDINGS_BAD_ARGS = USAGE_ERROR

# -- scripts/dev/fact_status.py ---------------------------------------------------------
FACT_STATUS_OK = OK
FACT_STATUS_OUT = FAILED
FACT_STATUS_BAD_ARGS = USAGE_ERROR
FACT_STATUS_BAD_REF = 2

# -- scripts/deploy_tools/runbook_gates.py ----------------------------------------------
# A failed gate exits its own number, 1..N, which each runbook's `--help` lists; those are per
# runbook and so are not rows here. 69 is sysexits' EX_UNAVAILABLE: the cluster could not be
# asked, so no gate graded anything.
RUNBOOK_GATES_OK = OK
RUNBOOK_GATES_BAD_ARGS = USAGE_ERROR
RUNBOOK_GATES_UNAVAILABLE = 69

# -- scripts/secrets_mgmt/secret_rotation.py --------------------------------------------
# A refused argument (a record-tier name for `rotate`, a future date for `record`, a key the
# registry lacks) and a missing SECRET_ROTATION_KUMA were 2 until #4443; both are usage.
SECRET_ROTATION_OK = OK
SECRET_ROTATION_FAILED = FAILED
SECRET_ROTATION_SOPS_FAILED = 3
SECRET_ROTATION_BAD_ARGS = USAGE_ERROR

# -- scripts/backup/b2_drain.py ---------------------------------------------------------
# `prune_backups/tasks/b2-drain.yml` fails the play on anything but 0 and 1, so a refused
# volume reports while a usage error or a mid-run B2 failure stops the run.
B2_DRAIN_OK = OK
B2_DRAIN_REFUSED = FAILED
B2_DRAIN_ERRORED = 2
B2_DRAIN_BAD_ARGS = USAGE_ERROR

# -- scripts/infra_map/gen_infra_map.py -------------------------------------------------
INFRA_MAP_OK = OK
INFRA_MAP_LIVE_UNREADABLE = 2
INFRA_MAP_BAD_ARGS = USAGE_ERROR

# -- scripts/dev/renovate_rebase.py -----------------------------------------------------
RENOVATE_REBASE_OK = OK
RENOVATE_REBASE_NO_CHECKBOX = FAILED
RENOVATE_REBASE_GH_FAILED = 2
RENOVATE_REBASE_SOAKING = 3
RENOVATE_REBASE_BAD_ARGS = USAGE_ERROR

# -- scripts/dev/renovate_branch_sweep.py -----------------------------------------------
RENOVATE_SWEEP_OK = OK
RENOVATE_SWEEP_GH_FAILED = 2
RENOVATE_SWEEP_BAD_ARGS = USAGE_ERROR

# -- scripts/renderer/homelab_render.py -------------------------------------------------
# C1 in docs/target-architecture/contracts.md fixes 2 for a render refused before it writes.
# The parser exits 64 on a usage error, so 2 always means that refusal.
RENDER_OK = OK
RENDER_FAILED = FAILED
RENDER_REFUSED = 2
RENDER_BAD_ARGS = USAGE_ERROR

# -- scripts/renderer/shadow_diff.py ----------------------------------------------------
SHADOW_OK = OK
SHADOW_DIFFERENT = FAILED
SHADOW_REFUSED = 2
SHADOW_BAD_ARGS = USAGE_ERROR


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
            "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_locks.py plan <tag>` by hand to "
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
            "CI red or unproven, blocked by a change needing a hand, the deploy failed, the health gate "
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
        _c(CI_DISARMED, "CI_DISARMED", "the CI gate is disarmed and checks nothing."),
        _c(CI_UNPROVEN, "CI_UNPROVEN", "no CI verdict can ever arrive for the SHA."),
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
    "scripts/deploy_tools/narrow_setup.py": (
        _c(
            NARROW_SETUP_NARROWED,
            "NARROW_SETUP_NARROWED",
            "printed the comma-separated tags the range needs from the role.",
        ),
        _c(
            NARROW_SETUP_REFUSED,
            "NARROW_SETUP_REFUSED",
            "refused to narrow, so the caller applies the whole role tag.",
            "stderr names the path or tag that made the derivation uncertain.",
        ),
        _c(
            NARROW_SETUP_NOTHING,
            "NARROW_SETUP_NOTHING",
            "the range reaches no host through the role, so the role needs no tag at all; "
            "stdout is `narrowed-to-nothing`.",
        ),
    ),
    "scripts/diagnostics/probe.py": (
        _c(
            PROBE_OK,
            "PROBE_OK",
            "the probe read its source and observed nothing wrong.",
            verdict="ok",
        ),
        _c(
            PROBE_FAILED,
            "PROBE_FAILED",
            "the probe read its source and observed a bad verdict.",
            "The lines above the verdict name what failed.",
            verdict="fail",
        ),
        _c(
            PROBE_USAGE_ERROR,
            "PROBE_USAGE_ERROR",
            "an argument the probe cannot act on, so it read nothing.",
            "stderr names the argument and the forms it accepts. Fix the invocation; a retry "
            "of the same one never succeeds.",
        ),
        _c(
            PROBE_UNREADABLE,
            "PROBE_UNREADABLE",
            "the probe could not read its source, so it observed nothing.",
            "stderr carries the failed read. Retry once the source answers; this is not a "
            "verdict about the thing probed.",
            verdict="unreadable",
        ),
    ),
    "scripts/dev/fanout_lib/place.py": (
        _c(
            FANOUT_PLACE_OK,
            "FANOUT_PLACE_OK",
            "`launch`: every batch launched with every issue claimed. `status`: every batch "
            "is running, done, landed or cleaned.",
        ),
        _c(
            FANOUT_PLACE_FAILED,
            "FANOUT_PLACE_FAILED",
            "`launch`: refused before placing (a claim holder, a live batch, an issue fetch or "
            "label, a solo-only or shared-file issue), or a batch failed to launch. `status`: "
            "a batch needs a hand (needs-input, no-pr, no-verdict, no-report).",
            "stderr names the refusal; for `status`, read the batch's line.",
        ),
        _c(
            FANOUT_PLACE_NOTHING_PLACED,
            "FANOUT_PLACE_NOTHING_PLACED",
            "`launch` placed nothing: no host had headroom, the ssh budget or the live-batch "
            "cap refused, or every issue's claim was refused.",
        ),
        _c(
            FANOUT_PLACE_NO_HOST_READABLE,
            "FANOUT_PLACE_NO_HOST_READABLE",
            "no host's headroom could be read.",
        ),
        _c(
            FANOUT_PLACE_BATCH_FAILED,
            "FANOUT_PLACE_BATCH_FAILED",
            "`status`: a batch failed: its unit exited non-zero or its session set is_error.",
            "Read the stderr tail on the batch's line.",
        ),
        _c(
            FANOUT_PLACE_NO_SIGNING_HOST,
            "FANOUT_PLACE_NO_SIGNING_HOST",
            "no candidate host signs commits GitHub verifies, or the registered signing keys "
            "could not be read.",
        ),
        _c(
            FANOUT_PLACE_PARTLY_LAUNCHED,
            "FANOUT_PLACE_PARTLY_LAUNCHED",
            "`launch` started batches, but at least one issue's claim was refused and dropped.",
            "Each `dropped` line on stderr names an issue no batch carries.",
        ),
        _c(
            FANOUT_PLACE_BAD_ARGS,
            "FANOUT_PLACE_BAD_ARGS",
            "the command line is wrong: a malformed --batch, an unregistered --repo, or a "
            "run-id with no manifest.",
        ),
        _c(
            FANOUT_PLACE_UNREAD,
            "FANOUT_PLACE_UNREAD",
            "`status`: a host's read timed out and no batch reported anything worse.",
            "Run `status` again; the batches on that host may still be running.",
        ),
    ),
    "scripts/docs/build_docs.py": (
        _c(BUILD_DOCS_OK, "BUILD_DOCS_OK", "every generator ran and the site built."),
        _c(BUILD_DOCS_CRASHED, "BUILD_DOCS_CRASHED", "build_docs itself crashed."),
        _c(
            BUILD_DOCS_GENERATOR_FAILED,
            "BUILD_DOCS_GENERATOR_FAILED",
            "one or more generators failed, so the published pages are incomplete. It wins "
            "over 3 when both halves fail.",
        ),
        _c(
            BUILD_DOCS_SITE_FAILED,
            "BUILD_DOCS_SITE_FAILED",
            "every generator ran but the site build failed, so the docs site still serves its "
            "previous build.",
        ),
        _c(BUILD_DOCS_BAD_ARGS, "BUILD_DOCS_BAD_ARGS", "the command line is wrong."),
    ),
    "scripts/dev/findings.py": (
        _c(FINDINGS_OK, "FINDINGS_OK", "done."),
        _c(
            FINDINGS_FAILED,
            "FINDINGS_FAILED",
            "gh failed, or `reap` refused a git read failure rather than call it "
            '"nothing is claimed".',
        ),
        _c(
            FINDINGS_REFUSED,
            "FINDINGS_REFUSED",
            "nothing was written because the issue refuses it: closed, manual, deferred, "
            "outside the register, held by another worktree, not claimed, or lost a race.",
        ),
        _c(
            FINDINGS_BAD_ARGS,
            "FINDINGS_BAD_ARGS",
            "the command line is wrong, including a --worktree name the claim trailer could "
            "not carry.",
        ),
    ),
    "scripts/dev/fact_status.py": (
        _c(
            FACT_STATUS_OK,
            "FACT_STATUS_OK",
            "every section read is IN, the write is done, or `notice` ran.",
        ),
        _c(
            FACT_STATUS_OUT,
            "FACT_STATUS_OUT",
            "a section is OUT, a lint finding blocks, or `reverify` wrote the lock.",
        ),
        _c(FACT_STATUS_BAD_ARGS, "FACT_STATUS_BAD_ARGS", "the command line is wrong."),
        _c(FACT_STATUS_BAD_REF, "FACT_STATUS_BAD_REF", "`notice` found no merge base."),
    ),
    "scripts/deploy_tools/runbook_gates.py": (
        _c(RUNBOOK_GATES_OK, "RUNBOOK_GATES_OK", "every gate run passed."),
        _c(
            RUNBOOK_GATES_BAD_ARGS,
            "RUNBOOK_GATES_BAD_ARGS",
            "the command line is wrong, including a missing or unknown runbook.",
        ),
        _c(
            RUNBOOK_GATES_UNAVAILABLE,
            "RUNBOOK_GATES_UNAVAILABLE",
            "the cluster could not be asked, so no gate graded anything.",
            "Check that kubectl is installed and the kubeconfig names the production "
            "cluster, then re-run.",
        ),
    ),
    "scripts/secrets_mgmt/secret_rotation.py": (
        _c(SECRET_ROTATION_OK, "SECRET_ROTATION_OK", "done."),
        _c(
            SECRET_ROTATION_FAILED,
            "SECRET_ROTATION_FAILED",
            "the registry is out of sync under --check, --push pushed down, or a --deploy "
            "redeploy failed after the new tokens were written.",
        ),
        _c(
            SECRET_ROTATION_SOPS_FAILED,
            "SECRET_ROTATION_SOPS_FAILED",
            "a `sops set` failed or timed out partway through the rotate batch.",
        ),
        _c(
            SECRET_ROTATION_BAD_ARGS,
            "SECRET_ROTATION_BAD_ARGS",
            "the command line is wrong: a name or date the subcommand refuses, or --push "
            "without SECRET_ROTATION_KUMA set.",
        ),
    ),
    "scripts/backup/b2_drain.py": (
        _c(B2_DRAIN_OK, "B2_DRAIN_OK", "every requested volume drained, or would be."),
        _c(
            B2_DRAIN_REFUSED,
            "B2_DRAIN_REFUSED",
            "at least one requested volume was refused, for example because it is still live.",
        ),
        _c(
            B2_DRAIN_ERRORED,
            "B2_DRAIN_ERRORED",
            "the drain or a B2 API call failed mid-run, including a failed post-delete "
            "verification.",
        ),
        _c(
            B2_DRAIN_BAD_ARGS,
            "B2_DRAIN_BAD_ARGS",
            "the command line is wrong: a missing credential, an unreadable input file, or "
            "no volumes named.",
        ),
    ),
    "scripts/infra_map/gen_infra_map.py": (
        _c(
            INFRA_MAP_OK, "INFRA_MAP_OK", "the map was written, or was already current."
        ),
        _c(
            INFRA_MAP_LIVE_UNREADABLE,
            "INFRA_MAP_LIVE_UNREADABLE",
            "live collection lacked a required tool, so the previous map was left in place.",
        ),
        _c(INFRA_MAP_BAD_ARGS, "INFRA_MAP_BAD_ARGS", "the command line is wrong."),
    ),
    "scripts/dev/renovate_rebase.py": (
        _c(
            RENOVATE_REBASE_OK,
            "RENOVATE_REBASE_OK",
            "ticked, already ticked, or reticked.",
        ),
        _c(
            RENOVATE_REBASE_NO_CHECKBOX,
            "RENOVATE_REBASE_NO_CHECKBOX",
            "the PR body carries no rebase checkbox.",
        ),
        _c(
            RENOVATE_REBASE_GH_FAILED,
            "RENOVATE_REBASE_GH_FAILED",
            "`gh` failed; its stderr is printed.",
        ),
        _c(
            RENOVATE_REBASE_SOAKING,
            "RENOVATE_REBASE_SOAKING",
            "the PR is still soaking, or under --retick its soak status does not read "
            "SUCCESS, so nothing was written.",
        ),
        _c(
            RENOVATE_REBASE_BAD_ARGS,
            "RENOVATE_REBASE_BAD_ARGS",
            "the arguments are not one PR number and an optional --retick.",
        ),
    ),
    "scripts/dev/renovate_branch_sweep.py": (
        _c(
            RENOVATE_SWEEP_OK,
            "RENOVATE_SWEEP_OK",
            "the report printed; an empty orphan set is the healthy answer.",
        ),
        _c(
            RENOVATE_SWEEP_GH_FAILED,
            "RENOVATE_SWEEP_GH_FAILED",
            "`gh` or `git` failed, or no open Dependency Dashboard was found.",
        ),
        _c(
            RENOVATE_SWEEP_BAD_ARGS,
            "RENOVATE_SWEEP_BAD_ARGS",
            "the command line is wrong.",
        ),
    ),
    "scripts/renderer/homelab_render.py": (
        _c(RENDER_OK, "RENDER_OK", "every requested tag rendered under DIR/cluster/."),
        _c(
            RENDER_FAILED,
            "RENDER_FAILED",
            "a template failed to render, so nothing was written.",
            "The message names the template and the undefined name or filter error.",
        ),
        _c(
            RENDER_REFUSED,
            "RENDER_REFUSED",
            "the render was refused before anything was written.",
            "Pass --secret-stubs with a fresh shadow_diff.py stubs file, name only renderable "
            "tags, and render into an empty --out.",
        ),
        _c(RENDER_BAD_ARGS, "RENDER_BAD_ARGS", "the command line is wrong."),
    ),
    "scripts/renderer/shadow_diff.py": (
        _c(SHADOW_OK, "SHADOW_OK", "done, and compare or stubcheck found nothing."),
        _c(
            SHADOW_DIFFERENT,
            "SHADOW_DIFFERENT",
            "compare found a difference, or stubcheck a file lacking a stub token.",
            "Never copy or post a tree stubcheck fails.",
        ),
        _c(
            SHADOW_REFUSED,
            "SHADOW_REFUSED",
            "refused: an empty set, a missing named member or a misplaced file.",
            "Render every tag on both sides, then compare again.",
        ),
        _c(SHADOW_BAD_ARGS, "SHADOW_BAD_ARGS", "the command line is wrong."),
    ),
}


def contract(entry_point: str) -> tuple[Code, ...]:
    """`CONTRACTS[entry_point]`, or an empty tuple for an entry point that declares none."""
    return CONTRACTS.get(entry_point, ())
