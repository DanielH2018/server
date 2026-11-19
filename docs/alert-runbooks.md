# Alert runbooks

Each critical alert links its runbook as its `runbook_url`, and the notification prints it as
its `Runbook:` line. This page holds the runbook of each critical alert that no other page
covers, one section per alert, named as the alert is. A few warnings whose descriptions
outgrew a notification have a section too. An alert's description says what to do
first. Its section here says what the alert means, then the cases the description leaves out.

`tests/services/test_alert_runbook_urls.py` fails a critical rule with no `runbook_url`, a
link to a section this page does not have, and a description over the cap the notification
can carry.

## The alert plane

### `AlertmanagerNotificationsFailing`

Alertmanager failed at least one send through one integration (Discord or email) in the last
30 minutes, for any reason but `rateLimited`.

- Read `alertmanager_notifications_failed_total` by `reason` in Prometheus.
- Read the `alertmanager` pod's log in the `observability` namespace for the send error.

### `AlertmanagerNotificationsRateLimited`

An integration answered Alertmanager's sends as rate limited in every 7-minute window for 10
minutes. Alertmanager sends a group again at its next flush after a failed one, so a short
burst during a large rollout delivers late and never fires this alert. One that persists means
the resends are failing too.

- Read the `alertmanager` pod's log in the `observability` namespace for the receiver's answer.
- Look in Karma for an alert group that changes on every evaluation and so sends on every
  `group_interval`.

### `WarningUnresolved`

A warning has fired for 48 hours, so it pages as a critical. The `escalated_from` label names
the warning and `escalated_service` its service.

- Fix the warning itself: the Karma link in the notification lists it with its own
  description and runbook. This alert resolves when the warning does.
- A warning that holds by design takes the rule label `escalate: "false"`, which stops the
  escalation. Its rule then repeats daily instead of every 6 hours.
- To silence one escalation, match `escalated_from` in the silence.

### `LokiRulerNotificationsFailing`

The `loki-homelab` ruler failed its sends to Alertmanager in the last 15 minutes, so no firing
log rule reaches anyone.

- Read the `loki-homelab` pod's log for `Error sending alerts`.
- Check the `alertmanager-callers` NetworkPolicy in the `observability` namespace. A policy
  that no longer admits the ruler's pod refuses every send.

## monitor-bridge

`docs/monitor-bridge-internals.md` describes the exporter, and
`docs/monitor-bridge-checks.md` describes each check.

### `MonitorBridgeDown`

`up{job="monitor-bridge"}` has been 0 or missing for 10 minutes.

- Read the `monitor-bridge` pod's events and log in the `homelab` namespace.
- A missing series with a running pod means the `monitor-bridge` scrape job is gone from
  `prometheus.yml`.

### `BridgeCheckStale`

The scrape works, but the run loop is hung or skipping rows. Either a row's
`homelab_check_last_run_timestamp_seconds` has aged past its `homelab_check_deadline_seconds`,
or fewer rows export `homelab_check_success` than `homelab_check_rows` counts.

- Read the `monitor-bridge` pod's log in the `homelab` namespace. The summary names the stale
  checks.
- A pod restart clears a hung loop. A check that is skipped at every run needs its own fix.

### `WANUnreachable`

Both WAN probe providers failed from monitor-bridge, so the house has no working link or no
DNS.

- Check the router and the modem first.
- Read the probe's errors: `kubectl -n homelab logs deploy/monitor-bridge | grep 'DOWN
  wan_reachable '`.
- An inhibit rule holds the rows that need the internet while this fires: Discord delivery, R2
  headroom, Cloudflare IP drift, Healthchecks.io console drift and the domain registration
  read. They page again after the WAN returns if they are still down.

### `DiscordDeliveryFailing`

One of the five Discord webhooks (Kuma, CrowdSec, `gitops-deploy`, *arr, healthchecks) no longer
resolves, or the Gmail SMTP backstop login failed.

- Read which: `kubectl -n homelab logs deploy/monitor-bridge | grep 'DOWN discord '`.
- Rotate the named webhook in SOPS with the `add-secret` skill's flow, then let `promote`
  redeploy its consumer.

### `DomainRegistrationExpiring`

The registry's RDAP record dates the domain's expiry within the alert's threshold.

- Renew the domain at the registrar.
- A lapse takes down ACME renewal, every public route, the WireGuard endpoint name and the
  alert plane's own notifications.

### `SmartHealthFailing`

Scrutiny reports a drive whose collector data is older than 26 hours, whose SMART
self-assessment or an attribute threshold failed, or whose NVMe wear passed 80%. Scrutiny's
own notifier is unconfigured, so this is the only drive-failure page.

