"""Guards for the staging-gate backfill's verdict logic.

The point of the backfill is to decide whether the gate is trustworthy enough to block on, so
its own classification has to be trustworthy first. Every test here drives the same functions
the runner drives, and each has a rejecting half — a check only ever observed passing is not
evidence it can fail.
"""

import json
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
    monkeypatch.setattr(bf, "staged_services_at", lambda _sha: {"freshrss", "traefik"})
    assert bf.gateable_services("x") == {"freshrss"}


def test_a_commit_touching_no_subset_service_is_not_gateable(monkeypatch):
    # The rejecting half: such a commit must yield an empty set so the runner skips it,
    # rather than being gated on an empty tag list.
    monkeypatch.setattr(
        bf, "changed_paths", lambda _sha: ["docs/staging-phase-c.md", "README.md"]
    )
    monkeypatch.setattr(bf, "staged_services_at", lambda _sha: {"freshrss"})
    assert bf.gateable_services("x") == set()


def test_a_commit_predating_the_services_staging_support_is_not_gateable(monkeypatch):
    """The era filter. Such a commit deploys prod-shaped config to a cluster that cannot take
    it and comes back REJECTED, which has no honest triage answer — it is neither a gate
    misfire nor a defect in the commit — so it would block the verdict forever."""
    monkeypatch.setattr(
        bf,
        "changed_paths",
        lambda _sha: ["ansible/roles/k8s/freshrss/templates/deployment.yaml.j2"],
    )
    monkeypatch.setattr(bf, "staged_services_at", lambda _sha: {"traefik"})
    assert bf.gateable_services("x") == set()


def test_the_era_filter_reads_the_inventory_at_that_commit(monkeypatch):
    """Rejecting half: the filter must come from the commit's own inventory, not today's."""
    monkeypatch.setattr(
        bf,
        "run_git",
        lambda *_a: "containers_list:\n  - name: traefik\n  - name: freshrss\n",
    )
    assert bf.staged_services_at("x") == {"traefik", "freshrss"}


def test_a_commit_before_the_staging_inventory_existed_stages_nothing(monkeypatch):
    def missing(*_a):
        raise bf.subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(bf, "run_git", missing)
    assert bf.staged_services_at("x") == set()


# ── the gate checkout only fast-forwards ─────────────────────────────────────────────────


def _plan(*shas):
    return [(sha, "subject", {"freshrss"}) for sha in shas]


def test_a_commit_the_gate_checkout_has_passed_is_unrunnable():
    """The remote assert refuses a SHA that is not HEAD after the merge, so planning one is
    planning a false failure. Catching it here turns a wasted run into a message naming the
    reset that would make the window runnable."""
    stale = bf.unrunnable(
        _plan("old"), "head", ancestor_check=lambda sha, _of: sha == "old"
    )
    assert stale == ["old"]


def test_a_commit_ahead_of_the_gate_checkout_is_runnable():
    assert bf.unrunnable(_plan("new"), "head", ancestor_check=lambda *_a: False) == []


# ── the ledger ───────────────────────────────────────────────────────────────────────────


def test_the_ledger_carries_earlier_runs_forward(tmp_path):
    """A backfill is a one-shot — the gate's tree ends it at the newest planned commit — so a
    streak that only counted one invocation could never reach the required length."""
    path = tmp_path / "runs.jsonl"
    path.write_text(
        "\n".join(json.dumps(bf.asdict(_run(bf.OK))) for _ in range(3)) + "\n"
    )
    assert bf.clean_streak(bf.load_ledger(path)) == 3


def test_a_missing_ledger_starts_from_nothing(tmp_path):
    assert bf.load_ledger(tmp_path / "absent.jsonl") == []


def test_the_subset_comes_from_staging_gate_rather_than_a_local_copy():
    """Pins the reuse. A fourth copy of the subset here would drift from the three that
    test_staging_subset_copies_agree.py already keeps in step."""
    import staging_gate

    assert bf.staging_gate.STAGING_SERVICES is staging_gate.STAGING_SERVICES
