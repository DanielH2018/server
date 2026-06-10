# kopia — Encrypted backup client

Kopia backup server/UI for encrypted, deduplicated backups. See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `kopia/kopia` (version-pinned, Renovate-managed)
- **Host:** daniel-server · **Port:** 51515 · **URL:** `kopia.<domain>` (Authelia: yes)
- **Networks:** `kopia` — a dedicated isolation net shared only with Traefik
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **Runs intentionally unauthenticated** (workaround for a Kopia basic-auth bug). This is
  by design — don't re-flag it as a vuln. Authelia in front + the dedicated `kopia`
  network (no other app container can reach `kopia:51515`) are the compensating controls.
- `templates/entrypoint.sh.j2` starts the server; `templates/kopiaignore.j2` is the
  global exclude list.
- **B2 bucket lifecycle (2026-06-10):** the repo speaks B2's *S3 endpoint*, where deletes
  only HIDE versions — without a lifecycle rule the bucket billed 7.6 GiB for a 4.6 GiB
  repo (years of maintenance churn). Rule now: `daysFromHidingToDeleting: 7` — a one-week
  undelete window (deliberate: kopia is unauthenticated, hidden versions are the last
  defense against a backup wipe) after which B2 purges. Inspect/adjust via rclone inside
  the container (creds from `/app/config/repository.config`):
  `rclone backend lifecycle b2:daniel-server-kopia` · billable size:
  `rclone size b2:daniel-server-kopia --b2-versions` · purge hidden now: `rclone cleanup`.
- **Backup assurance is three-tier:** snapshots (daily 19:00, in-container policy) →
  weekly `kopia snapshot verify --verify-files-percent=1` cron (blobs readable) →
  **monthly restore drill** (`files/restore-drill.sh` → `/usr/local/bin/`, cron 1st
  05:00): restores one rotating service dir from the latest snapshot inside the
  container, asserts sanity (compose-file sentinel + file-count floor), writes
  `/var/lib/kopia-restore-drill/state.json` — monitor-bridge's `restore_drill` check
  alerts on failure, >35 d staleness, or missing state. Run it manually anytime:
  `/usr/local/bin/kopia-restore-drill.sh`.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Entry/ignore: `templates/entrypoint.sh.j2`, `kopiaignore.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "kopia"`