- Read which drive and which leg: `kubectl -n homelab logs deploy/monitor-bridge | grep 'DOWN
  scrutiny '`.
- Stale collector data is the collector, not the drive: read the `scrutiny` collector's log.
- A failed self-assessment or high wear means the drive needs replacing. Check the backups of
  every volume on it first (`docs/longhorn-disaster-recovery.md`).

## The edge and its probes

### `CertExpiringSoon`

Traefik serves a certificate that expires within 14 days, so its ACME renewal has failed.

- Read Traefik's log for the ACME error.
- Run `uv run scripts/diagnostics/probe.py cert <host>` for the chain Traefik serves.

### `IngressProbeFailedCritical`

The `blackbox_exporter` probe of a home-critical ingress route has failed for 2 minutes.

- Read `probe_http_status_code` for the instance, then the workload's log.
- A probe behind SSO expects Authelia's 302 and never reaches the backend. A failure there is
  in Traefik or Authelia, not the workload, so read their logs first.

### `ExtraProbeFailedCritical`

One of the hand-written `blackbox_exporter` probes of a critical target has failed for 2 minutes.

- Read `probe_http_status_code` or `probe_dns_lookup_time_seconds` for the instance, then the
  target's own log.

### `BlackboxExporterDown`

Every `blackbox-ingress` target has failed its scrape for 5 minutes, so the exporter is down or
unreachable and no ingress probe runs. The per-service ingress alerts go quiet while it lasts.

- Read the `blackbox-exporter` pod's events and log in the `homelab` namespace.

### `CrowdSecAppSecVerifyFailed`

The `crowdsec-jobs` sidecar found no enabled appsec-config or appsec-rule, so the inline WAF is
not loaded. The bouncer fails open, so the edge is ban-list-only.

- Run `cscli appsec-configs list` in the `crowdsec` pod.
- Read the `crowdsec-appsec-verify` lines in the `crowdsec-jobs` container's log.

### `CrowdSecJobStale`

A `crowdsec-jobs` job has not finished a run within three of its periods, so the sidecar's
scheduler is hung or skips the job at every slot.

- Read the `crowdsec-jobs` container's log in the `crowdsec` pod. A pod restart clears a hung
  scheduler.

### `CrowdSecJobsExporterAbsent`

No `up{job="crowdsec-jobs"}` series has existed for 10 minutes, so the scrape job is gone from
`prometheus.yml`. The three CrowdSec sidecar verdicts are unmonitored, which is not the same as
healthy.

- Read the rendered `prometheus.yml` for the `crowdsec-jobs` job.

## Host checks

Each check writes a textfile under `/var/lib/node-exporter-textfile/` on its host, and its
`*NotReporting` rule pages when the file stops changing.

### `DiskHealthFailed`

`disk-health.sh` on daniel-box read the root filesystem above its threshold, or `df` failed.

- `homelab_disk_root_used_ratio{check="disk-health"}` holds the share it read, and the
  `disk-health` syslog tag holds the run's message.
- Longhorn stops scheduling replicas on the node at 75%, so free space before that floor.
  `sudo k3s crictl rmi --prune` frees containerd's unused images.

### `DiskHealthNotReporting`

No `disk-health` verdict newer than 25 minutes reached Prometheus, so nothing watches the root
disk.

- Read the `disk-health` syslog tag and `systemctl status homelab-disk-health.timer` on
  daniel-box, then `disk-health.prom` in the textfile directory.

### `HostDiskFull`

A host filesystem is over 90% full.

- Read `df -h` on the host, then `du -xh --max-depth=1` on the mount.
- Longhorn stops scheduling replicas on a node at 75%, so a node root this full already
  refuses new replicas.

### `LonghornBackupHealthNotReporting`

No `longhorn-backup-health` verdict newer than 25 minutes reached Prometheus, so nothing
watches the Longhorn backup plane.

- Read the `longhorn-backup-health` syslog tag and `systemctl status
  homelab-longhorn-backup-health.timer` on daniel-box.
- Then read `longhorn-backup-health.prom` in the textfile directory.

### `EtcdSnapshotOffboxNotReporting`

No `etcd-snapshot-offbox` verdict newer than 25 hours reached Prometheus, so nothing compares
the cluster token with its off-box stamp.

- Read the `etcd-snapshot-offbox` syslog tag and `systemctl status
  homelab-etcd-token-drift.timer` on daniel-box.
- Then read `etcd-snapshot-offbox.prom` in the textfile directory.
  `docs/k3s-etcd-restore.md` says why the token stamp matters.

