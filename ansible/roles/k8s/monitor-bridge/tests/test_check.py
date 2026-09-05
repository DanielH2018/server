"""The CHECKS registry itself: what is declared, what is selected, and the loop heartbeat.

`CHECKS` has to agree with the deployed manifests — a check needing an env var the manifest does
not set fails at runtime, and a monitor with no check never beats. CHECKS_ONLY/CHECKS_SKIP are
how the twin deployments split that registry between them.
"""

from dataclasses import replace

import os
import re
from pathlib import Path


import bridge.common
from bridge.config import load_config
import bridge.streaks
import bridge.net
import checks.storage
import check
import gates
import registry


# ── loop heartbeat (container healthcheck reads this file's mtime) ─────────────


def test_touch_heartbeat_writes_and_refreshes(tmp_path, monkeypatch, cfg):
    hb = tmp_path / "heartbeat"
    cfg = replace(cfg, HEARTBEAT_FILE=str(hb))
    bridge.common.touch_heartbeat(cfg.HEARTBEAT_FILE)
    assert hb.exists()
    first = hb.stat().st_mtime
    os.utime(hb, (first - 100, first - 100))  # backdate, then refresh
    bridge.common.touch_heartbeat(cfg.HEARTBEAT_FILE)
    assert hb.stat().st_mtime > first - 100


def test_touch_heartbeat_never_raises(monkeypatch, cfg):
    # Best-effort like push(): a heartbeat failure must not kill the loop.
    cfg = replace(cfg, HEARTBEAT_FILE="/nonexistent-dir/heartbeat")
    bridge.common.touch_heartbeat(cfg.HEARTBEAT_FILE)


def _read_sibling(relpath):
    return (Path(__file__).resolve().parent / relpath).read_text()


def test_checks_and_env_secret_push_tokens_agree():
    # Every KUMA_PUSH_* check.py reads must have an env entry in the env-secret and
    # vice-versa. A check added to CHECKS without its env silently never pushes (empty
    # token) with no Kuma no-heartbeat to self-correct. Single-deployment since the
    # Docker uninstall (2026-08-14) — the remnant compose this used to partition
    # against is archived.

    # Matched as a bare quoted literal, NOT as a `tok("...")` call: the four reachability-gate
    # tokens reach _env() through _gate() rather than through the registry's helper, and a
    # scanner keyed to one call shape stops seeing a token the moment it moves one call outward —
    # it reads green while checking nothing. Shape-independent is also exact here: every quoted
    # KUMA_PUSH_* under files/ is a token the bridge reads.
    #
    # Scanned across EVERY runtime module rather than one file. The tokens live in two of them
    # since the 17b split — the registry's in registry.py, the four gates' in check.py's
    # run_once — and a single-file scan would have gone quiet for the larger half the moment the
    # list moved. The tree is the census rather than the ship list because
    # ansible/tests/services/test_monitor_bridge_modules.py already pins the two to be equal.
    files = Path(__file__).resolve().parent.parent / "files"
    in_code = set()
    for module in sorted(files.rglob("*.py")):
        in_code |= set(re.findall(r'"(KUMA_PUSH_[A-Z0-9_]+)"', module.read_text()))
    # Non-vacuity, by name rather than by count: a scan that stopped finding the registry would
    # otherwise compare an empty set against an empty set the day env-secret.yaml.j2 was emptied
    # too, and the census has to name a member of EACH of the two modules that carry tokens.
    assert {"KUMA_PUSH_DISK", "KUMA_PUSH_PROMETHEUS"} <= in_code, sorted(in_code)
    in_twin = set(
        re.findall(
            r"^\s*(KUMA_PUSH_[A-Z0-9_]+):",
            _read_sibling("../templates/env-secret.yaml.j2"),
            re.MULTILINE,
        )
    )
    assert in_code == in_twin, "only in check.py=%s ; only in env-secret=%s" % (
        sorted(in_code - in_twin),
        sorted(in_twin - in_code),
    )


def test_every_push_token_env_is_wired_to_a_monitor():
    # Each KUMA_PUSH_* env value var must also appear as a push_token in the
    # kuma-static-monitors Secret, i.e. a push monitor actually exists to receive what
    # the check pushes. (Pre-uninstall this read AutoKuma labels on the remnant compose;
    # the static Secret has been the declaration home for the cluster bridge all along.)

    env_text = _read_sibling("../templates/env-secret.yaml.j2")
    env_vars = set(
        re.findall(r"KUMA_PUSH_[A-Z0-9_]+: \"\{\{ ([a-z0-9_]+) \}\}\"", env_text)
    )
    monitors_text = _read_sibling("../../uptime-kuma/templates/static-monitors.yaml.j2")
    label_vars = set(
        re.findall(r'"push_token": "\{\{ ([a-z0-9_]+) \}\}"', monitors_text)
    )
    assert env_vars, "no KUMA_PUSH_* env vars parsed — regex drift?"
    assert env_vars <= label_vars, (
        "env push tokens with no monitor declared: %s" % sorted(env_vars - label_vars)
    )


