# n8n — Workflow automation

n8n with an external task-runner sidecar. See repo-root `CLAUDE.md`.

> **Migrated to k3s on 2026-08-06.** This role is the live service on daniel-box; deploy it
> with `--tags n8n` from there. The two images are built in-cluster with BuildKit by the
> sibling `k8s/n8n-images` role, from `roles/k8s/n8n-images/templates/Dockerfile.j2`,
> `Dockerfile-runners.j2` and `templates/config/n8n-task-runners.json.j2` — edit those there.
> The Docker role's compose template is gone (recover it from git history if ever needed);
> `containers/n8n/data` is still on disk from the migration. **Parts of this file still
> describe the pre-migration Docker deployment** — the notes on the broker, the webhook bypass
> and the encryption key all still hold, but the network and host details do not.

## At a glance
- **Images:** built by `k8s/n8n-images` from its `templates/Dockerfile.j2` (`n8n`) +
  `Dockerfile-runners.j2` (`n8n-runners`)
- **Host:** daniel-box (k8s) · **Port:** 5678 · **URL:** `n8n.<domain>` (Authelia: yes)
- **Networks:** apps + `internal` (the runner connects to the broker over `internal`, but the
  broker binds `0.0.0.0:5679` so it's ALSO reachable from `apps` siblings — the gate is
  `n8n_runner_auth_token`, NOT network isolation; see the broker note below)
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`

## Notable
- **`n8n-runners` executes arbitrary workflow code** — the resource cap on it is the main
  DoS guard. It reaches the main container's broker at `n8n:5679` over `internal` using
  `n8n_runner_auth_token` (from secrets).
- **`/webhook/` bypasses Authelia** (public webhooks) via a dedicated higher-priority
  Traefik router. `/webhook-test/` is intentionally NOT exposed (dev-only endpoint).
- Both images are built — update via redeploy, not Watchtower.
- **DR / encryption key:** the credential-encryption key lives in `./data/config` and the
  encrypted credentials in `./data/database.sqlite` — both inside the `./data` bind mount, so
  they are backed up together on n8n's Longhorn PVC (Kopia, and its restore drill, retired
  2026-08-13). Deliberately **NOT** also pinned in SOPS: it's redundant (key + credentials are
  co-located, so losing `./data` loses both — a separate SOPS copy of the key can't decrypt
  credentials that are gone), and setting `N8N_ENCRYPTION_KEY` to anything but the on-disk key
  crashes n8n with a key-mismatch. Don't "harden" this by adding it to secrets.

## Editing
- Images: `templates/Dockerfile*.j2` + `templates/n8n-task-runners.json.j2` (built/copied by
  the `n8n-images` k8s role)
- Deploy (from daniel-box): `uv run ansible-playbook ansible/deploy.yml --tags "n8n"`
