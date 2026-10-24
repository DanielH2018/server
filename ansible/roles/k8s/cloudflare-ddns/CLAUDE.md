# cloudflare-ddns — keeps two Cloudflare A records pointed at the router's WAN IP

Runs `favonia/cloudflare-ddns` twice: one Deployment updates a **direct** (unproxied) record,
the other a **proxied** one behind Cloudflare's edge. See repo-root `CLAUDE.md` for shared
conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=cloudflare-ddns` with exit 5
- **Image:** `favonia/cloudflare-ddns` (`cloudflare_ddns_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Digest-pinned image**, with the tag kept alongside the digest for Renovate's k8s-defaults
  manager.
- **Secrets** (SOPS keys, not values): `cloudflare_dns_token`,
  `cloudflare_ddns_direct_push_token`, `cloudflare_ddns_proxied_push_token`.
- **Flux applies it, not Ansible** (D5.19, the first Wave D PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. To
  give it back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain
  revert of the flag leaves two writers on the objects. Both Deployments are `RollingUpdate`
  with one replica and no readinessProbe, so the Kustomization's `wait` reads a pod Ready as
  soon as its container starts. That is the same gate the explicit `rollout status` task gave
  under Ansible.
- **The Secret substitutes from `homelab-cluster-vars`.** The three keys above are `${<key>}`
  placeholders in the published render. Flux substitutes the values from the
  `homelab-cluster-vars` Secret, which it decrypts from `ansible/vars/cluster-vars.sops.env`.
  The Kustomization depends on `cluster-vars`, so a rotated value is live before
  cloudflare-ddns reconciles. A rotation reaches the cluster only through `cluster_vars.py
  write`. The renderer stamps a restart checksum on both Deployments, so a changed token rolls
  both pods.
- **Where the tasks went.** `tasks/main.yml` holds the `k8s/manifests` include and the "Wait
  for the cloudflare-ddns rollouts" task, and no Flux path runs either. They stay for the
  handback. The Kustomization's `wait` replaces the wait task, and the restart checksum
  replaces the `manifests_extra_rollouts` restart described below.

## Notable
- Renders **two** Deployments (`cloudflare-ddns-direct`, `cloudflare-ddns-proxied`), neither
  named after the service. Under Ansible, before D5.19, `manifests_extra_rollouts` named both
  explicitly — this is also
  what restarts them on a Secret change, since `manifests_rollout: ''` disables the shared
  restart wiring too. Rotating a push token without both names in
  `manifests_extra_rollouts` leaves both pods running on the stale token (found 2026-08-30,
  both monitors DOWN behind a green deploy).
- Each Deployment pushes its own Kuma heartbeat over its own token — a shared token would
  hide either arm going stale behind the other still pushing.

- favonia's image serves no metrics, so two shadow rule families watch it (M6 batch B7).
  `CloudflareDdnsUnavailable` in observability's `pods` group reads each Deployment's available
  replicas. `CloudflareDdnsDirectUpdateSilent` and `CloudflareDdnsProxiedUpdateSilent` in
  loki-homelab's `homelab-logs.yaml` fire when a pod logs none of "are already up to date",
  "Updated an outdated" or "Added a new" for 15 minutes. The pod name separates the two,
  because both containers are named `cloudflare-ddns`.

## Editing
- Manifests: `templates/deployment-direct.yaml.j2`, `templates/deployment-proxied.yaml.j2`,
  `templates/secret.yaml.j2`.
