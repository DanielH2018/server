# Healthchecks.io — the off-premises dead-man's switch

Every heartbeat in this homelab pushes to Uptime Kuma, which runs on the cluster those
heartbeats watch. A cluster or WAN outage therefore silences the push *and* the monitor
waiting for it: the failure and the alarm share a failure domain, and nothing alerts.

Healthchecks.io closes exactly that hole. It is off-site, and it alerts on **silence** —
the one signal an on-prem monitor structurally cannot produce. It does not replace Kuma;
it is a second destination for the handful of jobs whose silence actually matters.

## What is wired

| Check slug | Source | Pings `/fail` when |
|---|---|---|
| `longhorn-backup-health` | `roles/setup/k3s/templates/longhorn-backup-health.sh.j2` | target unavailable, backups stale/missing/errored, or the daily count exceeds the B2 budget |
| `daniel-box-disk-health` | `roles/setup/k3s/templates/disk-health.sh.j2` | `/` over the headroom threshold, or `df` fails |
| `etcd-snapshot-offbox` | `roles/setup/k3s/templates/etcd-snapshot-offbox.sh.j2` | the snapshot or its R2 upload failed, the credentials file is missing or incomplete, or snapshots are disarmed |
| `manifest-prune-check` | `roles/setup/k3s/templates/manifest-prune-check.sh.j2` | any of its three arms: live routing or workload objects absent from the staged manifests; a `copy:`-deployed artifact differing byte-for-byte from the repo; or a `template:`-rendered setup script whose source checksum no longer matches the render manifest this host wrote |
| `pi-peer-backup` | `roles/k8s/pi-peer-backup/files/pull-pi-peers.sh` | rsync failed, or fewer than 2 peer files landed |
| `registry-gc` | `roles/k8s/registry/templates/registry-gc.sh.j2` | GC job exceeded its deadline, the registry pod would not terminate, the job manifest failed to apply, or the registry did not come back afterwards |
| `uptime-kuma-alive` | `roles/setup/k3s/templates/longhorn-backup-health.sh.j2` | that script's Kuma push returned non-zero — see *Watching the spine itself* below |

The cron behind each slug, read from the variables that set it:

<!-- Generated from the cron variables in the k3s, pi-peer-backup and registry roles; edit those. -->
--8<-- "assets/generated/fragments/deadman-cadences.md"

Cadences are the host's clock, and daniel-box runs **UTC** — not the `America/Chicago` the
containers use. A check created in Chicago time drifts 5-6 hours from the cron that feeds it
and fires false alarms.

## Period and grace — the console settings

These live only in the Healthchecks.io console; nothing in this repo can set them. Until
2026-08-30 only three of the seven were written down anywhere, so the other four could not be
verified against their crons at all. Set them to match this table.

The period or expression is the cron in the table above: a `*/10` minute cron is a Simple
check with a 10-minute period, and every other slug is a Cron check carrying that expression
in UTC.

| Check slug | Schedule type | Grace |
|---|---|---|
| `longhorn-backup-health` | Simple | 20 minutes |
| `daniel-box-disk-health` | Simple | 20 minutes |
| `uptime-kuma-alive` | Simple | 20 minutes |
| `manifest-prune-check` | Cron | 1 hour |
| `etcd-snapshot-offbox` | Cron | 1 hour |
| `pi-peer-backup` | Cron | 1 hour |
| `registry-gc` | Cron | 1 hour |

**The 20-minute grace on the three 10-minute checks is derived, not chosen.** Their crons skip
their first run after a boot — `boot_grace_active` in `kuma-push-lib.sh`, sized by
`k3s_health_cron_boot_grace_s` — so the widest legitimate gap between two pings is *two* periods,
20 minutes, not one. The grace has to cover that gap and nothing wider: at 30 minutes a genuinely
dead cron would sit undetected through three slots.

That bound is what keeps the boot grace itself under 600 seconds. A boot grace longer than the
cron interval could skip two consecutive slots, widening the legitimate gap to 30 minutes and
making this table wrong. `ansible/tests/setup/test_health_cron_boot_grace.py` enforces the relation
rather than either number.

**A `/fail` ping ignores all of this.** Grace bounds *silence*; a `/fail` is an explicit failure
report and alerts on arrival. That is why the fix for post-boot noise is a skipped run rather
than a wider grace — see the boot-grace note under *Watching the spine itself*.

## Watching the spine itself

The five job checks above all answer "did this job run and succeed." `uptime-kuma-alive`
answers a different question: is the thing every alert terminates in still working.

