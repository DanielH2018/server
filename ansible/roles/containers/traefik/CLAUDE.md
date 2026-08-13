# traefik — demoted CrowdSec log agent (edge retired)

See repo-root `CLAUDE.md` for shared conventions.

**Since E7 (2026-08-13) this role runs only the CrowdSec agent.** The Docker `traefik` and
`authelia` containers it used to bundle retired: the k8s edge (`roles/k8s/traefik`) serves
everything now, and its Authelia (`roles/k8s/authelia`) owns `auth.<domain>` and the OIDC
issuer. What's left here is the same demoted agent slice-6 B2 (2026-08-09) introduced —
`DISABLE_LOCAL_API` + `LOCAL_API_URL` point it at the cluster LAPI (`roles/k8s/crowdsec`), so
it holds no decisions and no bouncer registrations. It now originates only this host's
auth.log (SSH) — the traefik access-log and Authelia stanzas retired with their containers.
The Docker Authelia role is archived at `roles/containers/archive/authelia`; the old edge's
own crowdsec dashboard role was archived earlier at `roles/containers/archive/crowdsec`. Plan
and rationale: `docs/k3s-migration/slice-7-phase-e-server-retirement.md`.

## At a glance
- **Host:** daniel-server
- **Networks:** proxy, monitoring
- **Depends on:** nothing — nothing depends on it either, now that the edge is gone.
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- `crowdsec-acquis.yaml.j2` is now a single auth.log stanza (`type: syslog`). The whitelist
  files, `crowdsec-profiles.yaml`, and the Discord notifier template moved to
  `roles/k8s/crowdsec/files/` and `templates/` at E7 — this role no longer renders or copies
  them; the cluster engine (`roles/k8s/crowdsec`) and the k8s traefik/authelia pods' sidecar
  agents are the only consumers now.
- **Retirement tombstones** at the end of `tasks/main.yml` unwind everything the old edge left
  on this host: the `docker-user-rules`/`docker-user-seed`/`traefik-init` systemd units, the
  "Verify DOCKER-USER origin lock" / "Check Cloudflare IP allowlist drift" / "Verify CrowdSec
  AppSec WAF" crons, their scripts under `/etc` and `/usr/local`, their monitor-bridge state
  dirs under `/var/lib`, and the Traefik logrotate config. Drop these once daniel-server (the
  only host that ever ran this role) has deployed past E7 — the same pattern the old
  home-allowlist tombstone used from B2 until this rewrite finally dropped it.
- `acme.json` and the old `data/` dir are left untouched on disk (rollback material) — nothing
  in this role manages them anymore.

## Editing
- Compose: `templates/docker-compose.yml.j2` (single `crowdsec` service) · Acquis:
  `templates/crowdsec-acquis.yaml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "traefik"`
