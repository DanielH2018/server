#!/usr/bin/env python3
"""Guards that the Longhorn StorageClass never pins a replica count.

The `longhorn` role renders a `Setting` named `default-replica-count` from
k3s_longhorn_replica_count, and Flux applies it cleanly — reading the setting back
returns the rendered value. But a StorageClass parameter beats the global setting, so
every PVC can still bind at 3 replicas:

    kubectl -n longhorn-system get settings.longhorn.io default-replica-count  -> 1
    kubectl get sc longhorn -o jsonpath='{.parameters.numberOfReplicas}'       -> 3

Upstream's deploy/longhorn.yaml hardcodes `numberOfReplicas: "3"` in the
longhorn-storageclass ConfigMap, and a StorageClass parameter beats the global
setting. On a one-node cluster that means every volume asks for 3 replicas it can
never schedule and sits permanently Degraded — the exact "real fault buried in
expected noise" k3s_longhorn_replica_count exists to prevent.

The `longhorn` role replaces upstream's class with a kustomize patch of the ConfigMap
(files/patches/longhorn-storageclass.yaml, H1.5a), whose class omits the parameter so the
setting governs. The risk is re-syncing that patch from a newer upstream and pasting the
parameter back in, which is what these tests catch.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_storageclass.py
"""

from lib import yaml_fast
from lib.service_tiers import resolved_tier_lists

from _k8s_render import rendered_docs

from _helpers import load_defaults
from lib.repo_paths import K3S_ROLE, K8S_ROLES

LONGHORN_ROLE = K8S_ROLES / "longhorn"
CLASS_PATCH = LONGHORN_ROLE / "files" / "patches" / "longhorn-storageclass.yaml"
UPSTREAM = LONGHORN_ROLE / "files" / "upstream" / "longhorn.yaml"
CLASS_KEY = "storageclass.yaml"
LONGHORN_DEFAULTS = LONGHORN_ROLE / "defaults" / "main.yml"


def _class_in(configmap: dict) -> dict:
    """The StorageClass a `longhorn-storageclass` ConfigMap carries under its one key."""
    return yaml_fast.safe_load(configmap["data"][CLASS_KEY])


def _patched_configmap() -> dict:
    return yaml_fast.safe_load(CLASS_PATCH.read_text())


def _upstream_configmap() -> dict:
    for doc in yaml_fast.safe_load_all(UPSTREAM.read_text()):
        if (
            isinstance(doc, dict)
            and doc["kind"] == "ConfigMap"
            and doc["metadata"]["name"] == "longhorn-storageclass"
        ):
            return doc
    raise AssertionError("longhorn.yaml declares no ConfigMap longhorn-storageclass")


def _drift_from_upstream(upstream: dict, ours: dict) -> list[str]:
    """Each top-level key where `ours` differs from `upstream` beyond `numberOfReplicas`."""
    expected = {
        **upstream,
        "parameters": {
            k: v
            for k, v in upstream.get("parameters", {}).items()
            if k != "numberOfReplicas"
        },
    }
    return [
        key
        for key in sorted(set(expected) | set(ours))
        if expected.get(key) != ours.get(key)
    ]


def _rendered_setting_names() -> set[str]:
    """The name of every Longhorn `Setting` the `longhorn` role renders."""
    return {
        doc["metadata"]["name"]
        for role, _tpl, doc in rendered_docs()
        if role == "longhorn" and doc.get("kind") == "Setting"
    }


def test_storageclass_does_not_pin_a_replica_count():
    """The whole point of shipping our own class — see the module docstring."""
    sc = _class_in(_patched_configmap())
    assert "numberOfReplicas" not in sc.get("parameters", {}), (
        "files/patches/longhorn-storageclass.yaml must not set numberOfReplicas. A StorageClass "
        "parameter overrides the default-replica-count setting the role renders, so "
        "pinning it here silently ignores k3s_longhorn_replica_count."
    )


