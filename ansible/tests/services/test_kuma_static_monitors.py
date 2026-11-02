"""Guards for the AutoKuma static entity files.

The static-monitors Secret is the alerting spine's declaration set. Two silent failure modes
get guards here:

- A monitor without a notification link is created and never pages (the macro's
  conditional-emission trap, a per-file responsibility).
- A push monitor with retries > 0 flaps on a single missed cron beat. This file is the
  SOLE guard of that rule.

Every entity also carries the fields AutoKuma v2.0.0 parses (`type` mandatory), and ids —
the filenames — must stay unique.
"""

from _helpers import image_tag
from _kuma_entities import (
    ROLE_DEFAULTS,
    _entities,
    bridge_env,
)

# Entity types the Secret declares that are not monitors: the two notifications and the two
# tags the notification templates read. Every guard below that iterates monitors skips these.
NOT_MONITORS = frozenset({"notification", "tag"})


def test_every_entity_parses_and_declares_a_type():
    entities = _entities()
    assert entities, "no entities rendered — the Secret went empty"
    for name, entity in entities.items():
        assert name.endswith(".json"), (
            f"{name}: filename must carry the .json extension"
        )
        assert entity.get("type"), f"{name}: missing the mandatory `type` field"


def test_every_monitor_is_linked_to_the_discord_notification():
    # The silent-failure mode: an unlinked monitor is created and never pages. Notifications
    # themselves are the link's target, not carriers of one.
    for name, entity in _entities().items():
        if entity["type"] in NOT_MONITORS:
            continue
        assert "discord" in entity.get("notification_name_list", []), (
            f"{name}: monitor has no discord notification link — it would never page"
        )


def test_push_monitors_never_retry():
    # The only enforcement of this rule: a cron-fed push
    # monitor with retries > 0 turns one missed beat into interval*retries of silence
    # instead of a page.
    for name, entity in _entities().items():
        if entity["type"] != "push":
            continue
        retries = entity.get("max_retries", entity.get("maxretries"))
        assert retries == 0, (
            f"{name}: push monitor must set max_retries 0, got {retries}"
        )


# Monitors deliberately held at resendInterval 0, with the condition that lifts each hold. A
# hold is for a tile that is down, cannot recover without an event no operator controls, and
# would otherwise resend into a channel until it gets muted. Enumerated here rather than left to
# the template so that adding one is a visible decision and forgetting to remove one is a test
# that keeps naming it.
#
# An empty set is the normal state — it means every push monitor re-notifies.
RESEND_HELD: set[str] = set()


def test_autokuma_pin_carries_resend_interval_on_push_monitors():
    """The guard below asserts a field only some AutoKuma versions keep.

    In AutoKuma v2.0.0, `resendInterval` was declared on exactly three monitor variants —
    MonitorHttp, MonitorJsonQuery and MonitorKeyword. MonitorPush had no such field, so serde
    dropped it as unknown and the value never reached Kuma: push tiles notified once on the
    down transition and then stayed silent. Upstream moved the field into
    `with_monitor_common_fields_impl!` in 2.1.0-rc.1 ("Fix resend_interval missing for most
    monitor types, see #152"), where every variant carries it.

    Below that version the assertions in test_push_monitors_re_notify_while_still_down are
    statements about this repo rather than about what deploys. This test fails when the pin
    moves off a version known to carry the field, so whoever moves it re-reads the paragraph
    above and checks whether the push tiles are still re-notifying.
    """
    # fact: ansible/roles/k8s/uptime-kuma/CLAUDE.md#Traps
    # Versions verified BY READING kuma-client/src/models/monitor.rs at the tag, not by trusting
    # a release note. Add a version here only after doing the same.
    CARRIES_RESEND_ON_PUSH = {"2.1.0-rc.2"}
    pinned = ROLE_DEFAULTS["uptime_kuma_k8s_autokuma_image"]
    tag = image_tag(pinned)
    assert tag in CARRIES_RESEND_ON_PUSH, (
        f"AutoKuma pin moved to {pinned!r} — re-verify in that tag's monitor.rs that "
        "`resend_interval` is still in the shared field set and not per-variant, then add the "
        "tag above. On a version that lost it, every push monitor pages exactly once per outage."
    )


