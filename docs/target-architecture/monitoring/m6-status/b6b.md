### B6b pod producers

B6b's soak read, [#6728](https://github.com/DanielH2018/server/issues/6728): B6b's shadow PR
merged as `9ebb51406c` at 2026-10-23T21:15Z. Prometheus first evaluated the `pod-producers` group
at 2026-10-23T21:22:47Z. The read used M1 convention 6's two queries over `[168h:1m]` ending at
2026-10-31T16:08Z, with M2's 8-minute margin (*Which soak minutes count as explained* in M2). The
window starts at 2026-10-24T16:08Z, after the group's first evaluation, so every tile has the
full 168 hours. All five tiles read up when the queries ran. Prometheus's oldest sample was 21.7
days old, and
`increase(prometheus_rule_evaluation_failures_total{rule_group=~".*;pod-producers"}[168h])` was 0.

Each tile maps to the rules that the shadow PR's comments in `pod-producers.yaml` assign to its
producer. The three sidecar tiles share `CrowdSecJobsExporterAbsent` and take
`CrowdSecJobStale` by the `check` label the sidecar exports:

- `CrowdSec Remote Allowlist`: `CrowdSecRemoteAllowlistFailed`,
  `CrowdSecJobStale{check="crowdsec-remote-allowlist"}`, `CrowdSecJobsExporterAbsent`.
- `CrowdSec AppSec`: `CrowdSecAppSecVerifyFailed`,
  `CrowdSecJobStale{check="crowdsec-appsec-verify"}`, `CrowdSecJobsExporterAbsent`.
- `CrowdSec Home Allowlist`: `CrowdSecHomeAllowlistFailed`,
  `CrowdSecJobStale{check="crowdsec-home-allowlist"}`, `CrowdSecJobsExporterAbsent`.
- `Configarr Sync`: `ConfigarrSyncFailed`, `ConfigarrHealthNotReporting`.
- `Janitorr Errors`: `JanitorrErrorsFound`, `JanitorrHealthNotReporting`.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| CrowdSec Remote Allowlist | 10040 | 291 | 240 | 69 | 40 | 18 | 0 |
| CrowdSec AppSec | 9986 | 0 | 0 | 0 | 0 | 0 | 0 |
| CrowdSec Home Allowlist | 10034 | 15 | 15 | 0 | 0 | 0 | 0 |
| Configarr Sync | 10015 | 0 | 0 | 0 | 0 | 0 | 0 |
| Janitorr Errors | 10010 | 0 | 0 | 0 | 0 | 0 | 0 |

Every firing minute belongs to a `Failed` rule. `CrowdSecJobStale` never fired: the largest
value of the job's age minus its deadline over the window was negative for all three jobs.
The two `NotReporting` rules never fired: the oldest successful run either CronJob reached was
833 seconds, against the rules' 1800. `CrowdSecJobsExporterAbsent` never fired:
`absent(up{job="crowdsec-jobs"})` held for 4 minutes on 2026-10-25 from 07:40 and 8 minutes on
2026-10-28 from 00:45, under its `for: 10m`, both inside spans when Prometheus itself had no
samples. The episodes, all times UTC:

- **CrowdSec Remote Allowlist and CrowdSec Home Allowlist, 2026-10-25 from 14:16.** Both jobs
  logged `kubectl … failed rc=1: Error from server (ServiceUnavailable)` while the API server
  was out. Remote Allowlist's tile and alert cover 14:16 to 14:30. Home Allowlist's cover 14:17
  to 14:31.