The gap it closes: `longhorn-backup-health` and `daniel-box-disk-health` ping hc-ping.com
whether or not their Kuma push succeeded, which is right for a backup check and leaves Kuma
itself uncovered. A dead Kuma pod, a wedged SQLite, or a NetworkPolicy that fences off :3001
silences every one of the ~82 tiles — there is no Alertmanager, so nothing else pages — while
all six checks above keep reporting green. A *cluster* outage is caught, because these scripts
go silent with it. A Kuma-only outage was caught by nothing.

`longhorn-backup-health` carries its Kuma push's exit status to this second slug: success
pings, failure pings `/fail`. It was chosen over a dedicated cron because it already runs every
10 minutes and already talks to Kuma — no new unit, no new credential.

Set the check's **period to 10 minutes and its grace to 20**, matching the cron. It is not a
job check, so `/start` is never sent and the duration column stays empty; that is expected.

### The first run after a boot is skipped

Both slugs this script feeds page on a `/fail`, and a `/fail` alerts immediately whatever the
grace says. So a cron firing seconds after a boot — against a cluster whose pods have not
started — pages every time the host restarts, and no console setting can prevent it.

That is the 2026-08-30 restart, exactly: daniel-box came up at 07:39:48, this cron ran at
07:40:00, and the last pod reached Ready at 07:45:06. Both `longhorn-backup-health` and
`uptime-kuma-alive` sent `/fail` at 07:40 for an outage that ended five minutes later.

The script now calls `boot_grace_active` and exits 0 while the host's uptime is under
`k3s_health_cron_boot_grace_s`, so the first slot is skipped and the second reports honestly.
The guard **fails open**: an unreadable `/proc/uptime` runs the check, because a guard that
cannot read the clock must not silence the dead-man indefinitely.

It covers only the `*/10` crons. For a daily check a skipped slot is a skipped day, which is a
worse trade than the rare boot landing inside its one-minute window — and their 1-hour graces
already tolerate a late run.

What it does not cover: Kuma accepting pushes while failing to *deliver* notifications. The
`Discord Delivery` tile watches the Discord leg from inside Kuma, and the email tier is the
second channel. A Kuma that accepts a push and then drops the alert still reads green here.

## What leaves the house

Healthchecks.io stores the ping body (first 100 kB per ping), so the body is the disclosure
surface. Two of the six send a generic string instead of their Kuma message, because theirs
name internal infrastructure, and one sends no body at all:

| Check | Body sent off-site | Why |
|---|---|---|
| `longhorn-backup-health` | full message | Names namespaces/PVCs, and the backup-target condition can carry the B2 bucket. **Kept deliberately** — this is the one whose detail makes a 3 AM page actionable. Drop the `--data-raw` argument to send status only. |
| `daniel-box-disk-health` | full message | A disk percentage. Nothing to withhold. |
| `etcd-snapshot-offbox` | none | Status only. Its failure message can carry the R2 bucket name and k3s's own error text; the bucket is the same one every Longhorn backup lives in, so nothing about it goes off-site. The detail is in Kuma. |
| `manifest-prune-check` | generic | Its message names live IngressRoute/Middleware objects — internal service and hostname fragments. |
| `pi-peer-backup` | generic | An rsync failure echoes `PI_SRC`, which carries the Pi's LAN IP and ssh user. |
| `registry-gc` | full message | A blob/link count, or a failure naming the cluster namespace and the `registry` Deployment. Low value to an outsider and it is what makes the failure actionable. |
| `uptime-kuma-alive` | none | Status only, and status is the whole signal — it reports on Kuma, not on what Kuma found. |

The withheld detail is still in Kuma, on the LAN. The off-site copy only has to carry *that*
something is wrong; the diagnosis is available as soon as you can reach the house.

## The ping key

A **ping key** (Settings → Ping key), not an API key. It is write-only: it cannot read check
state, list checks, or manage anything. Leaking it therefore does not disclose data — it lets
someone hold your checks green, suppressing the alerts this mechanism exists to deliver. That
is why it is not inlined into the heartbeat scripts.

The `{{ sys_user }}`-run scripts are `0755` (their crons must execute them), so anything inlined
in them is readable by every local account on daniel-box — which is already true of the Kuma push
tokens sitting in them today. The ping key has a wider blast radius than a Kuma token (one
spoofs a single monitor, the other spoofs every check in the project), so it lives in
`/etc/healthchecks/ping.env` at `0640 root:{{ sys_user }}` and is sourced at runtime. That
covers every caller: `manifest-prune-check` and `registry-gc` run as root, the other two as
`{{ sys_user }}`. Same shape as `/etc/renovate-notify/config.env`.

