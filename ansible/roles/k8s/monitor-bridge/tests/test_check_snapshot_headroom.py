"""Snapshot-space headroom against a capped Longhorn volume's spec.snapshotMaxSize (#1627).

Every behaviour gets an accept/reject pair, so a rule that stopped matching fails its own test
rather than reading green. The last test is the non-vacuity half: it derives the capped volumes
from the TREE (which role patches `spec.snapshotMaxSize`) rather than from the declaration it
checks, so a second capped volume that nobody declared fails here instead of going unwatched.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_check_snapshot_headroom.py
"""

import re
from dataclasses import replace
from pathlib import Path

import bridge.net
import bridge.streaks
import checks.storage
import gates
import registry
from verdicts.storage import parse_snapshot_caps, snapshot_used_by_pvc

ROLES = Path(__file__).resolve().parents[3]
ENV_SECRET = Path(__file__).resolve().parents[1] / "templates" / "env-secret.yaml.j2"

# jellyfin-config, the one capped volume: 16 GiB, against 2,081,329,152 bytes of snapshots
# measured 2026-09-10 (12.1% of the cap). Used as the realistic clean case below.
JELLYFIN_CAP = 17179869184
JELLYFIN_USED = 2081329152


def _snapshot(volume, snapshot, size, pod="longhorn-manager-m6x4s"):
    return ({"volume": volume, "snapshot": snapshot, "pod": pod}, float(size))


def _volume(volume, pvc):
    return ({"volume": volume, "pvc": pvc, "pvc_namespace": "homelab"}, 8589934592.0)


def _arm(
    cfg, monkeypatch, snapshots, volumes, caps="jellyfin-config=%d" % JELLYFIN_CAP
):
    """State the two Prometheus vectors the check joins, keyed by which metric is asked for."""

    def _vector(_cfg, promql, *a, **k):
        return snapshots if "snapshot_actual_size" in promql else volumes

    monkeypatch.setattr(bridge.net, "prom_vector", _vector)
    return replace(cfg, SNAPSHOT_CAPS=caps)


def test_a_capped_volume_well_under_its_cap_is_clean(monkeypatch, cfg):
    cfg = _arm(
        cfg,
        monkeypatch,
        [_snapshot("pvc-jf", "autodeploy-jellyfin-1", JELLYFIN_USED)],
        [_volume("pvc-jf", "jellyfin-config")],
    )
    ok, msg = checks.storage.check_snapshot_headroom(cfg)
    assert ok
    assert "1 capped volume(s) under 90% of cap" in msg
    assert "jellyfin-config at 12.1%" in msg


def test_a_capped_volume_past_the_warn_ratio_is_flagged(monkeypatch, cfg):
    cfg = _arm(
        cfg,
        monkeypatch,
        [_snapshot("pvc-jf", "weekly-b-1", int(JELLYFIN_CAP * 0.91))],
        [_volume("pvc-jf", "jellyfin-config")],
    )
    cfg = replace(cfg, SNAPSHOT_CAP_CONSECUTIVE=1)
    ok, msg = checks.storage.check_snapshot_headroom(cfg)
    assert not ok
    assert "jellyfin-config 91.0% of cap" in msg
    assert "refuses new snapshots at the cap" in msg


def test_a_breach_holds_up_until_the_streak_then_pages(monkeypatch, cfg):
    """A prune leaves markRemoved snapshots counted for a cycle, so one breach must not page."""
    cfg = _arm(
        cfg,
        monkeypatch,
        [_snapshot("pvc-jf", "weekly-b-1", JELLYFIN_CAP)],
        [_volume("pvc-jf", "jellyfin-config")],
    )
    held_ok, held_msg = checks.storage.check_snapshot_headroom(cfg)
    assert held_ok
    assert "down streak 1/3 (prune lag grace)" in held_msg
    assert checks.storage.check_snapshot_headroom(cfg)[0]
    assert not checks.storage.check_snapshot_headroom(cfg)[0]


