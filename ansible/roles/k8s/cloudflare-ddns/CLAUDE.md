# cloudflare-ddns — keeps two Cloudflare A records pointed at the router's WAN IP

Runs `favonia/cloudflare-ddns` twice: one Deployment updates a **direct** (unproxied) record,
the other a **proxied** one behind Cloudflare's edge. See repo-root `CLAUDE.md` for shared
conventions.

## At a glance
- **Image:** `favonia/cloudflare-ddns` (`cloudflare_ddns_k8s_image`), digest-pinned; tag kept
  alongside the digest for Renovate's k8s-defaults manager.
- **Deploy tag:** `--tags "cloudflare-ddns"`. No route — infra role, no public UI.
- **Storage:** none — no PVC, stateless.
- **Auto-deploy: denylisted.** `favonia/cloudflare-ddns` is a scratch image with no HTTP
  server, listener or shell — no readinessProbe of any kind can exist. `manifests_rollout: ''`
  skips both the shared rollout wait and the stability soak, so nothing gates either
  Deployment.
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