def test_storageclass_does_not_pin_a_backup_block_size():
    """Same trap as numberOfReplicas, one setting along.

    `backupBlockSize` became a StorageClass parameter in Longhorn 1.10, and a parameter beats
    the `default-backup-block-size` setting the role renders — so declaring it here would
    silently ignore k3s_longhorn_backup_block_size. Worse than the replica case, because
    StorageClass parameters are immutable AND a volume's block size is immutable: correcting a
    wrong value would mean recreating the class and then every volume provisioned through it.
    """
    sc = _class_in(_patched_configmap())
    assert "backupBlockSize" not in sc.get("parameters", {}), (
        "files/patches/longhorn-storageclass.yaml must not set backupBlockSize. A StorageClass parameter "
        "overrides the default-backup-block-size setting the role renders, so pinning it "
        "here silently ignores k3s_longhorn_backup_block_size."
    )


def test_role_renders_the_backup_block_size_setting():
    """With the parameter absent, the setting is the only lever that reaches new volumes."""
    assert "default-backup-block-size" in _rendered_setting_names(), (
        "The longhorn role must render default-backup-block-size. Every B2 cost Longhorn incurs is "
        "priced per block — prune, backup and restore alike — so this is the one setting "
        "that moves all three, and it reaches volumes only at creation time."
    )


def test_backup_block_size_is_a_value_longhorn_accepts():
    """Longhorn takes 2 or 16 MiB and nothing between; a typo here is silently wrong."""
    defaults = yaml_fast.safe_load(LONGHORN_DEFAULTS.read_text())
    assert defaults["k3s_longhorn_backup_block_size"] in (2, 16), (
        "k3s_longhorn_backup_block_size must be 2 or 16 (MiB). Longhorn rejects other "
        "values, and the Setting reports success regardless."
    )


def test_storageclass_stays_the_cluster_default():
    """Dropping this annotation breaks every PVC that omits storageClassName."""
    sc = _class_in(_patched_configmap())
    assert sc["metadata"]["name"] == "longhorn"
    assert sc["provisioner"] == "driver.longhorn.io"
    annotations = sc["metadata"].get("annotations", {})
    assert annotations.get("storageclass.kubernetes.io/is-default-class") == "true", (
        "longhorn must remain the default StorageClass — it replaces the one upstream "
        "marks default, so losing the annotation leaves the cluster with none."
    )


def test_role_still_renders_the_default_replica_count_setting():
    """With the parameter gone, the setting is the only lever left."""
    assert "default-replica-count" in _rendered_setting_names(), (
        "The longhorn role must keep rendering default-replica-count. Since the StorageClass no "
        "longer pins numberOfReplicas, that setting is what k3s_longhorn_replica_count "
        "actually reaches volumes through."
    )


def test_the_patch_targets_the_configmap_the_upstream_file_declares():
    # Kustomize refuses a build whose patch matches nothing, so a refresh that renames the
    # ConfigMap fails here before it fails the render. A patch has no order to get wrong, which
    # the deleted apply-after-install test used to guard.
    patch, upstream = _patched_configmap(), _upstream_configmap()
    assert patch["kind"] == "ConfigMap"
    assert patch["metadata"]["name"] == upstream["metadata"]["name"]
    assert patch["metadata"]["namespace"] == upstream["metadata"]["namespace"]
    assert set(patch["data"]) == {CLASS_KEY}


def test_the_class_differs_from_upstreams_only_by_the_replica_count():
    # Step 2 of the per-hop procedure (docs/longhorn-upgrade.md): a release that adds a
    # parameter fails here until the patch carries it.
    drift = _drift_from_upstream(
        _class_in(_upstream_configmap()), _class_in(_patched_configmap())
    )
    assert drift == [], (
        f"the patched class differs from the vendored upstream class in {drift}. Re-sync "
        "files/patches/longhorn-storageclass.yaml from longhorn.yaml, minus numberOfReplicas"
    )


def test_a_class_that_drops_a_parameter_or_keeps_the_replica_count_is_flagged():
    upstream = _class_in(_upstream_configmap())
    ours = _class_in(_patched_configmap())
    assert "numberOfReplicas" in upstream["parameters"]
    assert _drift_from_upstream(upstream, ours) == []
    dropped = {**ours, "parameters": {**ours["parameters"]}}
    del dropped["parameters"]["backupTargetName"]
    assert _drift_from_upstream(upstream, dropped) == ["parameters"]
    pinned = {**ours, "parameters": {**ours["parameters"], "numberOfReplicas": "3"}}
    assert _drift_from_upstream(upstream, pinned) == ["parameters"]


