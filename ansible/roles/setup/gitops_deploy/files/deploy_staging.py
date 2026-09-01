# ansible/roles/setup/gitops_deploy/files/deploy_staging.py
"""Scoping a k8s batch to the staging subset and summarising its verdict."""

from __future__ import annotations


def staging_scope(services: set[str], subset: set[str]) -> tuple[set[str], set[str]]:
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
    return services & subset, services - subset


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
