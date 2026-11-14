# longhorn-restore-drill — the restore drill's own Flux tag and its stamp ConfigMap

The tag that holds the nightly restore drill's cluster objects: the empty ConfigMap
`longhorn-restore-drill-stamps`, the script ConfigMap, the CronJob, the ServiceAccount and the
RBAC. The CronJob's `suspend` is `false`, and the *Mode* bullet below says why.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=longhorn-restore-drill` gates the
  rollout
- **Images:** `alpine/k8s` (`longhorn_restore_drill_runner_image`), `python`
  (`k3s_longhorn_restore_drill_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `${DRILL}` (no declared tier (runtime claim named by a shell variable))
<!-- /generated_from -->

- **Flux applies it.** Its `containers_list` entry is in `cluster/services.yml` and
  `depends_on: [longhorn]`.
- **It is a tag of its own, not part of `longhorn`.** The drill's objects live in `homelab`.
  The disaster-recovery order waits on `longhorn` at step 6, and nothing creates `homelab`
  until step 8, so a `homelab` object in `longhorn` would stall step 6.
  `tests/k8s/test_step6_tags_render_into_created_namespaces.py` holds that. The
  generated `homelab-namespace` kind gives this tag its `dependsOn`, so it waits for `homelab`.
- **The tag renders no Namespace.** A Namespace object here would be a second writer of
  `homelab` beside the generated `homelab-namespace` kind.
- **The stamp ConfigMap carries two annotations.** `kustomize.toolkit.fluxcd.io/ssa: IfNotPresent`
  makes Flux create it once and never revert the drill's writes to its data.
  `kustomize.toolkit.fluxcd.io/prune: disabled` keeps it when a render drops it, because the
  tag prunes like any per-tag Kustomization and no render restores its state.
  `tests/k8s/test_restore_drill_stamps.py::test_the_stamp_configmap_renders_empty_with_both_annotations`
  holds both.
- **A revert of the tag leaves the ConfigMap live**, because `prune: disabled` keeps it.
  Deleting it is **operator-only**.
- **`longhorn_drill_namespace` mirrors `k3s_longhorn_restore_drill_namespace`.** Backup health's
  readers read the second.
  `tests/k8s/test_restore_drill_stamps.py::test_the_stamp_namespace_equals_the_host_drills`
  holds the pair equal.
- **The census names the ConfigMap and the CronJob.**
  `scripts/deploy_tools/bundle_census.py:NAMED_MEMBERS` lists both, so a published render that
  drops either fails `promote.py check-bundle`.

## What the drill is and what it writes (the autonomous-role contract)

The CronJob `longhorn-restore-drill` (`templates/cronjob.yaml.j2`) runs
`templates/longhorn-restore-drill.sh.j2` from the script ConfigMap at 04:10 UTC. It restores the
newest backup of ONE volume into a NEW volume, mounts it in a throwaway pod, checks the data is
real, and tears everything down.

- **SQLite databases must pass `PRAGMA integrity_check`.** A block snapshot taken mid-write can
  restore cleanly and still hold a corrupt database. The probe pod checks every file with a
  SQLite header, and any result but `ok` fails the run before the success stamps. A database
  the read-only mount cannot open, because it has a hot `-journal` or a `-wal` without `-shm`,
  is copied with its sidecars into the pod's emptyDir and checked there. The run writes
  `sqlite.<pvc>` as `<epoch> checked=<n> bad=<n>` on a pass and on a failure. A probe log with no
  summary line fails the run. The probe image (`k3s_longhorn_restore_drill_image`) is
  `python:3.14-alpine`, for Python's `sqlite3` module. ENFORCED:
  `tests/longhorn/test_longhorn_restore_drill_sqlite.py::test_a_corrupt_database_fails_the_drill_and_stamps_no_success`,
  `tests/longhorn/test_longhorn_restore_drill_sqlite.py::test_the_probe_reports_a_corrupt_database_as_bad`.

- **Scope.** It creates and deletes the Volume, PV, PVC and probe pod named
  `restore-drill-<pvc>`, and patches the stamp ConfigMap. It never touches the live volume. It
  also deletes those objects for any `restore-drill-` Volume older than the Job's deadline plus
  its grace period, whichever PVC that Volume restored.
  `templates/rbac.yaml.j2` grants exactly the calls the script makes.
  `tests/k8s/test_rbac_against_script_drill.py::test_the_drill_role_grants_every_call_its_script_makes`
  holds that, and
  `tests/k8s/test_rbac_against_script_drill.py::test_a_drill_role_without_the_volume_delete_wait_is_flagged`
  is its red half.
- **The drill's three bounds** are an operator pin, an empty-restore waiver with a size ceiling and
  one retry per failure. `docs/k3s-node-plane-crons-and-incidents.md` derives them, and a test
  holds each. ENFORCED:
  `tests/longhorn/test_longhorn_restore_drill_operator_pin.py::test_argv_pin_drills_that_volume_not_the_rotations_pick`,
  `tests/longhorn/test_longhorn_restore_drill_byte_floor.py::test_an_undeclared_volume_still_fails_with_no_files`,
  `tests/longhorn/test_longhorn_restore_drill_empty_waiver.py::test_a_filled_declared_volume_fails_and_stamps_nothing`,
  `tests/longhorn/test_longhorn_restore_drill_retry.py::test_a_retry_already_spent_leaves_the_rotation_alone`.
- **Mode.** `suspend` in `templates/cronjob.yaml.j2` is the lever, and it is `false`. Both runners
  name their restore `restore-drill-${PVC}` and `cleanup()` deletes that name on every exit, so a
  scheduled run beside a host-cron run could tear down the other's restore. The host cron file is
  gone, so the CronJob is the only runner. Setting `suspend: true` again is the first step of any
  revert that restores the host cron. ENFORCED:
  `tests/longhorn/test_longhorn_restore_drill_deadline.py::test_the_cronjob_is_unsuspended_and_never_overlaps`.
- **The schedule is the post-deploy gate.** No gate Job runs the drill per revision, because a
  copy would run a real restore beside the scheduled one. The CronJob carries
  `homelab/gate: schedule`, and `probe.py health longhorn-restore-drill` reads it. With no Job
  yet, the gate passes for one schedule period after Flux first applied the revision and fails
  after that. With a Job, the gate reads the newest firing, which must have succeeded within two
  periods. ENFORCED:
  `scripts/diagnostics/tests/test_probe_health_cronjob_schedule_gate.py::test_no_job_past_one_schedule_period_of_the_apply_fails`,
  `tests/deploy/test_cronjob_only_roles_include_the_gate.py::test_the_schedule_gate_marker_names_exactly_the_ungated_roles`.
- **Abort valves.** The Job's `activeDeadlineSeconds` is the restore timeout plus the probe pod's
  300-second bound plus a 600-second margin. The kubelet's SIGTERM at the deadline runs the
  script's `cleanup` through its TERM trap, within the 300-second grace period. ENFORCED:
  `tests/longhorn/test_longhorn_restore_drill_deadline.py::test_the_deadline_clears_the_restore_timeout_plus_the_probe_bound`.
- **The probe's scratch emptyDir is capped.** Its `sizeLimit` is
  `k8s/roles/longhorn-restore-drill/defaults/main.yml:k3s_longhorn_restore_drill_max_actual_bytes`,
  the candidates' actualSize cap, which also bounds an operator-pinned run. A database copied
  there for the read-write check therefore always fits. ENFORCED:
  `tests/longhorn/test_longhorn_restore_drill_sqlite.py::test_the_scratch_copy_is_capped_at_the_candidate_size_cap`.
- **The next run sweeps what a SIGKILL leaves.** A TERM pending while bash parses a `$(` runs no
  trap, so SIGKILL skips `cleanup`. Bash offers no setting that closes this race, and an OOM kill
  or a lost node skips `cleanup` the same way. The next run tears down each older leftover before
  it reads the candidates, and logs a `SWEPT:` line. A delete that fails or outlasts
  `longhorn_restore_drill_delete_timeout_seconds` fails the run and keeps the Volume, which is how
  the next run finds the leftover again. A stuck leftover, such as a pod Terminating on a lost node,
  therefore blocks every nightly drill and the stamps it refreshes, with a `FAIL:` line each night,
  until check 7 reports `last-success` as stale. A younger Volume stays, because it can belong to a
  live hand run. The sweep deletes no Backup, because the drill creates its Volume in the
  `no-backup` group and a leftover is never backed up. Longhorn's label rule would otherwise put
  the Volume in `default`, and `allow-recurring-job-while-volume-detached` lets the daily tick back
  up a detached Volume. The tier classifier reads no Volume label, so *At a glance* reports this
  claim with no declared tier. ENFORCED:
  `tests/longhorn/test_longhorn_restore_drill_state.py::test_the_drill_volume_is_created_out_of_the_default_backup_group`,
  `tests/longhorn/test_longhorn_restore_drill_state.py::test_the_next_run_sweeps_a_leftover_past_the_deadline_and_grace`,
  `tests/longhorn/test_longhorn_restore_drill_state.py::test_a_volume_a_live_run_can_hold_is_not_swept`,
  `tests/longhorn/test_longhorn_restore_drill_state.py::test_a_leftover_that_cannot_be_removed_fails_the_run_before_any_restore`.
- **Required evidence.** The pod log, with each verdict line prefixed `longhorn-restore-drill:`.
  The `PASS:` line and the stuck-probe `FAIL:` line carry `probe=<n>s`, which is the time the
  probe pod's 300-second bound limits, image pull included. Compare it against the bound for the
  largest SQLite candidates before raising the bound. ENFORCED:
  `tests/longhorn/test_longhorn_restore_drill_deadline.py::test_the_pass_line_reports_the_probe_time_the_bound_limits`.
  Read it with `probe.py loki-query '{container="longhorn-restore-drill"}'`. Backup health's check
  7 reads the stamp ConfigMap's `last-success`, and check 8 reads the per-volume keys, so a drill
  that stops running pages through the stamp and not through the log.
- **The image runs BusyBox.** The runner is `alpine/k8s` (`longhorn_restore_drill_runner_image`),
  whose `mktemp`, `date`, `sort` and `stat` are BusyBox applets over musl. The script tests run
  the script inside that digest as well. ENFORCED:
  `tests/longhorn/test_longhorn_restore_drill_image.py::test_a_suffixed_mktemp_template_passes_on_the_host_and_fails_under_the_image`.
- **Next-run review.** Before widening scope, read the last week of pod logs for the retries
  and the `FAIL:` lines.
- **Hand runs are operator-only.** The agent's kubeconfig cannot create a Job. The plain and the
  pinned form are in `docs/k3s-node-plane-crons-and-incidents.md`, and both run outside the
  04:10 UTC slot. A pinned run stamps `success.<pvc>` and not `last-success`.
- **The drill's keys live here.** The eight `k3s_longhorn_restore_drill_*` keys are in
  `defaults/main.yml`. A host package renders without a k8s role's defaults, so no host file can
  read one.
- **The `DECIDED:` markers sit beside the keys** in `defaults/main.yml`: the 4 GiB size cap,
  the 2700-second timeout and the 04:10 UTC slot. The script keeps its own, including the
  candidate list published from the full eligible set and the pinned run that leaves the global
  stamp alone.
