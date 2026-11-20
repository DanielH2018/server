# Alert runbooks

Each critical alert links its runbook as its `runbook_url`, and the notification prints it as
its `Runbook:` line. This page holds the runbook of each critical alert that no other page
covers, one section per alert, named as the alert is. A warning has a section too when its
edge cases did not fit its description. An alert's description says what to do
first. Its section here says what the alert means, then the cases the description leaves out.

`tests/services/test_alert_runbook_urls.py` fails a critical rule with no `runbook_url` and a
link to a section this page does not have. It also fails any alert, warning or critical, whose
description is over the cap the notification can carry.

Every summary reads `<service or host>: <what is wrong>`, so each line of a notification opens
with the thing that broke. The subject is the rule's own `service` label, an inventory host,
or labels of the firing series such as `{{ $labels.namespace }}/{{ $labels.pod }}`. The same
test fails a summary in any other shape.

## The alert plane

### `AlertmanagerNotificationsFailing`

Alertmanager failed at least one send through one integration (Discord or email) in the last
30 minutes, for any reason but `rateLimited`.

- Read `alertmanager_notifications_failed_total` by `reason` in Prometheus.
- Read the `alertmanager` pod's log in the `observability` namespace for the send error.

### `AlertmanagerNotificationsRateLimited`

An integration answered Alertmanager's sends as rate limited in every 7-minute window for 15
minutes. Alertmanager sends a group again at its next flush after a failed one, so a burst
during a large rollout that fails one group for up to two flushes delivers late and never
fires this alert. One that persists means
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

### `CAdvisorCoverageShortfall`

The per-pod cAdvisor vector behind the restart, OOM or CPU rule (the `arm` label) holds fewer
than 20 pods, where the estate holds about 70 to 100. The restart, OOM and CPU rules read green
while it lasts, so a crash loop, an OOM kill or CPU throttling goes unreported.

- An empty vector is the healthy answer for an offender filter, so a thin vector says that the
  `kubernetes-cadvisor` job or a selector is broken, not that the cluster is quiet.
- Read the `kubernetes-cadvisor` targets on Prometheus. A down target is the job; all targets
  up with a thin vector points at the selector in the rule.


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

One of the five Discord webhooks (Alertmanager, CrowdSec, `gitops-deploy`, *arr, healthchecks) no
longer resolves, or the Gmail SMTP backstop login failed.

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

### `TraefikIngressAddressNotPinned`

The traefik Service does not hold the reserved ingress VIP, or Prometheus has no
kube-state-metrics scrape job. Cloudflare's origin, the router's port-forward and the LAN DNS
all hardcode that VIP, so a Service on an auto-assigned address serves nothing those clients
dial, behind a Ready pod.

- MetalLB does not re-evaluate an existing allocation, so editing the pinned address does not
  move the Service.
- Delete the Service. The next apply of traefik re-creates it with a fresh allocation.
- The same rule exists for the mosquitto-lan, jellyfin-lan and pihole-dns Services, each with
  its own pinned address.


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

### `K3sConfigRestartMetricsAbsent`

No `homelab_k3s_restart_failed{host="daniel-box"}` series reaches Prometheus. The restart unit
writes it on every run, so either its textfile never arrived or the timer stopped.

- `K3sConfigRestartFailed` and `K3sConfigRestartPending` read empty while this fires, so a
  failed or refused restart pages nothing.
- Read `systemctl list-timers homelab-k3s-restart.timer` and
  `journalctl -u homelab-k3s-restart -p notice` on daniel-box.
- Then check the textfile directory for `homelab-k3s-restart.prom`.

### `CheckoutSyncFailed`

`homelab-checkout-sync` on daniel-box refused its last run, so the primary checkout is not
following master.

- `homelab_checkout_sync_refused` names the reason: a dirty tree (an untracked file counts), a
  branch other than master, a HEAD that origin/master does not descend from, an empty CI
  context set or walk bound, or a missing or refused GitHub token.
- Read `journalctl -t homelab-checkout-sync` on daniel-box for the run's message.
- One untracked file parks the sync until it moves.

### `CheckoutSyncNotReporting`

No `checkout-sync` verdict newer than 2 hours reached Prometheus, so nothing confirms that the
primary checkout follows master.

- A fetch that keeps failing writes no verdict, and neither does a git-tree lock held for hours.
- Read `systemctl status homelab-checkout-sync` and `journalctl -t homelab-checkout-sync` on
  daniel-box, then `checkout_sync.prom` in `/var/lib/node-exporter-textfile/`.