### `K3sConfigRestartFailed`

The last windowed restart of `k3s.service` on daniel-box did not end with the unit active,
under a new MainPID, with the node Ready past its restart. A run killed after it restarted k3s
ends here too.

- Read `journalctl -u homelab-k3s-restart -p notice` on daniel-box.
- Recover the node by hand if it is down.
- The unit makes one attempt per config hash. To allow another attempt, run `sudo rm
  /var/lib/homelab-k3s-restart/failed.sha256`. `docs/k3s-upgrade.md` describes the unit.

### `SystemdUnitsFailed`

A warning. The host's `systemd-units` check found a system unit in the failed state, and the
alert fires once per failed unit.

- Read `systemctl status <unit>` and `journalctl -u <unit> -b` on the host, and fix the cause.
- `systemctl reset-failed <unit>`, or a clean run of the unit, clears the state. The next
  check, within 5 minutes, resolves the alert.
- A summary that names no unit means the host wrote no `homelab_systemd_unit_failed` series.
  Either `systemctl` could not list the units (`journalctl -t systemd-units`), or the host's
  package predates the per-unit series. Run `systemctl --failed` on the host.

### `TextfileCollectorError`

A shadow warning. node_exporter's textfile collector on the host has failed for 10 minutes, so
a file in `/var/lib/node-exporter-textfile` is malformed or unreadable, and every series in it
is gone.

- The summary lists the files seen in the last 7 days and missing now. A rejected file drops
  out of `node_textfile_mtime_seconds`, but so does a file a retired check stopped writing.
- A file that was malformed from its first write never reported an mtime, so the list can omit
  it. The node-exporter pod's log names the file it rejects.

### `PiSdHealthFailed`

`pi-sd-health` on daniel-pi ended with `status=down`. One of three legs failed: the root
filesystem's ext4 `errors_count` is nonzero or unreadable, the rotated-log integrity verdict is
bad or stale, or `/var/log` is not log2ram's tmpfs.

- The `pi-sd-health` line in `/var/log/pi-health/health.log` names the failing leg.
- Only a nonzero `errors_count` calls for an `fsck` and a card replacement.
- A `/var/log` that is not a tmpfs means log2ram refused to start. Fix its cause, then reboot.

### `PiSdHealthNotReporting`

No `pi-sd-health` verdict newer than 15 minutes reached Prometheus, so nothing watches the SD
card.

- Read `systemctl status homelab-pi-sd-health.timer` on daniel-pi, then `pi-sd-health.prom` in
  the textfile directory.

## Power

The UPS is USB-attached to daniel-server, and the `nut` role's pod there runs upsd, the
driver and the primary `upsmon`. Each node runs a secondary `upsmon` that powers it off when
the primary raises FSD. `k8s/roles/nut/CLAUDE.md` has the shutdown chain.

### `UPSOnBattery`

NUT reports `ups.status` OB for 10 minutes: mains power is gone and the UPS carries the load.

- Check the mains supply. The summary carries the charge and the runtime left.
- At `battery.runtime.low` the UPS raises FSD, and both nodes shut down.

### `UPSBatteryLow`

The battery charge is under 50% (`UPS_CHARGE_MIN_PCT`) with mains power on, for 10 minutes.

- Read the charge history in Prometheus. A charge that does not recover over a few hours
  means the battery is failing.

### `UPSRuntimeLow`

The UPS estimates under 5 minutes of runtime (`UPS_RUNTIME_MIN_S`) on a full charge, for 10
minutes. The battery has aged.

- Plan a battery replacement.

### `UPSReplaceBattery`

NUT reports `ups.status` RB for 10 minutes: the UPS's own self-test says the battery needs
replacing. This trips before charge or runtime read low.

- Replace the battery.

### `UPSSensorAbsent`

The NUT exporter answers its scrape but publishes no series for one arm for 10 minutes, so
the UPS is unmonitored on that arm. A renamed or removed series is the usual cause.

- Read nut-exporter's `/ups_metrics` for the series it does publish.

### `UpsSecondaryFailed`

The secondary watchdog on the host ended with `status=down`: `upsmon.conf` is unreadable or has
no `MONITOR` line, `nut-monitor` is not active, or `upsc` cannot reach upsd. Until it is fixed,
the host would not power off on FSD.

- Run `systemctl is-active nut-monitor` and `upsc <endpoint> ups.status` on the host.
- The `ups-secondary-health` syslog tag holds the run's message.

### `UpsSecondaryNotReporting`

