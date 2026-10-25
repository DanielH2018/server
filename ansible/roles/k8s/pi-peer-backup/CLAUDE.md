# pi-peer-backup — nightly pull of daniel-pi's WireGuard peer keys

Successor to the daniel-server peer-pull host cron (host flip 1, 2026-08-14). The Pi's
wg-easy peer configs (wg0.conf/wg0.json — client private keys, un-rebuildable) rsync
nightly into a Longhorn PVC; the Saturday weekly shard (weekly-backup-d6) carries it to B2. This
replaced the retired Kopia scope for the Pi.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=pi-peer-backup` with exit 5
- **Image:** `ghcr.io/danielh2018/pi-peer-backup` (`pi_peer_backup_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `pi-peer-backup-data` (weekly -> B2 (default target))
<!-- /generated_from -->

- **Flux applies it, not Ansible** (#5652, Wave C). Its `containers_list` entry carries
  `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it. A merged change
  reaches the cluster when `promote` publishes and verifies the next bundle. To give it back
  to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects. Its Kustomization renders `decryption`, because the
  private key arrives as the SOPS manifest `pi-peer-backup.ssh-key-secret.sops.yaml`.
- **Shape:** CronJob 23:00 (America/Chicago), built image (alpine + rsync/ssh/curl + the
  script), any node. The promote workflow's `images` job builds the image and pushes
  `ghcr.io/danielh2018/pi-peer-backup` under its content tag. The defaults name
  `<k8s_registry_pull_host>`; the renderer rewrites the ref to GHCR with `imagePullPolicy:
  IfNotPresent`. The image is 8.9 MB, so a cold pull is short.
- **The Pi half is in the Pi's host package.** The forced-command wrapper and the authorized
  public key are installed on daniel-pi by the `pi-peer-backup-target` component of
  `homelab-daniel-pi` (`hosts/components/pi-peer-backup-target/`) since H7.25. The component's
  authorized-key line holds a copy of `pi_peer_backup_src_dir`, which
  `hosts/lib/tests/test_pi_tuning_component.py` holds equal to this role's default.
- **Auth:** dedicated ed25519 key (`pi_peer_backup_ssh_key` in SOPS; public half
  authorized on the Pi by the `pi-peer-backup-target` component). The private key has a Secret of its own,
  `pi-peer-backup-ssh-key`, rendered from `templates/ssh-key-secret.yaml.j2`, because Flux
  strips the newlines from a placeholder's value (target slice R2, PR 6). The role lists that
  file in `render_sops_manifests`, so a placeholder render leaves it out. The CronJob projects
  it and `pi-peer-backup-ssh` into one volume at `/ssh`, so the script still reads `/ssh/id`.
  The Pi's host key is pinned from the committed `files/daniel-pi-ssh-host-ed25519.pub`, so
  `pi-peer-backup-ssh` renders from git alone (target slice R3); the job runs
  `StrictHostKeyChecking=yes` — never TOFU. To re-pin after the Pi is re-keyed, read the key
  over the operator's trusted connection with
  `ssh daniel-pi cat /etc/ssh/ssh_host_ed25519_key.pub`, commit it, and merge; `promote`
  applies the new `pi-peer-backup-ssh` Secret.
- **The key is pinned to one operation.** sshd authorizes it with
  `restrict,command="/usr/local/bin/pi-peer-backup-shell <src dir>"`; the wrapper
  (`hosts/components/pi-peer-backup-target/pi-peer-backup-shell.sh`, root-owned on the Pi) re-derives one
  `sudo rsync --server --sender` of `pi_peer_backup_src_dir` from `SSH_ORIGINAL_COMMAND` and
  refuses a login, a receiver, another path, any long option but `--timeout=N`, and `-s`
  (`--secluded-args`, which re-reads the path from the protocol stream and would bypass the
  pinned directory, #2015) — matched in the letters BEFORE the blob's `e`, since the `s` in
  the capability list after it (`e.iLsfxCIvu`) is a capability, the split rrsync makes. It
  validates the request structurally rather than pinning the argv, because rsync's
  short-option blob is version-negotiated and moves with the alpine base image. Until
  2026-09-17 the key carried no options, so reading the Secret was a shell as a NOPASSWD-sudo
  user on the Pi (#1927). `ansible/tests/services/test_pi_peer_backup_forced_command.py` runs
  the wrapper against the captured request and eight refusals.
- **sudo rsync** on the Pi is required (files are root-owned); the ubuntu user there
  has NOPASSWD sudo. The wrapper's exec'd argv stays `sudo rsync …`, so it works under a
  rule scoped to rsync as well as under `ALL`.
- **Alerting:** the job pushes "WG Pi Peer Backup" (Kuma push monitor, 2.5-day window)
  directly — up on success with the file count, down with the rsync error or the
  file-count-floor breach (>= 2 files required; no `--delete`, so an empty source can
  never wipe the copy). A host key that no longer matches the pin pushes down with
  `daniel-pi host key differs from the committed pin` instead of the generic rsync error.
  `ansible/tests/services/test_pi_peer_backup_host_key_failure.py` holds that branch apart
  from an unreachable Pi, which also exits 255. The job also pings an off-premises Healthchecks.io check
  (`HC_PING_URL`, slug `pi-peer-backup`) when one is configured: Kuma resolves to a
  Service in this cluster, so a cluster outage silences both the push and the monitor
  waiting for it. See `docs/healthchecks-io-deadman.md`.
  The script writes its verdict and any failed push to stdout in the host crons' syslog
  shape (`<ts> <pod> pi-peer-backup: status=… ` / `push failed (http=… rc=…[ by=kuma]) (…)`)
  so monitor-bridge's Swallowed Push Verdicts check reads this pusher too, through its
  `{container="pi-peer-backup"}` selector (#1943) — a Kuma-rejected push here pages within one bridge cycle.
  **What the monitor means:** "the nightly 23:00 run happened," not "some run happened
  recently." The gate run below skips both pushes, so a routine promotion can't mask a missed
  scheduled firing or page from a deploy-time run instead of the backup itself. `configarr`
  takes the OPPOSITE position: its health reader counts a gate run like any other finished
  Job, because a configarr gate run genuinely performs the reconcile, where this role's gate
  run is a dead-man push about a specific 23:00 firing. See
  `ansible/roles/k8s/configarr/CLAUDE.md`.
- **The verdict is also exported** (M6 batch B7). After each scheduled run the script sends one
  merge PATCH, with curl and the pod's ServiceAccount token, that sets `homelab/check-verdict`,
  `homelab/check-last-run` and `homelab/check-message` on the ConfigMap `pi-peer-backup-verdict`
  (`templates/verdict-configmap.yaml.j2`, rendered with no annotations so the script is their
  only writer). kube-state-metrics exports the verdict, and the `pods` rule group's
  `PiPeerBackupFailed` and `PiPeerBackupNotReporting` read it in shadow. `templates/rbac.yaml.j2`
  grants `patch` on that one name. The CronJob mounts the token for that call alone, so the
  backup pod holds an API token it did not hold before. The message is stripped of IPv4
  addresses and JSON-hostile characters. A failed export makes the run exit 1, so the CronJob's
  last successful time stops moving. The gate run skips the export, like the pushes.
- **The gate Job, in `pi-peer-backup-verify`.** `defaults/main.yml` names the CronJob in
  `flux_cronjob_gates` as a verify gate, so the renderer copies it into the Job
  `pi-peer-backup-deploy-gate-<spec digest8>` (`scripts/renderer/renderer_lib/cronjob_gate.py`).
  Flux runs it once per role revision, after the `pi-peer-backup` Kustomization reads Ready.
  `files/pull-pi-peers.sh` recognizes the run by its pod's `pi-peer-backup-deploy-gate-`
  hostname prefix, the same prefix `k8s/cronjob-gate`'s Job carried before the flip. Two
  things changed with the move, and the flip accepted both:
    - **A failed run now holds the revision.** The gate sets `backoffLimit: 0`, and any
      non-zero exit fails the `-verify` Kustomization, so a Pi that is down or unreachable
      during a promotion holds it. `k8s/cronjob-gate` only reported a non-zero exit.
    - **`Forbid` no longer keeps a scheduled firing off a gate run.** The copy carries no
      ownerReference to the CronJob, so the two can overlap in both directions. The mirror is
      retention-free and idempotent. The claim is ReadWriteOnce, so two pods on different
      nodes give the second a Multi-Attach wait until its 600s `activeDeadlineSeconds`, and
      the verify Kustomization's 660s `timeout` reports that as a failed gate.
- **Manual run/proof:** `kubectl -n homelab create job ppb-manual --from=cronjob/pi-peer-backup`
  (this bypasses the gate-run hostname check, so a manual proof run DOES push — same as a
  scheduled run).

## Editing
- Script: `files/pull-pi-peers.sh` (baked into the image — a merged edit moves the content tag,
  and `promote` builds the image and runs the gate).
- Gate: `flux_cronjob_gates` in `defaults/main.yml`. `tasks/main.yml` keeps the image-builder
  and manifests includes for a handback, and no Flux path runs them.
- Pi half: `hosts/components/pi-peer-backup-target/component.yml`, with its two files.