### `SystemdUnitsFailed`

A warning. The host's `systemd-units` check found a system unit in the failed state, and the
alert fires once per failed unit.

- Read `systemctl status <unit>` and `journalctl -u <unit> -b` on the host, and fix the cause.
- `systemctl reset-failed <unit>`, or a clean run of the unit, clears the state. The next
  check, within 5 minutes, resolves the alert.
- A summary that names no unit means the host wrote no `homelab_systemd_unit_failed` series.
  Either `systemctl` could not list the units (`journalctl -t systemd-units`), or the host's
  package predates the per-unit series. Run `systemctl --failed` on the host.
- A unit that names `OnFailure=homelab-alert@%n.service`, or its own
  `homelab-<name>-alert.service` such as `homelab-claude-triage-poll-alert.service`, pages
  Discord through that hook first. Once the hook delivers, the check labels the unit's
  series `paged_by="homelab-alert"`, and Alertmanager sends this alert to its `blackhole`
  receiver, which notifies nobody. A page that the hook's own rate limit skipped counts as
  delivered, because the skip means the hook paged earlier in its window. The alert stays visible in Karma. The check's first run after the
  failure decides the label, and the label holds until the unit recovers. If the hook has
  not delivered by that run, the label stays off and this alert pages. After 48 hours the
  alert still escalates to `WarningUnresolved`, which pages.

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

### `AideCheckFailed`

The weekly AIDE scan ended with `status=down`. The rule carries `escalate: "false"`, so
Alertmanager repeats it daily and `WarningUnresolved` never pages it.

- The scan fails when a file changed that no package explains, when AIDE exited with an error
  and checked nothing, or when the previous week's run still held the lock.
- Review every unexplained path before you accept it. `sudo /usr/lib/homelab/aide-check
  --accept` records the new baseline.
- `sudo systemctl start homelab-aide-check.service` re-runs the check. A pass clears this
  alert. A DOWN run exits 0, so the unit fails only when the run could not write its verdict.

### `RkhunterScanFailed`

The weekly rkhunter scan ended with `status=down`. The rule carries `escalate: "false"`, so
Alertmanager repeats it daily.

- The scan fails when rkhunter raised a warning, a suspect file or a possible rootkit, or when
  it wrote no summary.
- An account change clears on the next run.
- `sudo systemctl start homelab-rkhunter-check.service` re-runs the scan. A DOWN run exits 0,
  so the unit fails only when the run could not write its verdict.

### `KubeletShutdownLockMissing`

The kubelet on the node holds no shutdown delay lock, so the next shutdown stops pods without
draining them.

- `systemd-analyze cat-config systemd/logind.conf` prints the setting that the k3s log line in
  `loki-homelab` names.
- Restart k3s-agent. The lock check runs every 5 minutes, and its next run resolves the alert.

### `ClaudeMemorySyncNotReporting`

The copy of the memory store from daniel-box to daniel-server has not reported within its
deadline.

- The timer stopped firing, or every run since failed and its `OnFailure` unit posted to
  Discord.
- A run that the `ExecCondition` skips, because daniel-box's `MEMORY.md` is empty, writes
  nothing either. An empty store is not a healthy state, so restore the store.
- If `claude_code_memory_sync_enabled` is false on purpose, delete this rule.

### `ClaudePipelineTimelineNotReporting`

The daily PR timeline snapshot has not reported within its deadline. The journal inputs it
saves rotate after about ten days, so a gap that long loses data.

- The timer stopped firing, or every run since failed and its `OnFailure` unit posted to
  Discord.
- Read the `claude-pipeline-timeline` file in `/var/lib/node-exporter-textfile/`.
- If `claude_code_pipeline_timeline_enabled` is false on purpose, delete this rule.

### `ComposeUpStale`

The boot's compose-run on daniel-pi wrote no result: `homelab_compose_up_timestamp_seconds`
predates the boot or is missing.

- The `homelab-compose` unit is masked or disabled, systemd killed the run at its
  `TimeoutStartSec`, or compose-run failed to write the `homelab_compose` file in
  `/var/lib/node-exporter-textfile/`.
- Read `systemctl status homelab-compose` and `journalctl -b -u homelab-compose` on daniel-pi.

### `ComposeRestartUnreported`

