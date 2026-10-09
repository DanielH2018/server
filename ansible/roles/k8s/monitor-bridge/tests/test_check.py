"""The CHECKS registry itself: what is declared, what is selected, and the loop heartbeat.

`CHECKS` has to agree with the deployed manifests — a check needing an env var the manifest does
not set fails at runtime, and a monitor with no check never beats. CHECKS_ONLY/CHECKS_SKIP are
how a deployment narrows that registry.
"""

from dataclasses import replace

import os


import bridge.common
from bridge.config import load_config
import bridge.streaks
import checks.storage
import check
import gates
import registry
from _check_gate_helpers import mk
from _fake_sources import FakeSink, FakeSources
from gates import Gates


# ── loop heartbeat (container healthcheck reads this file's mtime) ─────────────


def test_touch_heartbeat_writes_and_refreshes(tmp_path, cfg):
    hb = tmp_path / "heartbeat"
    cfg = replace(cfg, HEARTBEAT_FILE=str(hb))
    bridge.common.touch_heartbeat(cfg.HEARTBEAT_FILE)
    assert hb.exists()
    first = hb.stat().st_mtime
    os.utime(hb, (first - 100, first - 100))  # backdate, then refresh
    bridge.common.touch_heartbeat(cfg.HEARTBEAT_FILE)
    assert hb.stat().st_mtime > first - 100


def test_touch_heartbeat_never_raises(cfg):
    # Best-effort like push(): a heartbeat failure must not kill the loop.
    cfg = replace(cfg, HEARTBEAT_FILE="/nonexistent-dir/heartbeat")
    bridge.common.touch_heartbeat(cfg.HEARTBEAT_FILE)


# A representative CHECKS_ONLY subset: only the host-state-file checks, every gate off. No
# deployment carries a filter — these tests keep the MECHANISM honest for whenever a split is
# next expressed.
SUBSET_ONLY = frozenset({"gitops_alive", "gitops_status"})


def test_check_enabled_only_and_skip_semantics():
    assert gates.check_enabled("disk", frozenset(), frozenset())
    assert gates.check_enabled("gitops_alive", SUBSET_ONLY, frozenset())
    assert not gates.check_enabled("disk", SUBSET_ONLY, frozenset())
    assert not gates.check_enabled("disk", frozenset(), frozenset({"disk"}))
    # skip wins even against an explicit only-listing
    assert not gates.check_enabled("disk", frozenset({"disk"}), frozenset({"disk"}))


def test_name_set_parses_csv_with_spaces():
    # Read through the field, not through load_config's private parser: CHECKS_ONLY is what
    # check_enabled consumes, and a parser that stopped being called would still pass a test
    # aimed at the parser itself.
    assert load_config({"CHECKS_ONLY": " a, b ,c,,"}).CHECKS_ONLY == frozenset(
        {"a", "b", "c"}
    )
    assert load_config({"CHECKS_ONLY": ""}).CHECKS_ONLY == frozenset()


def test_validate_rejects_unknown_names():
    problems = gates.validate_check_filter(
        frozenset({"no_such_check"}), frozenset({"also_bogus"}), registry.build_checks()
    )
    assert any("no_such_check" in p for p in problems)
    assert any("also_bogus" in p for p in problems)


def test_validate_rejects_enabled_dependent_with_disabled_gate():
    # Skipping the prometheus gate while its dependents still run would reintroduce the
    # one-outage-N-page storm the gate exists to prevent.
    problems = gates.validate_check_filter(
        frozenset(), frozenset({"prometheus"}), registry.build_checks()
    )
    assert len(problems) == 1
    assert "gate prometheus is disabled" in problems[0]


def test_validate_accepts_only_and_skip_shapes():
    # Both filter directions of a gate-free subset must validate clean.
    assert (
        gates.validate_check_filter(SUBSET_ONLY, frozenset(), registry.build_checks())
        == []
    )
    assert (
        gates.validate_check_filter(frozenset(), SUBSET_ONLY, registry.build_checks())
        == []
    )


def test_subset_names_are_real_checks():
    # Guard (mirrors the PROM_DEPENDENT guard): the subset must track CHECKS renames.
    names = {c.name for c in registry.build_checks()}
    assert SUBSET_ONLY <= names