No `ups-secondary-health` verdict newer than 25 minutes reached Prometheus, so nothing confirms
the secondary `upsmon` can still hear upsd.

- Run `systemctl status homelab-ups-watchdog.timer` and `journalctl -u
  homelab-ups-watchdog.service` on the host.
- Then read `ups-secondary-health.prom` in the textfile directory.

## Storage

`docs/longhorn-disaster-recovery.md` holds the Longhorn restore procedures, and
`docs/volume-snapshot-drills.md` the snapshot revert.

### `HostFilesystemRemountedReadOnly`

The kernel remounted a filesystem read-only in the last hour. An `sdX` device is a Longhorn
iSCSI volume, and its workload cannot write until the volume detaches and reattaches.

- Read `{job="syslog", program="kernel"}` in `loki-homelab` for the device and the errors before
  it.
- Scale the workload to 0, wait for the volume to read `detached`, and scale back. A rollout
  restart does not detach the volume, so it does not clear the mount.

### `KubeletCSIMountReadOnly`

A CSI mount went read-only after an iSCSI stall, behind a Longhorn volume that still reads
healthy.

- Scale the workload to 0, wait for the volume to read `detached`, and scale back. A rollout
  restart does not clear it.

### `LonghornVolumesMissing`

Either no `up{job="longhorn"}` series exists, so the scrape job is gone from `prometheus.yml`,
or the scrape works and `longhorn_volume_robustness` is missing. Replica redundancy is
unmonitored while this fires, which is not the same as healthy.

- Read `uv run scripts/diagnostics/probe.py targets` for the `longhorn` job, then the
  longhorn-manager pods' logs.

### `LonghornDiskOverSchedulingFloor`

A Longhorn disk is past 75% used. Above that floor Longhorn places no replica on the node, so
with two replicas a rebuild or a new volume stays degraded.

- The disk shares the node's root filesystem, so containerd's unused images count too. Run
  `sudo k3s crictl rmi --prune` on the node.
- Then read `df -h /` and Longhorn's per-volume `actualSize` for what else holds the space.

### `LonghornSnapshotNearCap`

A claim's snapshot space is at 90% of its cap. Longhorn refuses new snapshots at the cap rather
than pruning, so the next deploy of the service fails until snapshots are deleted by hand.

- Delete snapshots of the volume by hand, keeping the ones a revert needs
  (`docs/volume-snapshot-drills.md`).

### `LonghornSnapshotCapUnmonitored`

A claim declares a snapshot cap and has no `longhorn_snapshot_actual_size_bytes` series, so
its snapshot headroom is unmonitored, which is not the same as empty.

- Check that the volume exists and that the `longhorn` scrape job still exports the series.

### `PVCFull`

A claim is over 85% full. A claim cannot borrow space.

- Expand the volume in its role's PVC template, then let `promote` apply it.

### `PVCBelowFreeFloor`

The `valheim-server` claim has less free space than the 3 GiB its updater stages before it
swaps the install in.

- Expand the volume in its role.

### `PVCFreeFloorUnmonitored`

`PVCBelowFreeFloor` finds no `kubelet_volume_stats_available_bytes` series for
`valheim-server`, so the claim's free-space floor is unmonitored.

- The claim was renamed or excluded, or the kubelet stopped reporting it. Update the rule if
  the claim was renamed.

### `PVCCoverageShortfall`

Fewer claims report `kubelet_volume_stats` than the rule's floor. PVC fullness is unknown, not
fine.

- Read the `kubernetes-kubelet` targets, one per node, with `uv run
  scripts/diagnostics/probe.py targets`.

### `EtcdDBNearQuota`

The etcd database is near its 2 GiB quota. At the quota etcd rejects every write and the
control plane stops.

- Compact etcd and run `etcdctl defrag` on daniel-box, or raise `--etcd-arg=quota-backend-bytes`.
- `docs/k3s-etcd-restore.md` holds the restore procedure if the control plane is already down.

## Pods

### `AutofixBridgeDown`

`up{job="autofix-bridge"}` has been 0 or missing for 10 minutes.

- Read the `autofix-bridge` pod's events and log in the `homelab` namespace.
- A missing series with a running pod means the scrape job is gone from `prometheus.yml`.

### `AutofixBridgeStale`

The scrape works, but the loop is hung or never completes a cycle. Either the cycle's
`homelab_check_last_run_timestamp_seconds` has aged past its deadline, or the exporter serves
no `homelab_check_success`.

- Read the `autofix-bridge` pod's log in the `homelab` namespace. A pod restart clears a hung
  loop. `docs/autofix-bridge-actuators.md` describes the planes it runs.
