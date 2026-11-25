# vaultwarden — Bitwarden-compatible password store

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=vaultwarden` gates the rollout
- **Image:** `vaultwarden/server` (`vaultwarden_k8s_image`)
- **Route:** `vaultwarden.local.<domain>` (LAN only), no Authelia
- **Claim:** `vaultwarden-data` (weekly -> B2 (default target))
<!-- /generated_from -->

## Notable
- **Held at zero replicas** by `vaultwarden_k8s_replicas` in `defaults/main.yml`, so Flux
  resets any hand scale-up. The claim still binds, because the `longhorn` StorageClass binds
  `Immediate`. To bring it up, raise the count and drop `probe: false` from the
  `containers_list` entry in the same PR; the ingress probe then expects a 200. Drop
  `vaultwarden-data` from `k3s_longhorn_restore_drill_empty_ok_pvcs` there too, because a small
  vault stays under the waiver's size ceiling. ENFORCED:
  `tests/longhorn/test_longhorn_restore_drill_empty_waiver.py::test_vaultwarden_is_waived_only_while_it_has_no_replicas`.
- **The route has no Authelia gate.** The Bitwarden apps and browser extension call the API
  directly and cannot follow Authelia's 302. The route is LAN-only (`public_route: false`), so
  a phone off the LAN reaches it over wg-easy.
- **Signups are off and `/admin` is disabled.** `SIGNUPS_ALLOWED=false` and no `ADMIN_TOKEN`.
  To create the first account, flip `SIGNUPS_ALLOWED` to `"true"` for one deploy, register,
  then flip it back. An `ADMIN_TOKEN` would need a SOPS secret and a `secret.yaml.j2`.
- **`DOMAIN` must match the route.** WebAuthn and attachment links are built from it, so it
  names the `.local.` host. Making the route public means changing `DOMAIN` too.
- **Backup:** `vaultwarden-data` is a weekly B2 shard (Monday). Before the vault holds real
  data, consider moving it to the daily R2 tier: that needs a `tier: home-critical` entry with
  `backup_claims`, per the header of `cluster/services.yml`.
- **Renovate does not automerge the image** (`k8s_autodeploy: false`). Vaultwarden migrates
  its SQLite schema on start, so an upgrade wants an operator watching.

## Editing
- Manifests: `templates/`. The Service and the IngressRoute render from `k8s/templates/service-default.yaml.j2` and `k8s/templates/ingressroute-default.yaml.j2`, off the containers_list entry.
- Image pin, Renovate stance, resources and uid: `defaults/main.yml`.
