# vaultwarden — Bitwarden-compatible password store

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=vaultwarden` gates the rollout
- **Image:** `vaultwarden/server` (`vaultwarden_k8s_image`)
- **Route:** `vaultwarden.local.<domain>` (LAN only), no Authelia
- **Claim:** `vaultwarden-data` (daily -> R2)
<!-- /generated_from -->

## Notable
- **The source allowlist is the vault API's gate, not the hostname.** The Bitwarden apps and
  extension call the API directly and cannot follow Authelia's 302, so the route carries no
  Authelia. `public_route: false` keeps the name off public DNS, but a hostname is not access
  control, and with no Authelia nothing else stands in front of the login API.
  The `vaultwarden-lan-only` ipAllowList (`templates/middleware.yaml.j2`) admits
  `k8s/roles/vaultwarden/defaults/main.yml:vaultwarden_k8s_allowed_sources`, matched on the
  remote address. ENFORCED:
  `tests/services/apps/test_vaultwarden_gate.py::test_every_vault_router_but_the_probes_carries_the_allowlist`.
- **Never put a pod or service CIDR in the allowlist.** It admits every pod in the cluster.
  WireGuard peers arrive as the wg-easy pod's IP, which changes on every restart, so remote
  access waits on giving wg-easy a stable source address. ENFORCED:
  `tests/services/apps/test_vaultwarden_gate.py::test_the_rendered_allowlist_admits_no_cluster_range_is_clean`.
- **The ingress probe has its own route.** The blackbox prober is a pod, outside the
  allowlist, and it probes the bare host. The `vaultwarden-probe` router admits the pod CIDR to
  exactly `Path(/)`, the static web-vault page. `monitoring_route()` cannot do this, because it
  matches a `PathPrefix`, and `/` as a prefix is the whole API.
- **Vaultwarden trusts `X-Real-IP` only from Traefik's node.**
  `k8s/roles/vaultwarden/defaults/main.yml:vaultwarden_k8s_trusted_proxies` is daniel-box's
  pod subnet, where Traefik is pinned. The header keys the login rate limit, so a wider value
  lets a caller pick its own bucket. If Traefik moves node, every client shares one bucket
  until this follows it.
- **Signups are off and `/admin` is disabled.** No `ADMIN_TOKEN` is set, so nothing can delete
  a stray account. To register, set `vaultwarden_k8s_signups_allowed: true` and narrow
  `vaultwarden_k8s_allowed_sources` to the registering device's /32 in the same PR. Land it,
  register, then revert both. `SIGNUPS_ALLOWED` renders from a bool, because Vaultwarden reads
  an unparsable value as unset, which means open. ENFORCED:
  `tests/services/apps/test_vaultwarden_gate.py::test_signups_render_open_only_from_a_true_bool`.
- **Invitations, org creation, password hints, emergency access and Sends are off.** It is a
  single-user vault with no outside recipients.
- **Website icons are on, kept out of backups and logs.** The icon cache lives in the `/tmp`
  emptyDir, because each cache file is named for a vault domain. `HTTP_REQUEST_BLOCK_REGEX`
  keeps the unauthenticated `/icons/` endpoint from fetching this domain's own routes. The
  icon module logs at warn. Traefik's access log still records each `/icons/<domain>` path.
- **`DOMAIN` must match the route.** WebAuthn credentials and attachment links are built from
  it, so changing the route's exposure means registering every WebAuthn key again.
- **Backup:** `vaultwarden-data` is on the daily R2 tier through `r2_backup_claims` (ADR 0007).
  R2 carries no object lock, so the operator keeps an offline encrypted export as the copy
  that survives a bucket deletion. The backup holds `rsa_key*`, which signs API tokens. After a
  suspected backup leak, stop the pod, delete `rsa_key*` and start it again. Every client then
  logs in again. Re-adding the claim to the restore drill's empty-restore waiver is refused.
  ENFORCED:
  `tests/longhorn/test_longhorn_restore_drill_empty_waiver.py::test_vaultwarden_is_waived_only_while_it_has_no_replicas`.
- **Renovate does not automerge the image** (`k8s_autodeploy: false`). Vaultwarden migrates
  its SQLite schema on start, so an upgrade wants an operator watching.

## Editing
- Manifests: `templates/`. The Service renders from `k8s/templates/service-default.yaml.j2`
  off the containers_list entry. The role owns its IngressRoute and its allowlist Middleware.
- Image pin, Renovate stance, resources, uid, allowlist and trusted proxy:
  `defaults/main.yml`.
