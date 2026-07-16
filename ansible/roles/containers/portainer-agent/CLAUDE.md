# portainer-agent — Portainer Agent on daniel-pi

Lets the **server's** Portainer manage **daniel-pi**'s Docker as a second environment
(start/stop/recreate/exec/console/logs/stacks from one pane). See repo-root `CLAUDE.md` for
shared conventions and the `portainer` role for the server side.

## At a glance
- **Image:** `portainer/agent:2.43.0-alpine` — **pinned to match the server's `portainer-ce` 2.43.0**
  (the agent API version tracks the Portainer server it connects to), Renovate-managed, watchtower off.
- **Host:** daniel-pi ONLY (LAN-only host — [[daniel-pi-lan-only]]). Not on the server.
- **Port:** `9001` (TLS), published bound to the **Pi's LAN IP** (`server_ip` = the Pi's IP on a
  `-e target=daniel-pi` deploy), never `0.0.0.0`. **Not Traefik-routed, no Authelia.**
- **Networks:** portainer-agent (own isolation net) · **Depends on:** nothing (mounts the raw socket, not the docker-proxy).
- **Config in:** `ansible/inventory/host_vars/daniel-pi.yml` → `containers_list`

## Notable
- **No GitOps path to the Pi** — deploy by hand (the deployer runs on the server only):
  `uv run ansible-playbook ansible/deploy.yml --tags portainer-agent -e target=daniel-pi`
  (`-e target=`, NOT `--limit` — the play's `hosts:` defaults to the local hostname).
- **Security — the agent mounts the Docker socket = root-equivalent on the Pi** (same class as the
  server's Portainer socket-proxy, [[portainer-socket-proxy-exec-tradeoff]]). Contained by four things:
  1. **`AGENT_SECRET`** (SOPS `portainer_agent_secret`) — the Agent API rejects requests without it.
  2. **TLS** on :9001 (agent self-signs; Portainer trusts it on add).
  3. Port bound to the **Pi LAN IP only** (off the WireGuard tunnel / other interfaces).
  4. A **`DOCKER-USER` host rule** (`/etc/portainer-agent-firewall.sh`, applied by the
     `portainer-agent-firewall.service` systemd oneshot) that lets **only the server's IP** reach
     :9001 and drops the rest. Docker-published ports **bypass ufw**, so this lock canNOT live in
     ufw — it mirrors traefik's `docker-user-rules` mechanism. Reboot-safe (`After=docker.service`).
- **`cap_drop: ALL` is sufficient** — verified 2026-07-06 the agent starts + serves under it (root's
  owner-match on the root:docker socket needs no `DAC_OVERRIDE`; the daemon does the privileged work).
- **Socket mounted `:ro`** — API send/recv isn't a file write, so exec/console/create still work
  (same as the repo's socket-proxies POSTing through `:ro`). If exec/console ever fails, drop the `:ro`.
- **Healthcheck** = `nc -z localhost 9001` (busybox `nc` ships in the image; there is no built-in
  HEALTHCHECK). Interval follows the Pi's `container_healthcheck_interval` (60s) to limit fork churn.
- **Volume browsing is OFF** — `/var/lib/docker/volumes` is intentionally not mounted (least privilege).
  Add that mount if you want Portainer's volume file browser for the Pi.
- **dozzle stays** — Portainer's log view overlaps it, but dozzle is 6 MB, a nicer merged live-tail,
  and an independent failure domain (works if Portainer/the agent is down). Not redundant enough to drop.

## Registering the Pi in the server's Portainer (one-time, manual)
The `AGENT_SECRET` is MUTUAL — the **server's** Portainer injects a matching `AGENT_SECRET` from a file-mounted secret
(SOPS `portainer_agent_secret`, in the `portainer` role) so it authenticates to the agent
automatically; you do NOT paste the secret in the UI. Portainer environments live in Portainer's own
BoltDB, not in Ansible, so after both are deployed, add the environment once in the UI:
1. Server Portainer → **Environments** → **Add environment** → **Docker Standalone** → **Agent**.
2. **Name:** `daniel-pi`. **Environment address:** `10.0.0.139:9001`. Leave TLS at the default
   (the agent self-signs; the server trusts it). **Connect.**
3. Verify the Pi's containers appear and that **Console/exec** into a Pi container works.
If the environment shows **down**, the usual causes are the server's `AGENT_SECRET` not matching the
agent's (redeploy both after a `portainer_agent_secret` rotation) or the DOCKER-USER lock (only the
server IP may reach :9001).

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Firewall: `templates/portainer-agent-firewall.sh.j2` + `.service.j2`
- Deploy (Pi, from the server): `uv run ansible-playbook ansible/deploy.yml --tags portainer-agent -e target=daniel-pi`
