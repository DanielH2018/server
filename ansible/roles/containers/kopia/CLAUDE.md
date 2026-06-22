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
- **B2 usage monitor (2026-06-11):** the bucket is the **10 GB free tier**; a daily host
  cron (`files/b2-usage.sh` → `/usr/local/bin/kopia-b2-usage.sh`, 02:30) measures
  **billable** bytes (`rclone size --b2-versions` — hidden versions count, `kopia blob
  stats` undercounts) with creds read at runtime from `repository.config`, and writes
  `/var/lib/kopia-b2-usage/state.json` — monitor-bridge's `b2_usage` check alerts at 85%
  of the cap, on probe failure, or staleness. Was 6.56 GB (66%) when added.
- **Backup assurance is three-tier:** snapshots (daily 19:00, in-container policy) →
  weekly `kopia snapshot verify --verify-files-percent=1` cron (blobs readable) →
  **monthly restore drill** (`files/restore-drill.sh` → `/usr/local/bin/`, cron 1st
  05:00): restores one rotating service dir (rotation folds in the year — `(month+year) %
  len` — so the singly-covered slot, incl. authelia the SSO root of trust, moves year over
  year) inside the container and asserts a **service-specific state-file sentinel** (e.g.
  `grafana/data/grafana.db`, `authelia/config/configuration.yml` — proves the right tree
  with real data, not just any compose file) plus a file-count floor. Two extra integrity
  guards: it fails if the **latest snapshot is >48 h old** (catches a stalled scheduler,
  independent of which snapshot it restores), and **quarterly restores the OLDEST retained
  snapshot** instead of the newest to exercise the retention tail (the real DR case). Writes
  `/var/lib/kopia-restore-drill/state.json` — monitor-bridge's `restore_drill` check
  alerts on failure, >35 d staleness, or missing state. Run it manually anytime:
  `/usr/local/bin/kopia-restore-drill.sh`.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Entry/ignore: `templates/entrypoint.sh.j2`, `kopiaignore.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "kopia"`
