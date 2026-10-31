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

**Cutover-2: the push stops.** The two producers are timers in daniel-box's host package, moved
there by H7.40's fourth slice ([#6788](https://github.com/DanielH2018/server/pull/6788)), so
this Cutover-2 rides in that PR instead of editing the cron scripts a second time. Cutover-1a
([#6784](https://github.com/DanielH2018/server/pull/6784)) deleted both tiles, and Cutover-1b
([#6787](https://github.com/DanielH2018/server/pull/6787)) makes the four rules page.
`sync-artifacts` and `claude-otel-health` drop the `kuma-push-lib.sh` source, the push URL and
the token. Each keeps `write_check_textfile` and logs `status=<up|down> <message>` under its own
syslog tag. The sync script did not log a clean run before, and the heartbeat did not either,
because `kuma_push` logged only a failed push, so the journal line is new for both.

Neither unit loads a credential now, so `package.yml` drops `artifacts_sync_push_token` and
`claude_otel_push_token` from `credentials:`, and the SOPS file needs no `host_secrets.py sync`.
Both keys stay in `PENDING_RETIREMENT`. B6a's token PR retires them a week after Cutover-1b.

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

**The test.** `hosts/lib/tests/test_claude_artifacts_node_timers.py::test_neither_producer_pushes_to_kuma`
holds both shipped scripts to no Kuma library, `kuma_push` or push URL. Its red case is
`test_a_script_that_pushes_to_kuma_is_flagged`. The two script tests read the verdict back from
the journal line a `logger` stub records, in place of a stubbed `kuma_push`.
