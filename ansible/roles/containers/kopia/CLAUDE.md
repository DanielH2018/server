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
  **Since 2026-07-03 the same cron also asserts the `daysFromHidingToDeleting: 7`
  lifecycle rule** (`rclone backend lifecycle`) and pages on drift — a purge-immediately
  mis-set makes billable bytes DROP (this monitor and b2_trend go greener) while silently
  destroying the undelete window above.
- **Backup assurance is three-tier:** snapshots (daily 00:00, in-container policy) →
  weekly `kopia snapshot verify --verify-files-percent=1` cron (blobs readable; `files/verify.sh`
  → `/var/lib/kopia-verify/state.json` → monitor-bridge's `verify` check → the "Backup Verify"
  Kuma monitor, which alerts on a failed/stale verify — the wrapper captures the verify's own
  exit code, which the old `... | logger` cron silently swallowed) →
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
  `/usr/local/bin/kopia-restore-drill.sh`. The drill also header-magic-checks `*.db`
  sentinels (karakeep/grafana) — the image has no `sqlite3`, so it asserts the
  `SQLite format 3` magic rather than a full `PRAGMA integrity_check`.
- **Retention (entrypoint policy):** 7 daily + 4 weekly + **3 monthly** (monthlies added
  2026-06-24 for a >28 d DR horizon; config-only source dedupes, so the B2 cost is small).
  The entrypoint also re-asserts `kopia maintenance set --owner me --enable-full true`
  idempotently — full maintenance is what actually GCs expired blobs from B2; without an
  owner running it the bucket grows unbounded (the `b2_usage` monitor only sees the symptom).
- **Bare-metal disaster recovery** (server gone — reconnect to B2 from a fresh host and
  restore everything): [`docs/kopia-disaster-recovery.md`](../../../../../docs/kopia-disaster-recovery.md).
  All five repo creds are in SOPS, which is DR-closed, so the capability survives a total loss.
- **The Pi is (almost) intentionally NOT in Kopia scope.** The snapshot source is only the server's
  `containers/`. daniel-pi runs stateless / Ansible-reconstructible services (docker-proxy, glances,
  dozzle, autoheal) that re-template on a redeploy. **The one exception (2026-07-04): wg-easy's peer
  configs** (`wg0.conf`/`wg0.json` — WireGuard private keys a redeploy can NOT rebuild). The wg-easy
  role runs a daily **daniel-server** cron (`/usr/local/bin/wg-easy-pull-pi-peers.sh`, 23:30, before
  the 00:00 snapshot) that `sudo rsync`-pulls the Pi's `containers/wg-easy/config/` into
  `containers/wg-easy/pi-peers/` on the server — inside this snapshot source — so an SD-card death
  doesn't force re-enrolling every VPN client. Everything else on the Pi stays out of scope by design.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Entry/ignore: `templates/entrypoint.sh.j2`, `kopiaignore.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "kopia"`
