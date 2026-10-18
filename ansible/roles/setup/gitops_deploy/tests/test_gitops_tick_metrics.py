"""The tick's textfile metric: the counter file itself, the label each branch of main() names,
and the overrides entrypoint() applies on top (#5676).

The branch labels run the real main() against the `tick` fixture's scripted checkout, one
case per journal line the deployer wrote in the five days to 2026-10-18, so two of those
lines landing on one label fails here rather than on a dashboard.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_tick_metrics.py

import dataclasses

import pytest

import gitops_tick_metrics
from deployer_lib.deploy_toolbox import DeployTools

LOCAL = "1" * 40
ORIGIN = "2" * 40
DOCKER_TEMPLATE = "ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2"
# A contention stamp later than any tick_started the suite can see, so the marker reads as
# written by the tick under test without reading the live clock.
AFTER_ANY_TICK_START = 4_000_000_000.0


def _runs(path) -> dict[str, int]:
    counts, _duration = gitops_tick_metrics.read_previous(str(path))
    return counts


def _gauge(path, name: str) -> float | None:
    for line in path.read_text().splitlines():
        if line.startswith(name + " "):
            return float(line.split()[1])
    return None


# ── the counter file ──────────────────────────────────────────────────────────────────────────
def test_each_record_adds_one_and_writes_every_outcome(tmp_path):
    prom = tmp_path / "gitops_tick.prom"
    gitops_tick_metrics.record(str(prom), "merged", now=100.0, duration=4.0)
    gitops_tick_metrics.record(str(prom), "merged", now=200.0, duration=6.0)
    gitops_tick_metrics.record(str(prom), "held", now=300.0, duration=1.0)
    runs = _runs(prom)
    # Zeros included, so `increase()` sees each series before its first tick.
    assert set(runs) == set(gitops_tick_metrics.OUTCOMES)
    assert runs["merged"] == 2 and runs["held"] == 1 and runs["parked"] == 0
    assert _gauge(prom, "gitops_tick_last_duration_seconds") == 1.0
    assert _gauge(prom, "gitops_tick_last_timestamp_seconds") == 300.0
    assert not list(tmp_path.glob(".gitops_tick.*")), (
        "the temp file is renamed into place, never left beside it"
    )


def test_an_unknown_outcome_is_refused(tmp_path):
    prom = tmp_path / "gitops_tick.prom"
    with pytest.raises(ValueError, match="unknown tick outcome"):
        gitops_tick_metrics.record(str(prom), "deployed", now=1.0)
    assert not prom.exists()


def test_a_garbled_file_restarts_the_counts_from_zero(tmp_path):
    prom = tmp_path / "gitops_tick.prom"
    prom.write_text('gitops_tick_runs_total{outcome="merged"} lots\nnot a metric\n')
    gitops_tick_metrics.record(str(prom), "merged", now=1.0, duration=2.0)
    assert _runs(prom)["merged"] == 1


def test_behind_seconds_reads_the_marker_and_is_zero_without_one():
    assert gitops_tick_metrics.behind_seconds(f"{ORIGIN} 1000", now=1600.0) == 600.0
    assert gitops_tick_metrics.behind_seconds(None, now=1600.0) == 0.0
    assert gitops_tick_metrics.behind_seconds("garbled", now=1600.0) == 0.0


def test_the_skipped_lock_script_keeps_the_last_duration(tmp_path):
    # The ExecStopPost= path: flock exited 75, so no tick ran and there is no duration to
    # report. The behind gauge still moves, read from the state directory's marker.
    prom = tmp_path / "gitops_tick.prom"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "behind_since").write_text(f"{ORIGIN} 1000\n")
    gitops_tick_metrics.record(str(prom), "merged", now=500.0, duration=42.0)
    rc = gitops_tick_metrics.main(
        ["skipped-lock", "--textfile", str(prom), "--state-dir", str(state_dir)]
    )
    assert rc == 0
    assert _runs(prom)["skipped-lock"] == 1 and _runs(prom)["merged"] == 1
    assert _gauge(prom, "gitops_tick_last_duration_seconds") == 42.0
    assert (_gauge(prom, "gitops_tick_behind_seconds") or 0) > 0


# ── the label each branch of main() names ─────────────────────────────────────────────────────
def _labelled_main(gitops_deploy, tick, state) -> list[str]:
    labels: list[str] = []
    tools = dataclasses.replace(tick.tools, note_outcome=labels.append)
    gitops_deploy.main(tools, tick.config, state)
    return labels


def test_a_converged_tick_is_nothing(gitops_deploy, tick, state):
    tick.origin = tick.local
    assert _labelled_main(gitops_deploy, tick, state) == ["nothing"]


def test_a_held_origin_is_held(gitops_deploy, tick, state):
    state.write_hold(ORIGIN)
    tick.paths = [DOCKER_TEMPLATE]
    assert _labelled_main(gitops_deploy, tick, state) == ["held"]


def test_pending_ci_is_ci_wait(gitops_deploy, tick, state):
    tick.ci = "pending"
    tick.paths = [DOCKER_TEMPLATE]
    assert _labelled_main(gitops_deploy, tick, state) == ["ci-wait"]


def test_a_dirty_tree_is_dirty(gitops_deploy, tick, state):
    tick.dirty = True
    tick.paths = ["docs/x.md"]
    assert _labelled_main(gitops_deploy, tick, state) == ["dirty"]


def test_a_bring_up_playbook_park_relabels_the_merge(gitops_deploy, tick, state):
    tick.paths = ["ansible/bootstrap.yml"]
    tick.files[f"{LOCAL}:ansible/bootstrap.yml"] = "- hosts: all\n"
    tick.files[f"{ORIGIN}:ansible/bootstrap.yml"] = "- hosts: all\n  become: true\n"
    assert _labelled_main(gitops_deploy, tick, state)[-1] == "parked"
    assert tick.merges == []


def test_a_docs_only_range_is_merged(gitops_deploy, tick, state):
    tick.paths = ["docs/x.md"]
    assert _labelled_main(gitops_deploy, tick, state)[-1] == "merged"
    assert tick.merges


# ── entrypoint(): the overrides, and the write being opt-in ───────────────────────────────────
def _entrypoint(gitops_deploy, state, fake_main, metrics):
    """entrypoint() with main() replaced and git answering two fixed SHAs."""
    tools = DeployTools(
        run=lambda argv, **_kw: ORIGIN if argv[-1].startswith("origin/") else LOCAL,
        is_ancestor=lambda _repo, _a, _d: True,
        discord_post=lambda _webhook, _content: True,
    )
    return gitops_deploy.entrypoint(
        tools, gitops_deploy.tick_config(), state, main=fake_main, metrics=metrics
    )


def _labels(label: str):
    def fake_main(tools, _config, _state) -> int:
        tools.note_outcome(label)
        return 0

    return fake_main


def test_a_tick_records_the_label_main_named(gitops_deploy, state, tmp_path):
    prom = tmp_path / "gitops_tick.prom"
    assert _entrypoint(gitops_deploy, state, _labels("merged"), str(prom)) == 0
    assert _runs(prom)["merged"] == 1
    assert _gauge(prom, "gitops_tick_last_duration_seconds") is not None


def test_a_tick_that_writes_a_new_hold_is_failed(gitops_deploy, state, tmp_path):
    prom = tmp_path / "gitops_tick.prom"

    def rolls_back(tools, _config, st) -> int:
        tools.note_outcome("merged")
        st.write_hold(ORIGIN)
        return 0

    _entrypoint(gitops_deploy, state, rolls_back, str(prom))
    assert _runs(prom)["failed"] == 1 and _runs(prom)["merged"] == 0


def test_a_hold_carried_over_from_an_earlier_tick_is_not_failed(
    gitops_deploy, state, tmp_path
):
    # The converse: the hold predates this tick, so the tick did not fail; it held.
    prom = tmp_path / "gitops_tick.prom"
    state.write_hold(ORIGIN)
    _entrypoint(gitops_deploy, state, _labels("held"), str(prom))
    assert _runs(prom)["held"] == 1 and _runs(prom)["failed"] == 0


def test_a_service_lock_deferral_this_tick_is_lock_deferred(
    gitops_deploy, state, tmp_path
):
    prom = tmp_path / "gitops_tick.prom"

    def defers(tools, _config, st) -> int:
        tools.note_outcome("merged")
        st.record_contention(ORIGIN, "sonarr", AFTER_ANY_TICK_START)
        return 0

    _entrypoint(gitops_deploy, state, defers, str(prom))
    assert _runs(prom)["lock-deferred"] == 1


def test_a_retryable_fetch_failure_is_fetch_failed(gitops_deploy, state, tmp_path):
    prom = tmp_path / "gitops_tick.prom"

    def blips(_tools, _config, _state) -> int:
        raise gitops_deploy.RetryableFetchError("blip")

    assert _entrypoint(gitops_deploy, state, blips, str(prom)) == 0
    assert _runs(prom)["fetch-failed"] == 1


def test_a_crash_is_failed_and_still_raises(gitops_deploy, state, tmp_path):
    prom = tmp_path / "gitops_tick.prom"

    def crashes(_tools, _config, _state) -> int:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        _entrypoint(gitops_deploy, state, crashes, str(prom))
    assert _runs(prom)["failed"] == 1


def test_an_unwritable_textfile_does_not_fail_the_tick(gitops_deploy, state, tmp_path):
    missing_dir = tmp_path / "absent" / "gitops_tick.prom"
    assert _entrypoint(gitops_deploy, state, _labels("merged"), str(missing_dir)) == 0
    assert (tmp_path / "last_run").exists()


def test_no_textfile_path_writes_nothing(gitops_deploy, state, tmp_path):
    _entrypoint(gitops_deploy, state, _labels("merged"), None)
    assert (tmp_path / "last_run").exists(), "the tick itself ran to completion"
    assert not list(tmp_path.rglob("*.prom"))
