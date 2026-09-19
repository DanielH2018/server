# cloudflare-ddns — keeps two Cloudflare A records pointed at the router's WAN IP

Runs `favonia/cloudflare-ddns` twice: one Deployment updates a **direct** (unproxied) record,
the other a **proxied** one behind Cloudflare's edge. See repo-root `CLAUDE.md` for shared
conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "cloudflare-ddns"`
- **Image:** `favonia/cloudflare-ddns` (`cloudflare_ddns_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — unprobeable, not merely probe-less —
  favonia/cloudflare-ddns is a scratch image running an outbound update loop with no HTTP
  server, no listener and no shell, so no httpGet, tcpSocket or exec readinessProbe can exist.
  Both rendered Deployments are therefore ungated: the role sets manifests_rollout: '', which
  skips the shared rollout wait AND the stability soak, and nothing replaces them.
<!-- /generated_from -->

- **Digest-pinned image**, with the tag kept alongside the digest for Renovate's k8s-defaults
  manager.
- **Secrets** (SOPS keys, not values): `cloudflare_dns_token`,
  `cloudflare_ddns_direct_push_token`, `cloudflare_ddns_proxied_push_token`.

## Notable
- Renders **two** Deployments (`cloudflare-ddns-direct`, `cloudflare-ddns-proxied`), neither
  named after the service, so `manifests_extra_rollouts` names both explicitly — this is also
  what restarts them on a Secret change, since `manifests_rollout: ''` disables the shared
  restart wiring too. Rotating a push token without both names in
  `manifests_extra_rollouts` leaves both pods running on the stale token (found 2026-08-30,
  both monitors DOWN behind a green deploy).
- Each Deployment pushes its own Kuma heartbeat over its own token — a shared token would
  hide either arm going stale behind the other still pushing.

## Editing
- Manifests: `templates/deployment-direct.yaml.j2`, `templates/deployment-proxied.yaml.j2`,
  `templates/secret.yaml.j2`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "cloudflare-ddns"`.