`homelab-reconcile` started `homelab-compose.service` on daniel-pi, and compose-run wrote no
result for that start.

- systemd killed the run at its `TimeoutStartSec`.
- compose-run failed to write the `homelab_compose` file in `/var/lib/node-exporter-textfile/`
  after `up` succeeded. The run exits 0, so `HostReconcileFailed` stays quiet.
- Read `journalctl -u homelab-compose -u homelab-reconcile` on daniel-pi.

### `PiRecoveryNotReporting`

No `pi-recovery-health` verdict newer than 15 minutes reached Prometheus from daniel-pi, so
nothing confirms that the Pi's containers are watched.

- Read `systemctl status homelab-pi-recovery.timer` on daniel-pi, then the
  `pi-recovery-health` file in `/var/lib/node-exporter-textfile/`.
- A host that sets `optimize_pi_recovery_restart_enabled` to false drops the timer, so this
  alert fires on purpose there.

### `PiWatchdogFailed`

`watchdog.service` on daniel-pi has not been active for 30 minutes, so the `max-load-1 = 24`
load test is off. The hardware watchdog stays armed, because `wd_keepalive` holds the device.

- A stop, or a daemon that died, is the cause. A restart leaves the daemon running and raises
  nothing.
- Run `sudo systemctl start watchdog` on daniel-pi.
- The `pi-watchdog-health` lines in `/var/log/pi-health/health.log` date the stop.

### `PiAuditFailed`

`pi-audit-health` on daniel-pi has read `status=down` for 30 minutes. Either `auditd.service`
is not active, or it is active and `/var/log/audit/audit.log` has had no write for an hour.

- `auditd` suspends logging when `/var/log` runs out of space, and only a restart resumes it.
  Read `df -m /var/log` first, then run `sudo systemctl restart auditd`.
- The `pi-audit-health` lines in `/var/log/pi-health/health.log` date the stop.

### `ClaudeGuardrailAuditFailed`

The `claude-guardrail-audit` check ended with `status=down`, so the auditd watches on Claude
Code's guardrail paths are not all in place. Until it clears, a write to a hook, a settings
file
or a memory store leaves no audit record.

- The check fails when auditd is not running, when kernel auditing is off, or when a guardrail
  path that exists has no watch.
- Run `journalctl -t claude-guardrail-audit` on the host, then `systemctl status auditd`.
- The check reloads the watches every 5 minutes, so a fixed host resolves the alert at the next
  run.

### `ClaudeGuardrailAuditNotReporting`

No `claude-guardrail-audit` verdict newer than 15 minutes reached Prometheus, so nothing
reloads
a watch that an auditd restart dropped.

- Read `systemctl status homelab-claude-guardrail-audit.timer` and `journalctl -t
  claude-guardrail-audit` on the host.
- Read the `claude-guardrail-audit` file in `/var/lib/node-exporter-textfile/`.

### `ClaudeDotfilesMirrorFailed`

The dotfiles root mirror ended with `status=down`, so T2's config stays at the last verified
tag.

- The mirror could not fetch the dotfiles repo.
- The newest tag is unsigned, is signed by a key other than the operator's, or does not descend
  from the last verified tag.
- Run `journalctl -t claude-dotfiles-mirror` on the host.

### `ClaudeDotfilesMirrorNotReporting`

No `claude-dotfiles-mirror` verdict newer than 35 minutes reached Prometheus, so no new
dotfiles
tag is being verified.

- Read `systemctl status homelab-claude-dotfiles-mirror.timer` and `journalctl -t
  claude-dotfiles-mirror` on the host.
- Read the `claude-dotfiles-mirror` file in `/var/lib/node-exporter-textfile/`.

### `KubeletShutdownLockNotReporting`

No `kubelet-shutdown-lock` verdict newer than 900 seconds reached Prometheus, so nothing reads
kubelet's shutdown delay lock on the node.

- Read `systemctl status homelab-kubelet-shutdown-lock.timer` and `journalctl -t
  kubelet-shutdown-lock` on the node.
- Read the `kubelet-shutdown-lock` file in `/var/lib/node-exporter-textfile/`.

### `ClaudeWeeklySummaryNotReporting`

No `claude-weekly-summary` verdict newer than 169 hours reached Prometheus, so last week's
unattended Claude runs were not summarised. The timer runs every Monday and 10 minutes after
each activation.

- Read `systemctl status homelab-claude-weekly-summary.timer` and `journalctl -t
  claude-weekly-summary` on daniel-box.
