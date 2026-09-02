# ansible/roles/setup/gitops_deploy/files/deploy_staging.py
"""Scoping a k8s batch to the staging subset and summarising its verdict."""

from __future__ import annotations

from collections.abc import Set as AbstractSet


def staging_scope(
    services: AbstractSet[str], subset: AbstractSet[str]
) -> tuple[set[str], set[str]]:
    """Split a deploy into the half staging can speak for and the half it cannot.

    Decision 3 of docs/staging-phase-c.md. Staging runs six services of roughly fifty-four, and
    **a gate over a subset gates only that subset** — the design's single most important
    limitation and the one most likely to be forgotten once the tile is green.

    Returns (gated, ungated). Both halves are returned rather than just the first because the
    caller has to SAY which services staging never saw: a silent skip and a silent pass look
    identical in a log afterwards, and that ambiguity is how a subset gate gets over-read as
    coverage of the whole fleet.

    An empty `gated` is the ordinary case, not an error — most deploys touch nothing staging
    runs.
    """
    return set(services & subset), set(services - subset)


def staging_verdict_summary(
    gated: set[str], ungated: set[str], deploy_rc: int, expect_rc: int
) -> str:
    """One line an operator can act on, naming what was and was not covered.

    `deploy_rc` and `expect_rc` are staging_gate.py's and staging_expectations.py's verdicts:
    0 pass, 1 rejected/failed, 2 no verdict. They are kept apart because Decision 4 needs
    "staging rejected this change" told apart from "staging could not be asked" — a guest that
    will not boot and a genuinely bad manifest are the same message otherwise, and an operator
    who cannot tell them apart learns to override on reflex.
    """
    if not gated:
        return (
            f"staging: nothing to gate; {len(ungated)} service(s) unchecked by staging"
        )
    # Named on EVERY verdict, not just the pass. A rejection is the moment an operator is most
    # likely to read the line as a statement about the whole deploy, and Decision 3's point is
    # that a silent skip and a silent pass look identical afterwards — which is just as true of
    # a silent skip beside a rejection.
    unchecked = f"; {len(ungated)} unchecked" if ungated else ""
    # The deploy's verdict is read FIRST, and expect_rc only counts once the deploy passed.
    # The caller leaves expect_rc at 2 whenever it never ran the expectation check, so a real
    # staging deploy failure always arrives as (1, 2) — and a plain `or` on that pair reported
    # a genuinely bad manifest as "staging could not be asked, which is not a rejection".
    # Decision 4's whole point is keeping those two apart, and that ordering told the operator
    # to discount the one verdict worth acting on.
    if deploy_rc == 2 or (deploy_rc == 0 and expect_rc == 2):
        return (
            f"staging: NO VERDICT on {sorted(gated)} "
            f"(deploy={deploy_rc}, expect={expect_rc}) — staging could not be asked, "
            f"which is not a rejection{unchecked}"
        )
    if deploy_rc != 0:
        return f"staging: REJECTED {sorted(gated)} — the deploy failed on staging{unchecked}"
    if expect_rc != 0:
        return (
            f"staging: REJECTED {sorted(gated)} — deployed, but a service did not answer "
            f"as declared{unchecked}"
        )
    return f"staging: PASS on {sorted(gated)}{unchecked}"


# The verdict words `consult_staging` returns and `main()` branches on. Strings rather than an
# enum because this module is imported under `uv run --no-project`, and because they are what a
# journal line has to read as — a verdict an operator cannot name is one they cannot act on.
STAGING_PASS = "pass"
STAGING_REJECTED = "rejected"
STAGING_NO_VERDICT = "no_verdict"
# Nothing was asked: the gate is off, or the change touched nothing staging runs. Distinct from
# NO_VERDICT, which means the gate WAS asked and could not answer — Decision 3's point is that a
# silent skip and a silent pass look identical afterwards, and the same is true of a skip and a
# failed consultation.
STAGING_SKIPPED = "skipped"


def staging_verdict(deploy_rc: int, expect_rc: int) -> str:
    """The one word for a (deploy, expect) exit-code pair.

    Kept in lockstep with `staging_verdict_summary` by
    test_the_verdict_word_and_the_summary_never_disagree, which walks every pair either could
    see. Splitting the branch order across two functions is exactly how a gate comes to block on
    a verdict whose own log line says something else.
    """
    if deploy_rc == 2 or (deploy_rc == 0 and expect_rc == 2):
        return STAGING_NO_VERDICT
    if deploy_rc != 0 or expect_rc != 0:
        return STAGING_REJECTED
    return STAGING_PASS


def staging_blocks(verdict: str | None, *, blocking: bool) -> bool:
    """Whether this verdict stops the prod deploy. Slice 4 of docs/staging-phase-c.md.

    Two decisions are pinned here rather than left to the call site.

    ONLY A REJECTION BLOCKS. `NO_VERDICT` means the gate could not be asked, which is never the
    change's fault, and blocking on it would park prod behind the availability of a single guest
    on a NAT network that covers six services of fifty-four. The cost of passing through is that
    a staging outage becomes a way past the gate — accepted, because that fallback is exactly the
    behaviour prod had before the gate existed, while blocking a good change is a regression.
    That is the same asymmetry the entry condition already uses to exclude a false-PASS rate.
    A pass-through is loud: `consult_staging` alerts on every non-PASS, internal errors included.

    NOTHING BLOCKS WHILE `blocking` IS FALSE, whatever the verdict. The gate stays advisory until
    `STAGING_GATE_BLOCKING` is set, which is a separate switch from `STAGING_GATE` because the
    entry condition can be met long after the code lands.
    """
    return blocking and verdict == STAGING_REJECTED


# The outcome vocabulary `backfill_staging_gate.py` records in its ledger, restated here because
# the two trees cannot import each other — `deploy_staging.py` ships to the host in the role's
# files/, and the backfill runs from the repo. The three words are asserted equal to that
# module's constants by test_tick_and_backfill_agree_on_the_outcome_vocabulary, so a rename on
# either side fails rather than silently splitting the ledger's meaning in two.
TICK_OK = "pass"
TICK_FALSE_FAILURE = "false-failure"
TICK_NEEDS_TRIAGE = "needs-triage"

_TICK_OUTCOMES = {
    STAGING_PASS: TICK_OK,
    # NO_VERDICT is a false failure by definition: the gate could not be asked, which is never a
    # property of the commit. Same call `classify` makes on the backfill side.
    STAGING_NO_VERDICT: TICK_FALSE_FAILURE,
    # A rejection is either the gate misfiring or a real defect, and only an operator can say
    # which. Recorded as needing triage rather than guessed at — guessing is how a broken gate
    # comes to report itself healthy.
    STAGING_REJECTED: TICK_NEEDS_TRIAGE,
}


def staging_tick_outcome(verdict: str) -> str | None:
    """The ledger outcome for a real gated tick's verdict, or None when there is nothing to record.

    Args:
        verdict: one of `staging_verdict`'s words, or `STAGING_SKIPPED`.

    Returns:
        The outcome word, or None for a verdict that is not a sample.

    A SKIPPED verdict returns None and MUST NOT be written. `consult_staging` returns it on two
    paths that measured nothing — the gate is off, and the tick touched no staging service — and
    a tick runs every ten minutes. Recording those would bury the real samples under thousands of
    rows that say only that the gate did not run, which is the same reason the backfill drops its
    own SKIPPED rather than shipping it to the ledger.
    """
    return _TICK_OUTCOMES.get(verdict)
