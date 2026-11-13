# M6 — Switch the readers and retire Kuma

Status: DONE. C-4b merged as [#7841](https://github.com/DanielH2018/server/pull/7841) on
2026-11-09, after the other Phase C PRs. All five exit criteria hold. Exits 1, 2, 3 and 5 were
read on 2026-11-12 at `061b0146e`. Exit 4's fixtures passed that day, and its live drill, which
is operator-only, ran at B-1d on 2026-10-17:

1. `ansible/roles/k8s/uptime-kuma/` does not exist. `probe.py metric 'up{job="uptime-kuma"}'`
   printed `No data`. `kubectl get svc,ingressroute,cm,cronjob,deploy -n homelab -l
   homelab/role=uptime-kuma` printed `No resources found in homelab namespace.` The same
   label across every namespace, with `pvc` and `pv`, printed `No resources found`. No PVC or
   PV name contains `kuma`, among 45 of each. None of the 44 Longhorn volumes carries a
   `kuma` namespace or PVC name, and every volume carries a PVC name. The 45th PV,
   `media-data`, is a local volume. The operator's delete after C-2b has therefore happened.
2. `grep -c '_push_token:'` printed 0 for `ansible/secret_rotation.yml` and for
   `ansible/vars/secrets.yml`. `secret_rotation.py audit --check` exited 0.
   [#7178](https://github.com/DanielH2018/server/issues/7178) closed on 2026-11-06.
3. C-4b landed after D6, closing [#7835](https://github.com/DanielH2018/server/issues/7835).
   The census row in `ansible/tests/repo/test_census_rows_text.py` held the allowlist until
   the operator retired it on 2026-11-12
   ([#8177](https://github.com/DanielH2018/server/issues/8177)).
4. `test_a_critical_alert_active_after_the_apply_that_still_fires_is_flagged` and
   `test_a_warning_alert_labelled_verify_active_after_the_apply_is_flagged`, in
   `scripts/diagnostics/tests/test_probe_health_alerts.py`, passed. At the B-1d drill,
   `probe.py health littlelink` exited 1 and named `HealthGateDrill{severity=critical}`
   (*PR B-1, with B-1d and B-1e*).
5. `probe.py --help` printed no `kuma` line. `probe.py monitors` printed
   `0 paging alert(s) firing` and exited 0.

Each batch's results are in *Status by batch*, below the introduction.

Depends on: M2, M3, M4, M5

Related issues: [#4563](https://github.com/DanielH2018/server/issues/4563), the health gap over the old ReplicaSet. `health_rollout.py`
survives M6, so the fix stays relevant.

Unblocks: nothing in the slice index. H2 and H3 keep the producer contract M3 pins.

This slice finishes the monitoring migration. It moves every check M2 to M5 left behind, using
the recipe each of those slices proved, switches every reader of Kuma to the alert plane, and
deletes Uptime Kuma, AutoKuma, `probe.py kuma-drift` and the push tokens. It follows the
conventions in [M1](m1-alertmanager.md#conventions-every-monitoring-slice-follows), which this
page calls "M1 convention N".

The slice index's one line, "switch the readers and retire Kuma", undersells it. Kuma cannot go
while anything still pushes to it. At the baseline, M2 to M5 leave behind:

- 38 bridge checks and 3 gates, among them M2's `workload_log_errors` row and the
  `swallowed_verdicts` row M5 hands over (M5 Open question 4);
- 38 other push producers, registry-gc among them (*Baseline*);
- 14 hand-written probes.

No other slice owns them. M2's Open question 6 names M6 as the fallback owner of the
`workload_log_errors` port, and no other slice's text adopts it, so this page owns it (batch
A7). M5 Open question 4 passes `swallowed_verdicts` and its token to M6, which deletes them in
C-7. This plan therefore runs in three phases (A drains the producers, B switches the readers,
C deletes Kuma). *Open questions* proposes splitting it in the slice index.

## Status by batch

Each batch, and each Phase B or Phase C PR, has its soak reads, cutovers and other results
below, in batch order. M6 is done, so this section is a closed record. Until 2026-11-12
each record lived in its own file under `m6-status/`, so that parallel PRs did not
conflict over one block (#6656).

### A1 hosts

A1's shadow rules are the 33 rules of `hosts.yaml`. The `hosts` group first evaluated at
2026-10-23T19:15:47Z, so the soak window starts there. The first soak read was not clean:
[#6621](https://github.com/DanielH2018/server/issues/6621) found 921 minutes in which the Memory
tile read DOWN with no matching shadow alert. [#6650](https://github.com/DanielH2018/server/pull/6650)
traced every one of them to the `claude-rc.service` crash loop on daniel-box (2026-10-26 00:23 to
2026-10-30 14:38 UTC). Its cgroup existed for one second per start, so the `claude-rc` series
appeared and vanished, and a bare `for: 10m` reset on every reappearance.
`ClaudeCgroupNotReporting` now wraps its condition in `max_over_time((...)[5m:1m])` and keeps
`for: 10m`. The tuned rule has been live since 2026-10-30 at 19:37Z.

The operator ruled on 2026-10-31 (comment on #6621) that A1's Cutover-a proceeds on #6650's 168h
replay plus the live window of the tuned rule, as for A2 ([#6628](https://github.com/DanielH2018/server/issues/6628)).
Any unexplained minute in either blocks.

**The re-read of the soak, 2026-10-31T11:30Z.** The read repeated #6650's queries over
`[168h:1m]` (M1 convention 6 with M2's 8-minute margin), mapping each tile to the `hosts.yaml`
rules that port its bridge row:

- Root Disk: `HostDiskFull`, and `HostMetricsMissing` and `HostCoverageShortfall` with
  `arm=~"disk.*"`.
- Memory: `HostMemoryHigh` and `ClaudeCgroup*`, and `HostMetricsMissing` and
  `HostCoverageShortfall` with `arm="memory"`.
- Host Temperature: `HostTemperature*`, `CPUThermalThrottled` and `PiUndervoltage`, and
  `HostMetricsMissing` and `HostCoverageShortfall` with `arm="thermal-throttle"`.
- UPS Battery Health: `UPS*`.
- Pi Pressure is B8's tile. A1 owns its Prometheus half: `PiLoadHigh`, `PiMemoryLow`,
  `PiDiskFull`, `PiFilesystemReadOnly` and `PiMetricsMissing`.

Prometheus's oldest sample was 21.5 days old, so retention covered the window.
`increase(prometheus_rule_evaluation_failures_total{rule_group=~".*;hosts"}[168h])` was 0.

| Tile | Samples | DOWN min | Firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| Root Disk | 10033 | 0 | 0 | 0 | 0 | 0 | 0 |
| Memory | 10033 | 1325 | 421 | 921 | 651 | 17 | 0 |
| Host Temperature | 10038 | 0 | 0 | 0 | 0 | 0 | 0 |
| UPS Battery Health | 10038 | 0 | 0 | 0 | 0 | 0 | 0 |
| Pi Pressure | 10038 | 50 | 59 | 3 | 0 | 12 | 2 |

The counts equal #6650's. Prometheus does not re-evaluate stored `ALERTS` under a changed rule,
so the replay still shows the untuned rule's 421 firing minutes. Every Q1 minute is explained:

- **Memory Q1, 921 strict minutes.** All 921 fall within 15 minutes of a gap in daniel-box's
  `claude-rc` series (921 of 921 covered, 0 uncovered). The five latest DOWN minutes sit at 14:29
  to 14:39 on 2026-10-30, inside the loop's tail, because `claude-rc.service` restored at
  14:38:30. A `[52h:1m]` window ending 2026-10-26T00:20Z, before the loop, reads Q1 0. #6650's
  backtest of the tuned rule gives Q1 0 for the whole 168h.
- **Memory Q2, 17 strict minutes.** One- to five-minute edges inside the same loop, where the
  bridge's sample caught the cgroup and pushed up while the rule was still firing. The ±8m margin
  absorbs all 17.
- **Pi Pressure Q1, 3 strict minutes (2026-10-26 04:35 to 04:37).** The tail of one Pi load
  episode. `PiLoadHigh` cleared at 04:34, and the tile held the bridge's DOWN push until the
  next cycle.
- **Pi Pressure Q2, 12 strict and 2 ±8m minutes.** The 2 are 2026-10-26 03:33 and 03:34, where
  `PiLoadHigh` crossed 1.5 per core before the bridge's first DOWN cycle at 03:41. `b8.md`
  records the same episode.
- **Root Disk, Host Temperature, UPS Battery Health.** No DOWN minute and no shadow alert in 168h.

**The live window of the tuned rule, 2026-10-30T19:37Z to 2026-10-31T11:30Z (`[954m:1m]`).** All
five tiles read 936 samples, 0 DOWN minutes and 0 firing minutes. Q1 and Q2 are 0, strict and
±8m, for all five. `ALERTS{alertname="ClaudeCgroupNotReporting"}` has no pending or firing
sample, so the tuned rule stayed quiet rather than merely unfired. The 18 minutes without a tile
sample are five gaps of 3, 2, 5, 3 and 5 minutes, in the hours starting 19:37, 20:37, 22:37,
00:37 and 02:37Z. `up{job="uptime-kuma"}` never read 0, and the Kuma pod started at 02:42:44Z.

**The cgroup arms.** H7.40 slice 1 (2026-10-31, about 02:00Z) runs both `claude-rc.service` and
`homelab-claude-rc.service` on daniel-box. The `claude-rc` and `fleet` series on daniel-box, and
`fleet` on daniel-server, report in 600 of 600 minutes over 10 hours. The packaged unit
crash-loops beside the Ansible-era one (477 restarts, each exiting 1 with `This folder is already
served by a terminal claude remote-control`), and `claude-cgroup-metrics.sh` prefers
`claude-rc.service`'s cgroup, which is why the series is steady. [#6703](https://github.com/DanielH2018/server/issues/6703)
tracks it. A restart in which the two units swap the cgroup the writer reads would flap the
series again, and `ClaudeCgroupNotReporting` would then page.

**The deployed rules, read at 11:31Z.** `kubectl get configmap -n observability prometheus-rules -o jsonpath='{.data.hosts\.yaml}'`
is byte-identical to `hosts.yaml` on master. All 33 rules carry `shadow: "true"`,
`prometheus_rule_group_rules` for the group reads 33, and no A1 alert was firing.

A1's cutover is three stacked PRs that merge in this order, not the plan's single Cutover-a.
uptime-kuma and monitor-bridge are both `owner: flux`, so no deploy orders a tile deletion
before a row deletion, and the batch uses the owner-independent fallback of *When D5 flips a
role M6 edits*, as A2 to A5 did:

1. Cutover-a1, [#6704](https://github.com/DanielH2018/server/pull/6704): an exclusion in the
   `CHECKS` loop of `static-monitors.yaml.j2` deletes the four tiles. Every rule stays shadow.
   Its live read is `probe.py metric 'monitor_status{monitor_name=~"Root Disk|Memory|Host Temperature|UPS Battery Health"}'`,
   which must return no series. Pi Pressure keeps its tile for B8.
2. Cutover-a2, [#6706](https://github.com/DanielH2018/server/pull/6706): the four bridge rows, their check bodies and their env keys go, and the
   four push tokens enter `PENDING_RETIREMENT`. It merges only after the read above passes.
3. Cutover-b, the PR that adds this paragraph: the 28 shadow rules of `hosts.yaml` that A1 owns drop `shadow`, and the
   `kuma-cutover` group loses A1's four terms. `PiMetricsMissing`'s undervoltage arm becomes its
   own rule, `PiUndervoltageMissing`. `test_alertmanager_routing.py` moves the rules to `PAGING`:
   `HostDiskFull` and the five `UPS*` rules as critical alerts to Discord and email, the rest as
   warnings to Discord.

**The five Pi pressure rules stay shadow.** `PiLoadHigh`, `PiMemoryLow`, `PiDiskFull`,
`PiFilesystemReadOnly` and `PiMetricsMissing` are the Prometheus half of `pi_pressure`, and B8's
`Pi Pressure` tile counts them. B8 cuts that row over and drops their `shadow`. A1 therefore adds
no `CheckWithoutPager` term for Pi Pressure, and deletes nothing those rules read.

**The undervoltage arm.** `PiMetricsMissing` held an `undervoltage` arm that is check_host_temp's:
the bridge paged `no undervoltage alarm sensor scraped (Pi node-exporter blind?)` while the Pi's
scrape was up. Left in a rule that stays shadow until B8, that page would have no pager after
Cutover-a2 deleted the row. Cutover-b moves the arm into `PiUndervoltageMissing` (same
expression, `for: 10m`, `severity: warning`, `service: daniel-pi`, no `shadow`) and leaves
`PiMetricsMissing` its four other arms. The arm never fired in the 168h window (`ALERTS` for
`PiMetricsMissing{arm="undervoltage"}` has no sample, pending or firing).

**Severity of the tiles against their rules.** Root Disk and UPS Battery Health were `critical`
tiles on the email tier. `HostDiskFull` and the five `UPS*` rules are `critical`, so they keep
Discord and email. Memory and Host Temperature were not on the tier, and their rules are
`warning`. Root Disk's missing-metric arms (`HostMetricsMissing` and `HostCoverageShortfall` with
`arm=~"disk.*"`) are `warning`, so they page Discord alone where the tile mailed too.

**Inhibit decision: A1 needs none.** The plan turns the bridge's `EXPORTER_DEPENDENT` map into an
inhibit sourced on `ScrapeTargetDown`, which A3's Cutover-b made live. The shadow PR wrote the
rules so that nothing remains for it to hold:

- Every missing-data arm (`HostMetricsMissing`, `HostTemperatureNotReporting`,
  `ClaudeCgroupNotReporting`, `PiUndervoltageMissing`, `UPSSensorAbsent`) starts from `up == 1`,
  so it fires only while the scrape succeeds.
- Every threshold arm reads a series that a dead scrape drops, so it goes quiet.
- `ClaudeCgroupNotReporting` holds its arm for 5 minutes after the last `up == 1` sample. That
  hold ends before `ScrapeTargetDown`'s `for: 10m` fires.
- `HostCoverageShortfall` and `HostTemperatureCoverageShortfall` do fire beside
  `ScrapeTargetDown`, because they count hosts. Neither carries `job` or `instance`, so an
  `equal: [job, instance]` inhibit cannot match them. A keyless inhibit would mute the census
  while a renamed series empties another host's vector. A dead exporter pages twice there, the
  safe direction, and A5 ruled the same way for its two census rules.

A promtool case in `hosts.test.yaml` pins the claim: with the node and node-pi scrapes at `up 0`
and every other series absent, only the two coverage floors fire. A `# DECIDED:` marker in the
header of `hosts.yaml` records it.

**Soak effect.** `renderer_lib/soak.py:longest_for` counts the `for:` of every unshadowed
`critical` rule against its `service` label. Read on the tree of Cutover-a1 and on the tree of
Cutover-b, the map differs by one key. `nut-exporter: 600` is new, from the five `UPS*` rules
(`for: 10m`). `HostDiskFull` has no `for:`, and every other A1 rule is `warning`, so the other
keys do not move. `nut-exporter` is a published tag, so a promote scoped to it waits 600s plus
the scrape and evaluation margin, up from the 120s floor that rules with a templated `service`
give every tag. A fleet-wide promote already waits on `deploy-pipeline`'s 900s, so its wait does
not move. `test_lander_deadlines_cover_the_soak.py` and the `Lander deadlines cover the soak`
hook pass.

The two-hour no-pager bound counts from the moment the `uptime-kuma` Kustomization reports
Cutover-a1's merge commit, because Kuma stops paging the four checks then and every A1 rule is
still shadow. It ends when the `observability` Kustomization reports Cutover-b's merge commit.
Cutover-a2's apply falls inside it. `CheckWithoutPager` pages critical two hours after the last
`monitor_status` sample of any of the four tiles. If the bound is exceeded, revert Cutover-a2
first when it has merged, so the bridge pushes again, and then Cutover-a1, so the tiles return.
Reverting Cutover-a1 alone after Cutover-a2 restores tiles that no push reaches, and they go
DOWN. Both reverts worked only until the A1 token PR,
[#6949](https://github.com/DanielH2018/server/pull/6949), retired the keys.

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens, and the operator's commit removed the keys from `secrets.yml` on 2026-11-02. A revert of
a cutover above no longer brings back a working tile, because no token is left for it to push
with.

### A2 containers

A2's soak read, [#6622](https://github.com/DanielH2018/server/issues/6622), recorded in
[#6630](https://github.com/DanielH2018/server/pull/6630), and its cutover-time re-read,
[#6628](https://github.com/DanielH2018/server/issues/6628). Every minute is explained. The
container-checks group loaded at 2026-10-23T18:19:47Z, which is the first sample of
`prometheus_rule_group_rules` for the group. Each tile's alerts are the ones the shadow PR
`8dc305303` wrote for it: the tile's own alert, plus `CAdvisorCoverageShortfall` with that tile's
`arm`. "Strict" counts same-minute coincidence, and "±8m" is M1 convention 6's tolerance, as the
queries in `m2-first-bridge-family.md` (*Verification*) write it.

**The operator's ruling on 2026-10-30.** Cutover-a does not wait for a full 168h live soak of the
tuned `ContainerCPUThrottled`. It proceeds on [#6630](https://github.com/DanielH2018/server/pull/6630)'s replay over stored series plus the live
window available at cutover time
([the ruling](https://github.com/DanielH2018/server/issues/6628#issuecomment-6534675658)). Before
Cutover-a, both reads below run again at cutover time, and any unexplained minute in either
blocks the cutover.

**The 168h read of the three tiles.** It ran over `[168h:1m] @ 1793398800`, which ends at
2026-10-30T22:20:00Z. Its DOWN, firing, Q1 and Q2 counts equal [#6630](https://github.com/DanielH2018/server/pull/6630)'s read at
18:21 UTC. Each tile's sample count is 5 lower. The 4 hours the window dropped were fully
sampled, and the 4 hours it added hold the 5-minute gap the live read below explains.
`increase(prometheus_rule_evaluation_failures_total{rule_group=~".*container-checks"}[168h])` is
0 at the same end, so the zeros below are evaluations that stayed quiet, not failures.

| Tile | Samples | DOWN min | Firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| k3s Container Restarts | 10046 | 0 | 0 | 0 | 0 | 0 | 0 |
| k3s Container OOM | 10051 | 0 | 0 | 0 | 0 | 0 | 0 |
| k3s CPU Throttling | 10051 | 21 | 5 | 21 | 16 | 5 | 0 |

The CPU tile's minutes are [#6630](https://github.com/DanielH2018/server/pull/6630)'s three episodes, all under the rule as deployed before
[#6630](https://github.com/DanielH2018/server/pull/6630). Prometheus does not re-evaluate stored `ALERTS` under a changed rule, so these counts
stay the same until the episodes leave the window.

- The 16 Q1 ±8m minutes are E1 (2026-10-29 01:44–54) and E2 (03:16–20). A dip of one or two
  minutes reset the old rule's `for: 15m` on `kustomize-controller-755dcfc6dc-fg8sv`. [#6630](https://github.com/DanielH2018/server/pull/6630)'s
  `max_over_time(...[5m:1m])` closes both. Its replay of the tuned rule leaves 0 Q1 ±8m minutes
  over the same series.
- The 5 Q2 strict minutes are the old rule firing on `fg8sv` at 2026-10-29 21:32–36, six
  minutes before E3's DOWN. They fall inside ±8m.

**The live read of the tuned rule.** [#6630](https://github.com/DanielH2018/server/pull/6630) merged at 2026-10-30T18:40:25Z. Prometheus reloaded
its rules at 18:38:16, before the merge, and next at 18:53:16
(`prometheus_config_last_reload_success_timestamp_seconds`), so the tuned rule went live at
18:53:16. The live `prometheus-rules` ConfigMap carries the `max_over_time` expression. The read
ran over `[3h:1m]`, the whole hours since the deploy, and over `[206m:1m]`, which starts at
18:54. Both end at 2026-10-30T22:20:00Z.

The tile has no sample at 20:34–20:38, which is the 5 minutes the sample counts miss. Uptime
Kuma restarted then. Its pod `uptime-kuma-67dcd4d546-7xtwh` started at 20:33:52
(`kube_pod_start_time` = 1793392432), rolled by A3's Cutover-a1 (`bc0b08115`, merged 20:26:50).
Kuma's exporter rebuilt its tiles gradually, from 11 `monitor_status` series at 20:34 to 70 at
20:40, and the CPU tile came back at 20:39. Prometheus kept evaluating rules through the gap,
because `Watchdog` has a sample in every minute. In those 5 minutes the tuned rule never fired.
The bridge's only `cpu` line in the window is a `streak 1/3` at 20:38:47, so it pushed no
DOWN for Kuma to store.
Q1 cannot see a minute with no tile sample, so the bridge's log is what covers the gap.

| Window | Samples | DOWN min | Firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| `[3h:1m]` | 175 | 0 | 0 | 0 | 0 | 0 | 0 |
| `[206m:1m]` | 201 | 0 | 0 | 0 | 0 | 0 | 0 |

The window held two throttling episodes, and neither side paged either one:

- `fg8sv` was pending from 19:56:54 to 20:04:54, 9 minutes, under `for: 15m`. The bridge logged
  no streak line for it.
- `uptime-kuma-67dcd4d546-7xtwh` was pending from 20:36:54 to 20:44:54, 9 minutes. The bridge
  logged one `throttling streak 1/3` line for the pod at 20:38:47 and nothing after it.
  This is the restarted Kuma pod's start-up CPU.

The live window is short, and it holds no episode long enough to page either side. It confirms
that the tuned rule does not fire where the bridge stays quiet. That E1 and E2 now page rests on
the replay, as the ruling accepts.

A2's soak read of the shadow PR, [#6622](https://github.com/DanielH2018/server/issues/6622):
A2's shadow PR is `8dc3053034`. Prometheus first evaluated the `container-checks` group at
2026-10-23T18:19:47Z, so the soak window starts there. The read used M1 convention 6's two
queries with M2's 8-minute margin (*Which soak minutes count as explained* in M2). It mapped
each tile to its own alert plus `CAdvisorCoverageShortfall` with that tile's `arm`. Over
`[168h:1m]`, read on 2026-10-30 at 18:21Z, k3s Container Restarts and k3s Container OOM
returned no minutes for either query. k3s CPU Throttling returned 16 minutes for query 1 and
none for query 2. The 16 minutes are two kustomize-controller episodes on 2026-10-29, from 01:44
to 01:54 and from 03:16 to 03:20. In both, a dip in the 15m-window condition shorter than one
bridge cycle reset `ContainerCPUThrottled`'s `for: 15m`, so they are misses.
[#6630](https://github.com/DanielH2018/server/pull/6630) tunes the rule: its condition sits
inside `max_over_time(...[5m:1m])`, so a sub-cycle dip no longer resets the hold. The PR's
figures for the tuned rule are a replay over stored series, which convention 6 does not accept
as a soak.

The tuned rule's live soak, [#6648](https://github.com/DanielH2018/server/issues/6648): #6630
merged as `4a871e9140`. Its `promote` run (41109931254) ran from 18:47:44Z to 18:55:05Z, and
`prometheus_config_last_reload_success_timestamp_seconds` records one reload inside it, at
2026-10-30T18:53:16Z. The tuned rule's soak window starts there. No later commit changes
`container-checks.yaml`, so the reloads after 18:53Z leave the rule as #6630 deployed it. Over
`[3h:1m]`, and over `[200m:1m]` to cover the window from 18:55Z, read on 2026-10-30 at 22:15Z,
both queries returned no minutes for k3s CPU Throttling, with and without the 8-minute margin.
Kuma read DOWN in no minute of the window, and the tile reported 195 of 200 minutes.
`ContainerCPUThrottled` fired in no minute. It went pending for 9 minutes on
`kustomize-controller-755dcfc6dc-fg8sv` and for 9 on `uptime-kuma-67dcd4d546-7xtwh`, both under
its `for: 15m`, and `probe.py alerts --check cpu` listed no bridge DOWN in the last day. The
group's evaluation failures over the window were 0, and retention covered 20.9 days.

The window holds no throttling episode, so it shows that the tuned rule does not fire with Kuma
up. It does not yet show that the rule catches an episode of E1's or E2's shape live. The
operator ruled on 2026-10-30 that Cutover-a does not wait for the full 168h; see the ruling
above.
[#6628](https://github.com/DanielH2018/server/issues/6628) tracks the full-length live soak.

A2's cutover is three stacked PRs that merge in this order, not the plan's single Cutover-a.
uptime-kuma and monitor-bridge are both `owner: flux`, so no deploy orders a tile deletion
before a row deletion, and the batch uses the owner-independent fallback of *When D5 flips a
role M6 edits*, as A3 and A4 did:

1. Cutover-a1, [#6682](https://github.com/DanielH2018/server/pull/6682): an exclusion in the
   `CHECKS` loop of `static-monitors.yaml.j2` deletes the three tiles. Every rule stays shadow.
   Its live read is `probe.py metric 'monitor_status{monitor_name=~"k3s Container Restarts|k3s Container OOM|k3s CPU Throttling"}'`,
   which must return no series.
2. Cutover-a2, [#6684](https://github.com/DanielH2018/server/pull/6684): the three bridge rows,
   their check bodies and helpers, the coverage verdict, the cAdvisor selector builder and their
   env keys go, and the three push tokens enter `PENDING_RETIREMENT`. It merges only after the
   read above passes.
3. Cutover-b, the PR that adds this paragraph: the four `container-checks.yaml` rules drop
   `shadow`, and the `kuma-cutover` group loses A2's three terms. `test_alertmanager_routing.py`
   moves the four rules from the blackhole to `PAGING`, all as warnings to Discord.

The tuned `ContainerCPUThrottled` was live on 2026-10-31 at 00:17Z. The `prometheus-rules`
ConfigMap's `container-checks.yaml` is byte-identical to the one on master, carries the
`max_over_time(...[5m:1m])` form, and still has `shadow: "true"` on all four rules. The
operator's ruling above lets Cutover-a proceed on that. Both soak reads run again at the moment
Cutover-a1 merges, as the ruling requires, and an unexplained minute in either blocks it.

The plan names no inhibit for A2, and this batch adds none. The three per-pod rules read
counters that a dead `kubernetes-cadvisor` scrape leaves empty, so they go quiet instead of
paging beside `ScrapeTargetDown`. `CAdvisorCoverageShortfall` is the one rule that fires on a
dead scrape, because its `or vector(0)` arm reads an empty vector as zero pods. It pages beside
`ScrapeTargetDown{job="kubernetes-cadvisor"}`, which is the double page the plan accepts as the
safe direction until an inhibit lands; the two share a 10-minute `for:`, so neither fires
first. An inhibit on `ScrapeTargetDown` would need `equal: [job, instance]` to stay narrow,
and the census carries neither label. Without `equal`, one node's dead cAdvisor would mute the
census while a selector break empties the other node's vector, which is the fault the census
exists to catch. None of A2's rules is critical, so dropping `shadow` leaves
`renderer_lib/soak.py:longest_for` unchanged.

The two-hour no-pager bound counts from the moment the `uptime-kuma` Kustomization reports
Cutover-a1's merge commit, because Kuma stops paging the three checks then and every A2 rule is
still shadow. It ends when the `observability` Kustomization reports Cutover-b's merge commit.
Cutover-a2's apply falls inside it. `CheckWithoutPager` pages critical two hours after the last
`monitor_status` sample of any of the three tiles. If the bound is exceeded, revert Cutover-a2
first when it has merged, so the bridge pushes again, and then Cutover-a1, so the tiles return.
Reverting Cutover-a1 alone after Cutover-a2 restores tiles that no push reaches, and they go
DOWN. Both reverts worked only until the A2 token PR,
[#6949](https://github.com/DanielH2018/server/pull/6949), retired the keys.

**The cutover-time re-read, 2026-10-31.** The ruling requires both soak reads to run again
before Cutover-a1 merges. They ran with the queries this record already uses, at about 00:54Z,
before [#6682](https://github.com/DanielH2018/server/pull/6682) merged. The first table
re-runs the recorded 168h read pinned at `@ 1793398800`. Every count equals the recorded one,
which shows the queries are the recorded ones.

| Tile | Samples | DOWN min | Firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| k3s Container Restarts | 10046 | 0 | 0 | 0 | 0 | 0 | 0 |
| k3s Container OOM | 10051 | 0 | 0 | 0 | 0 | 0 | 0 |
| k3s CPU Throttling | 10051 | 21 | 5 | 21 | 16 | 5 | 0 |

The second table is the same `[168h:1m]` window ending at about 00:54Z. Restarts and OOM have
5 fewer samples because the window slid 4 hours and the Uptime Kuma restart at 20:33 left a
5-minute gap. The CPU tile's Q1 counts are unchanged. Its firing and Q2 strict counts each rose
by 2, and its Q2 ±8m count rose from 0 to 2.

| Tile | Samples | DOWN min | Firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| k3s Container Restarts | 10041 | 0 | 0 | 0 | 0 | 0 | 0 |
| k3s Container OOM | 10046 | 0 | 0 | 0 | 0 | 0 | 0 |
| k3s CPU Throttling | 10046 | 21 | 7 | 21 | 16 | 7 | 2 |

The live window of the tuned rule runs from its deploy at 18:53:16Z on 2026-10-30. Over
`[6h:1m]` (350 samples) and `[364m:1m]` (354 samples), both ending at about 00:54Z, all three
tiles have 0 DOWN minutes, 0 Q1 strict and 0 Q1 ±8m. Restarts and OOM have 0 firing minutes.
The CPU tile has 2 firing, 2 Q2 strict and 2 Q2 ±8m minutes, the pair explained below. The
missing minutes are the two Kuma restarts, at 20:33 and 23:19.

Every Q1 minute is one already recorded. The 16 within ±8m are E1 and E2, and the other 5 match
the 21:32-36 firing on 2026-10-29. No Q1 minute is unexplained.

The two new Q2 minutes are 23:37:54Z and 23:38:54Z on 2026-10-30. `ContainerCPUThrottled`
fired on `homelab/uptime-kuma-7b98fc5554-xklfl` while the CPU tile was up.

- The pod started at 23:19:57Z, a new Kuma pod beside the earlier `67dcd4d546`.
- The rule went pending at 23:22:54Z and fired after its `for: 15m`.
- The bridge logged `throttling streak 1/3` at 23:24:40Z and `2/3` at 23:29:44Z, and no `3/3`.
  `probe.py alerts --check cpu --days 1` lists no DOWN.
- The bridge's third sample, at about 23:34:45Z, read clean, so its streak stopped at 2/3. The
  rule's `max_over_time(...[5m:1m])` kept the alert up for about 4 to 5 minutes after the raw
  condition cleared, which is the 5m hold the rule's comment documents.

This is a rule-side extra page for one of Kuma's own pod restarts, not a missed outage. The
raw per-minute condition was not recomputed, so the explanation is consistent with the
evidence rather than proven. The operator treated the pair as explained on 2026-10-31 and
proceeded with Cutover-a1.

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens, and the operator's commit removed the keys from `secrets.yml` on 2026-11-02. A revert of
a cutover above no longer brings back a working tile, because no token is left for it to push
with.

### A3 scrape

A3's soak read, [#6620](https://github.com/DanielH2018/server/issues/6620), recorded in
[#6645](https://github.com/DanielH2018/server/pull/6645): every minute is explained, with no rule
change. The read found one shape the tile pages and no A3 rule did: a scrape target that flaps
without holding `up == 0` for 10 minutes. [#6626](https://github.com/DanielH2018/server/issues/6626)
closed it with `ScrapeTargetFlapping` ([#6640](https://github.com/DanielH2018/server/pull/6640)),
which unblocked the cutover, described after the read. The shadow rules loaded at
2026-10-23T18:23:32Z, which is the first sample of `prometheus_rule_group_rules` for the `scrape-targets` and `log-pipeline` groups.
The read ran M1 convention 6's two queries per tile over `[168h:1m] @ 1793384612`, which ends at
2026-10-30T18:23:32Z. Each tile's alerts are the ones the shadow PR `dae51d452` wrote for it.
`ScrapeTargetDown` is split by `origin`, the same selector monitor-bridge's two checks use, and
`ScrapeTargetsMissing` counts for both scrape tiles.

- **Scrape Targets.** Query 1 returned 0 minutes and query 2 returned 0.
- **Cluster Scrape Targets.** Query 1 returned 20 minutes and query 2 returned 12.
    - 6 query-1 minutes are the tile's trailing edge. A tile reads DOWN until the bridge's next
      push, and the bridge pushes every 300 s (`INTERVAL`). The last firing samples are at
      2026-10-25 14:32, 2026-10-26 04:35 and 2026-10-29 13:04, and the tile read DOWN at most
      4 minutes longer each time (14:34–36, 04:37 and 13:06–07).
    - 14 query-1 minutes (2026-10-26 03:58–04:07 and 04:28–31) are daniel-pi failing before its
      unplanned reboot at 04:32:54. load1 reached 20.8 and MemAvailable fell to 99 MB. Its four
      exporters failed scrapes for 1 to 4 minutes at a time from 03:31. `check_cluster_targets`
      keeps one down streak for the whole check, so three 300 s cycles that each caught one of
      the Pi's exporters down paged the tile. No single series held `up == 0` for
      `ScrapeTargetDown`'s 10 minutes until alloy-pi's run from 04:21. Kuma's `Pi Pressure`
      tile read DOWN from 03:43 to 03:47 and from 03:53 to 04:37, and `Daniel Pi Recovery` from
      03:59 to 04:36, so the host fault had a live pager throughout. Their shadow successors,
      `PiLoadHigh` (03:37 to 04:33) and `PiRecoveryFailed` (03:58 to 04:35), fired too. A target
      that flaps on a healthy host would have no pager after Cutover-a, which is why Cutover-a
      waited on [#6626](https://github.com/DanielH2018/server/issues/6626), closed by
      [#6640](https://github.com/DanielH2018/server/pull/6640).
    - 5 query-2 minutes are the rule's leading edge (2026-10-25 14:26, 2026-10-30 12:36–39). The
      rule's `for: 10m` pages before the bridge's three-cycle streak
      (`CLUSTER_TARGETS_CONSECUTIVE`).
    - 7 query-2 minutes are a monitor-bridge restart clearing the tile while prowlarr's
      exportarr sidecar still read `up == 0` (2026-10-25 22:49–51 and 2026-10-30 12:52–55). The
      new bridge pods started at 22:47:21 and 12:50:31. A restart resets the in-memory streak,
      so the first cycle pushes `up`. The rule kept firing until prowlarr's new pod replaced
      the target, at 22:49:28 and 12:52:38.
- **Log Shipper Dropped Entries.** Query 1 returned 6 minutes and query 2 returned 0. The
  episode is the Pi's Alloy backlog after its reboot. Over two hours the Pi's Alloy dropped
  35,938 entries as `ingester_error`, and Loki discarded 56,494 as `too_far_behind`.
    - 3 minutes (2026-10-26 04:38–40) are the leading edge. Both rules went pending at 04:35:39
      and fired after their `for: 5m`, at 04:40:39 and 04:41:39. The bridge pushed DOWN on its
      first breaching cycle, and the tile read it at 04:37:09.
    - 3 minutes (05:36–38) are the trailing edge. The last firing sample is 05:34:39, and the
      tile read up at 05:38:09, inside one `INTERVAL`.

The `Cluster Scrape Targets` tile has no samples at 2026-10-25 07:39–47 (the weekly reboot),
2026-10-25 13:14–16 and 18:08–10, 2026-10-28 00:44–53 and 01:27–30, and 2026-10-29 21:32–38.
Query 2 counts a shadow alert that fires while the tile has no sample, and it counted none in
those gaps. `Watchdog` has a sample in every minute of four of the gaps, so Prometheus was
evaluating rules there. It has 2 samples in the reboot gap's 8 minutes and none at 2026-10-28
00:44–53, so neither side was read for most of those two gaps.

A3's cutover is three stacked PRs that merge in this order, not the plan's single Cutover-a.
uptime-kuma and monitor-bridge are both `owner: flux`, so no deploy orders a tile deletion
before a row deletion, and the batch uses the owner-independent fallback of *When D5 flips a
role M6 edits*:

1. Cutover-a1, [#6651](https://github.com/DanielH2018/server/pull/6651): an exclusion in the
   `CHECKS` loop of `static-monitors.yaml.j2` deletes the three tiles. Every rule stays shadow.
   Its live read is `probe.py metric 'monitor_status{monitor_name=~"Scrape Targets|Cluster Scrape Targets|Log Shipper Dropped Entries"}'`,
   which must return no series.
2. Cutover-a2, [#6652](https://github.com/DanielH2018/server/pull/6652): the three bridge rows,
   their check bodies and their env keys go, and the push tokens enter `PENDING_RETIREMENT`. It
   merges only after the read above passes.
3. Cutover-b, [#6653](https://github.com/DanielH2018/server/pull/6653): the A3 rules drop `shadow`, `BlackboxExporterDown` goes live with its
   inhibit on `ScrapeTargetDown{job="blackbox-ingress"}`, `LokiHomelabDown` is deleted, and the
   `kuma-cutover` group loses A3's three terms.

The two-hour no-pager bound counts from the moment the `uptime-kuma` Kustomization reports
Cutover-a1's merge commit, because Kuma stops paging the three checks then and every A3 rule is
still shadow. It ends when the `observability` Kustomization reports Cutover-b's merge commit.
Cutover-a2's apply falls inside it. `CheckWithoutPager` pages critical two hours after the last
`monitor_status` sample of any of the three tiles. If the bound is exceeded, revert Cutover-a2
first when it has merged, so the bridge pushes again, and then Cutover-a1, so the tiles return.
Reverting Cutover-a1 alone after Cutover-a2 restores tiles that no push reaches, and they go
DOWN. Both reverts worked only until the A3 token PR,
[#6949](https://github.com/DanielH2018/server/pull/6949), retired the keys.

Cutover-b leaves `FluxControllerDown`'s `up == 0` arm in `flux.yaml` shadow, and
`ScrapeTargetDown{job="flux"}` pages a dead controller after 10 minutes in the meantime. The
operator ruled on 2026-10-30 that a follow-up drops the label together with task 6's flux
inhibit ([#6654](https://github.com/DanielH2018/server/issues/6654)).

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens, and the operator's commit removed the keys from `secrets.yml` on 2026-11-02. A revert of
a cutover above no longer brings back a working tile, because no token is left for it to push
with.

### A4 edge

A4's soak read, [#6623](https://github.com/DanielH2018/server/issues/6623): A4's shadow PR is
`e79316c0bf`. Prometheus first evaluated the `edge` group at 2026-10-23T18:32:32Z, so the soak
window starts there. The read used M1 convention 6's two queries with M2's 8-minute margin
(*Which soak minutes count as explained* in M2). It mapped each tile to the `edge.yaml` rules
that port its bridge row. `EdgeMetricsMissing` counts for every tile except Traefik 421. Over
`[168h:1m]`, read on 2026-10-30 at 18:42Z, query 1 returned 20 minutes for Traefik Latency and
none for the other four tiles. Query 2 returned none for any tile. The 20 minutes are four
single-cycle bridge episodes: sonarr on 2026-10-27 at 00:27, and jellyfin on 2026-10-29 at
21:52 and on 2026-10-30 at 00:59 and 14:54. The bridge's `traefik_latency` row has no streak, so
it pages on one bad sample, and `TraefikBackendSlow` held `for: 5m`. M2's duration query, with
the per-backend condition as the arm, counted 2, 4, 4 and 5 bad minutes. The first three are
below the rule's `for:`, so they are explained. The fourth is a miss: the condition held from
14:45 to 14:48, failed for 3 minutes, then held from 14:52 to 14:54, and each dip reset the
hold. [#6629](https://github.com/DanielH2018/server/pull/6629) tunes the rule to fire when its condition held in at least 5 of the last 10
minutes, without a `for:`, and adds rule tests for both shapes, built from jellyfin's counters.
Replayed over the same `[168h:1m]`, the tuned rule reads true for jellyfin from 14:52 to 14:56
and nowhere else, which the 8-minute margin matches to Kuma's 14:55 to 14:59 DOWN minutes. That
count is a projection. Prometheus does not re-evaluate stored `ALERTS` under a changed rule, so
a re-run of query 1 over the same window still returns 20. Jellyfin's bursts, which may be
streams, are filed as [#6649](https://github.com/DanielH2018/server/issues/6649). The Traefik
5xx episode on 2026-10-25 from 14:22 to 14:26 is covered, because `TraefikBackend5xx` fired
from 14:23 to 14:25. Kuma held every A4 tile at status 3 (maintenance) from 07:30 to 08:16 that
day, which neither query counts. A4's Cutover-a waits until the tuned rule is live in the
`prometheus-rules` ConfigMap.

The tuned rule was live in the `prometheus-rules` ConfigMap on 2026-10-30 at 22:18Z:
`edge.yaml` there carries `count_over_time(` in `TraefikBackendSlow` and `shadow: "true"` on all
seven rules. That met the condition above.

A4's cutover is three stacked PRs that merge in this order, not the plan's single Cutover-a.
uptime-kuma and monitor-bridge are both `owner: flux`, so no deploy orders a tile deletion
before a row deletion, and the batch uses the owner-independent fallback of *When D5 flips a
role M6 edits*, as A3 did:

1. Cutover-a1, [#6668](https://github.com/DanielH2018/server/pull/6668): an exclusion in the
   `CHECKS` loop of `static-monitors.yaml.j2` deletes the five tiles. Every rule stays shadow.
   Its live read is `probe.py metric 'monitor_status{monitor_name=~"TLS Cert Expiry|Traefik 5xx|Traefik Latency|Traefik 404 Flood|Traefik 421"}'`,
   which must return no series.
2. Cutover-a2, [#6670](https://github.com/DanielH2018/server/pull/6670): the five bridge rows,
   their check bodies, the latency verdict and their env keys go, and the five push tokens enter
   `PENDING_RETIREMENT`. It merges only after the read above passes.
3. Cutover-b, the PR that adds this record: the seven `edge.yaml` rules drop `shadow`, and the
   `kuma-cutover` group loses A4's five terms. `test_alertmanager_routing.py` moves the edge
   rules from no expected outcome to `PAGING`: `CertExpiringSoon` is critical (Discord and
   email) and the other six are warnings (Discord).

The plan names no inhibit for A4. `EdgeMetricsMissing`'s second arm requires `up == 1`, so a
failed `traefik-k8s` scrape stays `ScrapeTargetDown`'s page and no pair of A4 rules pages one
fault twice. Dropping `shadow` from the critical `CertExpiringSoon` (`for: 10m`) raises
traefik's soak wait from 300 s to 600 s, which `test_lander_deadlines_cover_the_soak.py` covers.

The two-hour no-pager bound counts from the moment the `uptime-kuma` Kustomization reports
Cutover-a1's merge commit, because Kuma stops paging the five checks then and every A4 rule is
still shadow. It ends when the `observability` Kustomization reports Cutover-b's merge commit.
Cutover-a2's apply falls inside it. `CheckWithoutPager` pages critical two hours after the last
`monitor_status` sample of any of the five tiles. If the bound is exceeded, revert Cutover-a2
first when it has merged, so the bridge pushes again, and then Cutover-a1, so the tiles return.
Reverting Cutover-a1 alone after Cutover-a2 restores tiles that no push reaches, and they go
DOWN. Both reverts worked only until the A4 token PR,
[#6949](https://github.com/DanielH2018/server/pull/6949), retired the keys.

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens, and the operator's commit removed the keys from `secrets.yml` on 2026-11-02. A revert of
a cutover above no longer brings back a working tile, because no token is left for it to push
with.

### A5 storage

A5's soak read, [#6624](https://github.com/DanielH2018/server/issues/6624): A5's shadow PR is
`7d360df099`. Prometheus first evaluated the `storage` group at 2026-10-23T18:48:47Z, so the soak
window starts there. The read used M1 convention 6's two queries with M2's 8-minute margin
(*Which soak minutes count as explained* in M2). It mapped each tile to the `storage.yaml` rules
that port its bridge row, with `LonghornVolumesMissing` counted for both Longhorn tiles. Over
`[168h:1m]`, read at 2026-10-30T18:58Z, query 1 returned 5 minutes for k3s PVC Fullness and
none for the other four tiles, and query 2 returned none for any tile. The 5 minutes are one single-cycle bridge episode on
2026-10-30 at 11:33. At that sample daniel-box's kubelet scrape read `up=1` but carried none of
its 28 claims' volume stats, so the claim count fell from 43 to 15 and valheim-server reported no
free bytes. The bridge pages its free-space floor on one sample, and Kuma held DOWN until the next
push at 11:38. `PVCFreeFloorUnmonitored` (`for: 5m`) and `PVCCoverageShortfall` (`for: 15m`) saw
their conditions hold for 1 minute each, so the episode is one the rules drop by design. The
2026-10-25 DOWN runs from 14:17 to 14:21 and from 14:27 to 14:31 are covered, because
`PVCFreeFloorUnmonitored` fired from 14:20 to 14:28 while daniel-box's kubelet target was down.
`LonghornVolumeDegraded` went pending on many claims during the 2026-10-25 reboot and on
2026-10-28 at 00:54 and 01:29, for at most 4 minutes each. It never fired, and the bridge
recorded no `longhorn_volumes` DOWN. No minute is unexplained, so no rule changes and Cutover-a
is not blocked by the soak.

A5's cutover is three stacked PRs that merge in this order, not the plan's single Cutover-a.
uptime-kuma and monitor-bridge are both `owner: flux`, so no deploy orders a tile deletion
before a row deletion, and the batch uses the owner-independent fallback of *When D5 flips a
role M6 edits*, as A2, A3 and A4 did:

1. Cutover-a1, [#6694](https://github.com/DanielH2018/server/pull/6694): an exclusion in the
   `CHECKS` loop of `static-monitors.yaml.j2` deletes the five tiles. Every rule stays shadow.
   Its live read is `probe.py metric 'monitor_status{monitor_name=~"Longhorn Volume Redundancy|k3s PVC Fullness|Longhorn Snapshot Headroom|Kubelet CSI Mount Read-Only|etcd DB Size"}'`,
   which must return no series.
2. Cutover-a2, [#6695](https://github.com/DanielH2018/server/pull/6695): the five bridge rows,
   `checks/storage.py` and `checks/cluster_etcd.py`, the Longhorn, PVC and snapshot verdicts and
   the env keys go, and the five push tokens enter `PENDING_RETIREMENT`. It merges only after the
   read above passes.
3. Cutover-b, the PR that adds this paragraph: the ten `storage.yaml` rules drop `shadow`, and
   the `kuma-cutover` group loses A5's five terms. `test_alertmanager_routing.py` moves the ten
   rules to `PAGING`, all as critical alerts to Discord and email.

**The re-read of the soak, 2026-10-31.** The read above was repeated at 01:32Z with the same
queries (M1 convention 6 with M2's 8-minute margin, each tile mapped to the `storage.yaml` rules
that port its row, `LonghornVolumesMissing` counted for both Longhorn tiles) over `[168h:1m]`.
Prometheus's oldest sample was 21.1 days old, so retention covered the window, and the window
began after the `storage` group first evaluated. `increase(prometheus_rule_evaluation_failures_total{rule_group=~".*storage"}[168h])`
was 0.

| Tile | Samples | DOWN min | Firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m | Bridge DOWN episodes, 8d |
|---|---|---|---|---|---|---|---|---|
| Longhorn Volume Redundancy | 10042 | 0 | 0 | 0 | 0 | 0 | 0 | none |
| k3s PVC Fullness | 10042 | 15 | 9 | 11 | 5 | 5 | 0 | 3 |
| Longhorn Snapshot Headroom | 10042 | 0 | 0 | 0 | 0 | 0 | 0 | none |
| Kubelet CSI Mount Read-Only | 10042 | 0 | 0 | 0 | 0 | 0 | 0 | none |
| etcd DB Size | 10043 | 0 | 0 | 0 | 0 | 0 | 0 | none |

The 5 Q1 ±8m minutes of `k3s PVC Fullness` are the single-cycle episode of 2026-10-30 at 11:33
that the entry above records. A `[60m:1m]` window ending at 12:00 on that day holds all 5, and
the 5 DOWN minutes in it have no firing alert. `PVCFreeFloorUnmonitored` and
`PVCCoverageShortfall` held their conditions for 1 minute each (`count_over_time` of the rules'
own expressions over `[40m:1m]` ending at 11:50), under their `for:` of 5 and 15 minutes. The
2026-10-25 episodes (DOWN 14:17-21 and 14:27-31) leave 0 Q1 ±8m minutes, because
`PVCFreeFloorUnmonitored` fired from 14:20 to 14:28. The 6 Q1 strict minutes
there fall inside that firing run, and the other 5 are the 11:33 episode. No minute is unexplained.

The 38 minutes each tile lacks (10042 of 10080 samples) are Uptime Kuma restarts, which Q1
cannot see. The bridge's own DOWN episodes cover them: `probe.py alerts --check <name> --days 8`
lists none for `longhorn_volumes`, `snapshot_headroom`, `kubelet_plugin_readonly` and
`etcd_db_size`, and the three for `pvc_fullness` are the ones above plus 2026-10-23 12:41,
which is before the window.

**The deployed rules, read at 01:33Z.** `kubectl get configmap -n observability prometheus-rules -o jsonpath='{.data.storage\.yaml}'`
is byte-identical to `storage.yaml` on master. All 10 rules carry `shadow: "true"`,
`prometheus_rule_group_rules` for the group reads 10, and no A5 alert was firing.

**Inhibit decision: A5 needs none.** The plan assigns A5 no inhibit, and its row says only that
all ten rules are critical and that Longhorn's `state` label is selected explicitly. Each rule's
behaviour on a dead scrape decides whether a double page is possible:

- `LonghornVolumeDegraded`, `LonghornSnapshotNearCap`, `KubeletCSIMountReadOnly`,
  `EtcdDBNearQuota`, `PVCFull` and `PVCBelowFreeFloor` compare series values, so a dead scrape
  leaves them empty and quiet.
- `LonghornVolumesMissing` has `absent(up{job="longhorn"})` for a job that left
  `prometheus.yml`, and requires `up == 1` for the missing-series arm, so a failed scrape stays
  `ScrapeTargetDown`'s page.
- `LonghornSnapshotCapUnmonitored` joins the capacity series to the snapshot series, so a dead
  `longhorn` scrape empties both sides and the rule stays quiet.
- `PVCFreeFloorUnmonitored` and `PVCCoverageShortfall` do fire beside `ScrapeTargetDown` when
  a kubelet target dies, because their conditions are an `absent()` and a `count() or vector(0)`.
  Over the 168 hours, `ScrapeTargetDown` for `kubernetes-kubelet` fired for 3 minutes and
  `PVCFreeFloorUnmonitored` for 9, all in the 2026-10-25 episode. `PVCCoverageShortfall` never
  fired.

An inhibit for those two does not narrow cleanly. Neither rule carries `job` or `instance`
(`absent()` and `count()` drop them), so `equal: [job, instance]` cannot match. Without `equal`,
one dead kubelet target would mute the claim census while a selector break empties the other
node's vector, which is the fault the census exists to catch. The bridge row made the same call
on purpose: it had no `EXPORTER_DEPENDENT` entry for the kubelet job, so that a dead kubelet job
kept paging the census. The result is two critical pages for one dead kubelet target, the
direction the plan accepts until an inhibit lands, and the Kuma side paged twice here too (the
PVC tile and Scrape Targets). `PVCFreeFloorUnmonitored` (`for: 5m`) fires first, and
`ScrapeTargetDown` (`for: 10m`) second.

**Soak effect.** Unlike A2's four warnings, all ten A5 rules are `critical`, and
`renderer_lib/soak.py:longest_for` counts an unshadowed critical rule's `for:` against its
`service` label. Read before and after this PR with `longest_for()`, the map gains `longhorn: 900`
(`LonghornVolumeDegraded`, `LonghornVolumesMissing`, `LonghornSnapshotNearCap` and
`LonghornSnapshotCapUnmonitored` hold 15m) and `etcd: 300`, and no existing key changes. `etcd`
names no tag, so nothing reads it. `longhorn` is a published tag, so a promote scoped to that
tag waits 900s plus the scrape and evaluation margin, up from the 120s floor that rules with a
templated `service` give every tag. A fleet-wide promote already waits on `deploy-pipeline`'s
900s, so its wait does not move. The `Lander deadlines cover the soak` hook and
`test_lander_deadlines_cover_the_soak.py` pass.

The two-hour no-pager bound counts from the moment the `uptime-kuma` Kustomization reports
Cutover-a1's merge commit, because Kuma stops paging the five checks then and every A5 rule is
still shadow. It ends when the `observability` Kustomization reports Cutover-b's merge commit.
Cutover-a2's apply falls inside it. `CheckWithoutPager` pages critical two hours after the last
`monitor_status` sample of any of the five tiles. If the bound is exceeded, revert Cutover-a2
first when it has merged, so the bridge pushes again, and then Cutover-a1, so the tiles return.
Reverting Cutover-a1 alone after Cutover-a2 restores tiles that no push reaches, and they go
DOWN. Both reverts worked only until the A5 token PR,
[#6949](https://github.com/DanielH2018/server/pull/6949), retired the keys.

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens, and the operator's commit removed the keys from `secrets.yml` on 2026-11-02. A revert of
a cutover above no longer brings back a working tile, because no token is left for it to push
with.

### A6 bridge exporter

A6's shadow PRs A6-1 to A6-6 landed on 2026-10-23. A6-7r landed on 2026-10-31 as `0c085f613c`, which ran the three gate rows as ordinary rows and deleted `gates.py`. The cutover is three stacked PRs, which this record describes after the soak reads below: Cutover-1a (A6-7a1, the tiles), Cutover-1b (A6-7b, the rules page) and Cutover-2 (A6-7a2, the push path). A6-8 and A6-9 follow them. The first soak read found a blocker for A6-7a (below), and the operator's 2026-11-02 ruling waives the 7-day read that cleared it.

**The soak read, 2026-10-31T17:54Z.** The read ran M1 convention 6's two queries over `[168h:1m]` ending at that time, with M2's 8-minute margin (*Which soak minutes count as explained* in M2). Each tile maps to the one `bridge-exporter.yaml` rule that selects its row's `check`. `increase(prometheus_rule_evaluation_failures_total{rule_group=~".*bridge-exporter"}[168h])` is 0, `prometheus_rule_group_rules` for the group is 20, and the deployed `prometheus-rules` ConfigMap's `bridge-exporter.yaml` is byte-identical to master. `homelab_check_success` has been exported since before the window starts. All 19 bridge tiles read 1 when the queries ran.

| Tile | Rule | DOWN min | Firing min | Q1 | Q2 |
|---|---|---|---|---|---|
| WAN Reachable | `WANUnreachable` | 0 | 0 | 0 | 0 |
| Discord Delivery | `DiscordDeliveryFailing` | 0 | 0 | 0 | 0 |
| R2 Free Tier Headroom | `R2FreeTierHeadroomLow` | 0 | 0 | 0 | 0 |
| Cloudflare IP Drift | `CloudflareIPRangesDrifted` | 0 | 0 | 0 | 0 |
| Healthchecks.io Console Drift | `HealthchecksConsoleDrifted` | 1042 | 1042 | 0 | 0 |
| B2 Reachable | `B2Unreachable` | 0 | 1 | 0 | 1 |
| B2 Free Tier Headroom | `B2FreeTierHeadroomLow` | 0 | 1 | 0 | 1 |
| Arr Queue Warnings | `ArrQueueNeedsReview` | 1365 | 1372 | 0 | 0 |
| GitOps Deploy — Alive | `GitOpsDeployerNotTicking` | 0 | 0 | 0 | 0 |
| GitOps Deploy — Status | `GitOpsDeployerParked` | 1605 | 1617 | 0 | 0 |
| etcd Restore Drill | `EtcdRestoreDrillFailing` | 0 | 0 | 0 | 0 |
| Home Assistant Automations | `HomeAssistantAutomationsStalled` | 0 | 0 | 0 | 0 |
| n8n Prod Workflows | `N8nWorkflowsFailing` | 0 | 0 | 0 | 0 |
| Bazarr Health | `BazarrHealthDegraded` | 0 | 0 | 0 | 0 |
| Prowlarr Indexers | `ProwlarrIndexersFailing` | 34 | 30 | 5 | 1 |
| SMART Data / Health | `SmartHealthFailing` | 5 | 1 | 5 | 1 |
| k3s Speedtest | `SpeedtestDegraded` | 0 | 0 | 0 | 0 |
| Swallowed Push Verdicts | `SwallowedPushVerdicts` | 0 | 0 | 0 | 0 |
| **Host Packages — Status** | **none** | 364 | 0 | **364** | 0 |
| Prometheus Reachable | none (the Watchdog covers it) | 0 | 0 | 0 | 0 |

**Host Packages — Status has no rule.** `check_table.py` carries a 20th row, `host_packages`, which H3.5c added (`ce390940b4`) after this plan was written. `bridge-exporter.yaml` has 18 row rules and none selects `check="host_packages"`, and this plan never mentions the row. Its tile read DOWN for 364 minutes in the window (a 5.8-hour `no-green` episode on 2026-10-28 and two single-cycle `canary` episodes on 2026-10-29), and no shadow alert covers any of them. The row is also still a member of `PROM_DEPENDENT` and of `EXPORTER_DEPENDENT["node"]`, which A6-7r's text says A1 to A5 and B8 emptied. A6-7a's bijection test fails on it, and A6-9's count of 19 tokens is 20.

**The 10 minutes of Prowlarr Indexers and SMART Data / Health query 1 are one event, 2026-10-28 00:53 to 00:57.** Prometheus recorded no sample of any target from 00:39:49 to 00:52:42, a 13-minute gap in `up{job="monitor-bridge"}`. The bridge log shows the first of the row's two down cycles ("down streak 1/2") fell inside that gap, and the second cycle ("2 cycles") was the only one Prometheus saw. Kuma paged the row for 5 minutes. The rule's `for: 5m` saw one cycle (the gauge held 0 for five samples), went pending at 00:55 and cleared at 00:58 without firing. The Watchdog covers a Prometheus outage, so the alerting plane was blind rather than wrong.

**The `for: 5m` margin is thin for a one-cycle blip.** A `startup_grace` row's gauge holds 0 for one 300s cycle, which is five or six 1-minute samples. At 2026-10-28 01:29 to 01:34 six samples satisfied `for: 5m`, and `SmartHealthFailing` and `ProwlarrIndexersFailing` fired for 1 minute at 01:35 while Kuma read up (the bridge's grace held that cycle, and Kuma was refusing connections from 01:27 to 01:30). At 00:53 five samples did not satisfy it. Whether a single blip fires is therefore a matter of scrape alignment. A6-7b should decide whether the family's `for:` is 5m or longer before it drops `shadow`.

**The other query 2 minutes** (`B2Unreachable` and `B2FreeTierHeadroomLow`, one minute each at 2026-10-25 07:46) follow the weekly reboot. Kuma's tiles have no series from 07:39 to 07:47, and the bridge's WAN probe logged `Try again` at 07:40, so the gate rows exported 0 for three samples.

**The operator's rulings, 2026-10-31.** Both follow from the read above.

- `host_packages` gets a shadow rule, `HostPackageBuilderStalled`, and its 7-day soak starts when that rule's `observability` apply lands. A6-7r keeps the row's `prometheus` gate and its `node` exporter suppression, and A6-7a waits for the soak.
- The `startup_grace` family's rules move from `for: 5m` to `for: 10m`, two bridge cycles, while still shadow, so the soak measures the final rule. The 168-hour read above measured the `for: 5m` rules, and the family's 7-day soak restarts at the same apply.

**Status at the 2026-10-31 read.** Query 1 had 364 minutes with no covering rule (Host Packages) and 10 minutes explained by a Prometheus outage. A6-7b was not to proceed until `HostPackageBuilderStalled` and the `for: 10m` family had a clean 7-day read from the apply that shipped them.

**The operator's ruling, 2026-11-02.** Operator ruling 2026-11-02: cut over on a 48h soak read. The ruling waives the 7-day read above, and the read below stands in for it. The cutover PRs each say so in their bodies.

**The 48-hour read, 2026-10-31T19:22Z to 2026-11-02T19:25Z.** The verdict is CLEAN-WITH-NOTES. It used M1 convention 6's two queries with M2's 8-minute margin, over the window that begins at the apply that shipped `HostPackageBuilderStalled` and the `for: 10m` family.

- Query 1 misses: 0 for every rule.
- One true positive: `GitOpsDeployerParked` fired for 157 minutes on 2026-11-01 and matches its tile.
- Three 2-minute false positives at the weekly reboot on Sunday 2026-11-01, 07:43 to 07:44Z: `B2Unreachable`, `B2FreeTierHeadroomLow` and `HostPackageBuilderStalled`. The two B2 rules also read one minute at the weekly reboot in the 2026-10-31 read above.
- 19 of 20 tiles map one to one to a rule. `Prometheus Reachable` has no rule by design: Cutover-2 retires that gate, and the Watchdog covers a dead Prometheus.
- `prometheus_rule_group_rules{rule_group=~".*bridge-exporter"}` reads 21, which is the 20 rules of the first read plus `HostPackageBuilderStalled`. The group had 0 rule evaluation failures over the window.

**Required change folded in: `for: 10m` on the three reboot rules.** The three false positives are 2-minute dips, so Cutover-1b gives `B2Unreachable`, `B2FreeTierHeadroomLow` and `HostPackageBuilderStalled` a `for: 10m`. That matches the five `startup_grace` rows that carry `for: 10m` from the 2026-10-31 ruling. It lands in the PR that drops their `shadow`, so none of them pages unshadowed without it. The promtool cases hold a 2-minute dip quiet and fire on a 10-minute outage.

## The cutover, three stacked PRs

The three PRs each change one pager side only, and they merge in this order. The rules page comes before the push stop, as in the B2, B4, B6b and B6c stacks, because the push stop removes nothing a pager reads. Every PR uses `Refs` only and fills the repo's PR template.

1. **Cutover-1a, the tiles go** (branch `m6-a6-cutover-1a-tiles-go`, applied by `uptime-kuma`'s Flux promotion). The `CHECKS` loop of `static-monitors.yaml.j2` excludes all 20 rows, so Uptime Kuma declares no monitor-bridge tile. The 19 tiles that have a rule and `Prometheus Reachable` all go. `status-page-sync-configmap.yaml.j2` stops pinning bridge tiles into groups, and `uptime_kuma_k8s_status_page_groups` loses the four groups only bridge tiles filled (`Services`, `Observability`, `Backups & Storage` and `Media Automation`). Every `bridge-exporter.yaml` rule stays `shadow`, so this PR adds no pager and removes 20. The bridge keeps pushing into the deleted tokens until Cutover-2, and a push into a deleted token returns 404, which the bridge logs as `push failed`.
2. **Cutover-1b, the rules page** (branch `m6-a6-cutover-1b-rules-page`, applied by `observability`'s Flux promotion). Every `bridge-exporter.yaml` rule drops `shadow`, the three reboot rules gain `for: 10m`, and the `kuma-cutover` group loses A6's 19 terms.
3. **Cutover-2, the push path goes** (branch `m6-a6-cutover-2-push-stops`, applied by `monitor-bridge`'s Flux promotion). The bridge stops pushing, the `prometheus` row goes, and the 20 push tokens enter `PENDING_RETIREMENT`.

**The two-hour no-pager bound.** The tiles stop paging when the `uptime-kuma` Kustomization reports Cutover-1a's merge commit, and that apply opens the window. The `observability` Kustomization reporting Cutover-1b's merge commit closes it. `CheckWithoutPager` pages critical two hours after a tile's last `monitor_status` sample, so Cutover-1b has to apply within two hours of Cutover-1a's apply. Cutover-2's `monitor-bridge` apply falls outside the window. If Cutover-1b has not applied within two hours, revert Cutover-1a: every rule is still `shadow`, so the revert restores Kuma as the only pager, and the tiles go green on the next push because the 20 tokens stay in `secrets.yml` until A6-9.

**What the tiles' removal moved in the tests (Cutover-1a).** Measured on master: 27 monitors, 27 push tiles and 31 declarations. On the branch: 7 of each monitor and push tile, and 11 declarations. The floors that counted the tiles fall to one below the new count: the status-page census (`>= 27` to `>= 6`), the declaration index (`> 29` to `> 9`), the renderer's index (`> 24` to `> 6`), and the push-tile census in `test_kuma_push_token_format.py` and `test_kuma_push_resend_beats.py` (`>= 19` to `>= 6`). `Swallowed Push Verdicts` was the non-vacuity anchor of five tests, and `Status Page Sync` replaces it. `KNOWN_IDS` drops the two bridge tiles it named. `EMAIL_TIER` is empty, because the six tiles that mailed were bridge tiles. `test_bridge_push_monitors_share_one_interval` and `test_non_bridge_push_monitors_keep_their_own_interval` are deleted: with no bridge tile declared, the first asserts over an empty set and the second needs 20 tiles to move with the variable. `uptime_kuma_k8s_bridge_push_interval` stays, with no tile reading it, until C-1 deletes the role. `test_the_deleted_a6_tiles_are_not_declared` fails if a template declares any of the 20 tiles again, and `test_no_tile_carries_a_monitor_bridge_push_token` fails if any tile accepts one of the bridge's 20 tokens again.

**Cutover-1b in the tree.** All 21 `bridge-exporter.yaml` rules drop `shadow`: the 19 row rules, `MonitorBridgeDown` and `BridgeCheckStale`. The `kuma-cutover` group loses A6's 19 `CheckWithoutPager` terms (`n8n Prod Workflows`, `Bazarr Health`, `Prowlarr Indexers`, `SMART Data / Health`, `k3s Speedtest`, `Swallowed Push Verdicts`, `WAN Reachable`, `Discord Delivery`, `R2 Free Tier Headroom`, `Cloudflare IP Drift`, `Healthchecks.io Console Drift`, `B2 Reachable`, `B2 Free Tier Headroom`, `Arr Queue Warnings`, `GitOps Deploy — Alive`, `GitOps Deploy — Status`, `etcd Restore Drill`, `Home Assistant Automations` and `Host Packages — Status`) and keeps B9's six. `Prometheus Reachable` had no term. `test_alertmanager_routing.py` moves the 21 rules into `PAGING`, and `test_alertmanager_routing_a6_bridge.py` holds the non-vacuity cases: eight rules are `critical` and mail as well as post, the other 13 post to Discord alone, and a rule that regains `shadow` walks to the blackhole.

**The `for: 10m` on the three reboot rules.** `B2Unreachable`, `B2FreeTierHeadroomLow` and `HostPackageBuilderStalled` gain `for: 10m`. `test_bridge_grace_rules.py` names the three rows in `REBOOT_ROWS` and derives every other rule's `for:` from the `gate` column as before. `test_bridge_b2_rules.py` requires the B2 gate and its dependent to carry the same `for:`, because a dependent with a shorter one pages before its inhibit source exists. The promtool cases hold a 2-minute dip quiet, hold a 9-minute outage quiet, and fire both B2 rules and the host-packages rule at the 10-minute evaluation. Every `exp_labels` block lost its `shadow: "true"`.

**Soak effect.** `renderer_lib/soak.py:longest_for` counts critical rules that are not `shadow`. `B2Unreachable` and `B2FreeTierHeadroomLow` are `critical`, not `warning`, so the `for: 10m` is not free. Printing `longest_for()` on Cutover-1a's tree and on Cutover-1b's tree gives these differences, all new keys: `b2` 600, `monitor-bridge` 600, `scrutiny` 600, and `discord`, `r2` and `wan` 0. `b2`, `discord`, `r2` and `wan` name no deploy tag, so they lengthen no promotion's soak. `monitor-bridge` and `scrutiny` are deploy tags. Their 600 seconds come from `MonitorBridgeDown`, `BridgeCheckStale` and `SmartHealthFailing`, which already carried `for: 10m` as shadow rules and count once they drop `shadow`. `HostPackageBuilderStalled` is `warning` and adds nothing. `test_lander_deadlines_cover_the_soak.py` passes on this tree.

**Cutover-2 in the tree.** The bridge stops pushing, and the 20 `monitor_bridge_*_push_token` keys enter `PENDING_RETIREMENT` in `scripts/secrets_mgmt/pending_retirement.py` (they leave `standalone_secret_keys.json`). The `prometheus` gate row and `checks/cluster.py` are deleted. `host_packages` keeps `gate="prometheus"` as the label of the `EXPORTER_DEPENDENT` suppression, and `check.run_once` now runs the exporter-health probe once per cycle, before the first row, when an enabled row sits under that map. `run_once(cfg, src, checks, only, export)` loses its `sink` and `dry_run` arguments, `Sink` and `bridge.net.push` are deleted, and so are `Check.token`, `apply_startup_grace`, `GRACE_CYCLES`, `KUMA_URL` and the `KUMA_PUSH_*` env keys. The `startup_grace` value stays in the `gate` column and now means the row's rule carries `for: 10m`. A gate row that reads down exports 0 for its dependents with a message that names the alert (`GATE_SKIP`). `static-monitors.yaml.j2` loses the `CHECKS` loop, and `check_table.py` keeps the retired `token`, `kuma_id`, `display`, `description` and `status_group` fields until A6-8.

**What Cutover-2 moved in the tests.** `test_check.py`, `test_check_cli.py`, `test_check_exporters.py`, `test_verdict_export.py` and `_fake_sources.py` drop the sink and the push assertions and read the exporter's records instead (`FakeExport.records`). `test_check_streaks.py` loses the grace tests and keeps `down_streak`. `test_check_table.py` pins that the env Secret renders no `KUMA_*` key and no `GRACE_CYCLES`, and requires the table to keep at least 19 rows. `secret_checks.py` drops the rule-5 named member that counted `lookup('vars', check.token)` in the env Secret, because no such use remains. `test_k8s_lookup_readers.py` and `test_secret_consumer_census.py` state the cross-role lookup mechanism with the crowdsec allowlist and a fixture tree, since uptime-kuma no longer reads the check table. `test_census_rows_roles.py` deletes the `kuma-callers-read-the-entry-port` row, whose last real subject was the bridge's `KUMA_URL`, and `test_scrape_jobs_derive_from_containers_list.py` drops the bridge from its Kuma-caller cases. `test_secret_consumers.py` expects the 20 tokens to resolve no consumer.

**Left after A6-8.** The `netpol_from` entry `monitor-bridge` on uptime-kuma's `containers_list` entry in `daniel-box.yml` allows an ingress nothing dials now. The change is an inventory edit, which the deployer classes as broad, so it does not ride a Flux-promoted PR. `uptime_kuma_k8s_bridge_push_interval` has no template reader and waits for C-1, which deletes the role. The `py_table` filter has no template caller left either, only the docs fragments and the check-table test, so a later PR decides whether it stays.

**The pending-retirement module came back for A6, and A6-9 deleted it.** B6c's token PR (`057aa76f95`) deleted `scripts/secrets_mgmt/pending_retirement.py` with its readers, and Cutover-2 restored them for the bridge's 20 tokens. A6-9 applies the same deletion again: the module, its readers in `consumers.py`, `cluster_vars.py` and `cluster_vars_check.py`, its `HOST_INPUTS` entry and the keep-on-write tests.

**Revert order.** Revert Cutover-2 first, then Cutover-1b, then Cutover-1a. Reverting Cutover-2 alone makes the bridge push again, into tiles that no longer exist, so it logs `push failed` and pages nothing.

**A6-8 and A6-9, the code half.** The operator waived the one-week revert window for the token retirements on 2026-11-02, so this PR carries both. A6-8 deletes the `token`, `kuma_id`, `display`, `description` and `status_group` fields from `PushCheck` and from all 19 rows of `check_table.py`, with the `DECIDED` marker on spelled-out tokens. The rules in `bridge-exporter.yaml` already carry their own `description` annotation (21 of 21), so no description moves. `fragments_bridge.py` renders the `bridge-checks` table as check, gate, critical and runbook, and the gate-set rows name the gate by its row name. The `monitor-bridge-checks.md` bullets are titled by check name. `test_bridge_size_cap.py` holds the bridge's `files/` at 8,000 lines: the residual measured 7,836 lines over 39 modules with `deploy_pipeline.py` included, and the plan rounds up to the next 500. A6-9 adds the 20 `monitor_bridge_*_push_token` names to `git_dates.RETIRED`, deletes `PENDING_RETIREMENT` and its readers, and removes the `PREFIX_EXCEPTION_CONSUMERS` entry for `monitor_bridge_cloudflare_drift_push_token`, which was its only member.

**The operator's commit for A6-9.** The 20 keys are in `ansible/vars/secrets.yml`, `ansible/secret_rotation.yml` and the cluster file (`cluster-vars.source` and `cluster-vars.sops.env`). None is in a host store (`ansible/vars/hosts/*.sops.yaml`). The commit removes them from the store and the registry, regenerates the cluster files with `cluster_vars.py write`, and regenerates the doc fragments (`secret-tiers` and the three `bridge-*` fragments). The checks that stay red until then: `test_no_listed_departure_is_back_in_the_store`, `test_the_committed_cluster_files_pass_check`, `test_uptime_kuma_is_a_consumer_iff_a_tile_exists` and `test_every_committed_fragment_matches_what_the_generator_writes_now`.

### A7 log arm

A7's soak read, [#6721](https://github.com/DanielH2018/server/issues/6721): A7's shadow PR is
`c3d768d3f8`, which added `WorkloadLogErrors` to `homelab-logs.yaml`. The Loki ruler's
`homelab-logs` group predates the rule, so the group's first evaluation does not date the
rule. `loki_prometheus_rule_group_rules` for the group went from 4 to 5 at 2026-10-23T20:07Z,
which is when the ruler first evaluated `WorkloadLogErrors`. The `Workload Log Errors` tile's
first `monitor_status` sample is 2026-10-22T00:56Z, before the rule. The `[168h:1m]` window,
read at 2026-10-31T15:43Z, therefore starts at 2026-10-24T15:44Z, after both, and runs the full
168 hours.

The read used M1 convention 6's two queries with M2's 8-minute margin (*Which soak minutes
count as explained* in M2). The tile maps to one rule, `WorkloadLogErrors`, the rule the shadow
PR's comment names as the port of the row's log-pattern arm. The rule is a Loki rule, so its
firing history is not in Prometheus `ALERTS`. The read follows M5's method instead and
evaluates the rule's expression as a Loki metric query with `probe.py loki-query --json`, in
eight one-day chunks of `--step 1m`. Loki reported an `execTime` of 6.3 to 7.9 seconds per
chunk. A minute counts as
firing when the expression held for that minute and the 5 minutes before it, which is the
rule's `for: 5m`. The ruler evaluates every 5 minutes at its own offset, so a live alert can
start and end under 5 minutes later than the reconstruction, which is inside the margin.

Prometheus's oldest sample was 21.7 days old, so retention covered the window.
`increase(loki_prometheus_rule_evaluation_failures_total[168h])` for the group was 0. The
deployed `loki-homelab-rules` ConfigMap's `homelab-logs.yaml` is byte-identical to master.

| Tile | Samples | DOWN min | Firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m | Bridge DOWN episodes, 8d |
|---|---|---|---|---|---|---|---|---|
| Workload Log Errors | 10031 | 86 | 84 | 6 | 0 | 4 | 0 | 2 |

The window holds two episodes, and the tile and the rule agree on both:

- **karakeep, 2026-10-26.** The tile read DOWN from 13:25 to 13:54. The rule's expression
  held from 13:17 to 13:50 at 22 to 24 lines per hour, so the reconstructed alert fired from
  13:22 to 13:50. `probe.py alerts --check workload_log_errors --days 8` lists the bridge
  episode as 13:23 to 13:49, "fatal log lines in 1h: karakeep (24)".
- **bazarr, 2026-10-30.** The tile read DOWN from 01:10 to 02:05. The expression held from
  01:04 to 02:03 at 24 and then 40 lines per hour, so the alert fired from 01:09 to 02:03. The
  bridge episode is 01:09 to 02:00, "fatal log lines in 1h: bazarr (40)".

The 6 strict Q1 minutes are the tails of the two episodes: 13:51 to 13:54 on 10-26 and 02:04 to
02:05 on 10-30. The bridge pushes once per 300-second cycle, so the tile holds its last DOWN
push until the next cycle reports the hour clean. The 4 strict Q2 minutes are the heads: 13:22
to 13:24 on 10-26 and 01:09 on 10-30. The rule's one-minute reconstruction crosses the
threshold before the bridge's next cycle pushes DOWN, and Kuma's scrape adds up to a minute. The
margin absorbs all 10 minutes, so no minute is unexplained.

The tile lacks 49 of 10080 samples, in 12 runs of 1 to 8 minutes. Uptime Kuma's
`process_start_time_seconds` changed 11 times in the window, and `up{job="uptime-kuma"}` read 0
for 3 minutes, so the gaps are Kuma restarts and scrape misses. Q1 cannot see them. No run falls
within 8 minutes of either episode. The closest, 2026-10-29 21:33 to 21:37, ends more than 3
hours before the bazarr episode. The bridge's own DOWN episodes cover the gaps, and the two episodes above are
the only ones `probe.py alerts` lists for `workload_log_errors` over 8 days.

No minute is unexplained, so no rule changes, and the soak does not block Cutover-a. Two of M5's
costs still apply to Cutover-b: the alert never reaches Prometheus `ALERTS`, so the health gate
cannot see it, and every rule edit restarts Loki.

A7's cutover is three stacked PRs that merge in this order, not the plan's single Cutover-a.
uptime-kuma, monitor-bridge and loki-homelab are all `owner: flux`, so no deploy orders a tile
deletion before a row deletion, and the batch uses the owner-independent fallback of *When D5
flips a role M6 edits*, as A1 to A5 did:

1. Cutover-a1, [#6748](https://github.com/DanielH2018/server/pull/6748): an exclusion in the
   `CHECKS` loop of `static-monitors.yaml.j2` deletes the `Workload Log Errors` tile. The rule
   stays shadow. Its live read is
   `probe.py metric 'monitor_status{monitor_name="Workload Log Errors"}'`, which must return no
   series.
2. Cutover-a2, [#6755](https://github.com/DanielH2018/server/pull/6755): the
   `workload_log_errors` row, `check_workload_log_errors`, `with_log_errors`,
   `verdicts/cluster.py`, `log_error_counts` and the five `LOG_ERROR_*` keys go, and the push
   token enters `PENDING_RETIREMENT`. It merges only after the read above passes.
3. Cutover-b, the PR that adds this paragraph: `WorkloadLogErrors` drops `shadow`, and the
   `kuma-cutover` group loses A7's one term. `test_alertmanager_routing.py` moves the rule to
   `PAGING` as a warning, which posts to Discord and does not mail.

**Cutover-b applies two roles, not `loki-homelab` alone.** The plan says `land.sh` deploys
`loki-homelab` alone. `CheckWithoutPager` holds an `absent_over_time` term for the tile, and
Prometheus cannot read a Loki rule's `shadow` label, so the term cannot expire by itself. If
Cutover-b left it, the term would page critical two hours after Cutover-a1's apply, beside a
rule that pages now. Cutover-b therefore edits `observability` (the term and its promtool cases)
as well as `loki-homelab`, and both are flux-owned, so the merge is the apply for both.

**The re-read of the soak, 2026-10-31.** The read above was repeated at 16:08Z with the same
method (M1 convention 6 with M2's 8-minute margin; the tile from `probe.py metric
'monitor_status{monitor_name="Workload Log Errors"}[168h:1m]'`, the rule's expression from eight
one-day `probe.py loki-query --json --step 1m` chunks of 6.0 to 7.3 seconds each, a minute firing
when the expression held for it and the 5 minutes before it). The window began 2026-10-24
16:09Z, after the group first evaluated the rule. The counts equal the entry above: 10031 tile
samples, 86 DOWN minutes, 84 firing minutes, Q1 6 strict and 0 within 8 minutes, Q2 4 strict and
0 within 8 minutes. The two episodes are the same, karakeep on 2026-10-26 and bazarr on
2026-10-30, and `probe.py alerts --check workload_log_errors --days 8` still lists only those.
Prometheus's oldest sample was 21.7 days old, and
`increase(loki_prometheus_rule_evaluation_failures_total[168h])` was 0.

**The deployed rule, read at 16:10Z.** `kubectl get configmap -n homelab loki-homelab-rules -o
jsonpath='{.data.homelab-logs\.yaml}' | sha256sum` equals the hash of `homelab-logs.yaml` on
master. `WorkloadLogErrors` carries `shadow: "true"`, and `loki_prometheus_rule_group_rules` for
`homelab-logs` reads 9.

**Inhibit decision: A7 needs none.** The plan assigns A7 no inhibit. A Loki outage leaves the
rule quiet, because `count_over_time` over no stream is an empty vector, and
`LokiPodLogsSilent` and `LokiHomelabMetricsMissing` page that outage on their own. A container
that crash-loops while logging panic lines can fire `K8sContainerCrashLooping` and
`WorkloadLogErrors` for one fault. The direction is accepted, as the Kuma side also paged twice
there (Workload Health and Workload Log Errors).

**Soak effect: none.** The rule is a warning in a Loki rule file, and
`renderer_lib/soak.py:longest_for` reads only the observability rule directory and counts only
critical or `verify` alerts. `longest_for()` on #6755's tree and on this tree is equal over its
19 keys.

**How the paging is verified.** Prometheus's `ALERTS` never holds this alert, so `probe.py health
loki-homelab` and `probe.py monitors` cannot see it, and no `probe.py` subcommand reads
Alertmanager. After the `loki-homelab` Kustomization reports Cutover-b's commit and the pod is
Ready, read these in order:

1. `kubectl get configmap -n homelab loki-homelab-rules -o jsonpath='{.data.homelab-logs\.yaml}' | sha256sum`
   equals the hash of `homelab-logs.yaml` on master, and the output has no `shadow` under
   `WorkloadLogErrors`.
2. `probe.py metric 'loki_prometheus_rule_group_rules{rule_group=~".*homelab-logs"}'` still reads
   9, and `probe.py metric 'increase(loki_prometheus_rule_evaluation_failures_total[30m])'`
   reads 0 once the group has evaluated.
3. `probe.py metric 'loki_prometheus_rule_group_last_evaluation_timestamp_seconds'` is within 5
   minutes of now, which shows the new pod is evaluating.
4. `probe.py metric 'increase(loki_prometheus_notifications_errors_total[1h])'` and
   `loki_prometheus_notifications_dropped_total` read 0.

These show that the rule is loaded, unshadowed and evaluating, and that the ruler can send. They
do not show a page, because Alertmanager's `/api/v2/alerts` lists the alert only while it fires,
and `alertmanager_alerts{state="active"}` counts it only then. The routing from labels to Discord
is `test_alertmanager_routing.py`'s proof (it fails when `shadow` returns), and the first real
burst is the end-to-end one. No synthetic burst is run in production. If a burst happens
during the window, `probe.py metric 'alertmanager_alerts{state="active"}'` rises by one and the
Discord channel carries `WorkloadLogErrors`.

**Loki's restart.** Every edit to `homelab-logs.yaml` rolls Loki alone (the rules ConfigMap
moves the Deployment's restart checksum, and Alloy does not mount it). The Deployment is
`Recreate`. `up{job="loki-homelab"}[10d:1m]` shows a single missing or zero sample at
2026-10-23 20:06, 2026-10-24 03:17 and 2026-10-25 18:07. Each follows the merge of a rules edit
(20:00, 03:10 and 17:53) by 6 to 14 minutes, which is Flux's promotion delay, so the restart
costs about one minute of scrape at 1-minute resolution. The ruler's next evaluation follows
within its 5-minute interval.
The ruler holds a pending alert in memory, so a restart during a burst can
re-arm the 5-minute `for:`; this is expected from the design and was not measured.

**The two-hour bound.** The check has no live pager from the moment the `uptime-kuma`
Kustomization reports #6748's merge commit. `CheckWithoutPager` pages critical 2 hours after the
tile's last sample, so Cutover-b must be applied, by the `loki-homelab` and `observability`
Kustomizations both reporting its commit, within 2 hours of that moment. #6755's apply falls
inside the window.

**If Cutover-b has not been applied within two hours, revert in reverse order.** Revert
Cutover-b alone to restore `shadow` and the `CheckWithoutPager` term. Then revert #6755 so the
row pushes again, and then #6748 so the tile returns. Reverting #6748 alone after #6755 restores
a tile that no push reaches, and it goes DOWN. The reverts worked only until A7's token PR
([#6949](https://github.com/DanielH2018/server/pull/6949)) retired `monitor_bridge_k8s_workloads_push_token`.

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens, and the operator's commit removed the keys from `secrets.yml` on 2026-11-02. A revert of
a cutover above no longer brings back a working tile, because no token is left for it to push
with.

### B0 shared library

B0, the shared host-check library: [#6022](https://github.com/DanielH2018/server/pull/6022). It
merged on 2026-10-23 at `ea83541c9`. `hosts/components/check-lib/files/homelab-check-lib.sh`
carries `boot_grace_active`, `wan_reachable`, `reachout_verdict` and `write_check_textfile`, M3's
atomic textfile write. The library moved from `initial_setup` into its own host component when
H7.55b deleted the setup roles.

### B1 k3s crons

B1's soak read, [#6722](https://github.com/DanielH2018/server/issues/6722): B1's shadow PR
merged as `b1adf776c1` at 2026-10-23T19:48Z. The `host-checks` group predates B1, so its first
evaluation does not mark the start of B1's rules. `prometheus_rule_group_rules` for the group
rose in one step by at least 11 rules, B1's eleven, to 30 or more at 2026-10-23T19:54Z, so B1's
rules first evaluated then. The read
used M1 convention 6's two queries over `[168h:1m]` ending at 2026-10-31T15:44Z, with M2's
8-minute margin (*Which soak minutes count as explained* in M2). The window starts at
2026-10-24T15:44Z, after B1's first evaluation, so every tile has the full 168 hours.
Prometheus's oldest sample was 21.7 days old, and
`increase(prometheus_rule_evaluation_failures_total{rule_group=~".*;host-checks"}[168h])` was 0.

Each tile maps to the `Failed` and `NotReporting` pair that the shadow PR's rule comment in
`host-checks.yaml` assigns to its producer, by the `check` label the producer writes:

- `k3s Longhorn Backup`: `LonghornBackupHealthFailed`, `LonghornBackupHealthNotReporting`.
- `Off-box etcd Snapshot`: `EtcdSnapshotOffboxFailed`, `EtcdSnapshotOffboxNotReporting`.
- `Manifest Prune Drift`: `ManifestPruneCheckFailed`, `ManifestPruneCheckNotReporting`.
- `Release Staleness Drift`: `ReleaseStalenessCheckFailed`,
  `ReleaseStalenessCheckNotReporting`.
- `Live Object Drift`: `LiveDriftCheckFailed`, `LiveDriftCheckNotReporting`.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| k3s Longhorn Backup | 10010 | 7 | 7 | 0 | 0 | 0 | 0 |
| Off-box etcd Snapshot | 7940 | 0 | 0 | 0 | 0 | 0 | 0 |
| Manifest Prune Drift | 8577 | 1136 | 1189 | 1 | 0 | 54 | 27 |
| Release Staleness Drift | 9851 | 30 | 30 | 0 | 0 | 0 | 0 |
| Live Object Drift | 7792 | 598 | 648 | 1 | 0 | 51 | 27 |

Q1 ±8m is 0 for all five tiles. Every firing minute belongs to a `Failed` rule. No
`NotReporting` rule fired, so every producer wrote its textfile within its deadline all week.
The episodes, all times UTC:

- **k3s Longhorn Backup, 2026-10-25 14:24 to 14:30.** `longhorn-backup-health` logged
  `status=down backup target list fetch failed (rc=124): kubectl timed out after 30s` at 14:23.
  The tile and `LonghornBackupHealthFailed` cover the same 7 minutes.
- **Release Staleness Drift, 2026-10-25 19:01 to 19:30.** `release-staleness-check` logged
  `git fetch origin master failed … cannot lock ref 'refs/remotes/origin/master'` at 19:00. The
  tile and `ReleaseStalenessCheckFailed` cover the same 30 minutes.
- **Manifest Prune Drift, 2026-10-25 05:16 to 18:10 and 2026-10-29 05:16 to 12:16.** Both
  episodes open with `status=down deployed code differs from the repo` for the
  `/opt/live-drift-check` and `/opt/longhorn-backup-health` host libraries.
  `ManifestPruneCheckFailed` fired from 05:17 on 2026-10-25, one minute after the tile went
  DOWN, so 1 Q1 strict minute falls inside the margin. On 2026-10-29 the alert and the tile
  start in the same minute.
- **Live Object Drift, 2026-10-25 05:46 to 16:40.** `live-drift-check` logged `status=down 1
  with no apply baseline` for `configmap homelab/registry-gc-ghcr-mirror-keep` at 05:45.
  `LiveDriftCheckFailed` fired from 05:47, one minute after the tile went DOWN, so 1 Q1 strict
  minute falls inside the margin.

All 54 Q2 ±8m minutes, 27 for each drift tile, fall where the tile had no `monitor_status`
series at all. Q2 strict and Q2 ±8m count those minutes, because a missing series is not DOWN.
A `[168h:1m]` query split each tile's Q2 ±8m minutes by tile state: 0 while the tile read up,
and 27 while the tile was absent. The minutes run from 07:47 to 13:32 on 2026-10-25, inside two
Uptime Kuma restarts. Kuma's `process_start_time_seconds` moved to 07:41:23 and to 13:14:04:

- At 07:44, `count(monitor_status)` fell from 91 to 16. Prometheus itself had no samples from
  07:40 to 07:42, and the tiles of the weekly reboot window read maintenance (3) up to 07:39.
  The two drift tiles returned at 08:11, still DOWN.
- At 13:15, `count(monitor_status)` fell from 84 to 6, while `up{job="uptime-kuma"}` read 1.
  The two drift tiles returned at 13:41, still DOWN.

A push tile has no series after a Kuma restart until its producer's next push. Both tiles read
DOWN on either side of each gap, and the drift that the rule reported continued through it. Q2
counts these minutes as a shadow alert firing while Kuma read up, but Kuma read nothing. The
rule was right, and the tile was blind. The same restarts explain the samples each tile lacks
from 10080. The daily tiles lack the most, because each waits up to a day for its next push.

**`KumaDown` has no Kuma tile, so it was read against its own input.** It replaces the
Healthchecks.io `uptime-kuma-alive` ping, not a tile. Over the same window, `KumaDown` never
went firing. `up{job="uptime-kuma"} == 0` held for 2 minutes on 2026-10-28 at 01:28 and for 1
minute on 2026-10-29 at 21:33, both under the rule's `for: 5m`. `absent(up{job="uptime-kuma"})`
read true for 4 minutes on 2026-10-25 from 07:40 and for 8 minutes on 2026-10-28 from 00:45.
`absent(up{job="prometheus"})` read true for 3 of the first and 7 of the second, so Prometheus
was not running then and evaluated nothing. The minute left over in each span is under `for: 5m`.

**The deployed rules, read at 15:47Z.** `kubectl get configmap -n observability
prometheus-rules -o jsonpath='{.data.host-checks\.yaml}'` is byte-identical to `host-checks.yaml`
on master. All eleven B1 rules carry `shadow: "true"`, and no B1 alert was pending or firing.

No minute is unexplained, so no rule changes. The soak does not block Cutover-a, which B1 runs
as Cutover-1a (*Every other producer* in M6).

**The soak re-read, 2026-10-31T19:02Z.** The read of the cutover PRs repeated the queries above
over `[168h:1m]` ending at 19:02Z (window start 2026-10-24T19:03Z, after B1's first evaluation),
with the same tile-to-rule mapping and M2's 8-minute margin. Prometheus's oldest sample was 19.5
days old, and `increase(prometheus_rule_evaluation_failures_total{rule_group=~".*;host-checks"}[168h])`
was 0. Q1 ±8m is 0 for all five tiles, and Q1 strict and Q2 strict equal the 15:44Z read, so no
new minute exists:

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| k3s Longhorn Backup | 10006 | 7 | 7 | 0 | 0 | 0 | 0 |
| Off-box etcd Snapshot | 7743 | 0 | 0 | 0 | 0 | 0 | 0 |
| Manifest Prune Drift | 8380 | 1136 | 1189 | 1 | 0 | 54 | 25 |
| Release Staleness Drift | 9837 | 30 | 30 | 0 | 0 | 0 | 0 |
| Live Object Drift | 7595 | 598 | 648 | 1 | 0 | 51 | 25 |

The two drift tiles' Q2 ±8m minutes (25 each, against 27 in the earlier read, because the
window now starts 3 hours later) are all minutes with no tile series, 07:48 to 13:32 on
2026-10-25 inside the two Kuma restarts above. The three daily tiles have no sample after 12:36Z
today, and Kuma's `process_start_time_seconds` is 17:16Z, the same restart gap: a push tile has
no series until its producer's next push. The deployed `host-checks.yaml` was byte-identical to
master at 19:02Z, with no B1 alert pending or firing and every B1 rule `shadow: "true"`.

B1's cutover is three stacked PRs that merge in this order (*Every other producer* in M6):

1. Cutover-1a, the PR that adds this paragraph: the five tiles (`longhorn-backup`,
   `etcd-snapshot-offbox`, `manifest-prune-check`, `release-staleness-check` and
   `live-drift-check`) leave `static-monitors.yaml.j2`, and their status-page patterns leave the
   `uptime-kuma` defaults. Every rule stays shadow. The five producers keep pushing into deleted
   tokens until Cutover-2, and `KUMA_RETIRING_TAGS` already lists all five tags
   (`longhorn-backup-health`, `etcd-snapshot-offbox`, `manifest-prune-check`,
   `release-staleness-check`, `live-drift-check`), so their refused pushes do not page. Its live
   read is `probe.py metric 'monitor_status{monitor_name=~"k3s Longhorn Backup|Off-box etcd Snapshot|Manifest Prune Drift|Release Staleness Drift|Live Object Drift"}'`,
   which must return no series. The five push tokens (`longhorn_backup_push_token`,
   `etcd_snapshot_push_token`, `manifest_prune_push_token`, `release_staleness_push_token` and
   `live_drift_push_token`) enter `PENDING_RETIREMENT` here and not in Cutover-2. The tiles were
   their only cluster renders, so `cluster_vars.py check` refuses the keys in the cluster file
   from this PR on, and the k3s scripts keep pushing into them until Cutover-2. B1's token PR,
   [#6949](https://github.com/DanielH2018/server/pull/6949), retired the keys.
2. Cutover-1b, the PR that adds this item: the ten k3s-cron rules and `KumaDown` drop `shadow`,
   and the `kuma-cutover` group loses B1's five terms. `test_alertmanager_routing_b1_k3s_crons.py`
   pins the outcome: `LonghornBackupHealth*`, `EtcdSnapshotOffbox*` and `KumaDown` are critical,
   so they post to Discord and mail, and the other six rules are warnings that post to Discord
   alone. The tiles had the same severities (the two critical tiles were on the email tier).
   It is stacked and green before Cutover-1a merges, and it lands as soon as Cutover-1a's live
   read passes.
3. Cutover-2, the PR that adds this item: the five producers stop pushing to Kuma. The four
   shell scripts lose `kuma_push`, the token read and the `uptime-kuma-alive` ping, and
   `live_drift_check.py` loses its push. `health-crons.yml` removes the two Kuma credential files
   (`kuma-push.env` and `live-drift-check.env`) instead of rendering them. The monitor-bridge
   `uptime-kuma-alive` Healthchecks.io entry goes, and `HC_ROUTED_TAGS` drops
   `longhorn-backup-health`. The k3s setup role is a hand apply, so the operator runs
   `k3s-bringup.yml --tags "backup-health,etcd-snapshot,manifest-prune,release-staleness,live-drift"`
   after the merge (the exact lines are under *Operator steps* in the PR body). One behaviour
   ends with the push: the Kuma-only "Not a recovery" re-notify of `release-staleness-check` on a
   changed stale set. D6.17 disarms that check, so the loss is accepted.

**Operator-only, before Cutover-1a merges: pause `uptime-kuma-alive` in the Healthchecks.io
console.** The plan places the pause before Cutover-1b, but that is too late. From Cutover-1a's
apply, `longhorn-backup-health.sh` pushes into a deleted token, Kuma answers 404 as JSON,
`kuma_push` sets `KUMA_PUSH_OK=0`, and the script pings `uptime-kuma-alive/fail` every 10
minutes. Healthchecks.io would then page "Kuma is dead" for a healthy Kuma. The pause also keeps
Kuma's real death from paging from both Healthchecks.io and a live `KumaDown`. From the pause to
Cutover-1b's deploy nothing watches Kuma, which is the safe direction. Pause immediately before
Cutover-1a merges, so that window stays short. Cutover-2 removes the ping and the
entry, and the operator deletes the check in the console afterwards (*`KumaDown` replaces
`uptime-kuma-alive`* in M6).

**Inhibit decision: B1 adds one.** A3's Cutover-b has landed, so the plan's condition holds.
`KumaDown` (critical, `for: 5m`) and `ScrapeTargetDown{job="uptime-kuma"}` (warning, `for: 10m`)
both read `up{job="uptime-kuma"} == 0`, so one dead Kuma would page twice. The inhibit in
`alertmanager-secret.yaml.j2` takes `alertname="KumaDown"` as the source and
`alertname="ScrapeTargetDown"` with `job="uptime-kuma"` as the target, with `equal: [job,
instance]`, the form `FluxControllerDown` uses. `equal` matters for the `absent()` arm: it
carries no `instance`, so it holds no scrape page, and `ScrapeTargetDown` cannot fire without an
`up` series. `test_alertmanager_kuma_inhibit.py` pins the five cases, including the rejecting
ones (another instance, another job, another alert for the job, the `absent()` arm). Both rules
are live in this PR, as inhibition runs before routing. C-1a deletes the inhibit with `KumaDown`.

**Soak effect.** `renderer_lib/soak.py:longest_for` counts the `for:` of every unshadowed
`critical` rule against its `service` label. The ten k3s-cron rules carry `service: daniel-box`
and no `for:`. `KumaDown` has `service: uptime-kuma` and `for: 5m`, so the `uptime-kuma` tag's
soak wait gains 300s. `test_lander_deadlines_cover_the_soak.py` passes.

The two-hour no-pager bound counts from the moment the `uptime-kuma` Kustomization reports
Cutover-1a's merge commit. It ends when the `observability` Kustomization reports Cutover-1b's.
`CheckWithoutPager` pages critical once the last `monitor_status` sample of a B1 tile is older
than its term's range: 2h for k3s Longhorn Backup, 2h10m for Release Staleness Drift and 50h10m
for the three daily tiles. The daily tiles are therefore covered by the hand-kept two-hour bound
alone. If the bound is exceeded, revert Cutover-2 first if it merged, then Cutover-1b,
then Cutover-1a.

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens, and the operator's commit removed the keys from `secrets.yml` on 2026-11-02. A revert of
a cutover above no longer brings back a working tile, because no token is left for it to push
with.

### B2 initial_setup

Soak read, [#6723](https://github.com/DanielH2018/server/issues/6723): B2's shadow PR merged
as `0ed30cfdec` at 2026-10-23T20:42Z. Prometheus first evaluated the `initial-setup-checks`
group at 2026-10-23T20:46:47Z
(`min_over_time(timestamp(prometheus_rule_group_last_evaluation_timestamp_seconds{rule_group=~".*;initial-setup-checks"})[21d:1m])`).
That is 186.9 hours before the read, so the read covers a full `[168h:1m]` window. The window
ended at 2026-10-31T15:45Z and started at 2026-10-24T15:45Z, after the first evaluation. The
read used M1 convention 6's two queries with M2's 8-minute margin (*Which soak minutes count as
explained* in M2). Prometheus's oldest sample was 21.66 days old, the group held 12 rules, and
`increase(prometheus_rule_evaluation_failures_total{rule_group=~".*;initial-setup-checks"}[168h])`
was 0.

Each tile maps to the `<Check>Failed` and `<Check>NotReporting` pair that sits under its
`# Kuma tile "…"` comment in `initial-setup-checks.yaml`. The two Loki Read Route tiles share
alert names, so the read splits them by `service` (`daniel-box` or `daniel-server`).
`HostCheckOverdue` is not counted, because an inhibit mutes it beside every pair.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m | Q2 ±8m, tile reading up |
|---|---|---|---|---|---|---|---|---|
| Docs Refresh | 7762 | 2776 | 4143 | 1 | 0 | 1368 | 1345 | 0 |
| Homelab Evals | 306 | 0 | 10058 | 0 | 0 | 10058 | 10058 | 305 |
| Secret Rotation | 6836 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Setup Plane Drift (agent hosts) | 7143 | 354 | 391 | 0 | 0 | 37 | 22 | 0 |
| Loki Read Route (daniel-box) | 9739 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Loki Read Route (daniel-server) | 9738 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

Q1 ±8m is 0 for all six tiles. Docs Refresh's one strict Q1 minute is 2026-10-27T06:18Z,
the recovery edge of a DOWN run. The run at 06:17:13 logged `status=up`, and its textfile write
cleared `DocsRefreshFailed` one evaluation before Kuma's next sample read the up push. The
margin absorbs it.

**Q2 counts minutes in which the tile had no series.** Q2 subtracts `monitor_status == 0`. A
push tile reports no `monitor_status` series after a Kuma restart until its next push, so Q2
also counts those minutes. The last column requires `monitor_status == 1` instead. Docs Refresh
and Setup Plane Drift have no Q2 minute in which the tile read up, so their 1345 and 22 minutes
are failures that the shadow rule kept firing on while Kuma reported nothing. Two cases show
it:

- Docs Refresh's run at 2026-10-31T06:28:49Z failed its test suite. The textfile reads 0, and
  the tile had no series at the read. The window therefore ends inside a real DOWN, which the
  margin's caveat about the last 8 minutes does not affect.
- Setup Drift's push at 2026-10-25T13:20 got `http=404 by=kuma`, logged as `push failed`,
  while the tile was missing.

**Q1 cannot see a DOWN that Kuma never recorded,** and the slow tiles lack many samples. Homelab
Evals has 306 of 10080. The producers' own syslog closes that gap.
`probe.py loki-query --since 7d '{program=~"secret-rotation-audit|eval-run|docs-refresh|setup-drift-check"} |~ "status=(down|up)" !~ "push failed"'`
returns 6 Docs Refresh DOWN lines and 13 Setup Drift DOWN lines. It returns none for the eval
sweep or the secret audit. The witness tag `loki-read-route-health` has no `status=down` line
in the 7 days. For each of the 19 DOWN lines, `ALERTS{alertname="<Check>Failed",alertstate="firing"} @ <line + 300s>`
returned the alert. The Docs Refresh lines record failed suites on 2026-10-25, 10-29 and
10-31, failed shard-weight refreshes on 10-25 and 10-29, and failed generators on 10-26. The
Setup Drift lines are 2026-10-25 08:20 to 14:21: daniel-server ran an old render of
`aide-check.sh.j2` and `rkhunter-check.sh.j2`. `min_over_time(homelab_check_success{check=~"secret-rotation-audit|loki-read-route-health"}[168h])`
is 1 for all three series.

**Homelab Evals: a test run's leaked verdict, not a rule fault.** `EvalSweepFailed` fired for
10058 of the 10080 minutes, 305 of them while the tile read up. The tile's samples run from
2026-10-25 02:29:20 to 07:34:20, so `monitor_status == 1` holds for 310 one-minute steps,
02:30 to 07:39, the last five from Prometheus's lookback. The alert fired in all of them except
02:30 to 02:34. The tile has had no series since the weekly reboot. The rule read
`eval-run.prom` correctly, but test runs wrote that file and the sweep did not:

- The sweep runs weekly, on Sunday at 02:00. `changes(homelab_check_last_run_timestamp_seconds{check="eval-run",origin="daniel-box"}[8d])`
  was 159.
- The journal holds one sweep in the 8 days, from 02:22 to 02:28:44 on 2026-10-25, with
  `status=up`. Its write (`last_run` 1792895324) read 1 from 02:30 to 02:33.
- Writes at 02:33:50, 02:34:16, 02:52:58 and 03:34:56 have no `eval-run` journal line. The
  02:33:50 write set success back to 0, and the 03:34:56 write is the 0 that nothing has
  overwritten since.
- The 22 window minutes in which the alert did not fire have four causes:
    - 2026-10-24 22:23, 1 minute: a leaked write of 1.
    - 2026-10-25 02:30 to 02:34, 5 minutes: the sweep's real 1.
    - 2026-10-25 07:40 to 07:45, 6 minutes: the weekly reboot. Prometheus evaluated the group
      209 times that hour, against 240 in a normal hour.
    - 2026-10-28 00:45 to 00:54, 10 minutes: an evaluation gap, with 221 and 212 evaluations
      in the two hours that hold it. The textfile read 0 throughout.
- daniel-server also exports `eval-run`, last written at 2026-10-25T00:43:16Z. No sweep runs
  on daniel-server.
- The writes stopped once the test leak guard covered every child process.
  `f0f97fcbcc` ([#6191](https://github.com/DanielH2018/server/issues/6191),
  2026-10-25T03:23Z) pointed `TEXTFILE_DIR` at a temp directory. `9fb68d9650`
  ([#6238](https://github.com/DanielH2018/server/issues/6238), 2026-10-29) carried it into
  children given a hand-built `env=`. `changes(...[6d])` is 0 on both nodes.
- docs-refresh's file changed 14 times in 168 hours, which is its twice-daily schedule, so the
  leak did not reach it inside the window.

The rule is correct, so B2 changes no rule. The leaked daniel-server file gives
`HostCheckOverdue` a false page at 2026-11-01T06:43Z, and nothing overwrites it. It is filed as
[#6735](https://github.com/DanielH2018/server/issues/6735).

**The soak does not block Cutover-a.** For B2 that is the plan's Cutover-1a, which deletes the
six tiles with every rule still shadow. Every Q1 minute is covered, and every Q2 minute is
explained. One condition applies to Cutover-1b, which drops `shadow` within two hours of
Cutover-1a. It must not merge while
`homelab_check_success{check="eval-run",origin="daniel-box"}` holds the leaked 0, or
`EvalSweepFailed` pages Discord on a test verdict. The weekly sweep at 2026-11-01T02:00Z
overwrites the file. Open B2's window after that run reads 1, or after a sweep run by hand
does.

**The soak re-read and the gate, 2026-11-01T13:12Z.** The combined window's cutover PRs repeated
the queries above over `[168h:1m]` ending at 13:12Z (window start 2026-10-25T13:12Z, after the
group's first evaluation), with the same tile-to-rule mapping and M2's 8-minute margin.
Prometheus's oldest sample was 22 days old. Every tile except Homelab Evals read up when the queries ran, and Homelab Evals has no
series for the reason above.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| Docs Refresh | 6992 | 2702 | 3907 | 1 | 0 | 1206 | 1184 |
| Homelab Evals | 308 | 0 | 9425 | 0 | 0 | 9425 | 9425 |
| Secret Rotation | 5886 | 0 | 0 | 0 | 0 | 0 | 0 |
| Setup Plane Drift (agent hosts) | 6193 | 62 | 99 | 0 | 0 | 37 | 22 |
| Loki Read Route (daniel-box) | 9583 | 0 | 0 | 0 | 0 | 0 | 0 |
| Loki Read Route (daniel-server) | 9583 | 0 | 0 | 0 | 0 | 0 | 0 |

Q1 ±8m is 0 for all six tiles. The window has no new kind of minute, and a second read over the
20 hours since the earlier read's end (2026-10-31T15:45Z) shows the only new Q2 minutes:

- **Docs Refresh, 63 minutes from 2026-10-31T17:27Z to 18:28Z.** `DocsRefreshFailed` kept
  firing on the failed run of 06:28Z, which the earlier read records as a real DOWN. Kuma
  restarted at 15:06Z, so the tile had no series until the 18:28Z run logged `status=up`
  (`docs-refresh: status=up regenerated, committed, PR opened for docs-refresh/2026-10-31-1828`).
  The textfile cleared the rule at the same run.
- **Homelab Evals, 541 minutes ending 2026-11-01T02:27Z.** The leaked 0 that the earlier read
  describes held until the weekly sweep overwrote it. The tile still has no series.

**The gate b2.md set for the window holds.** The sweep of 2026-11-01 ran at 02:00Z.
`homelab_check_last_run_timestamp_seconds{check="eval-run",origin="daniel-box"}` reads 1793500032
(02:27:12Z) and `homelab_check_success{check="eval-run",origin="daniel-box"}` reads 1. The
journal line is `eval-run: status=up regenerated, committed, PR opened for
evals-history/2026-11-01-0227`. `EvalSweepFailed` and every other B2 alert are quiet, so
Cutover-1b drops `shadow` from rules that read the verdicts the producers hold today.

**`HostCheckOverdue{check="eval-run",origin="daniel-server"}` does not block B2.** The warning is
firing because daniel-server's leaked `eval-run.prom` (last written 2026-10-25T00:43:16Z, value 0)
is older than the generic deadman's limit, which is [#6735](https://github.com/DanielH2018/server/issues/6735).
It is a live rule with no `shadow` label, so it pages today, and neither cutover PR changes
that. The two `EvalSweep*` rules pin `origin="daniel-box"` in every selector, so none of the
twelve B2 rules reads this series. The inhibit that mutes `HostCheckOverdue` beside a `NotReporting`
pair uses `equal: [check, origin]`, so it covers the daniel-box series only. The page clears
when #6735 removes the leaked file.

**The deployed state, read at 13:20Z.** `kubectl get configmap -n observability prometheus-rules
-o jsonpath='{.data.initial-setup-checks\.yaml}'` is byte-identical to the file on master, and
the live `monitor-bridge-script-verdicts-logs` ConfigMap's `KUMA_RETIRING_TAGS` block is
identical to master's, which lists `docs-refresh`, `eval-run`, `secret-rotation-audit`,
`setup-drift-check` and `loki-read-route-health`.

B2 cuts over in the combined window of B2, B4 and B6b (*No-pager windows* in M6, amended by the
Cutover-1a PR that adds this paragraph). The window has four stacked PRs:

1. Cutover-1a deletes the six tiles (`docs-refresh`, `homelab-eval-sweep`, `secret-rotation`,
   `setup-drift-check`, `loki-read-route-daniel-box` and `loki-read-route-daniel-server`) with
   every rule still shadow. The producers keep pushing into deleted tokens until Cutover-2, and
   `KUMA_RETIRING_TAGS` lists their five tags. The live read is
   `probe.py metric 'monitor_status{monitor_name=~"Docs Refresh|Homelab Evals|Secret Rotation|Setup Plane Drift .*|Loki Read Route .*"}'`,
   which must return no series after the `uptime-kuma` promote. The six tokens
   (`docs_refresh_push_token`, `homelab_eval_push_token`, `secret_rotation_push_token`,
   `setup_drift_push_token`, `loki_route_witness_push_token` and
   `loki_route_witness_daniel_server_push_token`) enter `PENDING_RETIREMENT` here.
2. Cutover-1b drops `shadow` from the twelve rules and removes the six `CheckWithoutPager` terms.
3. The setup-plane Cutover-2 stops the pushes. Each producer keeps its textfile verdict and its
   syslog `status=` line and loses its push. On daniel-box the four producers ship in
   `homelab-daniel-box` (the `docs-refresh`, `eval-sweep`, `secret-rotation` and
   `loki-route-witness` components), so the host's `homelab-updater.timer` installs them and
   `land.sh` waits for that host. daniel-server's setup-drift check and Loki witness are
   `initial_setup` tasks in `tasks/crons.yml`, which also deletes the two retired env files.
   `narrow_setup.py` maps a `crons.yml` change to every tag in that file, and
   `setup_reach_hosts.py` owes the role on daniel-server and daniel-pi, so the tick records an
   `initial_setup` `manual_plane` line for both hosts. On the Pi the apply finds no env file to
   delete. `secret_rotation.py audit` loses its push arm, and the wrapper asks for `--verdict`;
   `--push` stayed an alias until the token PR deleted it, because the wrapper and the script
   reach daniel-box on different planes. A failed weekly `secret-rotate` no longer pages at once
   through the shared tile: one that leaves a dirty tree, an unpushed commit or an unlanded
   branch pages through the daily audit, and one that reverts pages once its token goes
   overdue. The component and package `credentials:` lists keep the six keys, because
   `host_secrets.py check` pins them to each host's SOPS file.
4. B2's token PR, [#6949](https://github.com/DanielH2018/server/pull/6949), retired the keys.

**Cutover-1b, the rules page.** The combined window's second PR drops `shadow` from the twelve rules in `initial-setup-checks.yaml` and
removes the six `CheckWithoutPager` terms of B2 (Docs Refresh, Secret Rotation, Setup Plane Drift, Homelab Evals and the two Loki Read Route tiles). Every rule file is `files/rules/initial-setup-checks.yaml`.
All twelve are `warning`, which posts to Discord alone, the channel mix the six tiles had. The routing test is `ansible/tests/services/test_alertmanager_routing_b2_b4_b6b.py`.

**Inhibits.** None is added. The generic `HostCheckOverdue` deadman is already muted beside every `<Check>NotReporting` pair by the inhibit on `equal: [check, origin]`, and that inhibit's source is the rule whichever of `shadow` it carries. `HostCheckOverdue{check="eval-run",origin="daniel-server"}` is a different series ([#6735](https://github.com/DanielH2018/server/issues/6735)) and stays a live page until the leaked file goes. No B2 rule overlaps a scrape-target page: the checks read textfile verdicts, and a dead `node` exporter is `ScrapeTargetDown`'s alone.

The no-pager bound is 2 hours from Cutover-1a's `uptime-kuma` apply. Cutover-1b is `observability`,
which Flux applies through `promote`, so it waits out Sunday 02:05 to 08:15 UTC like the rest of
the window.

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens, and the operator's commit removed the keys from `secrets.yml` on 2026-11-02. A revert of
a cutover above no longer brings back a working tile, because no token is left for it to push
with.

### B3 gitops_deploy

Soak read, [#6724](https://github.com/DanielH2018/server/issues/6724): B3's shadow PR merged as
`f4a0d6b49` at 2026-10-23T22:01Z. Prometheus first evaluated the `gitops-checks` group at
2026-10-23T22:06:48Z, so the window starts there. The read used M1 convention 6's two queries
with M2's 8-minute margin (*Which soak minutes count as explained* in M2), over `[168h:1m]` ending
at 2026-10-31T15:42Z. That window starts at 2026-10-24T15:42Z, after the group's first evaluation,
so neither tile has a short window. Prometheus's oldest sample was 21.7 days old, and
`increase(prometheus_rule_evaluation_failures_total{rule_group=~".*gitops-checks"}[168h])` was 0.

The shadow PR's `# Kuma tile "…"` comments map each tile to a pair of rules in `gitops-checks.yaml`:

- GitHub Ruleset Drift maps to `GitHubRulesetDrift` and `GitHubRulesetDriftNotReporting`.
- GitHub Interaction Limit maps to `GitHubInteractionLimitNotArmed` and
  `GitHubInteractionLimitNotReporting`.

The group's two GitOps tick rules shadowed the "GitOps Deploy — Alive" tile, which is not B3's,
so the read leaves them out. H7.52 deleted them with the deployer's timer. Two later commits
edited the file: `dc172c803` reworded two descriptions, and `119f1d645` added the two tick rules.
No B3 rule's `expr` changed after the first evaluation.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| GitHub Ruleset Drift | 7003 | 0 | 0 | 0 | 0 | 0 | 0 |
| GitHub Interaction Limit | 6963 | 0 | 0 | 0 | 0 | 0 | 0 |

Neither tile read DOWN in the window, and no B3 rule fired or went pending:
`count_over_time(ALERTS{alertname=~"GitHub.*"}[168h])` returned no series. Every Q1 and Q2 column
is therefore 0 by construction. Both producers reported success for the whole window:
`homelab_check_success` for each check has 10068 samples and none of them is 0.

**The missing samples are Kuma restarts, not lost verdicts.** Each tile lacks about 3,100 of
10,080 minutes, and Q1 cannot see a minute with no `monitor_status` sample. After a restart, Kuma
exports no status for a push tile until that tile's next push, and these tiles are pushed once a
day, at 06:40 and 06:50. Each gap opens at a restart of the `uptime-kuma` process and closes at
the next daily push:

| Kuma restart | Ruleset Drift resumes | Interaction Limit resumes |
|---|---|---|
| 2026-10-25 07:41 | 10-26 06:41 (1381 min) | 10-26 06:51 (1391 min) |
| 2026-10-28 00:51 | 10-28 06:41 (356 min) | 10-28 06:51 (366 min) |
| 2026-10-29 21:32 | 10-30 06:41 (548 min) | 10-30 06:51 (558 min) |
| 2026-10-30 20:33 | 10-31 06:41 (606 min) | 10-31 06:51 (616 min) |
| 2026-10-31 12:36 | open at the read (186 min) | open at the read (186 min) |

The sample counts reconcile: 7003 + 2891 + 186 = 10080 for Ruleset Drift, and 6963 + 2931 + 186 =
10080 for Interaction Limit. The window's six other restarts fall inside a gap that was already
open. The gaps did not hide a DOWN, for three reasons:

- Each script calls `write_check_textfile` in the same `push()` call that sends the Kuma push, so
  `homelab_check_success` carries the verdict Kuma would have received. It read 1 at every
  minute, gaps included.
- `homelab_check_last_run_timestamp_seconds` changed 7 times for each check, once a day. The
  timers rerun every 30 minutes while red, so a red run would have added changes. The largest
  `time() - homelab_check_last_run_timestamp_seconds` in the window was 24 hours, under the
  90000s deadline.
- The longest gap is 23 hours, under the tiles' 90000s interval, so Kuma's own no-push deadline
  could not expire inside one.

**The deployed rules.** `prometheus_rule_group_rules` for `gitops-checks` read 6, the rule count
of `gitops-checks.yaml` on master, and no B3 alert was firing. The ConfigMap itself was not read:
`kubectl` has no working kubeconfig for the reading user on daniel-server or daniel-box.

No minute is unexplained, so no rule changes, and the soak does not block Cutover-a. B3 takes the
non-bridge form of *Order*, so its Cutover-a is the plan's Cutover-1a (the tiles go), followed by
Cutover-1b (the rules drop `shadow`) and Cutover-2 (the pushes stop). The soak saw no DOWN, so
the rules' firing side rests on `tests/rules/gitops-checks.test.yaml`, which fires each of the
four rules.

**The cutover is three stacked PRs.** B3 takes the non-bridge form of *Order*, and its tiles are
hand-written keys of the static-monitors Secret, so Cutover-1a deletes them outright and needs no
exclusion in the `CHECKS` loop:

1. Cutover-1a, the tiles go: `github-ruleset-drift.json` and `github-interaction-limit.json`
   leave `static-monitors.yaml.j2`, and the two `^github-…$` patterns leave the status-page
   groups. Every B3 rule stays `shadow`. `ruleset_drift_push_token` and
   `interaction_limit_push_token` enter `PENDING_RETIREMENT` in this PR, because the cluster
   file holds both keys for the tile render and `cluster_vars.py check` refuses a key that no
   cluster render references (`test_the_committed_cluster_files_pass_check` reads red without
   the entries). The scripts still render the tokens from `secrets.yml` until Cutover-2.
2. Cutover-1b, the rules page: the four B3 rules drop `shadow`, and `kuma-cutover` loses B3's two
   `[50h10m]` terms.
3. Cutover-2, the push stops: both scripts drop `kuma_push`, `PUSH_URL` and the push token, and
   keep `write_check_textfile`. The tick applies it, as `gitops_deploy` is a setup role the
   deployer applies itself (`setup_routing.py --host daniel-box` routes it with `on_tick_host:
   true`).

**The re-read of the soak, 2026-10-31T19:02Z.** The read above was repeated with the same queries
(M1 convention 6 with M2's 8-minute margin, each tile mapped to its rule pair by the
`# Kuma tile "…"` comments) over `[168h:1m]`. Prometheus's oldest sample was 19.5 days old, and
`increase(prometheus_rule_evaluation_failures_total{rule_group=~".*gitops-checks"}[168h])` was 0.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| GitHub Ruleset Drift | 6803 | 0 | 0 | 0 | 0 | 0 | 0 |
| GitHub Interaction Limit | 6763 | 0 | 0 | 0 | 0 | 0 | 0 |

The sample counts are 200 below the table above because this window ends 200 minutes later and the
2026-10-31 12:36 restart gap was still open: the last sample of both tiles is 12:35Z. A Kuma
process also started at about 17:16Z, so the tiles do not re-export until the 06:41 and 06:51 pushes
on 2026-11-01. `count_over_time(ALERTS{alertname=~"GitHub.*"}[168h])` returned no series, and
`min_over_time(homelab_check_success{check=~"github-.*"}[168h])` read 1 for both checks. No Q1
minute is new, so nothing blocks Cutover-1a.

**The deployed state, read at 19:02Z.** `kubectl get configmap -n observability prometheus-rules`
holds `gitops-checks.yaml` byte-identical to master, with all six rules `shadow: "true"`, and
`prometheus_rule_group_rules` reads 6. `kuma-cutover.yaml` differs from master by the
`Host Packages — Status` term alone, which another batch added and `observability` had not yet
applied. B3's two terms are live. `monitor-bridge-script-verdicts-logs` holds `github-ruleset-drift`
and `github-interaction-limit` in `KUMA_RETIRING_TAGS`, so the *Order* gate (the entries deployed
before Cutover-1a) is met.

**Cutover-1a's live read needs a discriminator.** `monitor_status{monitor_name=~"GitHub (Ruleset
Drift|Interaction Limit)"}` returns nothing now, from the restart gap, so a bare empty read after
the merge proves nothing. The read that does is the three together:

- The `uptime-kuma` Kustomization reports Cutover-1a's merge commit.
- At the next 06:40 and 06:50 run, `journalctl -t github-ruleset-drift` and `-t
  github-interaction-limit` show `push failed (… by=kuma …)`, which Kuma answers for a token that
  no live monitor holds.
- `monitor_status` for both names stays empty after 06:52Z.

**The no-pager bound.** `uptime-kuma` and `observability` are both `owner: flux`, so the merge is
the apply. The two hours count from the moment the `uptime-kuma` Kustomization reports Cutover-1a's
merge commit, because Kuma stops paging the two checks then and every B3 rule is still shadow. They
end when the `observability` Kustomization reports Cutover-1b's merge commit. The `CheckWithoutPager`
terms are `[50h10m]` and the tiles are silent until 06:41 on 2026-11-01, so for B3 the bound rests
on whoever merges, not on the alert. If the bound is exceeded, revert Cutover-1b when it has
merged, then Cutover-2 when it has merged, so the scripts push again, then Cutover-1a, so the
tiles return.

**Cutover-1b: the rules page.** The four B3 rules (`GitHubRulesetDrift`,
`GitHubRulesetDriftNotReporting`, `GitHubInteractionLimitNotArmed` and
`GitHubInteractionLimitNotReporting`) drop `shadow`, and `kuma-cutover.yaml` loses B3's two
`[50h10m]` terms with their promtool series and expected alerts. All four rules are `warning`, so
Alertmanager posts them to Discord alone, the channel mix both tiles had.
The group's two GitOps tick rules kept `shadow`, because they shadowed the `GitOps Deploy — Alive`
tile, which is not B3's. H7.52 deleted them with the deployer's timer.

**Inhibit decision: B3 adds none.** The plan turns `EXPORTER_DEPENDENT` into an inhibit sourced on
`ScrapeTargetDown`. B3 needs none, because both rule shapes already hold on a dead exporter:

- A failure rule (`homelab_check_success == 0`) has no series once the `node` target is down, so
  it cannot fire.
- A NotReporting rule carries `unless on (origin) (up{job="node"} == 0)`, which holds it while the
  exporter is down. Its `absent_over_time` arm needs a 25-hour silence on top of that.

A dead `node` exporter on daniel-box therefore pages as `ScrapeTargetDown` alone.

**Soak effect: none.** `renderer_lib/soak.py:longest_for` counts the `for:` of every unshadowed
critical rule. The four rules are warnings and carry no `for:`, so the map is unchanged.

**Routing.** `test_alertmanager_routing_b3_gitops.py` is a sibling of the routing module, which is
at its line cap. It walks the four rules through the rendered routing tree: each reaches Discord
alone, muted for the weekly reboot. The two `GitOpsTick*` rules walk to the blackhole. A B3 rule
that regains `shadow` also walks to the blackhole and fails the test (`1 failed, 7 passed` with
`shadow` restored on `GitHubRulesetDrift`).

**Cutover-2: the push stops.** `github-ruleset-drift.sh` and `github-interaction-limit.sh` drop
the `kuma-push-lib.sh` source, `KUMA_HOST`, `KUMA_RESOLVE_IP`, `PUSH_URL` and the push token. Each
keeps `write_check_textfile`, and each logs `status=<up|down> <message>` under its own syslog tag.
The ruleset script already did. The interaction-limit script gains the line, because `kuma_push`
logged only a failed push and the journal is the host-side record of a clean run. Both templates
render no secret now, so their two install tasks lose `no_log`, and the two timers' unit
descriptions read `textfile verdict`. The timers keep their `kuma-check-` names, because a rename
would orphan the installed units.

**Which plane applies it.** `gitops_deploy` is a setup role the tick applies itself on daniel-box
(`setup_routing.py --host daniel-box` reads `on_tick_host: true`). A template change in the role
reads `broad_setup` with `broad_manual` false, so the tick fast-forwards and runs
`initial_setup.yml --tags gitops_deploy` before `land.sh` deploys any k8s role. Nothing here needs
a hand apply, and daniel-server and daniel-pi run neither timer.

**How to verify the push stopped.** Three reads, in this order. `rootq` cannot print either script,
because neither is on its path allowlist.

1. The tick record on daniel-box shows `gitops_deploy` applied at Cutover-2's merge commit, and
   no `gitops_deploy` entry stays in `owed.jsonl` or `hold_sha`.
2. After the next 06:40 and 06:50 run, `journalctl -t github-ruleset-drift` and
   `journalctl -t github-interaction-limit` show one `status=up` line each and no `push failed`
   line, where Cutover-1a's runs showed `push failed (... by=kuma ...)`. The
   interaction-limit line is also the proof that the new script ran: the old script logged
   nothing for a clean run.
3. `homelab_check_last_run_timestamp_seconds` for both checks advances to that run, and
   `homelab_check_success` reads 1.

**Test changes that follow the push.** The two script harnesses read the verdict back from the
journal line a `logger` stub records, in place of a stubbed `kuma_push`.
`test_neither_producer_pushes_to_kuma` in `test_gitops_check_rule_literals.py`, which C-6 retired, held both rendered
scripts to no Kuma library, `kuma_push` or push URL, with a red case for a pushing script. It also
found a `kuma_push` word in a comment of this PR's first draft. The two scripts leave the census of
secret-bearing host scripts, so the named member of three censuses moves to `registry-gc.sh`: the
secret-path census, the protected-bash hook test and the no-`no_log` census. The census floor in
`test_secret_bearing_host_scripts_are_not_world_readable.py` drops from 5 to 3.

**Token PR.** `ruleset_drift_push_token` and `interaction_limit_push_token` stayed in
`PENDING_RETIREMENT` from Cutover-1a until
[#6949](https://github.com/DanielH2018/server/pull/6949) retired both keys and deleted the two
entries in the same commit. The operator's commit removed the keys from `secrets.yml` on
2026-11-02.

### B4 other setup roles

Soak read, [#6725](https://github.com/DanielH2018/server/issues/6725): B4's shadow PR is
[#6052](https://github.com/DanielH2018/server/pull/6052), merged as `b063f17f1`. Prometheus
first evaluated the `other-setup-checks` group at 2026-10-23T22:50:48Z, more than 168h before
the read, so every tile has a full window. The read used M1 convention 6's two queries with
M2's 8-minute margin (*Which soak minutes count as explained* in M2) over `[168h:1m]`, ending
at 2026-10-31T15:40Z. Each tile was mapped to its rules by the `# Kuma tile "…"` comment above
them in `other-setup-checks.yaml`. The two UPS tiles share their alert names, so each also
selects its host's `service` label. Prometheus's oldest sample was 21.7 days old, so retention
covered the window. `increase(prometheus_rule_evaluation_failures_total{rule_group=~".*;other-setup-checks"}[168h])`
was 0, and `prometheus_rule_group_rules` read 16.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| UPS Secondary (daniel-box) | 10010 | 18 | 14 | 10 | 2 | 6 | 6 |
| UPS Secondary (daniel-server) | 10010 | 0 | 0 | 0 | 0 | 0 | 0 |
| Fake Remux Scan | 10010 | 0 | 0 | 0 | 0 | 0 | 0 |
| Fake Remux Replace | 10025 | 0 | 20 | 0 | 0 | 20 | 20 |
| MKV Attachment Repair | 10025 | 180 | 179 | 1 | 0 | 0 | 0 |
| etcd Restore Drill (full) | no series | — | 10063 | — | — | 10063 | 10063 |
| etcd Restore Drill (egress fence) | no series | — | 10063 | — | — | 10063 | 10063 |
| Renovate Notifier — Alive | 5628 | 0 | 0 | 0 | 0 | 0 | 0 |
| Renovate Agent — Alive | 6061 | 0 | 0 | 0 | 0 | 0 | 0 |

**UPS Secondary (daniel-box), 2 Q1 ±8m minutes: Kuma held the tile DOWN after the check
recovered.** The minutes are 2026-10-25 14:39 and 14:40. The 14:22 run of
`ups-secondary-health` timed out reaching upsd's ClusterIP and wrote `success 0`. The 14:30 run
wrote `success 1` and logged `status=up`, with no `push failed` line under its tag.
`UpsSecondaryFailed` fired from 14:24 to 14:31, so the rule tracked the producer's verdict
exactly. Kuma logged "No heartbeat in the time window" for the tile at 14:30:01 and read DOWN
until the 14:40 push landed at 14:41. M2's margin assumes a 300s push cadence, and this producer
pushes every 600s, so the Kuma-side hold runs 2 minutes past the margin. The fault itself was
the 2026-10-25 daniel-box disruption that A5 and B8 record: A5's kubelet target went down from
14:20 to 14:28, and B8's Traefik stall ran from 14:25 to 14:28. The daniel-server watchdog
reaches upsd locally and stayed up throughout.

**The two Q2 episodes fall inside the weekly reboot.** `UpsSecondaryFailed` for daniel-box
fired from 2026-10-25 07:46 to 07:51, and `FakeRemuxReplaceFailed` fired from 07:52 to 08:11.
Kuma read status 3 (maintenance) on every B4 tile from 07:31 to 08:21, and query 2's `== 0`
test counts that as up. The UPS run at 07:40 found no route to upsd while the cluster
rebooted, and the 07:50 run passed. The reconciler's 07:40 run failed with `No route to host`,
the heartbeats at 07:50 and 08:00 read that state, and the reconciler passed at 08:00:05.
Alertmanager's `weekly-reboot` interval covers Sunday 07:25 to 08:15 UTC, and every route a B4
rule takes after its cutover carries it: both `severity="critical"` routes and the default
Discord route. So the rules page nobody in that window, which matches what Kuma did.

**MKV Attachment Repair's 180 DOWN minutes are a real fault both sides saw.** From 2026-10-30
01:21 to 04:20, the repair sweep reported `ffprobe` failures on a batch of newly imported files
and repaired them over successive runs. `MkvAttachmentRepairFailed` fired for 179 of the 180
minutes. The one strict Q1 minute is the onset at 01:21, which the margin covers.

**The drill tiles have no `monitor_status` series, so their Q1 cannot be read.** Kuma exports a
push tile's gauge only after a heartbeat in the running pod's lifetime. The uptime-kuma pod
started six times in the window, at 2026-10-25 07:41 and 13:14, 10-28 00:44, 10-29 21:32,
10-30 20:33 and 10-31 15:06. The monthly drill last pushed on 2026-10-01, before both the
window and Prometheus's retention. Neither drill has written a textfile yet either. Both
`NotReporting` rules fired for the whole window (10063 minutes, which excludes the minutes the
`node` scrape was down) through their `absent_over_time` arm. The rule file's header records
this as expected until the 2026-11-01 11:20 UTC run writes its first file. The runner on
daniel-server, `/usr/local/bin/etcd-restore-drill-vm`, was installed on 2026-10-24. It sources
`homelab-check-lib.sh`, calls `write_check_textfile` and renders `DEADLINE_S=3024000`, so that
run writes both files.

**The renovate tiles' missing minutes are Kuma restarts, not missed beats.** Each gap in their
series starts at one of the Kuma pod starts above and ends at the tile's next daily push: about
13:05 for the notifier and about 11:08 for the agent. Query 1 cannot see those minutes, but the
producers' `homelab_check_last_run_timestamp_seconds` shows a run every day. The longest gap
between runs was about 24.5 hours for the agent, under its 28-hour deadline, and under 36 hours
for the notifier. Neither tile could have gone DOWN unseen, and neither `NotReporting` rule
fired. The 55 to 70 minutes the 10-minute tiles lack are the same restarts, and each of those
tiles returned at its next push within 10 minutes. `97ca99188` added the disarm clause to
`RenovateAgentNotReporting` on 2026-10-30, inside the window; the rule was quiet before and
after.

No minute is a miss, so no rule changes.

**The deployed rules, read at 15:49Z.** `kubectl get configmap -n observability prometheus-rules -o jsonpath='{.data.other-setup-checks\.yaml}'`
is byte-identical to `other-setup-checks.yaml` on master. The only B4 alerts firing were the two
drill `NotReporting` rules.

**The soak does not block Cutover-1a for the seven 10-minute and daily tiles. The drill pair
holds the whole cutover until the 2026-11-01 run is read.** M6's *Order* deletes the drill
tiles only together with the five `[2h]` tiles, so Cutover-1a removes all nine tiles at once.
Cutover-1b must then follow inside the two-hour bound and drop the drill rules' `shadow`. Before
the first drill run, those two rules are firing, and `EtcdRestoreDrillNotReporting` is
critical, so dropping `shadow` would page Discord and email at once. Cutover-1a therefore waits
until after 2026-11-01 11:20 UTC, and this read is the gate:

- `homelab_check_last_run_timestamp_seconds{check=~"etcd-restore-drill-vm|etcd-restore-drill-egress-fence",origin="daniel-server"}`
  returns two series.
- `ALERTS{alertname=~"EtcdRestoreDrill.*"}` is empty, or its `Failed` alert matches a DOWN on
  the drill's own Kuma tile.

**The soak re-read and the gate, 2026-11-01T13:12Z.** The combined window's cutover PRs repeated
the queries above over `[168h:1m]` ending at 13:12Z (window start 2026-10-25T13:12Z, after the
group's first evaluation), with the same tile-to-rule mapping and M2's 8-minute margin.
Prometheus's oldest sample was 22 days old.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| UPS Secondary (daniel-box) | 9983 | 18 | 8 | 10 | 2 | 0 | 0 |
| UPS Secondary (daniel-server) | 9983 | 0 | 0 | 0 | 0 | 0 | 0 |
| Fake Remux Scan | 9982 | 0 | 0 | 0 | 0 | 0 | 0 |
| Fake Remux Replace | 9990 | 0 | 20 | 0 | 0 | 20 | 20 |
| MKV Attachment Repair | 9990 | 180 | 179 | 1 | 0 | 0 | 0 |
| etcd Restore Drill (full) | 112 | 111 | 10065 | 0 | 0 | 9954 | 9946 |
| etcd Restore Drill (egress fence) | 112 | 0 | 9954 | 0 | 0 | 9954 | 9954 |
| Renovate Notifier — Alive | 4666 | 0 | 0 | 0 | 0 | 0 | 0 |
| Renovate Agent — Alive | 5043 | 0 | 0 | 0 | 0 | 0 | 0 |

The two Q1 ±8m minutes of UPS Secondary (daniel-box) are 2026-10-25 14:39 and 14:40, the Kuma hold
that the earlier read explains. The 10-minute producer pushes past M2's margin. MKV Attachment
Repair's 180 DOWN minutes are the 2026-10-30 `ffprobe` fault, with one strict Q1 minute at the
onset. The window no longer holds 2026-10-25's 07:46 to 07:51 `UpsSecondaryFailed` episode, so
that tile's Q2 reads 0 here.

**Fake Remux Replace's 20 Q2 minutes are this morning's weekly reboot, not the 10-25 episode.**
`FakeRemuxReplaceFailed` fired from 2026-11-01T07:51:30Z to 08:10:30Z
(`min_over_time(timestamp(ALERTS{alertname=~"FakeRemuxReplace.*",alertstate="firing"})[168h:1m])`
and the matching `max_over_time`). The reboot cron fires at 07:30Z, Kuma's maintenance window
covers the same period, and Alertmanager's `weekly-reboot` interval (Sunday 07:25 to 08:15 UTC)
mutes every route the rule takes after its cutover. The shape matches the earlier read's 10-25
episode.

**The drill ran at 2026-11-01T11:20Z and failed, and the gate's second clause holds.**

- Both textfiles exist. `homelab_check_last_run_timestamp_seconds{check=~"etcd-restore-drill-vm|etcd-restore-drill-egress-fence",origin="daniel-server"}`
  returns two series, 1793532060 (11:21:00Z) and 1793532042 (11:20:42Z).
- `EtcdRestoreDrillNotReporting` and `EtcdRestoreDrillEgressFenceNotReporting` fired from the
  window's start to 11:20Z because the monthly runner had written no file yet, the expected state
  the rule file's header records. The rules read the right series, and both cleared when the run
  wrote its files. The monthly tile has no `monitor_status` series for the same reason, so Q2
  counts every one of those minutes.
- The egress fence passed: `homelab_check_success{check="etcd-restore-drill-egress-fence"}` reads 1.
  The full drill failed: `homelab_check_success{check="etcd-restore-drill-vm"}` reads 0, the journal
  line is `etcd-restore-drill-vm: status=down run=20261101T112001Z full restore drill FAILED (exit
  1): etcd-restore-drill: scratch server exited during startup`, and the `etcd Restore Drill (full)`
  tile reads DOWN since 11:21Z. `EtcdRestoreDrillFailed` (shadow, critical) fires beside it, so
  the rule and the tile agree. That is the gate's second clause, a `Failed` alert that matches a
  DOWN on the drill's own tile.
- The fault is real and is not a rule fault. The restore stage completed, then the scratch
  `k3s server` exited with `cannot use public IP 127.0.0.1 with endpoint reconciler`
  (`scripts/backup/etcd_restore_drill.sh` starts it with `--advertise-address 127.0.0.1`, and the
  k3s version the guest runs rejects a loopback address). [#6846](https://github.com/DanielH2018/server/issues/6846)
  owns it.

B4 therefore joins the window, with one consequence recorded here. Cutover-1b drops `shadow` from
`EtcdRestoreDrillFailed`, which is critical, so it pages Discord and email at the merge and keeps
firing until a drill run passes. The drill runs monthly, so that is the first of December unless
the operator runs it by hand after #6846 is fixed. Kuma's tile is DOWN today for the same cause.

**The deployed state, read at 13:20Z.** `other-setup-checks.yaml` in the live `prometheus-rules`
ConfigMap is byte-identical to master's file, and `KUMA_RETIRING_TAGS` in the live monitor-bridge
ConfigMap is identical to master's, which lists `ups-secondary-health`, `fake-remux-health` and
`etcd-restore-drill-vm`. The renovate pair lists nothing, because a unit's `curl` logs no
`push failed` line.

B4 cuts over in the combined window of B2, B4 and B6b. The window has four stacked PRs:

1. Cutover-1a deletes all nine tiles with every rule still shadow, and carries the renovate pair's
   Cutover-2 (*Order* in M6). The notifier and agent units lose the `ExecStartPost` beat, their
   `KUMA_PUSH_URL` lines, and the agent's crash-report push, so a beat into a deleted tile cannot
   fail the unit. The tick applies it on daniel-box. Its live read is
   `probe.py metric 'monitor_status{monitor_name=~"UPS Secondary .*|Fake Remux .*|MKV Attachment Repair|etcd Restore Drill.*|Renovate .* — Alive"}'`,
   which must return no series after the `uptime-kuma` promote. Nine push tokens enter
   `PENDING_RETIREMENT`: `ups_secondary_push_token`, `ups_secondary_daniel_server_push_token`,
   `monitor_bridge_fake_remux_push_token`, `monitor_bridge_fake_remux_replace_push_token`,
   `mkv_attachment_repair_push_token`, `etcd_drill_full_push_token`, `etcd_drill_fence_push_token`,
   `monitor_bridge_renovate_alive_push_token` and `renovate_agent_kuma_push_token`.
2. Cutover-1b drops `shadow` from the sixteen rules and removes the seven `CheckWithoutPager`
   terms, which closes the drills' window with the five `[2h]` terms.
3. The setup-plane Cutover-2 stops the remaining pushes, from the UPS secondary watchdog, the
   three fake-remux heartbeats and the etcd drill orchestrator. Each keeps its textfile verdict
   and its syslog line. The UPS watchdog ships in both cluster nodes' host packages (the
   `nut-secondary` component), so each host's `homelab-updater.timer` installs it. The
   `nut_host` role is emptied and its template edit reaches no other host, so it owes no
   `manual_plane` line. The tick applies `fake_remux` on daniel-box. The drill's change narrows
   to `hypervisor-etcd-drill` and reaches daniel-server and daniel-pi, because the role's
   defaults changed, so the tick records a `hypervisor` `manual_plane` line for both. The
   daniel-server apply deletes the retired `etcd-drill-kuma-push.env`; on the Pi the role's
   install path is gated off and the apply changes nothing. The packages' `credentials:` lists
   keep both UPS keys, because `host_secrets.py check` pins them to each host's SOPS file.
4. B4's token PR, [#6949](https://github.com/DanielH2018/server/pull/6949), retired the keys.

**Cutover-1b, the rules page.** The combined window's second PR drops `shadow` from the sixteen rules in `other-setup-checks.yaml` and
removes the seven `CheckWithoutPager` terms of B4 (the two UPS secondaries, the three fake_remux tiles and the two renovate tiles; the drill tiles had no term). Every rule file is `files/rules/other-setup-checks.yaml`.
The four UPS rules, `EtcdRestoreDrillFailed` and `EtcdRestoreDrillNotReporting` are `critical`, so they mail as well as post; the other ten are `warning`. **`EtcdRestoreDrillFailed` fires at the merge**: the 2026-11-01 drill failed ([#6846](https://github.com/DanielH2018/server/issues/6846)) and keeps the verdict at 0 until a run passes. The rule is a deliberate page, since Kuma's tile is DOWN for the same cause. The drills' window closes with this PR, because the five `[2h]` terms that bounded it leave together with the drill rules' `shadow`.

**Inhibits.** None is added, for the reasons under B2. The renovate pair has a `NotReporting` rule only, and the deadman inhibit covers it. `UpsSecondary*` reads the secondary watchdog's own verdict and overlaps none of A1's `UPS*` rules, which read the UPS itself.

The no-pager bound is 2 hours from Cutover-1a's `uptime-kuma` apply. Cutover-1b is `observability`,
which Flux applies through `promote`, so it waits out Sunday 02:05 to 08:15 UTC like the rest of
the window.

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens, and the operator's commit removed the keys from `secrets.yml` on 2026-11-02.

### B5 Pi

B5's soak read, [#6726](https://github.com/DanielH2018/server/issues/6726): B5's shadow PR is
`ef12dd28e1`, committed at 2026-10-23T21:42Z. Prometheus first evaluated the `pi-checks` group
at 2026-10-23T21:53:48Z
(`min_over_time(timestamp(prometheus_rule_group_last_evaluation_timestamp_seconds{rule_group=~".*;pi-checks"})[21d:1m])`).
That is more than 168 hours before the read, so both tiles get the full window. The read used
M1 convention 6's two queries over `[168h:1m]` ending at 2026-10-31T15:42Z, with M2's 8-minute
margin (*Which soak minutes count as explained* in M2). The window starts at
2026-10-24T15:42Z.

Each tile maps to the two rules that the `pi-checks.yaml` comments name for it:

- "Daniel Pi SD Health" maps to `PiSdHealthFailed` and `PiSdHealthNotReporting`.
- "Daniel Pi Recovery" maps to `PiRecoveryFailed` and `PiRecoveryNotReporting`.

Prometheus's oldest sample was 21.7 days old, so retention covered the window.
`increase(prometheus_rule_evaluation_failures_total{rule_group=~".*pi-checks"}[168h])` was 0.
`prometheus_rule_group_rules` for the group read 4.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| Daniel Pi SD Health | 10036 | 0 | 0 | 0 | 0 | 0 | 0 |
| Daniel Pi Recovery | 10036 | 107 | 107 | 1 | 0 | 1 | 0 |

The ±8m columns use `last_over_time(ALERTS{...}[17m:1m] offset -8m)` for Q1 and
`min_over_time(monitor_status{...}[17m:1m] offset -8m) == 0` for Q2. Every firing minute was
`PiRecoveryFailed`. Neither `NotReporting` rule fired, and no SD Health rule fired.

The Recovery tile's 107 DOWN minutes form three episodes. `PiRecoveryFailed` fired across each
one, and `probe.py alerts --pi --check recovery --days 8 --raw` holds the producer's message for
every cycle:

- **2026-10-24, DOWN 18:56 to 20:00 (65 min), firing 18:57 to 20:00.** Every cycle from 18:55
  to 19:55 reads `not running: pi-canary []; restart FAILED: pi-canary`. `49848cf98` (18:48)
  ran the Pi's first Compose service, `pi-canary`, and the canary failed to start.
  `b17385f09` (committed 19:39) raised its memory reservation to the daemon's 6 MiB floor. The
  episode ended at 20:00, after the Pi's updater timer installed that change. This was a real
  fault that both planes paged.
- **2026-10-25, DOWN 20:17 to 20:20 (4 min), firing 20:17 to 20:21.** One cycle at 20:15 reads
  `not running: wg-easy [status=created ...]; restarted: wg-easy`. The restart took, and the
  rule fires on a restart by design (the `# DECIDED:` marker in
  `hosts/components/pi-heartbeats/templates/pi-recovery-health.sh.j2`).
- **2026-10-26, DOWN 03:59 to 04:36 (38 min), firing 03:59 to 04:36.** The cycles read
  `not running: autoheal [... error=failed to create task for container: AlreadyExists ...];
  restart FAILED: autoheal`. This is the Pi overload that B8's read records, which ended when
  the Pi rebooted at 04:32:54. `up{job="node-pi"}` read 0 for single minutes in that span.
  Loki holds no DOWN line from the producer after 04:18. The producer kept running, because
  `homelab_check_last_run_timestamp_seconds` advanced every 5 minutes from 04:05:08 to
  04:35:11. Its lines from 04:20 to 04:30 did not reach Loki during the overload. The textfile
  read `homelab_check_success` 0 until the 04:35 run, so the rule and the tile cleared together
  at 04:36.

The one strict Q1 minute is 2026-10-24 18:56. The producer wrote its first DOWN at 18:55:18,
and Kuma took the push immediately. The rule fired one minute later, after the next `node-pi`
scrape and rule evaluation. The one strict Q2 minute is 2026-10-25 20:21. Kuma took the 20:20
`up` push before Prometheus scraped the new textfile. Both minutes are the scrape and evaluation
lag that the margin absorbs, so Q1 ±8m and Q2 ±8m are 0.

Kuma exported no sample for either tile for 44 minutes (10036 of 10080), and Q1 cannot see
those minutes. Its scrape target read `up` 0 for 3 minutes and was missing for 12, and the
other gaps are minutes when the scrape answered without the tiles' series. Other tiles show the
same gaps: Daniel Pi Host has 10025 samples. `probe.py alerts --pi --check sd --days 8` lists
no DOWN for `pi-sd-health`, and the Recovery episodes above are the only three for
`pi-recovery-health`.

**The deployed rules, read at 15:42Z.** All four rules carry `shadow: "true"`, and none was
firing (`ALERTS{alertname=~"PiRecovery.*|PiSdHealth.*"}` returned no series).
`homelab_check_success` read 1 for both checks, and `homelab_check_deadline_seconds` read 900,
which matches the rules' 900-second literals.

**Inhibit decision: B5 needs none.** The plan's B5 row assigns no inhibit, and no B5 rule
double-pages a dead Pi. Both `NotReporting` rules carry
`unless on (origin) (up{job="node-pi"} == 0)`. Both `Failed` rules read
`homelab_check_success`, and that series leaves with the dead scrape. `PiHostDown` therefore
pages alone.

No minute is unexplained, so no rule changes. **The soak does not block Cutover-a.** B5 is a
heartbeat batch, and the plan names its tile deletion Cutover-1a (*Cutover-1a, the tiles go*).

### B5 cutover record

B5 cuts over in three stacked PRs: Cutover-1a ([#6778](https://github.com/DanielH2018/server/pull/6778))
deletes the two tiles, Cutover-1b drops `shadow` from the four rules, and Cutover-2 drops the
push from the Pi package's two heartbeats.

**The soak, re-read before Cutover-1a.** The same queries ran over `[168h:1m]` ending at
2026-10-31T19:02Z, so the window starts at 2026-10-24T19:02Z. The ±8m columns use M2's
`[16m:1m] offset -8m` form (*Which soak minutes count as explained* in M2), where the read above
used `[17m:1m]`. The strict columns do not depend on that form, so compare the two tables by
those.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| Daniel Pi SD Health | 10032 | 0 | 0 | 0 | 0 | 0 | 0 |
| Daniel Pi Recovery | 10032 | 100 | 101 | 0 | 0 | 1 | 0 |

No new episode started. The later window start cuts off the first six minutes of the 2026-10-24
canary episode, which removes the one strict Q1 minute (18:56) and seven DOWN minutes. The
Recovery tile's three episodes are the ones above. `probe.py alerts --pi --check recovery --days 8`
lists the same three. Its SD-health twin timed out reading Loki (exit 75, unreadable), so the SD
tile's clean read rests on the metric: 0 DOWN minutes, no firing minute, and no `PiSdHealth*`
series in `ALERTS`.

**The deployed rules matched master.** `pi-checks.yaml` in the live `prometheus-rules` ConfigMap
is byte-identical to the file on master, all four rules carry `shadow: "true"`, and none was
firing. `up{job="node-pi"}` carries `origin="daniel-pi"`, so the `unless on (origin)` guard of
both `NotReporting` rules matches. The live monitor-bridge carries `pi-sd-health` and
`pi-recovery-health` in `KUMA_RETIRING_TAGS`.

**Inhibit decision: none, and Cutover-1b adds none.** B8 added an inhibit of `PiPortDead` by
`PiHostDown` because `PiPortDead` has no `up` gate. B5 has no such rule:

- Both `NotReporting` rules carry `unless on (origin) (up{job="node-pi"} == 0)`.
- Both `Failed` rules read `homelab_check_success`, which goes stale with the dead scrape. The
  exporter-down case in `pi-checks.test.yaml` now holds all four rules quiet while `up` reads 0.
- A dead Pi therefore pages as `PiHostDown` alone.
- `PiRecoveryFailed` and `PiPortDead` can fire together when a container dies on a live Pi. They
  report different facts (what died and whether its restart took, against a refused port), and
  `PiRecoveryFailed` is a warning that posts to Discord alone. An inhibit between them would hide
  the recovery heartbeat's message.

**Channel mix.** `PiSdHealthFailed` and `PiSdHealthNotReporting` are critical, so they post to
Discord and mail, as the tag-severity critical tile did. `PiRecoveryFailed` and
`PiRecoveryNotReporting` are warnings, so they post to Discord alone, as the tile did.
`test_alertmanager_routing_b5_pi.py` holds both outcomes and a shadowed copy's walk to the
blackhole.

**The no-pager bound.** B5's checks have no live pager from Cutover-1a's `uptime-kuma` apply to
Cutover-1b's `observability` apply. If Cutover-1b has not applied within two hours of Cutover-1a's
apply, revert Cutover-1a. The two `[2h]` `CheckWithoutPager` terms are the backstop, and
Cutover-1b removes them with their promtool cases.

**The revert order.** Revert Cutover-2 first and wait for the Pi to install the revert, because
the Pi must push again before its tiles exist. Then revert Cutover-1b, which restores `shadow`
and the two terms. Then revert Cutover-1a, which restores the tiles. Reverting Cutover-1a while
Cutover-1b is live restores a second pager. Reverting it while Cutover-2 is live restores tiles
that nothing pushes to, and both go DOWN within ten minutes.

### B5 Cutover-2 record

Cutover-2 removes the Kuma push, the token read and the push-library source from `pi-sd-health`
and `pi-recovery-health`, and removes both tags from `HC_ROUTED_TAGS`. It deploys `monitor-bridge`
(Flux applies the merge) and the `homelab-daniel-pi` package (the Pi's `homelab-updater.timer`
installs it).

**Host secrets.** The package's `credentials:` listed `pi_sd_health_push_token` and
`pi_recovery_push_token` until the token PR, and the Pi's SOPS file held both. `host_secrets.py check` pins
the list to the file, and changing the file takes the operator's `host_secrets.py sync daniel-pi`.
B5's token PR, [#6949](https://github.com/DanielH2018/server/pull/6949), removed both keys from the list and the
file in that one step. No unit loads either credential after Cutover-2.

**The gap between the two applies.** The Pi's `health.log` lines for a lost push read
`push failed (status=down: ...)`, with no `by=kuma`. `parse_push_line` reads them as `swallowed`,
not `rejected`, so `KUMA_RETIRING_TAGS` exempts none of them, and `HC_ROUTED_TAGS` was the only
exemption. Once `monitor-bridge` applies Cutover-2 and before the Pi installs the package, a Pi
cycle that ends `down` pages `Swallowed Push Verdicts`, if that line is the newest `pi-*` line
in the 3-hour window and a sibling tag landed. The page clears when the Pi installs the package,
because the new lines read `status=down ...` and parse as runs. The window is the time from
`monitor-bridge`'s apply to the Pi's install: the updater fires every 15 minutes, outside
23:00 to 00:59 and 04:00 to 05:59, after the builder publishes the package. Land Cutover-2 while
the last `pi-recovery-health` and `pi-sd-health` lines read `status=up`, and outside those two
blackout windows.

**How to verify the Pi applied it.** `scripts/deploy_tools/land_lib/host_wait.py:wait_for_hosts`
waits on the first two reads, and `land.sh` ends `deferred` when it cannot wait:

1. `homelab_host_applied_info{origin="daniel-pi"}` carries a `sha` that is the merge commit or
   descends from it, and `homelab_host_reconcile_ok{origin="daniel-pi"}` reads 1.
2. `homelab_host_update_success_timestamp_seconds{origin="daniel-pi"}` is newer than the
   merge.
3. `homelab_check_last_run_timestamp_seconds{check=~"pi-sd-health|pi-recovery-health"}` is newer
   than that update time, so the new scripts ran after the install.
4. `ssh daniel-pi tail -n 2 /var/log/pi-health/health.log` shows `status=` lines and no
   `push failed`. `ssh daniel-pi grep -c kuma_push /usr/lib/homelab/pi-sd-health` prints 0.
5. `Swallowed Push Verdicts` is not paging, and `ALERTS{alertname=~"PiSdHealth.*|PiRecovery.*"}`
   returns no series while both checks read 1.

**The revert.** Reverting Cutover-2 restores the push, the token read and both `HC_ROUTED_TAGS`
entries. The Pi's SOPS file held both tokens until the token PR, so the revert needed no operator step. Revert it
before Cutover-1b and Cutover-1a, and wait for the Pi to install the revert first (the revert
order above).

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens. On 2026-11-02 the operator's commit removed the keys from `secrets.yml`, from the
package's `credentials:` and, through `host_secrets.py sync daniel-pi`, from the Pi's SOPS file.
A revert of a cutover above no longer brings back a working tile, because no token is left for
it to push with.

### B6a host crons

B6a's soak read, [#6727](https://github.com/DanielH2018/server/issues/6727): B6a's shadow PR
merged as `ef7c4c4964` at 2026-10-23T19:21Z. The `host-checks` group predates B6a, so its
first evaluation does not mark the start of B6a's rules. `prometheus_rule_group_rules` for the
group rose in one step from 15 to 19, B6a's four rules, between 19:33Z and 19:34Z, so B6a's
rules first evaluated then. B1's step to 30 came later, at 19:54Z. The read used M1
convention 6's two queries over `[168h:1m]` ending at 2026-10-31T16:07Z, with M2's 8-minute
margin (*Which soak minutes count as explained* in M2). The window starts at
2026-10-24T16:07Z, after B6a's first evaluation, so both tiles have the full 168 hours.
Prometheus's oldest sample was 21.7 days old, and
`increase(prometheus_rule_evaluation_failures_total{rule_group=~".*;host-checks"}[168h])` was 0.

Each tile maps to the `Failed` and `NotReporting` pair that the shadow PR's rule comment in
`host-checks.yaml` assigns to its producer, by the `check` label the producer writes:

- `Artifacts Peer Sync`: `ArtifactsSyncFailed`, `ArtifactsSyncNotReporting`
  (`check="sync-artifacts"`).
- `k3s Telemetry OTLP Door`: `OtlpDoorClosed`, `OtlpDoorNotReporting`
  (`check="claude-otel-health"`).

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| Artifacts Peer Sync | 10040 | 0 | 0 | 0 | 0 | 0 | 0 |
| k3s Telemetry OTLP Door | 10010 | 0 | 0 | 0 | 0 | 0 | 0 |

Neither tile read DOWN, and no B6a rule fired or went pending. `ALERTS` holds no series for
any of the four rules over the window. The same read returns 1189 firing minutes for B1's
`ManifestPruneCheckFailed` and 648 for `LiveDriftCheckFailed`, so `ALERTS` records shadow
rules. The empty result means B6a's rules were quiet, not that the read misses them.

**The rules read the live series.** A clean soak would also follow from a selector that
matches nothing, so the rules' inputs were read directly:

- `homelab_check_success{origin="daniel-box"}` holds 10068 one-minute samples for each check,
  and none of them is anything but 1. The producers reported success all week, which agrees
  with the tiles.
- The largest age of `homelab_check_last_run_timestamp_seconds` over the window was 539s for
  `sync-artifacts`, against the rule's 900s deadline, and 839s for `claude-otel-health`,
  against 1500s.
- Each textfile series is absent for 12 minutes, the same 12 minutes in which daniel-box's
  `node` scrape had no `up` series. No absence outside that scrape gap remains, and 12
  minutes is under both `absent_over_time` windows.
- The red/green shapes for the four rules are in `host-checks.test.yaml`, which the shadow PR
  added.

**The samples each tile lacks from 10080 are maintenance and Kuma restarts.** Neither can
hide a failure, because the producer's verdict was 1 throughout:

- Both tiles read maintenance (3) on 2026-10-25 only: `Artifacts Peer Sync` for 44 minutes
  from 07:26 to 08:15, and `k3s Telemetry OTLP Door` for 39 minutes from 07:31 to 08:20. That
  is the weekly reboot window. Q1 counts only 0, so these minutes are not DOWN.
- `Artifacts Peer Sync` had no `monitor_status` series for 40 minutes, and `k3s Telemetry OTLP
  Door` for 70. `process_start_time_seconds{job="uptime-kuma"}` changed 11 times in the
  window. Every absent minute falls within 30 minutes of a Kuma restart, or while Kuma's
  scrape read 0 or had no `up` series. A push tile has no series after a restart until the
  producer's next push, and the crons push every 5 and every 10 minutes, so 30 minutes covers
  each gap. The OTLP tile lacks more because its cron runs half as often.

**A stale `sync-artifacts` series from daniel-server is outside the mapping.** A selector
without `origin` also returns `homelab_check_success{check="sync-artifacts",origin="daniel-server"}`.
Its last run timestamp is 2026-10-25T02:23Z, and it has no sample after 2026-10-29T02:17Z.
That is the stale check file daniel-server held until the role's removal task existed
([#6355](https://github.com/DanielH2018/server/issues/6355)). All four rules select
`origin="daniel-box"`, so the series never reached them.

**The deployed rules, read at 16:09Z.** `kubectl get configmap -n observability
prometheus-rules -o jsonpath='{.data.host-checks\.yaml}'` on daniel-box has the same SHA-256 as
`host-checks.yaml` on master. All four B6a rules carry `shadow: "true"`.

No minute is unexplained, so no rule changes. The soak does not block Cutover-a, which B6a
runs as Cutover-1a (*Every other producer* in M6).

B6a's cutover is Cutover-1a, Cutover-1b and Cutover-2. The first two are PRs of their own, and
Cutover-2 rides in H7.40 slice 4's PR. `uptime-kuma` and `observability` are both `owner: flux`, so a merge is the apply and
*The cutover order*'s `land.sh` lines do not hold for them:

1. Cutover-1a, `m6-b6a-cutover-1a-tiles-go`: `claude-otel-health.json` and `artifacts-sync.json`
   leave `static-monitors.yaml.j2`, with their two status-page patterns. `artifacts_sync_push_token`
   and `claude_otel_push_token` enter `PENDING_RETIREMENT`, because no cluster render references
   them once the tiles go. Every B6a rule stays shadow. Its live read is
   `probe.py metric 'monitor_status{monitor_name=~"Artifacts Peer Sync|k3s Telemetry OTLP Door"}'`,
   which must return no series.
2. Cutover-1b, the PR that adds this section: the four `host-checks.yaml` rules drop `shadow`,
   and the `kuma-cutover` group loses B6a's two terms.
3. Cutover-2, in [#6788](https://github.com/DanielH2018/server/pull/6788): it drops the push from
   `sync-artifacts.sh.j2` and `claude-otel-health.sh.j2`. See *Cutover-2 waits for H7.40 slice 4*
   and *Cutover-2: the push stops* below.

**The re-read of the soak, 2026-10-31T19:02Z.** The read above was repeated with the same
queries (M1 convention 6 with M2's 8-minute margin, each tile mapped to its rule pair by the
`check` label) over `[168h:1m]`. The window starts at 2026-10-24T19:02Z, after B6a's rules first
evaluated at 2026-10-23T19:34Z. Prometheus's oldest sample was older than the window, and
`increase(prometheus_rule_evaluation_failures_total{rule_group=~".*;host-checks"}[168h])` was 0.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| Artifacts Peer Sync | 10036 | 0 | 0 | 0 | 0 | 0 | 0 |
| k3s Telemetry OTLP Door | 10006 | 0 | 0 | 0 | 0 | 0 | 0 |

No Q1 minute is new or unexplained. `ALERTS` holds no series for the four rules.
`homelab_check_success{check=~"sync-artifacts|claude-otel-health",origin="daniel-box"}` holds
10068 one-minute samples for each check, and none of them differs from 1. The largest age of
`homelab_check_last_run_timestamp_seconds` over the window was 539s for `sync-artifacts` and 839s
for `claude-otel-health`, against the rules' 900s and 1500s deadlines.

**The deployed rules, read at 19:02Z.** `kubectl get configmap -n observability prometheus-rules`
holds a `host-checks.yaml` with the same SHA-256 as master's, and 32 `shadow: "true"` labels on
both sides. Its `kuma-cutover.yaml` lacks A6's `Host Packages — Status` term, which master gained
in `4ad2c4091d`, so the live bundle lags master by that one commit. B6a's two terms are on both
sides. `monitor-bridge`'s `verdicts/logs.py` ConfigMap on the cluster lists `sync-artifacts` and
`claude-otel-health` in `KUMA_RETIRING_TAGS`, so the exclusion is deployed ahead of Cutover-1a, as
*Order* requires.

**Inhibit decision: none.** Both NotReporting rules carry `unless on (origin) (up{job="node"} ==
0)`, and a dead node exporter leaves each Failed rule no series, so `ScrapeTargetDown` stays the
one page. Kuma's two tiles had no exporter suppression either.

**Soak effect: none.** All four rules are `warning`, and `renderer_lib/soak.py:longest_for` counts
only critical rules, so Cutover-1b adds no wait to any promotion.

**Routing.** `test_alertmanager_routing_b6a_host_crons.py` is a sibling of the routing module,
which is at its line cap. It walks the four rules through the rendered routing tree: each
reaches Discord alone, muted for the weekly reboot. A rule that regains `shadow` walks to the
blackhole and fails it. The main module's `test_host_sets_names_every_host_check_rule` now lists
the four among the paging host-plane rules.

**The two-hour no-pager bound** counts from the moment the `uptime-kuma` Kustomization reports
Cutover-1a's merge commit, because Kuma stops paging both checks then and every B6a rule is still
shadow. It ends when the `observability` Kustomization reports Cutover-1b's merge commit.
`CheckWithoutPager` pages critical two hours after the last `monitor_status` sample of either
tile. If Cutover-1b has not deployed within two hours, revert Cutover-1a. Its rules are still
shadow, so the revert restores Kuma as the only pager, and the tiles go green on the crons' next
push because both tokens stayed in `secrets.yml` until B6a's token PR. If Cutover-2 has merged by
then, revert it first, so the crons push again.

**Cutover-2 waits for H7.40 slice 4.** The plan's plane for Cutover-2 is the tick: `claude_code`
routes to `initial_setup.yml` (`setup_routing.py --host daniel-box`), and on this tree the two
crons are still tasks of `roles/setup/claude_code/tasks/node_crons.yml`, which
`claude_code_packaged_hosts` does not exclude. [#6678](https://github.com/DanielH2018/server/issues/6678)
(H7.40 slice 4) moves exactly those two crons into `hosts/components/claude-code/`. Cutover-2
edits `sync-artifacts.sh.j2` and `telemetry-health.sh.j2`, which that slice moves, so it is not
written in parallel. After slice 4 the crons ship in `homelab-daniel-box`, and Cutover-2's plane
becomes that package (the builder publishes it and the host's updater installs it), not the
tick. Until Cutover-2 lands, both crons push into deleted tokens: `kuma_push` logs `push failed`,
and `KUMA_RETIRING_TAGS` keeps `swallowed_verdicts` from paging it. No step depends on a tile's
interval, so the delay leaves no check unpaged, but it misses the plan's "land Cutover-2 the same
day".

**Cutover-2: the push stops.** The two producers are timers in daniel-box's host package, moved
there by H7.40's fourth slice ([#6788](https://github.com/DanielH2018/server/pull/6788)), so
this Cutover-2 rides in that PR instead of editing the cron scripts a second time. Cutover-1a
([#6784](https://github.com/DanielH2018/server/pull/6784)) deleted both tiles, and Cutover-1b
([#6787](https://github.com/DanielH2018/server/pull/6787)) made the four rules page.
`sync-artifacts` and `claude-otel-health` drop the `kuma-push-lib.sh` source, the push URL and
the token. Each keeps `write_check_textfile` and logs `status=<up|down> <message>` under its own
syslog tag. The sync script did not log a clean run before, and the heartbeat did not either,
because `kuma_push` logged only a failed push, so the journal line is new for both.

Neither unit loads a credential now, so `package.yml` drops `artifacts_sync_push_token` and
`claude_otel_push_token` from `credentials:`, and the SOPS file needs no `host_secrets.py sync`.
Both keys stayed in `PENDING_RETIREMENT` until B6a's token PR retired them.

**Which plane applies it.** The merge changes the `homelab-daniel-box` package, so daniel-box's
updater installs the two rewritten units and scripts, and `land.sh` waits for that apply. No
tick and no hand apply is involved.

**How to verify the push stopped.** After the package installs and the next run of each timer:

1. `journalctl -t sync-artifacts` and `journalctl -t claude-otel-health` show a `status=up` line
   and no `push failed (... by=kuma ...)` line. Cutover-1a's runs showed that line, because the
   pushes went into a deleted token.
2. `homelab_check_success` for both checks stays 1, and `homelab_check_last_run_timestamp_seconds`
   advances to that run.
3. `unitq cat homelab-sync-artifacts.service` and `unitq cat homelab-claude-otel-health.service`
   show no `LoadCredential=` line.

**The test.** `test_neither_producer_pushes_to_kuma` in
`hosts/lib/tests/test_claude_artifacts_node_timers.py`, which C-6 retired, held both shipped scripts to no Kuma library, `kuma_push` or push URL. Its red case is
`test_a_script_that_pushes_to_kuma_is_flagged`. The two script tests read the verdict back from
the journal line a `logger` stub records, in place of a stubbed `kuma_push`.

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens, and the operator's commit removed the keys from `secrets.yml` on 2026-11-02. A revert of
a cutover above no longer brings back a working tile, because no token is left for it to push
with.

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
4. B6b's token PR, [#6949](https://github.com/DanielH2018/server/pull/6949), retired the keys.

Flux does not promote on Sunday between 02:05 and 08:15 UTC, so a Flux-owned half that merges in
that span waits for 08:15Z, and a revert waits with it.

**Cutover-1b, the rules page.** The combined window's second PR drops `shadow` from the nine rules in `pod-producers.yaml` and
removes the five `CheckWithoutPager` terms of B6b (the three CrowdSec tiles, Configarr Sync and Janitorr Errors). Every rule file is `files/rules/pod-producers.yaml`.
`CrowdSecAppSecVerifyFailed`, `CrowdSecJobStale` and `CrowdSecJobsExporterAbsent` are `critical`; the other six are `warning`.

**Inhibits.** None is added. `CrowdSecJobsExporterAbsent` fires on `absent(up{job="crowdsec-jobs"})` and `ScrapeTargetDown` on `up == 0`, so one dead exporter never fires both arms. The two CronJob rules end in `unless on () (up{job="kube-state-metrics"} == 0)`, which leaves a dead kube-state-metrics to `ScrapeTargetDown`.

The no-pager bound is 2 hours from Cutover-1a's `uptime-kuma` apply. Cutover-1b is `observability`,
which Flux applies through `promote`, so it waits out Sunday 02:05 to 08:15 UTC like the rest of
the window.

**Cutover-2, the Flux-plane producers.** The combined window's last B6b PR stops the pushes.
The crowdsec-jobs sidecar no longer imports `kuma_push` or reads a push URL: `Job.exports`
replaces `Job.push_env`, and `run_job` prints the verdict and records it for `/metrics`. The
`crowdsec-jobs` Secret (`jobs-secret.yaml.j2`) is gone. The `configarr-health` and
`janitorr-health` readers log `status=<up|down>` and patch the verdict ConfigMap, and their
`health-secret.yaml.j2` files and `KUMA_PUSH_URL` variables are gone. The three producers
leave uptime-kuma's `netpol_from` list in `daniel-box.yml`, since none dials Kuma now.

`kuma_push.py` stays in the two health ConfigMaps, because `host_lib.py` imports it at module
level; no reader calls it. The five keys (`crowdsec_remote_allowlist_push_token`,
`monitor_bridge_appsec_push_token`, `monitor_bridge_home_allowlist_push_token`,
`monitor_bridge_configarr_push_token`, `monitor_bridge_janitorr_push_token`) enter
`PENDING_RETIREMENT`, and `B6B_TILE_DELETED_PUSH_TOKENS` is deleted. B6b's token PR,
[#6949](https://github.com/DanielH2018/server/pull/6949), retired the keys.

The five tags stay in `KUMA_RETIRING_TAGS` until C-7, because pods that ran before the apply may
still hold a log line from a refused push. The readers' `status=` lines still count as landed
siblings in `swallowed_verdicts`; that is harmless, since the check pages only on a failed push.

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens, and the operator's commit removed the keys from `secrets.yml` on 2026-11-02. A revert of
a cutover above no longer brings back a working tile, because no token is left for it to push
with.

### B6c registry GC

B6c joined M6 on 2026-11-02 (#6926). D5's registry PRs retired the push store's GC phase
only, and the mirror phases of `registry-gc.sh` kept pushing to the `Registry GC` tile, so
C-1a's gate could never clear while the plan left the push to D5.

**The shadow PR** is the PR that adds this file. It changes four things:

- `registry-gc.sh.j2` sources `homelab-check-lib.sh` and calls
  `write_check_textfile registry-gc` in `finish`, before the push and on every run, with a
  `DEADLINE_S` of 612000, the tile's interval. A run with no token or a refused push still
  writes the file.
- `rules/registry.yaml` gains `RegistryGcFailed` and `RegistryGcNotReporting`, both
  `warning` and `shadow: "true"`, with promtool cases in `registry.test.yaml`.
- `KUMA_RETIRING_TAGS` gains `registry-gc`.
- `kuma-cutover.yaml` gains the `[340h10m]` term for `Registry GC`.

The tick applies the script change (`k8s_host_files`), and Flux applies the two k8s halves
through `promote`. NotReporting fires in shadow from its deploy until the first Sunday run
writes the file.

**Still to come, in order:**

1. The soak read, one week after the first Sunday run that writes the textfile.
2. Cutover-1a: delete `registry-gc.json` and the `^registry-gc$` status-page pattern from
   `uptime-kuma`, and move `registry_gc_push_token` to `PENDING_RETIREMENT`. The tag must
   have been in the deployed `KUMA_RETIRING_TAGS` for a week first.
3. Cutover-1b: drop `shadow` from both rules and remove the `Registry GC` term.
4. Cutover-2: drop the push, `PUSH_TOKEN`, `KUMA_HOST` and the `kuma-push-lib.sh` source from
   `registry-gc.sh.j2`. The healthchecks.io dead-man stays.
   `.claude/hooks/tests/test_block_protected_bash.py` asserts the token in the script's
   protected paths, so the same PR updates it. With no token in the render, the script is no
   longer a STUB, and `probe.py applied` compares it (#6917).
5. The token PR retires `registry_gc_push_token`. C-1a waits for it. The operator waived the
   week after Cutover-1b on 2026-11-02, because Cutover-1a (#6963), Cutover-1b (#6965) and
   Cutover-2 (#6972) had all landed. It is the last retirement the plans schedule, so it also
   deletes `scripts/secrets_mgmt/pending_retirement.py`, its readers in `consumers.py`,
   `cluster_vars.py` and `cluster_vars_check.py`, its `HOST_INPUTS` entry in
   `scripts/renderer/renderer_lib/host_inputs.py`, and the tests that hold it. M5's plan
   scheduled that deletion for its PR 5. #6371 closed with the clause moved here.

**Cutover-1a, the tile goes** (branch `m6-b6c-cutover-1a-tiles-go`). It deletes the `registry-gc`
declaration from `static-monitors.yaml.j2` and the `^registry-gc$` status-page pattern.
"Backups & Storage" keeps `match: []`, because four monitor-bridge rows pin themselves into it.
`registry_gc_push_token` enters `PENDING_RETIREMENT`, because no cluster render references it
once the tile goes. `registry-gc.sh.j2` keeps pushing into the deleted token until Cutover-2.
`KUMA_RETIRING_TAGS` lists `registry-gc`, so `swallowed_verdicts` does not page the refused push.
Both rules stay `shadow`, so this PR adds no pager and removes one.

**What the tile's removal moved in the tests.** The floors that counted the tile fell by one:
the monitor census (`>= 28` to `>= 27`), the declaration index (`> 30` to `> 29`), the renderer's
index (`> 25` to `> 24`), and the three push-tile or token-name floors (`>= 20` to `>= 19`). The
tile was a named member of two censuses, so `GATED_MEMBERS` swaps it for `aide-daniel-pi.json`,
`_INCIDENT_TILE` becomes `aide-daniel-pi`, and `KNOWN_IDS` drops it. The worst-run guard read the
tile's interval twice. It now reads the script's `DEADLINE_S`, which the same guard already
equated to the rule literals. `test_the_deleted_b6c_tile_is_not_declared` fails if the tile is
declared again.

**The gates are not read, and cannot pass yet.** Read on 2026-11-02 at 18:29Z:

- `homelab_check_last_run_timestamp_seconds{check="registry-gc",origin="daniel-box"}` reads
  1793643955 (2026-11-02T18:25:55Z) and `homelab_check_success` reads 1. The textfile exists, but
  it came from a run outside the Sunday 04:20 schedule. The first scheduled run that writes it is
  2026-11-08, so the soak read is due on or after 2026-11-15.
- The live `monitor-bridge-script-verdicts-logs` ConfigMap lists `registry-gc` in
  `KUMA_RETIRING_TAGS`. #6931 landed on 2026-11-02, so the tag has been deployed for a week on
  2026-11-09 at the earliest, and Cutover-1a waits for that.
- `monitor_status{monitor_name="Registry GC"}` reads 1, and `ALERTS{alertname=~"RegistryGc.*"}` is
  empty. The tile is live and both rules are quiet.

The operator reads the soak, with M2's 8-minute margin over `[168h:1m]`, before merging. The
two-hour no-pager bound counts from the `uptime-kuma` apply of this PR. It ends at the
`observability` apply of Cutover-1b. If Cutover-1b has not applied by then, revert this PR. Its
rules are still `shadow`, so the revert restores Kuma as the only pager, and the tile goes green
on the next push because the token stays in `secrets.yml` until the token PR. A Cutover-2 that
has merged is reverted first, so the cron pushes again.

**Cutover-1b, the rules page** (branch `m6-b6c-cutover-1b-rules-page`, stacked on Cutover-1a).
`RegistryGcFailed` and `RegistryGcNotReporting` drop `shadow`, and the `kuma-cutover` group
loses B6c's `Registry GC` term. The group stays, because B9's six terms and Phase A's terms
remain. Both rules are `warning`, which posts to Discord alone, the channel mix the tile had.

**Routing.** `test_alertmanager_routing_b6c_registry_gc.py` is a sibling of the routing module,
which is at its line cap. It walks both rules through the rendered routing tree: each reaches
Discord alone, muted for the weekly reboot. A rule that regains `shadow` walks to the blackhole
and fails it. The main module's censuses read no `registry.yaml` rule, so none needs a row.

**Inhibits: none added.** `RegistryGcNotReporting` ends in `unless on (origin) (up{job="node"}
== 0)`, and a dead node exporter leaves `RegistryGcFailed` no series, so `ScrapeTargetDown`
stays the one page. The generic `HostCheckOverdue` deadman is already muted beside the
`NotReporting` pair by the inhibit on `equal: [check, origin]`.

**Soak effect: none.** Both rules are `warning` and carry no `verify` label, so neither the
health gate nor the soak reads them, and `renderer_lib/soak.py:longest_for` counts critical
rules only. A firing `RegistryGcFailed` therefore does not block `registry`'s `promote`.

**Precondition.** `RegistryGcNotReporting` pages when the textfile is older than 612000s, which
is 7 days 2 hours. The file was last written 2026-11-02T18:25:55Z, so it ages out at
2026-11-09T20:25Z unless a run refreshes it. The scheduled run of 2026-11-08 04:20 comes first.
A merge before a file exists at all pages at once, because the rule's absent arm fires on no
series.

**Cutover-2, the push stops** (branch `m6-b6c-cutover-2-setup-plane`, stacked on Cutover-1b).
`registry-gc.sh.j2` loses `PUSH_TOKEN`, `KUMA_HOST`, `KUMA_RESOLVE_IP`, the `kuma-push-lib.sh`
source and the push call. It keeps the textfile write and the healthchecks.io dead-man.

**Which plane applies it.** `k8s_host_files` is a setup role, and `setup_routing.py --host
daniel-box` places it on the tick (`initial_setup.yml --tags k8s_host_files`, `on_tick_host`
true). `deploy_changes.setup_tags_for` maps the script's path to `k8s_host_files`, with
`broad_setup` set and `broad_manual` clear. No hand apply is owed, and the playbook entry is
gated to daniel-box, so no other host owes the role.

**Four things follow from a render that holds no token.**

- `tasks/registry.yml` loses the script task's `no_log` and the three tasks that recorded the
  script's SHA-256, because the render holds no STUB now and `probe.py applied` hashes it. The
  old record file stays on daniel-box, and nothing reads it.
- The script was the last secret-bearing host script, so the census behind the hook's arm 2
  (`secret_bearing_host_paths`) is empty. The hook's tests run over a fixture checkout whose
  Ansible tree installs a script with a fake token. Each flagged and clean pair stays.
- These assertions retired because their subject is gone: `MUST_FIND`'s `registry-gc.sh` entry and
  its test, `test_tiering_the_address_did_not_drop_its_script_from_the_census`, the
  `len(census) >= 1` line of the world-readable test, `KNOWN_SECRET_SCRIPTS` with
  `test_the_census_finds_the_known_members`, and the two hook tests that derived a set from the
  hook's own tree (`..._in_a_subdirectory_of_the_hooks_checkout_...` and `..._in_no_checkout_...`).
  `test_the_k8s_host_files_apply_writes_the_sha256_record` retired with the record task.
- The push corpus of `test_cron_scripts_publish_via_pr.py` falls from 5 to 4 templates. Its named
  members become the aide and rkhunter checks, and `_MIN_CORPUS` stays 3.

**How to verify.** After the tick applies it and the next run: `journalctl -t registry-gc` shows
no `push failed` line, `probe.py applied` reads `/usr/local/bin/registry-gc.sh` as `ok`, and
`homelab_check_last_run_timestamp_seconds{check="registry-gc"}` advances. The token PR retires
`registry_gc_push_token`.

**The token PR, the last retirement.** The agent's half adds `registry_gc_push_token` to
`scripts/secrets_mgmt/git_dates.py:RETIRED` and drops it from `CROSS_HOST_PUSH_TOKENS`. It deletes
`PENDING_RETIREMENT` with `scripts/secrets_mgmt/pending_retirement.py`, the three readers, the
`HOST_INPUTS` entry and the `pending` seam on `cluster_vars.write`. Three tests retire with
their subject: `test_a_write_keeps_a_pending_retirement_key_and_drops_an_ordinary_one`,
`test_a_retired_key_has_left_the_pending_allowance` and
`test_consumer_tags_registry_gc_token_is_manual`. The operator's commit removes the key from
`secrets.yml`, its row in `ansible/secret_rotation.yml` and its line in the cluster files. With
the key gone, `git grep -n registry_gc_push_token -- ansible/roles ':!*/tests/*'` returns
nothing, which is C-1a's quoted gate.

### B7 pods

B7's soak read, [#6729](https://github.com/DanielH2018/server/issues/6729): B7's shadow PR
committed to master as `8f78be9641` at 2026-10-24T03:10Z. The `pods` group is new in that PR,
so its first evaluation marks the start of B7's Prometheus rules: 2026-10-24T03:16:49Z, read from
`min_over_time(timestamp(prometheus_rule_group_last_evaluation_timestamp_seconds{rule_group=~".*;pods"})[200h:1m])`.
The read used M1 convention 6's two queries over `[168h:1m]` ending at 2026-10-31T16:22Z, with
M2's 8-minute margin (*Which soak minutes count as explained* in M2). The window starts at
2026-10-24T16:22Z, after the first evaluation, so every tile has the full 168 hours.
Prometheus's oldest sample was 21.7 days old, and
`increase(prometheus_rule_evaluation_failures_total{rule_group=~".*;pods"}[168h])` was 0.

Each tile maps to the rules that `pods.yaml`'s rule comments assign to its producer. The two
cloudflare-ddns tiles split by the `deployment` label:

- `Arr Auto-Block`: `ArrAutoBlockFailing`, `AutofixBridgeDown`, `AutofixBridgeStale`.
- `Cloudflare DDNS Direct`: `CloudflareDdnsUnavailable{deployment="cloudflare-ddns-direct"}`,
  plus the LogQL rule `CloudflareDdnsDirectUpdateSilent` in loki-homelab's `homelab-logs.yaml`.
- `Cloudflare DDNS Proxied`: `CloudflareDdnsUnavailable{deployment="cloudflare-ddns-proxied"}`,
  plus the LogQL rule `CloudflareDdnsProxiedUpdateSilent`.
- `WG Pi Peer Backup`: `PiPeerBackupFailed`, `PiPeerBackupNotReporting`.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| Arr Auto-Block | 10042 | 0 | 1 | 0 | 0 | 1 | 1 |
| Cloudflare DDNS Direct | 10036 | 2 | 0 | 2 | 2 | 0 | 0 |
| Cloudflare DDNS Proxied | 10036 | 7 | 0 | 7 | 7 | 0 | 0 |
| WG Pi Peer Backup | 7602 | 0 | 0 | 0 | 0 | 0 | 0 |

The firing column counts `ALERTS` in Prometheus. The two LogQL rules never reach `ALERTS` (M5
open question 1), so each was replayed through `probe.py loki-query` at a 1-minute step over the
7 days. Neither expression held at any step. The same replay with the range cut to `[10m]`
returned a hit for the proxied pod at 2026-10-28T00:49Z, mid-window, which shows the replay reads
the whole week. Both Deployments kept at least one available replica all week, so
`CloudflareDdnsUnavailable` had nothing to fire on.

Q1 ±8m is 0 for Arr Auto-Block and WG Pi Peer Backup. The 9 Q1 minutes of the two cloudflare-ddns
tiles fall in two episodes, all times UTC:

- **Both cloudflare-ddns tiles, 2026-10-25 08:19 to 08:20.** The weekly reboot's maintenance
  window held both tiles at maintenance (3) until 08:18. Kuma logged `Monitor #381 'Cloudflare
  DDNS Proxied': Failing: No heartbeat in the time window` at 08:17:41, and the same for #388
  Direct at 08:17:44. Both pods had logged `already up to date` and `Successfully sent a ping to
  Uptime Kuma` at 08:14:48, inside the tiles' 360-second interval, and logged both again at
  08:19:48. The tiles read up from 08:21. No other tile went DOWN at 08:19. The two drift tiles
  that read DOWN then were in B1's real drift episode. The updaters ran every 5 minutes through
  the episode, so the tile measured Kuma's maintenance exit rather than the producer. The LogQL
  rules read the update line, which never stopped for 15 minutes.
- **Cloudflare DDNS Proxied, 2026-10-30 15:18 to 15:22.** The proxied pod logged `Cloudflare
  trace IPv4 detection timed out before any endpoint returned a valid response` and `No valid
  IPv4 addresses were detected` at 15:16:47. favonia pinged Kuma with the failure, so the tile
  read DOWN. The next cycle, at 15:21:42, logged `already up to date`. One failed cycle is the
  shape the LogQL rule deliberately does not page: its 15-minute window needs three failed cycles
  in a row, where the tile pages on one (*B7, the pods* in M6, and the rule's comment in
  `homelab-logs.yaml`). The direct pod's cycles succeeded through the same minutes.

The one Q2 minute is `ArrAutoBlockFailing` at 2026-10-25 07:46. The rule was right, and the tile
had no data. The node reboot restarted autofix-bridge at 07:39:48. Its first cycle, at 07:39:53,
logged `DOWN autofix-bridge error: <urlopen error [Errno -3] Try again>`, a DNS failure before
the cluster's resolver was back. All three attempts to push that DOWN failed, the last at
07:40:58. Kuma was restarting then, as `process_start_time_seconds` moving to 07:41:23 shows.
`homelab_check_success` read 0 from 07:44 to 07:46, and the 07:45:58 cycle logged `OK queue
clean (Sonarr, Radarr)`. The tile had no series from 07:40 to 07:46 and read maintenance from
07:47, so Q2 counts the minute against a tile that read nothing. Q2 ±8m restricted to minutes
the tile read up is 0.

The episode needs no rule change for Cutover-1b. `ArrAutoBlockFailing` is `warning`, so once it
drops `shadow` it takes the default Discord route. In `alertmanager-secret.yaml.j2` that route
carries `mute_time_intervals: [weekly-reboot]`, as every paging route except the Watchdog does.
The mute derives from the reboot cron's group vars and spans Sunday 07:25 to 08:15 UTC: a
5-minute lead before the 07:30 cron, a 5-minute shutdown delay and a 40-minute recovery
allowance. 2026-10-25 was a Sunday, so the 07:46 alert falls inside the mute, and live it would
have paged nobody, as Kuma's maintenance window paged nobody.
`ansible/tests/services/test_alertmanager_reboot_mute.py` holds the mute equal to Kuma's window.
A failed cycle outside the reboot window pages on one sample, which matches the tile's
`max_retries` of 0.

The samples each tile lacks from 10080 are Kuma's. `process_start_time_seconds{job="uptime-kuma"}`
moved eleven times in the window, and Prometheus itself had no samples from 07:40 to 07:42 on
2026-10-25 and from 00:46 to 00:52 on 2026-10-28. A push tile has no series after a Kuma restart
until its producer's next push. The 5-minute producers lose a minute or two per restart. WG Pi
Peer Backup's CronJob succeeds once a night at about 04:00, so each restart blanks that tile
until the next run. The five gaps begin at 07:40 on 2026-10-25, 00:45 on 2026-10-28, 21:33 on
2026-10-29, 20:35 on 2026-10-30 and 12:37 on 2026-10-31. Together they hold the tile's missing
2476 minutes. Through them the producer kept working.
`changes(kube_cronjob_status_last_successful_time{cronjob="pi-peer-backup"}[168h])` is 7, the
age of the last success never exceeded 86433 seconds, and the verdict annotation never read
`down`. Arr Auto-Block's 44 maintenance minutes are the weekly reboot window, not DOWN.

**The deployed rules, read at 16:22Z.** `kubectl get configmap -n observability
prometheus-rules -o jsonpath='{.data.pods\.yaml}'` is byte-identical to `pods.yaml` on master,
and `prometheus_rule_group_rules` reads 6 for the group. The `homelab-logs.yaml` key of `kubectl
get configmap -n homelab loki-homelab-rules` is byte-identical to master. All eight B7 rules carry
`shadow: "true"`, and no B7 alert was pending or firing.

No minute is unexplained, so no rule changes. The soak does not block Cutover-a, which B7 runs
as Cutover-1a (*Every other producer* in M6).

### B7 cutover

B7's cutover is three stacked PRs, all `owner: flux` roles, so each merge is the apply once
Flux reports the commit: Cutover-1a ([#6779](https://github.com/DanielH2018/server/pull/6779),
the four tiles go), Cutover-1b
([#6786](https://github.com/DanielH2018/server/pull/6786), the rules page) and Cutover-2 (this
PR, the push stops). The record sits in the last PR so that the first two stay free of
conflicts on this file.

**The soak re-read, 2026-10-31T19:02Z.** The read repeated the one above with the same method
(M1 convention 6 with M2's 8-minute margin, the tiles mapped to the same rules) over
`[168h:1m]`, so the window began 2026-10-24T19:02Z, after the `pods` group first evaluated. The
counts equal the first read's.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| Arr Auto-Block | 10037 | 0 | 1 | 0 | 0 | 1 | 1 |
| Cloudflare DDNS Direct | 10035 | 2 | 0 | 2 | 2 | 0 | 0 |
| Cloudflare DDNS Proxied | 10035 | 7 | 0 | 7 | 7 | 0 | 0 |
| WG Pi Peer Backup | 7442 | 0 | 0 | 0 | 0 | 0 | 0 |

Every Q1 and Q2 minute is one of the episodes above, and the last 170 minutes hold no DOWN
minute on any of the four tiles. Kuma restarted again at 17:16:31Z, after the first read
(`process_start_time_seconds` moved 12 times in the window), which accounts for the lower
sample counts. The two LogQL rules were replayed through `probe.py loki-query --json --step 1m`
in eight one-day chunks, and neither held at any step. The control is the proxied expression cut
to `[10m]`, which held for 3 steps in the chunk containing 2026-10-28T00:49Z.

**Live state before Cutover-1a.** The `pods.yaml` and `homelab-logs.yaml` keys of the deployed
ConfigMaps hashed equal to master, and every B7 rule carried `shadow`. The live
`monitor-bridge-script-verdicts-logs` ConfigMap hashed equal to `verdicts/logs.py` on master,
which lists `pi-peer-backup` in `KUMA_RETIRING_TAGS`, so the *Order* gate for Cutover-1a held.

**No new page in the gap.** `WorkloadLogErrors` pages on more than 20 lines an hour that match
`(?i)(panic:|fatal|traceback|out of memory)` for one container. The lines the three pushers
write into a deleted token are `push failed (...)` from `kuma_push.py` and favonia's `Failed to
send HTTP(S) request to Uptime Kuma`. Neither holds a pattern word, and the pattern matched
none of the three containers' lines over the 24 hours before Cutover-1a.

**Inhibit decision.** `AutofixBridgeDown` inhibits `ScrapeTargetDown{job="autofix-bridge"}` with
no `equal` list, in `alertmanager-secret.yaml.j2`, because both read `up == 0` for the one job
and A3's task 6 defers the inhibit to this batch's Cutover-1b. `test_alertmanager_autofix_inhibit.py`
holds it. No other inhibit: a dead cloudflare-ddns pod fires `CloudflareDdnsUnavailable` and
`K8sDeploymentZeroAvailable`, as the tile and that rule paged together before, and the
`PiPeerBackup` pair has nothing to hold. The `.+NotReporting` inhibit of `HostCheckOverdue`
cannot catch `PiPeerBackupNotReporting`, which carries neither `check` nor `origin`.

**Soak effect.** `longest_for()` gains `autofix-bridge` and `cloudflare-ddns` at 600 seconds each
at Cutover-1b. The other 19 keys are unchanged.

**How paging is verified.** The four Prometheus rules through `ALERTS`, `probe.py health
autofix-bridge` and `probe.py health cloudflare-ddns`. The two Loki rules never reach `ALERTS`,
so the check is A7's list: the ConfigMap hash, `loki_prometheus_rule_group_rules` at 9, no
evaluation failures, a fresh last-evaluation timestamp and no notification errors. Loki and
Alertmanager each restart once at Cutover-1b.

**The no-pager bound and the revert order.** The window opens when the `uptime-kuma` Kustomization
reports Cutover-1a and closes when `observability` and `loki-homelab` report Cutover-1b. If
Cutover-1b has not deployed within two hours, revert Cutover-1a. `CheckWithoutPager` names the
three short-interval tiles two hours after their last sample and `WG Pi Peer Backup` after
120h10m, so the guard is a backstop for that tile, not its bound. After Cutover-2 merges, revert
in reverse order: Cutover-2, Cutover-1b, then Cutover-1a. Reverting Cutover-1a alone after
Cutover-2 restores tiles that no push reaches. All three reverts worked only until the token PR.

**Cutover-2.** autofix-bridge stops pushing and drops the `kuma_push.py` staging entry, which
C-3a requires. The two cloudflare-ddns Deployments drop favonia's Kuma ping setting and the
Secret's two token keys. pi-peer-backup keeps its verdict line, its ConfigMap export and its
Healthchecks.io ping, and drops the Kuma request and the `push_url` key. The four tokens
(`arr_autoblock_push_token`, `cloudflare_ddns_direct_push_token`,
`cloudflare_ddns_proxied_push_token` and `monitor_bridge_pi_peers_push_token`) join
`PENDING_RETIREMENT`, and the three that `standalone_secret_keys.json` listed leave it. B7's token PR,
[#6949](https://github.com/DanielH2018/server/pull/6949), retired the keys. `pi-peer-backup` stays in `KUMA_RETIRING_TAGS` until C-7, and the
uptime-kuma `netpol_from` entries for the four callers stay until C-1 removes the callers policy.

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens, and the operator's commit removed the keys from `secrets.yml` on 2026-11-02. A revert of
a cutover above no longer brings back a working tile, because no token is left for it to push
with.

### B8 probes

Soak read, [#6625](https://github.com/DanielH2018/server/issues/6625): B8's
shadow PR merged as `50f2b2c410` at 2026-10-23T19:28Z. Prometheus first scraped the
`blackbox-extra` job at 19:39:56Z, so the window starts there. The read used M1
convention 6's two queries over `[168h:1m]` ending at 2026-10-30T19:39:56Z, with M2's
8-minute margin (*Which soak minutes count as explained* in M2). Each tile was mapped to its shadow rule by the
target's `service` label. Loki maps to M5's `LokiHomelabDown`, and Pi Pressure maps to
`PiPortDead` plus A1's Prometheus half of the row: `PiLoadHigh`, `PiMemoryLow`,
`PiDiskFull`, `PiFilesystemReadOnly` and `PiMetricsMissing` in `hosts.yaml`.
`PiUndervoltage` belongs to `check_host_temp`, so it is not part of the mapping.

- Query 1 returned no minutes for 14 of the 15 tiles. SLZB-06M Coordinator read DOWN
  for 38 minutes, and `ExtraProbeFailed` covered every one.
- Pi Pressure read DOWN for 50 minutes, all on 2026-10-26 from 03:43 to 04:37. Every
  bridge cycle in that span blamed load5, and the port arm read "2 pi port(s)
  listening", so `PiPortDead` alone covers none of them. With A1's rules counted, 3
  minutes remain, 04:35 to 04:37. The Pi rebooted at 04:32:54
  (`node_boot_time_seconds`), so load5 fell to 0.17 per core and `PiLoadHigh`
  cleared. The tile held the bridge's last DOWN push until the next 300s cycle, which
  the margin absorbs.
- Query 2 found three episodes. On 2026-10-23 from 19:42 to 19:58, the Email-to-RSS
  probe failed over IPv6 (`probe_ip_protocol` 6) while Kuma read up. `efd7fda5a0`
  set `preferred_ip_protocol: ip4` and committed at 19:52:54, and the probe read 4
  and up from 19:59. That episode was a fault in the probe, fixed in the window.
- On 2026-10-25 from 14:25 to 14:28, the four `.local.`
  probes behind Traefik (livesync, scrutiny, homelab-mcp, the edge all-clear) and
  all 26 `blackbox-ingress` targets timed out at 9.5s. M4's live
  `IngressProbeFailed` and `IngressProbeFailedCritical` fired for 19 of those
  targets, and A4's read records Traefik 5xx in the same window. Kuma's tiles declare
  no `timeout` and retry twice, so Kuma tolerated the stall. The stall was a real edge
  fault, not a rule defect.
- The third episode is `PiLoadHigh` on 2026-10-26 at 03:33 and 03:34, outside the
  margin. Load5 crossed 1.5 per core between 03:31 and 03:34 and fell back. The
  bridge's first DOWN cycle came at 03:41, so its 300s samples missed the crossing.
  The rule has no `for:`, which matches the bridge's single-sample page.

No minute is unexplained, so no rule changes, and the soak does not block Cutover-a.

B8's cutover is three stacked PRs that merge in this order, not the plan's single Cutover-a.
uptime-kuma and monitor-bridge are both `owner: flux`, so no deploy orders a tile deletion
before a row deletion, and the batch uses the owner-independent fallback of *When D5 flips a
role M6 edits*, as A1 to A5 did:

1. Cutover-a1, [#6711](https://github.com/DanielH2018/server/pull/6711): the 14 hand-written
   tiles leave `static-monitors.yaml.j2`, and an exclusion in its `CHECKS` loop drops `Pi
   Pressure`. Every rule stays shadow. Its live read is `probe.py metric 'monitor_status{monitor_name=~"..."}'`
   over the 15 names, which must return no series.
2. Cutover-a2, [#6712](https://github.com/DanielH2018/server/pull/6712): the `pi_pressure` row,
   `check_pi_pressure`, `with_pi_ports`, the two Pi verdicts, the `PI_*` keys and
   `EXPORTER_DEPENDENT["node-pi"]` go, and `monitor_bridge_pi_push_token` enters
   `PENDING_RETIREMENT`. It merges only after the read above passes.
3. Cutover-b, the PR that adds this paragraph: the four `extra-probes.yaml` rules and the five Pi
   pressure rules in `hosts.yaml` drop `shadow`, and the `kuma-cutover` group loses B8's 15
   terms. `PiHostDown` gains an Alertmanager inhibit on `PiPortDead`.

**The re-read of the soak, 2026-10-31T13:35Z.** The read above was repeated with the same
queries (M1 convention 6 with M2's 8-minute margin, each tile mapped to its rule by the target's
`service` label) over `[168h:1m]`. Prometheus's oldest sample was 21.6 days old, and
`increase(prometheus_rule_evaluation_failures_total{rule_group=~".*(extra-probes|hosts|scrape-targets)"}[168h])`
was 0 for all three groups. The window starts at 2026-10-24T13:35Z, after `blackbox-extra`
first scraped, so the 2026-10-23 Email-to-RSS IPv6 episode is no longer in it.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| Daniel Pi Host | 10062 | 0 | 0 | 0 | 0 | 0 | 0 |
| Daniel Pi WG Easy | 10059 | 0 | 0 | 0 | 0 | 0 | 0 |
| k3s LiveSync | 10065 | 0 | 2 | 0 | 0 | 2 | 2 |
| k3s Mosquitto (VIP) | 10061 | 0 | 0 | 0 | 0 | 0 | 0 |
| k3s Jellyfin (VIP) | 10060 | 0 | 0 | 0 | 0 | 0 | 0 |
| SLZB-06M Coordinator | 10064 | 38 | 38 | 0 | 0 | 0 | 0 |
| k3s nut (upsd) | 10059 | 0 | 0 | 0 | 0 | 0 | 0 |
| k3s homelab-mcp (edge gate) | 10065 | 0 | 2 | 0 | 0 | 2 | 2 |
| Loki | 10065 | 0 | 0 | 0 | 0 | 0 | 0 |
| k3s Scrutiny | 10065 | 0 | 2 | 0 | 0 | 2 | 2 |
| iCal Proxy k8s | 10065 | 0 | 0 | 0 | 0 | 0 | 0 |
| Pi-hole k8s DNS | 10065 | 0 | 0 | 0 | 0 | 0 | 0 |
| Email-to-RSS (Worker) | 10059 | 0 | 0 | 0 | 0 | 0 | 0 |
| Homelab Edge (all-clear) | 10057 | 0 | 1 | 0 | 0 | 1 | 1 |
| Pi Pressure | 10034 | 50 | 59 | 3 | 0 | 12 | 2 |

Q1 ±8m is 0 for all 15 tiles. The 3 strict Q1 minutes of Pi Pressure sit inside 2026-10-26 04:30
to 04:38, the span above where the Pi rebooted at 04:32:54. SLZB-06M's 38 DOWN minutes are all
inside `ExtraProbeFailed` (strict Q1 0). The Q2 minutes are the 2026-10-25 14:25 to 14:28 Traefik
edge stall on the four `.local.` probes (a `[90m:1m]` window ending 15:00 holds all 7) and
`PiLoadHigh` at 03:33 and 03:34 on 2026-10-26 (a `[6m:1m]` window ending 03:36 holds both).

**Loki maps to `ScrapeTargetDown`, not `LokiHomelabDown`.** The mapping above names
`LokiHomelabDown`, which A3's Cutover-b replaced with `ScrapeTargetDown` (`service:
loki-homelab`, `for: 10m`, no `shadow`). The Loki row uses that rule with `job="loki-homelab"`.
Without the job selector the rule's `alloy` target adds 3 Q2 minutes, which belong to the
alloy scrape on daniel-server and not to the Loki tile. Loki has no `shadow` to drop, so only its
`CheckWithoutPager` term goes.

**The deployed rules, read at 13:36Z.** `kubectl get configmap -n observability prometheus-rules`
holds `extra-probes.yaml`, `hosts.yaml` and `kuma-cutover.yaml` byte-identical to master.
`prometheus_rule_group_rules` reads 5, 34 and 1, and no B8 shadow alert was firing.

**Inhibit decision: B8 adds one.** The plan turns `EXPORTER_DEPENDENT` into an inhibit sourced on
`ScrapeTargetDown`, with `equal: [job, instance]`. For B8 that cannot match, and the rules that
need it are few:

- The five Pi pressure rules and `PiUndervoltage` read series that a dead scrape drops, or start
  from `up == 1`, so a dead Pi leaves them quiet beside `ScrapeTargetDown`.
- `PiPortDead` has no `up` gate, because a blackbox probe's `probe_success` exists while the
  target is down. It fires once per published port (alloy and wg-easy), 10 minutes after the Pi
  dies. The bridge's port arm sat inside `pi_pressure`, which `EXPORTER_DEPENDENT["node-pi"]`
  suppressed while the Pi's exporter was down, so the host paged alone. Without an inhibit a dead
  Pi pages two more groups than Kuma's three tiles did (`group_by: [alertname, service]`).
- `PiHostDown` (`up{job="node-pi"} == 0`, `for: 2m`) fires first and carries `instance` `<pi>:9100`,
  where `PiPortDead` carries `<pi>:<port>` and a different `job`. No `equal` list can match, and
  the estate has one Pi, so the inhibit has none.

The rule is `source_matchers: alertname="PiHostDown"`, `target_matchers: alertname="PiPortDead"`.
`ExtraProbeFailed{service="wg-easy"}` is not held, because Kuma's WG Easy tile paged beside the
ping tile. The change is in `alertmanager-secret.yaml.j2`, so the Alertmanager Secret changes and
Alertmanager rolls when `observability` applies. `test_alertmanager_pi_inhibit.py` evaluates the
rendered rule: it accepts a held port and rejects the rule with its source removed (`2 failed, 3
passed`).

**Soak effect: none.** `renderer_lib/soak.py:longest_for` counts the `for:` of every unshadowed
critical rule against its `service` label. `ExtraProbeFailedCritical` is the one B8 rule that is
critical, and it carries no literal `service`, so it counts under the `None` key at 120 seconds.
Read before and after this PR with `longest_for()`, the map is identical, because other rules
already put `None` at 120. The `Lander deadlines cover the soak` hook and
`test_lander_deadlines_cover_the_soak.py` pass.

**Routing.** `test_alertmanager_routing_b8_probes.py` is a sibling of the routing module, which is
at its line cap. It walks the five `extra-probes` rules through the rendered routing tree:
`ExtraProbeFailedCritical` reaches Discord and email, and the others reach Discord alone, all
muted for the weekly reboot. A rule that regains `shadow` walks to the blackhole and fails it.
A1's `test_alertmanager_routing_a1_hosts.py` now expects the five Pi pressure rules in the paging
set.

The two-hour no-pager bound counts from the moment the `uptime-kuma` Kustomization reports
Cutover-a1's merge commit, because Kuma stops paging the 15 checks then and every B8 rule is
still shadow. It ends when the `observability` Kustomization reports Cutover-b's merge commit.
Cutover-a2's apply falls inside it. `CheckWithoutPager` pages critical two hours after the last
`monitor_status` sample of any of the 15 tiles. If the bound is exceeded, revert Cutover-a2 first
when it has merged, so the bridge pushes again, and then Cutover-a1, so the tiles return.
Reverting Cutover-a1 alone after Cutover-a2 restores a Pi Pressure tile that no push reaches, and
it goes DOWN. Both reverts worked only until B8's token PR,
[#6949](https://github.com/DanielH2018/server/pull/6949), retired the key.

**Token PR.** [#6949](https://github.com/DanielH2018/server/pull/6949) retired this batch's push
tokens, and the operator's commit removed the keys from `secrets.yml` on 2026-11-02. A revert of
a cutover above no longer brings back a working tile, because no token is left for it to push
with.

### B9 integrity scans

B9's soak read, [#6730](https://github.com/DanielH2018/server/issues/6730): B9's shadow PR
merged as `ab283899e` at 2026-10-24T23:17Z. The `host-checks` group predates B9, so its first
evaluation does not mark the start of B9's rules. `prometheus_rule_group_rules` for the group
rose from 37 to 57 at 2026-10-24T23:38Z. That step equals the 20 alerts `ab283899e` added, and
twelve of them are B9's, so B9's rules first evaluated then. The read ended at
2026-10-31T16:18Z, 160.7 hours after the first evaluation. All six tiles therefore have a
160-hour window, not 168. The read used M1 convention 6's two queries over `[160h:1m]`, with
M2's 8-minute margin (*Which soak minutes count as explained* in M2). Prometheus's oldest
sample was 21.7 days old, and
`increase(prometheus_rule_evaluation_failures_total{rule_group=~".*;host-checks"}[160h])` was 0.
The B9 rules in `host-checks.yaml` are unchanged since `ab283899e`.

The shadow PR's rule comment in `host-checks.yaml` gives each host the `Failed` and
`NotReporting` pair for each scan, keyed by `service` (the host):

- `AIDE Integrity (<host>)`: `AideCheckFailed` and `AideCheckNotReporting`.
- `rkhunter Scan (<host>)`: `RkhunterScanFailed` and `RkhunterScanNotReporting`.

| Tile | Samples | DOWN min | Shadow firing min | Q1 strict | Q1 ±8m | Q2 strict | Q2 ±8m |
|---|---|---|---|---|---|---|---|
| AIDE Integrity (daniel-box) | 2694 | 0 | 1645 | 0 | 0 | 1645 | 1645 |
| rkhunter Scan (daniel-box) | 2609 | 0 | 4409 | 0 | 0 | 4409 | 4409 |
| AIDE Integrity (daniel-server) | 2713 | 0 | 0 | 0 | 0 | 0 | 0 |
| rkhunter Scan (daniel-server) | 2610 | 0 | 0 | 0 | 0 | 0 | 0 |
| AIDE Integrity (daniel-pi) | 0 | 0 | 9574 | 0 | 0 | 9574 | 9574 |
| rkhunter Scan (daniel-pi) | 2587 | 0 | 9574 | 0 | 0 | 9574 | 9574 |

**Q1 is 0 for every tile, but the zero proves little.** No tile read DOWN for a single minute,
because a weekly push tile cannot read DOWN inside this window:

- Uptime Kuma restarted ten times in the window. `process_start_time_seconds{job="uptime-kuma"}`
  moved on 2026-10-25 at 07:41, 13:14 and 18:08, on 2026-10-28 at 00:51, on 2026-10-29 at 21:32,
  on 2026-10-30 at 20:33 and 23:19, and on 2026-10-31 at 01:06, 02:42 and 12:36.
- After a restart, a push tile has no `monitor_status` series until its producer's next push. Its
  626400s or 612000s timer also starts again from the restart. The longest gap between two
  restarts was about 2.3 days, so no tile ever reached its interval without a push.
- That is why each tile holds at most 2713 samples of 9600 minutes. Every AIDE tile lost its series
  at the 2026-10-28 restart and every rkhunter tile at the 2026-10-29 restart, and none returned
  before its next weekly run.

Q2 strict and Q2 ±8m are equal, because no tile read DOWN, so the margin matches nothing. Every Q2
minute is a `NotReporting` rule. No `Failed` rule fired, because every scan that wrote a
textfile wrote `homelab_check_success` 1. Each scheduled run reconciles as follows:

| Run (UTC) | Host | Journal `status=` line | Tile returns | Textfile `last_run` | Rule |
|---|---|---|---|---|---|
| AIDE, Mon 2026-10-26 03:00 | daniel-server | 03:30:38 `status=up`, run 30m | 03:32, up | 03:30:38 | quiet |
| AIDE, Mon 2026-10-26 03:00 | daniel-box | 03:49:17 `status=up`, run 48m | 03:51, up | 03:49:17, first write | `NotReporting` clears 03:50 |
| AIDE, Mon 2026-10-26 03:00 | daniel-pi | not shipped to Loki | no push | none | `NotReporting` firing |
| rkhunter, Wed 2026-10-28 02:00 | daniel-server | 02:02:09 `status=up`, run 2m | 02:03, up | 02:02:09 | quiet |
| rkhunter, Wed 2026-10-28 02:00 | daniel-box | 02:02:38 `status=up`, run 2m | 02:04, up | 02:02:38, first write | `NotReporting` clears 02:04 |
| rkhunter, Wed 2026-10-28 02:00 | daniel-pi | not shipped to Loki | 02:26, up | none | `NotReporting` firing |

The episodes, all times UTC:

- **daniel-box, both tiles: the rule fired from deploy until the first run.** The textfile series
  for `aide-check` first appeared at 2026-10-26 03:50 and for `rkhunter-check` at 2026-10-28
  02:04. Both were the first scheduled runs after the tick put the new scripts on the host. The
  shadow PR's rule comment states this behaviour: "Each NotReporting rule fires from its deploy
  until the scan's first run writes the file, up to a week". All 1645 and 4409 minutes fall where
  the tile had no series, so Kuma read nothing while the rule fired.
- **daniel-server never fired.** Its `aide-check` and `rkhunter-check` series already existed at
  the window's start. Their `last_run` was 2026-10-24 21:25:47, a run by hand before the merge,
  so the deadline held until the scheduled runs replaced it.
- **daniel-pi, both tiles: no textfile in the whole window.** Prometheus has no `aide-check` or
  `rkhunter-check` series with `origin="daniel-pi"`. `node_textfile_mtime_seconds{job="node-pi"}`
  lists five files, and neither scan's file is one of them. The collector itself works, because
  `pi-sd-health` and `pi-recovery-health` reach Prometheus. Until 2026-10-31 the Pi ran both scans
  from `initial_setup`'s cron, and the new scripts reach the Pi only by an operator-run apply that
  did not happen. The rkhunter tile read up for 2587 minutes from 2026-10-28 02:26, so the old
  script ran and pushed to Kuma but wrote no textfile. Those 2587 minutes are the only Q2 minutes
  in the batch while a tile read up. The rule was right, because nothing reported to Prometheus.
- **daniel-pi AIDE: the Monday run left no trace, which is inferred, not shown.** The tile's last
  sample was 2026-10-21 21:57, reading up, and no push reached Kuma afterwards. The 2026-10-26
  run started at 03:00. B8's read records the Pi's load5 above 1.5 per core from 03:31 and the bridge
  blaming load5 until 04:37, and `node_boot_time_seconds` dates a reboot at 04:32:54. The Pi's
  journal is not shipped to Loki (`config.alloy.j2`), so no log line confirms that the reboot
  cut the scan short. Kuma read nothing for the whole week, while `AideCheckNotReporting` fired.

**The Pi's scripts are packaged since 2026-10-31.** `1a218aeb9` (2026-10-31T13:48Z) added the
`integrity-scans` component to the Pi's package. The Pi reports version `0.6508+g1a218aeb941a`,
and its `homelab-aide-check.timer` and `homelab-rkhunter-check.timer` were activated at 14:27Z.
The component renders the role's scripts with `initial_setup_host_lib_dir: /usr/lib/homelab`.
The same package's `kuma-push` and `pi-heartbeats` components install `kuma-push-lib.sh` and
`homelab-check-lib.sh` there, so both `source` lines resolve in the render. No Pi run of the
packaged scripts has happened yet. The first AIDE run is Monday 2026-11-02 at 03:00, and the
first rkhunter run is Wednesday 2026-11-04 at 02:00. This session had no ssh route to the Pi,
so nothing on the host was read directly.

**The deployed rules, read at 16:18Z.** `kubectl` has no kubeconfig on daniel-server, so the
ConfigMap was not compared byte for byte. `prometheus_rule_group_rules` for `host-checks` reads
75. The two firing B9 alerts, `AideCheckNotReporting` and `RkhunterScanNotReporting` for
`daniel-pi`, carry `shadow="true"`. All twelve B9 rules in `host-checks.yaml` carry
`shadow: "true"`.

No Q1 minute needs explaining, and no rule changes. The soak does not block Cutover-1a for the
four daniel-box and daniel-server tiles. **It blocks Cutover-1a for the two daniel-pi tiles.**
Cutover-1a deletes all six tiles. The Pi's `Failed` rules have never had a series to read, so
after the cutover a Pi rkhunter warning or a failed AIDE run would page nobody. Only
`NotReporting` would fire, and it is already firing. The block clears when the Pi's `aide-check`
and `rkhunter-check` series exist and both Pi `NotReporting` rules have cleared. The earliest
reads that can show this follow the runs on 2026-11-02 and 2026-11-04.
[#6750](https://github.com/DanielH2018/server/issues/6750) holds that re-read.

#### The tag PR

B9's shadow PR (`ab283899e`) added no tag, so *The cutover order* gives B9 a separate PR. Branch
`m6-b9-kuma-retiring-tags` ([#7067](https://github.com/DanielH2018/server/pull/7067)) adds `aide-check` and `rkhunter-check` to `KUMA_RETIRING_TAGS` in
`monitor-bridge/files/verdicts/logs.py`, with a case in `test_check_swallowed_verdicts.py`, and
deploys `monitor-bridge`. Cutover-1a waits a week after that deploy.

The exemption covers a `rejected` push line from either tag, which is what a scan logs while it
pushes into a tile that Cutover-1a deleted. It does not cover a lost `status=down` push, which
still pages. The Pi ships only `health.log` to Loki, and its refused-push lines carry no
`by=kuma`, so the exemption covers daniel-box and daniel-server and none of the Pi's lines.

#### The daniel-pi re-read, 2026-11-02

The re-read for [#6750](https://github.com/DanielH2018/server/issues/6750) ran from
2026-11-02T18:48Z to 18:53Z. The Pi's first packaged AIDE run was the scheduled one at 03:00. The
operator started the Pi's first packaged rkhunter run by hand, ahead of its Wednesday slot.

| Check on daniel-pi | `homelab_check_success` | `last_run` (UTC) | `NotReporting` rule | Kuma tile |
|---|---|---|---|---|
| `aide-check` | 1 | 2026-11-02 05:39:11 | `AideCheckNotReporting` cleared 05:40 | up |
| `rkhunter-check` | 1 | 2026-11-02 18:46:14 | `RkhunterScanNotReporting` cleared 18:47 | up |

- **Both series exist.** `homelab_check_success{check=~"aide-check|rkhunter-check",origin="daniel-pi"}`
  reads 1 for each check, from `job="node-pi"`. Both `last_run` timestamps fall on 2026-11-02, so
  each series came from a packaged run. The old cron scripts wrote no textfile.
- **Both rules have cleared.** `ALERTS{alertname=~"AideCheck.*|Rkhunter.*",service="daniel-pi"}`
  returned no series at 18:48:08Z and again at 18:53:29Z. `probe.py alerts --pi` dates the last
  firing episodes as ending at 05:40 for AIDE and 18:47 for rkhunter, one evaluation after each
  textfile's first write. The operator's comment on #6750 reads `RkhunterScanNotReporting` still
  firing at "~18:50Z". That comment was posted at 18:47:28Z, so its read came before the 18:47
  evaluation that cleared the rule, and the two reads agree. Each
  rule shows a five-minute gap on 2026-11-01 between 07:39 and 07:44. Both rules have that gap,
  and no Pi textfile existed then, so the gap is not a clearance.
- **Both tiles read up.** `monitor_status` reads 1 for `AIDE Integrity (daniel-pi)` and
  `rkhunter Scan (daniel-pi)`, and `min_over_time` over the last 13 hours is 1 for both. The AIDE
  tile had no series in the 48 hours before 04:48, so its series came from the 05:39 run's push.
  The rkhunter tile has read up since the old script's
  push on 2026-10-28.
- **The rkhunter run's journal**, as the operator read it, ends
  `status=up scan completed: 0 warnings, 0 suspect files, 0 possible rootkits (exit 0, run 24m)`.
  At its start, `rkhunter --update` logged `Invalid WEB_CMD configuration option: Relative
  pathname: "/bin/false"`, from the distribution's `/etc/rkhunter.conf`. The unit's `ExecStartPre=-`
  discards that exit code, so the scan verdict is unaffected.
  [#6964](https://github.com/DanielH2018/server/issues/6964) holds that.
  This session had no ssh route to the Pi and read no journal itself.

**The Pi block on Cutover-1a has cleared.** Both conditions above hold: the Pi's `aide-check` and
`rkhunter-check` series exist, and both Pi `NotReporting` rules have cleared. The Pi's `Failed`
rules have a series to read, so after the cutover a Pi rkhunter warning or a failed AIDE run
fires `RkhunterScanFailed` or `AideCheckFailed`. B9's soak no longer blocks Cutover-1a for any of
its six tiles.

#### Cutover-1a, the tiles go

Cutover-1a is branch `m6-b9-cutover-1a-tiles`, tracked by
[#7062](https://github.com/DanielH2018/server/issues/7062). It deletes the six
`AIDE Integrity (<host>)` and `rkhunter Scan (<host>)` declarations from
`static-monitors.yaml.j2` and the `^aide-` and `^rkhunter-` status-page patterns. The twelve B9
rules stay `shadow`, so this PR removes six pagers and adds none. Cutover-1b adds them.

The six tokens enter `PENDING_RETIREMENT`, because no cluster render references them once the
tiles go: `aide_daniel_box_push_token`, `aide_daniel_server_push_token`,
`aide_daniel_pi_push_token`, `rkhunter_daniel_box_push_token`, `rkhunter_daniel_server_push_token`
and `rkhunter_daniel_pi_push_token`. A6's Cutover-2 restored the module the same way. Each host
package keeps its token as a credential, and each scan keeps pushing into the deleted token until
Cutover-2.

What the removal moved in the tests:

- The census floors fall to one, because `Status Page Sync` is the only push tile left.
  `GATED_MEMBERS` and `_INCIDENT_TILE` name it, and `KNOWN_IDS` swaps the three scan ids for
  `discord`.
- `test_aide_check.py` and `test_rkhunter_check.py` lose their tile-gating tests, and their
  deadline test reads the script's `DEADLINE_S` where it read the tile's `interval`. The
  `PUSH_TOKENS` map keeps the one-token-per-host test.
- `test_integrity_scan_rule_literals.py` stops comparing `DEADLINE_S` to a tile. The helper
  The setup-plane helper that rendered a Kuma tile for a guard had no other reader, so it goes
  with its census entry.
- `test_the_deleted_b9_tiles_are_not_declared` fails if a scan tile returns.

**The tag precondition is waived.** *The cutover order* gives B9 a separate PR that adds
`aide-check` and `rkhunter-check` to `KUMA_RETIRING_TAGS` and deploys `monitor-bridge`, and
Cutover-1a waits a week after that deploy. At `15e4275491` the set in
`monitor-bridge/files/verdicts/logs.py` listed neither tag. That PR is
[#7067](https://github.com/DanielH2018/server/pull/7067), and the operator waived its week on
2026-11-03, so Cutover-1a follows its deploy at once. Without the tags, a push refused by a
deleted token reads as `rejected` and pages Swallowed Push Verdicts. The scans push weekly (AIDE
on Monday 03:00, rkhunter on Wednesday 02:00), so the refusal is in the read window for a few
hours after each run, until Cutover-2 stops the push on that host.

The reads on 2026-11-03 at 19:21Z. `monitor_status{monitor_name=~"AIDE.*|rkhunter.*"}` returns no
data, because Kuma's restarts dropped the weekly tiles' series (the issue's 7-day read still
finds all six). `ALERTS{alertname=~"AideCheck.*|Rkhunter.*"}` returns no data, so no B9 rule is
firing.

The two-hour no-pager bound counts from the `uptime-kuma` apply of this PR and ends at the
`observability` apply of Cutover-1b. If Cutover-1b has not applied by then, revert this PR. The
rules are still `shadow`, so the revert restores Kuma as the only pager, and each tile recovers
on the scan's next push because the tokens stay in `secrets.yml` until the token PR.

#### Cutover-1b, the rules page

Cutover-1b is branch `m6-b9-cutover-1b-rules-page`, stacked on Cutover-1a. It drops `shadow` from
the twelve rules in `host-checks.yaml`: `AideCheckFailed`, `AideCheckNotReporting`,
`RkhunterScanFailed` and `RkhunterScanNotReporting` for each host. All twelve are `warning`, which
posts to Discord alone, the channel the six tiles used. `land.sh` deploys `observability` alone.

B9's six `CheckWithoutPager` terms were the last ones in the `kuma-cutover` group, because A6's
Cutover-1b and B6c's removed theirs. The plan's rule for the last term applies: this PR deletes
`files/rules/kuma-cutover.yaml`, its promtool test, its `observability_rule_groups` entry and the
test lists that named it. The routing test loses its `check-without-pager` case, and the
`TextfileCollectorError` and `ComposeUpFailed` rules are the host group's only shadow rules left.
`test_host_sets_names_every_host_check_rule` pins that set exactly, so a B9 rule that regains
`shadow` fails it, and it asserts that each B9 rule walks to Discord alone.

The group's `unless on() (up{job="uptime-kuma"} == 0)` clause goes with the file. C-1a's scope in
the plan lists that clause as a reader of the scrape job, so C-1a has nothing to carry there.

#### Cutover-2, the push stops

Cutover-2 is branch `m6-b9-cutover-2-push-stops`, stacked on Cutover-1b. It is a host-package
change: `aide-check.sh.j2` and `rkhunter-check.sh.j2` are inputs of the `integrity-scans`
component, so the change reaches each host only in a new version of its package. `land.sh` waits
for all three hosts. The Pi's `homelab-updater.timer` skips 23:00 to 00:59 and 04:00 to 05:59 UTC.

What the diff does:

- Both scripts drop the `kuma-push-lib.sh` source, the push target, the credential and env-file
  token read, and the push with its "token not set" branch. Each keeps its `status=` journal line
  and its textfile write, which `AideCheck*` and `RkhunterScan*` read.
- Both service units lose `LoadCredential=` and the `homelab-secrets.service` ordering, and their
  `Description=` no longer says the scan pushes.
- The component's contract drops the push from its scope and evidence lines.
- The package specs keep `credentials:` for all six tokens, because `host_secrets.py check` pins
  the list to each host's SOPS file, which only the operator's `host_secrets.py sync` changes. B9's
  token PR removes the keys from both in one step. The two `*-kuma-push.env.j2` templates stay for
  the same PR.
- The `kuma-push` component stays, because C-3a deletes it. C-3a waits for this PR's package on all
  three hosts, and its precondition read quotes each host's installed version:
  `dpkg-query -W -f '${Version}' homelab-<host>`.
- The AIDE and rkhunter test helpers read a run's verdict from the logged `status=` line where
  they read the push stub. `test_the_rendered_script_carries_no_kuma_push` fails on the master
  scripts (`redproof origin/master` reports RED) and passes on the new ones.
  `test_cron_scripts_publish_via_pr.py` re-decides its corpus anchor: no `.sh.j2` push script is
  left, so it names authelia's config-secret and loki-homelab's `config.alloy`, with a floor of 2.

From Cutover-1a to the package install on a host, the scan pushes into a deleted token and
`kuma_push` logs `push failed`. The plan's *B9's tags* paragraph says `KUMA_RETIRING_TAGS` should
exempt `aide-check` and `rkhunter-check` first, and it does not (see Cutover-1a above).

#### The token PR

The token PR is branch `m6-b9-tokens`, tracked by
[#7062](https://github.com/DanielH2018/server/issues/7062). The operator waived the
token-retirement week on 2026-11-03, as for A6, so it follows Cutover-2 at once. It retires the six
per-host push tokens: `aide_daniel_box_push_token`, `aide_daniel_server_push_token`,
`aide_daniel_pi_push_token`, `rkhunter_daniel_box_push_token`, `rkhunter_daniel_server_push_token`
and `rkhunter_daniel_pi_push_token`.

The code half deletes `scripts/secrets_mgmt/pending_retirement.py` with its readers and its
keep-on-write tests, the last retirement the plans schedule, and adds the six names to
`git_dates.RETIRED`. Each host package loses the two scan tokens from `credentials:`. The two
`*-kuma-push.env.j2` templates and the guards that rendered them go, along with the six names in
`CROSS_HOST_PUSH_TOKENS`. The `legacy:` entries of the `integrity-scans` component stay, so a host
still removes the env files an earlier apply wrote.

Each host's SOPS file holds its own two keys and none of the other hosts', read with
`secretq has` on 2026-11-03. daniel-box holds `aide_daniel_box_push_token` and
`rkhunter_daniel_box_push_token`, daniel-server holds the two `daniel_server` keys, and daniel-pi
holds the two `daniel_pi` keys. `cluster-vars.source` holds all six. The operator removes the keys
from the three host SOPS files with `host_secrets.py sync`, and from `secrets.yml`,
`secret_rotation.yml`, the secret-tiers fragment and the cluster file, in a commit on this branch.

### PR B-1, with B-1d and B-1e

PR B-1, `probe.py health` reads the alert plane: [#5339](https://github.com/DanielH2018/server/pull/5339).
It merged on 2026-10-16 at `ba681c934`, after C7's amendment for Open question 4 and the slice
index's M6 split landed in #5323. On master the gate passed homepage (Ansible-owned, referenced to
`applied_at`), speedtest (`owner: flux`, referenced to its Kustomization's revision) and
netpol-baseline (no route, so the probe half is skipped), each with 0 gated alerts.

PR B-1d, the drill rule: [#5406](https://github.com/DanielH2018/server/pull/5406). It merged on
2026-10-17 at `c9943f04e` and settled with `observability`. `probe.py health littlelink` then
exited 1 and named `HealthGateDrill{severity=critical} firing since 2026-10-17T14:11:13Z`. In the
15 minutes after the rule went live, Alertmanager sent no Discord or email notification, and its
webhook rate matched the two hours before. PR B-1e, the drill's removal:
[#5411](https://github.com/DanielH2018/server/pull/5411). It merged at `f8e28584f` and settled with
`observability`, and `probe.py health littlelink` exited 0 again. littlelink's `applied_at` read
`2026-10-16T23:03:57Z` at all three reads, so the drill holds. D5's flux PR for littlelink no
longer waits on M6 (Open question 12).

C7's content-digest reference: [#5369](https://github.com/DanielH2018/server/pull/5369). It amends
C1, C6 and C7, and the operator accepted the amendment on 2026-10-17. It merged at `2226b7b61` and
settled with `observability`. Every flux tag's `gotk_resource_info` carries `content_digest`, and
peanut's reference stayed at `2026-10-17T14:00:54Z` while three later commits landed.

### PR B-2

PR B-2, `probe.py monitors` and `probe.py alerts` on `ALERTS`, postflight's Kuma checks, and
`kuma-drift` deleted: [#6829](https://github.com/DanielH2018/server/pull/6829). It merged on
2026-11-01 at `05ede55db`. On 2026-11-12 `probe.py --help` printed no `kuma-drift` line, and
`probe.py monitors` printed `0 paging alert(s) firing` and exited 0.

- **`monitors`** rolls up `ALERTS{alertstate="firing"}` by severity and `service`, then lists
  active silences. A missing Watchdog exits 75. Shadow alerts print under their own heading and
  do not change the exit code, because five fired on 2026-11-01 while Kuma read 35/35 up.
- **`monitors` also reads Alertmanager's active alerts**, and folds in the ones `ALERTS`
  lacks. The loki-homelab ruler sends its alerts to Alertmanager directly, so a reader of
  `ALERTS` alone missed `LokiPiLogsSilent` while it fired on 2026-11-01. `alerts` has no
  history for them, because Alertmanager keeps none.
- **`monitors` keeps one Kuma arm**, `monitor_status{job="uptime-kuma"} != 1`, because A6's
  bridge rows, B2, B4, B5, B6b and B9 still page through Kuma. The arm is not in the plan's
  B-2 row. C-1, which deletes the `uptime-kuma` scrape, removes it with `format_kuma` and its
  two tests in `scripts/diagnostics/tests/test_probe_monitors.py`.
- **`firing_ingress.py` is deleted**, because the rollup reads the same `ALERTS` series. The
  plan's tree predates the module.
- **`kuma_pi.py` is deleted** with `kuma_live.py` and `kuma_table_loop.py`, because
  `kuma-drift --pi` was its only caller.
- **postflight** checks `up{job="alertmanager"} == 1` and the Watchdog firing as one §9.1
  check. `check_kuma_scrape` stays until C-1.

### PR B-4

PR B-4 ([#6798](https://github.com/DanielH2018/server/issues/6798)) replaces Homepage's
Uptime-Kuma tile with an Alertmanager tile in the same grid slot. The tile is a
`prometheusmetric` widget with three blocks, each read from Prometheus `ALERTS`:

- **Critical** counts firing `severity="critical"` alerts.
- **Warning** counts firing `severity="warning"` alerts.
- **Watchdog** counts the firing Watchdog, so it reads 1 while the alert plane evaluates.

The two severity counts drop `shadow="true"` and `drill="true"`, the two matchers the routing
tree sends to its blackhole receiver. The tile links to `https://alertmanager.local.<domain>`,
the `two_factor` UI from B-6b. The widget names its blocks with `label:`, so the two Kuma
heading renames in `custom.css.j2` are deleted rather than moved. Homepage no longer calls
`homepage_href('uptime-kuma', domain)`, which C-1a needs gone.

Before the merge, `probe.py metric` returned 0, 0 and 1 for the three queries. The Homepage
pod reaches Prometheus through the `prometheus-callers` NetworkPolicy, which names
`app=homepage` in the `homelab` namespace on port 9090; the Headlamp tile already reads the same
ClusterIP. The verify-by read through the `homelab-ui` MCP server waits for the promotion.

### PR B-5

B-5 ([#6819](https://github.com/DanielH2018/server/issues/6819)) changes two Grafana boards
in the `observability` role.

- `Apps/uptime-kuma-monitors.json` is deleted, with its line in
  `observability_dashboard_folders`.
- `Infrastructure/alert-history.json` reads Prometheus's `ALERTS` series instead of the Loki
  reconstruction of Kuma DOWN pushes. Every panel filters on firing state, leaves out
  `Watchdog`, and groups by `service` through a `$service` variable built on
  `label_values(ALERTS, service)`. The two tables carry the `shadow` label, so an alert whose
  batch has not cut over stays visible and reads as not paged by Alertmanager.
- `ansible/tests/services/test_alert_history_board_reads_alerts.py` holds every panel query to
  that shape. It replaces the test that parsed the bridge's DOWN line out of the old board.

The board's history spans Prometheus's 30-day retention, not Loki's 744 hours. Kuma episodes
from before a batch's cutover show only as far back as its shadow rule has fired.

Before the PR, the panel queries ran read-only against live Prometheus through
`probe.py metric`. Every `ALERTS` series carried a non-empty `service` label
(`count(ALERTS{service=""})` returned no data).

Nothing in B-5 stops or mutes a Kuma tile or push.

The post-apply read is owed at landing. It is a `uv run pytest -m ui -k grafana` run or a
`homelab-ui` render of the board after Flux applies `observability`, quoted here.

### PR B-6a and B-6b

B-6a ([#6689](https://github.com/DanielH2018/server/issues/6689)) wrote the `two_factor` rule
for `alertmanager.local` with `group:admins` as its subject, and the `deny` rule after it, in
`ansible/roles/k8s/authelia/templates/config-secret.yaml.j2`.

B-6b ([#6690](https://github.com/DanielH2018/server/issues/6690)) adds the UI route,
`ansible/roles/k8s/observability/templates/alertmanager-ingressroute.yaml.j2`, and sets
`--web.external-url=https://alertmanager.local.<domain>` on the Deployment. The route shares
its host with the GET-only read door from
[#6691](https://github.com/DanielH2018/server/issues/6691), which outranks it on the door's two
paths. Its verify-by read, a test silence created and expired through the UI, waits for the
promotion.

### PR B-7

PR B-7, the docs, skills, agents and evals that still pointed a reader at Kuma: the PR that
adds this record, [#7058](https://github.com/DanielH2018/server/pull/7058), for [#7052](https://github.com/DanielH2018/server/issues/7052). It deploys
nothing. Each item of *Phase B* item 8, and what changed:

- **The root `CLAUDE.md` *Where to Look* rows.** No change. `git grep -n -i kuma -- CLAUDE.md`
  returns nothing, because B-2 had already edited the *Claude tooling* bullet.
- **`.claude/skills/add-secret/SKILL.md`.** The 32-hex Kuma push-token bullet is replaced by a
  rule never to add a push token, pointing at ADR 0026's producer recipes. The `auto` tier
  example says the remaining push tokens are the only `auto` entries.
- **`.claude/skills/new-container/SKILL.md`.** No change. It does not name Kuma.
- **`.claude/skills/homelab-review/accepted-designs.md`.** The "down = no heartbeat" bullet now
  names the alert-plane deadmen (`<Check>NotReporting`, `HostCheckOverdue`,
  `BridgeCheckStale`). The Pi bullet names B8's `PiHostDown` and `PiPortDead` and A1's Pi rules
  instead of static Kuma labels, and drops `HOST_METRIC_ORIGIN_EXCLUDE`, which no file in the
  tree holds. `uptime-kuma-data` stays in the no-backup bullet, because the claim exists until
  C-2b deletes it.
- **The three reviewer agents.**
    - `homelab-backup-observability-reviewer.md` describes one alert plane, the producer
      recipes and the deadmen, and its method checks the rules directory before it reports a
      missing monitor.
    - `homelab-cicd-reviewer.md` names the secret-rotation audit timer and
      `SecretRotationAuditFailed` in place of the daily Kuma push.
    - `homelab-container-reviewer.md` is unchanged. Its one mention is the
      AutoKuma macro the Pi compose files inline, which the plan's census allowlist keeps.
      [#7210](https://github.com/DanielH2018/server/issues/7210) later deleted the macro and
      the mention.
- **`.claude/hooks/hooklib/service_lines.py`.** Two docstrings change: the release-staleness
  verdict sits beside a textfile metric, and `ReleaseStalenessCheckNotReporting` pages a dead
  cron. The `--kuma` flag the cron passes to `probe.py releases` is a live CLI flag, so it
  stays.
- **The eval cases.** `homelab-backup-observability-reviewer/001` grades a missing
  blackbox-ingress probe instead of a missing Kuma monitor; `kuma: false` stays its opt-out
  until C-4a renames the key. `002-kuma-push-no-retries` is replaced by `002-wan-gate-no-for`,
  which grades the deliberate absence of a `for:` on `WANUnreachable`. The two
  `homelab-cicd-reviewer` cases quote the alloy compose file's live label. `evals/history.json`
  keeps the old case id as history, because the eval-run cron owns that file.
- **`ansible/roles/k8s/homelab-mcp/CLAUDE.md`.** The `/health` router now exists for the
  `blackbox-extra` job's `http_body_ok` probe, not for a Kuma tile.
- **The ADR (C-0).** No change. ADR 0026 was written and accepted on 2026-10-24.

The same PR adds a *Token PR* line to each of the 15 batch records that
[#6949](https://github.com/DanielH2018/server/pull/6949) covered
([#6950](https://github.com/DanielH2018/server/issues/6950)). It also re-checked the comment
halves of [#6821](https://github.com/DanielH2018/server/issues/6821) and
[#6882](https://github.com/DanielH2018/server/issues/6882). Outside history, generated pages and
tests, no comment still names `probe.py kuma-drift` or a Loki-reading `probe.py alerts`, so
nothing changed there. Their C-1a halves, `format_kuma` and the `KUMA_QUERY` arm of
`scripts/diagnostics/probe_lib/monitors.py`, wait for C-1a.

**Files B-7 did not reword.** About 35 hand-written `docs/` pages still name Kuma, among them
`docs/monitor-bridge-checks.md`, `docs/k3s-node-plane-crons-and-incidents.md` and
`docs/healthchecks-io-deadman.md`. Item 8 does not list them, and no other M6 PR row does.
[#7056](https://github.com/DanielH2018/server/issues/7056) tracks the sweep, after C-1 and C-3a.

`ansible/roles/k8s/monitor-bridge/files/bridge/common.py` is one of the files #6821 names. Two of
its docstrings still say the bridges push to Kuma and that Kuma forwards to Discord. B-7 left
them, because the file ships in the monitor-bridge and autofix-bridge builds. Both roles are
flux-owned, so a docstring edit would roll two workloads in a PR that otherwise deploys nothing.
[#7060](https://github.com/DanielH2018/server/issues/7060) rewords them in the next real edit to
the file.

### PR B-10

PR B-10, triage's poller drops its Kuma source:
[#6997](https://github.com/DanielH2018/server/pull/6997). It merged on 2026-11-02 at `589c932c8`.
It was held until A6's Cutover-1b was live, so that every Kuma pager had an `ALERTS` rule and the
`ALERTS` path saw each fall the Kuma path saw. [#6876](https://github.com/DanielH2018/server/pull/6876)
named the poller as a Kuma consumer.

The PR removes `KUMA_QUERY`, `KUMA_UP_QUERY`, `kuma_up_keys`, the `kuma` arm of `unknown_sources`
and of `down_keys`, and the `kuma_up` argument of `step` from
`hosts/components/claude-code/files/claude_triage_poll.py`. Recovery is now generic: a key that is
not firing and whose source is not unknown recovers. The first successful poll on the new code
therefore drops every `kuma:` key in `poll-state.json` and opens no episode for it.

**Only daniel-box carries the poller.** The component's `when:` gates `claude_triage_poll.py` on
`claude_code_triage_enabled`, which `ansible/inventory/host_vars/daniel-box.yml` sets true and
`hosts/components/claude-code/defaults/main.yml` leaves false. A render of both host packages
before the change put the file in `homelab-daniel-box` and not in `homelab-daniel-server`. Item 9's
**Order** and C-1a's precondition once said "both hosts". They now say daniel-box, because
daniel-server has no poller to read the scrape job that C-1a deletes.

C-1a's precondition asked for daniel-box applied and a journal of `homelab-claude-triage-poll`
on daniel-box with no `uptime-kuma` query. It held before C-1a merged: *PR C-1a and C-1b*
records 667 `query=` lines after 2026-11-03 00:50:25Z and no `uptime-kuma` line.

### PR C-0

PR C-0, the ADR: [#6091](https://github.com/DanielH2018/server/pull/6091). It merged on 2026-10-24
at `d0d4fc140`. The operator accepted ADR 0026, and
`docs/adr/0026-one-alert-plane-on-prometheus-and-alertmanager.md` reads `status: Accepted`.

### PR C-1a and C-1b

#### PR C-1a

[#7077](https://github.com/DanielH2018/server/pull/7077), from [#7061](https://github.com/DanielH2018/server/issues/7061), deletes
`KumaDown` with its inhibit, Prometheus's `uptime-kuma` scrape job and its credential,
`check_kuma_scrape`, the two `job="uptime-kuma"` rows in `files/rules/scrape-targets.yaml`, and
the Kuma read in `probe.py monitors`.

**The operator waived the Phase A week on 2026-11-03.** C-1a's precondition asked for a week of
Phase A with no Kuma page. The operator waived it as they waived the token week on 2026-11-02.
Every other precondition held, and these reads ran on 2026-11-03 at about 23:08Z.

The gate has two halves ([#7075](https://github.com/DanielH2018/server/issues/7075)). The instant
read returns the one tile:

```console
$ probe.py metric 'count by (monitor_type, monitor_name) (monitor_status)'
monitor_name=Status Page Sync, monitor_type=push = 1
```

`prod` holds `a4d5def5fa`, and the file declares one monitor at that commit:

```console
$ kubectl -n flux-system get ocirepository homelab \
    -o 'jsonpath={.status.artifact.metadata.org\.opencontainers\.image\.revision}'
master@sha1:a4d5def5fa1e3416f6db889c7e6dee589e811aff
$ git show a4d5def5fa:ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2 \
    | grep -E '\.json: \|$' | grep -vE '^ +(discord|email|tag-[a-z]+)\.json:'
  status-page-sync.json: |
```

**The triage poller has not read Kuma since B-10 installed.** Its last `uptime-kuma` line is
2026-11-03 at 00:50:25Z. A 7-day read is not clean, because the lines before that are the old
poller's. Since then the journal holds 667 `query=` lines and no `uptime-kuma` line. daniel-box
runs `homelab-daniel-box` `0.6730+g35edf86f1457`, whose commit descends from B-10's `589c932c85`.

**Homepage has no Kuma read left.** Its Kustomization reports the digest that holds `a4d5def5fa`,
and `git grep uptime-kuma a4d5def5fa -- ansible/roles/k8s/homepage` returns nothing.

**The cluster file does not change.** `cluster_vars.py check` reads clean, because
`uptime-kuma/templates/secret.yaml.j2` still renders `prometheus_kuma_api_key` for the maintenance
sync. C-1a therefore needs no operator `cluster_vars.py write`. C-5 retires the key.

**The render drops no object.** `prometheus-scrape-credentials` loses one key and stays, so the
PR carries no `Removes-Object:` line.

The two post-promotion reads follow once `promote` moves `prod` to a commit that holds C-1a. C-1b's
preconditions quote them again.

#### PR C-1b

C-1b empties the `uptime-kuma` render down to its two claims. The PR body quotes its
preconditions, its render and its `Removes-Object:` lines. The post-promotion read has not run:
it needs `promote` to move `prod` to a commit that holds C-1b.

**Files the plan does not list.** Each one pinned a deleted template, the deleted task, or a reader
that went with them:

- `uptime-kuma/defaults/main.yml` shrinks to the claims, because the renderer refuses
  `flux_prepull_timeout` on a render with no workload.
- `scripts/renderer/renderer_lib/task_inputs.py` drops `kuma_declarations`, and
  `secret_checks.py`'s rule 5 names a different member.
- `scripts/secrets_mgmt/consumers.py` no longer names `uptime-kuma` as a token's consumer, and
  `kuma_status_page_sync_push_token` moves from tier `auto` to `assisted` until C-5.
- `deploy_cross_role.py`'s k8s table loses `uptime-kuma` from the `kuma_push.py` row, which the
  plan gave to C-3a. The row's other two members stay for C-3a.
- `python-liquid` leaves the dev dependencies with the two tests that used it.
- `test_cron_scripts_publish_via_pr.py`: the push corpus falls to one template, because the
  Authelia bypass held the other `api/push` literal.
- `test_host_maintenance.py` takes over the reboot timer's assertion from
  `test_maintenance_window.py`, because the timer outlives the Kuma window.

**The plan's precondition grep names more than the plan predicted.** The other hits are comments,
the entry itself, C-2b's `k8s_host_files` task, C-7's `KumaNotificationDropped`, C-3a's push
library, an inert exclusion regex in `edge.yaml` and the disarmed `render_records` role. None
reads the entry's `port` or `hostname`, and the full cluster render passes.

**The prepull Kustomization leaves two objects behind.** The render drops
`flux/uptime-kuma-prepull.yaml`, so the root prunes that Kustomization under `deletionPolicy:
Orphan`. The Job `prepull-uptime-kuma-dd816639` and the ConfigMap `prepull-uptime-kuma-41b32c91`
stay in `flux-system`. The plan's post-promotion read does not see them. The operator deletes them
with the claims after C-2b.

### PR C-2a and C-2b

#### PR C-2a

[#7087](https://github.com/DanielH2018/server/pull/7087), from
[#7086](https://github.com/DanielH2018/server/issues/7086), deletes `uptime-kuma` from the bespoke
policies and from ical-proxy's `netpol_from`.

**One PR holds the order.** `netpol-baseline` renders all five grants, including ical-proxy's fence
through `networkpolicy-callers.yaml.j2`. No workload role's render moves, so *the roles first* has
nothing to order.

**Three of the four bespoke policies held a grant.** `networkpolicy-jellyfin.yaml.j2` leaves `:8096`
open to every source and never named Kuma as a peer. It held only a comment about Kuma's VIP tile,
and that comment is what C-2a deletes there.

#### PR C-2b

The branch `worktree-fanout-7095`, from
[#7095](https://github.com/DanielH2018/server/issues/7095), deletes the role directory, its
`containers_list` entry, and `k8s_host_files/tasks/uptime-kuma.yml` with its import.

**`ls` cannot answer the staging-directory read.** `/etc/rancher/k3s/manifests` is mode 0700, so
`ls` as the operator's account answers `Permission denied`. `rootq stat` lists the path by name and
runs as root. It answers `refused: /etc/rancher/k3s/manifests/autokuma-claims does not exist`,
while its control `/etc/rancher/k3s/manifests/registry-jobs` prints a stat line.

**The claims leave the no-backup list with the entry.** `longhorn-reconcile` then moves each
`longhorn`-class volume from `no-backup` to `default`, and `daily-backup` backs up a detached
volume because `allow-recurring-job-while-volume-detached` is `true`. The operator's delete
therefore belongs before the first 03:30 UTC after the promotion.

### PR C-3a and C-3b

#### C-3a, the push libraries

The reads ran at base `764b603be` on 2026-11-04 at about 16:30Z. [#7109](https://github.com/DanielH2018/server/issues/7109)
holds the rulings.

- **The filename grep.** `git grep -l kuma_push.py -- ansible hosts scripts ':!*/tests/*'` listed
  only files this PR edits or deletes.
- **Read 1.** Every check the plan names reports a run after its batch's Cutover-2, except
  `aide-check`. The operator waived that one on 2026-11-04, because read 3 finds no installed
  AIDE script that sources the library. Its next run is 2026-11-09 03:00.
- **Read 2.** The `git grep -n kuma-push-lib` over templates, files and `hosts` printed no
  script that sources the library. Each hit is a comment, the library, its component or a
  `legacy:` row.
- **Read 3.** On each host the grep listed the library's own copies, including the Python
  `kuma_push.py` copies under `/opt` and `/usr/lib/homelab/builder` on daniel-box, and
  comment-only hits in `homelab-check-lib.sh`, `setup-drift-lib.sh` and
  `etcd-drill-guest-run.sh`. On the Pi it also matched `/usr/lib/homelab/manifest.json`, the
  package manifest that names the library's path. Four retained daniel-box release trees under
  `/opt/homelab/releases/k3s-disk-health/` still hold the `source` line.
  `/opt/homelab/current/k3s-disk-health` points at `45a8eb0ef73d`, which does not.
- **Unreadable scripts.** `github-ruleset-drift.sh`, `github-interaction-limit.sh` and
  `registry-gc.sh` on daniel-box return `Permission denied`, so the rendered source stands in
  for them. `git grep -c kuma-push-lib` over those three and `eval-run` exits 1, which means no
  hit.
- **Package versions.** daniel-box and daniel-server run `0.6733+ge35604c7d422` and daniel-pi
  runs `0.6738+g53142821af01`. B9's Cutover-2, `35edf86f1`, is an ancestor of both SHAs.

Two changes went beyond the plan's list, and the plan now names both. `render_records.py` read
`kuma_push` through the re-export, which the filename grep cannot see. The dropped record task
would have left the `kuma-push-lib` fragment behind as permanent drift, so the absent task removes
it too.

### PR C-4a and C-4b

#### PR C-4a

[#7121](https://github.com/DanielH2018/server/pull/7121), from
[#7115](https://github.com/DanielH2018/server/issues/7115), renames the `kuma:` key to `probe:` on
15 entries and feeds the `blackbox-ingress` job from `ingress_probe_targets`. Its base is
`936338336`, which is after C-2b.

**The render read.** The job's rendered targets match the base: `diff` exits 0, with 26 targets on
each side. The reworded job comment renders into `prometheus.yml`, so `checksum/config` moves and
the promotion rolls Prometheus once. The `probe_success` count after promotion has no recorded read
yet. To take it, run `count(probe_success{job="blackbox-ingress"})` and expect 26.

**The narrowing read.** `deploy_tags.py narrow` did not refuse. It names the 15 renamed entries'
own tags, `ingress_probes.py -> observability`, and this readers line:

```text
narrow: containers_list readers of kuma,probe on 15 entries -> longhorn-reconcile,observability via ansible/inventory/host_vars/daniel-box.yml
```

**`longhorn-reconcile` is a reader the plan does not predict.** Its `defaults/main.yml` calls
`tier_backup_claims`, `longhorn_weekly_claims` and `longhorn_nobackup_claims`, and none of them has
a `FILTER_FIELDS` row. `containers.reader_reads` therefore returns `None` for that file, so any
field edit reaches the tag. The plan's "No template reads a whole entry" does not cover a role's
defaults. [#7120](https://github.com/DanielH2018/server/issues/7120) holds the follow-up.

**`has_ingress_route` is a second reader the plan does not list.**
`scripts/diagnostics/probe_lib/health_alerts.py` read the opt-out through `kuma_ingress_monitors`.
On that filter it would return True for all 8 `probe: false` entries, so C-4a moved it too.

**The `id` and `name` overrides moved verbatim, and nothing reads them.**
[#7119](https://github.com/DanielH2018/server/issues/7119) holds their removal.

#### PR C-4b

[#7835](https://github.com/DanielH2018/server/issues/7835) deletes
`ansible/filter_plugins/kuma_monitors.py`, its import and its `kuma_ingress_monitors`
registration in `scripts/lib/ansible_jinja_env.py`. D6 had already deleted
`narrow_lib/containers.py`, so no `FILTER_FIELDS` row was left to remove. The plugin's other
filter, `kuma_malformed_push_tokens`, had no caller left, so it went with the file. The census
row's allowlist loses C-4b's two entries, which closes exit criterion 3's C-4b wait.

### PR C-5

Tracked by [#7173](https://github.com/DanielH2018/server/issues/7173). C-5 retires four keys:
`kuma_status_page_sync_push_token`, `prometheus_kuma_api_key`, `uptime_kuma_password` and
`uptime_kuma_username`. A `git grep` on 2026-11-05 found no reader of any of them. `secretq keys`
on the same day found none of them in a host SOPS file or the cluster file, so the operator's
`cluster_vars.py write` is expected to change nothing.

The operator runs `host_secrets.py check` rather than `host_secrets.py sync`. `sync` re-encrypts
even with an unchanged key set, and a host file ships in its host package, so committing one
would make the landing wait on that host for no change in content. Without a host file in the
commit, the PR waits on no host package.

The code half adds the four names to `git_dates.RETIRED`. It deletes
`sops_io.malformed_push_tokens` and `cluster_vars.py`'s `_push_token` refusal, as the plan assigns,
and `rotation_tools.decrypted_values`, because `audit`'s shape check was its only caller. `audit`
therefore no longer decrypts anything.

`render_records_push_token` stays. `ansible/roles/setup/render_records/templates/config.env.j2`
still renders it, and `consumers.py:CROSS_HOST_PUSH_TOKENS` still names it. R4 owns its removal,
in the secrets commit that follows its role deletion (`docs/target-architecture/render/r4-delete-k8s-ansible-path.md`,
*The push token leaves after the role*). After C-5, `grep -c '_push_token:'` prints 1 for both
`ansible/secret_rotation.yml` and `ansible/vars/secrets.yml`, so exit criterion 2 holds only once
R4's commit lands. [#7178](https://github.com/DanielH2018/server/issues/7178) tracks that.

### PR C-6

Tracked by [#7191](https://github.com/DanielH2018/server/issues/7191). The reads ran on 2026-11-05
at base `decdee190`, with daniel-box on `0.6776+gc0f46a5e9157`, daniel-server on
`0.6760+g46a2bc43e376` and daniel-pi on `0.6769+g7f3ec44c07c3`.

- **The absent tasks.** `ls` reports no such file for `/usr/local/lib/kuma-push-lib.sh` and
  `/var/lib/homelab/setup-deployed-manifest.d/kuma-push-lib` on all three hosts. It also reports
  no such file for `kuma_push.py` in every `/opt` consumer directory on daniel-box and
  daniel-server, read by name. `/opt/containerd` denies the `find`, and it holds no consumer.
- **The `legacy:` entries.** `ls` reports no such file for each path C-6 deletes, on every host,
  and `systemctl list-unit-files` prints no `kuma-check-loki-read-route` or
  `kuma-check-secret-rotation-audit` unit on daniel-box. C-6 deletes the secret-rotation entries.
  It keeps the three `kuma-check-loki-read-route` units and their down marker in
  `hosts/components/loki-route-witness/component.yml`, against the plan's list. daniel-server
  still runs them from the `initial_setup` role, and reconcile retires them through these entries
  when daniel-server's package takes the component. Without them that move would leave the old
  timer running, with the entry for its script deleting the script under it.
- **Exit criterion 3.** The precondition grep lists only the allowlist's identifier holders and
  the two generated records. C-4b's three entries stay until D6.

The census found far more than the plan's budget: 553 lines in 208 files, against about 100
lines. C-6 reworded what the plan assigns to it, which leaves `hosts/` holding only the Pi compose
files and rootq. The remaining 167 files sit in `_PROSE_HOLDERS`, each with its reason.
[#7194](https://github.com/DanielH2018/server/issues/7194) owns those, and
[#7195](https://github.com/DanielH2018/server/issues/7195) owns the redaction rules.

### PR C-7

The deletion lands in [#7146](https://github.com/DanielH2018/server/issues/7146), read at base
`9d64d9f1c8`. Its PR quotes the `git grep` from C-6's precondition, restricted to the C-7 files,
and the grep matches nothing. The plan's C-7 step records what those reads changed.

## Goal and exit criterion

**Goal.** Every check pages through Alertmanager. `probe.py health <tag>` reads the alert plane
(Phase B item 1 states the exact selector and how it departs from C7). Homepage shows the alert
plane. No template, task, inventory key, script or skill calls or reads Kuma, and no
push-token secret remains.

**Exit criterion.** All five hold:

1. `ansible/roles/k8s/uptime-kuma/` does not exist. `probe.py metric 'up{job="uptime-kuma"}'`
   returns nothing. The read-only
   `kubectl get svc,ingressroute,cm,cronjob,deploy -n homelab -l homelab/role=uptime-kuma`
   returns nothing. That read holds once C-1b's prune has reached the cluster (*Phase C*).
   The two claims outlive it until the **operator-only** delete after C-2b.
2. `grep -c '_push_token:' ansible/secret_rotation.yml` prints 0, and so does
   `grep -c '_push_token:' ansible/vars/secrets.yml`. Key names are plaintext in the encrypted
   file, so the second read prints a count and never a value. `secret_rotation.py audit --check`
   exits 0.
3. Nothing reads Kuma. This is a functional criterion, not a sweep of every mention. It is
   reached only after C-4b, which landed after D6 (#7835).
    - None of these identifiers appears in a non-test file under `ansible/`, `hosts/`,
      `scripts/` or `.claude/`, outside the allowlist below: `/api/push/`, `kuma_push`, `KUMA_URL`,
      `KUMA_PUSH_`, `monitor_status`, `entry_port('uptime-kuma')`, `kuma-push-lib`,
      `kuma_ingress_monitors`. C-6 quotes the `git grep` that shows it
      (*Phase C* item 6). A read through an indirection escapes that grep:
      `netpol-probe-job.yaml.j2` reads the entry's port through `entry_port(t.service)`. The
      named-member case C-1 adds to `ansible/tests/k8s/test_netpol_probe_targets.py` holds
      that read instead.
    - A ratchet census row in `ansible/tests/repo/test_census_rows_text.py` held every other
      mention of `kuma` in those trees to a frozen allowlist. Each allowlist entry named its
      file and its reason, and a file outside it that mentioned `kuma` failed CI. The
      operator retired the identifier row and this row on 2026-11-12
      ([#8177](https://github.com/DanielH2018/server/issues/8177)), because Kuma no longer
      exists for either to guard. The bullets below record what they held.
    - Before it matches `kuma`, the census row strips every citation of this plan: the
      filename `m6-retire-kuma`, and any path that starts
      `docs/target-architecture/monitoring/m6-`. A citation of the plan stays true after Kuma
      is gone, and #6903 counted 34 non-test files outside `hosts/` that carry one. A file
      whose only `kuma` is such a citation therefore needs no allowlist entry. The row's red
      fixture also holds a bare `kuma` beside a citation, and the row fails that fixture.
    - Both bullets share one allowlist. At C-6 it holds the entries below, and each entry
      names the identifiers it may still hold:
        - `scripts/secrets_mgmt/git_dates.py`: `RETIRED` names retired keys by design, so it
          holds `kuma_push` inside key names.
        - `scripts/dev/pytest_shard_weights.json` and
          `scripts/dev/pytest_shard_runner_weights.json`: generated records keyed by test node
          ids, which can name deleted tests. The docs-refresh cron rewrites both, so the row
          leaves them out of its selector rather than listing them, because a stale-allow
          failure would turn master red on the cron's own commit. The selector also leaves out
          every SOPS-encrypted file: a rotation rewrites them unattended, and base64 ciphertext
          can spell `kuma` by chance.
        - The census row itself and its red fixture.
        - The `kuma-check-` unit-name prefix, and the `/var/lib/kuma-check/` directory
          of the same units' down markers, wherever a file holds them. ADR 0026 records
          the operator's ruling that the timers keep their names.
        - The four Pi compose templates under
          `hosts/components/pi-compose/services/` with their inert
          `org.homelab.kuma-docker-monitor` labels (see *Open questions*, item 7).
        - `hosts/components/claude-code/files/homelab_rootq.py`: its `/api/push/` pattern is
          an output redaction rule, not a Kuma read.
        - Any file the B-7 and C-6 PRs could not reword, with its reason. At C-6 these are
          the 167 files of `_PROSE_HOLDERS`, none of which may hold an identifier above. C-6
          measured 553 lines in 208 files against its budget of about 100, so it reworded the
          files this plan assigns to it and #7194 owns the rest.
    - Test directories are outside the selector. Many tests use `uptime-kuma` as a sample deploy
      tag, and a test that reads Kuma is deleted with its subject (`.claude/rules/python-layout.md`,
      *A check retires when its subject does*).
4. `probe.py health <tag>` exits non-zero, naming the alert, while a non-shadow alert for
   `<tag>` fires that is `critical` or labelled `verify="true"` and became active after the
   tag's last apply. A test fixture shows it,
   and so does the live drill in *Verification*.
5. `probe.py monitors` lists firing alerts from `ALERTS`, and `probe.py kuma-drift` is gone from
   `probe.py --help`.

## Baseline at `70591c307`

### What M2 to M5 leave

- **Push tokens.** `ansible/secret_rotation.yml` lists 83 push tokens:
    - 44 belong to the rows of `CHECKS` in `check_table.py`.
    - 8 more carry the `monitor_bridge_` prefix but belong to other producers: `appsec`,
      `configarr`, `fake_remux`, `fake_remux_replace`, `home_allowlist`, `janitorr`,
      `pi_peers` and `renovate_alive`.
    - 31 carry other names.

  design.md's "52 of them the bridge's" counts by prefix. The bridge's own rows hold 44 (from
  `comm` over the two lists).
- **Retiring a key leaves its registry row behind.**
  `scripts/secrets_mgmt/secrets_lib/secret_registry.py:sync` adds missing rows and only
  reports stale ones, and `secret_rotation.py`'s `sync` help says existing entries are
  preserved. `secret_rotation.py audit --check` exits non-zero on any missing or stale row. The prek hook
  `secret-rotation-registry-sync` runs it on every change to `ansible/vars/secrets.yml` or
  `ansible/secret_rotation.yml`, and CI runs prek. Commit `6894e3523` ("Retire the E7-orphaned
  credentials") is the precedent: it deleted the retired keys' rows from
  `ansible/secret_rotation.yml` by hand, in the commit that removed the keys.
- **Bridge rows.** M2 removes no row: its PR 4a replaces `k8s_workloads` with
  `workload_log_errors`, and its PR 5 keeps `monitor_bridge_k8s_workloads_push_token` because
  that row still pushes on it. `check_table.py` holds 44 rows at the baseline, four of them
  gates. M5 removes three (`loki_ingestion`, `kuma_notify_failures` and the `loki_reachable`
  gate) and keeps `swallowed_verdicts` (M5 Open question 4). That leaves 41 rows: 38 checks
  plus the `prometheus`, `b2_reachable` and `wan_reachable` gates. Their sources, gates and
  grace:
    - **20 read Prometheus only:** `disk`, `cert`, `memory`, `restarts`, `oom`, `cpu`,
      `targets`, `traefik5xx`, `traefik_latency`, `traefik_404`, `traefik_421`,
      `etcd_db_size`, `host_temp`, `ups`, `shipper_dropped`, `cluster_targets`,
      `longhorn_volumes`, `pvc_fullness`, `snapshot_headroom` and `kubelet_plugin_readonly`.
      The folded arms on `memory` (`checks/host.py:with_claude_cgroups`) and
      `shipper_dropped` (its export-failure arm, which C-7's `OtelCollectorExportFailing` rule
      replaced) read Prometheus too.
    - **1 reads Prometheus and a TCP connect:** `pi_pressure`.
      `checks/host_edge.py:check_pi_pressure` ends in `with_pi_ports`, which TCP-connects from
      the bridge to each of the Pi's published ports (`PI_PUBLISHED_PORTS`, rendered from
      `pi_publishers` in `templates/env-secret.yaml.j2`) under a down streak. Its marker reads
      `DECIDED TCP connect is the whole signal`. No PromQL rule expresses that arm.
      Its test `test_dead_port_is_named_with_the_recreate_hint`, in `test_check_pi.py`, is the
      detector for row 6 of `docs/failure-classes.md`.
    - **15 read other sources:**
        - 4 behind the WAN gate: `discord`, `r2_usage`, `cloudflare_ips_drift` and
          `healthchecks_drift`.
        - 1 behind B2: `b2_storage`.
        - 5 under `startup_grace`: `n8n`, `bazarr`, `prowlarr_indexers`, `scrutiny` and
          `speedtest`.
        - 5 with no gate: `arr_queue`, `gitops_alive`, `gitops_status`,
          `etcd_restore_drill` and `ha_heartbeat`.
    - **2 read loki-homelab, both under `startup_grace`:**
        - `workload_log_errors`, which M2's PR 4a creates. Its body calls
          `checks/logs.py:with_log_errors`, whose only caller at the baseline is the
          `k8s_workloads` body in `checks/cluster.py`. M5 keeps `with_log_errors` and
          `LOKI_URL` (its PR 4), and M5's PR 5 does not retire this row's token. M2's Open
          question 6 lists what the port deletes.
        - `swallowed_verdicts`, whose `gate` M5's PR 4 changes from `loki_reachable` to
          `startup_grace`. It reads `checks/logs.py:SWALLOWED_VERDICTS_LOGQL` (host syslog)
          and `SWALLOWED_VERDICTS_POD_LOGQL` (the `pi-peer-backup` container), and it is not
          `critical`. `verdicts/logs.py:swallowed_verdicts` pages on a `rejected` push, one
          Kuma answered with `by=kuma` because no live monitor holds the token, and on a lost
          `status=down` push while another tag landed one. It skips the tags in
          `HC_ROUTED_TAGS`, which `test_hc_routed_tags_are_exactly_the_scripts_that_read_kuma_push_ok`
          pins to the `KUMA_PUSH_OK` readers. M5 leaves the row and
          `monitor_bridge_swallowed_verdicts_push_token` to M6.
- **`startup_grace` is a plain streak.** `check.py:run_once` calls
  `bridge.streaks.apply_startup_grace` on every down cycle of a `startup_grace` row, with no
  test of when its target started. `GRACE_CYCLES` is `"2"` in `templates/env-secret.yaml.j2`,
  so a page waits one extra 300s cycle whatever the target's age.
- **Other producers.** M3 removes 1, leaving 38, and M6 moves all 38. Most are host scripts; four run in pods. Where each lives:
    - **daniel-box host crons in the k3s role:** longhorn backup health, the etcd snapshot,
      manifest-prune, release staleness and live-drift. M6 owns the last three only until
      their Kuma side is gone. After C-2b, D6.17 disarms the checks and D6.18 deletes them
      (*Cross-slice follow-up*, #4609).
    - **The initial_setup role, on both nodes:** docs-refresh, the eval sweep, the two
      Loki-route witnesses, secret rotation and setup drift.
    - **The gitops_deploy role:** the ruleset-drift and interaction-limit timers.
    - **Other setup roles:** two etcd drills (hypervisor), three fake_remux pushes, two UPS
      secondaries (nut_host), render_records, renovate_agent and renovate_notify.
    - **The Pi:** SD-card health and container recovery (optimize_pi).
    - **Two daniel-box crons, installed by `setup/claude_code`:** artifacts sync and the
      telemetry health check (B6a).
    - **Five pod producers that B6 first took for host crons (B6b):** the crowdsec-jobs
      sidecar's three Kuma-pushed jobs (remote allowlist, AppSec verify, home allowlist) and the
      `configarr-health` and `janitorr-health` CronJobs. H2 moved all five into pods before M6
      started, so no textfile reaches them. The operator ruled on 2026-10-23 that a pod exports
      through a metrics endpoint, not a hostPath textfile and not a Pushgateway.
    - **B6c, registry-gc.** The weekly mirror GC cron that `setup/k8s_host_files` installs on
      daniel-box. The plan first left it to D5's Wave D registry PR, which was to retire the
      GC with its tile and token. D5's registry PRs landed (#5689, #5729, #5746, and the flip
      in #5830), and #5746 retired only the push store's GC phase. The mirror phases still
      run and still push, so M6 drains the push like any other host cron (#6926, Open
      question 13).
    - **Pods:** autofix-bridge (a Deployment), the two cloudflare-ddns Deployments, the
      pi-peer-backup CronJob, and Kuma's own status-page sync.
- **Push-tile intervals** (`static-monitors.yaml.j2`, `interval`). Bridge tiles use
  `uptime_kuma_k8s_bridge_push_interval` (1200s). Of the host tiles, the three fake_remux tiles
  and the two UPS secondaries (`nut_host_watchdog_interval_minutes` × 120) are 1200s. Several
  k8s-role cron tiles are 600s. The B2 and B3 tiles are 7200s or more, and so are the etcd
  drills, render_records, renovate_agent and renovate_notify.
- **Hand-written probes.** M4 moves the 26 generated ingress tiles. Live, Kuma also exports 14
  non-push monitors from `static-monitors.yaml.j2`:
    - six HTTP: the Pi's wg-easy, Email-to-RSS, iCal proxy, LiveSync's 401, Scrutiny and
      Loki's `/ready`;
    - two keyword: the edge all-clear and the homelab-mcp edge gate;
    - one DNS: Pi-hole on the DNS VIP;
    - four port: the Jellyfin VIP, the Mosquitto VIP, `nut` and the SLZB coordinator;
    - one ping: the Pi host.

### Which plane applies each producer

`scripts/deploy_tools/setup_routing.py --host daniel-box` derives the routing below.
`deploy_setup_roles.py:tick_applies_setup_role` is true only for an `initial_setup.yml` role
whose `when:` admits the tick's host.

- **The GitOps tick, on daniel-box, at fast-forward:** `initial_setup`, `gitops_deploy`,
  `hypervisor`, `fake_remux`, `nut_host`, `render_records`, `renovate_agent`,
  `renovate_notify` and `k8s_host_files`. The tick applies them before `land.sh` deploys any k8s role.
- **A hand, never the tick:**
    - `k3s` routes to `k3s-bringup.yml`, which `deploy_changes.py:_BROAD_MANUAL_PREFIXES`
      keeps out of the tick. The tick records it in `manual_plane`.
    - `optimize_pi` has `host: daniel-pi`. Its apply is
      `initial_setup.yml --tags optimize_pi -e target=daniel-pi`, one play at a time on the Pi.
    - Every daniel-server half (initial_setup's second Loki witness, nut_host's daniel-server
      secondary). No tick runs on daniel-server, and `land.sh`'s note on the hosts the tick
      does not reach names that run.
- **`land.sh`, as k8s role deploys, or Flux:** every k8s role, including B6b's three
  (`crowdsec`, `configarr`, `janitorr`, each `owner: flux` by the end of Wave D, so the merge is
  the apply) and the pods (B7). B6a's two crons are a setup role, `claude_code`, so the tick
  applies them. registry-gc's cron is in `k8s_host_files`, a setup role the tick applies
  too (B6c).

### When D5 flips a role M6 edits

The planes above name the apply while a role is Ansible-owned. D5's Wave D flips most roles
M6 edits, in the order of its wave table, and from each flip on, C4 has `land.sh`, the tick
and `deploy.sh` skip the role. For an `owner: flux` role the merge is the apply. Flux
reconciles the commit, and a PR's live read waits until the role's Kustomization reports that
commit (C6), not for a `land.sh` `VERDICT:` line. C-1a waits for B6c's token PR, which
flips no role. Every k8s role that C-1 and C-2 edit was flux-owned by the end of Wave D
(2026-10-22).

Most M6 PRs change one pager side, so the owner decides only who applies them. Three steps
need one role's apply to land before another's. While a role was Ansible-owned, the toposort
inside one `land.sh` run gave that order. A Flux `dependsOn` cannot replace it across planes:
it orders two Kustomizations, never a Flux apply against a `land.sh` deploy. So monitor-bridge
stays Ansible-owned until its step has deployed, by an index sub-edge (Open question 1):

- **monitor-bridge flips after A6-7a has deployed.** Every bridge-row Cutover-a, A6-7a
  included, deletes a tile and its push in one edit. Only `uptime-kuma` deploying before
  `monitor-bridge` keeps the tile from going DOWN (*The cutover order*). After A6-7a no row
  pushes, and every later monitor-bridge change is order-free, C-7's included.

The other two steps are C-1's, and the roles they order are flux-owned before C-1. The
operator ruled on 2026-10-21 that D5 flips uptime-kuma with janitorr and jellyfin (#5820),
because holding it for C-1 put D5's last three PRs behind Phase C (#5821). observability
flipped in #5816, before C-1, and netpol-baseline flipped last in Wave D (#5833). Phase C therefore
orders these steps inside Flux:

- **netpol-baseline's half needs no split.** C-1's `netpol-baseline` change removes the
  callers policy that the status-page sync CronJob's push crosses. Applied before the
  `uptime-kuma` prune deletes that CronJob, it refuses the push, and Kuma pages its
  `Status Page Sync` tile 15 to 30 minutes later: the tile's `interval` is 1800 s and the
  CronJob runs every 15 minutes. netpol-baseline is `K8S_APPLIES_LAST`, so
  `toposort.py:build_k8s_dep_map` makes it depend on every other entry, and its Kustomization
  carries `dependsOn: uptime-kuma` (a `homelab-render cluster --role netpol-baseline
  uptime-kuma observability` render at `fdae34795` shows the edge). Within one promotion Flux
  applies the callers policy only after uptime-kuma's prune reads Ready. The same holds for
  the probe row, because `netpol-baseline-verify` depends on `netpol-baseline-deferred`.
  C-2a's order is free, because no Kuma pod is left by then.
- **observability's half is a PR of its own, C-1a.** C-1 removes `KumaDown` before Kuma's
  prune, because a Kuma pruned first fires `KumaDown`'s `absent()` arm 5 minutes later. The
  same render gives the `observability` and `uptime-kuma` Kustomizations no edge between
  them: each depends on `authelia`, `cluster-vars` and `traefik` alone. Within one promotion
  Flux reconciles the two in parallel. So C-1a removes `KumaDown` and the scrape job, and C-1b
  empties the role only after a live read shows both gone (*Phase C*).
    - **Rejected: a `depends_on: [observability]` edge on uptime-kuma's entry.** The edge
      would run the other way from the one #5821's comment names: uptime-kuma has to wait on
      observability, not the reverse. It costs a PR of its own, because arm 2 of
      `scripts/validate/render_diff_gates.py` refuses a tag that gains an edge while its own
      render moves. It is also weaker than the split. A Ready `observability` Kustomization
      shows that the apply finished, not that Prometheus reloaded its rules, and the split
      reads the reload itself.
- **uptime-kuma retires by D2's two PRs.** C-1b is the first: it empties the render down to
  the two claims, and Flux prunes every object the render drops. C-2b is the second: it removes the entry and the
  role once the prune has reached the cluster. Two other forms were rejected:
    - The Ansible tombstone this plan used before #5821. The tombstone ConfigMap existed only
      to satisfy the `k8s/manifests` prune guard, which never runs for a flux-owned tag. A
      flux-owned tag may render an empty directory (`docs/flux-deploy-plane.md`, *Retiring a
      flux-owned service*).
    - A handback, `owner: released` and then Ansible's removal. `deletionPolicy: Orphan`
      leaves every object serving with no writer, and arm 4 refuses a flux-owned name in
      `k8s_retired_services`. The retirement would cost three PRs and an Ansible deploy for
      a role D6 is about to remove the path of.

Every other role M6 edits needs no edge:

- **authelia.** C-1b's bypass removal is safe in either order against the route's prune. Each
  order leaves `uptime-kuma.local` under a tier at least as strict as the bypass.
- **loki-homelab (Wave A'), autofix-bridge, cloudflare-ddns, crowdsec, artifacts and B6's
  roles.** Their shadow PRs, their Cutover-b or Cutover-1b, their Cutover-2 and C-7 each change
  one side after a Cutover-a or Cutover-1a has removed the tile, so either plane can apply
  them. A B batch's `KUMA_RETIRING_TAGS` read and the two-hour bound read the applied commit,
  whoever applied it.
- **configarr and janitorr.** D5 flips each only after its H2 family PR (H2.5b, H2.5c) and its
  `k8s_host_files` conversion PR (`d5-bulk-move.md` item 26). H2.5b removes configarr's
  `install_host_lib.yml` and `stamp_deployed.yml` imports, and H2's janitorr PR does the same
  for janitorr. Both have flipped, so neither holds a `kuma_push.py` pair. Each `/opt` copy is
  an absent arm in `k8s_host_files`, which the tick applies. B6b kept `kuma_push.py` in each
  `health-configmap.yaml.j2`, because `host_lib.py` imports it at module level, so C-3a edits
  both flux-owned roles (C-3a).

If the index declines the monitor-bridge edge, the fallback is the owner-independent form,
at a cost. A flux-owned monitor-bridge splits each remaining bridge-row Cutover-a in two: an
exclusion in the `CHECKS` loop of `static-monitors.yaml.j2` drops the tile first, and the row
PR follows a read that the tile is gone.

### Kuma's footprint

- **The census.** `git grep -il kuma 70591c307` lists 548 files. The largest groups are:
    - `ansible/tests` (90) and `ansible/roles/k8s/monitor-bridge` (52);
    - `docs/archive` (29), which is history and stays;
    - `scripts/diagnostics` (25) and `scripts/deploy_tools` (24), mostly fixtures that use
      `uptime-kuma` as a sample tag;
    - `ansible/roles/k8s/uptime-kuma` (21), `ansible/roles/setup/k3s` (17) and
      `ansible/roles/setup/initial_setup` (14).
  Restricted to non-test files under `ansible`, `scripts`, `.claude` and `evals`, it is still
  about 200 files, 38 of them non-test scripts. That is why exit criterion 3 is functional.
- **The uptime-kuma role.** 3,655 lines in `git ls-files`. Its parts:
    - 4 images: Kuma, AutoKuma, the kuma CLI and the Python status-page sync.
    - 2 no-backup claims, `uptime-kuma-data` and `autokuma-data`, declared as
      `no_backup_claims` on its `containers_list` entry.
    - 2 CronJobs: `kuma-maintenance-sync` and `kuma-status-page-sync`.
    - The 540-line `static-monitors.yaml.j2`. Its bridge tiles render from a loop over
      monitor-bridge's `CHECKS`, reading each row's `kuma_id`, `display`, `description`,
      `token` (through `lookup('vars', ...)`) and `critical`.
      `templates/status-page-sync-configmap.yaml.j2` reads each row's `status_group` and
      `kuma_id` from the same table.
    - The secrets `uptime_kuma_username` and `uptime_kuma_password`, and Prometheus's scrape
      credential `prometheus_kuma_api_key`.
- **Seven template files and two shared defaults.** `git ls-files` on the role's `templates/`
  lists seven files, none of them a Service or an IngressRoute. Those two come from
  `ansible/roles/k8s/manifests/defaults/main.yml:manifests_shared_defaults`, which
  `manifests/tasks/main.yml` adds because the entry in `host_vars/daniel-box.yml` sets `port`
  and `hostname`.
- **`entry_port('uptime-kuma')` callers:** the push URLs in monitor-bridge, autofix-bridge,
  both cloudflare-ddns Deployments and pi-peer-backup; the role's own templates; and
  Prometheus's `uptime-kuma` scrape target in `prometheus.yaml.j2`. One more read goes through
  an indirection: `netpol-baseline/templates/netpol-probe-job.yaml.j2` sets
  `port = t.port if t.port is defined else containers_list | entry_port(t.service)`, and the
  `uptime-kuma` row of `netpol_baseline_probe_targets` has no `port`. `entry_port` raises
  ValueError for an entry with no `port`.
- **The entry's `hostname` readers.** Homepage's `templates/config/services.yaml.j2` calls
  `containers_list | homepage_href('uptime-kuma', domain)`, and
  `filter_plugins/homepage_tiles.py:homepage_href` raises ValueError for an entry with no
  `hostname`.
- **Authelia.** The entry sets `use_authelia: true` and `auth_tier: one_factor`, which
  `filter_plugins/authelia_access.py:authelia_service_rules` turns into a generated rule.
  `authelia/templates/config-secret.yaml.j2` also holds a hand-written `uptime-kuma.local`
  bypass for `/api/push/`, `/metrics` and `/api/status-page/`, ahead of the generated rules.
- **The push plumbing:**
    - `ansible/roles/setup/initial_setup/files/kuma-push-lib.sh` (278 lines). It holds
      `kuma_push`, and also `boot_grace_active`, `wan_reachable` and `reachout_verdict`, which
      host scripts use regardless of Kuma. `kuma_push` returns 0 on a refused push and logs
      `push failed (… by=kuma …)`.
    - `ansible/roles/setup/common/files/kuma_push.py` (130 lines), which `host_lib.py`
      re-exports. autofix-bridge (`defaults/main.yml`) and the status-page sync stage it in
      their pods. `setup/common/tasks/install_host_lib.yml` copies it beside every
      `host_lib.py` (`loop: [kuma_push.py, host_lib.py]`), and
      `test_host_lib_sibling_copies.py` holds each copy to a stamp. Eight `stamp_deployed`
      pairs name it: `gitops_deploy/tasks/code.yml`, `renovate_agent/tasks/code.yml`,
      `renovate_notify/tasks/code.yml`, `fake_remux/tasks/main.yml`,
      `k8s/configarr/tasks/main.yml`, `k8s/janitorr/tasks/main.yml`, and two in
      `k3s/tasks/health-crons.yml` ("Record the deployed Longhorn backup health reader",
      tagged `backup-health`, and "Record the deployed live-drift check", tagged
      `live-drift`). configarr and janitorr import `install_host_lib.yml`
      (`deploy_cross_role.K8S_ROLES_IMPORTING_SETUP_FILES`). Both facts moved after this
      baseline. H7.2e moved the file to `hosts/lib/kuma_push.py`, and `ca9ea9da0` gave
      `deploy_ui/tasks/code.yml` a ninth pair. *Files under `hosts/`* below and C-3a use
      the paths at HEAD.
    - `ansible/roles/setup/common/tasks/kuma_check_timer.yml` (118 lines) and its two unit
      templates, with 8 callers (`KNOWN_CHECKS`). Every unit path derives from
      `kuma-check-{{ kuma_check_name }}`, and the `absent` arm removes only those names.
    - `deploy_cross_role.py:SETUP_FILES_SHIPPED_BY_OTHER_ROLES` names `kuma_check_timer.yml`,
      both unit templates and `kuma_push.py`. Its k8s table maps `kuma_push.py` to
      `autofix-bridge` and `uptime-kuma`. `scripts/deploy_tools/narrow_setup.py` and
      `setup_role_chains.py` name `kuma_check_timer.yml` too.
- **The readers:**
    - `probe.py monitors`, which reads `monitor_status`, and `probe.py kuma-drift`, both in
      `scripts/diagnostics/probe_lib/monitors.py` (597 lines). `monitors.py` imports
      `probe_lib/kuma_table_loop.py`, which parses the two table loops in
      `static-monitors.yaml.j2`. Also `probe.py alerts` (`probe_lib/alerts.py`), which
      rebuilds bridge DOWN history from Loki.
    - `scripts/diagnostics/postflight.py` imports `kuma_live` and `monitors` at module level.
      `check_kuma_monitors` and `check_kuma_drift` read them, and `check_kuma_scrape` reads
      `up{job="uptime-kuma"}`.
    - Triage's poller, `hosts/components/claude-code/files/claude_triage_poll.py`, which
      both host packages ship. It opens episodes from `monitor_status{job="uptime-kuma"}`
      and holds every Kuma key while `up{job="uptime-kuma"}` is not 1. Once C-1a deletes
      the scrape job, that `up` series stops existing, so a poller still reading Kuma would
      freeze every Kuma key in its last state (#6862).
    - homelab-mcp has no Kuma tool and imports no `kuma_live`.
      `git grep -l kuma_live` lists only `postflight.py`, `probe_lib/monitors.py`, their tests,
      `docs/reference/scripts.md` and `pytest_shard_weights.json`. One sentence in its
      `CLAUDE.md` says its `/health` router exists so the Kuma tile can prove the edge routes.
    - Homepage's `uptimekuma` widget in `templates/config/services.yaml.j2`, with its
      block-label overrides in `custom.css.j2`. They are held by
      `ansible/tests/services/test_homepage_block_label_overrides.py`.
    - The Grafana boards `Apps/uptime-kuma-monitors.json` and
      `Infrastructure/alert-history.json`.
    - Prometheus's `uptime-kuma` scrape job, which reaches the Service directly with the API
      key as basic auth.
    - `scripts/docs/fragments_lib/fragments_bridge.py` reads each row's `display` and
      `status_group`, and the `bridge-gate-sets` fragment parses `gates.py`'s `_members`
      assignments.
- **The NetworkPolicy grants that name Kuma:**
    - the `netpol_from` list on the uptime-kuma entry, which has 7 callers;
    - `uptime-kuma` as a caller in `networkpolicy-loki-homelab.yaml.j2`,
      `networkpolicy-mosquitto.yaml.j2`, `networkpolicy-nut.yaml.j2` and
      `networkpolicy-jellyfin.yaml.j2`, and in ical-proxy's `netpol_from`;
    - the `uptime-kuma` row in `netpol_baseline_probe_targets`. Its port comes from the entry
      (above). The task "Fail if any probe target has no ready endpoint" in
      `netpol-baseline/tasks/main.yml` also fails the play for an active row whose Service has
      no ready endpoint. That task is `when: not k8s_no_mutate`, so a `--dry-run` skips it.
- **Healthchecks.io.** `uptime-kuma-alive` is pinged by `longhorn-backup-health.sh.j2` with its
  Kuma push's status (`docs/healthchecks-io-deadman.md`, *Watching the spine itself*). Its
  consumers: `monitor-bridge/defaults/main.yml:monitor_bridge_healthchecks_expected` (read by
  the `healthchecks_drift` row), the deadman rows in `scripts/docs/fragment_renderers.py`, and
  the tests `scripts/docs/tests/test_fragments_storage.py`,
  `scripts/docs/tests/test_gen_doc_fragments.py`,
  `ansible/tests/services/test_monitor_bridge_healthchecks_expected.py`,
  `hosts/tests/test_health_cron_boot_grace.py` and
  `scripts/validate/tests/test_backup_health_shim.py`.
- **M5's Kuma rule.** M5 adds one Kuma rule, `KumaNotificationDropped` (`critical`,
  `service: uptime-kuma`), to `loki-homelab/files/rules/homelab-logs.yaml`. It adds no
  `KumaRefusedPush` and no `HostPushVerdictSwallowed` (M5 Open question 4). A loki-homelab
  rule never reaches Prometheus `ALERTS` (M5 open question 1). The same file keeps the three
  `Loki*LogsSilent` rules, and `LokiPodLogsSilent` is the census's named member.
- **The M5 handoff gate.** M5's *Unblocks* line gates M6's first Cutover-1a on the M5-to-M6
  row that M5 Open question 4 requests in contracts.md's *Requested amendments not yet
  applied*. At `70591c307` that list holds no M5 row.
- **The Pi node_exporter unit.** `optimize_pi/templates/node_exporter.service.j2` passes
  `--no-collector.textfile`, and the Pi has no textfile directory.
- **Narrowing.** `narrow_lib/containers.py:changed_fields` says an added or removed entry
  "moves every reader that iterates the list". `narrow_lib/filters.py` refuses a deleted
  filter plugin. `docs/gitops-pipeline.md` (*A deploy-plane change is narrowed before it is
  applied*) says a refusal runs no playbook and records `k8s_unapplied` lines.
- **No route to Alertmanager.** M1 adds none, so a silence is an exec into the pod
  (**operator-only**, M1 open question 2).
- **No ADR covers the alert plane.** design.md's *ADR changes this migration needs* assigns
  "one alert plane on Prometheus and Alertmanager, retiring Uptime Kuma (M6)", operator-only.

## Design

### Recipes

Every batch below applies one recipe a previous slice proved, and follows M1 conventions 5 and
6. The recipes:

- **M2: a Prometheus-reading bridge check becomes a rule.** A shadow PR adds the rules and
  their tests. A 7-day soak gates on the query. The cutover is M2's two PRs (*The cutover is
  two PRs*): PR a deletes the row, its body, its tests and its env keys with every rule still
  shadow, and PR b drops `shadow`. A token PR retires the push token (**operator-only** half
  first, see *Steps*).
- **M5: a Loki-reading check becomes a LogQL rule** on the loki-homelab ruler, with M5's
  three costs (A7).
- **M3: a host producer writes `/var/lib/node-exporter-textfile/<check>.prom`.** It writes the
  `homelab_check_*` series, and two rules per check (`<Check>Failed` and `<Check>NotReporting`)
  carry the deadline its template derives from its own schedule.
- **M4: a probe becomes a blackbox_exporter module.** The probe job carries `service` and
  `page_tier` labels, and the rules select on `page_tier`.
- **Exporter: a check that only the bridge can read.** The bridge exposes the same
  `homelab_check_*` series on `/metrics`, one `check` label per row, and Prometheus scrapes it.
  Each exporter also gets an aggregate staleness rule (see A6), because a hung loop behind a
  live HTTP thread keeps `up` at 1.
- **CronJob: a producer that is a CronJob.** Rules read kube-state-metrics'
  `kube_cronjob_status_last_successful_time` and `kube_job_status_failed`.

### The cutover order

Each check pages from Kuma or from Alertmanager, never both (M1 convention 5). One cutover
PR that both removes a Kuma pager and drops `shadow` cannot keep that. The k8s play deploys
roles in toposorted order: `ansible/filter_plugins/toposort.py` over daniel-box's
`containers_list` puts `observability` before `uptime-kuma` and `uptime-kuma` before
`monitor-bridge`, as M2's PR 4a states. So `observability` ships the live rules first, while
the Kuma pager still runs. Once the commit merges, any other session's `observability`
deploy also ships the live rules from master's tip. Phase A runs batches in parallel, so those
deploys are frequent. M2's *The cutover is two PRs* rejects the one-PR form on exactly these
grounds.

Every cutover therefore splits so that each PR changes one pager side only. Then no merge, no
interleaved deploy and no toposort order can leave a check with two pagers. The worst case is
a short window with none, which is the safe direction of convention 5.

- **A bridge row (A1 to A5, A7, B8, and A6-7 as A6-7a and A6-7b).** M2's PR 4a and PR 4b.
  B8 takes this form because its 14 probes are Kuma's own and its one push goes with the
  `pi_pressure` row: its Cutover-a deletes the 14 tiles and that row together.
    1. **Cutover-a, the Kuma side goes.** Delete the row. That removes its tile (the derived
       loop in `static-monitors.yaml.j2`) and its push in one edit. Every rule stays shadow.
       `land.sh` deploys `uptime-kuma` before `monitor-bridge`, so the tile is gone before the
       push stops and never goes DOWN. Both roles are Ansible-owned for every Cutover-a, by
       the monitor-bridge edge in *When D5 flips a role M6 edits*. The PR records a read of
       `monitor_status{monitor_name="<tile>"}` returning nothing for each tile, and the
       `monitor-bridge` deploy time.
    2. **Cutover-b, the rules page.** Drop `shadow`. `land.sh` deploys `observability` alone
       (`loki-homelab` for A7). Cutover-b is open and green, stacked on Cutover-a, before
       Cutover-a merges, and lands as soon as Cutover-a's reads pass.
    3. **The no-pager bound.** If Cutover-b has not deployed within two hours of Cutover-a's
       `monitor-bridge` deploy, revert Cutover-a. Its rules are still shadow, so the revert
       restores Kuma as the only pager.
- **Every other producer (B1 to B7, and B9).** Three PRs, because the producer's plane (above) decides
  when its change applies, and the tick applies a setup role before `land.sh` runs:
    1. **Cutover-1a, the tiles go.** Delete the batch's tiles from `static-monitors.yaml.j2`,
       with every rule still shadow. `land.sh` deploys `uptime-kuma`. The PR records the
       `monitor_status` reads, as above.
    2. **Cutover-1b, the rules page.** Drop `shadow`. `land.sh` deploys `observability` alone.
       It is stacked and green before Cutover-1a merges, under the same two-hour bound.
    3. **Cutover-2, the push stops.** Drop the push from the producer. Its plane applies it:
       the tick, a hand, or `land.sh`, per *Which plane applies each producer*.

  From Cutover-1a to Cutover-2, the producer pushes into a deleted token. `kuma_push` returns
  0 and logs `push failed (… by=kuma …)`. `swallowed_verdicts` reads that line as a
  `rejected` push and would page it, which is the M5 risk M6 must close before its first
  Cutover-1a (*Order*). M6 takes M5's first option: an exclusion, not an early deletion.
    - **The exclusion.** `verdicts/logs.py` gains a set, `KUMA_RETIRING_TAGS`, beside
      `HC_ROUTED_TAGS`. `swallowed_verdicts` leaves a tag in it out of the `rejected` set
      only. Its lost `status=down` pushes still count, and its `status=` lines still count as
      landed siblings. `HC_ROUTED_TAGS` cannot hold these tags, because its test pins it to
      the `KUMA_PUSH_OK` readers.
    - **Who adds a tag.** Each B1 to B7 shadow PR adds its producers' tags, as
      `parse_push_line` reads them (the script name for a host script, `pi-peer-backup` for
      B7's pod), and deploys `monitor-bridge` too. The tags are in the set a week before
      Cutover-1a deletes their tiles. Placing them in Cutover-1a instead would deploy
      `monitor-bridge` after `uptime-kuma` (the toposort), and a refused push read in that gap
      could hold two down cycles and page.
    - **B9's tags.** B9's shadow PR (`ab283899e`) merged before B9 joined this recipe, and
      it added no tag. A separate PR therefore adds `aide-check` and `rkhunter-check`, the
      `logger -t` tags of the two scans, with their cases, and deploys `monitor-bridge`. B9's
      Cutover-1a waits a week after that deploy, for the reason above.
    - **What the shadow week gives up.** While a batch's tile is live, a `rejected` push from
      its producer means its token matches no tile. That tile then receives no push and goes
      DOWN at its own interval, so the exclusion loses only the faster, named page.
    - **The test,** in `test_check_swallowed_verdicts.py`. A `rejected` line from a listed tag
      returns up, and the same line from an unlisted tag returns down. A lost `status=down`
      line from a listed tag still pages. Every listed tag is a syslog tag that a rendered
      producer logs under, derived from the tree the way the `HC_ROUTED_TAGS` test derives
      its readers, so a misspelt tag fails. The derivation covers a shell script's name, a
      Python producer's identifier (`kuma_push.py` logs `push failed (...)` too, for
      configarr, janitorr, renovate_agent and fake_remux), and `pi-peer-backup`.
    - **The `HC_ROUTED_TAGS` producers.** `longhorn-backup-health` (B1), `pi-sd-health` and
      `pi-recovery-health` (B5) read `KUMA_PUSH_OK`, which
      `test_hc_routed_tags_are_exactly_the_scripts_that_read_kuma_push_ok` derives from the
      renders. Their Cutover-2 drops the push, so the same commit drops their tags from
      `HC_ROUTED_TAGS`, or that test goes red, and deploys `monitor-bridge`. B1's and B5's
      shadow PRs add those tags to `KUMA_RETIRING_TAGS` as well. For B1, that keeps the
      refused pushes still inside the 3h window from paging once `HC_ROUTED_TAGS` loses them.
      For B5 the entry exempts nothing, as the next bullet explains.
    - **B1 is order-free; B5 is not.** `KUMA_RETIRING_TAGS` exempts a `rejected` line only,
      and only a line carrying `by=kuma` parses as `rejected`. B1's host cron logs to the
      daniel-box journal, which Loki reads, so a push that Kuma refuses carries `by=kuma` and
      the exemption covers it. `monitor-bridge` and B1's hand apply can then land in either
      order, and the class-2 residual in *Risks* covers the window between them. The Pi ships
      only `/var/log/pi-health/health.log` to Loki, because its journal is not shipped. A
      refused push there logs `push failed (status=down: ...)` with no `by=kuma`, so
      `parse_push_line` reads it as `swallowed`, and the exemption covers none of the Pi's
      lines. Once `monitor-bridge` applies B5's Cutover-2, `HC_ROUTED_TAGS` no longer exempts
      them either. From that apply until the Pi's `homelab-updater.timer` installs the
      package, every Pi cycle that ends `down` pages Swallowed Push Verdicts. That timer runs
      every 15 minutes and skips 23:00 to 00:59 and 04:00 to 05:59 UTC. No network fault is
      needed. The page duplicates the batch's live `PiRecoveryFailed` or `PiSdHealthFailed`
      rule for the same DOWN verdict, so it is a second page for a real fault, not a false one.
      B5 accepts the window: its Cutover-2 lands while both Pi checks read `status=up`, outside
      the updater's skip hours. B5's Cutover-2 landed in
      [#6794](https://github.com/DanielH2018/server/pull/6794) on 2026-11-01. Exempting a DOWN line without `by=kuma` would close the window,
      but it contradicts the exclusion's rule that a lost DOWN still counts.

  Land Cutover-2 the same day anyway. No step depends on a tile's interval, so a slow tick or a
  delayed hand apply leaves no check unpaged.
- **No hand apply of a tick-applied role on daniel-box.** The tick is the one writer for
  those roles there (checklist 1). A daniel-server half, `k3s` and `optimize_pi` take a hand
  run, which is **operator-only** where it needs `become`.
- **A `k3s` hand apply names its task tags, never `--tags k3s`.** The `setup/k3s` `CLAUDE.md`
  calls `--tags k3s` the maximal apply, which makes three control-plane gates reachable. Each
  task in `tasks/health-crons.yml` carries a per-producer tag (`backup-health`,
  `etcd-snapshot`, `manifest-prune`, `release-staleness`, `live-drift`), and the two
  `kuma_check_timer.yml` imports there carry `manifest-prune` and `live-drift`. Every `k3s`
  apply below is run on daniel-box as
  `uv run ansible-playbook ansible/k3s-bringup.yml --tags "<the PR's tags>"`
  (**operator-only**), with the tags the PR names.

### No-pager windows

About 15 batches each open a window in which their checks have no live pager. It runs from
the Kuma-side PR's deploy to the shadow-dropping PR's deploy. The two-hour bound above
depends on someone remembering to revert. Three rules bound the windows without that.

- **One window at a time.** A batch's Kuma-side PR merges only when no other batch's window
  is open.
    - **One window may carry several batches whose soaks are clean** (operator ruling of
      2026-10-31). One Cutover-1a PR deletes all their tiles, and one Cutover-1b PR drops
      `shadow` from all their rules under the same two-hour bound. Each producer plane then
      gets one Cutover-2 PR. A batch whose soak has a blocking read stays out of the window and
      waits for its own. The first combined window carries B2, B4 and B6b.
- **`CheckWithoutPager`, a temporary rule.** It lives in its own group, `kuma-cutover`, in
  `files/rules/kuma-cutover.yaml`.
    - A rule that does not fire has no series, so PromQL cannot read its `shadow` label. The
      rule therefore lists the tiles itself. Each shadow PR adds one term per tile its batch
      retires, `absent_over_time(monitor_status{monitor_name="<tile>"}[2h])`. Each shadow-dropping PR
      (Cutover-b, Cutover-1b, A6-7b) removes its batch's terms. A term exists exactly while
      its rule is `shadow`, and it reads 1 once the tile has been gone for 2h.
    - The expression joins the terms with `or`, then applies
      `unless on() (up{job="uptime-kuma"} == 0)`, with no `for:`. The 2h hold is the range,
      because the deploy soak waits out the longest `for:` of every critical rule that is not
      shadow (`renderer_lib/soak.py:longest_for`), and a `for: 2h` would lengthen every
      promotion's soak by 2h and fail `test_lander_deadlines_cover_the_soak.py`. A Kuma outage
      fires no term, because Kuma's own liveness pager covers it. `absent_over_time()` carries
      the tile's name as a label, so the page names the tile. A misspelt tile name fires two hours after its
      shadow PR deploys, so the soak catches it.
    - It is `critical`, with `service: uptime-kuma`, the role whose tiles it counts. It
      does not carry `service: observability`. Until B-1, C7's literal gate fails on any
      firing alert for a tag, and the shadow-dropping PR that ends a window deploys
      `observability`.
    - With empty input every term fires, which is the group's zero-series case (M1
      convention 2).
    - A rule with no term is not valid PromQL, and shadow PRs overlap. So a PR that removes
      the last term deletes the group and its `observability_rule_groups` entry, and a PR
      that adds a term to a missing group re-creates it. After Phase A's last
      shadow-dropping PR, the group stays deleted.
- **A6-7a in a quiet window.** A6-7a opens the widest window, over 16 checks and two gates.
  It merges only when no other window is open, not within two hours of the weekly reboot
  (about 07:35), with A6-7b open and green, and with the operator reachable for the two
  hours that follow.

### Phase A — drain the producers

Each batch is one shadow PR, its cutover PRs (above), and a token PR, each about 100 to 200
lines or fewer. The shadow PRs and their soaks run in parallel when their batches have no
dependency between them. The cutover windows run one after another (*No-pager windows*).

| Batch | What moves | Recipe | Plane of the producer change | Gate, grace and inhibit replacements | Tokens |
|---|---|---|---|---|---|
| A1 hosts | `disk`, `memory`, `host_temp`, `ups`, and the Prometheus half of `pi_pressure` | M2 | monitor-bridge (`land.sh`) | `EXPORTER_DEPENDENT` becomes an inhibit rule whose source is A3's `ScrapeTargetDown`, not a new rule. For each job the map keys (`node`: disk, memory, host_temp; `node-pi`: host_temp, pi_pressure), `ScrapeTargetDown{job="<job>"}` inhibits those alerts with `equal: [job, instance]`, so a dead exporter holds only its own host's alerts where the map held them estate-wide. `origin` is not used: most cluster series lack it, and a label missing on both sides compares equal. The inhibit lands in the later of A1's Cutover-b and A3's Cutover-b (*A3 and the handoffs it closes*, task 6). Each target arm that fires on missing data (an `absent()` or not-reporting arm) has a `for:` of at least `ScrapeTargetDown`'s 10m, or it fires before its source. Threshold arms need no floor, because a dead exporter leaves them no data. Streaks such as `HWMON_TEMP_CONSECUTIVE` become `for:`. The `pi_pressure` row cuts over in B8, not here. | 4 |
| A2 containers | `restarts`, `oom`, `cpu` | M2 | monitor-bridge | Joins `homelab:pod_service:info`. `CADVISOR_PODS_MIN` becomes a census alert with a zero-series case. | 3 |
| A3 scrape | `targets`, `cluster_targets`, `shipper_dropped` | M2 | monitor-bridge | `CLUSTER_TARGETS_CONSECUTIVE` becomes `for: 10m`. Closes M1's, M4's and M5's handoffs: see *A3 and the handoffs it closes*. Cutover-a was gated on the flapping-target ruling in [#6626](https://github.com/DanielH2018/server/issues/6626), closed by [#6640](https://github.com/DanielH2018/server/pull/6640) (the soak read in *A3 scrape*, under *Status by batch*). | 3 |
| A4 edge | `cert`, `traefik5xx`, `traefik_latency`, `traefik_404`, `traefik_421` | M2 | monitor-bridge | The volume floors (`TRAEFIK_MIN_RPS`) become `and` clauses. | 5 |
| A5 storage | `longhorn_volumes`, `pvc_fullness`, `snapshot_headroom`, `kubelet_plugin_readonly`, `etcd_db_size` | M2 | monitor-bridge | All critical. Longhorn's one-hot `state` label is selected explicitly. | 5 |
| A6 bridge exporter | the 15 other-source checks, `swallowed_verdicts` and `host_packages`, plus the `b2_reachable` and `wan_reachable` gates | Exporter | monitor-bridge | See *The bridge as an exporter* and *Amendment for host_packages and a flux-owned bridge*. The `prometheus` gate retires with its token, because the Watchdog covers a dead Prometheus. `swallowed_verdicts` is exported until C-7 deletes it. | 20 |
| A7 log arm | `workload_log_errors` | M5 | monitor-bridge (`land.sh`); the rule deploys `loki-homelab` | `startup_grace` becomes `for: 5m`, as in A6. See *A7*. | 1 |
| B0 shared library | No check. Moves `boot_grace_active`, `wan_reachable` and `reachout_verdict` into `homelab-check-lib.sh`, which also gains `write_check_textfile` (M3's atomic write). Its copy task and its `stamp_deployed` record in `initial_setup/tasks/crons.yml` are `tags: [always]`, like the `kuma-push-lib.sh` pair beside them (*B0, B5, B7 and B8*). | — | the tick (initial_setup) on daniel-box; a hand on daniel-server and the Pi | — | 0 |
| B1 k3s crons | longhorn backup health, etcd snapshot, manifest-prune, release staleness, live-drift | M3 | a hand: `k3s-bringup.yml --tags "backup-health,etcd-snapshot,manifest-prune,release-staleness,live-drift"` (**operator-only**) | `KumaDown` replaces `uptime-kuma-alive` (see below). B1 retires the tiles and tokens of manifest-prune, release staleness and live-drift; D6.17 disarms those three checks and deletes B1's rules for them, and D6.18 deletes their code. | 5 |
| B2 initial_setup | docs-refresh, eval sweep, secret rotation, setup drift, the two Loki-route witnesses | M3 | the tick on daniel-box; a hand on daniel-server | daniel-server gets the textfile-directory task. The shadow PR adds six `CheckWithoutPager` terms. Each range is max(2h, 2 x the tile's push interval + 10m): `[4h10m]` for the two Loki Read Route tiles (7200s interval), `[26h10m]` for Docs Refresh (46800s), `[50h10m]` for Secret Rotation and Setup Plane Drift (agent hosts) (90000s), and `[348h10m]` for Homelab Evals. A push tile reports no `monitor_status` series after a Kuma restart until its next push, and Kuma restarted 19 times in the 7 days before this PR, so a `[2h]` range would page falsely for the slower tiles. | 6 |
| B3 gitops_deploy | ruleset drift, interaction limit | M3 | the tick | The shadow PR adds two `CheckWithoutPager` terms, each `[50h10m]`: both tiles have a 90000s interval, and 2 x 90000s + 10m is 50h10m. Both rules carry `service: github`, the thing checked. | 2 |
| B4 other setup roles | two etcd drills, three fake_remux, two UPS secondaries, render_records, renovate_agent, renovate_notify | M3 | the tick on daniel-box; a hand on daniel-server for its UPS secondary and for both etcd drills, which run only there: `initial_setup.yml --tags "nut_host,hypervisor-etcd-drill"` (**operator-only**) | The shadow PR leaves render_records out: D5 pinned its role absent (`render_records_state: absent`) and removed its tile, so there is nothing to shadow. fake_remux and the drills push several tiles under one syslog tag, so each verdict has its own check name. The renovate pair writes its textfile from a root-run `ExecStartPost` and has a NotReporting rule only, because a crash writes nothing. It adds seven `CheckWithoutPager` terms: `[2h]` for the five 1200s tiles, `[72h10m]` for the notifier and `[56h10m]` for the agent. The two drill tiles get none, since a monthly tile's range of 70d10m exceeds Prometheus's 30d retention. Their window is the batch's own: Cutover-1a deletes all of B4's tiles and Cutover-1b drops every B4 rule's `shadow`, so the five `[2h]` terms page an open B4 window by the second hour. `test_b4_rule_literals.py` fails while a drill rule is shadow and none of those five terms remains, and when a drill tile is deleted while all five tiles still render. `RenovateAgentNotReporting` follows `renovate_agent_enabled`: a disarmed agent's role writes `homelab_check_disarmed{check="renovate-agent"} 1`, and both that rule and `HostCheckOverdue` are quiet on the marker. The renovate pair's Cutover-2 lands in the same PR as B4's Cutover-1a (*Order*). | 10 |
| B5 Pi | SD-card health, container recovery | M3 | the `homelab-daniel-pi` package: the builder publishes it and the Pi's `homelab-updater.timer` installs it (H3.8b moved both heartbeats there). No `-e target=daniel-pi` apply. | H3.6 already enabled the textfile collector on the Pi (see below). The shadow PR ships `homelab-check-lib.sh` in the package and adds two `CheckWithoutPager` terms, both `[2h]` (the tiles push every 5 minutes, so max(2h, 2 x 10m + 10m) is 2h). | 2 |
| B6a host crons | artifacts sync, telemetry health (the two `setup/claude_code` crons) | M3 | the tick (`claude_code`) | They run on daniel-box's host, so M3's recipe applies as is. registry-gc is B6c's. | 2 |
| B6b pod producers | crowdsec remote allowlist, AppSec verify and home allowlist (the `crowdsec-jobs` sidecar); `configarr-health` and `janitorr-health` (CronJobs) | Exporter for the sidecar, CronJob for the two (*B6b*) | Flux, through `promote` (`crowdsec`, `configarr`, `janitorr`, `observability`, `netpol-baseline`) | The operator ruled on 2026-10-23 that a pod exports through a metrics endpoint. The sidecar serves `homelab_check_*` on a new `crowdsec-jobs` scrape job. Each CronJob patches its verdict onto a ConfigMap it owns, which kube-state-metrics exports. | 5 |
| B6c registry GC | registry-gc, the weekly mirror GC cron (#6926) | M3 | the tick (`k8s_host_files`, per `setup_routing.py --host daniel-box`) | The script calls `write_check_textfile registry-gc` in `finish`, on every run and before the push, with a `DEADLINE_S` of 612000, the tile's interval. `registry.yaml` carries `RegistryGcFailed` and `RegistryGcNotReporting` in shadow, with `service: registry`. Both are `warning`, because the tile notifies Discord only. `test_registry_gc_watchers_cover_the_worst_run.py` pins the deadline to the tile's interval and to both NotReporting literals. The shadow PR adds `registry-gc` to `KUMA_RETIRING_TAGS` and one `[340h10m]` `CheckWithoutPager` term. Cutover-1a deletes the `Registry GC` tile and its status-page pattern, and moves `registry_gc_push_token` to `PENDING_RETIREMENT`. Cutover-1b drops `shadow` and the term. Cutover-2 drops the push and the `kuma-push-lib.sh` source from `registry-gc.sh.j2`, and keeps the healthchecks.io dead-man. The token PR retires the key a week after Cutover-1b. | 1 |
| B7 pods | autofix-bridge; cloudflare-ddns direct and proxied; pi-peer-backup | Exporter, CronJob, a Prometheus rule plus a LogQL rule | `land.sh` | See *B7*. | 4 |
| B8 probes | the 14 hand-written monitors, plus one `tcp_connect` probe per Pi published port | M4 | `land.sh` | See *B8*. Cuts over the `pi_pressure` row. | 1 |
| B9 integrity scans | the weekly AIDE check and rkhunter scan, one tile each per host (#6168) | M3 | each host's own package, through its `integrity-scans` component (`hosts/<host>/package.yml`): the builder publishes it and the host's `homelab-updater.timer` installs it. `initial_setup_packaged_hosts['fail2ban']` lists all three hosts, so `initial_setup.yml --tags "aide,rkhunter"` writes neither scan anywhere, and no `-e target=daniel-pi` apply reaches the Pi. | Both scripts call `write_check_textfile` as `aide-check` and `rkhunter-check`, with a `DEADLINE_S` equal to the tile's interval (626400s and 612000s). `host-checks.yaml` carries `AideCheckFailed`, `AideCheckNotReporting`, `RkhunterScanFailed` and `RkhunterScanNotReporting` for each host, in shadow, and `test_integrity_scan_rule_literals.py` pins the deadline literals. The shadow PR adds six `CheckWithoutPager` terms: `[348h10m]` for the AIDE tiles and `[340h10m]` for rkhunter. Cutover-1b drops `shadow`, and Cutover-1a deletes the six tiles and their tokens. Cutover-2 drops the push and the `kuma-push-lib.sh` source from `aide-check.sh.j2` and `rkhunter-check.sh.j2`, and reaches each host in a new version of its package. C-3a waits for it on all three hosts. | 6 |

Phase A retires 78 push tokens: 41 from bridge rows and 37 from other producers. Phase C
retires the 79th, `kuma_status_page_sync_push_token`, together with Kuma's three other
secrets. The arithmetic: 83 at the baseline, less M5's 3 and M3's 1, leaves 79. B6c's
`registry_gc_push_token` is among Phase A's 78.

**Order.**

- A0 lands first, before any Cutover-a, Cutover-1a or A6-7a (see *A0* below).
- Each B batch's Cutover-1a waits for its shadow PR's `KUMA_RETIRING_TAGS` entries to be
  deployed (*The cutover order*). That is the gate M5 Open question 4 asks for. No
  contracts.md row gates it: contracts.md's *Round 2 — integration review (X2-series)* entry
  *Applied with no contract text.* says M5's handoff "changes slice counts, not a contract
  row" and that "M6's first Cutover-1a has no gate left from it".
- A1 to A5, B0 to B6 and B8 are independent of each other, except for the inhibit ordering
  below. Every host batch waits for B0's apply on its own host. Independence lets their
  shadow PRs and soaks overlap. Their cutover windows still open one at a time.
- Each inhibit lands in the later of the two PRs that drop `shadow` from its source and from
  its target (*A3 and the handoffs it closes*, task 6). For A1 that is the later of A1's
  Cutover-b and A3's Cutover-b. Until then a dead exporter pages twice, which is the safe
  direction.
- B1's Cutover-1b, which drops `KumaDown`'s `shadow`, and B7's autofix-bridge Cutover-1b,
  which drops `AutofixBridgeDown`'s, wait for A3's Cutover-a. Until that PR deletes the
  `Cluster Scrape Targets` tile, the tile pages the same failed scrape, which M1 convention 5
  forbids. `MonitorBridgeDown` drops `shadow` in A6-7b, which already follows A3's Cutover-a.
- D5 holds monitor-bridge, observability, netpol-baseline and uptime-kuma behind the M6 steps
  named in *When D5 flips a role M6 edits*, by index sub-edges (Open question 1).
- A7's shadow PR waits for M5's ruler to be live. Its Cutover-a waits for M2's PR 4a, which
  creates the row.
- A6-1 exports the gate rows, so it lands before A6-3 and A6-4 start their soaks. Otherwise
  `WANUnreachable` and `B2Unreachable` would soak for 7 days against no series.
- A6-7r and then A6-7a come after the Cutover-a of A1 to A5, of A7 and of B8. A6-7r deletes
  `gates.py`, whose `prometheus` and exporter suppression still serve those rows until then.
  A6-7a deletes the push path and `apply_startup_grace`, which serve every row that pushes.
  The bijection test A6-7a adds also fails while a non-exporter row remains. A6-7r lands
  before A6-7a, so the refactored loop runs live while Kuma still pages.
- B8's cutover comes after A1's, because it deletes the `pi_pressure` row that A1 leaves.
- B4's Cutover-1a carries the renovate pair's Cutover-2. The `renovate-notify` and
  `renovate-agent` units send their Kuma beat with `curl -f` from an `ExecStartPost`, so a beat
  into a deleted tile gets a 404 and fails the unit, and `OnFailure` pages every day until the
  push goes. The textfile `ExecStartPost` runs first, so the verdict survives either way. In one
  PR, the push and the tile go within one landing of each other, whichever plane applies first.
  Each unit runs once a day, so a 404 needs that unit's run to fall inside the gap. A push that
  stops first leaves a live tile that goes DOWN only after its 28h or 36h interval, far longer
  than the gap. A Cutover-2 merged on its own before Cutover-1a pages through Kuma if
  Cutover-1a has not deployed within that interval. A revert of Cutover-1a for the two-hour
  bound reverts the pair's push with it.
- B4's two etcd drill tiles carry no `CheckWithoutPager` term, and B4's other tiles bound their
  window. B4 therefore deletes the drill tiles only together with the five `[2h]` tiles, and
  drops the drill rules' `shadow` no later than those five terms leave `kuma-cutover`.
- B1's cutover waits for `KumaDown` to have soaked.
- B7's pi-peer-backup shadow PR, the first to use the CronJob recipe, waits for C7's
  amendment for Open question 3 to land in contracts.md.
- A3's Cutover-b, which drops `BlackboxExporterDown`'s `shadow`, waits for M4's PR 5, which
  makes the exporter the ingress pager (*A3 and the handoffs it closes*).
- B-1, B-1d and B-1e wait on no Phase A batch. They need M1's `ALERTS`, C7's amendment for
  Open question 4, and littlelink still Ansible-owned. D5's flux PR for littlelink waits for
  B-1e, a sub-edge the slice index must carry (Open question 12).
    - **Until contracts.md applies Open question 12's corrections, the index governs.** Its M6
      row puts all of M6 behind M2 to M5, and its register row for the split puts Phase B
      behind A6 with no B-1 exception. So B-1 to B-1e wait for M2 to M5 and for A6.
      The operator's 2026-10-23 ruling narrows that wait to M2 to M5's functional PRs, and
      contracts.md records it under the slice index.
    - Its D5 row holds only "the PR that flips the last Ansible-owned stateless leaf", not
      littlelink's. If littlelink has flipped by then, B-1d's PR names the stateless leaf D5
      still holds back, and quotes its entry carrying no `owner: flux`.
    - The stall: D5's last stateless-leaf flip, and with it D5's Wave A, waits for most of
      Phase A. *Risks* names the operator's choice that follows.
- C-1a waits for B6c's token PR, which retires `registry_gc_push_token` after B6c's
  cutovers have deleted the `Registry GC` tile and its push. Until B6c's Cutover-1a, C-1a's
  gate of one `monitor_status` series cannot pass (Open question 13). The index's M6c row
  names B6c's token PR for this edge. C-1a's quoted `registry_gc_push_token` grep and its
  gate also enforce the edge inside this slice.

**A0, the declaration floor (~40 lines, deploys nothing).** The uptime-kuma task
"Check the declaration index is populated" asserts `kuma_declarations | length > 50`, and it
runs before the manifests include. At `70591c307`, `static-monitors.yaml.j2` declares 57
literal `<id>.json` keys (a `grep -cE` for indented `*.json: ` keys), plus one per
`kuma_ingress_monitors` tile (26, which M4 removes) and one per `CHECKS` row (41 at this
page's baseline). Phase A and M4 leave five: `discord`, `email`, `tag-severity`,
`tag-runbook` and `status-page-sync`. The first Kuma-side PR that takes the count to 50 or
below fails that assert, and so does every later `uptime-kuma` deploy from master. The play
stops before `monitor-bridge`, but any other session's `monitor-bridge` deploy from master's
tip then stops a push whose tile is still live, and Kuma pages DOWN 1200s later.

- A0 replaces the numeric floor with a named-member assertion: the index holds
  `status-page-sync`, `discord`, `tag-severity` and `tag-runbook`, the entities that must
  survive until C-1. D5's flip of uptime-kuma (#5712) deleted the task, so the assert's home
  is the `INDEX_ASSERT` literal in
  `test_declaration_index_flux_form.py`, which A0 edits.
- Its test evaluates the role's own assert expression, the way
  `test_kuma_push_token_format.py` evaluates the token-format assert.
  An empty index must be refused. An index holding only the five survivors must be
  accepted, and so must the baseline render.
- "Check every push token is in the format AutoKuma accepts" stays until C-1. A shrinking
  index cannot trip it.
- A0 changes `tasks/` and a test only, so no rendered manifest moves and, per root
  `CLAUDE.md` *When to wait*, nothing deploys it. The new assert first runs at the first
  Kuma-side PR's `uptime-kuma` deploy. To show it green on the real render before then, A0's
  PR quotes `./scripts/deploy.sh --dry-run --tags uptime-kuma`.

**Files the batch rows do not name.** A `git grep` for exit criterion 3's identifiers at
`70591c307` finds these. Each goes with the PR that removes its subject:

- B2's secret-rotation shadow PR adds the textfile write to the audit wrapper. Its
  Cutover-2 deletes the `--push` arm of `scripts/secrets_mgmt/secret_rotation.py:cmd_audit`,
  which reads `SECRET_ROTATION_KUMA`, and `scripts/secrets_mgmt/rotation_tools.py:kuma_push`,
  with their cases in `test_secret_rotation.py` and `test_rotation_tools.py`. It also
  rewords the `scripts/secrets_mgmt/secret_bearing_host_paths.py` docstring that quotes the
  wrapper's export line.
- B4's renovate_agent token PR deletes `renovate_agent_kuma_push_token` from
  `scripts/secrets_mgmt/consumers.py`.
- B0 rewords the two `ansible/inventory/group_vars/all.yml` comments that place
  `wan_reachable` and `boot_grace_active` in `kuma-push-lib.sh`, because B0 moves both.
- B-2 rewords the `scripts/diagnostics/probe.py` docstring that names `monitor_status`.
- C-3a rewords the comments that name `kuma-push-lib.sh` in
  `ansible/roles/setup/common/tasks/stamp_deployed.yml` and
  `ansible/roles/setup/initial_setup/files/setup-drift-lib.sh`.
- C-5 deletes `scripts/secrets_mgmt/sops_io.py:malformed_push_tokens`, whose subject is the
  push tokens C-5 removes, and its call in `scripts/secrets_mgmt/secret_rotation.py:cmd_audit`,
  with their cases in `test_secret_sops_io.py` and `test_secret_rotation.py`.
- The monitor-bridge modules go in A6-7r, A6-7a (*The bridge as an exporter*) and C-7.

**Files under `hosts/`.** The host packages took over many producers and the push plumbing
after `70591c307`. The same `git grep` over `hosts ':!*/tests/*' ':!*/test_*'` therefore finds
files that the rows above name only by their Ansible path. Each goes with the PR named here:

- **The push producers.** The setup-plane Cutover-2 of B2 and B4 (`6ad661ed2`) removed the
  push from the `hosts/components/` producers: `docs-refresh.sh.j2`, `eval-run.sh.j2`,
  `loki-read-route-health.sh.j2`, `secret-rotate.sh.j2`, `secret-rotation-audit.sh.j2` and
  `nut-secondary/units/homelab-ups-watchdog.service.j2`. At `be259bc8a` the grep finds none
  of them.
- **B9's integrity scans.** The `integrity-scans` component renders `aide-check.sh.j2` and
  `rkhunter-check.sh.j2` from `ansible/roles/setup/initial_setup/templates/` into all three
  host packages. Both still source `kuma-push-lib.sh`, as
  `{{ initial_setup_host_lib_dir }}/kuma-push-lib.sh`, which the component sets to
  `/usr/lib/homelab`. The identifier grep finds them under `ansible/`, but C-3a's
  precondition read searched for the literal `/usr/local/lib/` path and missed them. B9's
  Cutover-2 (*The cutover order*) drains them, and C-3a waits for it on every host.
- **C-3a deletes the push plumbing:**
    - `hosts/lib/kuma_push.py`, and in `hosts/lib/host_lib.py` the re-export and the
      docstring sentence that names `kuma_push.py`.
    - The `lib: kuma_push.py` rows of `hosts/components/builder/component.yml` and
      `hosts/components/claude-code/component.yml`, with the
      `hosts/components/builder/CLAUDE.md` lines that list the file.
    - `hosts/components/kuma-push/`, which ships `/usr/lib/homelab/kuma-push-lib.sh`. Its
      entry leaves the `components:` list of `hosts/daniel-box/package.yml`,
      `hosts/daniel-server/package.yml` and `hosts/daniel-pi/package.yml`, and the comment
      in `hosts/components/pi-heartbeats/component.yml` that points at it goes too.
    - The renderer rows that read the Ansible copy of `kuma-push-lib.sh`: `SHARED_FILES` in
      `scripts/renderer/renderer_lib/host_sources.py` and `HOST_INPUTS` in
      `scripts/renderer/renderer_lib/host_inputs.py`.
- **C-6 deletes the reconcile `legacy:` entries that name Kuma.** Each entry retires a file
  or unit that an Ansible apply installed. C-6 deletes an entry once a read-only check finds
  that file or unit gone on its host:
    - The three `kuma-check-secret-rotation-audit` units in
      `hosts/components/secret-rotation/component.yml`, with the header comment in each
      file that names the old timer. The read is
      `systemctl list-unit-files 'kuma-check-secret-rotation-audit*'` on daniel-box. The three
      `kuma-check-loki-read-route` units and `/var/lib/kuma-check/loki-read-route.down` stay in
      `hosts/components/loki-route-witness/component.yml`: daniel-server still runs them, and
      reconcile retires them through these entries when its package takes the component.
    - `/opt/homelab-builder/kuma_push.py` in `hosts/components/builder/component.yml`. The
      read is C-3a's `ls /opt/*/kuma_push.py` on daniel-box.
    - `/usr/local/lib/pi-kuma-push-lib.sh` in `hosts/daniel-pi/package.yml`. The read is a
      read-only `ls` of that path over `ssh daniel-pi`.
    - The Ansible-era push env files: `/etc/homelab/loki-route-kuma-push.env` in
      `hosts/components/loki-route-witness/component.yml`,
      `/etc/homelab/rkhunter-kuma-push.env` and `/etc/homelab/aide-kuma-push.env` in
      `hosts/components/integrity-scans/component.yml`, and `/etc/nut/kuma-push.env` in
      `hosts/components/nut-secondary/component.yml`. The read is a read-only `ls <path>` on
      each host whose package ships the component, over `ssh` for daniel-server and
      daniel-pi. `rootq stat` cannot take these paths, because none is on its allowlist.
      Only `No such file or directory` passes: a `Permission denied` says nothing about the
      file.
- **The triage poller and rootq.** The poller,
  `hosts/components/claude-code/files/claude_triage_poll.py`, reads `monitor_status`. B-10,
  which the fix for #6862 adds to *Phase B*, drops that read. `hosts/components/claude-code/files/homelab_rootq.py` matches `/api/push/` in
  a redaction rule, not a Kuma read, so it belongs on the allowlist (#6862).
- **`hosts/components/claude-code/CLAUDE.md`**, in *The artifacts node's two timers*, says
  "Neither timer pushes to Kuma", and its ENFORCED line cites
  `test_neither_producer_pushes_to_kuma` in `hosts/lib/tests/test_claude_artifacts_node_timers.py`,
  which holds both scripts to "no Kuma library". Rewording only the quoted markers would
  leave `kuma` in the test name. C-6 deletes the test, the `KUMA_PUSH_MARKERS` tuple, the
  `kuma_push_markers` helper and the red case `test_a_script_that_pushes_to_kuma_is_flagged`. Their subject goes with C-3a,
  which deletes the library and `kuma_push.py`, so no shipped script can push any more
  (`.claude/rules/python-layout.md`, *A check retires when its subject does*). C-6 rewrites
  the bullet's lead to say that each run writes a check file and neither unit loads a
  credential. The rewritten bullet names Kuma nowhere and drops the ENFORCED line. C-6 retires the test of
  the same name in `ansible/tests/services/test_gitops_check_rule_literals.py` the same way,
  with the sentence in `ansible/roles/setup/gitops_deploy/CLAUDE.md` that cites it.

**Every `hosts/` file the grep finds, by PR.** `git grep -il kuma -- hosts ':!*/tests/*'
':!*/test_*'` lists 45 files at `d67fb3b9b`. The identifier grep that C-6 quotes finds about a
dozen of them. The rest mention Kuma only in a comment, a unit's `Description=`, a label or a
path, and the census row fails on each one all the same (#6891). Each file below goes with the
PR named for it, or onto the census allowlist with its reason. A file that two PRs touch is
listed under both. C-6 runs the same grep before it lands and rewords any hit this list does
not name.

- **C-3a**, with the push plumbing above:
    - `hosts/lib/kuma_push.py`, deleted.
    - `hosts/lib/host_lib.py`: the module docstring's "Uptime Kuma push" and "kuma_push.py"
      mentions, the comment above the re-export, and the re-export.
    - `hosts/components/kuma-push/component.yml`, deleted with its directory.
    - `hosts/components/builder/component.yml` and `hosts/components/claude-code/component.yml`:
      the `lib: kuma_push.py` rows.
    - `hosts/components/builder/CLAUDE.md`: the file list that names `kuma_push.py`, and the
      3.12-grammar sentence. That sentence cites
      `test_host_lib_and_kuma_push_parse_with_the_3_12_grammar`, which C-3a renames once
      `kuma_push.py` is gone.
    - `hosts/daniel-box/package.yml`, `hosts/daniel-server/package.yml` and
      `hosts/daniel-pi/package.yml`: the `kuma-push` entry in `components:`.
    - `hosts/components/pi-heartbeats/component.yml`: the comment that points at the
      `kuma-push` component.
- **B-10**, with the poller's Kuma source:
    - `hosts/components/claude-code/files/claude_triage_poll.py`.
    - `hosts/components/claude-code/templates/claude-triage-poll.service.j2`: its
      `Description=` names Kuma as an input.
    - `hosts/components/claude-code/defaults/main.yml`: the severity-floor comment says Kuma
      reads a tile's `tag-severity`, which stops being true when the poller drops Kuma.
- **B9's Cutover-2**, with the scans' push:
    - `hosts/components/integrity-scans/units/homelab-aide-check.service.j2` and
      `hosts/components/integrity-scans/units/homelab-rkhunter-check.service.j2`: each
      `Description=` says the scan pushes to Kuma.
    - `hosts/components/integrity-scans/component.yml`: the contract's scope and evidence
      lines that name the Kuma push.
- **Each batch's token PR**, which removes the key from a `credentials:` list and from the
  host's SOPS file in one step. The comment above each list says why the key stays until
  then, and goes with it:
    - B2's: `hosts/components/docs-refresh/component.yml`,
      `hosts/components/eval-sweep/component.yml`,
      `hosts/components/loki-route-witness/component.yml` and
      `hosts/components/secret-rotation/component.yml`.
    - B4's: `hosts/daniel-server/package.yml`, both its header line that calls the UPS
      watchdog's token the one credential and the comment above `credentials:`.
    - B5's: `hosts/daniel-pi/package.yml`.
- **C-3b**, with the task-file rename:
    - The comments that cite `roles/setup/common/tasks/kuma_check_timer.yml` or call the
      contract "the kuma-check form M6 left":
      `hosts/components/loki-route-witness/templates/loki-read-route-health.sh.j2`,
      `hosts/components/secret-rotation/templates/secret-rotation-audit.sh.j2`,
      `hosts/components/loki-route-witness/units/homelab-loki-route-witness.service.j2`,
      `hosts/components/secret-rotation/units/homelab-secret-rotation-audit.service.j2`,
      `hosts/components/loki-route-witness/component.yml` and
      `hosts/components/secret-rotation/component.yml`. They cite `check_timer.yml` after it.
    - The down marker stays `/var/lib/kuma-check/<name>.down`, because the units keep their
      names under ADR 0026. The comment edits still add the daniel-box and daniel-server
      packages to C-3b's deploys, because the comments render into both packages.
- **C-6**, which rewords each comment that names Kuma only as history:
    - The producers whose push a Cutover-2 removed:
      `hosts/components/docs-refresh/templates/docs-refresh.sh.j2`,
      `hosts/components/eval-sweep/templates/eval-run.sh.j2`,
      `hosts/components/loki-route-witness/templates/loki-read-route-health.sh.j2`,
      `hosts/components/secret-rotation/templates/secret-rotate.sh.j2`,
      `hosts/components/secret-rotation/templates/secret-rotation-audit.sh.j2`,
      `hosts/components/claude-code/templates/sync-artifacts.sh.j2` and
      `hosts/components/claude-code/templates/claude-otel-health.sh.j2`.
    - The component and package headers that tell the same history:
      `hosts/components/loki-route-witness/component.yml`,
      `hosts/components/secret-rotation/component.yml`,
      `hosts/components/nut-secondary/component.yml`,
      `hosts/components/pi-heartbeats/component.yml` and `hosts/daniel-box/package.yml`.
    - The comments that describe Kuma as a reader that C-1 deletes: the maintenance window
      in `hosts/components/host-maintenance/component.yml`,
      `hosts/components/host-maintenance/units/homelab-reboot.timer.j2`,
      `hosts/components/claude-code/templates/claude-triage-config.env.j2` and
      `hosts/components/secret-rotation/units/homelab-secret-rotation-audit.timer`. The timer's
      `ENFORCED:` line cites
      `ansible/roles/k8s/uptime-kuma/tests/test_maintenance_window.py`, which C-1 deletes, so
      the citation points at nothing from C-1 until C-6. No check resolves it: the facts lock
      reads Markdown sections, not unit files.
      C-6 keeps the timer's schedule and rewrites only the reason the comment gives for it.
    - `hosts/components/pi-peer-backup-target/pi-peer-backup-shell.sh`: "a Kuma DOWN"
      becomes the alert that pages for the nightly peer backup.
    - `hosts/components/claude-code/CLAUDE.md`, as above.
    - The `legacy:` entries above, in `hosts/components/builder/component.yml`,
      `hosts/components/loki-route-witness/component.yml`,
      `hosts/components/secret-rotation/component.yml`,
      `hosts/components/integrity-scans/component.yml`,
      `hosts/components/nut-secondary/component.yml` and `hosts/daniel-pi/package.yml`.
- **The census allowlist**, each with its reason:
    - `hosts/components/claude-code/files/homelab_rootq.py`: an output redaction rule, as above.
    - `hosts/components/pi-compose/services/alloy/compose.yaml.j2`,
      `hosts/components/pi-compose/services/autoheal/compose.yaml.j2`,
      `hosts/components/pi-compose/services/docker-proxy/compose.yaml.j2` and
      `hosts/components/pi-compose/services/wg-easy/compose.yaml.j2`: the inert
      `org.homelab.kuma-docker-monitor` label, the comments beside it, and the header and
      healthcheck comments that name AutoKuma or Uptime Kuma. A label change recreates the
      container, so these files leave the allowlist in the next Pi compose change that
      recreates their containers anyway (*Open questions*, item 7).

**Which package version carries a `hosts/` change.** The builder versions each host package
`0.<first-parent count>+g<sha12>` (`hosts/lib/host_package_builder.py:version`). The version
that carries a change is therefore known only after its merge. `land.sh` already waits for
each host whose package the PR changes. A later PR that depends on an earlier `hosts/`
change, such as C-3a on B9's Cutover-2, quotes the earlier change's arrival on each host:
`dpkg-query -W -f '${Version}' homelab-<host>` prints the installed version, and
`git merge-base --is-ancestor <merge commit> <sha12>` exits 0 for that version's SHA.

**Cross-area: H2 moves about 15 host crons into CronJobs.** If H2 has moved a cron before its
batch runs here, that check takes the CronJob recipe instead, and B7's kube-state-metrics change
lands first. If this batch runs first, H2's PR changes that check's rule input from the textfile
to kube-state-metrics. Either way the alert names and labels stay.

### A3 and the handoffs it closes

A3 moves `targets` and `cluster_targets`. Between them they read every `up` series in the
cluster Prometheus: `checks/cluster.py:check_cluster_targets` selects
`up{origin!="daniel-server"}`, its docstring calls the two selectors complementary, and
neither check exempts a job. Kuma's tiles for these rows page a dead scrape today. M1
convention 5 therefore forbids an unshadowed `up == 0` rule for any of those jobs until A3,
and three slices hand A3 the switch. A3 is the bridge-row form (*The cutover order*): a shadow
PR, Cutover-a, Cutover-b and the token PR. Its six extra tasks:

1. **The scrape-target rule exempts no job** (M1). A3's rule, `ScrapeTargetDown`, fires on
   `up == 0` for every series the two checks read, `alertmanager`, `blackbox-ingress` and
   `loki-homelab` included. Its `service` label names the role that deploys the target
   (Open question 2), and `job="alertmanager"` maps to `observability`, the role that deploys Alertmanager in M1.
   M1's `AlertmanagerMetricsMissing` then keeps the narrow scope M1 gives it, because A3's rule
   pages the failed scrape that M1 leaves to the tile. The rule is `warning`, like the tile's
   row. The shadow PR's promtool cases fire it for each of the three jobs, and for
   `alertmanager` they assert `service: observability`. A rule whose selector drops any of the
   three fails its case.
2. **`BlackboxExporterDown` drops `shadow`** (M4). Cutover-b drops it in the same commit as
   A3's own rules. Cutover-a has already deleted the tile that paged the same failure, so the
   split keeps one pager. This restores the 5-minute `critical` page for a dead exporter that
   M4's *Risks* defers to A3.
3. **The routing outcome flips** (M4 Open question 8). The same Cutover-b changes the
   `BlackboxExporterDown` set's expected outcome in M2's routing pytest from `blackhole` to
   `muted`, M4's wording. Without that edit the pytest fails on the dropped `shadow`.
4. **An inhibit keeps a dead exporter to one page** (M4 Open question 8, its preferred
   form). In Cutover-b, an Alertmanager inhibit rule takes `BlackboxExporterDown` as its
   source and A3's alert with `job="blackbox-ingress"` as its target. One dead target still
   pages through A3's rule, because `BlackboxExporterDown` fires only when every target reads
   0. Its test reads the rendered Alertmanager config. It fails when the inhibit's target
   matchers do not select `job="blackbox-ingress"`, or when its `equal` list names a label
   that `BlackboxExporterDown` does not carry, since such an inhibit never matches.
5. **`LokiHomelabDown` is deleted** (M5 Open question 5). Cutover-b deletes it, with its
   promtool and routing cases, because A3's rule pages the same `up{job="loki-homelab"}`
   series with `service: loki-homelab`. Keeping both would page one fault twice. M5 lets A3
   delete it when A3's rule covers the job. A dead loki-homelab then pages after A3's 10
   minutes rather than `LokiHomelabDown`'s 5. Kuma's tile took about 15. M5's table gives
   `LokiHomelabDown` `warning` and names no `verify` label. A3's rule has the same shape, so
   `probe.py health loki-homelab` gates on neither of them (Phase B item 1).

6. **The jobs with their own down rule get an inhibit, not a second page.** `KumaDown`,
   `MonitorBridgeDown` and B7's `AutofixBridgeDown` each read `up == 0` for one job, as
   `LokiHomelabDown` did. Each inhibits `ScrapeTargetDown` for its own job, through target
   matchers `alertname="ScrapeTargetDown"` and that literal `job`, with no `equal` list.
   These rules stay, unlike `LokiHomelabDown`, because each is `critical` with an `absent()`
   arm, which `ScrapeTargetDown` lacks. A1's inhibit takes `ScrapeTargetDown` as its source
   instead (the A1 row).
    - **`FluxControllerDown` is the fourth source.** Its `up == 0` arm reads one target per
      Flux controller, so its inhibit carries `equal: [job, instance]` and holds only the
      dead controller's `ScrapeTargetDown`. The rule's `absent()` arm carries no `instance`
      and holds nothing. It lands with the PR that drops that arm's `shadow`, after
      Cutover-b ([#6654](https://github.com/DanielH2018/server/issues/6654)).
    - **When each inhibit lands.** Alertmanager inhibits before it routes, so a shadow
      source would silence a live target, and nothing would page. Each inhibit therefore
      lands in the later of the PRs that drop `shadow` from its source and from its target:
      A3's Cutover-b, or B1's Cutover-1b for `KumaDown`, A6-7b for `MonitorBridgeDown` and
      B7's autofix-bridge Cutover-1b for `AutofixBridgeDown`. Until then the fault pages
      twice, which is the safe direction. C-1 deletes `KumaDown`'s inhibit with `KumaDown`.
    - **The test.** A config test beside the routing test reads the rendered Alertmanager
      config and the rule files. It fails when an inhibit's source or target matchers select
      no defined rule, or when its target matchers miss the job or label it names. It also fails when
      the source rule carries `shadow` and the target rule does not. For an inhibit with an
      `equal` list, it fails when a listed label is absent from the source's or a target's
      expected labels in the promtool cases.

`LokiHomelabDown` is a Prometheus rule in `observability`, so A3's Cutover-b still deploys
`observability` alone.

### `KumaDown` replaces `uptime-kuma-alive` (B1)

Kuma still pages for every unmigrated batch until C-1, so it keeps a watcher until then.

- **The rule.** `KumaDown`: `up{job="uptime-kuma"} == 0 or absent(up{job="uptime-kuma"})`,
  `for: 5m`, `critical`, `service: uptime-kuma`. It reads Prometheus's direct scrape of Kuma's
  Service, so Authelia is not in its path. It lands with B1's shadow PR, with promtool cases
  for both arms. B1's Cutover-1b drops its `shadow` after A3's Cutover-a (*Order*), and adds
  its inhibit on `ScrapeTargetDown{job="uptime-kuma"}` if A3's Cutover-b has landed.
- **What it proves.** A 200 from `/metrics` with the API key. That is weaker than
  `uptime-kuma-alive`, which proved Kuma accepted a push (see *Risks*).
- **`uptime-kuma-alive` retires in B1's Cutover-2**, in this order:
    1. **Operator-only, before B1's Cutover-1b merges:** pause `uptime-kuma-alive` in the
       Healthchecks.io console. Kuma's death then never pages from both Healthchecks.io and
       a live `KumaDown`. Between the pause and Cutover-1b's deploy nothing watches Kuma,
       which is the safe direction, under the same two-hour bound.
    2. Merge Cutover-2. It removes the ping from `longhorn-backup-health.sh.j2`, the entry
       from `monitor_bridge_healthchecks_expected`, the deadman row from
       `scripts/docs/fragment_renderers.py`, and the slug from the five tests the baseline
       lists, and regenerates the deadman fragments. `monitor-bridge` applies by its owner,
       in either order against the hand apply below (*The cutover order*). The
       operator applies `k3s-bringup.yml --tags "backup-health,etcd-snapshot,manifest-prune,release-staleness,live-drift"`
       by hand. `longhorn-backup-health.sh.j2`'s tasks carry `backup-health`. The
       push-credentials task carries four of B1's tags (every one but `live-drift`), plus
       `disk-health`.
    3. **Operator-only:** delete the check in the console within one `healthchecks_drift`
       cycle, so the drift row does not report the mismatch.
- **`KumaDown` is deleted in C-1**, in the deploy that runs before Kuma is removed.

### The bridge as an exporter (A6)

- **Serving.** `files/cli.py` keeps its 300s cycle. It sets three gauges per row:
  `homelab_check_success`, `homelab_check_last_run_timestamp_seconds` and
  `homelab_check_deadline_seconds`, the last one 900s for the 300s cycle. It also sets one
  gauge for the whole table, `homelab_check_rows`, so no rule carries the count as a literal.
  That gauge counts the enabled rows of `check_table.CHECKS`, gate rows included. The registry's
  count would be wrong: `registry.build_checks` drops every `is_gate` row, so the gate rows'
  series would exceed it. Production sets no `CHECKS_SKIP` or `CHECKS_ONLY`
  (`templates/env-secret.yaml.j2`), so in the pod the gauge is the table's length. It serves
  them from a stdlib `http.server` thread on a `port` that the entry declares. A `metrics` item
  on the entry renders the scrape job through `ansible/filter_plugins/scrape_jobs.py`, and the
  `homelab` baseline already admits Prometheus. During the shadow window it also keeps pushing
  (M1 convention 5).
- **What a row exports, from A6-1 on.** The gauge is written at each point that pushes today,
  so the gate rows export too. Those points are `gates.py:_gate` for a gate row and the loop
  in `check.py:run_once` for the rest. The exported value is the end state's verdict, and
  until A6-7a the push keeps today's value beside it:
    - **A gate row** (`wan_reachable`, `b2_reachable`, and `prometheus` until A6-7a deletes
      it) exports the verdict `_gate` pushes. `WANUnreachable` and `B2Unreachable` therefore
      soak against real series in A6-3 and A6-4.
    - **A `startup_grace` row** exports the verdict that `gates.py:_evaluate` returns, before
      `bridge.streaks.apply_startup_grace` runs. Its rule's `for: 10m` carries the grace (*Gates
      and grace*). The push keeps the held `up` until A6-7a.
    - **A row whose gate reads down this cycle** exports 0 with a fresh timestamp. Its body is
      not called, which is today's behaviour. The push keeps sending `up` with the `skipped`
      message until A6-7a. The rule fires, and the gate rule's inhibit holds it.
    - **A body that raises** exports 0. `_evaluate` keeps converting the exception into a
      `down` verdict, so the loop goes on to the next row (*The run loop after `gates.py`*).
- **Rules.** `files/rules/bridge-exporter.yaml` holds one plain rule per remaining row, each with
  its own `severity` from the row's `critical` flag and its own `service`. Two rules watch the
  exporter itself, both `critical` with `service: monitor-bridge`:
    - `MonitorBridgeDown`: `up{job="monitor-bridge"} == 0 or absent(up{job="monitor-bridge"})`,
      `for: 10m`.
    - `BridgeCheckStale`: any row whose
      `time() - homelab_check_last_run_timestamp_seconds{job="monitor-bridge"}` exceeds its
      `homelab_check_deadline_seconds`, or
      `count(homelab_check_success{job="monitor-bridge"})` below
      `homelab_check_rows{job="monitor-bridge"}`, `for: 10m`. It is one aggregate alert, so a
      hung loop pages once rather than once per row. It is the class-3 guard: a loop that hangs
      behind a live HTTP thread keeps `up` at 1 and serves stale gauges.
    - **Why `for: 10m`.** The pod has no readinessProbe (its glance block says probe-less), so
      a restart reads `up == 0` for a scrape, and the first 300s cycle after it has not yet
      set every row's gauge. Without a `for:`, either rule fires on that one evaluation, on
      every `monitor-bridge` deploy, and Phase B's health gate fails it. `for: 10m` is two
      `INTERVAL`s: the first cycle (up to one `INTERVAL`) plus the 1m scrape interval, with
      margin. A dead loop still pages within 10m of its deadline.
- **The bijection test**, in `ansible/roles/k8s/monitor-bridge/tests/`. It asserts a
  one-to-one match between the rows of `CHECKS` and the rules that select `check="<row>"`, with
  `MonitorBridgeDown` and `BridgeCheckStale` exempt by name. It lands with A6's cutover, when
  only exporter rows remain in `CHECKS`. The row count needs no test of its own there: A6-1's
  exporter test asserts that `homelab_check_rows` equals the count of enabled `CHECKS` rows,
  gate rows included, and a row deletion moves the gauge with it, so no later PR edits a rule
  for it.
- **Gates and grace:**
    - `WANUnreachable` inhibits alerts labelled `requires="wan"`, and `B2Unreachable` inhibits
      `requires="b2"`, through `inhibit_rules` in `alertmanager-secret.yaml.j2`. The gate row and
      its skipped dependents go to 0 in the same cycle and the same scrape. Neither the gate
      rules nor the dependents' rules carry a `for:`, and they share one rule group in
      `bridge-exporter.yaml`. So Prometheus sends source and target in one batch, and M1's root
      route waits `group_wait: 30s` (*The routing tree*), which lets the inhibitor see the source
      before the target's group first notifies.
    - **Paging suppression moves to Alertmanager. The skip before the body stays in the
      bridge.** A dependent row's body is not called while its gate reads down. The reason is
      the B2 transaction cost. `checks/b2.py:b2_storage_usage` caches only successes, and its
      docstring says "b2_reachable already owns the cap signal, so there is no spend spiral to
      protect against here". A cap answer is billed (`checks/b2.py:check_b2_reachable`), so
      calling the body during a cap breach would bill one listing every 300s cycle. The skip
      also keeps the four WAN bodies from waiting on their timeouts during a WAN outage.
    - **`startup_grace` becomes `for: 10m`**, two bridge cycles (the operator ruled so on
      2026-10-31; the plan's `for: 5m`, (`GRACE_CYCLES` − 1) × 300s, was one cycle). The bridge holds a down row `up` for its first down cycle and pushes `down` on the
      second, so `for: 10m` needs the same two down cycles. A gauge that reads 0 for one
      300s cycle spans five to seven 1-minute samples, so `for: 5m` fired on a one-cycle
      blip or not by scrape alignment (*Status by batch*, A6). The exported value is taken before
      the grace from A6-1 on, so the grace never applies twice. A6-7a deletes `apply_startup_grace` and the `grace_streaks`
      field of `State`.
    - **Each in-body streak stays, and its rule has no `for:`.** A rule's `for:` holds the whole
      row. These streaks hold one arm, and the row's other arms page at once:
        - `checks/service.py:check_arr_queue` streaks only the fetch failure
          (`ARR_FETCH_CONSECUTIVE`). Its comment reads "The FETCH rides a streak; the queue
          verdict below does not".
        - `checks/service.py:check_ha_heartbeat` streaks the heartbeat arm (`HA_CONSECUTIVE`)
          and runs `with_ha_ban` outside the streak.
        - `checks/host_edge.py:check_speedtest` streaks the fetch failure on cycles
          (`SPEEDTEST_CONSECUTIVE`) and counts its floor in results
          (`SPEEDTEST_FLOOR_CONSECUTIVE`).
        - `checks/notify.py:check_discord` streaks its whole down branch
          (`DISCORD_CONSECUTIVE`). The streak still stays: the body would change for no
          change in timing, and one rule shape covers every in-body streak.
        - `checks/service.py:check_n8n` keeps `verdicts/service.py:n8n_update_streaks`. It
          counts failed executions per workflow, which is not cycle hysteresis.
    - `n8n` and `speedtest` are under `startup_grace` as well. Today the grace stacks on their
      in-body streaks, so `for: 10m` on their rules needs the same two down cycles and adds no delay.
    - **`bridge/streaks.py` therefore stays.** It keeps `State`, `down_streak` and the probe
      caches `ProbeCache`, `StampedCache` and `B2ProbeCache`. `State` carries the caches that
      space out the external calls: `b2_probe` holds a billed B2 answer for
      `B2_PROBE_INTERVAL_S` (1800s), six cycles, and `r2_probe`, `email_probe`,
      `cloudflare_ips_probe` and `healthchecks_probe` hold theirs for their own intervals.
- **The residual bridge and its cap.** After C-7 the bridge is an exporter beside
  Alertmanager. It keeps 15 check bodies, `State`, `down_streak`, the probe caches and the
  gate skip. D2's PR D2.6d adds the deploy-pipeline family to the same exporter (C7), so the
  residual counts it.
    - An estimate from `wc -l` at the plan worktree's HEAD on 2026-10-12: the loop (`cli.py`, `check.py`,
      `check_table.py`, `registry.py`), the three `gitops_*` modules, the `checks/` modules
      for the B2, R2, WAN, notify, service, edge, Cloudflare, Healthchecks.io and gitops
      bodies, `verdicts/service.py` and all of `bridge/` sum to 7,456 lines. The push path
      and the deleted rows' settings in `bridge/config_host.py` and `bridge/config_cluster.py`
      leave, so the residual is about 6,500 lines before D2.6d.
    - **The planning cap is 7,000 production lines** under `files/`, with D2.6d's family
      inside it. A6-8 measures the residual and sets the cap to that figure plus D2.6d's
      production lines, rounded up to the next 500, in either direction.
    - A6-8 adds a test to the monitor-bridge tests directory. It sums the lines of every
      `.py` under `files/` and fails above the cap. A body that wants to join replaces one or
      raises the cap in a reviewed PR.
- **A6's PRs**, in order, each with its size:
    1. **A6-1, the exporter beside the push (~200 lines).** The serving thread, the gauges, the
       `port` and `metrics` items, two tests, and no rules. The gauges are written in
       `gates.py:_gate` and in `check.py:run_once`'s loop, with the values *What a row exports*
       lists. It deploys `monitor-bridge` and `observability`, plus what the narrowing names
       for the entry edit (*Steps*). The tests:
        - The exporter test asserts that `homelab_check_rows` equals the count of enabled
          `CHECKS` rows, gate rows included. It is red on an exporter that sets the gauge from
          `registry.build_checks`.
        - The run-loop export test, beside `test_check.py`, runs one `run_once` cycle with a
          `FakeSources`, a stated `Gates` and both gate rows present. `wan_reachable` and
          `b2_reachable` must each export `homelab_check_success`. With the WAN probe down, a
          WAN row must export 0 while it pushes `up` with the skip message, and its body must
          not be called. A `startup_grace` row's first down cycle must export 0 while it pushes
          the held `up`. A body that raises must export 0, and the next row must still run.
    2. **A6-2, the exporter's own rules in shadow (~80 lines).** `MonitorBridgeDown` and
       `BridgeCheckStale` with promtool firing cases, including a stale-timestamp case and a
       missing-row case that hold for 10m. Two cases must not fire: one missed scrape of `up`,
       and a restart whose first cycle has set only some rows' gauges for under 10m.
    3. **A6-3 to A6-6, the row rules in shadow, one PR per gate family (~100 to 150 lines
       each).** WAN (4 rows, `WANUnreachable` and its inhibit), B2 (1 row, `B2Unreachable` and
       its inhibit), `startup_grace` (6 rows, `swallowed_verdicts` among them) and ungated
       (5 rows). `swallowed_verdicts`'s rule is `severity: warning`, because the row is not
       `critical`, with `service: monitor-bridge`, the component that evaluates it until C-7.
       It carries no `verify`, so it gates no tag. Each rule has a promtool
       firing case and a zero-series case. The 7-day soak per family follows M1 convention 6.
       The `startup_grace` family's rules carry `for: 10m`, and no other A6 rule has a `for:`
       (*Gates and grace*). The WAN and B2 PRs add a test that derives each `requires` label
       from the row's `gate` column, so a gated row whose rule lacks the label fails. The
       same test checks that each such rule shares a rule group with its gate's rule. It also
       derives each rule's `for:` from the same column: `for: 10m` for a `startup_grace` row,
       and no `for:` for every other A6 row. A row that loses its grace, or gains a `for:` on
       top of an in-body streak, fails it. The `startup_grace` PR extends it to its family.
    4. **A6-7r, the run loop without `gates.py`, push still on (~200 added, ~1,100
       removed).** It lands after the same cutovers as A6-7a (*Order*) and before it.
       `land.sh` deploys `monitor-bridge` alone, and no tile changes. Both the push and the
       export behave as before, so this refactor is proven live while Kuma still pages.
       *The run loop after `gates.py`*, below, lists each reader it changes.
    5. **A6-7a, the Kuma side goes (~60 added, ~450 removed).** It deletes:
        - the push path: `bridge/net.py:push`, `bridge.sources.Sink`,
          `bridge.types.push_env`, and `run_once`'s push branch with its `dry_run`
          parameter, which `cli.py`'s `--dry-run` flag and `sink` parameter feed;
        - the `KUMA_PUSH_*` settings, `KUMA_URL` and `GRACE_CYCLES` in
          `templates/env-secret.yaml.j2`;
        - `bridge/streaks.py:apply_startup_grace` and `State.grace_streaks`, with any other
          `State` field that no remaining body reads. `State`, `down_streak` and the
          probe caches stay;
        - the `prometheus` row and its body, `checks/cluster.py:check_prometheus`.

       Every reader of those changes in the same commit, or the pod dies on its next roll:
        - `registry.py` imports `push_env` and builds each `Check` with a token read
          through it. It builds `Check` without one, and `bridge.types.Check` loses the
          token field. The tests that build the registry, such as `test_check_cli.py`,
          change with it.
        - `bridge/config.py` reads `KUMA_URL` through `_env` with no default, which makes
          the key required. Absent, `load_config` records a problem and `main()` exits 2.
          The field goes, and so does `GRACE_CYCLES`.
        - `check.py:run_once` and `cli.py:main` drop the `sink` and `dry_run` plumbing.
          The run-loop export test drops its push assertions and keeps the rest.
        - The `KUMA_URL` docstring in `bridge/sources.py`, the `KUMA_PUSH_*` comment in
          `cli.py` and the `KUMA_PUSH_<NAME>` line of `check_table.py`'s docstring are
          reworded.
        - The modules that read `bridge.streaks` do not change: `bridge/sources.py`
          (`State`), `checks/notify.py` (`StampedCache`, `down_streak`),
          `checks/service.py` and `checks/host_edge.py` (`down_streak`), and
          `checks/cloudflare_ips.py` and `checks/healthchecks.py` (`ProbeCache`).

       In the same commit A6-7a also removes the `CHECKS` loop from
       `static-monitors.yaml.j2` and the bridge pins from
       `status-page-sync-configmap.yaml.j2`, so no bridge tile outlives its push. It rewrites
       `fragments_bridge.py`'s `bridge-gate-sets` to read the `gate` column alone, deletes
       `kuma_table_loop.py`'s `CHECKS`-loop half, and adds the bijection test. Every A6 rule
       stays shadow. `land.sh` deploys `uptime-kuma` and `monitor-bridge`, in that order.
       It merges only in a quiet window (*No-pager windows*).
    5. **A6-7b, the rules page (~40 lines).** It drops `shadow` from every A6 rule and the two
       exporter rules, and updates their promtool cases. `land.sh` deploys `observability`
       alone, under the two-hour bound in *The cutover order*.
    6. **A6-8, the row fields and the size cap (~180 lines).** It adds the cap test
       (*The residual bridge and its cap*). It deletes `token`, `kuma_id`, `display` and
       `status_group`, and moves each `description` into its rule's `description` annotation.
       It updates `bridge/types.py`, `fragments_bridge.py`'s `bridge-checks` table and their
       tests, and deletes the `check_table.py` `DECIDED` marker on spelled-out tokens with
       what it governs. `LOKI_URL` and the Loki helpers in `bridge/net.py` stay: at the
       baseline, `checks/logs.py` reads `cfg.LOKI_URL` outside `with_log_errors` too, for
       `swallowed_verdicts`. C-7 deletes them with that row.
    7. **A6-9, the tokens (**operator-only** half first).**

### The run loop after `gates.py` (A6-7r)

At `70591c307` the rows that survive Phase A still run through `gates.py`.
`check.py:run_once` imports it as `gate_lib` and imports `Gates` from it. It calls
`gate_lib._gate` for each gate, `gate_lib._evaluate` for each row, `gate_lib.check_enabled`
and `gate_lib.down_exporters`. `registry.build_checks` skips every `is_gate` row, so
`wan_reachable` and `b2_reachable` run only through `_gate`. A6-7r removes that dependency
and changes nothing a pager sees:

- **`check.py:run_once`** runs the gate rows first as ordinary rows. It reads each row's gate
  from a new `Check.gate` field, not from the `Gates` sets. It keeps the skip before the body,
  the `startup_grace` hold on the push, and the gauge values *What a row exports* lists.
  A6-7r writes a `DECIDED the gate skip stays in the bridge` marker at the skip. Its reason
  names the billed B2 listing and the WAN timeouts (*Gates and grace*), so a later review
  does not move the skip into Alertmanager with the paging suppression.
  `_evaluate` moves into `check.py` unchanged, so a raising body still becomes a `down`
  verdict and the loop goes on. `check_enabled` moves into `check.py` too.
- **`registry.py:build_checks`** keeps the `is_gate` rows, orders them first, and passes the
  row's `gate` into `Check`. `bridge/types.py:Check` gains the field. The module docstring
  that calls the gate rows "`gates.py`'s" is reworded, and so is the `check_table.py` module
  docstring line that says `registry.build_checks` runs every row without `is_gate`.
- **`cli.py:main`** drops `gates.expand_gates_for_cli`, the gate rule of
  `gates.validate_check_filter`, and its `gate_config` parameter. A gate the filter disables
  reads as up, which is how `_gate` treats it today, so `--check discord` alone needs no
  expansion. The unknown-name check moves into `cli.py`.
- **Deleted with `gates.py`:** the `Gates` value; `down_exporters`, `EXPORTER_DEPENDENT` and
  the `DECIDED` marker on exporter suppression in `check.py`, whose members A1 and B8 have
  removed; and `PROM_DEPENDENT`, empty after A1 to A5. The `prometheus` row runs as an
  ordinary row and pushes until A6-7a deletes it. `docs/reference/decisions.md` regenerates
  without the marker.
- **The other readers:**
    - `defaults/main.yml:monitor_bridge_modules` loses `gates.py`. A module left in that list
      after its file is gone fails `test_monitor_bridge_modules.py`.
    - `fragments_bridge.py`'s `bridge-gate-sets` reads the `gate` column instead of parsing
      `gates.py`'s `_members` assignments, and the fragment regenerates.
    - The `bridge/streaks.py` module docstring stops naming `STARTUP_GRACE` in `gates.py`.
- **Tests.**
    - `test_check_gates.py`, `test_check_gate_dependents.py`, `test_check_gates_exporters.py`
      and `tests/_check_gate_helpers.py` are deleted. Each case that still has a subject
      folds into `test_check.py`.
    - The `run_once` cases in `test_check_wan_gate.py` and `test_check_b2_gate.py` are
      rewritten against `Check.gate`. Their probe-body cases do not change.
    - `test_check_cli.py`'s filter cases, `test_check_streaks.py`'s grace cases and A6-1's
      run-loop export test move to the new seam. The export test keeps every assertion it
      had.
- **Evidence in the PR.** After the `monitor-bridge` deploy, the PR quotes two reads.
  `monitor_status` must read 1 for every bridge tile. `count(homelab_check_success{job="monitor-bridge"})`
  must equal `homelab_check_rows{job="monitor-bridge"}`.
- **Size.** The PR must stay within slice format item 5's 100 to 200 lines. If the measured
  diff exceeds about 200 added lines, it splits along its two readers. The first PR moves
  `cli.py`'s filter helpers, and `gates.py` keeps `GATE_DEPENDENTS` for `run_once`. The
  second moves `run_once` and the registry, and deletes `gates.py`.

### A7, the log arm M2 leaves on the bridge

M2's Open question 6 lists the port, and this batch adopts that list as written. M5's text
keeps `with_log_errors` and does not retire the row's token, so without A7 the row would
reach A6-7a, fail the bijection test and lose its only pager.

- **Shadow PR (~80 lines).** A LogQL rule `WorkloadLogErrors` in
  `loki-homelab/files/rules/homelab-logs.yaml`, with the row's pattern, window and
  `LOG_ERROR_MAX`, `for: 5m` for `startup_grace`, `shadow: "true"`, `severity` from the row's
  `critical` flag and `service: k3s`, the value M2's cluster-wide k8s-workloads rules carry.
  `monitor-bridge` would name a component that stops evaluating the check at Cutover-a. The
  soak starts only after M2's PR 4a, because the "Workload Log Errors" tile it compares
  against exists only from then. The `DECIDED stays at 20` marker moves into
  the rule file as a `#` comment, so `docs/reference/decisions.md` keeps its row. It deploys
  `loki-homelab`, which restarts Loki and the Alloy DaemonSet (M5 *Baseline*).
- **M5's three costs apply.** Its alert never reaches Prometheus `ALERTS`, so the health gate
  cannot see it (M5 open question 1). LogQL has no promtool test (M5's departure from M1
  convention 2). Every rule edit restarts Loki and Alloy. Its red proof is M5's: a `lokitool`
  parse, a selector entry in `test_loki_stream_labels.py`'s census, and a live evaluation
  that fires on a known burst. The soak compares the rule's firing minutes with the tile's
  DOWN minutes, per M1 convention 6.
- **Cutover-a (~10 added, ~250 removed).** It deletes the `workload_log_errors` row,
  `check_workload_log_errors`, `checks/logs.py:with_log_errors`,
  `verdicts/cluster.py:log_error_verdict`, the `LOG_ERROR_*` keys in
  `templates/env-secret.yaml.j2` and their tests. The rule stays shadow. `land.sh` deploys
  `uptime-kuma` and `monitor-bridge`.
- **Cutover-b (~10 lines).** It drops `shadow` from `WorkloadLogErrors`. `land.sh` deploys
  `loki-homelab` alone, under the two-hour bound.
- **Token PR.** It retires `monitor_bridge_k8s_workloads_push_token` a week after Cutover-b.

### B0, B5, B7 and B8

- **B0's library reaches every host.** Consumers source `homelab-check-lib.sh` with
  `|| exit 1`, and they render under other roles' tags. The `kuma-push-lib.sh` copy in
  `initial_setup/tasks/crons.yml` is `tags: [always]` for that reason: its comment says the
  library "has to be on disk before any of them are rendered". `install_host_lib.yml` would
  not do, because it runs under each consumer's tag only. B0 therefore adds its copy task and
  its `stamp_deployed` record beside the `kuma-push-lib.sh` pair, both `tags: [always]`.
  `always` reaches every `initial_setup.yml` run, the Pi's `--tags optimize_pi` included. A
  `k3s-bringup.yml` run does not include `crons.yml`, which is why every host batch waits for
  B0's apply on its own host (*Order*). B0's test finds the copy task by its name and fails
  when it lacks `always`, and it fails when a rendered script sources the library without
  `|| exit 1`.
- **B5, the Pi's textfile collector. Already in place since H3.6;** the two steps below are the record of what that PR did, and B5 adds neither. `hosts/tests/test_pi_node_exporter_host_unit.py` holds the flag and the directory. The producers are the package's `pi-sd-health` and `pi-recovery-health` scripts (H3.8b), which source a package copy of `homelab-check-lib.sh` (`host_sources.SHARED_FILES`). In `optimize_pi`, under the `node-exporter-host` tag:
    - replace `--no-collector.textfile` in `templates/node_exporter.service.j2` with
      `--collector.textfile.directory=/var/lib/node-exporter-textfile`;
    - add the directory task with M3's owner contract: owner root, group `{{ sys_user }}`,
      mode 2775, files written 0644.

  A test in `hosts/tests/` asserts that the unit's flag and the directory task name the
  same path, and that the unit no longer carries `--no-collector.textfile`.
- **B6b, the pod producers (operator ruling of 2026-10-23):**
    - **The crowdsec-jobs sidecar** is long-running, so it is an exporter. `files/jobs_metrics.py`
      serves `homelab_check_success`, `homelab_check_last_run_timestamp_seconds` and
      `homelab_check_deadline_seconds`, labelled `check="<job tag>"`, for the three jobs that
      push to Kuma. The bridge exporter uses the same names. A `crowdsec-jobs` item in the
      `metrics` list of crowdsec's `containers_list` entry (port 9182) renders the scrape job,
      the Service port and the sidecar's `JOBS_METRICS_PORT`, and netpol-baseline's Prometheus
      grant on crowdsec gains the port. The timestamp starts at the exporter's start, so a
      restart does not read as a stale job.
    - **The two CronJobs** are short-lived, so each patches `homelab/check-verdict`,
      `homelab/check-last-run` and `homelab/check-message` onto a ConfigMap its role renders
      with no annotations (`configarr-health-verdict`, `janitorr-health-verdict`). The Role
      grants `patch` on that one name. A reader whose export fails exits 1.
    - **The smallest kube-state-metrics change is two collectors, `configmaps` and `cronjobs`,
      and one allowlisted annotation.** `jobs` was already collected (#5675).
      `--metric-annotations-allowlist=configmaps=[homelab/check-verdict]` exports the verdict
      as the label `annotation_homelab_check_verdict`, and the Prometheus job drops every
      other `kube_configmap_*` and `kube_cronjob_*` family and every ConfigMap series without
      the annotation. `cronjobs` adds `kube_cronjob_status_last_successful_time`, the run clock,
      because an annotation is a label and PromQL cannot compare it with the clock. The change
      adds a ClusterRole `list`/`watch` on ConfigMaps, which cannot be narrowed by name. It
      exports metadata only, and no Secret grant is added. This is the reconciliation of
      contracts.md's C7 text, which names `cronjobs,jobs`: `jobs` was already there, and the
      recipe needs `configmaps` too when a CronJob reports a verdict rather than only success.
    - **Rejected: exit non-zero on a bad verdict**, so that `kube_job_status_failed` and
      `last_successful_time` carry it with no ConfigMap. The readers deliberately exit 0 on a
      bad verdict, and configarr's gate Job would then hold a revision on a failing sync.
    - **Rejected: a Pushgateway** and **a hostPath textfile**, by the ruling. A hostPath would
      also need the pods on daniel-box and a root-owned directory.
    - **Rejected: annotating the CronJob or its Jobs.** A `patch` grant on CronJobs lets the
      reader rewrite its own image, and Jobs have generated names that `resourceNames` cannot
      list.
    - **Shadow rules** are the `pod-producers` group (`files/rules/pod-producers.yaml`): a
      failed rule per verdict, `CrowdSecJobStale`, `CrowdSecJobsExporterAbsent`, and a
      `NotReporting` rule per CronJob at three periods of its schedule (1800s).
    - **`KUMA_RETIRING_TAGS`** gains the five tags: `crowdsec-remote-allowlist`,
      `crowdsec-appsec-verify`, `crowdsec-home-allowlist`, `configarr-health` and
      `janitorr-health`.
    - **The soak** is 7 days from the deploy, and the orchestrator records it.
- **B7, the pods:**
    - **autofix-bridge** serves `homelab_check_*` on `/metrics` and gets `AutofixBridgeDown`
      and `AutofixBridgeStale`, the same pair as the monitor-bridge exporter, with the same
      `for: 10m` and the same restart cases. Its Cutover-2 also deletes the `kuma_push.py`
      staging entry from `autofix-bridge/defaults/main.yml` and the `CLAUDE.md` line that
      names it, which C-3a requires.
    - **cloudflare-ddns.** favonia's image serves no metrics. Two rules cover it:
        - A Prometheus rule `CloudflareDdnsUnavailable` on
          `kube_deployment_status_replicas_available` for each of the two Deployments, so
          `probe.py health cloudflare-ddns` sees a dead updater.
        - A loki-homelab rule (M5) that fires on `absent_over_time` of each Deployment's
          update line. It carries M5's three costs. Its alert never reaches Prometheus
          `ALERTS`, so the health gate cannot see it (M5 open question 1). LogQL has no promtool
          test (M5's departure from M1 convention 2). Every rule edit restarts Loki and the
          Alloy DaemonSet (M5 *Baseline*). Its red proof is M5's: a `lokitool` parse, a
          selector entry in `test_loki_stream_labels.py`'s census, and a live evaluation.
    - **pi-peer-backup** uses the CronJob recipe. B6b already added the kube-state-metrics
      collectors it needs, so B7 changes no kube-state-metrics argument.
    - **The shadow PR, as written.** One PR covers the four producers.
        - **File and group.** The rules are the `pods` group in `files/rules/pods.yaml`. The
          cloudflare-ddns log rules are in loki-homelab's `homelab-logs.yaml`.
        - **autofix-bridge** ships crowdsec's `jobs_metrics.py` in its ConfigMap rather than
          monitor-bridge's `verdict_export.py`, because that module's listener lives in
          `deploy_pipeline.py`, which imports the whole bridge. The `check` label is
          `arr-autoblock`. `jobs_metrics.py` has no row-count gauge, so `AutofixBridgeStale`'s
          missing-row arm reads no `homelab_check_success` series while `up` is 1. The pair keeps
          `for: 10m`, `critical` and `shadow`. The design named only the pair, but the tile's
          DOWN needs a verdict rule too, so `ArrAutoBlockFailing` (`warning`, no `for:`) ports it.
          The inhibit on `ScrapeTargetDown{job="autofix-bridge"}` waits for Cutover-1b.
        - **cloudflare-ddns.** `CloudflareDdnsUnavailable` is `warning` with `verify: "true"`,
          because the health gate reads `critical` alerts and `verify` ones, and the tile has no
          severity tag. It overlaps `K8sDeploymentZeroAvailable`. The two LogQL rules match
          "are already up to date", "Updated an outdated" or "Added a new" over 15 minutes. They
          match neither the Kuma ping line, which stops at Cutover-2, nor the "Checking the IP
          addresses" line, which prints after a failed cycle too. They are slower than the tiles
          (6 minutes, one missed beat) by design.
        - **pi-peer-backup** has no kubectl, so its script sends one merge PATCH with curl and a
          ServiceAccount token. The Role grants `patch` on `pi-peer-backup-verdict` and no
          `get`, because the script sends no GET. The pod now mounts an API token. The gate run
          skips the export. `PiPeerBackupNotReporting` waits three nights (259200s), 12 hours
          longer than the tile's 60 hours.
        - **`service` labels** name the thing checked: `autofix-bridge`, `cloudflare-ddns` and
          `pi-peer-backup`.
        - **`CheckWithoutPager`** gains four terms: Arr Auto-Block, Cloudflare DDNS Direct and
          Cloudflare DDNS Proxied at `[2h]`, and WG Pi Peer Backup at `[120h10m]` (2 x 216000s +
          10m). **`KUMA_RETIRING_TAGS`** gains `pi-peer-backup` alone, because the other two pods
          log to no stream `swallowed_verdicts` reads.
- **B8, the probes:**
    - Modules: `tcp_connect`, `dns`, and keyword checks through
      `fail_if_body_not_matches_regexp`.
    - The Pi ping becomes `up{job="node-pi"} == 0`. Loki's `/ready` retires, because M5's
      `LokiHomelabDown` covers it.
    - **The Pi's published ports.** One `tcp_connect` target per `pi_publishers` entry,
      rendered from the same `hostvars['daniel-pi'].containers_list` selection as
      `PI_PUBLISHED_PORTS`, each labelled `service: <container name>`. The rule `PiPortDead`
      keeps the bridge's down streak as `for:` and its annotation keeps the recreate hint. A
      target-count test asserts one target per publisher. B8's cutover deletes the
      `pi_pressure` row, `with_pi_ports` and its tests, and moves failure-classes row 6 to
      `PiPortDead`'s promtool firing case.
    - **Grants.** `blackbox-exporter` joins each grant that names `uptime-kuma`, from the
      caller census above.
    - No probe of Kuma's own route: it sits behind Authelia `one_factor`, so it would read a
      302 whether or not Kuma is up (class 2). `KumaDown` watches Kuma instead.
    - Token: none of the 14 is a push monitor. The 1 token in the table is `pi_pressure`'s.
    - **The soak read** is recorded in *B8 probes*, under *Status by batch*.

### Phase B — switch the readers

1. **`probe.py health`.** A new `scripts/diagnostics/probe_lib/health_alerts.py` reads the
   alert plane through `probe_lib/obs_api.py`. `run_health` folds it into its verdict after the
   rollout rollup.
    - **Deploy-pipeline and `flux` rules (C7).** They carry `service` labels like every
      rule. A critical `flux` rule fails the `flux` tag's gate. D2.6d's rules carry
      `service="deploy-pipeline"`, the one literal C7 *Rule labels* admits outside its
      rule. It names no deploy tag, so no health gate reads it. `health_alerts.py` needs no
      special case for either source.
    - **Merge gate.** B-1 merges only after C7's amendment for Open question 4 has landed in
      contracts.md. Until then C7's literal selector stands (contracts.md *Requested
      amendments not yet applied*).
    - **The reference time** depends on the tag's `owner` field (C4):
        - For an Ansible-owned tag it is `applied_at`, read with the existing
          `health.py:_deploy_applied_at`. `release_stamp.yml` writes that record after each
          real Ansible apply.
        - For an `owner: flux` tag the Ansible play skips the entry (C4), so the release
          record freezes at the last Ansible apply and would make every later alert gate. The
          reference is instead the time the tag's Kustomization first reported its current
          content, read from C6's `gotk_resource_info` `content_digest` label. The `revision`
          label moves on every master commit, whether or not it touches the tag, so it is the
          fallback only for a series without `content_digest` (#5338). With no such
          series, every firing alert in the gated set below gates, as with no release
          record.
    - **After a Prometheus restart**, an alert's `ALERTS_FOR_STATE` value may restart at the
      restart time, so a long-standing alert can read as new. That is recalled upstream
      behaviour (Open question 9), not a fact the tree shows. The intended verdict is to gate
      and name the alert with "active since Prometheus started". `health_alerts.py` reads
      `process_start_time_seconds{job="prometheus"}` to say so. A wrong failure is noisy; a
      wrong pass would read a fresh breakage as old.
    - **Alerts.** It reads the union of two selectors, joined to `ALERTS_FOR_STATE`, whose
      value is each alert's activation time:
      `ALERTS{alertstate="firing",service="<tag>",shadow!="true",alertname!="Watchdog",severity="critical"}`
      `or ALERTS{alertstate="firing",service="<tag>",shadow!="true",alertname!="Watchdog",verify="true"}`.
      That is the set D5's soak reads: the critical alerts, and any alert labelled
      `verify="true"` whatever its severity (contracts.md C7 *Verify successors*, shape 1).
      An alert in the set gates only when it became active after the reference time, as the
      soak's does. Older alerts, and `warning` and `info` alerts without `verify`, are
      printed as notes and never gate. With no reference time (no release record), every
      firing alert in the set gates.
    - **Probes.** The reference time for a probe is the moment `run_health`'s rollout rollup
      reports done, not `applied_at`. Under `Recreate`, a sample taken after the apply but
      before the new pod is Ready reads 0 and must not decide. The gate reads
      `probe_success{service="<tag>"}` with `timestamp()`:
        - no series within the lookback means the tag has no probe (a route-less tag such as
          `monitor-bridge` or `netpol-baseline`), so the probe half is skipped;
        - a tag whose rendered entry has no route skips the probe half too, even with an old
          series inside the lookback. The rollout half already renders the tag's role, so
          the probe half reads the route from the same render. Without this, C-1's removed
          route would read as a probe that stopped reporting;
        - a series whose newest sample predates the reference means wait: the gate re-reads for
          up to 90s (one 1m scrape interval plus slack), then fails naming the probe, because
          a probe that stopped reporting must not read as clean (class 3);
        - a fresh 0 means fail;
        - a routed tag with no series after the wait fails when `up{job="blackbox-ingress",
          service="<tag>"}` exists, since its loaded target stopped reporting, and when the
          job has no `up` series at all. It passes with a note when the job is loaded but
          the target is not: the route was added since `observability`, which is denylisted
          from auto-deploy, last deployed (#5346).
    - **Re-read once.** A probe alert that fired during a slow cold-pull rollout can still read
      firing at the first read. Both halves re-read once after the same wait before failing.
    - A window function such as `max_over_time` is not used: a window spanning the rollout lets
      the old pod's 1 pass a broken deploy.
    - It never reads a loki-homelab rule (M5 open question 1).
    - Every departure from C7's literal selector is listed in *Open questions*.
2. **`probe.py monitors`** becomes a rollup of firing alerts from `ALERTS`, grouped by
   `service` and `severity`. Root `CLAUDE.md` says "`monitors` answers what is down", and that
   stays true. **`probe.py alerts`** reads `ALERTS` history over `--days` from Prometheus instead
   of rebuilding bridge pushes from Loki. **`probe.py kuma-drift`** is deleted, with its half of
   `monitors.py`, `kuma_live.py`, `kuma_table_loop.py` and their tests. B-2 also edits the CLI
   wiring: the `kuma-drift` parser and its `--pi` flag in `probe_lib/cli_parser.py`, the
   registry row and the `run_kuma_drift` import in `probe_lib/subcommands.py`, and the
   docstring in `probe_lib/pi_plane.py` that says `kuma-drift`'s `--pi` handling stays in
   `monitors.py`.
    - **`--pi`.** At the baseline `monitors` has no `--pi`; `targets`, `kuma-drift` and
      `alerts` carry it. `monitors` stays without one. `alerts --pi` keeps its flag and
      selects the alerts whose `job` is one of the Pi's scrape jobs, the set
      `pi_plane.py` derives from `prometheus.yaml.j2`, or whose `service` is a Pi container
      (B8's `PiPortDead` targets). The flag's help text changes with it.
    - **The docs that point readers at `kuma-drift` change in B-2**, not B-7: the root
      `CLAUDE.md` *Claude Tooling* bullet, `docs/claude-tooling.md`, and the `kuma-drift` steps
      in `.claude/skills/add-secret/SKILL.md`. The facts lint's `vanished-identifier` rule
      flags a backticked `kuma-drift` once B-2 removes it from the code, so B-2 cannot land
      without them.
      `docs/reference/scripts.md` is generated and regenerates on its own, and the
      `evals/review_coverage/` JSON files are history.
3. **postflight**, in the same PR as item 2, because `postflight.py` imports `kuma_live` and
   `monitors` at module level. `check_kuma_monitors` and `check_kuma_drift` become one check:
   `up{job="alertmanager"} == 1` and the Watchdog firing. `check_kuma_scrape` stays until C-1,
   which deletes the scrape job it reads.
4. **homelab-mcp.** No code change: it has no Kuma tool. B-7 rewrites the one `CLAUDE.md`
   sentence about the Kuma `/health` tile to name B8's keyword probe.
5. **Homepage.** The Uptime-Kuma tile becomes an Alertmanager tile with a `prometheusmetric`
   widget. Its blocks are firing critical, firing warning and the Watchdog. Its CSS overrides
   and their test move with it.
6. **Grafana.** `uptime-kuma-monitors.json` is deleted. `alert-history.json` is rebuilt on
   `ALERTS`. Its panels group by `service`, so the deploy-pipeline and `flux` rules appear
   under the `service` values C7 gives them.
7. **The Alertmanager UI.** A LAN-only route, `alertmanager.local.<domain>`, behind Authelia
   `two_factor`, because it can silence every alert. It replaces the Kuma UI's pause button and
   resolves M1 open question 2.
    - **The policy.** An entry carries one `hostname`, and observability's is `grafana`, so
      `authelia_service_rules` cannot express a second route. The rule is hand-written in
      `authelia/templates/config-secret.yaml.j2`, ahead of the generated block, with a comment
      saying why. Without it the `*.local` wildcard would give `one_factor`.
    - **The route.** A hand-written IngressRoute in the `observability` role, LAN-only like
      `prometheus-ingressroute.yaml.j2`, naming the `authelia` chain Middleware that
      `observability/templates/middlewares.yaml.j2` already copies into the namespace.
    - **Order.** The policy deploys first (B-6a, `authelia`), the route second (B-6b,
      `observability`). The route therefore never exists under the wildcard's weaker tier.
8. **Docs, skills, agents and evals.** Every other page that tells a reader to look at Kuma
   changes in B-7:
    - the root `CLAUDE.md` *Where to Look* rows (B-2 has already edited the *Claude tooling*
      bullet);
    - the rest of `.claude/skills/add-secret/SKILL.md`, `.claude/skills/new-container/SKILL.md`
      and `.claude/skills/homelab-review/accepted-designs.md`;
    - the three reviewer agents;
    - `.claude/hooks/hooklib/service_lines.py`;
    - the eval cases that name Kuma;
    - `ansible/roles/k8s/homelab-mcp/CLAUDE.md`.
9. **Triage's poller** drops its Kuma source in B-10. It keeps the `ALERTS` path alone:
    - **The code.** `KUMA_QUERY`, `KUMA_UP_QUERY`, `kuma_up_keys`, the `kuma` arm of
      `unknown_sources` and of `down_keys`, and the `kuma_up` argument of `step` go.
    - **The state.** The first poll on the new code drops every `kuma:` key from
      `poll-state.json` and opens no episode for it. Without that step, a Kuma key that
      was down stays down for good, because no source can recover it any more.
    - **The docs.** *Slice 2: the poller* in `docs/claude-agent-user.md`, the poller's
      module docstring, and the component's `CLAUDE.md` describe one input.
    - **Order.** B-10 lands after Phase A, when every Kuma pager has an `ALERTS` rule, so the
      `ALERTS` path sees each fall the Kuma path saw. It lands before C-1a, which deletes
      the scrape job the poller reads. It ships in a new version of daniel-box's host package,
      the only host that runs the poller (`claude_code_triage_enabled`), so C-1a waits until
      daniel-box has installed it.

### Phase C — delete Kuma

**C-0, the ADR (operator-only).** The agent writes "One alert plane on Prometheus and
Alertmanager; Uptime Kuma retired" under `docs/adr/` at the next free number when it is
written, as Proposed, with or before B-7. D6 and G2 also take numbers, so none is reserved
here. C-1 waits for the operator's acceptance.

1. **C-1, empty the role**, in two PRs. C-1a removes Kuma's watcher in `observability`, and
   C-1b empties `uptime-kuma`. Both roles are flux-owned, and their Kustomizations share no
   `dependsOn` edge, so one promotion would apply the two halves in parallel (*When D5 flips
   a role M6 edits*). Elsewhere in this plan, "C-1" names both PRs, and "C-1 has deployed"
   means that C-1b's prune has reached the cluster.

   **C-1a, Kuma's watcher.** Preconditions, each checked and quoted in the PR:
    - the ADR is accepted;
    - every Phase A cutover, B8's cutover and M4's cutover have landed. This precondition also
      asked for a week of Phase A with no Kuma page, and the operator waived that week on
      2026-11-03 ([#7061](https://github.com/DanielH2018/server/issues/7061));
    - the one-series gate holds. It has two halves, and both must hold:
        - `probe.py metric 'count by (monitor_type, monitor_name) (monitor_status)'`, an
          instant read, returns exactly one series, the `Status Page Sync` push tile. Any other
          series, of any type, fails the gate. This read needs the scrape job that C-1a
          deletes, so C-1a quotes it and C-1b does not repeat it.
        - `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2` declares
          `status-page-sync.json` as its only monitor at the commit `prod` holds. The
          `homelab` OCIRepository's `org.opencontainers.image.revision` annotation names
          that commit. To list the monitors, filter the file's entity keys at that commit
          with `grep -E '\.json: \|$' | grep -vE '^ +(discord|email|tag-[a-z]+)\.json:'`,
          which drops the notifications and the tags.

      The instant read alone can pass falsely after a Kuma restart, because a weekly push
      tile has no series until its next push. The declaration catches that case, because a
      declared tile that a restart silenced is still declared. A
      `last_over_time(monitor_status[8d])` read cannot replace the pair, because it also
      returns every tile a cutover deleted inside the window: 99 series on 2026-11-03
      ([#7075](https://github.com/DanielH2018/server/issues/7075)).
    - B-2 and B-4 have landed and deployed. B-4 removes homepage's
      `homepage_href('uptime-kuma', domain)` call, which raises once the entry loses
      `hostname`, and B-2 replaces postflight's Kuma reads.
    - B-10 has landed, and daniel-box runs the package version that carries it. Until then
      the triage poller reads the scrape job that C-1a deletes (*Phase B* item 9).

   Scope:
    - Delete `KumaDown` with its inhibit, Prometheus's `uptime-kuma` scrape job, the
      `kuma_api_key` file in `scrape-credentials-secret.yaml.j2`, and
      `postflight.py:check_kuma_scrape`. The scrape job calls `entry_port('uptime-kuma')`, so
      it must go before C-1b removes `port`. The rule and the scrape job go in one commit. A
      scrape job removed without the rule fires `KumaDown`'s `absent()` arm.
    - In `files/rules/scrape-targets.yaml`, delete the `ScrapeTargetDown` and
      `ScrapeTargetFlapping` rows for `job="uptime-kuma"`, and take `uptime-kuma` out of both
      catch-all `job!~` lists. Both rows read the `up` series the scrape job writes, so
      neither can fire once the job is gone.
    - In `scripts/diagnostics/probe_lib/monitors.py`, delete `KUMA_QUERY`, `format_kuma` and
      the `monitors` read they serve.
    - Arm 3 of the cross-plane order gate names any object `observability`'s render drops.
      The PR carries a `Removes-Object:` line for each one.
    - **The cluster file.** If `cluster_vars.py check` reports that the cluster file holds a
      key no render references, the operator runs `cluster_vars.py write` on the PR's branch
      and commits what it writes (**operator-only**, because it decrypts).

   The merge does not apply C-1a. `promote` applies it once it moves `prod` to a commit that
   holds C-1a, and `observability-verify` checks it there. After the promotion, the PR's
   follow-up quotes two read-only reads, each at least one rule-evaluation interval apart:
    - `probe.py metric 'count(up{job="uptime-kuma"})'` returns no series. Prometheus has
      reloaded its config without the scrape job.
    - Then `probe.py metric 'ALERTS{alertname="KumaDown"}'` returns no series. A rule still
      loaded would hold a `pending` series as soon as its `absent()` arm turned true, so an
      empty result shows that Prometheus reloaded the rules too. A `pending` series means the
      rule pages within 5 minutes. Stop and report it rather than start C-1b.

   **C-1b, empty the role.** Preconditions, each quoted in the PR: C-1a's two reads above,
   taken after `prod` held C-1a; and
   `git grep -n -w uptime-kuma -- ansible ':!*/tests/*' ':!*.md' ':!ansible/roles/k8s/uptime-kuma/*'`,
   which lists only files C-1b edits and C-2a's grants: the four bespoke policies and
   ical-proxy's `netpol_from` in `host_vars/daniel-box.yml`. The grep uses `-w` rather than a
   quoted `'uptime-kuma'`, because an unquoted YAML value such as `service: uptime-kuma` is a
   reader too. C-1b handles any other hit first.

   C-1b is the first of D2's two retirement PRs (`docs/flux-deploy-plane.md`, *Retiring a
   flux-owned service*). Its scope:
    - Delete the seven templates. The render of the `uptime-kuma` tag then holds only the
      two claims, and Flux prunes every other object the render drops. No tombstone is
      needed: the `k8s/manifests` prune guard that required one runs only in an Ansible
      deploy, and every Ansible path skips a flux-owned tag.
    - Delete the task "Read the AutoKuma declarations back as data" and the comments above
      it. Its `lookup('template', ...)` reads `static-monitors.yaml.j2`, and the renderer
      evaluates it (`scripts/renderer/renderer_lib/task_inputs.py`). The two asserts on
      `kuma_declarations` left in uptime-kuma's conversion PR (#5712), before D5's flip.
    - Delete `tests/test_declaration_index_flux_form.py`, which reads the static-monitors
      Secret the render no longer emits, and every other test under the role's `tests/` that
      renders a deleted template. A test that reads only `files/` stays until C-2b.
    - Edit the include. Drop `manifests_secret_files`, which names the two deleted Secret
      templates. Set `manifests_rollout: ''`, because the renderer refuses a restart target
      that names no workload in the render, and `manifests_rollout` defaults to the tag.
      `k8s_claims` stays until C-2b, so the claims stay declared beside the entry's
      `no_backup_claims`, which `longhorn_groups.py` reads for the no-backup tier. D2's form
      drops the claims in its first PR. Here they stay, because each carries
      `kustomize.toolkit.fluxcd.io/prune: disabled` and Flux would leave them either way.
    - Drop `port`, `hostname`, `use_authelia`, `auth_tier` and `netpol_from` from the entry.
      The shared default Service and IngressRoute stop rendering, so the entry needs no
      `manifests_exclude_files`. D2's form leaves the entry alone only because a field edit
      moves the render of every Ansible-owned role that reads `containers_list`, and no role
      C-1b moves is Ansible-owned. Every other tag whose render the edit moves is ordered or
      order-free:
        - `netpol-baseline` and `netpol-baseline-verify` apply after uptime-kuma's prune, on
          the `dependsOn` edges in *When D5 flips a role M6 edits*.
        - `authelia` is safe in either order against the route's prune (the same section).
        - `blackbox-exporter` drops `uptime-kuma.local.<domain>` from the `hostAliases` the
          `ingress_host_aliases()` macro renders. The entry's `kuma: false` gives it no
          probe, so the order does not matter.
        - `pihole` drops the `address=/uptime-kuma.local.<domain>/` line from its dnsmasq
          ConfigMap. Nothing resolves that name once the route is pruned, so the order does
          not matter. The ConfigMap change restarts pihole, as any `hostname` change does.
        - The pushers that read `entry_port('uptime-kuma')`, cloudflare-ddns among them,
          stopped in Phase A. A render at `fdae34795` with the entry edit applied still
          fails on autofix-bridge's `entry_port` call, so the C-1b PR quotes its own render
          to show that no reader is left.
    - `netpol_from` goes in the same commit because `networkpolicy-callers.yaml.j2` renders
      `entry.port` for every entry with callers. Its seven callers are gone by then: Phase A
      stopped the five pushers, and this PR prunes the two CronJobs. The `# KUMA_URL`
      comments on that list in `host_vars/daniel-box.yml` go with it.
    - Delete the `uptime-kuma` row from `netpol_baseline_probe_targets` in the same commit,
      for the same reason. The row has no `port`, so `netpol-probe-job.yaml.j2` reads the
      entry's through `entry_port(t.service)`, which raises once `port` is gone. With the
      port patched in, `netpol-baseline-verify` would probe a Service this PR prunes. The
      comment above the row's fencing-shape group drops `uptime-kuma`.
    - Add a named-member case to `ansible/tests/k8s/test_netpol_probe_targets.py`: every row
      without `port` names a `containers_list` entry that exists and has `port`. After this
      PR no portless row names uptime-kuma, so the case's red proof is a synthetic fixture.
      A portless row naming a portless entry must fail, and so must a portless row naming no
      entry. C-2b's entry removal then cannot bring the failure back unnoticed.
    - Delete the hand-written `uptime-kuma.local` bypass in
      `authelia/templates/config-secret.yaml.j2`. Dropping `hostname` removes the generated
      rule.
    - Rewrite M2's weekly-reboot mute test to derive the window from `group_vars` alone,
      dropping its Kuma-equality half, because `test_maintenance_window.py` and the
      maintenance CronJob go with the templates.
    - **`Removes-Object:` lines.** Arm 3 fails the PR for each object a flux-owned tag's
      render drops without a line, and its failure line names the exact line to add. The
      `uptime-kuma` tag drops its Deployment, Service, two IngressRoutes, ConfigMaps,
      CronJobs and two Secrets, and `netpol-baseline` drops the `uptime-kuma` callers policy.
      The claims stay rendered, so they need no line.
      Take the list from the arm's output, not from this paragraph.
    - **The cluster file.** The render no longer references Kuma's credentials or the last
      push token, so `cluster_vars.py check` reports each key the cluster file still holds.
      The operator runs `cluster_vars.py write` on the PR's branch and commits what it writes
      (**operator-only**). The keys stay in `secrets.yml` until C-5. `cluster_vars.py`'s
      `_push_token` refusal checks a token's format and goes in C-5 with the tokens.
    - **The render check.** `deploy.sh --dry-run` refuses a flux-owned tag with exit 5, so
      the PR quotes `homelab-render cluster --role uptime-kuma netpol-baseline` run from its
      worktree instead. A clean render shows that the emptied directory renders and that the
      probe Job renders without the row. CI's cross-plane order gate renders the cluster at
      the PR's base and head.
    - **Tests.** `ansible/tests/k8s/test_netpol_probe_targets.py` runs in this PR's CI.
      `test_every_expecting_row_renders_a_leg` renders the probe Job and goes red on the
      `entry_port` raise, and the new named-member case holds the table to the entries.
    - **The promotion's gates.** `promote` applies C-1b, and its soak reads the critical
      alerts of every flux-owned tag:
        - `KumaDown` left in C-1a, so the prune fires nothing in `ALERTS`.
        - M5's `KumaNotificationDropped` is `critical` with `service: uptime-kuma`, but it is
          a loki-homelab rule, so it never reaches `ALERTS`.
        - `swallowed_verdicts`'s rule is a `warning` with `service: monitor-bridge` and no
          `verify`, so it gates no tag.
        - The `uptime-kuma` tag has no `-verify` Kustomization. `netpol-baseline-verify`
          runs without the row.
    - After the promotion, the read-only
      `kubectl get svc,ingressroute,cm,cronjob,deploy -n homelab -l homelab/role=uptime-kuma`
      returns nothing. The agent's ServiceAccount cannot read Secrets, so the operator
      confirms that the two Secrets are gone in the delete after C-2b. The two claims stay
      bound and detached.
2. **C-2, the grants and the entry**, in two PRs:
2. **C-2, the grants and the entry**, in two PRs:
    - **C-2a.** Delete `uptime-kuma` from the four bespoke policies and from ical-proxy's
      `netpol_from`. Deploy the roles, then `netpol-baseline` last, per its `CLAUDE.md`. The
      probe row went in C-1.
    - **C-2b.** The second of D2's two retirement PRs. Its precondition, quoted in the PR,
      is that `promote` has moved `prod` to a commit that holds C-1b and that C-1b's
      `kubectl get` returns nothing. Delete the role directory and its `containers_list`
      entry. The next bundle omits `flux/uptime-kuma.yaml`, and the root Kustomization prunes
      the `uptime-kuma` Kustomization. `deletionPolicy: Orphan` leaves only the two claims
      behind, because C-1b's prune already removed every other object. The name never joins
      `k8s_retired_services`, which arm 4 refuses for a flux-owned entry. Removing an entry
      moves every reader that iterates the list (*Baseline*, *Narrowing*), so record the
      narrowing's answer in the PR and follow *Steps* if it refuses.
      `k8s_host_files/tasks/uptime-kuma.yml`, the absent arm for the autokuma-claims staging
      directory, goes in the same PR with its import, once a read-only `ls` on daniel-box
      finds no such directory. After the promotion, the operator deletes with kubectl
      (**operator-only**) the objects no prune removes: the two claims, either Secret
      that a `kubectl get secret` still finds, and in `flux-system` the `prepull-uptime-kuma-*`
      Job and ConfigMap that C-1b's dropped prepull Kustomization orphaned. No promotion
      re-creates them, because the entry is gone. C-6's PR quotes exit criterion 1's `kubectl get` returning nothing.
3. **C-3, the plumbing**, in two PRs:
    - **C-3a, the push libraries.** Delete `kuma-push-lib.sh` with the `kuma-push` host
      component that packages it, and `hosts/lib/kuma_push.py` with its re-export from
      `hosts/lib/host_lib.py`. Delete the `kuma_push.py` rows from
      `deploy_cross_role.py`'s two tables. C-1b dropped the k8s row's `uptime-kuma` member. C-3a
      lands after C-2b, which deletes `uptime-kuma/files/push_heartbeat.py`, whose docstring
      names `kuma_push.py`. Every reader of `kuma_push.py` changes in the same commit, or a
      copy of a missing `src` fails the role that holds it:
        - `install_host_lib.yml` drops `kuma_push.py` from its loop and its header comment,
          and gains a `state: absent` task for the old copy beside each `host_lib.py`.
          `test_host_lib_sibling_copies.py` changes with it.
        - The nine `stamp_deployed` pairs for `kuma_push.py` go (*Baseline*, the push
          plumbing): `gitops_deploy`, `renovate_agent`, `renovate_notify`, `fake_remux`,
          `deploy_ui`, `configarr`, `janitorr` and the two in `k3s/tasks/health-crons.yml`.
          The baseline counted eight, before `deploy_ui` gained its pair. H2.5b and H2.5c
          removed the configarr and janitorr pairs, so seven remain at C-3a's base.
        - **The two health ConfigMaps change in the same commit.** B6b's Cutover-2 kept
          `kuma_push.py` in each `health-configmap.yaml.j2`, because `host_lib.py` imports it
          at module level, and deleting the file breaks both Flux renders. C-3a edits both
          ConfigMap templates, configarr's `health-cronjob.yaml.j2`, both `*_health.py`
          docstrings and both role `CLAUDE.md` files. One promotion applies each ConfigMap and
          its `host_lib.py` change together, and the cross-plane order gate judges the PR.
        - **`render_records.py` loses its push.** It reads `kuma_push` through the `host_lib`
          re-export, so `git grep kuma_push.py` does not list it. The role is pinned
          `state: absent`, so no host runs the script, and the edit changes no host.
        - **The `kuma-push-lib` drift fragment goes with the library.** Dropping the
          `stamp_deployed` record task leaves
          `/var/lib/homelab/setup-deployed-manifest.d/kuma-push-lib` on each host, and
          `setup-drift-lib.sh` reports a pair whose source left the repo as drift. The absent
          task below removes both paths.
        - The setup/common `CLAUDE.md` rows that name `kuma_push.py` are reworded. B7's
          autofix-bridge Cutover-2 has already reworded that role's line.
        - **The host packages change in the same commit.** *Files under `hosts/`* in
          *Baseline* lists each file. The `kuma-push` component leaves the `components:`
          list of all three `hosts/<host>/package.yml` files. The `lib: kuma_push.py` rows
          leave the `builder` and `claude-code` components. The renderer's two rows for the
          Ansible copy of `kuma-push-lib.sh` go too: `SHARED_FILES` in
          `scripts/renderer/renderer_lib/host_sources.py` and the `HOST_INPUTS` entry in
          `scripts/renderer/renderer_lib/host_inputs.py`. A row left behind names a deleted
          file, so every package render fails.
        - **Precondition, quoted in the PR.** B7's autofix-bridge Cutover-2 has removed the
          staging entry from `autofix-bridge/defaults/main.yml`, and C-1 has deleted the
          status-page sync's copy. `git grep -l kuma_push.py -- ansible hosts scripts ':!*/tests/*'`
          lists only files this PR edits or deletes.
        - **Precondition, quoted in the PR.** Every host producer's Cutover-2 has applied on
          its own host. Three reads show it. First, `homelab_check_last_run_timestamp_seconds`
          for every B1 check, every daniel-server check (B2's witness, B4's UPS secondary),
          every Pi check (B5) and every B9 integrity scan on all three hosts is newer than that
          batch's recorded Cutover-2 apply. That alone is also true of a shadow-phase script,
          which still sources the library. Second,
          `git grep -n kuma-push-lib -- 'ansible/roles/*/templates/*' 'ansible/roles/*/files/*' hosts ':!*/tests/*'`
          prints no script that sources the file. The pattern is not the full path, because
          B9's `aide-check.sh.j2` and `rkhunter-check.sh.j2` source it as
          `{{ initial_setup_host_lib_dir }}/kuma-push-lib.sh`, which the `integrity-scans`
          component renders as `/usr/lib/homelab`. Third, on each host, a read-only
          `grep -rl kuma-push-lib /usr/local/bin /usr/local/lib /usr/lib/homelab /opt` lists
          only the library's own copies: locally on daniel-box, and over `ssh` on
          daniel-server and the Pi. A path under `/opt/homelab/releases/<group>/<sha>/` does not
          fail this read unless `/opt/homelab/current/<group>` points at that `<sha>`. Retained
          releases keep the old `source` line until `release_bin_keep` prunes them, and no
          cron runs them.
        - **The host file.** `initial_setup/tasks/crons.yml`'s copy task is `tags: [always]`
          behind a `DECIDED` marker, and it runs on every host. C-3a replaces it with an
          `always`-tagged `state: absent` task for `/usr/local/lib/kuma-push-lib.sh` and the
          drift fragment, and drops the `stamp_deployed` record task for `kuma-push-lib`. The
          marker moves to the `homelab-check-lib.sh` copy task. The tick applies it on
          daniel-box. On daniel-server it is the hand run `land.sh`'s note names, run on that
          host. On the Pi it is `initial_setup.yml --tags optimize_pi -e target=daniel-pi`
          (**operator-only**), because `always` rides on any tag. C-6 deletes the absent task
          once a read-only `ls /usr/local/lib/kuma-push-lib.sh` finds no file on daniel-box,
          and over `ssh` on daniel-server and the Pi.
        - **Deploys.** The tick applies `initial_setup`, `gitops_deploy`, `renovate_agent`,
          `renovate_notify`, `fake_remux` and `deploy_ui` on daniel-box. `promote` applies
          `configarr` and `janitorr`, which are flux-owned. Their `/opt` copies are H2's absent
          arm in `k8s_host_files`, applied by the tick. The `k3s` hand apply is
          `--tags "backup-health,live-drift"` (**operator-only**), the tags on the two `k3s` stamps and on the tasks that install
          `host_lib.py` beside the backup health reader and the live-drift check. Each host
          installs a new version of its own package through its `homelab-updater.timer`, and
          dpkg removes the packaged `kuma-push-lib.sh` and `kuma_push.py` copies on that
          upgrade. `land.sh` waits for all three hosts.
        - **The `/opt` copies.** C-6 deletes the `install_host_lib.yml` absent task once a
          read-only `ls /opt/*/kuma_push.py` finds no file on daniel-box and, over `ssh`, on
          daniel-server.
    - **C-3b, the task-file rename.** ADR 0026 records the operator's ruling of 2026-10-24: the
      `kuma-check-*` units keep their names (*Open questions*, item 6). Rename
      `kuma_check_timer.yml` to `check_timer.yml`, its three templates to `check-timer.*.j2`,
      every `kuma_check_*` variable to `check_timer_*` at every importer, and
      `test_kuma_check_timer.py` with them. Without the variable rename, every importer fails
      the census row. The units and their down markers do not move, so the task file gains no
      legacy-unit removal. Update `SETUP_FILES_SHIPPED_BY_OTHER_ROLES`, `narrow_setup.py`,
      `setup_role_chains.py` and `test_setup_cross_role_files.py`, unless D6 has deleted them.
      Fix the reader of the glance blocks that name the timers:
        - `scripts/docs/catalog_lib/glance_facts.py:timer_units` finds a timer import only
          when the target ends in `common/tasks/kuma_check_timer.yml`, and it reads the
          `kuma_check_*` variables.
        - Unchanged, it finds nothing after the rename. The `regen-role-glance` hook would
          then rewrite the k3s, initial_setup, gitops_deploy and render_records blocks
          without their timers, and CI would agree with the generator (class 3).
        - C-3b points it at `check_timer.yml` and the new variables, and it keeps emitting
          `kuma-check-<name>.timer`. A named-member case in
          `scripts/docs/tests/test_gen_role_glance.py` asserts that the k3s role yields
          `kuma-check-manifest-prune.timer` and `kuma-check-live-drift.timer`, and it
          fails when the generator finds none.

      The rerun-while-red contract stays,
      because it still sets how fast a red check clears. The `k3s` hand apply is
      `--tags "manifest-prune,live-drift"`, the tags on its two timer imports.
    - **When D6.17 or D6.18 has landed first.** D6 opens after C-2b, so D6.17 and D6.18 can
      land before C-3a or C-3b. D6.17 disarms the two k3s timers and the live-drift check. It
      keeps the two timer imports and sets their `kuma_check_state` to `absent`. It turns the
      live-drift tasks into absent tasks that keep their `live-drift` tag. D6.18 deletes
      those imports and those absent tasks (#4609, #4635). Each PR therefore reads the k3s
      role at its base:
        - If D6.17 has set the two timer imports absent, C-3b still renames their variables.
          C-3b carries the literal `check_timer_state: absent` across the rename. Dropping it
          would re-arm the units that D6.17 disarmed. `glance_facts.py:timer_units` skips an
          import whose state is the literal `absent`, and C-3b keeps that skip under
          `check_timer_state`. Without the skip, the glance block would list two timers that
          are not installed. The named-member case therefore excludes the two k3s imports.
          It asserts that initial_setup yields `kuma-check-setup-drift.timer` instead. The
          `k3s` hand apply stays `--tags "manifest-prune,live-drift"`.
        - If D6.17 has merged but its hand apply has not run, C-3b can still merge first.
          D6.17's apply then runs on a tree that holds C-3b's rename, so the k3s absent arm
          is the renamed one, and it stops `kuma-check-manifest-prune` and
          `kuma-check-live-drift` as before.
        - If D6.18 has deleted the two timer imports, C-3b renames no k3s import and has no
          `k3s` hand apply. Its named-member case asserts the same initial_setup timer.
        - C-3a's `k3s` hand apply stays `--tags "backup-health,live-drift"` until D6.18
          deletes D6.17's absent live-drift tasks. If D6.18 has deleted them, the apply is
          `--tags "backup-health"` alone.

      The PR body quotes `git grep -n -E 'kuma_check_timer|kuma_check_state|live-drift' ansible/roles/setup/k3s/tasks/`
      at its base to show which case applies. A `kuma_check_state: absent` line on a k3s
      timer import means that D6.17 has landed. No `kuma_check_timer` import means that D6.18
      has landed.
4. **C-4, the names**, in two PRs:
    - **C-4a.** Add `ansible/filter_plugins/ingress_probes.py` with `ingress_probe_targets`,
      rename the `kuma:` key on `containers_list` entries to `probe:`, switch M4's probe job to
      the new filter, and add its row to `narrow_lib/containers.py:FILTER_FIELDS`. It also
      registers `ingress_probe_targets` in `scripts/lib/ansible_jinja_env.py`, beside the
      filters that file already imports, so a repo render of the probe job resolves it. No
      plugin is deleted, so the narrowing can answer (M4 open question 1).
    - **C-4b (landed, #7835).** Delete `kuma_monitors.py`, which no template calls after
      C-4a. In the same commit, remove its import and the `kuma_ingress_monitors` registration from
      `scripts/lib/ansible_jinja_env.py`, which would otherwise raise ImportError on every
      repo render. Remove the `kuma_ingress_monitors` row from
      `scripts/deploy_tools/narrow_lib/containers.py:FILTER_FIELDS` too, unless D6 has
      deleted the narrowing. `narrow_lib/filters.py` refuses any deleted plugin, so this PR
      waits for D6, which deletes the narrowing. Until then the file, its registration and
      that row are on the allowlist.
5. **C-5, secrets.** **Operator-only:** remove `kuma_status_page_sync_push_token`,
   `uptime_kuma_username`, `uptime_kuma_password` and `prometheus_kuma_api_key` from
   `secrets.yml`. The same commit deletes their rows from `ansible/secret_rotation.yml`, adds
   them to `RETIRED`, and carries the regenerated fragment. That is the token-PR rule in
   *Steps*. `render_records_push_token` stays for R4, which removes it after deleting the
   role that renders it, so exit criterion 2 holds only after R4's secrets commit.
6. **C-6, the census row.** It holds `kuma` to its allowlist from then on (exit criterion 3).
   Under `hosts/`, the grep below also matches the Kuma push producers and the push plumbing
   that moved into host packages after this plan was written, such as `hosts/lib/kuma_push.py`
   and `hosts/components/kuma-push/component.yml`. *Files under `hosts/`* in *Phase A* assigns
   each hit to its PR.
   Its precondition, quoted in the PR, is the read for exit criterion 3's first bullet. It
   must list allowlist entries only:
   `git grep -l -E "/api/push/|kuma_push|KUMA_URL|KUMA_PUSH_|monitor_status|entry_port\('uptime-kuma'\)|kuma-push-lib|kuma_ingress_monitors" -- ansible hosts scripts .claude ':!*/tests/*' ':!*/test_*'`.
   C-6 rewords any other hit whose PR missed it. Before C-4b, the C-4b entries stay listed.
   C-6 also deletes C-3a's two absent tasks, for `kuma-push-lib.sh` and for the `/opt` copies
   of `kuma_push.py`, once the reads C-3a names find no file. C-6 lands after C-7: until C-7,
   the `KUMA_REJECTED` comment in `verdicts/logs.py` and the `check_swallowed_verdicts`
   docstring name `kuma-push-lib.sh`, and `homelab-logs.yaml` holds
   `KumaNotificationDropped`, so the quoted grep and the census row would fail on C-7's
   files.
7. **C-7, M5's Kuma rule and the `swallowed_verdicts` row.** After C-1 and C-3a, delete
   `KumaNotificationDropped` from `homelab-logs.yaml`, the one Kuma rule M5 adds. Its subject
   is gone. The census source in `test_loki_stream_labels.py` stays, and its named member
   `LokiPodLogsSilent` survives. Deploying `loki-homelab` restarts Loki and the Alloy
   DaemonSet.
    - **The `swallowed_verdicts` row.** M5 hands it to M6 (M5 Open question 4), and A6
      exports it in the `startup_grace` family. Its subject, host pushes to Kuma, ends with
      C-3a. C-7 deletes in one commit: the row, `checks/logs.py:check_swallowed_verdicts`,
      `verdicts/logs.py:swallowed_verdicts` with `HC_ROUTED_TAGS`, `KUMA_RETIRING_TAGS` and
      the push-line parsing, its rule in `bridge-exporter.yaml`, and its tests.
      `LOKI_URL` and the Loki helpers in `bridge/net.py` go too, if nothing else reads them
      by then. The same commit rewords the `kuma-push-lib.sh` and `kuma_push.cap_kuma_msg`
      comments in `bridge/common.py` and the row's comment in `check_table.py`. Its token
      retired in A6-9.
    - **What the reads at C-7's base changed.** `loki_count` stays, with `LOKI_URL`, because
      `checks/service.py`'s HA ban arm reads it, so only `loki_lines` goes. `host_uptime_s`,
      `SWALLOWED_VERDICTS_WINDOW_S` and `BOOT_SETTLE_S` had the row as their only reader, so
      they go rather than being reworded. The cap comment sits in `bridge/msgfmt.py`. Both
      modules, `checks/logs.py` and `verdicts/logs.py`, held the row alone and go whole. The
      pod producers' verdict lines keep their shape, and their comments stop naming the row.
    - **Its deploys.** `observability`, `monitor-bridge` and `loki-homelab`, each by its
      owner: `land.sh` while Ansible-owned, the merge once `owner: flux`. loki-homelab is
      flux-owned from D5's Wave A', and monitor-bridge may be by then too. The row's gauge
      and `homelab_check_rows` leave together, so `BridgeCheckStale` never reads a short
      count, in either deploy order, and `KumaNotificationDropped`'s deletion is order-free.

### The tree

```diff
-ansible/roles/k8s/uptime-kuma/                        the whole role (3,655 lines), C-1b and C-2b
 ansible/roles/k8s/monitor-bridge/files/
-  gates.py                                            A6-7r
~  bridge/streaks.py                                   apply_startup_grace and grace_streaks out (A6-7a); State, down_streak and the probe caches stay
~  check.py                                            gauges at each push point (A6-1); _evaluate and check_enabled in, gate rows run as rows (A6-7r); push branch out (A6-7a)
~  cli.py                                              exporter in (A6-1); gate filter helpers out, name check in (A6-7r); --dry-run and sink out (A6-7a)
~  registry.py, bridge/types.py                        gate rows and Check.gate in (A6-7r); token and push_env out (A6-7a)
~  bridge/net.py, bridge/sources.py, bridge/config.py  push, Sink, KUMA_URL and GRACE_CYCLES out (A6-7a); sources.py keeps State
~  defaults/main.yml                                   gates.py out of monitor_bridge_modules (A6-7r)
   checks/notify.py, service.py, host_edge.py, cloudflare_ips.py, healthchecks.py   unchanged: they read State, down_streak and the caches in bridge/streaks.py
~  checks/cluster.py                                   check_prometheus out (A6-7a)
~  check_table.py                                      22 rows out (A1 to A5, A7, B8); prometheus out (A6-7a); tile fields out (A6-8); swallowed_verdicts out (C-7)
~  verdicts/logs.py                                    KUMA_RETIRING_TAGS (B1 to B7 shadow PRs, B9's tag PR); out with swallowed_verdicts (C-7)
~  checks/*.py, verdicts/*.py                          the Prometheus-reading bodies out (A1 to A5, B8); the log arm out (A7)
 ansible/roles/k8s/uptime-kuma/templates/
~  static-monitors.yaml.j2                             CHECKS loop out (A6-7a); B tiles out (each Cutover-1a)
~  status-page-sync-configmap.yaml.j2                  bridge pins out (A6-7a)
 ansible/roles/k8s/observability/
+  files/rules/hosts.yaml, containers.yaml, scrape.yaml, edge.yaml, storage.yaml   A1 to A5
+  files/rules/bridge-exporter.yaml                    A6-2 to A6-6
~  files/rules/host-checks.yaml                        B1 to B6a, two rules per producer; KumaDown (B1, out at C-1a)
~  files/rules/scrape-targets.yaml                     the uptime-kuma rows and exclusions out (C-1a)
+  files/rules/pod-producers.yaml                      B6b
+  files/rules/pods.yaml, extra-probes.yaml            B7, B8
+  tests/rules/<each new group>.test.yaml
+  templates/alertmanager-ingressroute.yaml.j2         B-6b
~  templates/prometheus.yaml.j2                        probe jobs in (B8); uptime-kuma job out (C-1a)
~  templates/scrape-credentials-secret.yaml.j2         kuma_api_key out (C-1a)
~  templates/alertmanager-secret.yaml.j2               inhibit_rules for WAN and B2 (A6)
~  templates/kube-state-metrics.yaml.j2                configmaps, cronjobs (B6b; jobs was #5675); B7 reuses cronjobs
 ansible/roles/k8s/authelia/templates/config-secret.yaml.j2   alertmanager two_factor (B-6a); kuma bypass out (C-1b)
 ansible/roles/k8s/loki-homelab/files/rules/homelab-logs.yaml   WorkloadLogErrors (A7); cloudflare-ddns rule (B7); KumaNotificationDropped out (C-7)
 ansible/roles/k8s/netpol-baseline/defaults/main.yml   the uptime-kuma probe row out (C-1b)
 ansible/roles/setup/common/tasks/install_host_lib.yml   kuma_push.py out of the loop, absent task in (C-3a), absent task out (C-6)
 ansible/roles/k8s/blackbox-exporter/files/blackbox.yml   tcp_connect, dns, keyword modules (B8)
 ansible/roles/setup/optimize_pi/templates/node_exporter.service.j2   textfile collector on (B5)
 ansible/roles/setup/initial_setup/files/
-  kuma-push-lib.sh                                    C-3a
+  homelab-check-lib.sh                                B0
 ansible/roles/setup/common/
-  tasks/kuma_check_timer.yml                          C-3b, renamed
+  tasks/check_timer.yml                               C-3b
 hosts/lib/
-  kuma_push.py                                        C-3a
~  host_lib.py                                         the kuma_push re-export out (C-3a)
 hosts/components/
-  kuma-push/                                          C-3a, with its entry in all three package.yml lists
~  builder/component.yml, claude-code/component.yml    the lib: kuma_push.py rows out (C-3a); builder's /opt legacy entry out (C-6)
~  loki-route-witness/, secret-rotation/component.yml  the kuma-check-* legacy entries out (C-6)
~  claude-code/files/claude_triage_poll.py             the Kuma source out (B-10, from #6876)
 hosts/daniel-pi/package.yml                           the pi-kuma-push-lib.sh legacy entry out (C-6)
 scripts/renderer/renderer_lib/
~  host_sources.py, host_inputs.py                     the kuma-push-lib.sh rows out (C-3a)
 ansible/filter_plugins/
+  ingress_probes.py                                   C-4a
-  kuma_monitors.py                                    C-4b, after D6
 scripts/diagnostics/probe_lib/
+  health_alerts.py                                    B-1
~  health.py                                           run_health reads it (B-1)
~  monitors.py                                         the drift half out; monitors reads ALERTS (B-2)
~  alerts.py                                           history from ALERTS (B-2)
~  cli_parser.py, subcommands.py, pi_plane.py         kuma-drift wiring and its --pi out (B-2)
-  kuma_live.py                                        B-2
-  kuma_table_loop.py                                  B-2 (its CHECKS half at A6-7a)
~ scripts/diagnostics/postflight.py                    B-2; check_kuma_scrape out at C-1
~ scripts/docs/fragments_lib/fragments_bridge.py       gate sets (A6-7r), row fields (A6-8)
~ scripts/docs/catalog_lib/glance_facts.py            timer_units follows the rename (C-3b)
~ scripts/lib/ansible_jinja_env.py                    ingress_probe_targets in (C-4a), kuma filter out (C-4b)
~ scripts/secrets_mgmt/secret_rotation.py, rotation_tools.py   the audit push out (B2); malformed_push_tokens call out (C-5)
~ ansible/roles/k8s/homepage/templates/config/services.yaml.j2, custom.css.j2   B-4
- ansible/roles/k8s/observability/files/dashboards/Apps/uptime-kuma-monitors.json   B-5
~ ansible/roles/k8s/observability/files/dashboards/Infrastructure/alert-history.json   B-5
```

## Steps, as PRs

**Phase A** follows the batch table. Per batch: one shadow PR; Cutover-a and Cutover-b (A
batches, B8); or Cutover-1a, Cutover-1b and Cutover-2 (B1 to B7, and B9 after its tag PR); then the token PR a week
after the last cutover. A6 runs as A6-1 to A6-9, with the refactor A6-7r before A6-7a.
*The cutover order* above names each deploy.
Phase A is about 75 PRs, and A0 lands before any Kuma-side PR. Each shadow PR adds its
tiles' terms to the `kuma-cutover` group, and each shadow-dropping PR removes them. A PR
that removes the last term deletes the group, and a PR that adds a term to a missing group
re-creates it, with its test, in about 40 lines (*No-pager windows*).

**Recording a batch's results.** A soak read, a cutover stack, a no-pager bound or any other
result went under the batch's own heading in *Status by batch*. While M6 ran, each heading
was a separate file under `m6-status/`, so that a PR recording one batch merged beside
another batch's PR without a conflict. M6 is done, and #8164 folded the files into this page.

**Every token PR, and C-5, is one commit with four parts.** The operator removes the keys
from `ansible/vars/secrets.yml` with `sops` (**operator-only**). In the same commit the agent
deletes the keys' rows from `ansible/secret_rotation.yml`, adds the keys to
`scripts/secrets_mgmt/git_dates.py:RETIRED`, and stages the fragment the `regen-doc-fragments`
hook rewrites. `sync` does not delete a stale row, so a commit without the row deletion fails
the `secret-rotation-registry-sync` hook (*Baseline*). The PR body quotes three reads:
`uv run python scripts/secrets_mgmt/secret_rotation.py audit --check` exiting 0,
`grep -c '_push_token:' ansible/secret_rotation.yml` with the count it expects, and
`git diff --stat ansible/vars/secrets.yml`. Plain `git diff` on that file prints plaintext.

**Narrowing, for every PR that edits `ansible/inventory/` or `ansible/filter_plugins/`.**
Before merging, run the read-only
`uv run python scripts/deploy_tools/deploy_tags.py narrow origin/master HEAD` from the PR's
worktree, and quote its output in the PR body. It names the tags `land.sh` and the tick reach.
If it refuses, the PR is broad: `land.sh` reports `needs-manual-apply` and the tick records
`k8s_unapplied` lines, and the session stops under root `CLAUDE.md` *When to wait*. The PRs
this applies to, with what the rules predict:

- **B0** rewords two comments in `group_vars/all.yml`. Expected: no k8s tag, because no value
  changes. If the narrowing refuses, the rewording moves to C-6, which quotes it too.
- **A6-1** adds `port` and `metrics` to the monitor-bridge entry. Expected: `monitor-bridge`,
  `observability` (`scrape_jobs`) and `netpol-baseline` if the entry has callers.
- **B8** edits other entries' `netpol_from` lists. Expected: each edited entry's role, and
  `netpol-baseline`, which deploys last.
- **C-1** drops five fields from the uptime-kuma entry and edits netpol-baseline's probe
  table. Expected: `observability` (`entry_port`), `uptime-kuma`, `authelia`
  (`authelia_service_rules`) and `netpol-baseline`. Homepage's `homepage_href` reader is gone
  by then (B-4).
- **C-2b** removes the entry. Expected: every reader that iterates the list, possibly the
  fleet-coverage refusal. The entry renders nothing by then, so the operator may apply the
  recorded tags in a quiet window.
- **C-4a** renames the `kuma:` key to `probe:` on every entry that has one, 16 at
  `19dee151c`, and adds a plugin. Expected, as
  `deploy_tags.py narrow` named it for a scratch commit of the rename, the plugin, the probe
  job's switch and the `FILTER_FIELDS` row at `19dee151c`:
    - Each renamed entry's own tag, because `entry_change_tags` compares whole entries. At
      `19dee151c` those are authelia, bento-pdf, crowdsec, freshrss, headlamp, home-assistant,
      ical-proxy, jellyfin, livesync, loki-homelab, navidrome, observability, pihole,
      scrutiny, uptime-kuma and zigbee2mqtt.
    - The readers of the renamed field, as the line
      `containers_list readers of kuma,probe on 16 entries -> observability,uptime-kuma`.
      `observability` reads `kuma` before the edit, through the probe job's
      `kuma_ingress_monitors` call, and `probe` after it, through `ingress_probe_targets`.
      `uptime-kuma` reads `kuma` through `static-monitors.yaml.j2`, which C-1 deletes.
    - The added plugin's callers, as `ingress_probes.py -> observability`. The plugin reaches
      only the templates that call `ingress_probe_targets`. Committed with no caller, it
      narrows to `-> (nothing)`.

  *Phase C* does not order C-4a against C-2b, so uptime-kuma's place in the output depends on
  C-4a's base. After C-1 it is no longer a reader. Before C-2b its entry still carries the
  key, so its own tag stays and the rename touches 16 entries. After C-2b its entry and role
  are gone, so the rename touches 15 entries and uptime-kuma is in neither line.

  No template reads a whole entry since `8e79e95d6` (#4955), so no other reader is expected.
  The `FILTER_FIELDS` row keeps it that way. A template calling a filter with no row reads
  every field (`narrow_lib/containers.py:reader_reads`), so without the row the probe job
  would become a whole-entry reader again. C-4a's own output would not show it, because
  `observability` is in the set either way. Every later edit to any entry would then reach
  `observability`.
- **C-4b** deletes a plugin, which `narrow_lib/filters.py` always refuses, so it waits for D6.

**Phase B and Phase C:**

| PR | Scope | Size | Deploys |
|---|---|---|---|
| B-1 | `health_alerts.py`, `run_health`, the `owner`-based reference time, fixtures; merges after C7's Open question 4 amendment | ~220 | none (a host tool) |
| B-1d | the health-gate drill rule, its promtool case, its blackhole route and the routing case (the rule removed in B-1e); B-1d and B-1e land before D5's littlelink flux PR (Open question 12) | ~40 | `observability` |
| B-2 | `monitors` and `alerts` on `ALERTS`; postflight's two Kuma checks; delete `kuma-drift`, `kuma_live.py`, `kuma_table_loop.py`; `cli_parser.py`, `subcommands.py`, `pi_plane.py`; the three `kuma-drift` docs; `probe.py`'s `monitor_status` docstring | ~230 added, ~900 removed | none |
| B-4 | Homepage tile, CSS and test | ~80 | `homepage` |
| B-5 | Grafana boards | ~150 JSON | `observability` |
| B-6a | Authelia `two_factor` rule for `alertmanager.local` and its render test | ~30 | `authelia` |
| B-6b | Alertmanager IngressRoute | ~40 | `observability` |
| B-7 | docs, skills, agents, evals, the ADR draft (C-0) | ~200 | none |
| B-10 | triage's poller reads `ALERTS` alone and drops its `kuma:` keys; its docs; after Phase A | ~60 removed | a new version of the daniel-box and daniel-server host packages |
| C-1a | `KumaDown` with its inhibit, the scrape job and its credential, `check_kuma_scrape`, the two `scrape-targets.yaml` rows, `monitors.py`'s Kuma read; the operator's `cluster_vars.py write` if the check asks; after B-2, B-4 and B6c's token PR (a quoted `git grep -n registry_gc_push_token -- ansible/roles ':!*/tests/*'` returns nothing); the two post-promotion reads that `KumaDown` and the scrape are gone | ~60 removed | the promotion (`observability`) |
| C-1b | empty the uptime-kuma render down to its claims: the seven templates, the declaration task and the tests that render them, the include's secret list and rollout; the Authelia bypass; the entry's routed fields and the probe row; the probe-row named-member case; `Removes-Object:` lines; the operator's `cluster_vars.py write`; a quoted `homelab-render cluster --role uptime-kuma netpol-baseline`; after C-1a's reads | ~3,050 removed, ~40 added | the promotion (`authelia`, `blackbox-exporter`, `pihole`, `uptime-kuma`, then `netpol-baseline`) |
| C-2a | the bespoke grants | ~55 removed | the edited roles, then `netpol-baseline` |
| C-2b | the role directory, the entry and `k8s_host_files/tasks/uptime-kuma.yml`, after C-1b's prune reads `NotFound`; then the **operator-only** delete of the claims and any Secret left | ~100 removed | the promotion |
| C-3a | `kuma-push-lib.sh` (an absent task on every host), `kuma_push.py`, the re-export, the `install_host_lib.yml` loop and an absent task for the `/opt` copies, the nine stamp pairs, the cross-role table rows; the `kuma-push` component, the `hosts/` copies of `kuma_push.py` and the renderer's two `kuma-push-lib.sh` rows; after B7's autofix-bridge Cutover-2, after B9's Cutover-2 on all three hosts and after C-2b | ~500 removed | a new version of all three host packages; the tick (initial_setup, gitops_deploy, renovate_agent, renovate_notify, fake_remux, deploy_ui on daniel-box); `promote` for the `configarr` and `janitorr` ConfigMaps; by hand on daniel-server and the Pi; `k3s --tags "backup-health,live-drift"` by hand |
| C-3b | the task-file and template rename; the variable rename; `glance_facts.py:timer_units` and its named-member case; the `hosts/` comments that cite the task file (*Every `hosts/` file the grep finds, by PR*) | ~300 changed | the tick; `k3s --tags "manifest-prune,live-drift"` by hand; a new version of the daniel-box and daniel-server host packages |
| C-4a | the key rename, `ingress_probes.py` and its `ansible_jinja_env.py` registration | ~160 | per the narrowing |
| C-4b | delete `kuma_monitors.py`, its `ansible_jinja_env.py` import and registration, and its `FILTER_FIELDS` row, after D6 | ~70 removed | none once D6 has landed |
| C-5 | secrets (**operator-only** half first) | ~100 | none |
| C-6 | the census row, after the quoted identifier `git grep`; C-3a's two absent tasks out; the host packages' Kuma `legacy:` entries out; the `hosts/` history comments (*Every `hosts/` file the grep finds, by PR*); after C-7 | ~100 | a new version of all three host packages; the tick (initial_setup and the `install_host_lib.yml` importers); `land.sh` for `configarr` and `janitorr` only while they import `install_host_lib.yml`; `k3s --tags "backup-health,live-drift"` by hand |
| C-7 | M5's `KumaNotificationDropped`; the `swallowed_verdicts` row with its bodies, rule, tests and `KUMA_RETIRING_TAGS` | ~350 removed | `observability`, `monitor-bridge`, `loki-homelab` |

B-2 and the old B-3 (postflight) are one PR, because `postflight.py` imports the modules B-2
deletes. Reverting B-2 restores both together.

**Two-writer check.** For every Phase B and C PR, no check changes its pager: Phase A moved
every pager already. The C-1 gate (exactly one `monitor_status` series and one declared
monitor, both the status-page sync tile) refuses a C-1 that would remove a live pager, whatever
the pager's monitor type.

## Verification

```bash
# Phase A, per batch: the shadow soak (M1 convention 6, with the batch's tile names), then
uv run python scripts/diagnostics/probe.py metric 'count by (monitor_type) (monitor_status)'
# Once A6-1 lands, every row of CHECKS reports, and none is stale
uv run python scripts/diagnostics/probe.py metric 'time() - homelab_check_last_run_timestamp_seconds > homelab_check_deadline_seconds'
uv run python scripts/diagnostics/probe.py metric 'count by (job) (homelab_check_success)'
# Once A6-1 lands, and again after A6-7r: both gate rows export, and the difference is 0
uv run python scripts/diagnostics/probe.py metric 'homelab_check_success{job="monitor-bridge",check=~"wan_reachable|b2_reachable"}'
uv run python scripts/diagnostics/probe.py metric 'count(homelab_check_success{job="monitor-bridge"}) - on() homelab_check_rows{job="monitor-bridge"}'
# Before C-1a, both halves of the gate: exactly one series, the status-page sync tile
uv run python scripts/diagnostics/probe.py metric 'count by (monitor_type, monitor_name) (monitor_status)'
# and only status-page-sync.json declared at the commit prod holds, which this names
kubectl -n flux-system get ocirepository homelab -o 'jsonpath={.status.artifact.metadata.org\.opencontainers\.image\.revision}'
git show <commit>:ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2 | grep -E '\.json: \|$' | grep -vE '^ +(discord|email|tag-[a-z]+)\.json:'
# Phase B: the health gate reads the alert plane
uv run python scripts/diagnostics/probe.py health observability
uv run python scripts/diagnostics/probe.py monitors
# Phase C, after C-2b and the operator's delete: Kuma is gone
uv run python scripts/diagnostics/probe.py metric 'up{job="uptime-kuma"}'
kubectl get svc,ingressroute,cm,cronjob,deploy -n homelab -l homelab/role=uptime-kuma
grep -c '_push_token:' ansible/secret_rotation.yml
grep -c '_push_token:' ansible/vars/secrets.yml
uv run python scripts/secrets_mgmt/secret_rotation.py audit --check
```

**The health-gate drill (B-1d and B-1e, live).** It runs after B-1 lands. It is C8's
**operator-only** production drill, because C8's fixture-tag substitute cannot exercise this
gate. `probe.py health <tag>` renders the tag's role to enumerate its workloads
(`land_lib/tools.py`'s note on the detached worktree says so), and
`health.py:_deploy_applied_at` needs the tag's release record. A scratch tag has neither.

- **Blast radius.** One rule and one Alertmanager route in `observability`, and one
  exemption in `scripts/validate/alert_conventions.py`, which otherwise refuses a constant
  expression on any alert but the `Watchdog`. The rule's alert routes to the blackhole
  receiver, so Alertmanager pages nobody, and littlelink keeps serving throughout. B-1e is the
  rollback.
- **What else fails while the rule is live.** Every `probe.py health littlelink` fails.
  `deploy.sh --tags littlelink` posts that verdict to the gitops-deploy Discord channel
  (`scripts/deploy_tools/deploy_lib/detach_run.py:notify`). A `land.sh` landing whose tags
  include littlelink reads unhealthy, and so does a broad landing that gates it. The
  deployer's own gate reads no alerts
  (`ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_io.py:deploy_k8s`), so a
  deployer-applied littlelink image-pin bump deploys green and sets no `hold_sha`. That bump
  still moves `applied_at` and voids the drill. Renovate's k8s digest rule automerges
  littlelink's GHCR `:latest@sha256` pin on its first run, with no soak.
- **Mitigation.** No other landing runs between B-1d's `observability` deploy and B-1e's, and
  littlelink has no open Renovate PR when B-1d lands.
- **Preconditions, quoted in B-1d and again in B-1e.**
    - littlelink's `containers_list` entry is not `owner: flux`. D5's soak reads every
      flux-owned service's critical alerts, so the drill alert on a flux-owned tag would fail
      any promote whose soak spans it. Open question 4 explains why the soak cannot exclude
      `drill="true"` alone.
    - littlelink's `applied_at` from its release record, read before B-1d merges.
- **Why the drill stays reachable.** D5 makes every role flux-owned (design.md's D5 row),
  and its wave A includes littlelink. Once D5 flips its last role, no tag meets the first
  precondition, so the drill must run before then. A sub-edge in the slice index orders it:
  D5's flux PR for littlelink waits for M6's B-1e (Open question 12). D5 may flip the rest of
  wave A meanwhile, because the drill reads no other tag. B-1, B-1d and B-1e need no Phase A
  batch and none of M2 to M5, so Open question 12 also asks the index to let them start after
  M1. The edge then holds littlelink's flip only until B-1e. The alternative, C8's fixture
  tag in a scratch namespace, was rejected. `probe.py health` renders the tag's role, and a
  scratch tag has no role. It would also need the soak to exclude a `drill` label, which
  Open question 4 rules out.

1. B-1d adds a rule `HealthGateDrill` (`expr: vector(1)`, `severity: critical`,
   `drill: "true"`, `service: littlelink`) with a promtool case that it fires with those
   labels. In the same PR, an Alertmanager route sends `drill="true"` to the blackhole
   receiver, ahead of the critical route. A routing case in M1's routing test proves that a
   critical `drill="true"` alert reaches the blackhole receiver, so nothing pages. That test
   walks the rendered `alertmanager.yml` in Python rather than running amtool. B-1d also
   exempts the rule from `alert_conventions.py`'s constant-expression check, which admits only
   an alert named `HealthGateDrill` that carries `drill="true"`, with a red proof for each
   half. The rule becomes active after littlelink's last `applied_at`, so it gates.
2. After the `observability` deploy, run `probe.py health littlelink`. It must exit non-zero
   and name `HealthGateDrill`. Read littlelink's `applied_at` again. Quote both in B-1e.
3. B-1e removes the rule file, its `observability_rule_groups` entry, its promtool case and
   the validator exemption with its tests. It keeps the route and its routing case. Run
   `probe.py health littlelink` again; it exits 0, because the gate reads only alerts firing
   now. Read littlelink's `applied_at` a third time.

If littlelink's `applied_at` moved at any of the three reads, a deploy reached it during the
window, for example a deployer-applied image-pin bump. The drill is then void and runs again.

B-1d's own landing gates `observability`, not `littlelink`, so it reads healthy. The
`probe_success` half is proved by fixtures, because a failing probe target would page through
M4's rules.

**Red proofs for the new checks:**

- Every rule: its promtool firing case, as in M2 to M5.
- `BridgeCheckStale` and `AutofixBridgeStale`: a stale-timestamp case and a missing-row case,
  each of which must fire after 10m. A single missed scrape, and a restart's first cycle that
  has set only some rows for under 10m, must not fire. `MonitorBridgeDown` and
  `AutofixBridgeDown` get the same two cases.
- The exporter test (A6-1): `homelab_check_rows` equals the count of enabled `CHECKS` rows,
  gate rows included. It is red on an exporter that sets the gauge from
  `registry.build_checks`, which drops the gate rows.
- The run-loop export test (A6-1, kept through A6-7r and A6-7a): one `run_once` cycle in
  which each of `wan_reachable` and `b2_reachable` exports `homelab_check_success`. A
  dependent skipped by a down gate exports 0 and its body is not called. A raising body
  exports 0 and the next row still runs. Until A6-7a, the test also asserts that the push
  keeps today's skip and grace values.
- `KUMA_RETIRING_TAGS` (each B1 to B7 shadow PR, and B9's tag PR): a `rejected` line from a listed tag returns
  up and from an unlisted tag returns down; a lost `status=down` line from a listed tag still
  pages; a listed tag that names no rendered script fails.
- The probe-row case (C-1): a portless row naming a portless entry fails, and so does a
  portless row naming no entry.
- B0: the library's copy task without `always` fails, and so does a rendered script that
  sources the library without `|| exit 1`.
- `KumaDown`: a case for `up == 0` and one for no series.
- A3's scrape-target rule: a firing case for each of `alertmanager`, `blackbox-ingress` and
  `loki-homelab`, the first asserting `service: observability`. A selector that drops any of
  the three fails its case.
- A3's inhibit: the config test fails on an inhibit whose target matchers miss
  `job="blackbox-ingress"`, or whose `equal` list names a label `BlackboxExporterDown` lacks.
- The other inhibits, in the same config test (A3 task 6):
    - `KumaDown`, `MonitorBridgeDown` and `AutofixBridgeDown`: a fixture whose target
      matchers name the wrong `job` fails, and so does one whose source names an alert no rule
      file defines.
    - A1: a fixture whose `equal` list names a label absent from the expected labels of the
      `ScrapeTargetDown` promtool case for `job="node"` fails. So does a fixture whose source
      is the undefined `NodeExporterDown`, and one whose missing-data target arm has a `for:`
      under 10m.
    - Any inhibit: a fixture whose source rule carries `shadow` while its target rule does not
      fails.
- A3's Cutover-b: M2's routing pytest expects `muted` for the `BlackboxExporterDown` set, and
  the same pytest on the commit without the outcome edit fails.
- `PiPortDead`: a dead-port case; it becomes failure-classes row 6's detector.
- The row-to-rule bijection test: a row with no rule must fail it.
- `health_alerts.py`, as fixtures in `scripts/diagnostics/tests/`:
    - must fail: a critical alert active after `applied_at` that still fires on the re-read; a
      `warning` alert labelled `verify="true"` that became active after `applied_at`; a
      `probe_success` of 0 sampled after the rollout completed; a probe series with no sample
      newer than the rollout's completion after the wait;
    - must pass: a firing Watchdog; a critical alert active before `applied_at`; a shadow
      alert; a `drill="true"` alert on another tag; a `warning` alert without `verify`; a 0
      sampled after the apply but before the rollout completed, followed by a 1; a tag with no probe series at all; a tag
      whose rendered entry has no route while its old probe series is inside the lookback.
    - a flux-owned tag whose release record is a month old: a critical alert active before the
      Kustomization reported its current revision must pass, and one active after it must
      fail;
    - an alert whose activation equals Prometheus's start time, after the tag's reference
      time: must fail, and the message must say the alert's age is unknown since Prometheus
      started.
- `HealthGateDrill`: its promtool firing case, and the amtool case that routes a critical
  `drill="true"` alert to the blackhole receiver.
- `WorkloadLogErrors` (A7): M5's red proof, as *A7* lists.
- Every token PR: `secret_rotation.py audit --check` exits 0 with the rows deleted. It exits
  non-zero on the same commit without the row deletion.
- The census row: its red fixture, a file outside the allowlist that mentions Kuma.
- B0's `write_check_textfile`: the M3 script test pattern, run against the shared library.
- B5: the unit-flag-and-directory test, red on a unit that keeps `--no-collector.textfile`. It exists since H3.6
  (`test_pi_node_exporter_host_unit.py`). B5's own test is `test_pi_heartbeat_textfiles.py`: each script writes its
  textfile before the push, a restarted container reads success 0, and the rules' deadline literals equal `DEADLINE_S`.
- The B-6a render test: `alertmanager.local.<domain>` resolves to `two_factor` at first
  match, and it fails if the rule sits behind the `*.local` wildcard.
- C-3b: the glance cases that fail when `timer_units` finds no `kuma-check-*` timer in the
  k3s role, and that read an import of `check_timer.yml`.
- A0: the named-member floor refuses an empty index and accepts the five survivors, and
  A0's PR quotes a green `--dry-run --tags uptime-kuma` on the real render.
- C-1: the dry run's prune-guard task passes on the emptied role, and the probe Job renders
  without the uptime-kuma row, quoted in the PR. The readiness task skips in a dry run, so
  that quote proves the render only.
- `CheckWithoutPager`: promtool cases in `kuma-cutover.test.yaml`. A listed tile absent for
  2 hours with `up{job="uptime-kuma"}` at 1 fires, naming the tile. The same case with `up`
  at 0 does not fire, and a listed tile that is present does not fire. The empty-input case
  fires.
- The size cap: the cap test with its cap set one line below the measured sum fails.

## Rollback

- **A Phase A batch.** M2's rules for PR 4a and PR 4b apply to every split:
    - Revert the shadow-dropping PR (Cutover-b or Cutover-1b) first. Its revert deploys
      `observability` (`loki-homelab` for A7) and restores `shadow`. The checks then have no
      pager until the Kuma-side PR is reverted too.
    - Revert the Kuma-side PR (Cutover-a or Cutover-1a, after Cutover-2) only while the
      shadow-dropping PR is unmerged or already reverted and deployed. Reverting it while the
      live rules run restores a second pager. One revert commit of both is also safe. The tile
      returns, with empty history, under its old AutoKuma id.
    - This works only while the batch's token PR has not landed, so each token PR waits a week.
- **A6 specifically.** Reverting A6-7a restores the push path, `apply_startup_grace`, the
  `prometheus` row and the `CHECKS` loop in `static-monitors.yaml.j2`, after A6-7b has been
  reverted. A6-8 must be reverted first if it has landed, because the loop reads the fields
  A6-8 deletes. Every other A batch must still have its token, or reverting A6 restores rows
  that push into missing tokens.
    - A6-7r stays when A6-7a is reverted. It changes neither the push nor the export, and
      A6-7a's revert restores code written against A6-7r's run loop.
    - Revert A6-7r alone only while A6-7a is unmerged. Its revert restores `gates.py` and
      changes no pager. After A6-7a, revert A6-7a first.
- **A3 specifically.** Reverting Cutover-b restores `shadow` on A3's rules and on
  `BlackboxExporterDown`, restores `LokiHomelabDown` in shadow, removes every inhibit it
  added and restores the routing pytest's `blackhole` outcome, all in one commit. An inhibit
  that a later PR added on `ScrapeTargetDown` (A1's, or `KumaDown`'s from B1's Cutover-1b)
  is removed in the same revert, or the config test fails on a shadow source over a live
  target. The order above then
  applies: revert Cutover-a next to restore the tiles as the pager.
- **A token PR.** Reverting it restores the rows in `ansible/secret_rotation.yml` and the
  `RETIRED` entries, but not the secret values. The operator re-adds them with `sops`.
- **B1's `uptime-kuma-alive`.** Reverting Cutover-2 restores the ping. The operator recreates
  the console check if step 3 has run.
- **Phase B.** Each PR reverts on its own, except B-2, which carries its postflight change.
  Reverting B-1 restores `probe.py health` without the alert read. B-1 changes the verdict of
  every caller of `probe.py health`, and they all go back to the rollout rollup alone:
  `deploy_lib/detach_notify.py:gate` and its `check_one`, which `deploy_lib/detach_run.py` runs
  as the deploy notifier after a detached deploy; `land_lib/tools.py`, which imports `gate` as
  `land.sh`'s health gate; and `land_lib/health_verdict.py`, which reads that gate's result.
  Reverting B-6 reverts B-6b first, then B-6a, so the route never sits under the wildcard
  tier.
- **Phase C.**
    - C-1 is reversible only until the operator deletes the two claims after C-2b, and Kuma's
      history is gone either way. Revert C-1b before C-1a, so `KumaDown` returns only once
      Kuma serves again. A revert of C-1b restores the probe row with the entry's `port` in one
      commit, so the probe Job renders again, and it restores the cluster-file keys only
      after the operator's `cluster_vars.py write`. Its Deployment re-attaches the two claims,
      which C-1b kept. A revert of C-1a restores `KumaDown` with the scrape job.
    - After C-5, restoring Kuma means new tokens: a rebuild, not a revert. C-1 therefore waits
      until Phase A has run a week with no Kuma page.
    - Reverting C-3a restores both libraries, the loop entry and the stamp pairs. Each
      consumer copies `kuma_push.py` again on its next apply, through the same planes C-3a
      names. Nothing pushes through it by then, because every Cutover-2 has applied.
    - Reverting C-3b restores the task file and the variable names. The units keep their
      names either way, so nothing is left running beside them.
- **A0.** Revert it only while no Kuma-side PR has landed. After the first one, the restored
  numeric floor can refuse every `uptime-kuma` deploy.
- **A partial C-1.** Between C-1a and C-1b, Kuma runs unwatched and unscraped. Nothing pages
  from it, because every pager moved in Phase A, and the status-page sync is the one tile
  left. If C-1b's promotion fails, `promote` holds `prod` and the hold names the failed
  Kustomization. A failed `uptime-kuma` reconcile leaves Kuma serving under its old objects.
  Fix forward, or revert C-1b. If `authelia` applied and `uptime-kuma` did not, Kuma's route
  sits under the `*.local` wildcard tier, which is stricter than the deleted bypass, and
  Kuma still pages. `netpol-baseline` waits on `uptime-kuma`, so the callers policy stays
  while Kuma's CronJobs exist.

## Risks

- **Class 4, a selector drifting from the hazard.** About 75 rules arrive across 15 batches.
  Mitigation: the row-to-rule bijection, the target-count tests from M4 and B8, the label
  census from M5, and the census row that holds `kuma`.
- **Class 3, empty read as clean.** A dead exporter or producer leaves no series, and a hung
  exporter loop leaves stale ones. Mitigation: `MonitorBridgeDown`, `BridgeCheckStale`,
  `AutofixBridgeStale`, `BlackboxExporterDown`, every `NotReporting` rule's `absent()` arm,
  the health gate's refusal of a missing fresh probe sample, and the Watchdog.
- **Class 2, a proxy for the property.** `KumaDown` proves Kuma serves `/metrics`, which is
  weaker than the accepted push `uptime-kuma-alive` proved. A Kuma that serves metrics while
  refusing pushes is not caught by `KumaDown`. Mitigation: `swallowed_verdicts` pages a
  `rejected` push from any producer outside `KUMA_RETIRING_TAGS`, through Kuma until A6-7b and
  through Alertmanager after it. Phase A shrinks what Kuma still carries every week. Residual:
  from Cutover-1a to Cutover-2, a producer's push lost on the network (no `by=kuma`) with
  `status=down` still pages through `swallowed_verdicts`, beside the batch's live rule. For
  an `HC_ROUTED_TAGS` producer the same lost push can page from Healthchecks.io too, from its
  Cutover-2's `monitor-bridge` deploy until its push stops and the 3h window passes. For B1
  either case needs a network fault during a DOWN verdict inside a same-day window. B5 needs
  no network fault: the Pi's refused push carries no `by=kuma`, so from `monitor-bridge`'s
  apply of B5's Cutover-2 until the Pi installs its package, any Pi DOWN cycle pages
  Swallowed Push Verdicts beside the live rule (*The cutover order*).
- **Class 5, a sequence treated as a transaction.** Phase C deletes in an order that the
  prune, the claims and the grants each constrain. Mitigation: the C-1a gate, the split
  between C-1a and C-1b with a live read between them, D2's two-PR retirement, and
  `netpol-baseline`'s `dependsOn` edge to `uptime-kuma`.
- **Class 1, fixes get no adversarial pass.** Phase A is about 75 PRs of similar shape, which
  invites copy-paste rules. Mitigation: each shadow PR's soak gate is a measured query, not a
  review, and the bijection test catches a missing rule.
- **The no-pager window between each Kuma-side PR and its shadow-dropping PR.** An outage that
  starts in it pages only once the shadow-dropping PR deploys and the rule's `for:` elapses.
  A6-7a opens the widest one, over 16 checks and two gates. Mitigation: M2's, applied to every
  split. The shadow-dropping PR is open and green before the Kuma-side PR merges, and the
  two-hour bound reverts the Kuma-side PR if it cannot follow. *No-pager windows* adds three
  rules: one window at a time, `CheckWithoutPager` paging a window older than two hours,
  and A6-7a in a quiet window. The cost is that about 15 cutover windows run in sequence,
  while the soaks still overlap.
- **A6-7r rewrites the run loop of a live pager.** A slip in it can fail in three ways.
  Lost suppression pages a WAN outage on every WAN tile. A lost push lets a tile go DOWN at
  its interval. A dropped gate row stops exporting its series (class 3), so `WANUnreachable`
  or `B2Unreachable` reads nothing and its inhibit never fires. Mitigation: the run-loop
  export test keeps its gate-row and skip assertions through A6-7r and A6-7a. The rewritten
  `run_once` cases assert the pushed value for each case, and the PR quotes the
  `monitor_status` and row-count reads after its deploy. Each of the first two failures is
  loud, because Kuma still pages.
- **The probe caches pace calls that cost money.** `checks/b2.py:check_b2_reachable` holds a
  billed B2 answer for `B2_PROBE_INTERVAL_S` (1800s). That is six 300s cycles, so losing
  `bridge.streaks.State` would multiply the gate's B2 calls by six. The R2, SMTP, Cloudflare
  and Healthchecks.io probes are cached the same way. Mitigation: `State` and its caches stay
  in `bridge/streaks.py`, and `bridge/sources.py` keeps building one per process. The cache
  cases in `test_check_b2_gate.py` keep running unchanged. Those are
  `test_b2_reachable_caches_failure_and_does_not_reprobe` and
  `test_b2_reachable_reprobes_after_the_interval`. The dependent skip keeps `b2_storage` from
  listing while B2 reads down (*Gates and grace*).
- **An inhibit that loses the race pages a dependent once.** The WAN and B2 gate rows and
  their dependents go to 0 in the same scrape. Mitigation: neither side carries a `for:`, both
  sit in one rule group, and M1's root `group_wait: 30s` lets the inhibitor see the source
  first. The A6-3 and A6-4 soaks cover a real WAN or B2 outage if one occurs. A missed race
  costs one extra page, which is the safe direction. Inhibition acts only in Alertmanager, so
  the skipped rows' alerts still appear in Prometheus `ALERTS`, and a Phase B health gate run
  during the outage reads them. A1's `ScrapeTargetDown` inhibit behaves the same way.
- **A7's alert is invisible to the health gate.** `WorkloadLogErrors` is a loki-homelab rule,
  so it never reaches Prometheus `ALERTS` (M5 open question 1). Mitigation: it pages through
  Alertmanager like any rule. Its reach into `probe.py health` waits on M5's open question.
- **A field drop breaks a reader that goes through an indirection.**
  `netpol-probe-job.yaml.j2` reads uptime-kuma's port through `entry_port(t.service)`, which
  no identifier grep finds. Mitigation: C-1's `-w uptime-kuma` precondition and the probe-row
  named-member case.
- **A deleted shared file breaks every role that copies or stamps it.** `kuma_push.py` has
  nine stamp pairs, one loop entry, one pod staging entry and two `lib:` rows in the host
  packages. Mitigation: C-3a's quoted `git grep -l kuma_push.py` precondition, which covers
  `hosts/`, and its deploy list.
- **The token registry blocks a retirement commit.** `sync` keeps stale rows, so a retirement
  that only adds `RETIRED` fails prek and CI. Mitigation: the four-part token commit in
  *Steps*, with `audit --check` quoted.
- **Class 6, restart is not recreate.** Its one detector is `pi_pressure`'s port arm. B8
  replaces it with `PiPortDead`, and `docs/failure-classes.md` row 6 moves to that rule's
  firing case in the same PR, so `ansible/tests/repo/test_failure_class_detectors.py` keeps
  resolving.
- **The renderer refuses an emptied role.** C-1b empties a flux-owned role, and the renderer
  refuses a restart target that names no workload in the render. Mitigation: the include's
  `manifests_rollout: ''`, and the `homelab-render` run C-1b quotes.
  `test_step_1_of_a_flux_owned_retirement_renders_no_object` renders the same step on
  bento-pdf. A0 removed the numeric declaration floor that Phase A's shrinking index would
  cross.
- **The tick or the toposort reorders a cutover.** The tick applies a setup role at
  fast-forward, before `land.sh` deploys a k8s role, and the k8s play deploys `observability`
  before `uptime-kuma`. Mitigation: each cutover PR changes one pager side only, so no order
  of applies, and no other session's deploy from master's tip, leaves a check with two pagers.
- **Concurrent edits.** monitor-bridge had 250 commits in 90 days (design.md's census). Other
  sessions will edit rows while their batch is in flight. Mitigation: claim each batch's rows
  with `findings.py claim` before the shadow PR, and keep batches to one family.
- **The off-site watcher changes hands.** From B1's Cutover-2, Alertmanager's Watchdog is the
  only off-site watcher of the alert path, and `KumaDown` watches Kuma from inside it.
  Mitigation: M1's drill proved the Watchdog, and B1's Cutover-2 re-runs that drill.
- **The live drill needs an Ansible-owned tag, and D5 removes them all.** Mitigation: the
  index sub-edge that holds D5's littlelink flux PR until B-1e (Open question 12). Residual:
  D5's slice end then waits on B-1e. B-1 needs only M1 and C7's amendment, so the wait is
  short if the index lets B-1 start early. The index does not yet: its M6 row and the
  register's split row keep B-1 behind M2 to M5 and A6, and its D5 row names the last
  stateless leaf rather than littlelink (*Order*). Until contracts.md applies Open question
  12's corrections, the operator chooses between holding littlelink and losing exit
  criterion 4's live half.
- **A cutover that hands work to another slice drops it.** M1, M4 and M5 each leave A3 a
  shadow drop, a deletion, a routing outcome or an inhibit. Mitigation: *A3 and the handoffs
  it closes* lists all five, and each has a red case in *Verification*. The routing pytest
  fails on an unshadowed `BlackboxExporterDown` whose outcome is not edited.
- **Registry GC pushes to Kuma until B6c's Cutover-2.** C-1a's gate of one
  `monitor_status` series refuses a C-1 that would delete that live tile. Mitigation: C-1a
  waits for B6c's token PR and quotes the `registry_gc_push_token` grep. If B6c stalls, Kuma
  stays up with one push tile and the status-page sync.
- **D5 flips an M6 target role mid-slice** (class 5). A flip moves the role's apply from
  `land.sh` to Flux, and three M6 steps relied on the toposort inside one `land.sh` run.
  Mitigation: *When D5 flips a role M6 edits* names the three. An index sub-edge keeps
  monitor-bridge Ansible-owned until A6-7a has deployed. The operator's 2026-10-21 ruling
  (#5821) flipped uptime-kuma, observability and netpol-baseline before C-1, so C-1 splits
  into C-1a and C-1b, and netpol-baseline's `dependsOn` edge orders the rest of C-1b.
  Every other M6 PR changes one pager side, so either owner can apply it. Residual: D5 runs
  Wave D in the order of its wave table, so holding monitor-bridge holds every role listed
  after it until A6-7a. If the index declines the edge, the fallback in that section costs
  one extra PR per remaining bridge-row Cutover-a.
- **Homepage loses a familiar tile.** Mitigation: the replacement tile shows the same three
  counts in the same grid slot. The *Homepage* `CLAUDE.md` note on positional block labels
  applies.

## Tests and docs that change in the same PR

Each PR that edits a cited path also edits the `CLAUDE.md` section citing it. A PR that changes a role's tasks,
timers or entry regenerates its glance block with `scripts/docs/gen_role_glance.py`.

- **Each Phase A batch.**
    - The shadow PR: its rule tests.
    - Cutover-a (A) or Cutover-1a (B): deletes `test_kuma_static_monitors.py`'s expectations
      for its tiles, and its rows in `docs/monitor-bridge-checks.md`.
    - Cutover-b (A) or Cutover-1b (B): updates the promtool cases that assert `shadow`.
    - Cutover-a (A) or Cutover-2 (B): deletes the producer's tests for the push.
    - The token PR: `ansible/secret_rotation.yml`, `RETIRED`, and the regenerated rotation
      fragment.
    - A host batch's Cutover-2: updates `test_kuma_check_timer.py`'s `KNOWN_CHECKS` and
      `test_kuma_env_renders_for_every_cron_tag.py`.
- **A0.** The red/green test of the named-member floor, in `ansible/tests/services/` beside
  `test_kuma_push_token_format.py`.
- **B2's secret-rotation Cutover-2.** `scripts/secrets_mgmt/tests/test_secret_rotation.py`
  and `test_rotation_tools.py` lose their push cases.
- **A1 to A5.** The monitor-bridge `CLAUDE.md` citations of `test_push_monitors_never_retry`
  and `test_every_node_exporter_job_is_mapped_in_exporter_dependent` go with the last row
  they cover.
- **A3.** The shadow PR: the scrape-target rule's promtool cases for the three named jobs.
  Cutover-b: `BlackboxExporterDown`'s promtool cases lose their `shadow` assertion, M2's
  routing pytest changes that set's outcome to `muted`, `LokiHomelabDown`'s rule, promtool
  cases and routing case are deleted, and the inhibit gains its config test beside the
  routing test. The same test gains the cases for task 6's inhibits in the PR that lands
  each, A1's included, and loses `KumaDown`'s in C-1.
- **A7.** The shadow PR: the census entry in `test_loki_stream_labels.py` and the moved
  `DECIDED stays at 20` marker. Cutover-a: `test_check_loki.py`'s `workload_log_errors` test
  and the `LOG_ERROR_*` cases go with their subject, and `docs/monitor-bridge-checks.md` and
  `docs/monitor-bridge-internals.md` lose the row.
- **A6-7a.** `scripts/docs/tests/test_fragments_bridge.py`,
  `ansible/roles/k8s/uptime-kuma/tests/test_status_page_groups.py`, the row-needs-a-tile check
  in `test_kuma_static_monitors.py`, the `CHECKS`-loop fixtures in
  `scripts/diagnostics/tests/test_probe_monitors.py`, and the bijection test. The monitor-bridge
  `CLAUDE.md` (*Gates and hysteresis*, `bridge/streaks.py`) and
  `docs/monitor-bridge-internals.md` are rewritten for the exporter. The `bridge-gate-sets`
  fragment regenerates.
- **A6-7a, the readers.** The registry tests, such as `test_check_cli.py`, build `Check`
  without a token, and the config tests drop `KUMA_URL` and `GRACE_CYCLES`.
  `test_check_streaks.py` loses its `apply_startup_grace` cases and keeps its `down_streak`
  cases. The run-loop export test drops its push assertions.
- **A6-7r.** *The run loop after `gates.py`* lists its tests. The monitor-bridge `CLAUDE.md`
  *Gates and hysteresis* section, its *Module layout* sentence on `gates.py`, and
  `docs/monitor-bridge-internals.md` drop `gates.py` in the same PR. `docs/monitor-bridge-checks.md`
  loses its `gates.py` references. The `bridge-gate-sets` fragment regenerates.
- **A6-8.** `ansible/roles/k8s/monitor-bridge/tests/test_check_table.py` and
  `test_fragments_bridge.py`.
- **B0.** A test of the shared library, in the existing setup tests, and the
  `always`-tagged copy task and stamp in `initial_setup/tasks/crons.yml` that install it, with
  the test that finds the copy task by name (*B0, B5, B7 and B8*).
- **B1 Cutover-2.** The five `uptime-kuma-alive` tests, `docs/healthchecks-io-deadman.md`, and
  the regenerated `deadman-cadences` and `deadman-graces` fragments. `longhorn-backup-health`
  leaves `HC_ROUTED_TAGS`, which keeps
  `test_hc_routed_tags_are_exactly_the_scripts_that_read_kuma_push_ok` green.
- **B5 Cutover-2.** `pi-sd-health` and `pi-recovery-health` leave `HC_ROUTED_TAGS` for the same
  test. The order against the Pi's package install is not free: a Pi DOWN cycle in between
  pages Swallowed Push Verdicts (*The cutover order*), so the PR lands while both Pi checks
  read `status=up`.
- **B5.** Nothing beyond the shadow PR. The unit-flag-and-directory test and the `optimize_pi` `CLAUDE.md`
  line the first draft named are obsolete: H3.6 wrote the test, and H3.8b moved the heartbeats and their
  contract into `hosts/components/pi-heartbeats/component.yml`.
- **B1 to B7, each shadow PR, and B9's tag PR.** The batch's `KUMA_RETIRING_TAGS` entries and their cases in
  `ansible/roles/k8s/monitor-bridge/tests/test_check_swallowed_verdicts.py`.
- **A6-1 and A6-2.** The exporter test for `homelab_check_rows`, the run-loop export test,
  and the promtool cases for the two exporter rules, restart cases included.
- **A6-3 to A6-6.** The rule-shape test that derives each A6 rule's `requires` label, its rule
  group and its `for:` from the row's `gate` column.
- **B7.** The census entry in `test_loki_stream_labels.py` for the cloudflare-ddns selector.
  autofix-bridge's Cutover-2 drops the `kuma_push.py` staging entry and its `CLAUDE.md` line.
- **B8.** `test_check_pi.py`'s port cases go with `with_pi_ports`. `docs/failure-classes.md`
  row 6 cites `PiPortDead`'s firing case in the `observability` rule tests.
- **B-1.** The `health_alerts.py` fixtures in `scripts/diagnostics/tests/`.
- **B-2.** `scripts/diagnostics/tests/test_probe_monitors.py` is rewritten, and
  `test_kuma_live.py` and `test_probe_monitors_gates.py` are deleted. `test_postflight.py`
  changes with it. `docs/failure-classes.md` row 3 loses its `kuma-drift` detector
  (`test_kuma_drift_reports_a_declared_monitor_that_is_not_live`, in
  `test_probe_monitors.py`). Its replacement detector is `BridgeCheckStale`'s missing-row
  firing case. `test_probe_registry.py` and `test_probe_pi_plane.py` lose their `kuma-drift`
  cases. The root `CLAUDE.md` *Claude Tooling* bullet, `docs/claude-tooling.md` and the
  add-secret skill's `kuma-drift` steps change here.
- **B-4.** `test_homepage_block_label_overrides.py`.
- **B-6a.** The render test, in `ansible/tests/services/` beside
  `test_authelia_access_tiers.py`.
- **B-7.** The docs named in Phase B item 8, and the ADR draft. `docs/reference/` pages
  regenerate on the next docs-refresh run.
- **C-1 to C-7.**
    - `ansible/tests/deploy/test_manifests_prune.py` is unchanged.
    - C-1: M2's weekly-reboot mute test loses its Kuma-equality half. The uptime-kuma role's
      `CLAUDE.md` and tests go with the role.
    - C-1: `test_postflight.py` loses `check_kuma_scrape`'s cases.
    - C-1: `ansible/tests/k8s/test_netpol_probe_targets.py` gains the probe-row named-member
      case, and runs against the table without the uptime-kuma row.
    - C-1: `test_kuma_push_token_format.py`, which evaluates the
      deleted token-format assert, is deleted, with A0's floor test. Every other
      `test_kuma_*.py` in `ansible/tests/services/` that reads a deleted template or task,
      such as `test_kuma_static_monitors.py` and `test_kuma_entities_parse_for_autokuma.py`,
      is deleted or cut to what still exists.
    - C-3a: `test_setup_cross_role_files.py`, `test_host_lib_sibling_copies.py`, and the
      setup/common `CLAUDE.md` rows for `kuma_push.py`. On the host side,
      `hosts/lib/tests/test_kuma_push.py` and `test_kuma_push_component.py` are deleted
      with their subjects. `test_host_lib.py`'s 3.12-grammar case drops `kuma_push.py`.
      `scripts/renderer/tests/test_renderer_host_inputs.py` and
      `test_renderer_host_mode.py` stop using `kuma-push-lib.sh` as their sample shared
      file.
    - C-3b: `test_kuma_check_timer.py` (in `hosts/tests/`) is renamed with its task
      file and gains the legacy-unit case. The setup/common, gitops_deploy, initial_setup and
      render_records `CLAUDE.md` files are updated, and their glance blocks regenerate.
      `scripts/docs/tests/test_gen_role_glance.py` gains the named-member timer case.
    - C-4a and C-4b: `scripts/lib/ansible_jinja_env.py` registers the new filter, then drops
      the old one.
    - C-4a: `test_kuma_ingress_monitors.py` (in `ansible/tests/services/`) is renamed with its
      filter, and `scripts/deploy_tools/tests/test_narrow_containers_fields.py` gains the row.
    - `docs/healthchecks-io-deadman.md` keeps no `uptime-kuma-alive` row (B1 removed it).
    - `docs/uptime-kuma-autokuma-record.md` gains a `**HISTORY —` header paragraph and stays.
    - C-6: the census row in `ansible/tests/repo/test_census_rows_text.py`.
    - C-7: `test_loki_stream_labels.py` keeps its source and its named member. The
      `swallowed_verdicts` tests in `ansible/roles/k8s/monitor-bridge/tests/` go with the row,
      `KUMA_RETIRING_TAGS` cases included.

## Complexity delta

- **Deleted:**
    - The uptime-kuma role (3,655 lines, 4 images, 2 claims, 2 CronJobs).
    - From monitor-bridge: `gates.py` (333 lines), `apply_startup_grace`, the push path, the
      check bodies of 21 Prometheus-reading rows, and the log arm (`with_log_errors`,
      `log_error_verdict`). That is roughly half of its 12.2k lines. The 15 bodies that read
      other sources stay, and so does `bridge/streaks.py`'s `State`, `down_streak` and probe
      caches (*Gates and grace*).
    - `CheckWithoutPager` and its `kuma-cutover` group, removed by the last
      shadow-dropping PR.
    - Their tests, a large share of the bridge's 12.8k test lines.
    - `kuma-push-lib.sh`'s push half, `kuma_push.py`, `monitors.py`'s drift half,
      `kuma_live.py` and `kuma_table_loop.py`.
    - 79 push tokens and 3 Kuma secrets, with their rotation-registry rows.
      `registry_gc_push_token` is among them, retired by B6c's token PR.
    - M5's `LokiHomelabDown`, which A3's scrape-target rule covers.
    - `uptime-kuma-alive` in Healthchecks.io, and one hand-written Authelia bypass.
    - The `swallowed_verdicts` row, its bodies and `KUMA_RETIRING_TAGS` (C-7), and M5's
      `KumaNotificationDropped`.
- **Added:**
    - About 75 rules with their tests, including `KumaDown` (deleted again in C-1a).
    - The bridge exporter loop, about 120 lines, with the `homelab_check_rows` gauge, and the
      autofix-bridge exporter.
    - `KUMA_RETIRING_TAGS`, one set that grows over B1 to B7 and B9 and goes in C-7.
    - `homelab-check-lib.sh`, about 60 lines.
    - Four blackbox modules and one `tcp_connect` target per Pi publisher.
    - `health_alerts.py`, about 150 lines.
    - Two census tests, one Alertmanager route, seven Alertmanager inhibits (A1's; A3's for
      `BlackboxExporterDown`, `KumaDown`, `MonitorBridgeDown` and `AutofixBridgeDown`, the
      `KumaDown` one deleted again in C-1a; and A6's for `WANUnreachable` and
      `B2Unreachable`) with one config test, one
      hand-written Authelia rule, and one ADR.
    - The temporary `CheckWithoutPager` rule, about 40 lines with its test, for the length
      of Phase A.
    - The size-cap test, one `DECIDED` marker on the gate skip, and about 5 lines per shadow
      PR for its tiles' terms.
- **Kept:** the bridge as an exporter, about 6,500 lines plus D2.6d's family, under a
  7,000-line cap.
- **Net:** one alert plane, where there were two stateful systems and a token per check.

## Open questions

1. **Contract gap, the slice index.** `M6` in contracts.md's slice index names only the reader
   switch and the deletion. The producer drain (Phase A, about 75 PRs) is assigned to no slice.
   Resolution: split the row into M6a (Phase A, depends on M2 to M5), M6b (Phase B, depends on
   M6a's A6, except B-1, B-1d and B-1e, which Open question 12 places after M1) and M6c
   (Phase C, depends on M6a and M6b, and on D6 for C-4b only). The orchestrator owns
   contracts.md and makes that edit. The sub-edges this page states in *Order* belong in the
   index too:
    - A7's shadow PR after M5's ruler is live, and A7's Cutover-a after M2's PR 4a;
    - D5's littlelink flux PR after B-1e (Open question 12);
    - C-1a after B6c's token PR (Open question 13), repointed from D5's registry PR on
      2026-11-02;
    - D5's monitor-bridge flip after A6-7a, live (*When D5 flips a role M6 edits*);
    - D6 after C-2b, live. Before #5821 this edge followed from uptime-kuma never flipping.
      The operator's 2026-10-21 ruling flipped it, and with it observability and
      netpol-baseline, before C-1. So the index states the edge, and no D5 flip waits on
      C-1.

   No row gate sits before the first Cutover-1a of B1 to B7. contracts.md's *Round 2 —
   integration review (X2-series)* entry *Applied with no contract text.* records that M5's
   handoff leaves none.

   The new D5 edges add no cycle, which the index states the way it does for B-1e. A6-7a
   waits on Phase A batches and on M2 to M5, and none of those waits on D5. C-1a waits on B6c's
   token PR, which waits only on B6c's own steps, and none of those waits on D5.
   C-4b waits on D6, and D6 waits on D5's tail and on C-2b, but nothing from C-1 to C-7 waits
   on C-4b.

   **Applied on 2026-10-16.** The slice index carries M6a, M6b with the B-1 exception, and
   M6c, with every sub-edge above and the no-cycle paragraph. No gate was named. **Amended on
   2026-10-22 (#5821).** The D5 row drops the observability, netpol-baseline and uptime-kuma
   edges, and the D6 row gains C-2b.
2. **Contract gap, C7: who sets `service` for an exporter.** The health gate's `service` must
   also match blackbox targets and bridge rows. C7 pins the label but not who sets it for an
   exporter. Resolution: C7 adds "a target label set by the scrape config, or a literal in the
   rule".
3. **Contract gap, C7 and H2.** CronJob-based checks (B7, and H2's moved crons) alert on
   kube-state-metrics series, which C7 does not name. Resolution: C7 lists the CronJob recipe
   beside M3's textfile contract.
   **Applied on 2026-10-16.** C7 *Check producers* names the recipe, and the gate before
   B7's pi-peer-backup shadow PR is clear.
4. **Contract gap, C7: the health gate's selector.** Phase B item 1 departs from C7's literal
   `ALERTS{alertstate="firing",service="<tag>"}` and `probe_success{service="<tag>"}` in six
   ways:
    - `shadow!="true"` (M1 open question 5 already asks for it);
    - `alertname!="Watchdog"`, because the Watchdog fires forever with
      `service: observability` and would fail every `observability` landing;
    - `severity="critical"` or `verify="true"`, the set D5's soak reads (C7 *Verify
      successors*, shape 1), so a warning-level verify successor gates too;
    - only alerts that became active after the tag's reference time (`ALERTS_FOR_STATE`), as
      D5 does, so an older alert cannot fail an unrelated deploy. The reference time is
      `applied_at` for an Ansible-owned tag, and the time its Kustomization first reported its
      current C6 `content_digest` for an `owner: flux` tag (#5338);
    - a `probe_success` sample newer than `applied_at`, so a sample taken mid-rollout cannot
      decide;
    - no probe half for a tag whose rendered entry has no route, even when an old
      `probe_success` series is still inside the lookback (C-1 removes uptime-kuma's route).

   Resolution: C7 adopts this selector, and D5's promote check and `probe.py health` share one
   definition of it. B-1 merges only after that amendment lands. Because the definition is
   shared, a `drill` exclusion cannot apply to one reader only, which is why the drill's tag
   must not be flux-owned.
   **Applied on 2026-10-16.** C7 *Health gate* states the six departures and the shared
   definition, and the gate before B-1 merges is clear.
5. **Batch order against H2.** Either side can go first (see *Phase A*). Resolution: whichever
   slice reaches a cron first converts it, and the other slice's plan drops that cron from its
   list.
6. **The `kuma-check-*` timer rename.** Renaming touches 8 timers on two hosts for a cosmetic
   gain, and C-3b needs a legacy-unit removal to be safe. Resolution: the operator ruled on
   2026-10-24 that the names stay (ADR 0026). C-3b shrinks to the task-file rename, and
   `kuma-check-` joins the census allowlist.
7. **The Pi's AutoKuma labels.** The `org.homelab.kuma-docker-monitor` label in
   `hosts/components/pi-compose/services/{alloy,autoheal,docker-proxy,wg-easy}/compose.yaml.j2`
   is inert once Kuma is gone. The shared macro that once emitted it had no caller, and
   [#7210](https://github.com/DanielH2018/server/issues/7210) deleted it. H5 rewrote the Pi compose files into the `pi-compose` service
   trees and kept the label. Changing it recreates every Pi container, which is a Pi deploy
   this slice does not schedule. Resolution: the four files stay on the census allowlist,
   with the comments beside the label and the header comments that name AutoKuma or Uptime
   Kuma. They leave it in the next Pi compose change that recreates their containers anyway.
8. **C-4b waits for D6.** `narrow_lib/filters.py` refuses any deleted plugin, and a refusal
   records every declared service as `k8s_unapplied`. Resolution: C-4b lands after D6 deletes
   the narrowing. If the operator wants it sooner, `filters.py` could learn that a deleted
   plugin whose filters have no caller at the old ref reaches nothing, with its own red and
   green fixtures. That is a deploy-safety change and gets its own PR.
9. **`ALERTS_FOR_STATE` across a Prometheus restart.** Upstream Prometheus restores an alert's
   activation time after a restart only for rules whose `for:` exceeds
   `--rules.alert.for-grace-period`, as recalled; nothing in the tree confirms it. If so, a
   long-firing alert with a short `for:` reads as new after a restart, and the health gate
   fails closed on it (Phase B item 1). Resolution: B-1's PR reads `ALERTS_FOR_STATE` for one
   firing short-`for:` alert before and after the next Prometheus pod restart, and records
   whether the value moved. If it does not move, the "age unknown" message is dropped.
   **Partly recorded on 2026-10-16 (B-1).** No restart fell inside B-1, and an agent cannot
   restart Prometheus, so the before/after pair is still owed
   ([#5333](https://github.com/DanielH2018/server/issues/5333)). One reading points the
   recalled way. The Watchdog has fired without a break, yet its `ALERTS_FOR_STATE` read
   `1792078185`, 6.4s after `process_start_time_seconds{job="prometheus"}` read
   `1792078178.57`. Its activation therefore reset at the last restart, so B-1 keeps the
   "age unknown" message.
   **Recorded on 2026-10-25 (#5333): the value moves, so the message stays.** Prometheus
   restarted at 07:42:40 UTC, and `process_start_time_seconds{job="prometheus"}` went from
   `1792858430.65` to `1792914160.96`. `AideCheckNotReporting{origin="daniel-box"}` has no
   `for:` and fired on both sides of the restart. Before it, an `@ 1792913500` query read its
   `ALERTS_FOR_STATE` as `1792885066` (23:37:46 UTC on 2026-10-24), 7.4h after that process
   started. After it, the value read `1792914226`, 65s after the new start. Every other alert
   that fired across the restart reset the same way, the Watchdog from `1792858485` to
   `1792914225`. None of those rules carries a `for:`. So the reading shows the reset for a
   short-`for:` rule and says nothing about restoration for a `for:` longer than the grace
   period. The pair for `AideCheckNotReporting{origin="daniel-box"}`:
    - Before the restart: `ALERTS_FOR_STATE` `1792885066`, `process_start_time_seconds`
      `1792858430.65`.
    - After the restart: `ALERTS_FOR_STATE` `1792914226`, `process_start_time_seconds`
      `1792914160.96`.
10. **Owner of the `workload_log_errors` port** (M2 Open question 6). M5's text keeps
    `with_log_errors` and does not retire the row's token, and M2 names M6 as the fallback
    owner. Resolution: M6 owns it as A7. If M5's text adopts the port before A7's shadow PR,
    A7 drops out, the bridge-row count returns to 40 and Phase A's token count to 76. M5 has
    declined it (M5 Open question 6), so M2's PR 4a gate must read this page's A7 as the
    adoption, and A7's shadow soak starts only after PR 4a creates the `Workload Log Errors`
    tile. M2's text still names M5 as the adopter, and this round's cross-slice notes ask M2
    to change it.
11. **The uptime-kuma asserts against R4's PR 4.** R4's precondition test names "Check the
    declaration index is populated" and "Check every push token is in the format AutoKuma
    accepts" by title, and R4's PR 4 moves them into `renderer_lib`
    (`docs/target-architecture/render/r4-delete-k8s-ansible-path.md`). A0 changes the first
    one's expression and keeps its title. Resolution: uptime-kuma's conversion PR (#5712) deleted
    both asserts before C-1, and C-1b deletes the declaration lookup. R4's test drops the two
    names. R4 waits on D6, and D6 waits on C-2b (Open question 1).
12. **Contract gap, the slice index: the drill against D5.** The live drill needs an
    Ansible-owned tag with a release record, and D5 leaves none (*Verification*). The index
    has no edge between them. Resolution: the index carries two sub-edges. D5's flux PR for
    littlelink waits for M6's B-1e. M6's B-1, B-1d and B-1e depend on M1 and C7's amendment
    for Open question 4 only, not on M2 to M5, so that wait stays short. The edges belong in
    the index because this page cannot hold a D5 PR.
    - **The register and the index misrecord the request.** The register's M6 split row reads
      "M6b (Phase B, on M6a's A6)" with no B-1 exception. The index's D5 row holds "the PR
      that flips the last Ansible-owned stateless leaf", where M6 and D5's wave table both name
      littlelink. Neither carries "C-1 after D5's registry PR". The request is three
      corrections: name littlelink in D5's index edge, add the B-1 to B-1e exception (M1 and
      C7's Open question 4 amendment only) to the M6 split row, and add C-1's edge to D5's
      registry PR.
    - Until they land, the index governs, and *Order* states the stall it implies.
    - **Applied on 2026-10-16.** All three corrections are in the index: D5's row names
      littlelink's flux PR, M6b carries the B-1 to B-1e exception, and M6c carries C-1's
      edge to D5's registry PR.
13. **Registry GC is M6's batch B6c.** The plan first gave registry-gc to D5. D5's Wave D
    registry PR was to retire the GC, its tile and its token, and D5's Open question 15 named
    D5 the only owner. D5's registry PRs landed (#5689, #5729, #5746, and the flip in #5830),
    and #5746 retired only the push store's GC phase. The mirror phases still run and still
    push, and no D5 PR removes the push, so a C-1a gated on D5's PR could never start
    (#6926). Resolution, 2026-11-02: B6c drains registry-gc under M3's recipe, M6 counts 79
    tokens, and C-1a waits for B6c's token PR.
    - **The index edge is stale, not wrong in effect.** contracts.md's M6c row still names
      D5's registry PR, which has landed, so that edge is satisfied. C-1a's quoted
      `registry_gc_push_token` grep and its one-series gate enforce the B6c wait. The
      orchestrator owns contracts.md, and the request is to repoint the row at B6c's token
      PR.
    - **Applied on 2026-11-02 (#6930).** contracts.md's M6c row and the paragraph after it
      name B6c's token PR, which waits on no D5 step.
    - The fallback this question once named, a B6 PR that moves registry-gc under M3's
      recipe, is what B6c does. Nothing is left for D5 to delete.
14. **D2.6d and A6-1 both need the bridge's serving thread.** D2.6d adds the deploy-pipeline
    family to the bridge's exporter, and A6-1 adds the serving thread, the `port` and the
    `metrics` item. The index gives D2.6d no edge to A6-1. Recommendation: whichever of the
    two lands first adds the thread and the scrape job, and the other reuses them, so the
    index needs no edge. Resolution: D2's owner confirms. If D2 declines, the index carries
    "D2.6d after M6's A6-1, live".

## Review log

### Round 1 (per-slice adversarial review)

Raised: 12 high, 13 medium. Fixed: 24. Refuted: 1.

- **M6-R1-01 (high)** The health gate never passes for `observability`, because the Watchdog
  always fires with `service: observability`. **Fixed.** Phase B item 1 excludes
  `alertname="Watchdog"`, gates only on critical alerts that became active after
  `health.py:_deploy_applied_at`, and lists the fixtures (a firing Watchdog and an older alert
  must pass). Open question 4 names the C7 divergence.
- **M6-R1-02 (high)** A6 deletes row fields that uptime-kuma's templates and
  `kuma_table_loop.py` still read. **Fixed.** A6-7 removes the `CHECKS` loop and the status-page
  pins in the same commit, and deletes `kuma_table_loop.py`'s `CHECKS` half. The fields go in
  A6-8. Keeping the fields until C-1 was rejected, because the 17 remaining bridge tiles would
  go DOWN 1200s after A6-7 stops their pushes. The review missed that
  `fragments_bridge.py` also reads `display`, `status_group` and `gates.py`. A6-7 and A6-8 now
  cover it.
- **M6-R1-03 (high)** `pi_pressure` has a TCP port arm that A1 dropped. **Fixed.** The baseline
  says 20 rows read Prometheus only. B8 adds one `tcp_connect` target per `pi_publishers` entry
  and `PiPortDead`, and cuts over the `pi_pressure` row. Failure-classes row 6 moves to
  `PiPortDead`'s firing case.
- **M6-R1-04 (high)** The exporter loses hung-loop detection, and the bijection test forbids a
  staleness rule. **Fixed.** `BridgeCheckStale` (aggregate: stale timestamp, or row count
  below `len(CHECKS)`) and `AutofixBridgeStale` (B7), each exempt from the bijection by name,
  with red proofs.
- **M6-R1-05 (high)** Exit criterion 3 was unreachable. **Fixed.** It is a functional criterion
  with a ratchet allowlist that names each file and its reason. The Pi labels go to H5 (open
  question 7).
- **M6-R1-06 (high)** M5's Kuma-subject log rules were never deleted. **Fixed.** C-7 deletes
  them after C-1 and C-3a. The census keeps its source and its named member
  `LokiPodLogsSilent`.
- **M6-R1-07 (high)** C-1's prune claim was false, because the shared-default Service and
  IngressRoute stay rendered. **Fixed.** C-1 drops `port`, `hostname`, `use_authelia`,
  `auth_tier` and `netpol_from`, and deletes the hand-written Authelia bypass. It moves the
  scrape job into C-1, because it calls `entry_port('uptime-kuma')`. It deploys
  `observability`, `uptime-kuma`, `authelia` and `netpol-baseline` in order, and verifies with
  the read-only `kubectl get`. `netpol_from` had to move too, because
  `networkpolicy-callers.yaml.j2` renders `entry.port` for every entry with callers.
- **M6-R1-08 (high)** The C-1 gate counted only push tiles. **Fixed.** It is
  `count by (monitor_type, monitor_name) (monitor_status)` equal to exactly the `Status Page Sync`
  tile. B8's and M4's cutovers are named preconditions.
- **M6-R1-09 (high)** The Kuma-route probe could not see a dead Kuma behind Authelia.
  **Fixed.** `KumaDown` on the direct scrape replaces it. It lands with B1's shadow PR and is
  deleted in C-1's first deploy, before Kuma goes. B8 has no Kuma-route probe. *Risks* states
  that `up` is weaker than an accepted push.
- **M6-R1-10 (high)** The tick applies the "hand-applied" batches, which reverses the cutover
  order. **Fixed.** *Which plane applies each producer* comes from
  `setup_routing.py --host daniel-box`. Every B batch cuts over in two PRs, pager first, and a
  hand apply of a tick-applied role on daniel-box is forbidden.
- **M6-R1-11 (high)** The ADR that design.md assigns to M6 was missing. **Fixed.** C-0 is
  **operator-only**, written as Proposed with or before B-7, at the next free number. C-1
  waits for its acceptance.
- **M6-R1-12 (high)** The live drill could not run as scheduled. **Fixed.** B-1d and B-1e run
  after B-1, with `HealthGateDrill` on `littlelink` and a `drill="true"` blackhole route.
- **M6-R1-13 (medium)** B-2 deleted modules that `postflight.py` imports. **Fixed.** B-2
  carries the postflight change and reverts as one. `check_kuma_scrape` goes in C-1.
- **M6-R1-14 (medium)** The baseline falsely said homelab-mcp imports `kuma_live`. **Fixed.**
  The baseline is corrected, and only the role's `CLAUDE.md` sentence changes (B-7).
- **M6-R1-15 (medium)** The C-3 timer rename left the old units running and broke the
  cross-role tables. **Fixed.** C-3b adds a legacy-unit removal with a test case, and updates
  `deploy_cross_role.py`, `narrow_setup.py`, `setup_role_chains.py` and the glance blocks. C-3
  is split into C-3a and C-3b and re-measured.
- **M6-R1-16 (medium)** B-6 did not say how the route gets `two_factor`. **Fixed.** A
  hand-written rule in `authelia/templates/config-secret.yaml.j2` (B-6a, deploys `authelia`,
  with a render test) lands before the IngressRoute (B-6b), which names the `authelia` chain
  Middleware.
- **M6-R1-17 (medium)** Docs, `CLAUDE.md` citations and failure-classes rows citing deleted
  tests were missing. **Fixed.** *Tests and docs* lists them per PR, with the `fact_status.py`
  and glance steps. Rows 3 and 6 get `BridgeCheckStale` and `PiPortDead`.
- **M6-R1-18 (medium)** The deploy tags for B8, C-2 and C-4 understated broad changes.
  **Fixed.** Every inventory or filter-plugin PR quotes `deploy_tags.py narrow`. C-2 and C-4
  are split, and C-4b waits for D6 (open question 8).
- **M6-R1-19 (medium)** The gate could read a mid-rollout `probe_success`. **Fixed.** A sample
  older than `applied_at` is ignored, the gate waits up to 90s for a fresh one, and the fixtures
  cover a stale 0 and a fresh 0.
- **M6-R1-20 (medium)** A6 was far over size and contradicted the shadow phase. **Fixed.** It
  is A6-1 to A6-9 with sizes, and the exporter runs beside the push until A6-7.
- **M6-R1-21 (medium)** Retiring `uptime-kuma-alive` omitted its consumers and the order.
  **Fixed.** The baseline lists the consumers, and B1's Cutover-2 sequence runs: pause the check
  (**operator-only**), merge, then delete the check within one `healthchecks_drift` cycle.
- **M6-R1-22 (medium)** B7's log rule hid M5's limits. **Fixed.** B7 names the three costs,
  adds `CloudflareDdnsUnavailable` so the health gate sees a dead updater, and gives M5's red
  proof.
- **M6-R1-23 (medium)** Turning `STARTUP_GRACE` into `for:` changes what the grace means.
  **Refuted.** At `70591c307`, `ansible/roles/k8s/monitor-bridge/files/check.py:run_once` calls
  `bridge.streaks.apply_startup_grace(name, ok, msg, cfg.GRACE_CYCLES, src.state.grace_streaks)`
  on every down result of a `startup_grace` row. Nothing tests the target's start time.
  `bridge/streaks.py:apply_startup_grace` is a consecutive-down count that "an `ok` result
  resets". `GRACE_CYCLES` is `"2"` in `templates/env-secret.yaml.j2`, and `bridge/config.py`'s
  comment says "a genuinely-down dependency still alerts after ~one extra INTERVAL". The grace
  already delays every page by one cycle, so `for: 5m` keeps that timing. The baseline now
  records this.
- **M6-R1-24 (medium)** M2's reboot-window equality test was not handled in C-1. **Fixed.** C-1
  rewrites it to derive the window from `group_vars` alone.
- **M6-R1-25 (medium)** B5 added the flag but no directory. **Fixed.** B5 replaces
  `--no-collector.textfile` (which the review also missed) and adds M3's directory task under
  `node-exporter-host`, with a test that the flag and the directory agree.

### Round c1 (per-slice adversarial review)

Raised: 4 high, 5 medium. Fixed: 9, two of them in part. Refuted in part: 2.

- **M6-C1-01 (high)** The one-PR cutovers for A1 to A6-7 and B Cutover-1 deploy the live rules
  before the Kuma tile goes, so two pagers run at once. **Fixed.** At `70591c307`,
  `ansible/filter_plugins/toposort.py` orders `observability` before `uptime-kuma` and
  `uptime-kuma` before `monitor-bridge`, which M2's PR 4a also states. *The cutover order* now
  says so and adopts M2's split for every cutover: Cutover-a and Cutover-b for A1 to A5, A7,
  B8 and A6-7 (as A6-7a and A6-7b), and Cutover-1a, Cutover-1b and Cutover-2 for B1 to B7.
  Each PR changes one pager side only, under M2's stacking rule and two-hour bound. The
  one-PR form is no longer called M2's recipe. *Steps*, *Rollback*, *Risks* (the no-pager
  window, the reordering risk), *Tests and docs* and the PR count (about 75) changed with it.
  The same toposort put `authelia` first in C-1, so C-1's deploy order and its partial-run
  rollback were corrected too. The Healthchecks.io pause for `uptime-kuma-alive` now comes
  before B1's Cutover-1b, so Kuma's death never pages from both Healthchecks.io and
  `KumaDown`.
- **M6-C1-02 (high)** The `workload_log_errors` row that M2 creates belongs to no slice.
  **Fixed.** M6 owns the port as batch A7, per M2's Open question 6, which names M6 as the
  fallback owner. A7 has a shadow LogQL rule under M5's recipe and costs, a split cutover
  that deletes the row, `with_log_errors`, `log_error_verdict` and the `LOG_ERROR_*` keys, and
  a token PR. The baseline is 40 rows (37 checks and 3 gates). Phase A retires 77 tokens and
  Phase C the 78th. A6-7a now waits for A7 and B8's cutovers too, because the bijection test
  fails on any non-exporter row. A6's families are unchanged. Open question 10 records what
  happens if M5 adopts the port first.
- **M6-C1-03 (high)** The token retirement leaves every push token in the rotation registry.
  **Fixed.** Confirmed at `70591c307`: `secret_registry.py:sync` only reports stale rows, and
  `audit --check` fails on them under the prek hook `secret-rotation-registry-sync`. Every
  token PR and C-5 is now one commit that also deletes the rows from
  `ansible/secret_rotation.yml`, following commit `6894e3523`, and quotes `audit --check`
  and the `_push_token:` count. Exit criterion 2 also reads `ansible/vars/secrets.yml`.
- **M6-C1-04 (high)** The drill's critical `littlelink` alert can fail D5's soak. **Fixed.**
  The drill is C8's operator-only production drill, with its blast radius stated. It takes a
  precondition that littlelink is not `owner: flux`. It reads littlelink's `applied_at` three
  times and is void if that moved. It adds a promtool firing case and an amtool routing case
  for `HealthGateDrill`. C8's fixture-tag form was rejected: `probe.py health` renders the
  tag's role to find its workloads (`land_lib/tools.py`'s note), and
  `health.py:_deploy_applied_at` reads the tag's release record. A scratch tag has neither.
- **M6-C1-05 (medium)** The health gate's "active after applied_at" test is unsound for
  flux-owned tags and after a Prometheus restart. **Fixed in part, refuted in part.** For an
  `owner: flux` tag, the reference time is when its Kustomization first reported its C6
  origin revision. The restart case fails closed, with a fixture and Open question 9 to
  confirm the upstream behaviour. Two fixtures were added. Refuted: B-1 does not change the
  verdict of `runbook_gates_lib/gate_runner.py` or `runbook_gates_lib/k3s_upgrade.py`. A
  case-insensitive grep for `health` in those files at `70591c307` finds only docstrings about
  the kubectl identity check that guards `probe.py health`. Neither calls it. *Rollback* now
  lists the real callers: `detach_notify.py:gate` (run by `detach_run.py`),
  `land_lib/tools.py`, and `land_lib/health_verdict.py`.
- **M6-C1-06 (medium)** B-1 has no merge gate on the C7 amendment its selector needs.
  **Fixed.** B-1 merges after the amendment for Open question 4 lands. B7's pi-peer-backup
  shadow PR waits for the amendment for Open question 3.
- **M6-C1-07 (medium)** The k3s hand applies name no narrowed tag. **Fixed.** From the tags in
  `setup/k3s/tasks/health-crons.yml`, B1 uses
  `--tags "backup-health,etcd-snapshot,manifest-prune,release-staleness,live-drift"`, C-3a
  uses `--tags backup-health` (the `host_lib.py` install), and C-3b uses
  `--tags "manifest-prune,live-drift"` (the two timer imports). *The cutover order* forbids
  `--tags k3s`.
- **M6-C1-08 (medium)** B-2 deletes `kuma-drift`, but the docs that point readers to it change
  only in B-7. **Fixed in part, refuted in part.** The root `CLAUDE.md` bullet,
  `docs/claude-tooling.md` and the add-secret skill's steps moved into B-2, with
  `fact_status.py verify`. G2's PR 3 has since retired the lock, and #7231 removed this step. The facts lint's `vanished-identifier` rule would block B-2
  without them. `cli_parser.py`, `subcommands.py` and `pi_plane.py` joined B-2's scope.
  Refuted: `monitors` carries no `--pi` at `70591c307`. `probe_lib/cli_parser.py` adds `--pi`
  only to `targets`, `kuma-drift` and `alerts`. B-2 states what `alerts --pi` selects.
- **M6-C1-09 (medium)** C-3a deletes `kuma-push-lib.sh` without a precondition that every
  hand-applied host has stopped sourcing it. **Fixed.** C-3a's precondition reads fresh
  `homelab_check_last_run_timestamp_seconds` for every B1, daniel-server and Pi check after
  its Cutover-2, plus a tree grep showing that no script sources the file. The copy task is
  `tags: [always]`, so C-3a replaces it with an `always`-tagged absent task. That task runs
  through the tick on daniel-box and through hand runs on daniel-server and the Pi. C-6
  deletes the task once each host shows the file gone. B0's batch row now names the hand
  applies on daniel-server and the Pi as well.

### Round c2 (per-slice adversarial review)

Raised: 1 critical, 2 high, 4 medium. Fixed: 7. Refuted: 0.

- **M6-C2-01 (critical)** C-1 could not deploy the emptied uptime-kuma role: the shared prune
  guard refuses an empty render, and the role's declaration lookup fails first. This is new
  evidence against M6-R1-07, whose fix was incomplete. **Fixed.** Confirmed at `70591c307`:
    - `ansible/roles/k8s/manifests/defaults/main.yml:manifests_prune` is `true`, and the
      uptime-kuma include passes no `manifests_prune: false`.
    - The `always`-tagged assert "Check that a role arming the prune renders something to
      keep" in `ansible/roles/k8s/manifests/tasks/main.yml` refuses an empty
      `manifests_files_resolved`, which excludes the Secret files.
    - "Read the AutoKuma declarations back as data" in
      `ansible/roles/k8s/uptime-kuma/tasks/main.yml` looks up `static-monitors.yaml.j2`.

  C-1 takes the finding's Option 1. It deletes the three pre-include tasks and the seven
  templates, and adds `templates/tombstone-configmap.yaml.j2` so the armed prune runs. It
  drops `manifests_secret_files` and sets `manifests_rollout: ''`, because the passed secret
  list names deleted templates and the drain would wait on a pruned Deployment. It quotes a
  `--dry-run`, and the guard runs in a dry run. `land.sh`'s health gate passes the emptied
  role: the rollout half reports nothing to gate, since `health.py` exits 0 for a role with no workload
  (`scripts/diagnostics/probe_lib/health.py:DECIDED exit 0, a pass with a note`). B-1's
  probe half skips a tag whose rendered entry has no route, with a fixture, so the probe
  series that stops when C-1 drops the route does not fail that gate either. The
  claims, the Secrets and the tombstone go in an **operator-only** delete after C-2b.
  Updated: exit criterion 1, C-2b, *Steps*, *Verification*, *Rollback* (partial C-1),
  *Risks*, *Tests and docs* (`test_kuma_push_token_format.py` and the other
  `ansible/tests/services/test_kuma_*.py` files that read deleted templates), *Complexity*
  and *The tree*.
- **M6-C2-02 (high)** Phase A's tile deletions take the index below the
  `kuma_declarations | length > 50` floor. **Fixed.** Measured at `70591c307`:
  `static-monitors.yaml.j2` declares 57 literal `*.json` keys (not the 59 the finding gives),
  plus one per `kuma_ingress_monitors` tile and one per `CHECKS` row, so five remain after
  Phase A and M4. New PR A0 lands before any Kuma-side PR. It replaces the floor with a
  named-member assertion on `status-page-sync`, `discord`, `tag-severity` and `tag-runbook`,
  keeps the task's title, and carries a red/green test. C-1 deletes both asserts and the
  lookup. Open question 11 records the interaction with R4's PR 4.
- **M6-C2-03 (high)** The health-gate selector dropped `verify="true"` warning rules, which
  contradicts D5's soak and contracts.md C7 *Verify successors*. **Fixed.** Confirmed in
  `docs/target-architecture/deploy/d5-bulk-move.md` (*The soak as a Job (D5.3)*) and C7.
  Phase B item 1 reads the union of the `severity="critical"` and `verify="true"` selectors,
  with the shadow, Watchdog and activation-time filters. Exit criterion 4, the no-record
  fallbacks and Open question 4 changed with it. Two fixtures were added: a `warning` alert
  labelled `verify="true"` and active after the reference time must fail, and a `warning`
  alert without `verify` must pass.
- **M6-C2-04 (medium)** A6-7a deleted `bridge.types.push_env`, which `registry.py` imports.
  **Fixed.** `ansible/roles/k8s/monitor-bridge/files/registry.py` does
  `from bridge.types import Check, push_env`. A6-7a now changes `registry.py` and
  `bridge.types.Check`. It also removes `bridge/config.py`'s `KUMA_URL`, which `_env` reads
  as required, so its absence would make `main()` exit 2. The `sources.py`, `cli.py` and
  `check_table.py` mentions are reworded in A6-7a. The review missed that the mentions in
  `checks/logs.py` and `verdicts/logs.py` belong to the `swallowed_verdicts` row. M5 leaves
  that row for M6 to delete once host pushes end, and no step deleted it. C-7 now deletes
  it, with its rule, its tests and the `bridge/common.py` comments.
- **M6-C2-05 (medium)** Exit criterion 3's identifiers remained in files no PR covered.
  **Fixed.** *Files the batch rows do not name* assigns each file to the PR that removes its
  subject:
    - B2: `cmd_audit --push`, `rotation_tools.py:kuma_push` and the
      `secret_bearing_host_paths.py` docstring.
    - B4's token PR: `consumers.py`.
    - B0: the `group_vars/all.yml` comments, with a narrowing quote.
    - B-2: the `probe.py` docstring.
    - C-3a: `stamp_deployed.yml` and `setup-drift-lib.sh`.
    - C-5: `sops_io.py:malformed_push_tokens` and its call in `cmd_audit`.

  The allowlist adds `scripts/dev/pytest_shard_weights.json`, a generated file. Each
  allowlist entry names the identifiers it holds. C-6 quotes the identifier `git grep` as its
  precondition.
- **M6-C2-06 (medium)** After C-3b's rename, the glance generator dropped the timers.
  **Fixed.** C-3b updates `scripts/docs/catalog_lib/glance_facts.py:timer_units` to the new
  task file and variables, and to `homelab-check-<name>.timer`. It renames the
  `kuma_check_*` variables, which would otherwise fail the census row. A named-member case
  in `scripts/docs/tests/test_gen_role_glance.py` fails when the k3s role yields neither
  timer.
- **M6-C2-07 (medium)** C-4a and C-4b left `scripts/lib/ansible_jinja_env.py` on the old
  filter, and exit criterion 3 contradicted its own allowlist until D6. **Fixed.** C-4a
  registers `ingress_probe_targets`. C-4b removes the import, the registration and the
  `FILTER_FIELDS` row. Exit criterion 3 is reached only after C-4b, and those three entries
  are allowlisted until then.

### Round c3 (per-slice adversarial review)

Raised: 3 high, 4 medium. Fixed: 7. Refuted: 0. Every finding was checked against
`70591c307` before its fix.

- **M6-C3-01 (high)** C-1 broke every netpol-baseline deploy: the `uptime-kuma` probe row
  reads the entry's port and gates on the pruned Service. **Fixed.** Confirmed:
  `netpol_baseline_probe_targets` holds the row with no `port`,
  `netpol-probe-job.yaml.j2` falls back to `entry_port(t.service)`,
  `scrape_jobs.py:entry_port` raises ValueError for an entry with no `port`, and the task
  "Fail if any probe target has no ready endpoint" loops the active rows. C-1 now deletes the
  row in the commit that drops `netpol_from`, and C-2a no longer names it. C-1's dry run is
  `--tags "uptime-kuma,netpol-baseline"`, and the plan says the readiness task skips in a dry
  run (`when: not k8s_no_mutate`), so the quote proves the render only.
  `test_netpol_probe_targets.py` is in C-1's tests, and C-1 adds a named-member case: every
  portless row names an entry that exists and has `port`. Its red proof is a synthetic
  fixture, because no portless row names uptime-kuma after C-1. Exit criterion 3 and *Risks*
  name the indirect read. Updated: *Kuma's footprint*, *Steps*, *Verification*, *Rollback*,
  *The tree* and *Tests and docs*.
- **M6-C3-02 (high)** M6 relied on M5 rules that M5 no longer adds, and B-batch cutovers
  would page through `swallowed_verdicts`. **Fixed.** Confirmed in `m5-loki-ruler.md` Open
  questions 4 and *Risks*. At `70591c307`, `check_table.py` has 44 rows and
  `ansible/secret_rotation.yml` 83 push tokens. M6 re-baselines to M5's output:
    - 41 rows, 38 checks and three gates. `swallowed_verdicts` sits under `startup_grace` and
      reads loki-homelab, and A6 exports it as a sixth `startup_grace` row.
    - A6 retires 19 tokens. Phase A retires 78 (41 bridge, 37 other) and Phase C the 79th
      (83 − 3 − 1).
    - M5 adds only `KumaNotificationDropped`, which C-7 deletes with the row.

  M6 takes M5's first option, the exclusion. `verdicts/logs.py` gains `KUMA_RETIRING_TAGS`,
  which `swallowed_verdicts` leaves out of its `rejected` set only. `HC_ROUTED_TAGS` cannot
  hold the tags, because
  `test_hc_routed_tags_are_exactly_the_scripts_that_read_kuma_push_ok` pins it to the
  `KUMA_PUSH_OK` readers. Each B1 to B7 shadow PR adds its producers' tags, a week before
  Cutover-1a deletes the tiles. Cutover-1a was rejected as the place, because the toposort
  deploys `monitor-bridge` after `uptime-kuma`, and a refused push read in that gap could
  page. The selector reads host syslog and the `pi-peer-backup` container, so B7 is in scope.
  Option 2 (an early deletion) was rejected, because it leaves no detector for a push into a
  deleted tile during Phase A's own tile deletions. *Order* gates the first Cutover-1a on
  M5's contracts row. Rewritten: *The cutover order*'s window paragraph, C-1's health-gate
  half (`KumaNotificationDropped` never reaches `ALERTS`; the `swallowed_verdicts` rule is a
  `warning` with `service: monitor-bridge` and no `verify`), *Risks* class 2 with its
  residual, C-7, A6-8 (`LOKI_URL` stays until C-7), the intro, A0's count, Open question 10
  and *Complexity*. contracts.md holds no M5 row at `70591c307`. The row is requested in this
  round's cross-slice notes.
- **M6-C3-03 (high)** C-3a deleted `kuma_push.py` while its consumers still copied, staged
  or stamped it. **Fixed.** The finding's list was incomplete. A `git grep` for
  `kuma_push.py` also finds a stamp pair in `renovate_notify/tasks/code.yml`, which the tick
  applies, and a second `k3s` stamp, "Record the deployed live-drift check", tagged
  `live-drift`. `install_host_lib.yml` names `test_host_lib_sibling_copies.py` as its
  enforcer. C-3a now:
    - drops `kuma_push.py` from `install_host_lib.yml`'s loop and adds an absent task for the
      old `/opt` copies, which C-6 deletes once a read finds none;
    - deletes the eight stamp pairs;
    - changes `test_host_lib_sibling_copies.py`;
    - requires B7's autofix-bridge Cutover-2 to have removed the staging entry;
    - quotes the `git grep -l kuma_push.py` precondition.

  Its deploys are the tick for five setup roles, `land.sh` for configarr and janitorr, and
  `k3s --tags "backup-health,live-drift"` by hand.
- **M6-C3-04 (medium)** `MonitorBridgeDown` and `BridgeCheckStale` had no `for:`, so a
  restart could page and fail the health gate. **Fixed.** Both rules, and the autofix-bridge
  pair, carry `for: 10m`: two `INTERVAL`s, which cover the first cycle after a restart plus
  the scrape interval. Promtool cases show that a missed scrape and a partial first cycle do
  not fire.
- **M6-C3-05 (medium)** `BridgeCheckStale`'s row-count literal went stale between A6-2 and
  A6-7a. **Fixed by the finding's second route.** The bridge exports `homelab_check_rows`,
  set from the registry, and the rule compares against it. No literal remains, so no later
  PR edits a rule for a row deletion. A6-7a needs no `observability` deploy for it, and C-7's
  ordering paragraph no longer depends on the toposort. A6-1's exporter test holds the gauge
  to the registry.
- **M6-C3-06 (medium)** C-1's preconditions omitted Phase B, though homepage reads the
  entry's `hostname`. **Fixed.** Confirmed: `homepage_href` raises without `hostname`. C-1
  now requires B-2 and B-4 landed and deployed. It quotes
  `git grep -n -w uptime-kuma` over non-test, non-Markdown files under `ansible/`, outside the
  role. The grep uses `-w` rather than the finding's quoted `'uptime-kuma'`, because an
  unquoted YAML value is a reader too. The narrowing expectation notes homepage.
- **M6-C3-07 (medium)** B0 did not say how `homelab-check-lib.sh` reaches a host under a
  tag-scoped run. **Fixed.** B0's copy task and its stamp are `tags: [always]` in
  `initial_setup/tasks/crons.yml`, like the `kuma-push-lib.sh` pair. Its test finds the copy
  task by name and fails without `always`. *B0, B5, B7 and B8* states why
  `install_host_lib.yml` would not do, and why host batches wait for B0's apply.

Checking the fixes against the tree found four more gaps, fixed in the same round:

- `pi-sd-health.sh.j2`, `pi-recovery-health.sh.j2` and `longhorn-backup-health.sh.j2` read
  `KUMA_PUSH_OK`. Once their Cutover-2 drops the push,
  `test_hc_routed_tags_are_exactly_the_scripts_that_read_kuma_push_ok` fails unless the same
  commit drops their tags from `HC_ROUTED_TAGS`. B1's and B5's Cutover-2 now do, and their
  shadow PRs add the tags to `KUMA_RETIRING_TAGS` too.
- C-6's identifier grep would fail on the `kuma-push-lib.sh` comments in
  `verdicts/logs.py` and `checks/logs.py` and on `KumaNotificationDropped`, which survive
  until C-7. C-6 now lands after C-7.
- C-3a's `git grep -l kuma_push.py` precondition would list
  `uptime-kuma/files/push_heartbeat.py`, which only C-2b deletes. C-3a now lands after C-2b.
- `kuma_push.py` logs `push failed (...)` for the Python producers, so the
  `KUMA_RETIRING_TAGS` membership test admits their identifiers, not only shell script
  names.

### Round integration-2 (integration review)

Raised against M6: 1 high, 5 medium. Fixed in this page: 4. Already met here, with the fix
owed by other slices: 2. Refuted: 0. Each claim was checked at `70591c307` or in the sibling
plans before the edit.

- **X2-08 (high)** M6's live health-gate drill needs an Ansible-owned leaf that D5 removes,
  and no edge orders the two. **Fixed by the sub-edge route.** Confirmed: `d5-bulk-move.md`
  *Waves and windows* puts littlelink in wave A and every other role in waves A' to D, and
  the index row for M6 has no edge to D5. The fixture-tag route was rejected for two reasons.
  `probe.py health` renders the tag's role, which a scratch tag lacks. The route would also
  need the soak to exclude a `drill` label, which Open question 4 rules out. The drill
  section gains *Why the drill stays reachable*, and the fallback to "another Ansible-owned
  stateless leaf" is gone, because after D5 no such leaf exists. Open question 12 asks the
  index for two sub-edges. D5's littlelink flux PR waits for B-1e, and B-1 to B-1e depend on
  M1 and C7's amendment only. Updated: *Order*, the B-1d row in *Steps*, *Risks* (the stall
  residual) and Open question 1.
- **X2-10 (medium)** Batch A3 owned none of the handoffs from M4, M5 and M1. **Fixed.**
  Confirmed: m6 had no mention of `BlackboxExporterDown`, `LokiHomelabDown` or
  `AlertmanagerMetricsMissing`. `checks/cluster.py:check_cluster_targets` exempts no job,
  and its docstring calls the two selectors complementary. A new section, *A3 and the
  handoffs it closes*, names the five tasks:
    - the scrape-target rule exempts no job, and `alertmanager` maps to `observability`;
    - `BlackboxExporterDown` drops `shadow` in Cutover-b;
    - the routing pytest outcome flips from `blackhole` to `muted`;
    - an inhibit lets `BlackboxExporterDown` suppress A3's alert for `job="blackbox-ingress"`;
    - `LokiHomelabDown` is deleted, which M5 Open question 5 allows because A3's rule covers
      the job.

  Updated: the A3 table row, *Order*, *Verification*, *Rollback*, *Risks*, *Tests and docs*
  and *Complexity*.
- **X2-11 (medium)** The register and the index lack rows and sub-edges that slices name as
  gates. **Not this page's edit.** contracts.md owns the register and the index. M6's own
  part is already right: Open question 4 lists six departures, and *Order* states A7's two
  sub-edges and the M5-to-M6 row gate. Open question 1 now lists every sub-edge M6 asks the
  index to carry, and the cross-slice notes ask contracts.md to apply them.
- **X2-12 (medium)** M2's PR 4a gate waits for M5 to adopt the port, but M5 declined it and
  M6 A7 adopted it. **Not this page's edit.** Confirmed in M5 Open question 6 and in M2's
  PR 4a gate line. Open question 10 now says that M2's gate must read A7 as the adoption,
  that A7's shadow soak starts after PR 4a, and that 76 tokens remain if A7 drops out. The
  rewording and the token count of three are M2's, in the cross-slice notes.
- **X2-13 (medium)** D5's wave D and M6's B6 both retired registry-gc. **Fixed by dropping
  it from B6.** Confirmed: `d5-bulk-move.md` retires the cron, the tile and
  `registry_gc_push_token`, and calls D5 "the only owner of registry GC". B6 has 7 tokens and
  five roles. Phase A retires 77 tokens and Phase C the 78th. C-1 waits for D5's registry
  PR, which its one-series gate would enforce anyway. Open question 13 records the fallback
  for an operator who wants Kuma gone first. Updated: the intro, *Baseline*, *Which plane
  applies each producer*, *Order*, the C-1 row, *Risks* and *Complexity*.
- **X2-16 (medium)** M3's and M5's token recipe leaves stale registry rows. **Already met
  here.** Confirmed: the docstring of `secret_registry.py:sync` reads "Add missing secrets
  ...; report stale registry entries". The prek hook `secret-rotation-registry-sync` runs
  `secret_rotation.py audit --check`. Commit `6894e3523` is "Retire the E7-orphaned
  credentials". M6's four-part token commit already deletes the rows (*Steps*). The fix is
  owed by M3, M5 and D5's registry PR, and the cross-slice notes ask for it.

### Round i1 (per-slice adversarial review)

Raised: 1 high, 4 medium. Fixed: 5. Refuted: 0. Each claim was checked at `70591c307` or in
the sibling plans before the edit. One part of M6-I1-01 was narrowed rather than fixed as
raised.

- **M6-I1-01 (high)** C-1's wait on D5's Wave D registry PR puts Phase C inside Wave D, while
  every Phase C deploy still assumes Ansible owns roles D5 flips around it. **Fixed, mostly by
  the finding's first option.**
    - Confirmed: `d5-bulk-move.md` item 26 runs Wave D "in the order of the wave table", and
      the registry GC retirement is a conversion PR while registry is Ansible-owned.
      contracts.md C4 has `land.sh`, `deploy.sh` and the tick skip a flux-owned entry.
    - A new section, *When D5 flips a role M6 edits*, says that the merge applies a
      flux-owned role. It names the three steps that need one role's apply before another's:
      the bridge-row Cutover-a, `KumaDown` before Kuma's prune, and the callers policy after
      the status-page sync CronJob's prune. That CronJob's tile has a measured 1800 s
      `interval` in `static-monitors.yaml.j2`.
    - Index sub-edges keep the four roles involved Ansible-owned: monitor-bridge after A6-7a,
      observability and netpol-baseline after C-1, and uptime-kuma never. These are narrower
      than the finding's list. monitor-bridge waits for A6-7a, not C-7, because C-7's
      deploys are order-free. authelia needs no edge, because either order leaves the route
      under a tier at least as strict as the bypass.
    - A `dependsOn` replacement was rejected. It orders two Kustomizations, never a Flux
      apply against a `land.sh` deploy, and a mixed state is one of each.
    - **Narrowed: the configarr and janitorr half.** H2.5b removes configarr's
      `install_host_lib.yml` and `stamp_deployed.yml` imports, and H2.5c does the same for
      janitorr. D5 flips neither role before its H2 PR (`d5-bulk-move.md` item 26). So the
      `kuma_push.py` pair C-3a deletes is never in a flux-owned role. C-3a, C-6 and their
      *Steps* rows now make that edit, and its `land.sh` deploy, conditional on H2 not having
      landed. The `/opt` copy is H2's absent arm in `k8s_host_files` once D5's conversion
      PR has moved it there.
    - Updated: *The cutover order*, *Order*, C-1's owner paragraph, C-3a, C-7, *Steps*,
      *Risks* (a new class-5 entry), and Open question 1 with the acyclicity argument. The
      cross-slice notes ask D5 and contracts.md for the edges.
- **M6-I1-02 (medium)** The first Cutover-1a waited for an M5-to-M6 row that contracts.md
  says will never exist. **Fixed.** Confirmed in contracts.md's *Round 2 — integration
  review (X2-series)* entry *Applied with no contract text.* *Order* and Open question 1 drop
  the row gate and cite that entry. The real gate stays: each B batch's `KUMA_RETIRING_TAGS`
  entries are deployed before its Cutover-1a. A cross-slice note asks M5 to reword its
  *Unblocks* line.
- **M6-I1-03 (medium)** A1's inhibit named a source alert, `NodeExporterDown`, that no slice
  defines. **Fixed.** Confirmed: the A1 row was the only mention under
  `docs/target-architecture`. A1 now takes A3's rule, named `ScrapeTargetDown`, as the
  source, for each job `gates.py:EXPORTER_DEPENDENT` keys (`node`, `node-pi`).
    - It uses `equal: [job, instance]` rather than `origin`. Most cluster series lack
      `origin`, and Alertmanager treats a label missing on both sides as equal.
    - Each target rule's `for:` is at least 10m.
    - The config test fails on an undefined source, on an `equal` label the source lacks, and
      on a short `for:`.
- **M6-I1-04 (medium)** A3's no-exemption rule double-paged the jobs `KumaDown`,
  `MonitorBridgeDown` and `AutofixBridgeDown` watch, and `KumaDown` could go live before A3.
  **Fixed.** A3 task 6 adds an inhibit for each job. These rules are kept, unlike
  `LokiHomelabDown`, because each carries an `absent()` arm that `ScrapeTargetDown` lacks.
    - A shadow source would silence a live target, because Alertmanager inhibits before it
      routes. So each inhibit lands in the later of the PRs that drop `shadow` from its
      source and from its target. The config test fails on a shadow source over a live
      target.
    - *Order* gains B1's and B7's autofix-bridge Cutover-1b after A3's Cutover-a.
    - C-1 deletes `KumaDown`'s inhibit.
    - Updated: *Verification*, *Rollback* (A3), *Tests and docs* and *Complexity*.
- **M6-I1-05 (medium)** The X2-08 and X2-11 sub-edges are only partly in contracts.md, and
  the register contradicts B-1's timing. **Fixed in this page's text. The register edit is
  contracts.md's.** Confirmed: the index's D5 row names "the last Ansible-owned stateless
  leaf", the register's split row has no B-1 exception, and no row carries C-1's registry
  edge.
    - Open question 12 asks for the three corrections.
    - *Order* says the index governs until they land, and states the stall: D5's last
      stateless-leaf flip waits for most of Phase A, and B-1d names whichever leaf D5 still
      holds.
    - The *Risks* drill entry matches.

### Round i2 (per-slice adversarial review)

Raised: 1 high. Fixed: 1, with three parts of its prescribed fix changed. Refuted: 0. Each
claim was checked at `70591c307` before the edit.

- **M6-I2-01 (high)** A6-7a deleted `gates.py` and `bridge/streaks.py`, but the surviving
  exporter rows still depend on both. **Fixed.** Confirmed at `70591c307`:
    - `check.py:run_once` imports `gates` as `gate_lib` and `Gates` from it, calls `_gate`,
      `_evaluate`, `check_enabled` and `down_exporters`, and calls
      `bridge.streaks.apply_startup_grace`.
    - `cli.py` calls `gates.expand_gates_for_cli`, `validate_check_filter` and
      `check_enabled`. `registry.build_checks` skips every `is_gate` row, so `wan_reachable`
      and `b2_reachable` run only through `_gate`.
    - `bridge/sources.py` imports `State`. `checks/notify.py`, `checks/service.py` and
      `checks/host_edge.py` call `down_streak`, and `checks/cloudflare_ips.py` and
      `checks/healthchecks.py` import `ProbeCache`.
    - `checks/b2.py:check_b2_reachable` holds a billed answer for `B2_PROBE_INTERVAL_S`
      (1800s). Losing `State` would multiply the gate's B2 calls by six. That closes the
      gap the review left open.

  The changes to the plan:
    - **A6-1** writes the gauges at both push points, so the gate rows export from the
      start, and `WANUnreachable` and `B2Unreachable` soak against real series. It exports the
      verdict taken before suppression and grace, and adds the run-loop export test the
      finding asks for, with skip, grace and raising-body cases. `homelab_check_rows` now
      counts the enabled `CHECKS` rows, gate rows included. The registry's count was wrong,
      because it drops the gate rows.
    - **A new A6-7r** removes `gates.py` while the push is still on, and changes no pager. It
      moves `_evaluate` and `check_enabled` into `check.py`, runs the gate rows as ordinary
      rows through a `Check.gate` field, and drops `cli.py`'s gate filter helpers. *The run
      loop after `gates.py`* names every reader by path and symbol.
    - **A6-7a** now deletes only the push path, `apply_startup_grace` and the `prometheus`
      row, beside its tile, fragment and bijection-test edits.
    - **A6-3 to A6-6** add a rule-shape test that derives each rule's `requires` label, rule
      group and `for:` from the `gate` column, so the per-row streak decision is enforced.
    - *Order*, *The tree*, *Verification*, *Rollback*, *Risks* (three new entries), *Tests
      and docs* and *Complexity* are updated. *Complexity* now counts the WAN and B2
      inhibits, which it had left out.

  Three parts of the prescribed fix were changed:
    - **Suppression is not removed while Kuma pages.** Removing it in a refactor PR deployed
      under the push would push DOWN to every WAN tile in a WAN outage. That is the storm the
      WAN gate's comment in `run_once` cites. Instead, the split happens where each value is
      written: the export carries the end-state value from A6-1, and the push keeps today's
      until A6-7a. The bridge still skips the body of a row whose gate reads down, because
      calling `b2_storage_usage` during a B2 cap breach would bill one listing every cycle.
      Its docstring relies on the gate for that.
    - **`bridge/streaks.py` stays. No `bridge/state.py` is added.** `State`, `down_streak`
      and the caches survive in place, so no reader's import changes and
      `monitor_bridge_modules` keeps its entry. Only `apply_startup_grace` and
      `grace_streaks` go.
    - **The streak decision is made per row.** `startup_grace` becomes `for: 5m`, and the
      export is taken before the grace. Every in-body streak stays, and its rule has no
      `for:`. A rule's `for:` holds the whole row, while `check_arr_queue`,
      `check_ha_heartbeat` and `check_speedtest` streak one arm. `check_arr_queue`'s comment
      reads "The FETCH rides a streak; the queue verdict below does not". `check_discord`'s
      streak covers its whole down branch, but it stays so that one rule shape covers every
      in-body streak. design.md's "its streaks and `STARTUP_GRACE` are `for:`" is now true
      only of the grace and of the Prometheus rows' streaks. A cross-slice note asks for the
      amendment.
  - The finding measured sizes against "checklist item 8". contracts.md's item 8 is *Risks*.
    The 100 to 200 line rule is slice format item 5, and the sizes are measured against it.
    A6-7r is about 200 added lines and names its split if the diff is larger.

### Cross-slice follow-up (2026-10-12)

- Cross-slice note from M5: the Baseline bullet *Retiring a key leaves its registry row
  behind* now says that `audit --check` exits non-zero on any missing or stale row
  (`secret_rotation.py:cmd_audit` at `70591c307`). Each token PR is already one commit, so
  the recipe's order needed no change.
- **#4609 (medium): B1 and D6.17 both retired the three drift checks' tiles and tokens.**
  B1 converts manifest-prune, release staleness and live-drift to textfile checks, and
  D6.17 removed the same tiles and tokens before deleting the checks. Neither page cited the
  other. **Fixed by giving the Kuma side to B1 and the checks to D6.** X2-13's remedy, which
  dropped registry-gc from B6, does not fit here. C-1 needs exactly one Kuma series, so the
  three tiles must be gone before C-1. D6 waits for C-2b through the uptime-kuma edge
  (*When D5 flips a role M6 edits*), so D6.17 cannot remove them first. B1 therefore keeps
  all three, and the B1 row and *Other producers* name D6.17 and D6.18 as the slices that
  delete the checks. D6.17 deletes B1's `<Check>Failed` and `<Check>NotReporting` rules for
  them in the PR that disarms their crons. D6.17 can also land before C-3a or C-3b. C-3b
  therefore gains *When D6.17 or D6.18 has landed first*: each PR reads the k3s role at its
  base, and C-3b's named member falls back to initial_setup's setup-drift timer.
- **#4635 (medium): *When D6.17 has landed first* assumed that D6.17 deletes the k3s timer
  imports and the live-drift tasks.** D6.17 keeps the imports and sets their
  `kuma_check_state` to `absent`. It turns the live-drift tasks into absent tasks that keep
  their tag. D6.18 deletes both. The section's fallback therefore never triggered between
  D6.17 and D6.18. In that window C-3b renamed imports whose units were already absent, and
  its named member asserted two timers that were not installed. Nothing told C-3b to carry
  the absent state across the variable rename, so the rename could re-arm the units that
  D6.17 disarmed. **Fixed.** The section is renamed *When D6.17 or D6.18 has landed first*, and
  its conditions name D6.18 as the PR that deletes. C-3b carries `check_timer_state: absent`
  across the rename and keeps `timer_units`'s skip of a literal `absent`. Its named member
  excludes those imports. The PR body's grep adds `kuma_check_state`, so it shows which
  case applies. The B1 row and *Other producers* say that D6.17 disarms the checks.

- **#4636 (medium): C-3b could merge between D6.17's merge and D6.17's hand apply.** The
  apply then ran on the renamed task file, whose absent arm removed only `homelab-check-*`
  units. The `kuma-check-manifest-prune` and `kuma-check-live-drift` units kept running after
  a green apply, until D6.18's step 4 or C-6's `systemctl list-unit-files 'kuma-check-*'`
  caught them. A merge gate on C-3b was rejected: it depends on a session reading
  `gitops_state.py` at the right moment, and it still misses an apply that runs late.
  **Fixed.** C-3b's legacy-unit removal runs in both arms, with a test case per arm, and
  *When D6.17 or D6.18 has landed first* names the ordering. C-6 deletes the removal on the
  same `systemctl list-unit-files 'kuma-check-*'` evidence as before.

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review round covers it.

- **RA-9 / O6 (no-pager windows).** *Design* gains *No-pager windows*: one cutover window at
  a time, the temporary `CheckWithoutPager` rule in its own `kuma-cutover` group, and A6-7a
  in a quiet window. The rule lists tiles as `absent()` terms, because a shadow rule that
  does not fire has no series to read. A PR that removes the last term deletes the group,
  and a PR that adds a term re-creates it, so it is gone after Phase A rather than at M6's
  last PR: a rule with no term is not valid PromQL. *Phase A* and *Order* let soaks overlap and serialize the windows. *Steps*,
  *Verification* (four promtool cases), *Risks* and *Complexity delta* follow.
- **F-11 (the residual bridge).** *The bridge as an exporter* gains *The residual bridge and
  its cap*: 15 bodies, `State`, `down_streak`, the gate skip and D2.6d's family, about
  6,500 lines plus D2.6d's, under a 7,000-line planning cap that A6-8 sets from its
  measurement. A6-8 adds the cap test (~150 to ~180
  lines), with a red proof in *Verification*. A6-7r plans a
  `DECIDED the gate skip stays in the bridge` marker. The *Complexity delta* says "roughly
  half", not "most", of the 12.2k lines leave. *Open questions* 14 covers the serving
  thread that D2.6d and A6-1 both need.
- **F-02 (C7).** Phase B item 1 notes that a critical deploy-pipeline or `flux` alert gates
  the tag its `service` names. D2's `service="deploy-pipeline"` names no tag, so it gates
  nothing, and C7 admits the literal. Item 6 notes that `alert-history.json` groups them by `service`.

### Amendment for a flux-owned Phase C (2026-10-22, #5821)

The operator ruled on 2026-10-21 that D5 flips uptime-kuma with janitorr and jellyfin (#5820),
overriding this plan's "uptime-kuma never flips". observability had flipped in #5816 and
netpol-baseline flipped last in Wave D (#5833), both before C-1. Not adversarially reviewed.

- *When D5 flips a role M6 edits* keeps only the monitor-bridge edge. It records that
  netpol-baseline's `dependsOn: uptime-kuma` orders C-1's netpol-baseline half, and that
  observability and uptime-kuma share no edge. It rejects a `depends_on` edge and a
  handback.
- C-1 splits into C-1a (`KumaDown` and the scrape job) and C-1b (the empty render), with two
  live reads between them. C-1b is D2's first retirement PR, with no tombstone and with its
  claims kept, and C-2b is its second. Both name the operator's `cluster_vars.py write`.
- D6's wait on C-2b becomes an index edge instead of a consequence of uptime-kuma staying
  Ansible-owned.
- Updated: exit criterion 1, *Order*, Phase C, *The tree*, *Steps*, *Rollback*, *Risks*,
  *Complexity delta*, Open questions 1 and 11.

### Amendment for host_packages and a flux-owned bridge (2026-10-31)

The A6 soak read of 2026-10-31 (*Status by batch*, A6) found three facts the plan did not know.
The operator ruled on two of them the same day. Not adversarially reviewed.

- **A 20th bridge row, `host_packages`.** H3.5c added it after this plan was written. It is
  the tile "Host Packages — Status", `gate="prometheus"`, and a member of
  `EXPORTER_DEPENDENT["node"]` in `gates.py`, because its builder arms read series that
  daniel-box's node exporter publishes. A6 therefore covers 17 non-gate rows and 3 gate rows,
  and retires 20 tokens, not 19. Every Phase A total that counts A6 grows by one.
    - **The rule.** `HostPackageBuilderStalled` in `bridge-exporter.yaml`, `warning`,
      `service: host-packages`, shadow, no `for:` and no `requires` label. Its expression
      ends in `unless on() (up{job="node"} == 0)`, which keeps the bridge's estate-wide
      suppression, so `ScrapeTargetDown` is the one page for a dead exporter.
      `test_bridge_prometheus_gated_rules.py` derives the family from the `gate` column.
    - **Its soak.** The 7-day soak of M1 convention 6 starts when the rule's `observability`
      apply lands. A6-7a and A6-7b wait for it. The A6 soak read of 2026-10-31 found 364
      minutes of the tile reading DOWN with no rule, so that soak is not a substitute.
    - **A6-7r keeps the row's behaviour.** `Check.gate` carries the `prometheus` gate and the
      exporter suppression moves into `check.py` as an `EXPORTER_DEPENDENT` entry for
      `host_packages`, with its `DECIDED` marker. The plan's "empty after A1 to A5" was wrong
      for both sets, so A6-7r no longer deletes `down_exporters` and `EXPORTER_DEPENDENT`.
      The row stays an exporter row after A6-7a, so the entry and the marker stay with it
      until the row is deleted.
- **`for: 10m` for the `startup_grace` family**, two bridge cycles, in place of `for: 5m`
  (*Gates and grace*). The rules stay shadow, so the soak measures the final rule.
- **monitor-bridge and uptime-kuma are `owner: flux`.** The plan's "monitor-bridge flips
  after A6-7a has deployed" and its `land.sh` lines for A6-7r, A6-7a and A6-7b no longer
  hold. A merge is the apply, and a live read waits for the Kustomization to report the
  merge commit. A6-7a takes A7's split form, as A7's Cutover-a1 and Cutover-a2 did:
    - **A6-7a1, the tiles.** Exclude the bridge tiles in the `CHECKS` loop of
      `static-monitors.yaml.j2` and remove the bridge pins from
      `status-page-sync-configmap.yaml.j2`, with every rule still shadow. The row stays, so
      the bridge pushes into deleted tokens and `swallowed_verdicts` must not page them. A7's
      Cutover-a1 added a tolerated-orphan set (`A7_ROWS_AWAITING_DELETION`) for this, and
      A6-7a1 extends it. The PR records `monitor_status{monitor_name="<tile>"}` returning
      nothing for each tile.
    - **A6-7a2, the rows.** Delete the push path, `apply_startup_grace`, the `KUMA_*` and
      `GRACE_CYCLES` settings, the `prometheus` row and `check_prometheus`, and add the
      bijection test. It follows a read that every tile is gone.
    - **A6-7b** stays stacked, open and green before A6-7a1 merges, under the two-hour bound
      from A6-7a1's deploy.