- Read the `claude-weekly-summary` file in `/var/lib/node-exporter-textfile/`.
- If `claude_code_weekly_summary_enabled` is false on purpose, delete this rule.

### `ClaudeCloneSyncNotReporting`

No `claude-clone-sync` verdict newer than 2100 seconds reached Prometheus, so the agent's clone
fast-forward has not completed a clean run.

- The timer stopped firing, or every run since failed and its `OnFailure` unit posted to
  Discord.
- Read `systemctl status claude-clone-sync.timer` and `journalctl -u claude-clone-sync` on the
  host.
- If `claude_code_agent_user_enabled` is false on purpose, delete this rule, or narrow its
  `hosts:` selector when another host keeps the timer.

### `ClaudeDotfilesSyncNotReporting`

No `claude-dotfiles-sync` verdict newer than 2100 seconds reached Prometheus, so the agent's
dotfiles apply has not completed a clean run.

- The timer stopped firing, or every run since failed and its `OnFailure` unit posted to
  Discord.
- Read `systemctl status claude-dotfiles-sync.timer` and `journalctl -u claude-dotfiles-sync`
  on the host.
- If `claude_code_agent_dotfiles` is false on purpose, delete this rule, or narrow its `hosts:`
  selector when another host keeps the timer.

### `ClaudeTriagePollNotReporting`

No `claude-triage-poll` verdict newer than 660 seconds reached Prometheus, so the monitor
triage
poll has not completed a clean run.

- The timer stopped firing, or every run since failed and its `OnFailure` unit posted to
  Discord.
- Read `systemctl status homelab-claude-triage-poll.timer` and `journalctl -u
  homelab-claude-triage-poll` on daniel-box.
- If `claude_code_triage_enabled` is false on purpose, delete this rule.

### `ClaudeFanoutLanderNotReporting`

No `claude-fanout-server-lander` verdict newer than 3300 seconds reached Prometheus, so the
fan-out server lander has not completed a clean run.

- The timer stopped firing, or every run since failed and its `OnFailure` unit posted to
  Discord.
- Read `systemctl status homelab-claude-fanout-server-lander.timer` and `journalctl -u
  homelab-claude-fanout-server-lander` on daniel-box.
- If `claude_code_fanout_server_lander_enabled` is false on purpose, delete this rule.

### `AptAutoremoveNotReporting`

No `apt-autoremove` verdict newer than 612000 seconds reached Prometheus, so nothing confirms
that the weekly apt autoremove still runs.

- The `Weekly apt autoremove` cron lives in root's crontab, not in a user crontab.
- Read `journalctl -t apt-autoremove` on the host.
- Read the `apt-autoremove` file in `/var/lib/node-exporter-textfile/`.

### `EtcdRestoreDrillEgressFenceNotReporting`

No `etcd-restore-drill-egress-fence` verdict newer than 35 days reached Prometheus from
daniel-server, so the monthly drill's egress fence went unmeasured.

- A run that dies before the fence leg executes writes nothing here.
- Read the newest run directory under `/var/log/etcd-restore-drill`.
- Then read the `etcd-restore-drill-egress-fence` file in `/var/lib/node-exporter-textfile/` on
  daniel-server.

### `RenovateNotifyNotReporting`

No `renovate-notify` verdict newer than 36 hours reached Prometheus from daniel-box, so the
daily notifier has not completed a run.

- Either the daily 13:00 timer did not fire or the run failed.
- A failed run has already posted to Discord through `OnFailure`.
- The alert says nothing about Renovate itself.
- Read `systemctl status homelab-renovate-notify.timer` and `journalctl -u
  homelab-renovate-notify` on daniel-box.


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

### `PiPeerBackupFailed`

The nightly `pi-peer-backup` script reported down. The WireGuard peer keys on daniel-pi are
un-rebuildable, so the last good copy is the only one, and a failing backup leaves it ageing.

- The causes are an rsync from daniel-pi that failed, a daniel-pi host key that no longer
  matches the committed pin, or fewer than two files coming back.
- Read the annotation `homelab/check-message` on the `pi-peer-backup-verdict` ConfigMap.
- Then run `kubectl -n homelab logs` on the newest `pi-peer-backup` Job.

## Registry mirrors

### `GhcrMirrorScaledToZero`

