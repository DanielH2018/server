"""PVC free-bytes floors: a claim whose peak is a step, not a slope (#1875).

valheim-server sat at 79% for days and reached 100% inside one 15-minute updater cycle, under
PVC_MAX_PCT the whole way, so the percentage arm reported the outage rather than the risk.
Every behaviour gets an accept/reject pair. The last test is the non-vacuity half: it reads
the transient the valheim role declares from the TREE and pins PVC_MIN_FREE to it by name, so
the two numbers cannot drift apart and a renamed claim fails here rather than going unwatched.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_check_pvc_floors.py
"""

import re
from dataclasses import replace
from pathlib import Path

import bridge.net
import checks.storage
from verdicts.storage import parse_pvc_floors, pvc_fullness_verdict

ROLES = Path(__file__).resolve().parents[3]
ENV_SECRET = Path(__file__).resolve().parents[1] / "templates" / "env-secret.yaml.j2"

GIB = 1024**3
# The 2026-09-17 shape at 10Gi: 7.7 G used of 9.8 G, 2.1 G free, against a 2.2 G copy the
# update was about to stage. 79% full, so the 85% arm stayed green until the ENOSPC.
VALHEIM_FLOOR = 3 * GIB
_PRE_UPDATE_PCT = 79.0
_PRE_UPDATE_FREE = 2.1 * GIB


def _pvc(pvc, pct, namespace="homelab"):
    return ({"namespace": namespace, "persistentvolumeclaim": pvc}, float(pct))


def _arm(cfg, monkeypatch, pcts, frees, floors="valheim-server=%d" % VALHEIM_FLOOR):
    """State the two claim vectors the check reads, keyed by which metric is asked for."""

    def _vector(_cfg, promql, *a, **k):
        return frees if "available_bytes)" in promql else pcts

    monkeypatch.setattr(bridge.net, "prom_scalar", lambda _cfg, *a, **k: 43.0)
    monkeypatch.setattr(bridge.net, "prom_vector", _vector)
    return replace(
        cfg,
        CLUSTER_PROM_URL="http://prometheus:9090",
        PVC_MAX_PCT=85.0,
        PVC_MIN_CLAIMS=32,
        PVC_EXCLUDE=["media-data"],
        PVC_MIN_FREE=floors,
    )


def test_a_claim_under_the_percentage_but_below_its_floor_is_flagged_at_once():
    breach, census, _ = pvc_fullness_verdict(
        [("valheim-server", "homelab", _PRE_UPDATE_PCT)],
        43,
        85,
        32,
        {"valheim-server": VALHEIM_FLOOR},
        {"valheim-server": _PRE_UPDATE_FREE},
    )
    assert "homelab/valheim-server 2.1G free < 3.0G" in breach
    assert "largest transient" in breach
    assert census == ""


def test_a_claim_with_its_transient_free_is_clean_and_the_floor_is_named_on_the_green_line():
    # The 20Gi shape after #1874: 7.7 G used, 12.3 G free, 39% full.
    breach, _, summary = pvc_fullness_verdict(
        [("valheim-server", "homelab", 38.5)],
        43,
        85,
        32,
        {"valheim-server": VALHEIM_FLOOR},
        {"valheim-server": 12.3 * GIB},
    )
    assert breach == ""
    assert "floors held: valheim-server 12.3G free >= 3.0G" in summary


def test_a_percentage_breach_and_a_floor_breach_are_both_reported():
    breach, _, _ = pvc_fullness_verdict(
        [("valheim-server", "homelab", 79.0), ("uptime-kuma-data", "homelab", 91.0)],
        43,
        85,
        32,
        {"valheim-server": VALHEIM_FLOOR},
        {"valheim-server": _PRE_UPDATE_FREE, "uptime-kuma-data": 0.1 * GIB},
    )
    assert breach.startswith("PVC over 85%: homelab/uptime-kuma-data 91%")
    assert "homelab/valheim-server 2.1G free < 3.0G" in breach


def test_a_declared_floor_whose_claim_reports_no_free_bytes_is_flagged_not_green():
    breach, _, _ = pvc_fullness_verdict(
        [("uptime-kuma-data", "homelab", 38.6)],
        43,
        85,
        32,
        {"valheim-server": VALHEIM_FLOOR},
        {"uptime-kuma-data": 0.5 * GIB},
    )
    assert "PVC_MIN_FREE names valheim-server" in breach
    assert "UNMONITORED" in breach


def test_no_declared_floor_leaves_the_verdict_as_it_was():
    breach, census, summary = pvc_fullness_verdict(
        [("uptime-kuma-data", "homelab", 38.6)], 43, 85, 32
    )
    assert breach == "" and census == ""
    assert "floors" not in summary