def test_recovery_resets_the_streak(monkeypatch, cfg):
    cfg = _arm(
        cfg,
        monkeypatch,
        [_snapshot("pvc-jf", "weekly-b-1", JELLYFIN_CAP)],
        [_volume("pvc-jf", "jellyfin-config")],
    )
    checks.storage.check_snapshot_headroom(cfg)
    assert bridge.streaks._down_streaks["snapshot_headroom"] == 1
    cfg = _arm(
        cfg,
        monkeypatch,
        [_snapshot("pvc-jf", "weekly-b-1", JELLYFIN_USED)],
        [_volume("pvc-jf", "jellyfin-config")],
    )
    assert checks.storage.check_snapshot_headroom(cfg)[0]
    assert bridge.streaks._down_streaks["snapshot_headroom"] == 0


def test_a_declared_cap_with_no_snapshot_series_is_flagged_not_green(monkeypatch, cfg):
    """Fail closed: the capped volume is snapshotted on every deploy, so empty means blind."""
    cfg = _arm(cfg, monkeypatch, [], [])
    cfg = replace(cfg, SNAPSHOT_CAP_CONSECUTIVE=1)
    ok, msg = checks.storage.check_snapshot_headroom(cfg)
    assert not ok
    assert "no snapshot series for capped volume(s) jellyfin-config" in msg


def test_no_declared_cap_is_clean_and_queries_nothing(monkeypatch, cfg):
    """The inert state until a cap is declared — and it must not report a fault."""

    def _explode(*a, **k):
        raise AssertionError("queried Prometheus with no cap declared")

    monkeypatch.setattr(bridge.net, "prom_vector", _explode)
    ok, msg = checks.storage.check_snapshot_headroom(replace(cfg, SNAPSHOT_CAPS=""))
    assert ok
    assert "no capped Longhorn volumes declared" in msg


def test_an_unparseable_cap_declaration_is_flagged(monkeypatch, cfg):
    cfg = replace(cfg, SNAPSHOT_CAPS="jellyfin-config=0", SNAPSHOT_CAP_CONSECUTIVE=1)
    ok, msg = checks.storage.check_snapshot_headroom(cfg)
    assert not ok
    assert "parsed to no usable cap" in msg
    # `0` is Longhorn's UNCAPPED value and the fleet default, so it is never a cap of zero.
    assert parse_snapshot_caps("jellyfin-config=0") == {}
    assert parse_snapshot_caps("jellyfin-config=%d" % JELLYFIN_CAP) == {
        "jellyfin-config": JELLYFIN_CAP
    }


def test_a_snapshot_reported_by_both_managers_counts_once():
    """The `longhorn` job scrapes both manager pods; a sum would read double the real size."""
    both = [
        _snapshot("pvc-jf", "weekly-b-1", 1000, pod="longhorn-manager-a"),
        _snapshot("pvc-jf", "weekly-b-1", 1000, pod="longhorn-manager-b"),
    ]
    assert snapshot_used_by_pvc(both, [_volume("pvc-jf", "jellyfin-config")]) == {
        "jellyfin-config": 1000
    }
    two_snapshots = [
        _snapshot("pvc-jf", "weekly-b-1", 1000),
        _snapshot("pvc-jf", "weekly-b-2", 1000),
    ]
    assert snapshot_used_by_pvc(
        two_snapshots, [_volume("pvc-jf", "jellyfin-config")]
    ) == {"jellyfin-config": 2000}


def test_the_check_is_registered_and_gated_by_prometheus():
    names = {c.name for c in registry.build_checks({})}
    assert "snapshot_headroom" in names
    # Its own absent-series branch pages, so a Prometheus outage has to suppress it or one root
    # cause lights two monitors.
    assert "snapshot_headroom" in gates.PROM_DEPENDENT


def _declared_caps() -> dict[str, int]:
    """The caps SNAPSHOT_CAPS declares in the rendered env, as a map."""
    match = re.search(r"^\s*SNAPSHOT_CAPS:\s*(\S.*)$", ENV_SECRET.read_text(), re.M)
    assert match, "no SNAPSHOT_CAPS entry in env-secret.yaml.j2"
    return parse_snapshot_caps(match.group(1).strip().strip("\"'"))