# ── backup routing lists ────────────────────────────────────────────────────────────────────
# Three hand-maintained lists of `namespace/pvcName` decide where every volume's backups go and
# how often: r2 (daily, Cloudflare), weekly (sharded across weekdays, B2), nobackup (neither).
# They are plain YAML with no schema and no cross-check, and the consequence of a typo is
# silent: a misspelt weekly entry leaves that volume in the daily group, so it keeps paying
# daily B2 transactions — the exact spend the weekday sharding exists to cut. Nothing would
# report it.

_ROUTING_LISTS = (
    "k3s_longhorn_r2_volumes",
    "k3s_longhorn_weekly_volumes",
    "k3s_longhorn_nobackup_volumes",
)


def _declared_pvcs() -> set[str]:
    """Every `namespace/name` a k8s role renders a PersistentVolumeClaim for.

    Every claim is rendered: a role declares it in `k8s_claims` or, for observability and
    media-volume, in its own template. Both arrive through `rendered_docs()`, so no defaults or
    tasks sweep is needed now that `k8s/volume-claim` is retired (#3387).

    The manifests are read RENDERED rather than text-scanned, because a PVC's name is a Jinja
    expression: scanning the templates would match `{{ ... }}` and find almost nothing, which
    reads as a clean result rather than as no coverage. The namespace is read off the rendered
    document for the same reason.
    """
    return {
        f"{doc['metadata'].get('namespace', 'homelab')}/{doc['metadata']['name']}"
        for _role, _tpl, doc in rendered_docs()
        if doc.get("kind") == "PersistentVolumeClaim"
        and doc.get("metadata", {}).get("name")
    }


# Members `_declared_pvcs` must find, so a render that stops producing claims fails by name
# rather than by a count. pihole-etc is a `longhorn-nobackup` claim from a literal
# `k8s_claims` list; traefik-acme comes from an expression-valued `k8s_claims`; grafana-data is
# observability's own template, in its own namespace.
_KNOWN_DECLARED_PVCS = frozenset(
    {
        "homelab/pihole-etc",
        "homelab/traefik-acme",
        "observability/grafana-data",
    }
)


def test_declared_pvcs_finds_every_known_claim():
    missing = _KNOWN_DECLARED_PVCS - _declared_pvcs()
    assert not missing, (
        f"_declared_pvcs no longer finds {sorted(missing)} — a render it reads moved, not "
        "that these claims disappeared"
    )


def test_backup_routing_lists_are_pairwise_disjoint():
    defaults = resolved_tier_lists(load_defaults(K3S_ROLE))
    lists = {name: set(defaults.get(name) or []) for name in _ROUTING_LISTS}
    overlaps = []
    for i, left in enumerate(_ROUTING_LISTS):
        for right in _ROUTING_LISTS[i + 1 :]:
            shared = lists[left] & lists[right]
            if shared:
                overlaps.append(f"{left} ∩ {right} = {sorted(shared)}")
    assert not overlaps, (
        "a volume in two routing lists has an ambiguous backup destination: "
        + "; ".join(overlaps)
    )


def test_backup_routing_lists_have_no_duplicates():
    defaults = resolved_tier_lists(load_defaults(K3S_ROLE))
    for name in _ROUTING_LISTS:
        entries = list(defaults.get(name) or [])
        dupes = sorted({e for e in entries if entries.count(e) > 1})
        assert not dupes, f"{name} lists {dupes} more than once"


def test_every_routed_volume_is_a_real_pvc():
    # A typo here does not fail anything at deploy — the label reconcile simply matches no
    # volume and moves on, leaving the PVC on whatever tier it was already in.
    defaults = resolved_tier_lists(load_defaults(K3S_ROLE))
    declared = _declared_pvcs()
    assert len(declared) > 20, (
        f"only found {len(declared)} PVC names — the collector stopped matching"
    )
    unknown = {
        f"{name}: {entry}"
        for name in _ROUTING_LISTS
        for entry in (defaults.get(name) or [])
        if entry not in declared
    }
    assert not unknown, (
        "these routing entries name no PVC declared by any k8s role, so they route "
        f"nothing: {sorted(unknown)}"
    )