- **CrowdSec Remote Allowlist, fourteen episodes from 2026-10-30 17:21 to 2026-10-31 15:40.**
  Each run logged `status=down remote-ips at cap (8)`, with the address it could not add.
  [#6709](https://github.com/DanielH2018/server/issues/6709) owns that fault: the allowlist
  spends its cap on home addresses that `home-ips` already covers. In each episode the alert
  starts and ends one minute after the tile, so 14 Q1 strict and 13 Q2 strict minutes fall
  inside the margin.

**The 40 Q1 ±8m minutes are pushes Kuma rejected.** They run from 21:09 to 21:48 on 2026-10-30,
inside one DOWN span of the Remote Allowlist tile from 20:51 to 22:30. The job reported
`status=up remote-ips: 8 live` at every run from 21:00 to 21:50, and the sidecar exported
`homelab_check_success` 1, so `CrowdSecRemoteAllowlistFailed` resolved at 21:01 and fired again
at 21:57. Kuma kept the tile DOWN because it rejected all 12 pushes from 21:00 to 21:56 with
HTTP 404 and `SQLITE_CONSTRAINT: UNIQUE constraint failed: stat_hourly.monitor_id,
stat_hourly.timestamp`. Each rejected insert named monitor 480, the Remote Allowlist tile, and
the hour bucket 21:00Z. Kuma's own log has the same error, for that monitor and that bucket only.
The rejections began 26 minutes after a Kuma restart at 20:33:53. They ended at the hour: the
22:00 push landed. The tile showed a stale DOWN, and the rule read the job correctly. Swallowed
Push Verdicts stayed up by design, because it does not count a rejected push from a tag in
`KUMA_RETIRING_TAGS`, and the one lost `down` push at 21:56 was followed by the 22:00 push,
which landed.

**The 5 remaining Q2 strict minutes are a Kuma restart.** Kuma restarted at 01:06:10 on
2026-10-31, and the Remote Allowlist tile had no `monitor_status` series from 01:07 to 01:10.
`CrowdSecRemoteAllowlistFailed` kept firing through 01:11 for the at-cap episode that started at
00:46. A missing series is not DOWN, so Q2 strict counts those minutes, and the margin covers
them. Kuma restarted 11 times in the window, which explains the samples each tile lacks from
10080.

**Both sides were blind on 2026-10-28 from 00:40 to 00:52.** The Home Allowlist job logged
`status=down` at 00:41 and 00:46, because cluster DNS lookups timed out. Its pushes failed with
`URLError`, and Prometheus kept no samples from 00:40 to 00:52. Neither query can see those runs,
and neither side reported them.

**The Configarr Sync and Janitorr Errors rows are clean but do not show coverage.** Neither tile
nor verdict went down all week, so their 0s test nothing. Over the window the verdict series
`kube_configmap_annotations` held 5067 minute-samples for `configarr-health-verdict` and 4931 for
`janitorr-health-verdict`, of 10080. Over `[24h:1m]` it held 706 and 669 of 1440, and the
control, `pi-peer-backup-verdict`, held 1440. Each gap starts at a Flux server-side apply of the
role's Kustomization and ends at the reader's next `*/10` run. On 2026-10-31, configarr applied
at 15:17:18, 15:27:13 and 16:03:57, and its series was missing from 15:17 to 15:19, from 15:27 to
15:29, and from 16:04 to 16:09. The readers write the annotations with `kubectl patch`, which
records them under the field manager `kubectl-patch`. Flux strips fields owned by kubectl
managers on each apply. pi-peer-backup sends its PATCH with curl and loses nothing.
`ConfigarrSyncFailed` and `JanitorrErrorsFound` have no `for:`, so a real `down` verdict would be
visible only until the next Flux apply, and the rule would flap or miss it. The fix is in the two
readers, not in a rule. [#6746](https://github.com/DanielH2018/server/issues/6746) tracks it.

**The deployed rules, read at 16:11Z.** `kubectl get configmap -n observability
prometheus-rules -o jsonpath='{.data.pod-producers\.yaml}'` is byte-identical to
`pod-producers.yaml` on master. All ten B6b rules carry `shadow: "true"`, and no B6b alert was
pending or firing.

No Q1 minute is unexplained, so no rule changes. The soak does not block Cutover-a, which B6b
runs as Cutover-1a (*Every other producer* in M6), for the three crowdsec tiles. It does block
Cutover-1a for Configarr Sync and Janitorr Errors until
[#6746](https://github.com/DanielH2018/server/issues/6746) lands and a fresh read shows their
verdict series present every minute, as the control's is.

**The soak re-read and the gate, 2026-11-01T13:12Z.** The combined window's cutover PRs repeated
the queries above over `[168h:1m]` ending at 13:12Z (window start 2026-10-25T13:12Z, after the
group's first evaluation), with the same tile-to-rule mapping and M2's 8-minute margin.
Prometheus's oldest sample was 22 days old, and all five tiles read up when the queries ran.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| CrowdSec Remote Allowlist | 10015 | 392 | 345 | 75 | 40 | 28 | 0 |
| CrowdSec AppSec | 9945 | 0 | 0 | 0 | 0 | 0 | 0 |
| CrowdSec Home Allowlist | 10007 | 15 | 15 | 0 | 0 | 0 | 0 |
| Configarr Sync | 9988 | 0 | 0 | 0 | 0 | 0 | 0 |
| Janitorr Errors | 9983 | 0 | 0 | 0 | 0 | 0 | 0 |

Q1 ±8m is 40 for CrowdSec Remote Allowlist alone, the same 40 minutes of 2026-10-30 21:09 to 21:48
that the earlier read attributes to pushes Kuma rejected (the `stat_hourly` constraint error).
A read over the 20 hours since the earlier read's end shows 4 strict Q1 and 5 strict Q2 minutes
for that tile, all inside the margin: the sidecar logged `remote-ips at cap` through 2026-10-31
18:40Z, and [#6709](https://github.com/DanielH2018/server/issues/6709) closed at 18:54Z. The
sidecar has logged no `status=down` line since 18:40Z.

**The gate for Configarr Sync and Janitorr Errors holds.** The earlier read held their cutover
until [#6746](https://github.com/DanielH2018/server/issues/6746) landed and the verdict series
was present every minute, as the control's is. The fix merged as `e97ecb9a81` at
2026-10-31T19:16Z (the readers now patch with `--field-manager=configarr-health` and
`--field-manager=janitorr-health`).

- Over `[17h:1m]` (window start 2026-10-31T20:16Z), the count of minutes in which
  `kube_configmap_annotations{configmap="pi-peer-backup-verdict"}` exists and the configarr or
  janitorr verdict does not is empty for both. The control and the two series are present in the
  same minutes, which is the parity the earlier read asked for.
- Over `[18h:1m]` the same count is 5 for configarr and 7 for janitorr, all before 20:16Z, so the
  fix went live between 19:16Z and 20:16Z. Flux's applies of the role's Kustomization since then
  have not removed a verdict.
- Over `[6h:1m]` each of the two holds 358 of 360 minutes, and the control and
  `up{job="kube-state-metrics"}` hold the same 358, so the 2 missing minutes are common to every
  series and are not a Flux apply. They fall in the 07:15Z to 08:15Z hour of this morning's
  weekly reboot.
- The issue's own verify-by is a 24-hour window that starts after the fix's promote. It cannot be
  complete before 2026-11-02T20:16Z. The parity read above is the evidence this gate uses, and
  the 24-hour read stays open for the issue.

**CrowdSec v1.8.2 ([#6812](https://github.com/DanielH2018/server/issues/6812)) did not disturb the
sidecar.** The `crowdsec` pod started at about 07:37Z today (the weekly reboot) and runs 2 of 2
containers with 0 restarts. All three jobs export `homelab_check_success` 1, with last runs 131,
190 and 790 seconds old against their deadlines. `up{job="crowdsec-jobs"}` was 0 for one minute
in the last 18 hours, under `CrowdSecJobsExporterAbsent`'s `for: 10m`, and no
`CrowdSec*`, `Configarr*` or `Janitorr*` alert was pending or firing in that time.

**The deployed state, read at 13:20Z.** `pod-producers.yaml` in the live `prometheus-rules`
ConfigMap is byte-identical to master's file, and `KUMA_RETIRING_TAGS` in the live monitor-bridge
ConfigMap is identical to master's, which lists `crowdsec-remote-allowlist`,
`crowdsec-appsec-verify`, `crowdsec-home-allowlist`, `configarr-health` and `janitorr-health`.

B6b cuts over in the combined window of B2, B4 and B6b. The window has four stacked PRs:

1. Cutover-1a deletes the five tiles (`crowdsec-remote-allowlist`, `crowdsec-appsec`,
   `crowdsec-home-allowlist`, `configarr` and `monitor-bridge-janitorr`) with every rule still
   shadow. The pods keep pushing into deleted tokens until Cutover-2. Its live read is
   `probe.py metric 'monitor_status{monitor_name=~"CrowdSec .*|Configarr Sync|Janitorr Errors"}'`,
   which must return no series after the `uptime-kuma` promote. The pods' own Secrets still
   render the five tokens, so none enters `PENDING_RETIREMENT`.
2. Cutover-1b drops `shadow` from the nine rules and removes the five `CheckWithoutPager` terms.
3. The Flux-plane Cutover-2 stops the pushes from the crowdsec sidecar and the two CronJobs.
4. B6b's token PR retires the keys a week after Cutover-1b.

Flux does not promote on Sunday between 02:05 and 08:15 UTC, so a Flux-owned half that merges in
that span waits for 08:15Z, and a revert waits with it.

**Cutover-1b, the rules page.** The combined window's second PR drops `shadow` from the nine rules in `pod-producers.yaml` and
removes the five `CheckWithoutPager` terms of B6b (the three CrowdSec tiles, Configarr Sync and Janitorr Errors). Every rule file is `files/rules/pod-producers.yaml`.
`CrowdSecAppSecVerifyFailed`, `CrowdSecJobStale` and `CrowdSecJobsExporterAbsent` are `critical`; the other six are `warning`.

**Inhibits.** None is added. `CrowdSecJobsExporterAbsent` fires on `absent(up{job="crowdsec-jobs"})` and `ScrapeTargetDown` on `up == 0`, so one dead exporter never fires both arms. The two CronJob rules end in `unless on () (up{job="kube-state-metrics"} == 0)`, which leaves a dead kube-state-metrics to `ScrapeTargetDown`.

The no-pager bound is 2 hours from Cutover-1a's `uptime-kuma` apply. Cutover-1b is `observability`,
which Flux applies through `promote`, so it waits out Sunday 02:05 to 08:15 UTC like the rest of
the window.
