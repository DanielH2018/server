# pihole — Network ad-blocker + Unbound resolver

LAN DNS sinkhole (Pi-hole) with a recursive Unbound upstream. See repo-root `CLAUDE.md`.

## At a glance
- **Images:** `pihole/pihole` (version-pinned, Renovate-managed) + `klutchell/unbound:latest`
  (`watchtower.enable=false` — it health-gates pihole and is the LAN's only DNS upstream; image
  updates are deliberate manual pulls: `deploy.yml --tags pihole -e common_pull=always` — a plain
  redeploy never re-pulls a tag already present locally)
- **Host:** daniel-server · **Port:** 80 · **URL:** `pihole.<domain>` (Authelia: yes)
- **Networks:** apps + pihole_internal (Unbound only on `pihole_internal`)
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **Highest-risk service (LAN DNS/DHCP).** After any change, verify resolution + DHCP.
- **Idempotent deploy (since 2026-06-09):** uses `common/docker_deploy` with
  `common_config_changed` wired to the bind-mounted resolver configs (`unbound.conf.j2`,
  `dnsmasq.yml.j2`). A no-op run recreates nothing and never touches `/etc/resolv.conf`;
  the Cloudflare fallback resolv.conf is written only on first install or when a
  recreate is actually coming.
- **This host's steady-state resolver is `pihole_host_dns_primary`** (`defaults/main.yml`,
  default `server_ip` = this Pi-hole), with 1.1.1.1 as the second line. Slice-6 B3 flips that
  default to the in-cluster DNS VIP; the fallback stays, because it is what keeps ~26 Docker
  services resolving when the cluster is down. Note the k3s node uses a different mechanism
  entirely — an Ansible-owned `systemd-resolved` drop-in pinning upstream resolvers
  (`roles/setup/k3s`), because a cluster node must never resolve through a VIP the cluster
  itself has to be up to serve.
- DNS/DHCP ports bound to **`{{ server_ip }}` (the LAN IP), not 0.0.0.0** — avoids being
  an open resolver / rogue DHCP source.
- Pi-hole resolves via Unbound (`FTLCONF_dns_upstreams: unbound`), a local recursive
  resolver — no third-party upstream DNS.
- Broad capability set (NET_ADMIN/NET_BIND_SERVICE/NET_RAW/SETFCAP/SYS_NICE…) is
  documented inline in the compose; don't trim it blindly. Add `SYS_TIME` only if
  Pi-hole NTP is enabled.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Resolver: `ansible/roles/k8s/pihole/templates/config/pihole-unbound.conf.j2`,
  `pihole-dnsmasq.conf.j2` (shared with `roles/k8s/pihole` since slice-6 B3 — one source, both copies;
  moved out of the old shared `ansible/templates/` during the macro-bridge inlining)
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "pihole"`