# The remnant's real config: only the host-state-file checks, every gate off. Three
# since the 2026-08-14 host flips (pi_peers + renovate_alive became direct pushers).
# A representative CHECKS_ONLY subset. No deployment carries a filter since the Docker
# uninstall (2026-08-14) retired the remnant — these tests keep the MECHANISM honest for
# whenever a split is next expressed.
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


def test_run_once_with_only_filter_touches_no_gate(monkeypatch, cfg):
    # With a CHECKS_ONLY filter active, run_once must evaluate exactly that set — no
    # gate probe, no metric check, no push for anything else.
    cfg = replace(cfg, CHECKS_ONLY=SUBSET_ONLY, CHECKS_SKIP=frozenset())
    evaluated = []
    # The one spy this file keeps: `_evaluate` is the single funnel every gate and every check
    # body goes through, so recording there is what proves NOTHING outside the filter was
    # evaluated. Stating a registry would only show which of the checks handed in ran.
    monkeypatch.setattr(
        gates,
        "_evaluate",
        lambda _cfg, name, fn: (evaluated.append(name), (True, "ok"))[1],
    )
    pushed = []
    monkeypatch.setattr(
        bridge.net, "push", lambda _cfg, token, ok, msg: pushed.append(msg)
    )
    check.run_once(cfg, registry.build_checks())
    assert set(evaluated) == SUBSET_ONLY
    assert len(pushed) == len(SUBSET_ONLY)


# ── check_pvc_fullness ──────────────────────────────────────────────────────
#
# These live here rather than beside check_longhorn_volumes in test_check_longhorn.py only because
# this file was the one in scope when the check landed. conftest.py's autouse _down_streaks reset
# is directory-wide, so the fixtures behave identically either way.


def _pvc_series(pvc, pct, namespace="homelab"):
    return ({"namespace": namespace, "persistentvolumeclaim": pvc}, float(pct))


def _arm_pvc(cfg, monkeypatch, vector, claims=43.0):
    cfg = replace(
        cfg,
        CLUSTER_PROM_URL="http://prometheus:9090",
        PVC_MAX_PCT=85.0,
        PVC_MIN_CLAIMS=32,
        PVC_CLAIMS_CONSECUTIVE=3,
        PVC_EXCLUDE=["media-data"],
    )
    monkeypatch.setattr(bridge.net, "prom_scalar", lambda _cfg, *a, **k: claims)
    monkeypatch.setattr(bridge.net, "prom_vector", lambda _cfg, *a, **k: vector)
    return cfg


def test_pvc_under_threshold_is_clean(monkeypatch, cfg):
    # The live shape on 2026-09-01: fullest claim 38.6%, nothing near the limit.
    cfg = _arm_pvc(
        cfg,
        monkeypatch,
        [_pvc_series("uptime-kuma-data", 38.6), _pvc_series("valheim-server", 33.8)],
    )
    ok, msg = checks.storage.check_pvc_fullness(cfg)
    assert ok
    assert "2 claim(s) under 85%" in msg
    assert "uptime-kuma-data 39%" in msg


def test_pvc_over_threshold_is_flagged(monkeypatch, cfg):
    cfg = _arm_pvc(
        cfg,
        monkeypatch,
        [_pvc_series("uptime-kuma-data", 38.6), _pvc_series("valheim-config", 91.2)],
    )
    ok, msg = checks.storage.check_pvc_fullness(cfg)
    # No grace on a fullness breach: it is monotonic, so a second cycle proves nothing.
    assert not ok
    assert "homelab/valheim-config 91%" in msg
    assert "uptime-kuma-data" not in msg


def test_pvc_excluded_claim_is_clean(monkeypatch, cfg):
    # media-data is a `local` PV on daniel-box's `/`, which check_disk already watches. Full or
    # not, this arm must not page for it — otherwise one full disk lights two monitors.
    cfg = _arm_pvc(
        cfg,
        monkeypatch,
        [_pvc_series("media-data", 99.0), _pvc_series("uptime-kuma-data", 38.6)],
    )
    ok, msg = checks.storage.check_pvc_fullness(cfg)
    assert ok
    assert "1 claim(s) under 85%" in msg


