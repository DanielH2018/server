# longhorn-restore-drill — the restore drill's own Flux tag and its stamp ConfigMap

The tag that holds the nightly restore drill's cluster objects. It renders one object so far,
the empty ConfigMap `longhorn-restore-drill-stamps` (H2.3b). The CronJob, its script and its
RBAC join it in H2.3d. The drill itself still runs as a host cron until then.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=longhorn-restore-drill` with exit 5
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible.** Its `containers_list` entry carries `owner: flux` and
  `depends_on: [longhorn]` from its first commit. `tasks/main.yml` holds the include of
  `k8s/manifests` and no Flux path runs it.
- **It is a tag of its own, not part of `longhorn`.** The drill's objects live in `homelab`.
  H1's DR order waits on `longhorn` at step 6, and nothing creates `homelab` until step 8, so a
  `homelab` object in `longhorn` would stall step 6.
  `ansible/tests/k8s/test_step6_tags_render_into_created_namespaces.py` holds that. The
  generated `homelab-namespace` kind gives this tag its `dependsOn`, so it waits for `homelab`.
- **The tag renders no Namespace.** A Namespace object here would be a second writer of
  `homelab` beside `ansible/deploy.yml`, which C4 forbids.
- **The stamp ConfigMap carries two annotations.** `kustomize.toolkit.fluxcd.io/ssa: IfNotPresent`
  makes Flux create it once and never revert the drill's writes to its data.
  `kustomize.toolkit.fluxcd.io/prune: disabled` keeps it when a render drops it, because the
  tag prunes like any per-tag Kustomization and no render restores its state.
  `ansible/tests/k8s/test_restore_drill_stamps.py::test_the_stamp_configmap_renders_empty_with_both_annotations`
  holds both.
- **A revert of the tag leaves the ConfigMap live.** C3's `deletionPolicy: Orphan` does that
  until D6.20, and `prune: disabled` does it after. Deleting it is **operator-only**.
- **`longhorn_drill_namespace` mirrors `k3s_longhorn_restore_drill_namespace`.** The host drill
  reads the second until H2.3f.
  `ansible/tests/k8s/test_restore_drill_stamps.py::test_the_stamp_namespace_equals_the_host_drills`
  holds the pair equal.
- **The census names the ConfigMap.** `scripts/deploy_tools/bundle_census.py:NAMED_MEMBERS`
  lists it, so a published render that drops it fails `promote.py check-bundle`.
