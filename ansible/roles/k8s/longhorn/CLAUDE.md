# longhorn — the storage control plane and the `longhorn` StorageClass

Longhorn's manager DaemonSet, its CRDs, its admission webhook and driver deployer, and the
ConfigMap that tells the manager which `longhorn` StorageClass to create. Every PVC in the
cluster binds through it. A bad apply here detaches volumes under every stateful service while
host probes stay green.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=longhorn` with exit 5
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (H1.5a). Its `containers_list` entry carries `owner: flux`
  from its first commit. `tasks/main.yml` holds the include of `k8s/manifests` and no Flux path
  runs it. A rebuilt cluster gets it from the `longhorn` Kustomization at step 7 of
  `docs/longhorn-disaster-recovery.md`'s *Order from H1.2*, and the settings and backup targets
  that `setup/k3s` still writes come after it (*The Longhorn window*, below).
- **The workloads are `files/upstream/longhorn.yaml`**, upstream's file byte for byte, pinned by
  `k3s_longhorn_version` and the `longhorn_vendored_manifests` item in `defaults/main.yml`.
  `scripts/dev/vendor_manifest.py` checks the pin and refreshes the copy. The refresh command
  sits above the pin. No render may fetch a URL (D5, C1).
- **Merging a bump of `k3s_longhorn_version` is the upgrade** (H1 Open question 6). `land.sh`
  runs the `longhorn-upgrade` gates before it merges, Longhorn supports no downgrade and one
  minor per hop, so read `docs/longhorn-upgrade.md` before you bump the pin.
- **Two patches change upstream objects**, both under `files/patches/`. The renderer lists them
  as the Kustomization's `patches:`, and each names its target by kind, name and namespace.
    - `longhorn-manager.yaml` adds a memory request and no limit to the manager container.
      A kustomize patch is copied verbatim and cannot read a variable, so
      `ansible/tests/longhorn/test_longhorn_control_plane_resources.py::test_manager_patch_holds_the_value_of_the_defaults_key`
      holds the literal equal to `k3s_longhorn_manager_memory_request`.
    - `longhorn-storageclass.yaml` replaces the class inside upstream's `longhorn-storageclass`
      ConfigMap, which longhorn-manager reads to create the `longhorn` class. The class omits
      `numberOfReplicas`, because a StorageClass parameter beats the `default-replica-count`
      setting. `ansible/tests/longhorn/test_longhorn_storageclass.py::test_the_class_differs_from_upstreams_only_by_the_replica_count`
      holds the class equal to upstream's minus that one parameter, so a release that adds a
      parameter fails until the patch carries it.
- **The file declares no `caBundle`, no webhook configuration and no Secret.** Under
  server-side apply a field the render does not declare stays with the controller that writes
  it, so the role needs no patch for a runtime-written field.
- **The tag renders `prune: false`** (`flux_prune: false`, with the `DECIDED:` marker beside it
  in `defaults/main.yml`). A render that dropped a CRD would make Flux delete every Volume of
  its kind. The cost: an object removed from the render, or retired by a later release, stays
  live until the operator deletes it (**operator-only**).
- **A StorageClass whose parameters change needs an operator delete.** StorageClass parameters
  are immutable, and the Ansible task that deleted the class when its parameters differed is
  gone. If the manager does not replace a class whose ConfigMap changed, the fix is an
  **operator-only** delete of the class, which the manager recreates from the ConfigMap.
- **`manifests_rollout_timeout` is a placeholder until the operator measures the roll**
  (`# MEASURE:` in `defaults/main.yml`). The DaemonSet rolls with `maxUnavailable: 100%`, so
  both manager pods are replaced at once.
- **`NEVER_SHED` names the tag** in `ansible/filter_plugins/service_tier.py`. The role is
  untiered, and the lost-node break-glass must not scale the storage control plane down.
  `ansible/tests/k8s/test_shed_set_is_derived_from_tier.py::test_never_shed_matches_the_untiered_roles_that_never_prune`
  holds the set equal to the untiered roles that set `flux_prune: false`.

## The Longhorn window, H1.5a until H1.7b
`setup/k3s` still holds the tasks that write Longhorn's Settings, its backup targets and its
RecurringJobs, and they fail on a cluster where Flux has not yet created `longhorn-system`.
Bring-up step 1 therefore skips the `longhorn` and `longhorn_backup` tags, and step 7a applies
them once the `longhorn` Kustomization reads `Ready`. The host tasks `setup/k3s` keeps for
Longhorn (`open-iscsi`, `nfs-common` and the multipathd blacklist) carry the tag
`longhorn_host` alone, so step 1 still runs them. `docs/longhorn-disaster-recovery.md`, *Order
from H1.2*, has the steps.