def test_push_monitors_re_notify_while_still_down():
    # Kuma's `resendInterval` default is 0, meaning "notify once on the down transition, then
    # never again", which leaves a tile that stays down silent. Asserted for every push monitor
    # so a new one cannot be added without it.
    for name, entity in _entities().items():
        if entity["type"] != "push":
            continue
        resend = entity.get("resendInterval")
        if entity.get("name") in RESEND_HELD:
            assert resend == 0, (
                f"{name} is in RESEND_HELD, so it must be held at 0 — a hold that drifts to a "
                f"non-zero value is worse than no hold, got {resend!r}"
            )
            continue
        assert isinstance(resend, int) and resend > 0, (
            f"{name}: push monitor needs a non-zero resendInterval or a sustained outage "
            f"pages exactly once, got {resend!r}"
        )


def test_both_managed_notifications_are_defined():
    entities = _entities()
    notifications = {n for n, e in entities.items() if e["type"] == "notification"}
    assert notifications == {"discord.json", "email.json"}


def test_notification_configs_declare_apply_existing():
    """A notification config without `applyExisting` can never compare equal to what Kuma stores.

    Kuma's save() forces the key in on every write (`notification.applyExisting = false` before
    `JSON.stringify`, server/notification.js), and AutoKuma's `config_eq` compares the NUMBER of
    config keys after dropping six ignored ones — a set that does not include applyExisting. So
    a declaration that omits it is permanently one key short, never matches, and is rewritten on
    every sync pass.

    The value must be false: Kuma reads the flag before forcing it (`applyExisting || false`),
    and true would attach the notification to every existing monitor.
    """
    for name, entity in _entities().items():
        if entity["type"] != "notification":
            continue
        config = entity.get("config", {})
        assert config.get("applyExisting") is False, (
            f"{name}: notification config must declare `applyExisting: false` or AutoKuma "
            f"rewrites it on every sync pass forever, got {config.get('applyExisting')!r}"
        )


# Monitors whose failure is invisible on Discord alone and cannot wait for someone to notice a
# muted channel — the tier that also mails. Enumerated rather than pattern-matched: "has 'B2' in
# the name" is exactly the rule that would put B2's headroom tile on this tier and leave R2's off
# it, though both watch a Longhorn backup target's remaining free-tier capacity.
#
# M6 batch A5's five storage tiles (Longhorn Volume Redundancy, k3s PVC Fullness, Longhorn
# Snapshot Headroom, Kubelet CSI Mount Read-Only and etcd DB Size) left the tier when Cutover-a1
# deleted them. Their critical rules page through Alertmanager once Cutover-b drops `shadow`.
#
# M6 batch A1's two critical tiles, Root Disk and UPS Battery Health, left it the same way in
# Cutover-a1. `HostDiskFull` and the five `UPS*` rules are critical, so Alertmanager mails them
# once A1's Cutover-b drops `shadow`.
#
# M6 batch B1's two critical tiles, k3s Longhorn Backup and Off-box etcd Snapshot, left it the same
# way in Cutover-1a. `LonghornBackupHealthFailed`, `EtcdSnapshotOffboxFailed` and the two
# `NotReporting` rules are critical, so Alertmanager mails them once B1's Cutover-1b drops
# `shadow`.
#
# M6 batch B8's three tiles on the tier, Homelab Edge (all-clear), k3s Mosquitto (VIP) and
# Pi-hole k8s DNS, left it with the other 12 probe tiles in Cutover-a1. The edge probe's rule
# `ExtraProbeFailedCritical` is critical, and so are the Mosquitto and Pi-hole probes while their
# entries are home-critical, so Alertmanager mails them once B8's Cutover-b drops `shadow`.
#
# M6 batch B5's Daniel Pi SD Health left the tier when Cutover-1a deleted it. `PiSdHealthFailed`
# and `PiSdHealthNotReporting` are critical, so Alertmanager mails them once Cutover-1b drops
# `shadow`.
#
# M6 batch B4's three tiles on the tier, etcd Restore Drill (full) and the two UPS Secondary tiles,
# left it when Cutover-1a deleted them. `EtcdRestoreDrillFailed`, `EtcdRestoreDrillNotReporting` and
# the four `UpsSecondary*` rules are critical, so Alertmanager mails them once Cutover-1b drops
# `shadow`.
#
# M6 batch A6's six tiles on the tier (B2 Reachable, B2 Free Tier Headroom, R2 Free Tier
# Headroom, SMART Data / Health, Discord Delivery and WAN Reachable) left it when Cutover-1a
# deleted every monitor-bridge tile. `B2Unreachable`, `B2FreeTierHeadroomLow`,
# `R2FreeTierHeadroomLow`, `SmartHealthFailing`, `DiscordDeliveryFailing` and `WANUnreachable` are
# critical, so Alertmanager mails them once Cutover-1b drops `shadow`. The set is empty because
# no Kuma tile mails any more.
EMAIL_TIER: set[str] = set()