def test_pvc_claim_floor_shortfall_is_flagged(monkeypatch, cfg):
    # The fail-closed arm, at the number it was sized for. A dead kubernetes-kubelet job leaves
    # the apiserver job reporting 27 of the 43 claims, and every survivor is under the limit — so
    # the vector alone still reads healthy and the census is the only thing that separates
    # "nothing is full" from "I cannot see daniel-server's claims". Held for the grace, then paged.
    cfg = _arm_pvc(
        cfg, monkeypatch, [_pvc_series("uptime-kuma-data", 38.6)], claims=27.0
    )
    ok1, msg1 = checks.storage.check_pvc_fullness(cfg)
    assert ok1
    assert "only 27 kubelet_volume_stats claims visible" in msg1
    checks.storage.check_pvc_fullness(cfg)
    ok3, msg3 = checks.storage.check_pvc_fullness(cfg)
    assert not ok3
    assert "only 27 kubelet_volume_stats claims visible" in msg3


def test_pvc_full_kubelet_coverage_is_clean(monkeypatch, cfg):
    # The REJECT half of the floor: losing the APISERVER job costs no coverage, because the
    # kubelet job reports all 43 claims on its own. A floor that fired here would page on a
    # harmless scrape change.
    cfg = _arm_pvc(
        cfg, monkeypatch, [_pvc_series("uptime-kuma-data", 38.6)], claims=43.0
    )
    ok, msg = checks.storage.check_pvc_fullness(cfg)
    assert ok
    assert "claims visible" not in msg


def test_pvc_absent_census_is_flagged(monkeypatch, cfg):
    # prom_scalar returns None on an empty vector. The ratio query still answers here, so this
    # reaches the census arm rather than the empty-vector one below — the two must not be
    # conflated, which is why each asserts its own wording.
    cfg = _arm_pvc(
        cfg, monkeypatch, [_pvc_series("uptime-kuma-data", 38.6)], claims=None
    )
    checks.storage.check_pvc_fullness(cfg)
    checks.storage.check_pvc_fullness(cfg)
    ok, msg = checks.storage.check_pvc_fullness(cfg)
    assert not ok
    assert "no kubelet_volume_stats claims visible" in msg


def test_pvc_empty_ratio_vector_is_flagged(monkeypatch, cfg):
    # The other blind shape: the census answers but no claim reports a ratio. An empty vector is
    # indistinguishable from "no claim is full", so it must page rather than report a worst.
    cfg = _arm_pvc(cfg, monkeypatch, [], claims=43.0)
    checks.storage.check_pvc_fullness(cfg)
    checks.storage.check_pvc_fullness(cfg)
    ok, msg = checks.storage.check_pvc_fullness(cfg)
    assert not ok
    assert "no PVC reported a fullness ratio" in msg


def test_pvc_breach_outranks_a_coverage_shortfall(monkeypatch, cfg):
    # Same ordering as check_disk: a claim that IS reporting and IS full outranks a complaint
    # about the ones that are not.
    cfg = _arm_pvc(cfg, monkeypatch, [_pvc_series("valheim-config", 91.2)], claims=27.0)
    ok, msg = checks.storage.check_pvc_fullness(cfg)
    assert not ok
    assert "PVC over 85%" in msg


def test_pvc_recovery_resets_the_census_streak(monkeypatch, cfg):
    cfg = _arm_pvc(
        cfg, monkeypatch, [_pvc_series("uptime-kuma-data", 38.6)], claims=27.0
    )
    checks.storage.check_pvc_fullness(cfg)
    cfg = _arm_pvc(cfg, monkeypatch, [_pvc_series("uptime-kuma-data", 38.6)])
    assert checks.storage.check_pvc_fullness(cfg)[0]
    assert bridge.streaks._down_streaks.get("pvc_fullness", 0) == 0


def test_pvc_fullness_is_gated_by_the_cluster_prometheus():
    # It reads CLUSTER_PROM_URL, so the gate watching its source is cluster_prometheus.
    # Membership in PROM_DEPENDENT would gate it on an instance it does not query (mirrors the
    # cluster_targets guard in test_check_gates.py).
    assert "pvc_fullness" in gates.CLUSTER_DEPENDENT
    assert "pvc_fullness" not in gates.PROM_DEPENDENT
    # A job-keyed suppression would turn the claim-count floor green on exactly the partial
    # kubelet outage it exists to catch — those claims are scraped under two jobs.
    for deps in gates.EXPORTER_DEPENDENT.values():
        assert "pvc_fullness" not in deps


def test_pvc_fullness_is_disabled_without_a_cluster_prometheus(monkeypatch, cfg):
    cfg = replace(cfg, CLUSTER_PROM_URL="")
    ok, msg = checks.storage.check_pvc_fullness(cfg)
    assert ok
    assert "disabled" in msg
