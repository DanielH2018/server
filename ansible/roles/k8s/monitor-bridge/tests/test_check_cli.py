"""check.py's command line, and the config faults main() reports instead of raising at import.

The pod runs `python /app/check.py` with no arguments, so the no-argument behaviour is the
contract these tests exist to pin: the CLI may add options, it may not change what happens
without them.
"""

import importlib

import pytest

import bridge.common
import bridge.config
import bridge.net
import check


def _silence(monkeypatch, pushes, ran):
    """Stub the transport and the registry so main() runs one cycle without touching the world."""
    monkeypatch.setattr(bridge.net, "push", lambda t, ok, m: pushes.append((t, ok, m)))
    monkeypatch.setattr(bridge.common, "touch_heartbeat", lambda path: None)
    monkeypatch.setattr(check, "check_prometheus", lambda: (True, "prom ok"))
    monkeypatch.setattr(check, "check_loki_reachable", lambda: (True, "loki ok"))
    monkeypatch.setattr(check, "check_b2_reachable", lambda: (True, "b2 ok"))
    monkeypatch.setattr(check, "check_cluster_prometheus", lambda: (True, "cluster ok"))
    monkeypatch.setattr(bridge.net, "prom_vector", lambda *a, **k: [])

    def _mk(name):
        def fn():
            ran.append(name)
            return True, "%s ok" % name

        return fn

    monkeypatch.setattr(check, "CHECKS", [check.Check("disk", "tok_disk", _mk("disk"))])


def test_no_arguments_means_loop_forever_and_push(monkeypatch):
    """The pod's own invocation: --once is off, --dry-run is off, so it loops and pushes.

    The Deployment runs `python /app/check.py` with no arguments, so "keeps looping" is the one
    behaviour the CLI may not change. Asserting the argparse defaults alone would pass even if
    main() returned after the first cycle, so this drives main() into the sleep and stops it
    there.
    """
    args = check.build_parser().parse_args([])
    assert args.once is False
    assert args.dry_run is False
    assert args.checks == []

    pushes, ran = [], []
    _silence(monkeypatch, pushes, ran)

    class _Slept(Exception):
        pass

    def _sleep(seconds):
        assert seconds == bridge.config.INTERVAL
        raise _Slept

    monkeypatch.setattr(check.time, "sleep", _sleep)
    with pytest.raises(_Slept):
        check.main([])
    assert ran == ["disk"]
    assert ("tok_disk", True, "disk ok") in pushes


def test_once_runs_exactly_one_cycle_and_returns_zero(monkeypatch):
    pushes, ran = [], []
    _silence(monkeypatch, pushes, ran)
    monkeypatch.setattr(
        check.time, "sleep", lambda s: pytest.fail("--once must not sleep")
    )
    assert check.main(["--once"]) == 0
    assert ran == ["disk"]
    assert ("tok_disk", True, "disk ok") in pushes


def test_dry_run_evaluates_every_check_and_pushes_nothing(monkeypatch):
    """The rejecting half of the test above: same cycle, zero pushes.

    A --dry-run that still pushed would overwrite a real monitor's state from a hand-run
    terminal, which is exactly what the flag exists to make safe.
    """
    pushes, ran = [], []
    _silence(monkeypatch, pushes, ran)
    assert check.main(["--once", "--dry-run"]) == 0
    assert ran == ["disk"]
    assert pushes == []


def test_check_flag_is_repeatable_and_filters_like_checks_only(monkeypatch):
    pushes, ran = [], []
    _silence(monkeypatch, pushes, ran)

    def _mk(name):
        def fn():
            ran.append(name)
            return True, "%s ok" % name

        return fn

    monkeypatch.setattr(
        check,
        "CHECKS",
        [
            check.Check("disk", "tok_disk", _mk("disk")),
            check.Check("memory", "tok_memory", _mk("memory")),
            # The unnamed third entry is what makes this test discriminating. With only the two
            # named checks registered, a --check that was parsed and then never threaded into
            # run_once() produces exactly the same `ran` list, because an empty CHECKS_ONLY
            # enables everything.
            check.Check("host_temp", "tok_temp", _mk("host_temp")),
        ],
    )
    monkeypatch.setattr(bridge.config, "CHECKS_ONLY", frozenset())
    monkeypatch.setattr(bridge.config, "CHECKS_SKIP", frozenset())
    # `prometheus` has to be named too: disk and memory are PROM_DEPENDENT, and the filter
    # refuses to start with a gate off under its dependents. That rule is the point of the
    # validation, so the test exercises the real spelling rather than patching around it.
    argv = ["--once", "--dry-run"]
    for name in ("disk", "memory", "prometheus"):
        argv += ["--check", name]
    assert check.main(argv) == 0
    assert sorted(ran) == ["disk", "memory"]
    assert "host_temp" not in ran


def test_an_unknown_check_name_exits_two_without_running_anything(monkeypatch):
    pushes, ran = [], []
    _silence(monkeypatch, pushes, ran)
    assert check.main(["--once", "--check", "no_such_check"]) == 2
    assert ran == []


def test_a_gate_disabled_under_its_dependents_exits_two(monkeypatch):
    """--check is validated exactly like CHECKS_ONLY, including the gate rule.

    Enabling a gated check without its gate reintroduces the alert storm the gate prevents, and
    is refused at startup rather than discovered during an outage.
    """
    pushes, ran = [], []
    _silence(monkeypatch, pushes, ran)
    assert check.main(["--once", "--check", "loki_ingestion"]) == 2
    assert ran == []


# ── bridge/config.py must never raise at import ─────────────────────────────────────────────


def test_a_malformed_number_is_recorded_rather_than_raised(monkeypatch):
    """Importing config with a garbage numeric must succeed and record one problem.

    The check that matters is the reload: a ValueError here used to kill the pod during import,
    before the heartbeat file existed and before any monitor could be told, with a traceback
    naming neither the variable nor its value.
    """
    monkeypatch.setenv("INTERVAL", "five minutes")
    cfg = importlib.reload(bridge.config)
    try:
        assert cfg.INTERVAL == 300  # the documented default, not a crash
        assert any("INTERVAL=" in p for p in cfg.CONFIG_PROBLEMS)
    finally:
        monkeypatch.delenv("INTERVAL")
        importlib.reload(bridge.config)


def test_a_well_formed_config_records_no_problems(monkeypatch):
    """The accepting half: the deployed environment must report an empty problem list."""
    cfg = importlib.reload(bridge.config)
    assert cfg.CONFIG_PROBLEMS == []
    assert cfg.INTERVAL == 300


def test_main_reports_config_problems_and_exits_two(monkeypatch):
    pushes, ran = [], []
    _silence(monkeypatch, pushes, ran)
    logged = []
    monkeypatch.setattr(
        bridge.common, "log", lambda *a: logged.append(" ".join(map(str, a)))
    )
    monkeypatch.setattr(
        bridge.config, "CONFIG_PROBLEMS", ["DISK_MAX_PCT='ninety' is not a number"]
    )
    assert check.main(["--once"]) == 2
    assert ran == []
    assert any("DISK_MAX_PCT" in line for line in logged)
