"""Every exit-code contract the deploy tools share, named once.

WHY ONE MODULE. `scripts/deploy.sh`'s contract used to be decoded twice: `land_lib/deploy.py`
compared a return code to the bare integers 2, 75 and 20, while `staging_gate.py` named the
same numbers in a frozenset. Nothing tied the two together, so a change to one could not fail
the other. Every consumer now imports the names from here.

THE FOUR CONTRACTS ARE DISJOINT VOCABULARIES THAT REUSE THE SAME SMALL INTEGERS. 2 means
"a tag matched no service" to deploy.sh, "bad arguments" to land.sh, "the gate could not be
asked" to staging_gate.py and "the branch is on origin with no PR" to publish_pr.py. The
prefixes are what let a reader tell which contract a value belongs to, so they are not
decoration: `publish_pr.py` in particular defines two groups of its own that both reuse 0 to
3 with different meanings, and they are `PUBLISH_*` and `UNLANDED_*` here for that reason.

Typical usage example:

    from deploy_tools.exit_codes import DEPLOY_LOCK_BUSY, DEPLOY_SH_NO_VERDICT

    if rc == DEPLOY_LOCK_BUSY:
        ...
"""

# -- scripts/deploy.sh ------------------------------------------------------------------
# The wrapper's own contract, read off `scripts/deploy.sh` (its header comment and the
# `exit` sites). 2, 3, 4 and 75 each mean NOTHING was deployed and each is a resume point;
# 20 is the inverse -- the playbook RAN and a task failed, so whatever applied before it is
# live. ansible-playbook's own 2/3/4 are collapsed onto 20 by the wrapper for exactly that
# reason; `tests/test_deploy_exit_codes.py` pins the disjointness.
DEPLOY_OK = 0
DEPLOY_TAG_MISS = 2
DEPLOY_BROAD = 3
DEPLOY_STALE = 4
DEPLOY_PLAYBOOK_FAILED = 20
DEPLOY_BAD_FLAGS = 64
DEPLOY_LOCK_BUSY = 75

# The subset that means staging (or a landing) never formed an opinion, because deploy.sh
# refused before it applied anything.
DEPLOY_SH_NO_VERDICT = frozenset(
    {DEPLOY_TAG_MISS, DEPLOY_BROAD, DEPLOY_STALE, DEPLOY_LOCK_BUSY}
)

# -- scripts/deploy_tools/gitops_tick.sh ------------------------------------------------
# 3 = the tick was skipped because the tree lock was held, so it fast-forwarded NOTHING.
# 75 = the wrapper stopped watching a run still in flight, which is not a failure.
TICK_OK = 0
TICK_LOCK_CONTENTION = 3
TICK_STILL_RUNNING = 75

# -- scripts/deploy_tools/await_ci.py ---------------------------------------------------
# `land_lib.tools.await_ci_verdict` maps await_ci's own CLI contract onto these.
CI_GREEN = 0
CI_RED = 1
CI_DISARMED = 2
CI_PENDING = 75

# -- scripts/deploy_tools/land.sh -------------------------------------------------------
# Documented in land.py's module docstring, which is what `--help` prints.
LAND_SETTLED = 0
LAND_FAILED = 1
LAND_BAD_ARGS = 2
LAND_GAVE_UP = 75

# -- scripts/deploy_tools/staging_gate.py -----------------------------------------------
# The gate's verdicts, which are also its exit codes. Three outcomes rather than two,
# because an operator who cannot tell "staging rejected this" from "staging could not be
# asked" learns to override on reflex. NOT_RUN is only ever returned under --report-busy.
GATE_PASS = 0
GATE_REJECTED = 1
GATE_NO_VERDICT = 2
GATE_NOT_RUN = 3

# -- scripts/deploy_tools/publish_pr.py, `publish` --------------------------------------
# What state the tree is in afterwards. 1 promises the commit is still local and there is
# nothing to clean up on origin; 2 promises the opposite.
PUBLISH_PUBLISHED = 0
PUBLISH_STILL_LOCAL = 1
PUBLISH_PUSHED_NO_PR = 2

# -- scripts/deploy_tools/publish_pr.py, `unlanded` -------------------------------------
# A different question, and deliberately a different vocabulary over the same integers.
UNLANDED_NOTHING = 0
UNLANDED_ORIGIN_UNREADABLE = 1
UNLANDED_PR_OPEN = 2
UNLANDED_NO_PR = 3