def test_run_once_with_only_filter_touches_no_gate(cfg):
    # With a CHECKS_ONLY filter active, run_once must evaluate exactly that set — no
    # gate probe, no metric check, no push for anything else.
    cfg = replace(cfg, CHECKS_ONLY=SUBSET_ONLY, CHECKS_SKIP=frozenset())
    evaluated = []
    # Every body `_evaluate` can reach is a recorder: each registry entry and each of the four
    # gate probes. That covers the whole registry, so a body outside the filter that ran
    # would be recorded, which stating a registry of only the subset could not show.
    checks = [replace(c, fn=mk(evaluated, c.name)) for c in registry.build_checks()]
    probes = Gates(
        probe_prometheus=mk(evaluated, "prometheus"),
        probe_loki=mk(evaluated, "loki_reachable"),
        probe_b2=mk(evaluated, "b2_reachable"),
        probe_wan=mk(evaluated, "wan_reachable"),
    )
    sink = FakeSink()
    # No answers: the Prometheus gate is outside the filter, so the exporter probe sends nothing.
    check.run_once(cfg, FakeSources(), checks, probes, sink)
    assert set(evaluated) == SUBSET_ONLY
    assert len(sink.pushes) == len(SUBSET_ONLY)


# ── check_pvc_fullness ──────────────────────────────────────────────────────
#
# Each `_arm_pvc` call builds a fresh `FakeSources` and so a zeroed `src.state`; a test whose
# cycles span two calls hands the first fake's state to the second.


def _pvc_series(pvc, pct, namespace="homelab"):
    return ({"namespace": namespace, "persistentvolumeclaim": pvc}, float(pct))


def _arm_pvc(cfg, vector, claims=43.0):
    """(cfg, src) for check_pvc_fullness: the claim census answers `claims`, the ratio `vector`."""
    cfg = replace(
        cfg,
        PVC_MAX_PCT=85.0,
        PVC_MIN_CLAIMS=32,
        PVC_CLAIMS_CONSECUTIVE=3,
        PVC_EXCLUDE=["media-data"],
        # The deployed free-bytes floor names valheim-server; these tests are about the
        # percentage and census arms, and state their own floors where they mean one.
        PVC_MIN_FREE="",
    )
    return cfg, FakeSources(prom_scalar=lambda q: claims, prom_vector=lambda q: vector)


def test_pvc_under_threshold_is_clean(cfg):
    # The live shape: fullest claim 38.6%, nothing near the limit.
    cfg, src = _arm_pvc(
        cfg,
        [_pvc_series("uptime-kuma-data", 38.6), _pvc_series("valheim-server", 33.8)],
    )
    ok, msg = checks.storage.check_pvc_fullness(cfg, src)
    assert ok
    assert "2 claim(s) under 85%" in msg
    assert "uptime-kuma-data 39%" in msg


def test_pvc_over_threshold_is_flagged(cfg):
    cfg, src = _arm_pvc(
        cfg,
        [_pvc_series("uptime-kuma-data", 38.6), _pvc_series("valheim-config", 91.2)],
    )
    ok, msg = checks.storage.check_pvc_fullness(cfg, src)
    # No grace on a fullness breach: it is monotonic, so a second cycle proves nothing.
    assert not ok
    assert "homelab/valheim-config 91%" in msg
    assert "uptime-kuma-data" not in msg


def test_pvc_excluded_claim_is_clean(cfg):
    # media-data is a `local` PV on daniel-box's `/`, which check_disk already watches. Full or
    # not, this arm must not page for it — otherwise one full disk lights two monitors.
    cfg, src = _arm_pvc(
        cfg,
        [_pvc_series("media-data", 99.0), _pvc_series("uptime-kuma-data", 38.6)],
    )
    ok, msg = checks.storage.check_pvc_fullness(cfg, src)
    assert ok
    assert "1 claim(s) under 85%" in msg