def _roles_that_cap_a_volume() -> dict[str, str]:
    """Role name -> the claim it caps, derived from the tree rather than from the declaration.

    A role caps a volume by writing `spec.snapshotMaxSize` (roles/k8s/jellyfin/tasks/main.yml is
    the only one today). The census matches the FIELD NAME anywhere under a k8s role's tasks,
    minus the roles that only READ it, rather than jellyfin's `kubectl patch` payload: a future
    role capping a volume through `kubernetes.core.k8s` or `--type=json` writes the same field
    with different syntax, and a payload-shaped pattern would miss it and pass. The claim comes
    from the role's own `<role>_k8s_claim` default, which is the variable the write resolves its
    volume through.
    """
    # The known readers. volume-snapshot reads the cap to gate its pre-deploy snapshot and never
    # writes one; volume-revert reads the same volumes. A role added here needs the reason in
    # writing, because every entry narrows what this guard can see.
    READERS = {"volume-snapshot", "volume-revert"}
    census = {}
    for tasks in sorted((ROLES / "k8s").glob("*/tasks/*.yml")):
        if (
            "snapshotMaxSize" not in tasks.read_text()
            or tasks.parents[1].name in READERS
        ):
            continue
        role = tasks.parents[1].name
        defaults = (tasks.parents[1] / "defaults" / "main.yml").read_text()
        claim = re.search(
            r"^%s_k8s_claim:\s*(\S+)" % re.escape(role.replace("-", "_")),
            defaults,
            re.M,
        )
        assert claim, "%s patches a cap but declares no <role>_k8s_claim" % role
        census[role] = claim.group(1)
    return census


def test_every_volume_a_role_caps_is_declared_to_the_monitor():
    capped = _roles_that_cap_a_volume()
    # Non-vacuity by NAME, not by count: a census that matched nothing would otherwise pass the
    # subset check below against an empty set, which is how a guard reads green while checking
    # nothing. jellyfin is the one role that caps a volume (16 GiB on jellyfin-config).
    assert capped.get("jellyfin") == "jellyfin-config", capped
    declared = _declared_caps()
    assert set(capped.values()) <= set(declared), (
        "capped in the tree, unwatched by the monitor: %s"
        % sorted(set(capped.values()) - set(declared))
    )
    # The cap VALUE, derived the way the jellyfin role derives it — 2 x the PVC size, the
    # smallest cap Longhorn accepts — so a PVC resize that moves the cap fails here rather than
    # leaving the monitor measuring against a stale number.
    jellyfin_defaults = (
        ROLES / "k8s" / "jellyfin" / "defaults" / "main.yml"
    ).read_text()
    size = re.search(r"^jellyfin_k8s_size:\s*(\d+)Gi\s*$", jellyfin_defaults, re.M)
    assert size, "jellyfin_k8s_size is no longer a plain Gi value"
    assert declared["jellyfin-config"] == int(size.group(1)) * 2 * 1024**3


def test_the_pending_push_token_is_wired_to_a_monitor_declaration():
    """The `| default('')` form dodges test_every_push_token_env_is_wired_to_a_monitor's regex.

    That guard matches `KUMA_PUSH_X: "{{ var }}"` exactly, so a token rendered with a default is
    invisible to it — and an invisible token is how a check ends up pushing to a monitor nobody
    declared. This is the replacement: the env var and the Kuma declaration must name the SAME
    variable, and the declaration must be guarded on it so an absent secret declares no monitor.
    """
    env_token = re.search(
        r"KUMA_PUSH_SNAPSHOT_HEADROOM:\s*\"\{\{\s*([a-z0-9_]+)\s*\|\s*default\(''\)\s*\}\}\"",
        ENV_SECRET.read_text(),
    )
    assert env_token, (
        "KUMA_PUSH_SNAPSHOT_HEADROOM is not rendered from a defaulted variable"
    )
    monitors = (
        ROLES / "k8s" / "uptime-kuma" / "templates" / "static-monitors.yaml.j2"
    ).read_text()
    var = env_token.group(1)
    assert "{%% if %s | default('') %%}" % var in monitors
    assert '"push_token": "{{ %s }}"' % var in monitors
