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
2 means "the CI gate is disarmed" to await_ci.py and "the branch is on origin with no PR" to
publish_pr.py; 3 means "the probe does not apply" to probe.py and "the branch has no PR" to
publish_pr.py's unlanded group. The prefixes are what let a reader tell which contract a value belongs to, so they are
not decoration: `publish_pr.py` in particular defines two groups of its own that both start
at 0 with different meanings, and they are `PUBLISH_*` and `UNLANDED_*` here for that reason.

`CONTRACTS` AT THE BOTTOM IS THE ONE RENDERABLE COPY. `lib/exit_messages.py` prints a failing
code's name and meaning from it, `docs/reference/scripts.md` renders its Exit-codes column from
it, and `test_exit_codes.py` asserts it covers every constant defined above. A table in a
skill, a hook or a CLAUDE.md paragraph is a copy nothing reads; this one is read by the thing
that exits.

Typical usage example:

    from lib.exit_codes import LAND_GAVE_UP

    if rc == LAND_GAVE_UP:
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
# to tell "you typed the command wrong" from `CI_DISARMED` arriving
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
UNLANDED_PR_RED = 4

# -- scripts/diagnostics/probe.py -------------------------------------------------------
# Every verdict subcommand: 0 and 1 are what the probe observed, and 75 means it observed
# nothing because it could not read its source (a curl, an ssh or a kubectl read that failed,
# or a read that came back empty). A caller retries on 75 and investigates on 1 (#4444). 64 is
# an argument the probe cannot act on, such as `--since 3x`: no retry can succeed, and nothing
# was observed, so it names no verdict (#4471). 3 is `health` reporting that the tag names
# nothing it can check, so a deploy gate skips the tag rather than grading it (#7895).
PROBE_OK = OK
PROBE_FAILED = FAILED
PROBE_NOT_APPLICABLE = 3
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
FACT_STATUS_LINT_FAILED = FAILED
FACT_STATUS_BAD_ARGS = USAGE_ERROR
FACT_STATUS_BAD_REF = 2

# -- scripts/deploy_tools/runbook_gates.py ----------------------------------------------
# A failed gate exits its own number, 1..N, which each runbook's `--help` lists; those are per
# runbook and so are not rows here. 69 is sysexits' EX_UNAVAILABLE: the cluster could not be
# asked, so no gate graded anything.
RUNBOOK_GATES_OK = OK
RUNBOOK_GATES_BAD_ARGS = USAGE_ERROR
RUNBOOK_GATES_UNAVAILABLE = 69

# -- scripts/deploy_tools/local_sweep.py ------------------------------------------------
# 69 is sysexits' EX_UNAVAILABLE again: no check graded the commit, which is neither verdict.
SWEEP_PASSED = OK
SWEEP_FAILED = FAILED
SWEEP_BAD_ARGS = USAGE_ERROR
SWEEP_NO_VERDICT = 69

# -- scripts/secrets_mgmt/secret_rotation.py --------------------------------------------
# A refused argument (a future date for `record`, a key the registry lacks) was 2 until #4443,
# and is usage.
SECRET_ROTATION_OK = OK
SECRET_ROTATION_FAILED = FAILED
SECRET_ROTATION_BAD_ARGS = USAGE_ERROR

# -- scripts/backup/b2_drain.py ---------------------------------------------------------
# `prune_backups.py` syncs the target only on 0 and 1, so a refused volume reports while a
# usage error or a mid-run B2 failure stops the run.
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
    "scripts/deploy_tools/land.sh": (
        _c(
            LAND_SETTLED,
            "LAND_SETTLED",
            "deployed and settled, or there was nothing to deploy.",
        ),
        _c(
            LAND_FAILED,
            "LAND_FAILED",
            "CI red or unproven, a change needing a hand, a tag no k8s entry declares, "
            "a held or failed promotion, a failed health gate, or a PR closed, conflicting or with red CI.",
        ),
        _c(LAND_BAD_ARGS, "LAND_BAD_ARGS", "the command line is wrong."),
        _c(
            LAND_GAVE_UP,
            "LAND_GAVE_UP",
            "gave up waiting — a budget elapsed, no promotion covered the merge in time, "
            "the health gate could not read its store, "
            "or no host package containing the merge is published.",
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
        _c(UNLANDED_NO_PR, "UNLANDED_NO_PR", "`unlanded`: a branch with no PR."),
        _c(UNLANDED_PR_RED, "UNLANDED_PR_RED", "`unlanded`: a red or conflicting PR."),
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
            PROBE_NOT_APPLICABLE,
            "PROBE_NOT_APPLICABLE",
            "`health`: the tag names nothing to check, so a deploy gate skips it.",
            verdict="skip",
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
            "`lint` found no error rule, or `report` or `notice` ran.",
        ),
        _c(
            FACT_STATUS_LINT_FAILED,
            "FACT_STATUS_LINT_FAILED",
            "`lint` found an error rule.",
        ),
        _c(FACT_STATUS_BAD_ARGS, "FACT_STATUS_BAD_ARGS", "the command line is wrong."),
        _c(FACT_STATUS_BAD_REF, "FACT_STATUS_BAD_REF", "`notice` found no merge base."),
    ),
    "scripts/deploy_tools/local_sweep.py": (
        _c(
            SWEEP_PASSED,
            "SWEEP_PASSED",
            "every local check passed. Under `--once`: the sweep reached a verdict, or the "
            "record already named the commit.",
        ),
        _c(SWEEP_FAILED, "SWEEP_FAILED", "a local check failed."),
        _c(SWEEP_BAD_ARGS, "SWEEP_BAD_ARGS", "the command line is wrong."),
        _c(
            SWEEP_NO_VERDICT,
            "SWEEP_NO_VERDICT",
            "no verdict: the commit could not be checked out, a check could not start or "
            "outran its time limit, or the sweep itself raised.",
        ),
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
            "the registry is out of sync or a row carries a tier with no cadence under "
            "--check, or the audit reads down under --verdict.",
        ),
        _c(
            SECRET_ROTATION_BAD_ARGS,
            "SECRET_ROTATION_BAD_ARGS",
            "the command line is wrong: a name or date the subcommand refuses.",
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
        _c(RENDER_OK, "RENDER_OK", "every requested tag or package rendered in DIR."),
        _c(
            RENDER_FAILED,
            "RENDER_FAILED",
            "a render or a secret check failed; nothing was written.",
            "The message names the template and the error, or the rule, tag, file and key.",
        ),
        _c(
            RENDER_REFUSED,
            "RENDER_REFUSED",
            "the render was refused before anything was written.",
            "Pass a fresh stubs file, name only renderable tags or packages that exist, "
            "and render into an empty --out.",
        ),
        _c(RENDER_BAD_ARGS, "RENDER_BAD_ARGS", "the command line is wrong."),
    ),
    "scripts/renderer/shadow_diff.py": (
        _c(SHADOW_OK, "SHADOW_OK", "done, and the subcommand found nothing."),
        _c(
            SHADOW_DIFFERENT,
            "SHADOW_DIFFERENT",
            "a round-trip difference or a failed fluxprobe case.",
        ),
        _c(
            SHADOW_REFUSED,
            "SHADOW_REFUSED",
            "refused: a tree with no cluster/, a busy --out, no flux, or a failed render.",
        ),
        _c(SHADOW_BAD_ARGS, "SHADOW_BAD_ARGS", "the command line is wrong."),
    ),
}


def contract(entry_point: str) -> tuple[Code, ...]:
    """`CONTRACTS[entry_point]`, or an empty tuple for an entry point that declares none."""
    return CONTRACTS.get(entry_point, ())
