# homelab-mcp — read-only MCP server for live homelab state

Streamable-HTTP MCP server that lets Claude Code on a LAN/WireGuard client read this
server's live state — Prometheus metrics, Loki logs, container status, Scrutiny SMART,
Home Assistant state, and the `ansible/` source tree. **Read-only**: no tool starts/stops/
execs a container, writes a file, or reads a secret. See repo-root `CLAUDE.md`.

## At a glance
- **Image:** built from `templates/Dockerfile.j2` (`python:3.14-slim` + `mcp`/`httpx`/`uvicorn`),
  app in `files/app.py` + `files/safe_reads.py`
- **Host:** daniel-server · **Port:** `8000` (internal; routed only via Traefik)
- **Authelia:** **no** — a headless MCP client can't pass 2FA. Gated instead by a **bearer
  token**, checked at Traefik AND re-checked in-app (so a shared-net peer can't skip Traefik).
- **Networks:** `proxy` (Traefik reaches the route here), `monitoring` (prometheus/loki/
  uptime-kuma/scrutiny/docker-proxy), `apps` (home-assistant:8123, same precedent as monitor-bridge)
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **LAN-only route:** defined in Traefik's file provider (`traefik/templates/config.yml.j2`),
  matching **only** `mcp.local.<domain>` + `Header(Authorization, Bearer <homelab_mcp_token>)`.
  No public `mcp.<domain>` router is emitted, and a wrong/missing token 404s before the app —
  mirrors the `livesync` `X-Sync-Token` pattern, which keeps the token out of Docker labels.
- **Secret handling is the point of the MCP layer.** docker-proxy runs `CONTAINERS=1`, so
  `GET /containers/{id}/json` returns every container's `Env` (secrets); the tools build replies
  from an explicit non-secret allowlist (`safe_reads.strip_container_fields`) and never pass `Env`
  through. The file tools are jailed to the read-only `ansible/` mount and deny `secrets.yml` +
  key material. All of this lives in `safe_reads.py` and is unit-tested (`test_safe_reads.py`).
- Secrets: `homelab_mcp_token` (bearer, add via `/add-secret` before first deploy) and the existing
  `claude_ha_token` (HA reads). Image is built — update via redeploy, not Watchtower; a weekly
  Sunday rebuild cron (06:25) pulls the newest base.

## Client (PC) config
`~/.claude.json`, user scope. Inline the literal token — Claude Code only expands
`${VAR}` in a header if VAR is exported in the shell that launches `claude` (it is NOT
sourced from hook env like `local.env`), so a bare `${HOMELAB_MCP_TOKEN}` sends an empty Bearer:
```jsonc
{ "mcpServers": { "homelab": {
    "type": "http", "url": "https://mcp.local.<domain>/mcp",
    "headers": { "Authorization": "Bearer <homelab_mcp_token value>" } } } }
```

## Editing
- Compose: `templates/docker-compose.yml.j2` · App: `files/app.py`, `files/safe_reads.py`,
  `templates/Dockerfile.j2` · Route: `roles/containers/traefik/templates/config.yml.j2`
- Tests: `uv run pytest ansible/roles/containers/homelab-mcp/files`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "traefik,homelab-mcp"`
  (traefik tag re-renders the file-provider route)
