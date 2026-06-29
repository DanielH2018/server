# n8n — Workflow automation

n8n with an external task-runner sidecar. See repo-root `CLAUDE.md`.

## At a glance
- **Images:** built from `templates/Dockerfile.j2` (`n8n`) + `Dockerfile-runners.j2` (`n8n-runners`)
- **Host:** daniel-server · **Port:** 5678 · **URL:** `n8n.<domain>` (Authelia: yes)
- **Networks:** apps + `internal` (the runner connects to the broker over `internal`, but the
  broker binds `0.0.0.0:5679` so it's ALSO reachable from `apps` siblings — the gate is
  `n8n_runner_auth_token`, NOT network isolation; see the broker note below)
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **`n8n-runners` executes arbitrary workflow code** — the resource cap on it is the main
  DoS guard. It reaches the main container's broker at `n8n:5679` over `internal` using
  `n8n_runner_auth_token` (from secrets).
- **`/webhook/` bypasses Authelia** (public webhooks) via a dedicated higher-priority
  Traefik router. `/webhook-test/` is intentionally NOT exposed (dev-only endpoint).
- Both images are built — update via redeploy, not Watchtower.
- **DR / encryption key:** the credential-encryption key lives in `./data/config` and the
  encrypted credentials in `./data/database.sqlite` — both inside the `./data` bind mount, so
  Kopia backs them up together (the restore drill even uses `n8n/data/config` as n8n's
  sentinel). Deliberately **NOT** also pinned in SOPS: it's redundant (key + credentials are
  co-located, so losing `./data` loses both — a separate SOPS copy of the key can't decrypt
  credentials that are gone), and setting `N8N_ENCRYPTION_KEY` to anything but the on-disk key
  crashes n8n with a key-mismatch. Don't "harden" this by adding it to secrets.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Images: `templates/Dockerfile*.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "n8n"`
