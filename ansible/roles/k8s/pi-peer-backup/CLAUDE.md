# pi-peer-backup — nightly pull of daniel-pi's WireGuard peer keys

Successor to the daniel-server peer-pull host cron (host flip 1, 2026-08-14). The Pi's
wg-easy peer configs (wg0.conf/wg0.json — client private keys, un-rebuildable) rsync
nightly into a Longhorn PVC; the 03:30 daily-backup group carries it to B2. This
replaced the retired Kopia scope for the Pi.

## At a glance
- **Shape:** CronJob 23:30 (America/Chicago), built image (alpine + rsync/ssh/curl + the
  script), any node (post-re-plumb registry pulls work everywhere).
- **Auth:** dedicated ed25519 key (`pi_peer_backup_ssh_key` in SOPS; public half
  authorized on the Pi by this role). Host key pinned at deploy over Ansible's own
  connection; the job runs `StrictHostKeyChecking=yes` — never TOFU.
- **sudo rsync** on the Pi is required (files are root-owned); the ubuntu user there
  has NOPASSWD sudo.
- **Alerting:** the job pushes "WG Pi Peer Backup" (Kuma push monitor, 2.5-day window)
  directly — up on success with the file count, down with the rsync error or the
  file-count-floor breach (>= 2 files required; no `--delete`, so an empty source can
  never wipe the copy).
- **Manual run/proof:** `kubectl -n homelab create job ppb-manual --from=cronjob/pi-peer-backup`

## Editing
- Script: `files/pull-pi-peers.sh` (baked into the image — redeploy rebuilds)
- Deploy (on daniel-box): `uv run ansible-playbook ansible/deploy.yml --tags "pi-peer-backup"`