def test_pvc_claim_floor_shortfall_is_flagged(cfg):
    # The fail-closed arm, at the number it was sized for. A dead kubernetes-kubelet job leaves
    # the apiserver job reporting 27 of the 43 claims, and every survivor is under the limit — so
    # the vector alone still reads healthy and the census is the only thing that separates
    # "nothing is full" from "I cannot see daniel-server's claims". Held for the grace, then paged.
    cfg, src = _arm_pvc(cfg, [_pvc_series("uptime-kuma-data", 38.6)], claims=27.0)
    ok1, msg1 = checks.storage.check_pvc_fullness(cfg, src)
    assert ok1
    assert "only 27 kubelet_volume_stats claims visible" in msg1
    checks.storage.check_pvc_fullness(cfg, src)
    ok3, msg3 = checks.storage.check_pvc_fullness(cfg, src)
    assert not ok3
    assert "only 27 kubelet_volume_stats claims visible" in msg3


def test_pvc_full_kubelet_coverage_is_clean(cfg):
    # The REJECT half of the floor: losing the APISERVER job costs no coverage, because the
    # kubelet job reports all 43 claims on its own. A floor that fired here would page on a
    # harmless scrape change.
    cfg, src = _arm_pvc(cfg, [_pvc_series("uptime-kuma-data", 38.6)], claims=43.0)
    ok, msg = checks.storage.check_pvc_fullness(cfg, src)
    assert ok
    assert "claims visible" not in msg


def test_pvc_absent_census_is_flagged(cfg):
    # prom_scalar returns None on an empty vector. The ratio query still answers here, so this
    # reaches the census arm rather than the empty-vector one below — the two must not be
    # conflated, which is why each asserts its own wording.
    cfg, src = _arm_pvc(cfg, [_pvc_series("uptime-kuma-data", 38.6)], claims=None)
    checks.storage.check_pvc_fullness(cfg, src)
    checks.storage.check_pvc_fullness(cfg, src)
    ok, msg = checks.storage.check_pvc_fullness(cfg, src)
    assert not ok
    assert "no kubelet_volume_stats claims visible" in msg


def test_pvc_empty_ratio_vector_is_flagged(cfg):
    # The other blind shape: the census answers but no claim reports a ratio. An empty vector is
    # indistinguishable from "no claim is full", so it must page rather than report a worst.
    cfg, src = _arm_pvc(cfg, [], claims=43.0)
    checks.storage.check_pvc_fullness(cfg, src)
    checks.storage.check_pvc_fullness(cfg, src)
    ok, msg = checks.storage.check_pvc_fullness(cfg, src)
    assert not ok
    assert "no PVC reported a fullness ratio" in msg


def test_pvc_breach_outranks_a_coverage_shortfall(cfg):
    # Same ordering as check_disk: a claim that IS reporting and IS full outranks a complaint
    # about the ones that are not.
    cfg, src = _arm_pvc(cfg, [_pvc_series("valheim-config", 91.2)], claims=27.0)
    ok, msg = checks.storage.check_pvc_fullness(cfg, src)
    assert not ok
    assert "PVC over 85%" in msg


def test_pvc_recovery_resets_the_census_streak(cfg):
    cfg, short = _arm_pvc(cfg, [_pvc_series("uptime-kuma-data", 38.6)], claims=27.0)
    checks.storage.check_pvc_fullness(cfg, short)
    assert short.state.down_streaks["pvc_fullness"] == 1
    cfg, src = _arm_pvc(cfg, [_pvc_series("uptime-kuma-data", 38.6)])
    src.state = short.state
    assert checks.storage.check_pvc_fullness(cfg, src)[0]
    assert src.state.down_streaks["pvc_fullness"] == 0


def test_pvc_fullness_is_gated_by_the_prometheus_gate():
    # It reads PROM_URL, and the `prometheus` gate watches that instance. Its own claim-count
    # floor is the other half: a Prometheus that answers while the kubelet volume stats go
    # unscraped is invisible to any reachability gate, so the floor must stay able to page.
    assert "pvc_fullness" in gates.PROM_DEPENDENT
    # A job-keyed suppression would turn the claim-count floor green on exactly the partial
    # kubelet outage it exists to catch — those claims are scraped under two jobs.
    for deps in gates.EXPORTER_DEPENDENT.values():
        assert "pvc_fullness" not in deps
