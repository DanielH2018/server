# n8n — Workflow automation

n8n with an external task-runner sidecar. See repo-root `CLAUDE.md`.

## At a glance
- **Images:** built from `templates/Dockerfile.j2` (`n8n`) + `Dockerfile-runners.j2` (`n8n-runners`)
- **Host:** daniel-server · **Port:** 5678 · **URL:** `n8n.<domain>` (Authelia: yes)
- **Networks:** apps + `internal` (runner↔broker traffic stays on `internal`)
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **`n8n-runners` executes arbitrary workflow code** — the resource cap on it is the main
  DoS guard. It reaches the main container's broker at `n8n:5679` over `internal` using
  `n8n_runner_auth_token` (from secrets).
- **`/webhook/` bypasses Authelia** (public webhooks) via a dedicated higher-priority
  Traefik router. `/webhook-test/` is intentionally NOT exposed (dev-only endpoint).
- Both images are built — update via redeploy, not Watchtower.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Images: `templates/Dockerfile*.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "n8n"`
