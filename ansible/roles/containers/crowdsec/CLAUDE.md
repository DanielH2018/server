# crowdsec — Metabase dashboard for CrowdSec

**Visualization only.** The CrowdSec detection engine runs in the `traefik` role; this
role is a Metabase instance (`crowdsec-dashboard`) with pre-configured dashboards.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** built from `templates/Dockerfile.j2` (Metabase base, baked-in dashboards)
- **Host:** daniel-server · **Port:** 3000 · **URL:** `crowdsec.<domain>` (Authelia: yes)
- **Networks:** proxy
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- JVM heap pinned `-Xmx1g` (`JAVA_TOOL_OPTIONS`): Metabase otherwise sizes Xmx to 25% of
  the cgroup limit and OOM-crash-loops under the M1 memory cap. **Don't lower the
  `deploy` memory limit (2 GB) without lowering Xmx too.**
- Needs `CHOWN`/`SETUID`/`SETGID` so Metabase can chown its H2 DB dir and `su` down.
- Data in external volume `crowdsec-db`; Watchtower auto-update disabled.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Image: `templates/Dockerfile.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "crowdsec"`
