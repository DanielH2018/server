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

## Where the claim is staged

`/etc/rancher/k3s/manifests/<service>-claims/<claim-name>.yaml` — a sibling of the consuming
role's own manifest directory, **not inside it**. That directory belongs to `k8s/manifests`,
which prunes every file the caller does not name in
`manifests_files`/`manifests_secret_files`. No caller names this claim, so staging it there
had the prune delete it on every deploy: a permanently `changed` task on an idempotent run,
and a claim never re-applied from the role's own directory (#1654, the benign sibling of
#1550). The name follows `headlamp-netpol` / `prowlarr-netpol` / `media-volume-probe` — a
directory under the manifest root that no role's `manifests_service` claims, so no
`kubectl apply -f <dir>/` sweeps it.

The filename is the **claim name**, not `pvc.yaml`. scrutiny, tdarr and uptime-kuma each
include this role twice under one `volume_claim_service`, and a fixed filename had the second
claim's render overwrite the first — only the last claim of a service was ever staged.

Both invariants are ENFORCED by
`ansible/tests/k8s/test_volume_claim_pvc_path_collision.py`.

**A change under `tasks/` here does not make any service's manifests stale.** This role sits
in every k8s service's `role_paths` for the release-staleness check, because its
`pvc.yaml.j2` supplies bytes to what they apply. Its `tasks/` does not — it decides how the
deploy runs. Staging the claim in a sibling directory (0b86a7d7) touched only `tasks/claim.yml`
and still marked all 53 services stale, parking the `Release Staleness Drift` monitor DOWN with
no deploy tag able to clear it (#1672). `_is_real_change` in
`scripts/diagnostics/probe_lib/releases.py` now drops a shared role's `tasks/`, `handlers/` and
`meta/`. `defaults/main.yml` stays in: `volume_claim_size` and `volume_claim_storage_class` are
read by `pvc.yaml.j2`, so a change there does move the applied PVC.

A role that later drops its `k8s/volume-claim` include (wg-easy and zigbee2mqtt both did)
leaves its `<service>-claims/` file behind, where the consuming role's prune used to clear
it. Nothing sweeps that directory, so the file is inert rather than resurrecting an object —
but it is stale, and removing it is a manual step.

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
