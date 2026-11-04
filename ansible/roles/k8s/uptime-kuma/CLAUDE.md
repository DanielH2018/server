# uptime-kuma — retired, rendering only its two claims

M6 retired Uptime Kuma (`docs/target-architecture/monitoring/m6-retire-kuma.md`, *Phase C*). This
role is the first of two PRs in `docs/flux-deploy-plane.md`, *Retiring a flux-owned service*. The
second PR deletes it. `docs/uptime-kuma-autokuma-record.md` keeps the incidents and source reading
behind the workload this role used to run.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=uptime-kuma` with exit 5
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** `uptime-kuma-data` (no backup (listed in k3s_longhorn_nobackup_volumes)),
  `autokuma-data` (no backup (listed in k3s_longhorn_nobackup_volumes))
<!-- /generated_from -->

- **The render holds the two claims and nothing else.** Each claim carries
  `kustomize.toolkit.fluxcd.io/prune: disabled`, so Flux leaves it in place either way. The entry's
  `no_backup_claims` keeps both in the no-backup tier.
- **`files/` and its tests stay until the role goes.** No render reads `files/`.
- **The operator deletes the two claims and any Secret left**, after the second PR.
