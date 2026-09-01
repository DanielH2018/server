"""The cAdvisor coverage floor: empty-after-filtering vs. an empty query.

restarts/oom/cpu filter a per-pod vector down to offenders, so empty is the HEALTHY answer and
a blind query is indistinguishable from it. The floor requires a minimum pod count before it
will read empty as healthy. That split ran live from the Phase G retarget to 2026-08-24 with
all three logging OK, found by reading the code rather than by an alert.
"""

from pathlib import Path


import bridge_config
import bridge_io
import checks_cluster
import check

_REPO = Path(__file__).resolve().parents[5]


# --- cAdvisor coverage floor -------------------------------------------------------------
# restarts/oom/cpu filter a per-pod vector down to offenders, so empty-after-filtering is the
# HEALTHY answer and an empty query is indistinguishable from it. That split ran live from the
# Phase G retarget to 2026-08-24 with all three logging OK, found by reading the code rather than
# by an alert. Each pair below is one input the floor must accept and one it must reject.


def _reset_cadvisor(monkeypatch, min_pods=20, consecutive=2):
    monkeypatch.setattr(bridge_config, "CADVISOR_PODS_MIN", min_pods)
    monkeypatch.setattr(bridge_config, "CADVISOR_CONSECUTIVE", consecutive)
    monkeypatch.setattr(checks_cluster, "_cadvisor_streaks", {})


def test_cadvisor_coverage_above_the_floor_is_clean():
    assert checks_cluster.cadvisor_coverage_shortfall(20, 20, "OOM kills") is None


def test_cadvisor_coverage_below_the_floor_is_flagged():
    msg = checks_cluster.cadvisor_coverage_shortfall(19, 20, "OOM kills")
    assert msg is not None
    assert "UNKNOWN" in msg
    assert "below the floor of 20" in msg


def test_cadvisor_empty_vector_is_flagged():
    # The 2026-08-24 shape exactly: an origin-pinned selector matched nothing.
    msg = checks_cluster.cadvisor_coverage_shortfall(0, 20, "CPU throttling")
    assert msg is not None
    assert "matching nothing" in msg


def test_a_covered_vector_with_zero_offenders_still_reads_clean(monkeypatch):
    # The inversion this floor could most easily introduce: "no OOM kills" is the common case and
    # must stay green. Without this the floor would page on every healthy cycle.
    _reset_cadvisor(monkeypatch)
    monkeypatch.setattr(
        bridge_io,
        "prom_vector",
        lambda *a, **k: [({"pod": "p%d" % i}, 0.0) for i in range(40)],
    )
    ok, msg = checks_cluster.check_oom()
    assert ok is True
    assert "no OOM kills" in msg


def test_check_oom_reads_unknown_not_green_when_blind(monkeypatch):
    _reset_cadvisor(monkeypatch, consecutive=1)
    monkeypatch.setattr(bridge_io, "prom_vector", lambda *a, **k: [])
    ok, msg = checks_cluster.check_oom()
    assert ok is False
    assert "UNKNOWN" in msg


def test_check_restarts_reads_unknown_not_green_when_blind(monkeypatch):
    _reset_cadvisor(monkeypatch, consecutive=1)
    monkeypatch.setattr(bridge_io, "prom_vector", lambda *a, **k: [])
    ok, msg = checks_cluster.check_restarts()
    assert ok is False
    assert "UNKNOWN" in msg


def test_check_cpu_throttle_reads_unknown_not_green_when_blind(monkeypatch):
    _reset_cadvisor(monkeypatch, consecutive=1)
    monkeypatch.setattr(bridge_io, "prom_vector", lambda *a, **k: [])
    ok, msg = checks_cluster.check_cpu_throttle()
    assert ok is False
    assert "UNKNOWN" in msg


def test_the_floor_holds_up_for_one_cycle_before_paging(monkeypatch):
    # A kubelet restart briefly empties cAdvisor; three monitors going red together on one
    # transient is the storm the gates exist to prevent.
    _reset_cadvisor(monkeypatch, consecutive=2)
    monkeypatch.setattr(bridge_io, "prom_vector", lambda *a, **k: [])
    ok, msg = checks_cluster.check_oom()
    assert ok is True
    assert "cAdvisor coverage shortfall 1/2" in msg
    ok, _ = checks_cluster.check_oom()
    assert ok is False


