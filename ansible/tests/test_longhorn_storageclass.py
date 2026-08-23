#!/usr/bin/env python3
"""Guards that the Longhorn StorageClass never pins a replica count again.

The failure this encodes actually happened (daniel-box, 2026-08-01). The k3s role
patches `settings.longhorn.io default-replica-count` to k3s_longhorn_replica_count
(1, until daniel-server joins at slice 7), and that patch applied cleanly — reading
the setting back returned 1. But every PVC still bound at 3 replicas:

    kubectl -n longhorn-system get settings.longhorn.io default-replica-count  -> 1
    kubectl get sc longhorn -o jsonpath='{.parameters.numberOfReplicas}'       -> 3

Upstream's deploy/longhorn.yaml hardcodes `numberOfReplicas: "3"` in the
longhorn-storageclass ConfigMap, and a StorageClass parameter beats the global
setting. On a one-node cluster that means every volume asks for 3 replicas it can
never schedule and sits permanently Degraded — the exact "real fault buried in
expected noise" k3s_longhorn_replica_count exists to prevent. It failed slice 0's
exit criteria; see docs/k3s-migration/slice-0-cluster-foundation.md.

The fix replaces upstream's class with files/longhorn-storageclass.yaml, which omits
the parameter so the setting governs. The risk now is re-syncing that file from a
newer upstream and pasting the parameter back in, which is what these tests catch.

Run: uv run pytest ansible/tests/test_longhorn_storageclass.py
"""

import yaml

from _k8s_render import rendered_docs

from _helpers import K8S_ROLES, SETUP_ROLES, leaf_tasks

K3S = SETUP_ROLES / "k3s"
STORAGECLASS = K3S / "files" / "longhorn-storageclass.yaml"


def _tasks():
    """Every task the role runs, in the order it runs them.

    main.yml became a list of import_tasks in the 2026-08-15 split, so the imports are
    expanded here — in import order, not alphabetically. The ordering assertions below
    compare task positions, so a wrong order would silently invert what they check.
    """
    tasks: list[dict] = []
    for entry in yaml.safe_load((K3S / "tasks" / "main.yml").read_text()) or []:
        imported = entry.get("ansible.builtin.import_tasks")
        if not imported:
            tasks += leaf_tasks([entry])
            continue
        loaded = yaml.safe_load((K3S / "tasks" / imported).read_text()) or []
        tasks += leaf_tasks(loaded)
    return tasks


def _commands(tasks: list[dict]) -> list[str]:
    """The `cmd:` of every command task, positionally aligned with `tasks`."""
    return [t.get("ansible.builtin.command", {}).get("cmd", "") for t in tasks]


def _index_of(commands: list[str], matches) -> int:
    """Position of the first matching command, as a failed assert rather than a raise."""
    for i, cmd in enumerate(commands):
        if matches(cmd):
            return i
    raise AssertionError(
        "No k3s role task runs a command matching this predicate — a task was renamed "
        "or restructured, and the ordering guarantee below is no longer being checked."
    )


def test_storageclass_does_not_pin_a_replica_count():
    """The whole point of shipping our own class — see the module docstring."""
    sc = yaml.safe_load(STORAGECLASS.read_text())
    assert "numberOfReplicas" not in sc.get("parameters", {}), (
        "longhorn-storageclass.yaml must not set numberOfReplicas. A StorageClass "
        "parameter overrides the default-replica-count setting the role patches, so "
        "pinning it here silently ignores k3s_longhorn_replica_count."
    )


def test_storageclass_does_not_pin_a_backup_block_size():
    """Same trap as numberOfReplicas, one setting along.

    `backupBlockSize` became a StorageClass parameter in Longhorn 1.10, and a parameter beats
    the `default-backup-block-size` setting the role patches — so declaring it here would
    silently ignore k3s_longhorn_backup_block_size. Worse than the replica case, because
    StorageClass parameters are immutable AND a volume's block size is immutable: correcting a
    wrong value would mean recreating the class and then every volume provisioned through it.
    """
    sc = yaml.safe_load(STORAGECLASS.read_text())
    assert "backupBlockSize" not in sc.get("parameters", {}), (
        "longhorn-storageclass.yaml must not set backupBlockSize. A StorageClass parameter "
        "overrides the default-backup-block-size setting the role patches, so pinning it "
        "here silently ignores k3s_longhorn_backup_block_size."
    )


def test_role_patches_the_backup_block_size_setting():
    """With the parameter absent, the setting is the only lever that reaches new volumes."""
    commands = _commands(_tasks())
    assert any(
        "settings.longhorn.io default-backup-block-size" in c for c in commands
    ), (
        "The role must patch default-backup-block-size. Every B2 cost Longhorn incurs is "
        "priced per block — prune, backup and restore alike — so this is the one setting "
        "that moves all three, and it reaches volumes only at creation time."
    )


def test_backup_block_size_is_a_value_longhorn_accepts():
    """Longhorn takes 2 or 16 MiB and nothing between; a typo here is silently wrong."""
    defaults = yaml.safe_load((K3S / "defaults" / "main.yml").read_text())
    assert defaults["k3s_longhorn_backup_block_size"] in (2, 16), (
        "k3s_longhorn_backup_block_size must be 2 or 16 (MiB). Longhorn rejects other "
        "values, and the patch reports success regardless."
    )