def test_the_deleted_b2_b4_and_b6b_tiles_are_not_declared():
    # M6 batches B2, B4 and B6b's combined Cutover-1a deleted these 20 hand-written tiles. RED if
    # one is declared again: AutoKuma would re-create it, and its producer (still pushing until
    # Cutover-2) would feed a tile whose rule already pages from Alertmanager.
    declared = {e["name"] for e in _entities().values()}
    assert not declared & {
        # B2
        "Docs Refresh",
        "Homelab Evals",
        "Secret Rotation",
        "Setup Plane Drift (agent hosts)",
        "Loki Read Route (daniel-box)",
        "Loki Read Route (daniel-server)",
        # B4
        "UPS Secondary (daniel-box)",
        "UPS Secondary (daniel-server)",
        "Fake Remux Scan",
        "Fake Remux Replace",
        "MKV Attachment Repair",
        "etcd Restore Drill (full)",
        "etcd Restore Drill (egress fence)",
        "Renovate Notifier \u2014 Alive",
        "Renovate Agent \u2014 Alive",
        # B6b
        "CrowdSec Remote Allowlist",
        "CrowdSec AppSec",
        "CrowdSec Home Allowlist",
        "Configarr Sync",
        "Janitorr Errors",
    }
    # Non-vacuity by name: a render that declared nothing would pass the line above.
    assert "Status Page Sync" in declared


def test_the_deleted_b6c_tile_is_not_declared():
    # RED if the tile is declared again: AutoKuma would re-create it beside the RegistryGc* rules
    # that replace it (M6 batch B6c). Cutover-2 stopped registry-gc.sh pushing into its token.
    declared = {e["name"] for e in _entities().values()}
    assert "Registry GC" not in declared
    # Non-vacuity by name: a render that declared nothing would pass the line above.
    assert "Status Page Sync" in declared


def test_the_deleted_b1_tiles_are_not_declared():
    # M6 batch B1's Cutover-1a deleted these five hand-written tiles. RED if one is declared
    # again: AutoKuma would re-create it, and its producer (still pushing until Cutover-2)
    # would feed a tile whose rule already pages from Alertmanager.
    declared = {e["name"] for e in _entities().values()}
    assert not declared & {
        "k3s Longhorn Backup",
        "Off-box etcd Snapshot",
        "Manifest Prune Drift",
        "Release Staleness Drift",
        "Live Object Drift",
    }
    assert "Status Page Sync" in declared


def test_the_deleted_b3_tiles_are_not_declared():
    # RED if either tile is declared again: AutoKuma would re-create it beside the
    # GitHubRulesetDrift* and GitHubInteractionLimit* rules that replace it (M6 batch B3).
    declared = {e["name"] for e in _entities().values()}
    assert not declared & {"GitHub Ruleset Drift", "GitHub Interaction Limit"}
    # Non-vacuity by name: a render that declared nothing would pass the line above.
    assert "Status Page Sync" in declared


# The 20 tiles M6 batch A6's Cutover-1a deleted: every tile monitor-bridge fed. Cutover-2 stops
# the push and removed the loop.
A6_TILES_DELETED = {
    "WAN Reachable",
    "Discord Delivery",
    "R2 Free Tier Headroom",
    "Cloudflare IP Drift",
    "Healthchecks.io Console Drift",
    "B2 Reachable",
    "B2 Free Tier Headroom",
    "Arr Queue Warnings",
    "GitOps Deploy — Alive",
    "GitOps Deploy — Status",
    "Host Packages — Status",
    "etcd Restore Drill",
    "Home Assistant Automations",
    "n8n Prod Workflows",
    "Bazarr Health",
    "Prowlarr Indexers",
    "SMART Data / Health",
    "k3s Speedtest",
    "Swallowed Push Verdicts",
    "Prometheus Reachable",
}


