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
- **The key is pinned to one operation.** sshd authorizes it with
  `restrict,command="/usr/local/bin/pi-peer-backup-shell <src dir>"`; the wrapper
  (`files/pi-peer-backup-shell.sh`, root-owned on the Pi) re-derives one
  `sudo rsync --server --sender` of `pi_peer_backup_src_dir` from `SSH_ORIGINAL_COMMAND` and
  refuses a login, a receiver, another path or any long option but `--timeout=N`. It
  validates the request structurally rather than pinning the argv, because rsync's
  short-option blob is version-negotiated and moves with the alpine base image. Until
  2026-09-17 the key carried no options, so reading the Secret was a shell as a NOPASSWD-sudo
  user on the Pi (#1927). `ansible/tests/services/test_pi_peer_backup_forced_command.py` runs
  the wrapper against the captured request and five refusals.
- **sudo rsync** on the Pi is required (files are root-owned); the ubuntu user there
  has NOPASSWD sudo. The wrapper's exec'd argv stays `sudo rsync …`, so it works under a
  rule scoped to rsync as well as under `ALL`.
- **Alerting:** the job pushes "WG Pi Peer Backup" (Kuma push monitor, 2.5-day window)
  directly — up on success with the file count, down with the rsync error or the
  file-count-floor breach (>= 2 files required; no `--delete`, so an empty source can
  never wipe the copy). It also pings an off-premises Healthchecks.io check
  (`HC_PING_URL`, slug `pi-peer-backup`) when one is configured: Kuma resolves to a
  Service in this cluster, so a cluster outage silences both the push and the monitor
  waiting for it. See `docs/healthchecks-io-deadman.md`.
  The script writes its verdict and any failed push to stdout in the host crons' syslog
  shape (`<ts> <pod> pi-peer-backup: status=… ` / `push failed (http=… rc=…[ by=kuma]) (…)`)
  so monitor-bridge's Swallowed Push Verdicts check reads this pusher too, through its
  `{container="pull"}` selector (#1943) — a Kuma-rejected push here pages within one bridge cycle.
  **What the monitor means:** "the nightly 23:30 run happened," not "some run happened
  recently." `k8s/cronjob-gate` runs a one-off Job (`pi-peer-backup-deploy-gate`) on every
  deploy of this role to prove a bumped image still starts; `files/pull-pi-peers.sh`
  recognizes that Job by its pod's hostname prefix and skips both pushes for it, so a routine
  Renovate-driven deploy can't mask a missed scheduled firing or page from a deploy-time probe
  instead of the backup itself. `configarr` — the other `k8s/cronjob-gate` caller — takes the
  OPPOSITE position: its health reader counts a gate run like any other finished Job, because a
  configarr gate run genuinely performs the reconcile, where this role's gate run is a dead-man
  push about a specific 23:30 firing. See `ansible/roles/k8s/configarr/CLAUDE.md`.
- **Manual run/proof:** `kubectl -n homelab create job ppb-manual --from=cronjob/pi-peer-backup`
  (this bypasses the gate-run hostname check, so a manual proof run DOES push — same as a
  scheduled run).

## Editing
- Script: `files/pull-pi-peers.sh` (baked into the image — redeploy rebuilds)
- Deploy (on daniel-box): `uv run ansible-playbook ansible/deploy.yml --tags "pi-peer-backup"`
- Deploy-time gate: `k8s/cronjob-gate`, included after the manifests deploy task. Its default
  timeout (660s) exceeds this CronJob's `activeDeadlineSeconds` (600s); see
  `ansible/roles/k8s/cronjob-gate/CLAUDE.md` for what it proves and does not prove.