def test_the_check_reads_free_bytes_only_when_a_floor_is_declared(monkeypatch, cfg):
    asked = []

    def _vector(_cfg, promql, *a, **k):
        asked.append(promql)
        return [_pvc("valheim-server", 38.5)]

    monkeypatch.setattr(bridge.net, "prom_scalar", lambda _cfg, *a, **k: 43.0)
    monkeypatch.setattr(bridge.net, "prom_vector", _vector)
    ok, _ = checks.storage.check_pvc_fullness(
        replace(cfg, CLUSTER_PROM_URL="http://prometheus:9090", PVC_MIN_FREE="")
    )
    assert ok
    assert not any("available_bytes)" in q for q in asked)


def test_the_check_pages_on_the_pre_update_shape_and_clears_on_the_grown_claim(
    monkeypatch, cfg
):
    cfg = _arm(
        cfg,
        monkeypatch,
        [_pvc("valheim-server", _PRE_UPDATE_PCT)],
        [_pvc("valheim-server", _PRE_UPDATE_FREE)],
    )
    ok, msg = checks.storage.check_pvc_fullness(cfg)
    assert not ok
    assert "valheim-server 2.1G free < 3.0G" in msg
    cfg = _arm(
        cfg,
        monkeypatch,
        [_pvc("valheim-server", 38.5)],
        [_pvc("valheim-server", 12.3 * GIB)],
    )
    ok, msg = checks.storage.check_pvc_fullness(cfg)
    assert ok
    assert "floors held: valheim-server 12.3G free >= 3.0G" in msg


def test_a_floor_on_an_excluded_claim_is_flagged_rather_than_ignored(monkeypatch, cfg):
    # PVC_EXCLUDE drops the claim from both vectors, so its floor reads as unmonitored — the
    # decaying-list failure the PVC_EXCLUDE comment warns about, surfaced instead of silent.
    cfg = _arm(
        cfg,
        monkeypatch,
        [_pvc("media-data", 50.0), _pvc("uptime-kuma-data", 38.6)],
        [_pvc("media-data", 300 * GIB), _pvc("uptime-kuma-data", 0.5 * GIB)],
        floors="media-data=%d" % GIB,
    )
    ok, msg = checks.storage.check_pvc_fullness(cfg)
    assert not ok
    assert "PVC_MIN_FREE names media-data" in msg


def test_an_unparseable_floor_declaration_is_flagged(monkeypatch, cfg):
    cfg = _arm(
        cfg, monkeypatch, [_pvc("valheim-server", 38.5)], [], floors="valheim-server=3G"
    )
    ok, msg = checks.storage.check_pvc_fullness(cfg)
    assert not ok
    assert "parsed to no usable floor" in msg


def test_parse_drops_a_malformed_or_zero_entry_and_keeps_the_rest():
    assert parse_pvc_floors("valheim-server=3221225472, junk, x=0, y=-1") == {
        "valheim-server": 3221225472
    }
    assert parse_pvc_floors("") == {}


def _declared_floors() -> dict[str, int]:
    m = re.search(r"^\s*PVC_MIN_FREE:\s*(\S+)\s*$", ENV_SECRET.read_text(), re.M)
    assert m, "PVC_MIN_FREE is no longer rendered in env-secret.yaml.j2"
    return parse_pvc_floors(m.group(1))


def test_the_valheim_floor_is_declared_to_the_monitor_at_the_roles_own_transient():
    """The tree-derived half, by NAME: the valheim role declares the transient its updater
    stages, and the monitor's floor for that claim must be that number. A census that matched
    nothing would otherwise pass an `all()` while checking nothing."""
    defaults = (ROLES / "k8s" / "valheim" / "defaults" / "main.yml").read_text()
    claim = re.search(r"^valheim_k8s_server_claim:\s*(\S+)\s*$", defaults, re.M)
    transient = re.search(
        r"^valheim_k8s_server_update_transient_bytes:\s*(\d+)\s*$", defaults, re.M
    )
    assert claim and transient, (
        "valheim no longer declares its claim and transient plainly"
    )
    declared = _declared_floors()
    assert declared.get(claim.group(1)) == int(transient.group(1)), declared
    # The floor must be reachable: a transient larger than the claim would page forever.
    size = re.search(r"^valheim_k8s_server_size:\s*(\d+)Gi\s*$", defaults, re.M)
    assert size, "valheim_k8s_server_size is no longer a plain Gi value"
    assert int(transient.group(1)) < int(size.group(1)) * GIB
