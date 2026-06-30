# traefik — Reverse proxy, TLS termination & CrowdSec WAF

Edge router for the whole homelab. See repo-root `CLAUDE.md` for shared conventions.
**This role bundles two containers:** `traefik` (version-pinned, Renovate-managed) and the
CrowdSec agent (`crowdsecurity/crowdsec:latest`, watchtower). The separate `crowdsec` role is only the
Metabase dashboard.

## At a glance
- **Host:** daniel-server
- **Networks:** proxy · **Authelia:** N/A (provides the forward-auth entrypoint)
- **Depends on:** nothing — **everything else depends on this** (deployed first).
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- TLS via Cloudflare DNS-01; routes services at `<hostname>.<domain>` from their labels.
- CrowdSec bouncer/WAF: `crowdsec-acquis.yaml`, `crowdsec-profiles.yaml`,
  `crowdsec-whitelist.yaml`, Discord alerts, home-IP allowlist updater.
- Ships **systemd units** (`traefik-init.service`, `docker-user-rules.service`) and
  logrotate — this role does more than run a container.
- The `labels()` macro imported by every other service's compose lives in the repo-level
  shared templates (`ansible/templates/traefik.yml.j2`) — NOT this role's
  `templates/traefik.yml.j2`, which is Traefik's *static config* (entrypoints, providers).
- **Wildcard default cert covers hand-rolled / path-bypass routers — don't re-flag a missing
  `certresolver` on them (review Network-L2, false positive).** `config.yml.j2`'s default TLS store
  sets a `defaultGeneratedCert` via the cloudflare resolver with SANs `*.<domain>` + `*.local.<domain>`.
  The secondary routers some services hand-roll for a path (`n8n-webhook`, `healthchecks-ping`,
  `karakeep-api`) carry `tls=true` with NO `certresolver` — that's fine: TLS cert selection is by
  **SNI (hostname), before path routing**, so they serve the same valid LE wildcard the co-hosted
  main router already provisions (verified: all three serve `CN=daniel-hunter.com`, issuer Let's
  Encrypt). Adding `certresolver` to them would only trigger redundant per-host ACME requests for
  zero gain. A NEW hand-rolled router on a NEW host not under the wildcard would still need one.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Static/dynamic cfg: `templates/config.yml.j2`, `templates/traefik.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "traefik"`
