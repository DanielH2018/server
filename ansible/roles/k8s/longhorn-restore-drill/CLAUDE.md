# longhorn-restore-drill — the restore drill's own Flux tag and its stamp ConfigMap

The tag that holds the nightly restore drill's cluster objects: the empty ConfigMap
`longhorn-restore-drill-stamps` (H2.3b), and since H2.3d the script ConfigMap, the CronJob, the
ServiceAccount and the RBAC. The CronJob shipped `suspend: true` at H2.3d, and H2.3e set
`suspend: false` after the operator confirmed the host cron file
`/etc/cron.d/longhorn-restore-drill` was gone.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=longhorn-restore-drill` with exit 5
- **Images:** `alpine/k8s` (`longhorn_restore_drill_runner_image`), `busybox`
  (`k3s_longhorn_restore_drill_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `${DRILL}` (daily -> B2 (default group))
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
- **The census names the ConfigMap and the CronJob.**
  `scripts/deploy_tools/bundle_census.py:NAMED_MEMBERS` lists both, so a published render that
  drops either fails `promote.py check-bundle`.

## What the drill is and what it writes (the autonomous-role contract)

The CronJob `longhorn-restore-drill` (`templates/cronjob.yaml.j2`) runs
`templates/longhorn-restore-drill.sh.j2` from the script ConfigMap at 04:10 UTC. It restores the
newest backup of ONE volume into a NEW volume, mounts it in a throwaway pod, checks the data is
real, and tears everything down. It moved here from `setup/k3s` at H2.3d.

- **Scope.** It creates and deletes the Volume, PV, PVC and probe pod named
  `restore-drill-<pvc>`, and patches the stamp ConfigMap. It never touches the live volume.
  `templates/rbac.yaml.j2` grants exactly the calls the script makes.
  `ansible/tests/k8s/test_rbac_against_script_drill.py::test_the_drill_role_grants_every_call_its_script_makes`
  holds that, and
  `ansible/tests/k8s/test_rbac_against_script_drill.py::test_a_drill_role_without_the_volume_delete_wait_is_flagged`
  is its red half.
- **The drill's three bounds** are an operator pin, an empty-restore waiver with a size ceiling and
  one retry per failure. `docs/k3s-node-plane-crons-and-incidents.md` derives them, and a test
  holds each. ENFORCED:
  `ansible/tests/longhorn/test_longhorn_restore_drill_operator_pin.py::test_argv_pin_drills_that_volume_not_the_rotations_pick`,
  `ansible/tests/longhorn/test_longhorn_restore_drill_byte_floor.py::test_an_undeclared_volume_still_fails_with_no_files`,
  `ansible/tests/longhorn/test_longhorn_restore_drill_empty_waiver.py::test_a_filled_declared_volume_fails_and_stamps_nothing`,
  `ansible/tests/longhorn/test_longhorn_restore_drill_retry.py::test_a_retry_already_spent_leaves_the_rotation_alone`.
- **Mode.** `suspend` in `templates/cronjob.yaml.j2` is the lever, and it is `false`. Both runners
  name their restore `restore-drill-${PVC}` and `cleanup()` deletes that name on every exit, so a
  scheduled run beside a host-cron run could tear down the other's restore. The host cron file is
  gone, so the CronJob is the only runner. Setting `suspend: true` again is the first step of any
  revert that restores the host cron. ENFORCED:
  `ansible/tests/longhorn/test_longhorn_restore_drill_deadline.py::test_the_cronjob_is_unsuspended_and_never_overlaps`.
- **Abort valves.** The Job's `activeDeadlineSeconds` is the restore timeout plus the probe pod's
  300-second bound plus a 600-second margin. The kubelet's SIGTERM at the deadline runs the
  script's `cleanup` through its TERM trap, within the 300-second grace period. ENFORCED:
  `ansible/tests/longhorn/test_longhorn_restore_drill_deadline.py::test_the_deadline_clears_the_restore_timeout_plus_the_probe_bound`.
- **Required evidence.** The pod log, with each verdict line prefixed `longhorn-restore-drill:`.
  Read it with `probe.py loki-query '{container="longhorn-restore-drill"}'`. Backup health's check
  7 reads the stamp ConfigMap's `last-success`, and check 8 reads the per-volume keys, so a drill
  that stops running pages through the stamp and not through the log.
- **The image runs BusyBox.** The runner is `alpine/k8s` (`longhorn_restore_drill_runner_image`),
  whose `mktemp`, `date`, `sort` and `stat` are BusyBox applets over musl. The script tests run
  the script inside that digest as well. ENFORCED:
  `ansible/tests/longhorn/test_longhorn_restore_drill_image.py::test_a_suffixed_mktemp_template_passes_on_the_host_and_fails_under_the_image`.
- **Next-run review.** Before widening scope, read the last week of pod logs for the retries
  and the `FAIL:` lines.
- **Hand runs are operator-only.** The agent's kubeconfig cannot create a Job. The plain and the
  pinned form are in `docs/k3s-node-plane-crons-and-incidents.md`, and both run outside the
  04:10 UTC slot. A pinned run stamps `success.<pvc>` and not `last-success`.
- **The drill's keys live here, not in `setup/k3s`.** The eight moved keys keep their
  `k3s_longhorn_restore_drill_*` names. No `setup/` task or template may read one. ENFORCED:
  `ansible/tests/repo/test_census_rows_roles.py::test_census_row_holds_on_the_tree`, row
  `drill-keys-off-the-host-plane`. The keys a host reader needs stay in `setup/k3s`:
  `k3s_longhorn_restore_drill_max_age_days`, `k3s_longhorn_restore_drill_coverage_slack_days`,
  the namespace and, until H2.3f, the stamp directory.
- **The `DECIDED:` markers moved with the keys** into `defaults/main.yml`: the 4 GiB size cap,
  the 2700-second timeout and the 04:10 UTC slot. The script keeps its own, including the
  candidate list published from the full eligible set and the pinned run that leaves the global
  stamp alone.
