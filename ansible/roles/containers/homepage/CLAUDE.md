# homepage — offsite dashboard (remote node)

## At a glance
- **Image:** `ghcr.io/gethomepage/homepage` (digest-pinned; multi-arch index, arm64 verified 2026-08-30)
- **Host:** `daniel-cloud` (Oracle A1, arm64) — a **second, separate** dashboard, not a mirror of the cluster's
- **Route:** `homepage.<domain>` · **Port:** 3000 · **Networks:** `proxy` · **Depends on:** traefik
- **Config:** `templates/config/*.j2` → bind-mounted at `containers/homepage/config/`
- **State:** none worth keeping. The config is re-rendered on every deploy.

## Notable
- **This role is the answer to "show a separate layout with only the live services".**
  There is no detection step and no conditional templating — the config here simply lists
  only what runs on this node, so when the house is dark the page is still correct because
  nothing on it depends on the house. The cluster's `services.yaml.j2` keeps all 14 tiles.
- **Widget URLs use node-local Docker service names** (`http://freshrss:80`,
  `http://karakeep:3000`) rather than public hostnames, which keeps widget traffic off the
  auth boundary entirely. The cluster's copy has to route its widgets through `-k8s`
  monitoring hostnames precisely because the public route's Authelia 401s them.
- **No seed initContainer**, unlike the cluster role. That exists because Homepage
  EROFS-crashloops when it cannot write `kubernetes.yaml`/`custom.js` into a read-only
  mount; a bind mount is already writable. It is also why the rootfs is not `read_only`.
- `docker.yaml` is deliberately empty — there is no docker-proxy on this node, so tiles use
  `siteMonitor` (a plain HTTP check) for liveness. Do not bind the raw Docker socket in.
- Config renders at mode 0600 and the template task is `no_log: true`: `services.yaml`
  embeds the FreshRSS credentials and the Karakeep API key.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Dashboard content: `templates/config/services.yaml.j2` (tiles), `settings.yaml.j2`
  (layout), `widgets.yaml.j2` (header strip)
- A config edit alone does not change the compose hash, so `tasks/main.yml` passes
  `common_config_changed` — without it the container keeps serving the old config.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "homepage" -e target=daniel-cloud`