def test_each_check_ages_its_shortfall_independently(monkeypatch):
    # A single shared counter would take three increments per cycle — all three checks run in the
    # same run_once pass — and blow through CADVISOR_CONSECUTIVE inside the first one.
    _reset_cadvisor(monkeypatch, consecutive=2)
    monkeypatch.setattr(bridge_io, "prom_vector", lambda *a, **k: [])
    assert checks_cluster.check_oom()[0] is True
    assert checks_cluster.check_restarts()[0] is True
    assert checks_cluster.check_cpu_throttle()[0] is True
    assert checks_cluster._cadvisor_streaks == {"oom": 1, "restarts": 1, "cpu": 1}


def test_a_covered_cycle_resets_the_streak(monkeypatch):
    _reset_cadvisor(monkeypatch, consecutive=2)
    monkeypatch.setattr(bridge_io, "prom_vector", lambda *a, **k: [])
    checks_cluster.check_oom()
    monkeypatch.setattr(
        bridge_io,
        "prom_vector",
        lambda *a, **k: [({"pod": "p%d" % i}, 0.0) for i in range(40)],
    )
    assert checks_cluster.check_oom()[0] is True
    assert checks_cluster._cadvisor_streaks["oom"] == 0


def test_cadvisor_floor_is_overridable_from_the_env_secret():
    env_secret = (
        Path(__file__).resolve().parents[1] / "templates" / "env-secret.yaml.j2"
    )
    assert 'CADVISOR_PODS_MIN: "20"' in env_secret.read_text(), (
        "CADVISOR_PODS_MIN must be rendered in env-secret.yaml.j2 so an operator can tune it "
        "without a code change, like HOST_ORIGINS_MIN"
    )


def _runtime_module_sources():
    """Every runtime module beside check.py, not just the entrypoint.

    The checks are moving out of check.py by domain (the 2026-09-01 split), so a walk pinned to
    `check.__file__` would stop seeing a cAdvisor check the day it moved — the guard-scope class
    this test was written to close, recreated one level up.
    """
    files = Path(check.__file__).resolve().parent
    return [
        p.read_text()
        for p in sorted(files.glob("*.py"))
        if not p.name.startswith("test_") and p.name != "conftest.py"
    ]


def _functions_calling(name):
    """Every top-level function in the runtime modules whose body calls `name`, by AST rather
    than by text. Matches both the bare `name(...)` form and the qualified `mod.name(...)` form,
    since the split moved `cadvisor_sel` behind `bridge_io.`.

    Derived, not enumerated. `_CADVISOR_METRICS` above is a literal tuple and the assertion it
    drives is about origin-pinning, not about the empty-vector floor — so before this, a FOURTH
    cAdvisor-derived check added later would inherit the pre-#495 "empty vector reads green"
    defect with every test still passing. That is the guard-scope class the estate has now carried
    five runs: a guard written alongside its fix inherits the fix's scope.
    """
    import ast

    out = set()
    for source in _runtime_module_sources():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                fn = call.func
                called = (
                    fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                )
                if called == name:
                    out.add(node.name)
    return out


def test_every_cadvisor_query_is_floored():
    """A cAdvisor check that skips the floor reads GREEN on an empty vector.

    cAdvisor series carry no `origin` label, so an outage or a relabel change empties the vector
    rather than erroring — and `all(...)` over nothing is True. #495 applied `_cadvisor_blind` to
    the three checks that existed; this derives the set instead, so a fourth fails here rather
    than shipping the old defect.
    """
    builders = _functions_calling("cadvisor_sel")
    floored = _functions_calling("_cadvisor_blind")
    assert builders, (
        "no function calls cadvisor_sel() — either the helper was renamed or this guard has "
        "stopped matching; a guard that matches nothing passes for the wrong reason"
    )
    missing = sorted(builders - floored)
    assert not missing, (
        "%s build a cAdvisor query without calling _cadvisor_blind(); an empty vector there "
        "reports green instead of UNKNOWN" % ", ".join(missing)
    )


def test_the_floor_helper_is_reached_by_every_check_that_needs_it():
    """The reject direction of the pair above: prove the derivation can actually find a gap.

    Asserting only `builders <= floored` would also pass if `_functions_calling` silently
    returned the empty set for both — the failure mode this repo calls a widening that lands
    green and inert. So pin the known membership too.
    """
    floored = _functions_calling("_cadvisor_blind")
    for expected in ("check_restarts", "check_oom", "check_cpu_throttle"):
        assert expected in floored, (
            "%s no longer calls _cadvisor_blind — #495's floor was removed from a check that "
            "had it" % expected
        )