def test_storageclass_stays_the_cluster_default():
    """Dropping this annotation breaks every PVC that omits storageClassName."""
    sc = yaml.safe_load(STORAGECLASS.read_text())
    assert sc["metadata"]["name"] == "longhorn"
    assert sc["provisioner"] == "driver.longhorn.io"
    annotations = sc["metadata"].get("annotations", {})
    assert annotations.get("storageclass.kubernetes.io/is-default-class") == "true", (
        "longhorn must remain the default StorageClass — it replaces the one upstream "
        "marks default, so losing the annotation leaves the cluster with none."
    )


def test_role_still_patches_the_default_replica_count_setting():
    """With the parameter gone, the setting is the only lever left."""
    commands = _commands(_tasks())
    assert any("settings.longhorn.io default-replica-count" in c for c in commands), (
        "The role must keep patching default-replica-count. Since the StorageClass no "
        "longer pins numberOfReplicas, that setting is what k3s_longhorn_replica_count "
        "actually reaches volumes through."
    )


def test_storageclass_is_applied_after_upstream_longhorn():
    """Ordering is load-bearing: upstream's apply re-pins the ConfigMap every run."""
    commands = _commands(_tasks())
    install = _index_of(commands, lambda c: "deploy/longhorn.yaml" in c)
    apply_class = _index_of(
        commands, lambda c: "apply -f" in c and "longhorn-storageclass.yaml" in c
    )
    patch_configmap = _index_of(
        commands, lambda c: "patch configmap longhorn-storageclass" in c
    )
    assert install < apply_class, (
        "The StorageClass must be applied AFTER `kubectl apply -f longhorn.yaml`, "
        "which recreates upstream's class and ConfigMap."
    )
    assert install < patch_configmap, (
        "The ConfigMap patch must run AFTER `kubectl apply -f longhorn.yaml`, which "
        're-applies upstream\'s numberOfReplicas: "3" into it on every run.'
    )


# ── backup routing lists ────────────────────────────────────────────────────────────────────
# Three hand-maintained lists of `namespace/pvcName` decide where every volume's backups go and
# how often: r2 (daily, Cloudflare), weekly (sharded across weekdays, B2), nobackup (neither).
# They are plain YAML with no schema and no cross-check, and the consequence of a typo is
# silent: a misspelt weekly entry leaves that volume in the daily group, so it keeps paying
# daily B2 transactions — the exact spend the weekday sharding exists to cut, and the sixth cap
# event is what sharding was the response to. Nothing would report it.

_ROUTING_LISTS = (
    "k3s_longhorn_r2_volumes",
    "k3s_longhorn_weekly_volumes",
    "k3s_longhorn_nobackup_volumes",
)


def _k3s_defaults() -> dict:
    return yaml.safe_load((K3S / "defaults" / "main.yml").read_text())


def _declared_pvcs() -> set[str]:
    """Every `namespace/name` a k8s role gets a PersistentVolumeClaim for.

    Two sources, and both are needed. A few roles render their own PVC manifest, but most get
    theirs from the shared `seed-volume` role via a `*_claim` var in their defaults — so a
    collector that only reads rendered manifests finds 20 of the 29 routed volumes and its
    "missing" list is mostly noise.

    The manifests are read RENDERED rather than text-scanned, because a PVC's name is a Jinja
    expression: scanning the templates would match `{{ ... }}` and find almost nothing, which
    reads as a clean result rather than as no coverage.
    """
    namespace = "homelab"
    names = {
        doc["metadata"]["name"]
        for _role, _tpl, doc in rendered_docs()
        if doc.get("kind") == "PersistentVolumeClaim"
        and doc.get("metadata", {}).get("name")
    }
    for defaults_file in K8S_ROLES.glob("*/defaults/main.yml"):
        values = yaml.safe_load(defaults_file.read_text()) or {}
        names |= {
            value
            for key, value in values.items()
            if key.endswith("_claim") and isinstance(value, str) and "{{" not in value
        }
    # A few roles pass the claim name to seed-volume as a literal rather than through a
    # defaults var (terraria-stats), so the defaults sweep alone misses them.
    for tasks_file in K8S_ROLES.glob("*/tasks/*.yml"):
        for line in tasks_file.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("seed_volume_claim:") and "{{" not in stripped:
                names.add(stripped.split(":", 1)[1].strip().strip("\"'"))
    return {f"{namespace}/{name}" for name in names}


def test_backup_routing_lists_are_pairwise_disjoint():
    defaults = _k3s_defaults()
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
    defaults = _k3s_defaults()
    for name in _ROUTING_LISTS:
        entries = defaults.get(name) or []
        dupes = sorted({e for e in entries if entries.count(e) > 1})
        assert not dupes, f"{name} lists {dupes} more than once"


def test_every_routed_volume_is_a_real_pvc():
    # A typo here does not fail anything at deploy — the label reconcile simply matches no
    # volume and moves on, leaving the PVC on whatever tier it was already in.
    defaults = _k3s_defaults()
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
