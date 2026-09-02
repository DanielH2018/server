"""A subset gate must say what it did NOT check, not just what passed.

Decision 3 of docs/staging-phase-c.md. Staging runs six services of roughly fifty-four, and the
temptation once the tile is green is to read it as "the deploy is safe" rather than "these six
rendered and started". A silent skip and a silent pass look identical in a log afterwards, which
is how a subset gate gets over-read as coverage of the whole fleet.

Decision 4 needs the other separation: "staging rejected this change" apart from "staging could
not be asked". A guest that will not boot, a dirty tree on daniel-server and a genuinely bad
manifest are the same alert otherwise, and an operator who cannot tell them apart overrides on
reflex.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))

from deploy_staging import staging_scope, staging_verdict_summary

SUBSET = {"traefik", "authelia", "freshrss", "node-exporter", "registry", "ical-proxy"}


def test_a_deploy_inside_the_subset_is_fully_gated() -> None:
    gated, ungated = staging_scope({"freshrss"}, SUBSET)
    assert gated == {"freshrss"}
    assert ungated == set()


def test_a_deploy_outside_the_subset_is_gated_by_nothing() -> None:
    gated, ungated = staging_scope({"jellyfin", "sonarr"}, SUBSET)
    assert gated == set()
    assert ungated == {"jellyfin", "sonarr"}


def test_a_mixed_deploy_splits() -> None:
    """The case the design most needs right.

    Shipping the half staging never saw on the strength of an unrelated service's pass.
    """
    gated, ungated = staging_scope({"freshrss", "jellyfin"}, SUBSET)
    assert gated == {"freshrss"}
    assert ungated == {"jellyfin"}


def test_a_pass_names_what_it_did_not_check() -> None:
    """The rejecting half for the over-reading failure.

    A summary that said only 'PASS' would satisfy a naive test while hiding that most of the deploy
    was never gated.
    """
    summary = staging_verdict_summary({"freshrss"}, {"jellyfin", "sonarr"}, 0, 0)
    assert "PASS" in summary
    assert "2 unchecked" in summary, (
        f"a passing summary must say how much it did not check; got {summary!r}"
    )


def test_a_fully_gated_pass_does_not_claim_unchecked_services() -> None:
    summary = staging_verdict_summary({"freshrss"}, set(), 0, 0)
    assert "unchecked" not in summary


def test_every_verdict_names_what_it_did_not_check() -> None:
    """Not just the pass.

    A rejection is where an operator is most likely to read the line as covering the whole deploy,
    so that is where omitting the ungated half misleads most.

    Observed 2026-08-28: a drill gating {freshrss, ical-proxy} out of a deploy that also carried
    sonarr returned `staging: REJECTED ['freshrss', 'ical-proxy'] — deployed, but a service did not
    answer as declared`. sonarr appeared nowhere, so the line reads as a verdict on the deploy
    rather than on two thirds of it.
    """
    for deploy_rc, expect_rc in ((2, 2), (1, 2), (0, 1)):
        summary = staging_verdict_summary(
            {"freshrss"}, {"jellyfin", "sonarr"}, deploy_rc, expect_rc
        )
        assert "2 unchecked" in summary, (
            f"deploy_rc={deploy_rc} expect_rc={expect_rc} dropped the ungated half: {summary!r}"
        )


def test_a_failed_staging_deploy_is_a_rejection_not_a_missing_verdict() -> None:
    """(1, 2) is the ONLY shape a real staging deploy failure can take.

    consult_staging leaves expect_rc at 2 unless the deploy passed, so a bad manifest always
    arrives here as deploy=1, expect=2. Reading expect_rc before the deploy's own verdict made
    that pair report `staging could not be asked, which is not a rejection` — an alert that
    tells the operator to discount the one verdict worth acting on, and the exact conflation
    Decision 4 exists to prevent. No drill can reach this: every drill so far passed.
    """
    summary = staging_verdict_summary({"freshrss"}, set(), 1, 2)
    assert "REJECTED" in summary, (
        f"a failed staging deploy must read as a rejection; got {summary!r}"
    )
    assert "NO VERDICT" not in summary


def test_an_unaskable_expectation_check_is_still_a_missing_verdict() -> None:
    """The other side of the same ordering: the deploy passed, the check could not run.

    Without this the fix above could be 'deploy_rc decides everything', which would report a
    checker that never ran as a PASS — a false green, and worse than the false NO VERDICT.
    """
    summary = staging_verdict_summary({"freshrss"}, set(), 0, 2)
    assert "NO VERDICT" in summary, (
        f"an expectation check that could not run is not a pass; got {summary!r}"
    )


def test_nothing_to_gate_is_not_reported_as_a_pass() -> None:
    """A deploy staging cannot speak for must never read as staging having approved it."""
    summary = staging_verdict_summary(set(), {"jellyfin"}, 0, 0)
    assert "PASS" not in summary
    assert "nothing to gate" in summary


def test_no_verdict_is_not_a_rejection() -> None:
    """Decision 4's core distinction.

    Both directions, since either script can be the one that could not run.
    """
    for deploy_rc, expect_rc in ((2, 0), (0, 2), (2, 2)):
        summary = staging_verdict_summary({"freshrss"}, set(), deploy_rc, expect_rc)
        assert "NO VERDICT" in summary, (deploy_rc, expect_rc, summary)
        assert "REJECTED" not in summary, (
            f"deploy={deploy_rc} expect={expect_rc} reads as a rejection, which would blame a "
            f"merge for staging's own outage: {summary!r}"
        )


def test_a_failed_deploy_is_a_rejection() -> None:
    summary = staging_verdict_summary({"freshrss"}, set(), 1, 0)
    assert "REJECTED" in summary
    assert "the deploy failed on staging" in summary


def test_a_deployed_service_that_answers_wrong_is_also_a_rejection() -> None:
    """The ical-proxy shape: the play exits zero and the routes are broken.

    If this ever stops being a rejection, slice 2 has been made decorative.
    """
    summary = staging_verdict_summary({"ical-proxy"}, set(), 0, 1)
    assert "REJECTED" in summary
    assert "did not answer as declared" in summary


def test_the_three_outcomes_are_distinguishable_in_text() -> None:
    """Pins the premise: the summary is what reaches Discord, so the words have to differ."""
    outcomes = {
        staging_verdict_summary({"a"}, set(), 0, 0),
        staging_verdict_summary({"a"}, set(), 1, 0),
        staging_verdict_summary({"a"}, set(), 2, 0),
    }
    assert len(outcomes) == 3
