# k8s/volume-claim — the shared PersistentVolumeClaim path

This role deploys nothing but a PVC. Sixteen caller roles ship no `pvc.yaml.j2` of
their own, so this is the only thing that creates their claim — it runs BEFORE
`k8s/manifests` so a workload never starts against a claim that doesn't exist yet.

**No standalone deploy tag.** Callers reach it via `include_role: name: k8s/volume-claim`
with `volume_claim_service`/`volume_claim_name`/`volume_claim_size`/`_storage_class`
vars, never `--tags volume-claim` — it isn't a `containers_list` entry, so a promoted
image bump here would match no play and deploy nothing while reporting success.

**`k8s_autodeploy: false`** for the same reason: it renders only a PVC, so there is no
workload for `rollout status` to gate, and a stray auto-deploy has an outsized blast
radius as the shared path 16 roles depend on. Reason is in `defaults/main.yml`.

## Notable
- Until 2026-09-01 this role also **seeded** claims from a Docker bind mount on
  daniel-server, under the name `seed-volume`. The source tree stopped existing when
  Docker was uninstalled there at the end of the k3s migration, so the seeding — and the
  name — were retired; only the PVC creation is left.
- Every mutating task is guarded on `k8s_no_mutate`, set at the role level rather than
  at each of the 24 call sites: an earlier guard checked `ansible_run_tags` instead,
  which only sees what the operator typed on the command line — `--tags freshrss` names
  no unsupported role and still reached this one, and a dry run of freshrss once removed
  a real seed pod against freshrss's live Longhorn PVC.
- Skipping this role under `--dry-run`/`--check` means a brand-new service's dry run
  validates its Deployment without ever proving the volume can be provisioned — nothing
  at admission checks a referenced PVC exists.