def test_the_deleted_a6_tiles_are_not_declared():
    # RED if the template declares a bridge tile again: AutoKuma would re-create it beside the
    # bridge-exporter.yaml rule that replaces it (M6 batch A6), and the bridge pushes into its
    # token until Cutover-2.
    declared = {e["name"] for e in _entities().values()}
    assert not declared & A6_TILES_DELETED
    # Non-vacuity by name: a render that declared nothing would pass the line above.
    assert "Status Page Sync" in declared


def test_no_tile_carries_a_monitor_bridge_push_token():
    # RED if a tile accepts a `monitor_bridge_*_push_token` again. M6 batch A6 retired all 20:
    # nothing pushes into them, and A6-9 removed the keys. Under the test render a token is
    # `stub-<variable name>`, so the variable is readable off the tile.
    tokens = {e.get("push_token", "") for e in _entities().values()}
    assert not [t for t in tokens if t.startswith("stub-monitor_bridge_")]
    # Non-vacuity by name: a render with no push tiles would pass the line above.
    assert "stub-kuma_status_page_sync_push_token" in tokens


def test_bridge_push_interval_is_a_multiple_of_the_loop():
    # The window must be a whole number of bridge cycles, and must tolerate more than one missed
    # push — at exactly 2x it is back to the 600s window that flaps. The loop's cadence is read
    # from the Secret the bridge pod receives, so a changed INTERVAL re-checks the window here.
    loop = int(bridge_env()["INTERVAL"])
    want = ROLE_DEFAULTS["uptime_kuma_k8s_bridge_push_interval"]
    assert want % loop == 0, "%s is not a whole number of %ss bridge cycles" % (
        want,
        loop,
    )
    assert want >= 3 * loop, "%s tolerates fewer than two missed pushes" % want


def test_email_tier_membership_is_exactly_declared():
    # Both directions matter. A monitor dropping off the tier loses the leg that survives a
    # degraded Discord; one drifting onto it dilutes an inbox that has to stay worth reading.
    named = {
        e["name"]
        for e in _entities().values()
        if e["type"] not in NOT_MONITORS
        and "email" in e.get("notification_name_list", [])
    }
    assert named == EMAIL_TIER


# The probe HOST matters as much as the path. Traefik ranks routers by rule length.
# `PathPrefix(`/.well-known/traefik-edge-selfcheck`)` is 48 characters, and littlelink's
# `Host(`www...`) || Host(`www.local...`)` is 68, so on `www.local` the edge all-clear probe gets
# littlelink's own 404 body. Any host that fronts a workload can lose the same way, and it can
# start losing later, when someone lengthens that workload's rule.
#
# `edge-selfcheck.local` fronts nothing and is pinned to the ingress VIP in the probing pod's
# hostAliases (`test_extra_probe_targets.py` holds the blackbox-exporter pin), so there is no
# Host() router for the path router to lose to. This guard is what keeps that true: it fails
# the moment any role renders an IngressRoute naming the probe host. It matches on the label
# alone, so it holds whatever `domain` the render resolves to.
_ALL_CLEAR_PROBE_LABEL = "edge-selfcheck.local."


def _route_rules_naming(label: str) -> list[str]:
    from _k8s_render import rendered_docs

    hits = []
    for role, _tpl, doc in rendered_docs():
        if doc.get("kind") != "IngressRoute":
            continue
        for route in doc["spec"].get("routes", []) or []:
            if label in route.get("match", ""):
                hits.append(f"{role}/{doc['metadata']['name']}: {route['match']}")
    return hits


def test_a_host_that_fronts_a_workload_is_flagged():
    # www.local is a host whose router outranks the path router. It is
    # also this rule's non-vacuity check: an IngressRoute corpus that stopped rendering would
    # make the assertion below pass while inspecting nothing.
    assert _route_rules_naming("www.local."), (
        "no IngressRoute names www.local — littlelink fronts it, so this guard has stopped "
        "seeing the corpus it is meant to inspect"
    )


def test_the_all_clear_probe_host_fronts_no_route():
    offenders = _route_rules_naming(_ALL_CLEAR_PROBE_LABEL)
    assert not offenders, (
        "an IngressRoute now fronts the all-clear probe host, so a longer Host() rule can "
        "outrank the edge self-check path and the probe reads that app's response instead:\n  "
        + "\n  ".join(offenders)
    )
