# traefik — Reverse proxy, TLS termination & CrowdSec WAF

Edge router for the whole homelab. See repo-root `CLAUDE.md` for shared conventions.
**This role bundles two containers:** `traefik` (`traefik:latest`) and the CrowdSec
agent (`crowdsecurity/crowdsec:latest`). The separate `crowdsec` role is only the
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
- The `labels()` and Authelia middleware macros live in `templates/traefik.yml.j2`,
  imported by every other service's compose.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Static/dynamic cfg: `templates/config.yml.j2`, `templates/traefik.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "traefik"`
