"""Guards for the staging-gate backfill's verdict logic.

The point of the backfill is to decide whether the gate is trustworthy enough to block on, so
its own classification has to be trustworthy first. Every test here drives the same functions
the runner drives, and each has a rejecting half — a check only ever observed passing is not
evidence it can fail.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import backfill_staging_gate as bf  # noqa: E402


def _run(outcome: str, sha: str = "a" * 40) -> bf.Run:
    return bf.Run(sha=sha, subject="s", tags="freshrss", rc=0, outcome=outcome, note="")


# ── classify: whose fault is a non-PASS ──────────────────────────────────────────────────


def test_a_pass_is_a_pass():
    assert bf.classify(bf.PASS)[0] == bf.OK


def test_no_verdict_is_a_false_failure():
    """NO_VERDICT means the gate could not be asked, which is never a property of the change.

    This is the load-bearing one. If NO_VERDICT counted as a pass, a gate that had stopped
    working entirely — dead ssh, wrong path, expired key — would backfill twenty clean runs
    and report itself ready to block. That is the exact shape of the defect this measurement
    exists to catch, so the mapping is asserted rather than assumed.
    """
    assert bf.classify(bf.NO_VERDICT)[0] == bf.FALSE_FAILURE


def test_a_rejection_is_not_silently_called_a_false_failure():
    """A REJECTED is either the gate misfiring or a real defect in the commit, and nothing in
    the exit code distinguishes them. Guessing either way corrupts the measurement: guessing
    'false' condemns a working gate, guessing 'true' lets a broken one pass."""
    assert bf.classify(bf.REJECTED)[0] == bf.NEEDS_TRIAGE


def test_an_unexpected_exit_code_is_a_false_failure():
    # Fails closed. An exit staging_gate.py does not define means the harness does not
    # understand what happened, which is not evidence the gate is healthy.
    assert bf.classify(99)[0] == bf.FALSE_FAILURE


# ── clean_streak: consecutive, not averaged ──────────────────────────────────────────────


def test_an_unbroken_run_counts_every_sample():
    assert bf.clean_streak([_run(bf.OK) for _ in range(20)]) == 20


def test_a_true_failure_does_not_break_the_streak():
    """The gate correctly rejecting a bad commit is evidence FOR it, not against."""
    runs = [_run(bf.OK), _run(bf.TRUE_FAILURE), _run(bf.OK)]
    assert bf.clean_streak(runs) == 3


def test_the_streak_is_measured_from_the_newest_run_backwards():
    """Rejecting half of the pair above, and the reason the metric is a streak at all.

    Nineteen passes with one false failure at the START is a fixed gate; the same nineteen
    with the false failure at the END is a gate that just broke. A mean scores both 95% and
    cannot tell them apart.
    """
    fixed = [_run(bf.FALSE_FAILURE)] + [_run(bf.OK) for _ in range(19)]
    broken = [_run(bf.OK) for _ in range(19)] + [_run(bf.FALSE_FAILURE)]
    assert bf.clean_streak(fixed) == 19
    assert bf.clean_streak(broken) == 0


# ── summarise: the verdict ───────────────────────────────────────────────────────────────


def test_a_clean_backfill_meets_the_condition():
    verdict, reasons = bf.summarise([_run(bf.OK) for _ in range(20)], required=20)
    assert verdict == "MET"
    assert reasons == []


def test_a_short_streak_does_not_meet_the_condition():
    verdict, reasons = bf.summarise([_run(bf.OK) for _ in range(19)], required=20)
    assert verdict == "NOT MET"
    assert any("clean streak" in r for r in reasons)


def test_an_untriaged_rejection_blocks_the_verdict_even_with_a_long_streak():
    """A REJECTED does not break the streak, so without this the condition could read MET
    while a rejection nobody attributed sits in the data."""
    runs = [_run(bf.NEEDS_TRIAGE)] + [_run(bf.OK) for _ in range(20)]
    verdict, reasons = bf.summarise(runs, required=20)
    assert verdict == "NOT MET"
    assert any("triage" in r for r in reasons)


# ── gateable_services: the shape of a run ────────────────────────────────────────────────


def test_only_subset_services_are_gated(monkeypatch):
    """A commit touching a k8s role staging does not run must not be gated on it — the deploy
    would exit 2 on a tag matching nothing, which reads as a broken gate."""
    monkeypatch.setattr(
        bf,
        "changed_paths",
        lambda _sha: [
            "ansible/roles/k8s/freshrss/templates/deployment.yaml.j2",
            "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
        ],
    )
    assert bf.gateable_services("x") == {"freshrss"}


def test_a_commit_touching_no_subset_service_is_not_gateable(monkeypatch):
    # The rejecting half: such a commit must yield an empty set so the runner skips it,
    # rather than being gated on an empty tag list.
    monkeypatch.setattr(
        bf, "changed_paths", lambda _sha: ["docs/staging-phase-c.md", "README.md"]
    )
    assert bf.gateable_services("x") == set()


def test_the_subset_comes_from_staging_gate_rather_than_a_local_copy():
    """Pins the reuse. A fourth copy of the subset here would drift from the three that
    test_staging_subset_copies_agree.py already keeps in step."""
    import staging_gate

    assert bf.staging_gate.STAGING_SERVICES is staging_gate.STAGING_SERVICES