(`registry-gc.sh` is `0700 root:root` — a root-only cron, so it never needed the `0755` the
others do. The env file is still the right home for the key: keeping it out of the scripts is
what makes the mode of any one of them stop mattering.)

For `pi-peer-backup` the URL is a key in the existing k8s Secret, not a file on a host.

## Activating it

The wiring is inert until a ping key exists — `healthchecks_ping_key` renders empty and every
call site skips its block, leaving the hosts exactly as they were. To turn it on:

1. **Create the six checks by hand** in the Healthchecks.io console, using the slugs in the
   table above. Set each one's period and grace to match the cadence column — a check whose
   period disagrees with its cron fires false alarms, and false alarms are how monitoring
   gets ignored.

   For a job that runs on a cron rather than an interval, use the console's **Cron** schedule
   type and paste the same expression, so the two cannot drift. `registry-gc` is `20 4 * * 0`
   in **UTC**, grace 1 hour — its own deadline is 20 min, plus up to 120s for the registry pod
   to terminate and 180s for the rollout back up, so an hour covers a slow run with margin.
   `etcd-snapshot-offbox` is `45 2 * * *` in **UTC**, grace 1 hour — the snapshot itself is
   seconds and the upload is ~30 MB, but nothing in the script bounds how long `k3s
   etcd-snapshot save` may block, so the grace is what distinguishes a slow run from a hung one.

   Create them manually rather than letting the URL auto-provision them. Auto-provisioning
   turns a typo'd slug into a second, unwatched check that reads green forever because
   something is pinging it. `ansible/tests/services/test_healthchecks_pings.py` fails the build if a
   call site ever adds the auto-provisioning parameter.

2. **Add the project ping key** (Settings → Ping key, *not* an API key):

   ```bash
   sops ansible/vars/secrets.yml          # add: healthchecks_ping_key: <key>
   uv run python scripts/secrets_mgmt/secret_rotation.py sync
   ```

   One key covers every check — the slug in the URL selects which one. Adding a fifth check
   later needs no new secret.

3. **Deploy** the five call sites:

   The three k3s-setup host heartbeats live in the k3s setup role, which `k3s-bringup.yml` runs —
   `scripts/deploy.sh` is hardcoded to `deploy.yml` and will not reach them, so take the
   git-tree lock by hand for that one:

   ```bash
   flock -w 180 /var/lock/server-git-tree.lock \
     uv run ansible-playbook ansible/k3s-bringup.yml \
     --tags "backup-health,disk-health,manifest-prune,etcd-snapshot"

   ./scripts/deploy.sh --tags "pi-peer-backup"
   ./scripts/deploy.sh --tags "registry"
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
sudo /usr/local/bin/etcd-snapshot-offbox.sh   # takes and uploads a real snapshot
sudo k3s kubectl -n homelab create job ppb-manual --from=cronjob/pi-peer-backup
sudo /usr/local/bin/registry-gc.sh   # scales the registry down for the duration — see below
```

Forcing `etcd-snapshot-offbox` is safe — it adds one more snapshot to R2 and the retention
setting ages the oldest out — but it is not free either: it is a real etcd read plus an upload,
so do not loop it.

Forcing `registry-gc` is not free: it takes the registry offline while the GC job runs, so
nothing in the cluster can pull a locally built image until it scales back up. Run it when no
build is in flight (it will skip and report `up` if one is), or just wait for Sunday.

A failed ping is logged locally (`logger -t <script>`, or the job's stderr) and nowhere else,
which is deliberate — a ping that cannot leave the house is precisely the case Healthchecks.io
catches by silence.

## What is deliberately not wired

- **`gitops-deploy.service`** — the role documents "no Kuma pushing from the deployer" as a
  decision: liveness is a `/var/lib/gitops-deploy/last_run` timestamp that monitor-bridge
  reads. Adding a push here would reverse that decision, which is a separate call to make.
- **Longhorn's `daily-backup` / `weekly-backup-d*` CronJobs** — generated by Longhorn from its
  RecurringJob CRDs, not rendered by this repo, so there is no command to append to.
  `longhorn-backup-health` covers the same ground and covers it better: it asserts backup
  *freshness and coverage* rather than that a job ran.
- **The other ~15 Kuma push sites.** Free-tier headroom is 20 checks and six are used; the
  limit is not the reason. An off-site check earns its slot only where the silence is both
  invisible and consequential, and adding the rest would mostly duplicate what Kuma already
  reports correctly whenever the cluster is up enough to have a problem worth reporting.
