# Healthchecks.io — the off-premises dead-man's switch

Every heartbeat in this homelab pushes to Uptime Kuma, which runs on the cluster those
heartbeats watch. A cluster or WAN outage therefore silences the push *and* the monitor
waiting for it: the failure and the alarm share a failure domain, and nothing alerts.

Healthchecks.io closes exactly that hole. It is off-site, and it alerts on **silence** —
the one signal an on-prem monitor structurally cannot produce. It does not replace Kuma;
it is a second destination for the handful of jobs whose silence actually matters.

## What is wired

| Check slug | Source | Cadence | Pings `/fail` when |
|---|---|---|---|
| `longhorn-backup-health` | `roles/setup/k3s/templates/longhorn-backup-health.sh.j2` | every 10 min | target unavailable, backups stale/missing/errored, or the daily count exceeds the B2 budget |
| `daniel-box-disk-health` | `roles/setup/k3s/templates/disk-health.sh.j2` | every 10 min | `/` over the headroom threshold, or `df` fails |
| `manifest-prune-check` | `roles/setup/k3s/templates/manifest-prune-check.sh.j2` | 05:15 daily | live IngressRoute/Middleware/Service objects absent from the staged manifests |
| `pi-peer-backup` | `roles/k8s/pi-peer-backup/files/pull-pi-peers.sh` | 23:30 daily | rsync failed, or fewer than 2 peer files landed |

## What leaves the house

Healthchecks.io stores the ping body (first 100 kB per ping), so the body is the disclosure
surface. Two of the four send a generic string instead of their Kuma message, because theirs
name internal infrastructure:

| Check | Body sent off-site | Why |
|---|---|---|
| `longhorn-backup-health` | full message | Names namespaces/PVCs, and the backup-target condition can carry the B2 bucket. **Kept deliberately** — this is the one whose detail makes a 3am page actionable. Drop the `--data-raw` argument to send status only. |
| `daniel-box-disk-health` | full message | A disk percentage. Nothing to withhold. |
| `manifest-prune-check` | generic | Its message names live IngressRoute/Middleware objects — internal service and hostname fragments. |
| `pi-peer-backup` | generic | An rsync failure echoes `PI_SRC`, which carries the Pi's LAN IP and ssh user. |

The withheld detail is still in Kuma, on the LAN. The off-site copy only has to carry *that*
something is wrong; the diagnosis is available as soon as you can reach the house.

## The ping key

A **ping key** (Settings → Ping key), not an API key. It is write-only: it cannot read check
state, list checks, or manage anything. Leaking it therefore does not disclose data — it lets
someone hold your checks green, suppressing the alerts this mechanism exists to deliver. That
is why it is not inlined into the heartbeat scripts.

Those scripts are `0755` (the `{{ sys_user }}` crons must execute them), so anything inlined in
them is readable by every local account on daniel-box — which is already true of the Kuma push
tokens sitting in them today. The ping key has a wider blast radius than a Kuma token (one
spoofs a single monitor, the other spoofs every check in the project), so it lives in
`/etc/healthchecks/ping.env` at `0640 root:{{ sys_user }}` and is sourced at runtime. That
covers both callers: `manifest-prune-check` runs as root, the other two as `{{ sys_user }}`.
Same shape as `/etc/renovate-notify/config.env`.

For `pi-peer-backup` the URL is a key in the existing k8s Secret, not a file on a host.

## Activating it

The wiring is inert until a ping key exists — `healthchecks_ping_key` renders empty and every
call site skips its block, leaving the hosts exactly as they were. To turn it on:

1. **Create the four checks by hand** in the Healthchecks.io console, using the slugs in the
   table above. Set each one's period and grace to match the cadence column — a check whose
   period disagrees with its cron fires false alarms, and false alarms are how monitoring
   gets ignored.

   Create them manually rather than letting the URL auto-provision them. Auto-provisioning
   turns a typo'd slug into a second, unwatched check that reads green forever because
   something is pinging it. `ansible/tests/test_healthchecks_pings.py` fails the build if a
   call site ever adds the auto-provisioning parameter.

2. **Add the project ping key** (Settings → Ping key, *not* an API key):

   ```bash
   sops ansible/vars/secrets.yml          # add: healthchecks_ping_key: <key>
   uv run python scripts/secret_rotation.py sync
   ```

   One key covers every check — the slug in the URL selects which one. Adding a fifth check
   later needs no new secret.

3. **Deploy** the four call sites:

   The three host heartbeats live in the k3s setup role, which `k3s-bringup.yml` runs —
   `scripts/deploy.sh` is hardcoded to `deploy.yml` and will not reach them, so take the
   git-tree lock by hand for that one:

   ```bash
   flock -w 180 /var/lock/server-git-tree.lock \
     uv run ansible-playbook ansible/k3s-bringup.yml \
     --tags "backup-health,disk-health,manifest-prune"

   ./scripts/deploy.sh --tags "pi-peer-backup"
   ```

   `pi-peer-backup` bakes its script into an image, so this rebuilds it. The CronJob needs no
   pod-annotation dance to pick up the changed Secret: each scheduled run creates a fresh pod
   that reads it at start.

4. **Point the checks at Discord** — the same webhook the rest of the homelab alerts to. The
   free tier allows the integration.

## Verifying

The `/10`-minute checks report within ten minutes of the deploy. To force the daily ones:

```bash
sudo /usr/local/bin/manifest-prune-check.sh
sudo k3s kubectl -n homelab create job ppb-manual --from=cronjob/pi-peer-backup
```

A failed ping is logged locally (`logger -t <script>`, or the job's stderr) and nowhere else,
which is deliberate — a ping that cannot leave the house is precisely the case Healthchecks.io
catches by silence.

## What is deliberately not wired

- **`gitops-deploy.service`** — the role documents "no Kuma pushing from the deployer" as a
  decision: liveness is a `/var/lib/gitops-deploy/last_run` timestamp that monitor-bridge
  reads. Adding a push here would reverse that decision, which is a separate call to make.
- **Longhorn's `daily-backup` / `weekly-backup` CronJobs** — generated by Longhorn from its
  RecurringJob CRDs, not rendered by this repo, so there is no command to append to.
  `longhorn-backup-health` covers the same ground and covers it better: it asserts backup
  *freshness and coverage* rather than that a job ran.
- **The other ~15 Kuma push sites.** Free-tier headroom is 20 checks and four are used; the
  limit is not the reason. An off-site check earns its slot only where the silence is both
  invisible and consequential, and adding the rest would mostly duplicate what Kuma already
  reports correctly whenever the cluster is up enough to have a problem worth reporting.