The ghcr-mirror Deployment has wanted zero replicas for 2 hours, or Prometheus has no
kube-state-metrics scrape job. While the mirror is down, containerd pulls ghcr.io images
straight from GHCR, so a WAN outage leaves no source to pull them from.

- `registry-gc.sh` scales the mirror to zero for its garbage collection and holds it there for
  about 25 minutes at most. A longer hold means a run died before its restore step.
- Run `journalctl -t registry-gc` on daniel-box, and confirm no `registry-gc.sh` is running.
- Restore the mirror with `kubectl -n homelab scale deploy/ghcr-mirror --replicas=1`.
- The rule also fires when Prometheus has no `kube-state-metrics` scrape job, because nothing
  can then read the replica count. `ScrapeTargetDown` pages for the missing scrape.

### `DockerhubMirrorScaledToZero`

The dockerhub-mirror Deployment has wanted zero replicas for 2 hours, or Prometheus has no
kube-state-metrics scrape job. While the mirror is down, containerd pulls docker.io images
straight from Docker Hub, so a WAN outage leaves no source to pull them from.

- `registry-gc.sh` scales this mirror to zero after the ghcr-mirror's phase, with the same hold
  of about 25 minutes at most. A longer hold means a run died before its restore step.
- Run `journalctl -t registry-gc` on daniel-box, and confirm no `registry-gc.sh` is running.
- Restore the mirror with `kubectl -n homelab scale deploy/dockerhub-mirror --replicas=1`.
- The rule also fires when Prometheus has no `kube-state-metrics` scrape job.

### `GhcrMirrorPullFailed`

The `ghcr-mirror-pull` check on a node has failed for 45 minutes. The node-exporter sidecar
GETs nut's manifest through the node's ghcr.io mirror endpoint every 600 seconds. containerd
falls back to GHCR past a broken mirror, so nothing breaks until a WAN outage, when nut cannot
cold-start.

- The GET failed in one of three ways: the endpoint refused it, it timed out, or it answered an
  error. The check also fails when the `nut-ghcr-mirror-probe` ConfigMap names no tag.
- Read the `ghcr-mirror-pull` container log of the node-exporter pod on the alert's `origin`
  node.
- Then read the ghcr-mirror pod, its NetworkPolicy and the node's `registries.yaml`.
- The 45-minute hold rides out the weekly garbage collection. A mirror that GC left at zero
  also pages `GhcrMirrorScaledToZero`.

### `DockerhubMirrorPullFailed`

The `dockerhub-mirror-pull` check on a node has failed for 45 minutes. The sidecar GETs
node-exporter's own manifest, by digest, through the node's docker.io mirror endpoint.
containerd falls back to Docker Hub past a broken mirror, so nothing breaks until a WAN outage,
when a pod whose image is not on its node cannot start.

- The GET failed in one of three ways: the endpoint refused it, it timed out, or it answered an
  error.
- Read the `dockerhub-mirror-pull` container log of the node-exporter pod on the alert's
  `origin` node.
- Then read the dockerhub-mirror pod, its NetworkPolicy and the node's `registries.yaml`.
- The 45-minute hold rides out the weekly garbage collection, which holds this mirror at zero
  for the same bounded window.


## GitHub checks

### `GitHubRulesetDrift`

The `github-ruleset-drift` check found a live GitHub ruleset that differs from the values
`hosts/components/github-checks/defaults/main.yml` declares.

- The check compares each ruleset's required status contexts, bypass actors, Renovate
  exclusion and agent-branch fence.
- The check also fails when the repo is not public, because the rulesets then do not enforce
  the merge gate.
- `github-ruleset-drift.sh` also ends `status=down` when it cannot read GitHub. A failed read
  leaves the rulesets unchanged, so re-run the check before editing anything.
- The `github-ruleset-drift` syslog tag on daniel-box names the ruleset id and the property
  that differs.
- Revert an accidental change in GitHub. For a deliberate one, update
  `hosts/components/github-checks/defaults/main.yml` to match.

### `GitHubInteractionLimitNotArmed`

`github-interaction-limit.sh` on daniel-box did not re-arm the repo's GitHub interaction limit.
The repo is open to non-collaborators until the limit is set again.

- The run ended `status=down` for one of three reasons: `gh` is not logged in for the deploy
  user, the PUT failed, or the limit read back did not match the declared one.
- Run `gh auth status` as the deploy user to check the login.
- Check the limit in the repo settings, and set it by hand if the script cannot.
