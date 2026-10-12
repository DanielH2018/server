# M5 — loki-homelab ruler

Status: PLANNED

Depends on: M1, and M2's PR 3 (the `weekly-reboot` mute) before the cutover. Two
contracts.md rows also gate PRs here: the row *Open questions* 2 requests gates PR 2a, and
the row *Open questions* 1 requests gates PR 4b. contracts.md's *Requests on C7* holds both
rows, each with that gate, so each PR merges once its row is there or applied.

Unblocks: M6 (with M2, M3 and M4). M6's A7 shadow PR waits on PR 3, which adds the rule-file
census that A7's selector joins. No contracts.md row gates M6 on M5. contracts.md's Round 2
(X2-11) records M5's handoff to M6 as "applied with no contract text", and M6's own text
carries each part of it (*Open questions* 4 and 5):

- the `swallowed_verdicts` row and its push token, which M6's C-7 deletes;
- the `KUMA_RETIRING_TAGS` exclusion, which each B shadow PR deploys before its Cutover-1a;
- the deletion of `LokiHomelabDown`, which M6's A3 Cutover-b makes (A3 item 5).

This slice turns on loki-homelab's ruler. It moves two of monitor-bridge's three Loki-reading
checks into LogQL rules and deletes the Loki gate that suppressed them. The third,
`swallowed_verdicts`, stays on the bridge until M6 deletes host pushes, its subject (*Open
questions* 4). The rules send to Alertmanager. C7 confines log rules to loki-homelab, because
the observability Loki holds Claude Code transcripts. The slice follows the conventions in
[M1](m1-alertmanager.md#conventions-every-monitoring-slice-follows), which this page calls
"M1 convention N", and departs from convention 2 in one place, stated under *The rules*.

## Goal and exit criterion

**Goal.** loki-homelab evaluates LogQL rules from git and sends the result to Alertmanager. The
bridge rows `loki_ingestion` and `kuma_notify_failures` and the `loki_reachable` gate are gone.
`swallowed_verdicts` stays on the bridge, gated by `startup_grace`. The selector census holds
each rule selector to what Alloy emits:

- its label names to the labels the Alloy streams carry;
- its `job` and `machine` values to `loki_streams`;
- its `container` values to a container name that a rendered template declares;
- each silence rule to its side of the Pi boundary: a cluster rule matches no `daniel-pi`
  stream, and the Pi rule matches no cluster stream.

Each selector also matched a live stream when its rule landed.

**Exit criterion.** All four hold:

1. `probe.py metric` reads the group-rules gauge for the `homelab-logs` group with 4 rules.
   The count holds at M5's end only, because M6 edits the file (*Open questions* 6). The age
   of its last evaluation is under 10 minutes, twice the group's `interval: 5m`. Both metric
   names and the `rule_group` label value are the ones PR 2a measured and recorded.
2. The PRs recorded that the ruler delivered to Alertmanager. Only a firing rule makes the
   ruler send, so delivery is proven by these records, not by a reading at slice end:
    - PR 2a recorded the notifier's error counter rising while the grant was absent.
    - PR 2b recorded `increase(loki_prometheus_notifications_sent_total[15m])` above 0 while
      `LokiRulerSmoke` fired. The group sends once per 5m interval, so a 15m window holds at
      least two sends.
    - `alertmanager_alerts{state="active"}` rose by one when the first send landed, and fell by
      one after PR 3 removed `LokiRulerSmoke`.

   At slice end, two readings prove the configuration only:
    - `loki_prometheus_notifications_alertmanagers_discovered` reads 1. Loki v3.7.8's
      `pkg/ruler/base/notifier.go:amConfigFromURL` builds a static config from
      `alertmanager_url` when discovery is off, so the gauge reads 1 whether or not the
      Service answers.
    - `increase(loki_prometheus_notifications_errors_total[5m])` reads 0, read from two minutes
      after a Loki pod starts. A ruler that sends nothing reports no error, so this reading
      also holds with the grant deleted.

   After PR 3, a delivery break shows only at the next real send, through
   `LokiRulerNotificationsFailing` (*Risks*).
3. `ansible/tests/services/test_loki_stream_labels.py`'s consumer census includes the rule
   file, and its named members include one rule. Its rule-file checks cover label names,
   `job`, `machine` and `container` values, and the Pi boundary. PR 3 recorded a non-zero
   `probe.py loki-query 'count_over_time(<selector>[24h])'` for each selector, and a non-zero
   count of `KumaNotificationDropped`'s filtered expression over a stated window.
4. The three rows are no longer in `CHECKS`, and Kuma exports no `monitor_status` for their
   tiles. `swallowed_verdicts` names `startup_grace` in its `gate` column. No alert in
   `homelab-logs.yaml` or `loki-homelab.yaml` carries `shadow`, except `LokiHomelabDown`. M6's
   A3 Cutover-b deletes that rule, and A3's warning scrape-target rule then pages job
   `loki-homelab` (M6 *A3 and the handoffs it closes*, item 5). The three push tokens are
   gone from `ansible/vars/secrets.yml` and `ansible/secret_rotation.yml`, and
   `ansible/tests/k8s/test_secret_consumer_census.py` passes.

## Baseline at `70591c307`

- **The ruler block exists and is empty.** `loki-homelab-config` in
  `ansible/roles/k8s/loki-homelab/templates/configmap.yaml.j2` sets `ruler.storage.type: local`
  with `directory: /loki/rules`, and `common.storage.filesystem.rules_directory: /loki/rules`.
  It has no `alertmanager_url`, no `enable_api` and no rule files. `/loki` is the
  `loki-homelab-data` claim, and `/etc/loki` is the config ConfigMap's mount.
- **The image is grafana/loki 3.7.8** (`loki_homelab_k8s_image`, the same pin as the
  observability Loki). Retention is `loki_homelab_retention: 744h`.
- **Loki's local ruler storage**, from `docs/sources/alert/_index.md` at `v3.7.8` (read with
  `gh api`):
    - It reads `<directory>/<tenant>/`, and the tenant is `fake` when `auth_enabled` is false.
    - It parses every non-hidden file there and ignores subdirectories.
    - `rule_path` is a separate scratch directory.
    - `lokitool rules lint --dry-run --rule-files=<f>` parses LogQL offline. The flag is in
      `pkg/tool/commands/rules.go` at that tag.
    - The configuration reference (`docs/sources/shared/configuration.md`) gives the ruler's
      `evaluation_interval` a default of 1m. A rule group's own `interval` overrides it.
- **The ruler's metrics exist only once a rule group loads.** Read with `gh api` at `v3.7.8`:
    - `pkg/ruler/base/manager_metrics.go` names the per-group series
      `<namespace>_prometheus_rule_group_rules`,
      `<namespace>_prometheus_rule_group_last_evaluation_timestamp_seconds` and
      `<namespace>_prometheus_rule_evaluation_failures_total`, labelled `user` and
      `rule_group`.
    - `pkg/ruler/base/manager.go:DefaultMultiTenantManager.newManager` registers a tenant's
      metrics registry and creates its notifier. The notifier's counters carry the same
      namespace prefix and a `user` label.
    - `DefaultMultiTenantManager.syncRulesToManager` calls `newManager` only for a tenant that
      has a rule group. With no group loaded, none of these series exist.

  This plan writes the namespace as `loki`, Loki's documented prefix. PR 2a loads one rule,
  then records every name and the exact `rule_group` value from
  `probe.py metric '{__name__=~"loki_prometheus_.*"}'`. No rule cites a name before that.
- **A rules edit restarts Loki and Alloy.** The role uses the shared rollout. Its role
  `CLAUDE.md` says the Alloy DaemonSet rolls whenever the role's ConfigMap changes, alongside
  the Loki Deployment (`manifests_extra_rollouts`). So every rule edit restarts both. The Loki
  Deployment is `Recreate`, so each restart is a new pod with a new address.
- **The four rows and their logic:**
    - **`loki_ingestion`** (`checks/logs.py:check_loki_ingestion`, gate `loki_reachable`, not
      critical) runs three arms, each a line count over a window that pages on zero:
        - `LOKI_STREAM`, the authlog and syslog union excluding the Pi, over
          `LOKI_FILETAIL_WINDOW` (3h).
        - `LOKI_DOCKER_STREAM`, `{job="k8s"}`, over `LOKI_WINDOW` (30m).
        - `LOKI_PI_STREAM`, `{job="pi"}`, over 3h.

      The selectors render from `loki_streams` in `ansible/inventory/group_vars/all.yml`
      through `templates/env-secret.yaml.j2`.
    - **`swallowed_verdicts`** (gate `loki_reachable`, not critical) reads
      `SWALLOWED_VERDICTS_LOGQL` and `SWALLOWED_VERDICTS_POD_LOGQL` over 3h. For each logger
      tag, the newest push-outcome line decides. It pages when a tag's newest line is a failed
      `status=down` push while another tag landed a push in the same window, or when Kuma
      itself refused the push (`by=kuma`) (`verdicts/logs.py:swallowed_verdicts`). Three
      properties of it have no LogQL counterpart:
        - The verdict excludes `verdicts/logs.py:HC_ROUTED_TAGS`, whose own scripts page a
          lost push through Healthchecks.io.
        - `checks/logs.py:check_swallowed_verdicts` never reads back past the node's boot
          (#2783). Inside `BOOT_SETTLE_S` it skips the cycle, and after it the window shrinks
          to the time since the settle. Its comment records that the fixed 3h window held the
          tile red for 2.9h after the weekly restart.
        - It fails open on a Loki error, returning up with "Loki Reachable owns a Loki fault".
    - **`kuma_notify_failures`** (gate `loki_reachable`, `critical`) reads
      `KUMA_NOTIFY_FAILURES_LOGQL` over 3h and pages on any drop. That filter has two
      alternatives, `{container="uptime-kuma"} |~ "Cannot send notification|ERROR:.{0,8}
      Error: "`. The first selects drop lines and the second selects reason lines.
      `verdicts/logs.py:kuma_notify_failures` pages only when `parse_notify_failure_line` read
      a drop. A reason line that `parse_notify_reason_line` reads only adds to the message. The
      comment above the constant records a measurement on 2026-09-17: 148 lines over 14 days,
      74 drops and 74 reasons. On 2026-10-11, `probe.py loki-query --since 1m
      'sum(count_over_time({container="uptime-kuma"} |= "Cannot send notification" [14d]))'`
      returned 41.
    - **`loki_reachable`** is a gate row. It probes `/loki/api/v1/labels` and suppresses the
      three rows above while Loki is down. Its wiring reaches past the row:
        - `files/gates.py` imports `checks.logs:check_loki_reachable`, derives
          `LOKI_DEPENDENT`, and carries `Gates.loki_dependent`, `Gates.probe_loki` and the
          `loki_reachable` key of `Gates.gate_dependents`.
        - `files/check.py:run_once` runs the gate through `gate_lib._gate` and skips a member
          of `gates.loki_dependent` while it is down.
        - The docstring of `files/bridge/types.py:PushCheck` names it among the gates.
        - Comments name it in `files/check_table.py`'s module docstring,
          `files/bridge/config.py`, the `DECIDED` marker in `files/checks/service.py` and
          `netpol-baseline/templates/networkpolicy-loki.yaml.j2`.
        - Tests name it: `tests/_check_gate_helpers.py`, `test_check_table.py:_GATES`,
          `test_check_gate_dependents.py`, `test_check_gates.py`, `test_check.py`,
          `test_check_b2_gate.py`, `test_check_gates_exporters.py`, `test_check_streaks.py`,
          `test_check_cli.py`, a comment in `test_check_ha.py`, and
          `test_check_swallowed_verdicts.py`, which asserts that `swallowed_verdicts` is in
          `gates.LOKI_DEPENDENT`. Outside the role,
          `scripts/docs/tests/test_fragments_bridge.py:test_every_non_gate_check_is_in_exactly_one_gate_set_row`
          asserts that `gates.py` declares a `loki_reachable` set, which
          `scripts/docs/fragments_lib/fragments_bridge.py:read_gate_set_names` reads into the
          gate-set fragment.
- **The ingestion arms do not count the Pi (#3739).** `monitor-bridge/tests/test_check_loki.py`
  derives `_PI_STREAMS` from `loki_streams` and matches selectors with `_selector_matches`.
  `test_cluster_arm_counts_no_pi_stream` and its flagged twin hold `LOKI_STREAM` and
  `LOKI_DOCKER_STREAM` off both Pi streams. A cluster arm that also matched a Pi stream would
  keep its count above zero through a total cluster outage. The tests read the two `Config`
  fields that PR 4a removes.
- **The bridge's Loki keys.** `bridge/config_io.py` declares and loads `LOKI_STREAM`,
  `LOKI_DOCKER_STREAM`, `LOKI_PI_STREAM`, `LOKI_WINDOW`, `LOKI_FILETAIL_WINDOW` and
  `LOG_ERROR_SELECTOR` through `_env`, and `SWALLOWED_VERDICTS_WINDOW_S`, `BOOT_SETTLE_S` and
  `KUMA_NOTIFY_FAILURES_WINDOW_S` through `_int`. `templates/env-secret.yaml.j2` renders each
  key. `LOKI_URL` is in `bridge/config.py`. The role's `CLAUDE.md` says a missing value lands
  in `CONFIG_PROBLEMS` and `main()` exits 2, so a key dropped from the template while its field
  remains stops every check. `BOOT_SETTLE_S` has a second reader,
  `checks/logs.py:check_shipper_dropped`.
- **The bridge's cadence.** It runs every `INTERVAL` (300 s, the role's `CLAUDE.md`). The
  docstring of `checks/logs.py:check_swallowed_verdicts` warns that a range query is the thing
  a busy Loki is slow at.
- **Prometheus already scrapes loki-homelab** as job `loki-homelab`, from the entry's `metrics`
  item in `host_vars/daniel-box.yml`. `probe.py metric 'count by (job) (up{job=~"loki.*"})'`
  returns `loki-homelab` and `loki-internal`.
- **A Loki outage already has a second Kuma pager.** `probe.py metric 'up{job="loki-homelab"}'`
  returns the labels `instance` and `job` only, with no `origin`. M1 convention 5 records that
  `checks/cluster.py:check_cluster_targets` selects `up{origin!="daniel-server"}`, so the
  `Cluster Scrape Targets` tile reads this job. Convention 5 forbids an unshadowed `up == 0`
  rule for such a job until M6's batch A3.
- **A Loki query fault also pages through Kuma.** `checks/logs.py:check_loki_reachable` raises
  when `/labels` does not return `success`, and its docstring says `_evaluate` then renders
  `Loki Reachable` down. While `/labels` answers, the gate passes and `check_loki_ingestion`
  runs. A failed count either raises into `_evaluate`, which pages (the role's `CLAUDE.md`), or
  reaches `verdicts/logs.py:loki_ingestion_fresh`, which returns down on `None` or 0. So until
  the tiles go, either `Loki Reachable` or `Loki Log Ingestion` pages a Loki that fails its
  queries.
- **The bridge's own pushes are outside `swallowed_verdicts`.** `checks/logs.py` selects
  `{job="syslog"}` in `SWALLOWED_VERDICTS_LOGQL` and `{container="pi-peer-backup"}` in
  `SWALLOWED_VERDICTS_POD_LOGQL`. A bridge push into a deleted tile's token is logged by the
  `monitor-bridge` pod, which neither selector reads.
- **A bridge tile's deadline is 1200s.** `uptime_kuma_k8s_bridge_push_interval` is 1200 in
  `ansible/roles/k8s/uptime-kuma/defaults/main.yml`, so a bridge tile goes DOWN 1200s after its
  last push.
- **Prometheus cannot see a Loki rule's alerts.** The ruler writes `ALERTS` only to a
  remote-write target. `prometheus.yaml.j2` deliberately runs without
  `--web.enable-remote-write-receiver`, and its comment records why. So C7's health gate,
  which reads Prometheus `ALERTS`, cannot see a log rule's alert. *Open questions* raises this.
- **The route admits only `/loki/api/v1/`.**
  `ansible/roles/k8s/loki-homelab/templates/ingressroute.yaml.j2` restricts the LAN route to
  that prefix, behind its ClientIP guard. A host can read `/loki/api/v1/rules` but not the
  ruler's `/prometheus/api/v1/alerts`.
- **The label vocabulary is guarded for `job` values only.** `test_loki_stream_labels.py`
  holds every consumer's selectors to the owner values:
    - Its consumers are the bridge env-secret, the `*_LOGQL` constants, the loki-homelab
      dashboards and the deploy annotation.
    - Its named-member census is `test_the_consumer_census_finds_its_named_members`.
    - `_selected_jobs` reads only `job` matchers, and `_unknown_jobs` compares only those. A
      `container`, `machine` or `app` matcher passes it unchecked.
    - The label-name guard is
      `monitor-bridge/tests/test_check_loki.py:test_loki_selectors_use_real_stream_labels`,
      over `LOKI_STREAM_LABELS` in the same file. It reads only the bridge's cfg selectors.
- **`probe.py loki-query` sets no step.** `probe_lib/obs_api.py:loki_query_url` builds a
  `query_range` URL from the query, limit, start, end and direction. The subcommand's flags are
  `--loki`, `--limit`, `--since` and `--json`, and `--since` defaults to Loki's one hour. Loki
  v3.7.8's `pkg/loghttp/params.go:defaultQueryRangeStep` (read with `gh api`) then picks
  `floor(range / 250)` seconds, so a 7-day range evaluates once every 2419 s. The default view,
  `probe_lib/metrics.py:format_loki`, prints only the value of each sample.
- **Cross-namespace reach.** loki-homelab runs in `homelab`, and Alertmanager runs in
  `observability`. The observability baseline admits only Traefik, its own namespace and the
  node addresses (`networkpolicy-observability.yaml.j2`). The ruler's push is refused today.
  kube-router admits a newly created source pod only after a delay
  ([ADR 0009](../../adr/0009-networkpolicy-default-deny-ingress.md)).

## Design

### Ruler configuration

The `ruler:` block in `loki-homelab-config` gains these settings:

```yaml
ruler:
  storage:
    type: local
    local:
      directory: /etc/loki-rules        # the rules ConfigMap; /loki/rules is left unused
  rule_path: /loki/rules-scratch        # scratch, on the claim
  alertmanager_url: http://alertmanager.<observability namespace>.svc:9093
  enable_api: true
```

`common.storage.filesystem.rules_directory` follows the same directory. A new ConfigMap,
`loki-homelab-rules`, has one key per rule group, `<group>.yaml`. A new role default,
`loki_homelab_rule_groups`, lists the groups, `[homelab-logs]`. `templates/rules-configmap.yaml.j2`
loops over that list and reads each file with
`lookup('file', playbook_dir + '/roles/k8s/loki-homelab/files/rules/' + group + '.yaml')`, the
form M1 convention 1 gives `observability_rule_groups`. It never globs, because the offline
renderer supports no `fileglob` (M1 *Baseline*). A file that the list does not name never
reaches the ConfigMap. So `alert_rules.py`'s lokitool phase fails when the stems in
`loki-homelab/files/rules/`, `loki_homelab_rule_groups` and the rendered `loki-homelab-rules`
keys are not one set. That check has an `_is_clean` and `_is_flagged` pair; the flagged
fixture is a rule file the list does not name. The ConfigMap mounts at
`/etc/loki-rules/fake`, a whole-directory mount outside `/etc/loki`, so the config volume's
atomic-writer swap cannot remove it. The ruler polls
that directory each minute, but the role-level rollout restarts Loki on every rules edit anyway.
*Risks* prices that.

The `homelab-logs` group sets `interval: 5m`, the bridge's `INTERVAL`. The ruler then issues
the bridge's query load, not five times it under the 1m default. Two values derive from the
interval: exit criterion 1's freshness bound (two intervals, 10m) and `LokiRulerNotEvaluating`'s
threshold (the same 10m).

### The cross-namespace grant

`ansible/roles/k8s/netpol-baseline/templates/networkpolicy-alertmanager.yaml.j2` is a new
bespoke policy. It opens with a `DECIDED` marker naming its shape: a cross-namespace peer,
which the callers model cannot express. The `bespoke-netpol-says-why-it-is-not-a-callers-fence`
census row requires that marker. The policy admits `app: loki-homelab` from the `homelab`
namespace to `app: alertmanager` on 9093, and it is added to the role's deferred observability
files, beside `networkpolicy-prometheus.yaml.j2`. M1's netpol probe row for `alertmanager`
stays `refused`, because the probe pod is not loki-homelab.

The grant lands in PR 2b, after PR 2a turns the ruler on. PR 2a can then observe the refused
sends, which is the grant's red half.

### The rules

`files/rules/homelab-logs.yaml` holds group `homelab-logs`, with `interval: 5m`, as a Loki
rule file with LogQL expressions. Each alert carries `shadow: "true"` until PR 4b.

| Bridge row | Alert | Expression, in words | `for` | Severity | `service` |
|---|---|---|---|---|---|
| `loki_ingestion`, host arm | `LokiHostLogsSilent` | `absent_over_time` of the authlog and syslog union, minus the Pi, over 3h | — | `warning` | `loki-homelab` |
| `loki_ingestion`, pod arm | `LokiPodLogsSilent` | `absent_over_time({job="k8s"}[30m])` | — | `warning` | `loki-homelab` |
| `loki_ingestion`, Pi arm | `LokiPiLogsSilent` | `absent_over_time({job="pi"}[3h])` | — | `warning` | `daniel-pi` |
| `kuma_notify_failures` | `KumaNotificationDropped` | `count_over_time` of `{container="uptime-kuma"}` lines that contain `Cannot send notification`, over 3h, `> 0` | — | `critical` | `uptime-kuma` |

Every `service` is a literal, so M1's label check passes with no derivation.

`KumaNotificationDropped`'s expression is
`count_over_time({container="uptime-kuma"} |= "Cannot send notification" [3h]) > 0`. It
keeps only the drop half of `KUMA_NOTIFY_FAILURES_LOGQL`. The
bridge fetches the reason lines too, but its verdict pages only on a drop line, and a reason
line only adds to the message (*Baseline*). No annotation templates line text (*Risks*), so a
reason alternative would add nothing to the rule. It would make the rule fire on any Kuma
`ERROR` line that carries `Error: `, with no drop. The substring is a little wider than
`verdicts/logs.py:_NOTIFY_FAILURE_RE`, which also needs ` to <name>` after it. PR 3 therefore
records the 14-day count of the rule's filter beside the count of
`|= "Cannot send notification to "` over the same window, and the two must agree. From PR 2a to
PR 3 the file holds a single temporary rule instead: `LokiRulerSmoke`, `expr: vector(1)`, with
`shadow: "true"`, `service: loki-homelab` and `severity: info`.

`files/rules/loki-homelab.yaml` is a Prometheus group in the `observability` role, listed in
`observability_rule_groups` (M1 convention 1). It replaces the gate and watches the ruler
itself. Each alert carries `service: loki-homelab`. `<recorded>` stands for the `rule_group`
value PR 2a records.

| Alert | Expression, in words | `for` | Severity | `shadow` |
|---|---|---|---|---|
| `LokiHomelabDown` | `up{job="loki-homelab"} == 0` | 5m | `warning` | until M6's A3 Cutover-b deletes it |
| `LokiHomelabMetricsMissing` | `absent(up{job="loki-homelab"})` | 10m | `warning` | none |
| `LokiRulerEvaluationFailing` | `increase` of the evaluation-failure counter for `<recorded>` over 10m, `> 0` | 15m | `warning` | until PR 4b |
| `LokiRulerNotEvaluating` | the age of `<recorded>`'s last evaluation exceeds 10m, or that gauge is `absent()`, `and on() up{job="loki-homelab"} == 1` | 10m | `warning` | until PR 4b |
| `LokiRulerNotificationsFailing` | `increase` of the notifier's error counter over 15m, above the threshold PR 3 sets from PR 2a's step | 5m | `critical` | none |

Each of the five has a reason for its shape:

- **`LokiHomelabDown`** is `up == 0` only. It stays shadow until M6's A3, because the
  `Cluster Scrape Targets` tile pages the same `up` series (*Baseline*, M1 convention 5). M6's
  A3 Cutover-b deletes it with its promtool and routing cases, because A3's scrape-target rule
  pages the same series with `service: loki-homelab` (M6 A3 item 5, *Open questions* 5). It
  never pages.
- **`LokiHomelabMetricsMissing`** is the family's zero-series alert, and the empty-input
  promtool case fires it (M1 convention 2). A renamed or dropped `loki-homelab` scrape job
  would otherwise silence every rule in this group. No Kuma tile reads a missing scrape
  job, so it pages from PR 1.
- **`LokiRulerEvaluationFailing`** fires only when failures recur. A one-off failure keeps
  the 10m increase above zero for 10 minutes, which is less than the 15m `for`. Each
  evaluation is a Loki query, so this alert also covers a Loki that answers its scrape but
  fails its queries, the case `Loki Reachable`'s `/labels` probe covered. It stays shadow
  until PR 4b. Until PR 4a deletes them, `Loki Reachable` and `Loki Log Ingestion` page the
  same fault (*Baseline*), and M1 convention 5 allows one pager.
- **`LokiRulerNotEvaluating`** stays silent while Loki itself is down or starting. The `up ==
  1` conjunct removes the outage, which is `LokiHomelabDown`'s condition. The `for` must exceed
  the gap from a Loki restart to the group's first evaluation, because the last-evaluation
  gauge reads the zero time until then. PR 2a measures that gap, and PR 3 raises the `for` if
  it exceeds 10m.
- **`LokiRulerNotificationsFailing`** needs its threshold held for 5m. Every Loki restart is
  a new source pod, and kube-router can refuse its first sends (*Baseline*). One refused send
  must not page `critical`. A broken delivery path must page, and after PR 3 only a firing
  log rule sends, once per 5m interval. With one rule firing, three failed sends fall inside
  15m, so `> 2` would be crossed only narrowly, if at all. PR 2a therefore records how far the
  counter steps per refused send while `LokiRulerSmoke` alone fires. PR 3 sets the threshold
  from that step: one firing rule's failed sends over 15m cross it, and one refused send does
  not. One firing rule cannot show whether the counter counts sends or alerts. Several rules
  firing across a restart's refused send differ in the two cases, so PR 3 reads which one
  holds from the notifier that Loki v3.7.8 vendors before it sets the threshold.

**A Loki outage pages once.** A dead Loki evaluates no log rule, and `LokiRulerNotEvaluating`
requires `up == 1`. Until M6's A3 the pager is Kuma's `Cluster Scrape Targets` tile. From A3's
Cutover-b it is A3's warning scrape-target rule, which pages job `loki-homelab` after 10
minutes. The same Cutover-b deletes `LokiHomelabDown` (*Open questions* 5). No inhibit rule is
needed.

**The departure from M1 convention 2.** LogQL has no `promtool test rules` counterpart, so the
`homelab-logs` alerts have no unit test that makes them fire. Each one gets these checks
instead:

- `lokitool rules lint --dry-run`, run from `alert_rules.py`, which parses every expression.
- The selector census.
- A live evaluation of its expression as a Loki metric query in PR 3's verification.
- For `KumaNotificationDropped`, the only rule with a line filter, a non-zero count of its
  inner `count_over_time` over a window that holds known drops. An empty result from a
  mistyped filter looks the same as a quiet week, so the proof needs real matching lines. The
  proof runs the rule's own filter, `|= "Cannot send notification"`, not the bridge's.

`alert_rules.py` exempts the Loki rule directory from its fires-in-a-test check by name. The
Prometheus group above follows convention 2 in full.

**Why `swallowed_verdicts` stays on the bridge.** A LogQL count over a fixed 3h cannot express
the three properties *Baseline* lists. It would page every Sunday when M2's `weekly-reboot`
mute ends at 08:15, for a push lost during the restart that stays inside the 3h range for
about 2.5 hours more. That is the page #2783 fixed. It would also page a second time for the
three `HC_ROUTED_TAGS` tags, which M1 convention 5 forbids. The bridge check already fails
open on a Loki error, so it needs no reachability gate. PR 4a changes its `gate` to
`startup_grace`, as M2 does for `workload_log_errors`. Its subject, host pushes to Kuma, ends
in M6, which deletes the row with them.

### The selector census

`test_loki_stream_labels.py` gains a source, `_loki_rule_exprs()`, which reads every `expr` from
the files in `ansible/roles/k8s/loki-homelab/files/rules/`. It joins `_consumers()`, and the
named-member census gains `LokiPodLogsSilent`. A selector naming a `job` no stream emits then
fails `test_every_consumer_selects_only_owner_values` in CI.

The rule-file source gets four more checks, because `_selected_jobs` reads `job` alone:

- **Label names.** Every label a rule selector matches on is in the set of labels the Alloy
  streams carry. PR 3 moves that derivation out of `monitor-bridge/tests/test_check_loki.py`
  into a stream-label helper both tests import, placed as `.claude/rules/python-layout.md`
  directs. The derivation is `_LOKI_STREAM_LABELS_OBSERVED` plus `_alloy_relabel_targets`,
  which together build `LOKI_STREAM_LABELS`.
- **`machine` values.** Each value a `machine` matcher names, with `=` or `!=`, is a
  `machine` value in `loki_streams`. A negative matcher on a value no stream carries excludes
  nothing.
- **`container` values.** The census maps each `container` value the rules use to the role
  whose template declares it, `uptime-kuma` to the uptime-kuma Deployment. It renders that
  template through `ansible/tests/_k8s_render.py:render_role_template` and asserts that the
  container name is there. A value with no mapping fails.
- **The Pi boundary (#3739).** PR 3 also moves `_PI_STREAMS` and `_selector_matches` from
  `test_check_loki.py` into the stream-label helper. The check maps each silence rule to the
  `loki_streams` roles it watches: `LokiHostLogsSilent` to `host_syslog` and `host_authlog`,
  `LokiPodLogsSilent` to `cluster_pods`, and `LokiPiLogsSilent` to `pi_containers`. Each rule
  must match its own streams. A cluster rule must match no Pi stream, and the Pi rule must
  match no cluster stream. A cluster rule that also counted a Pi stream would stay above zero
  through a total cluster outage and never fire. This check carries the property that PR 4a's
  deletion of the bridge's cluster-arm tests would otherwise drop.

Each check has an `_is_clean` and `_is_flagged` pair. The flagged fixtures are an `app=`
matcher, a `container` value no template declares, a `machine` value outside `loki_streams`,
and the pre-#3739 host selector `{job=~"authlog|syslog"}`, which also matches `pi_health`.
The rule file's selectors are literals copied from `loki_streams`, and this
census, not a Jinja render, keeps them equal (M1 convention 1). PR 4a removes the bridge's
ingestion selectors from the census. In the same commit,
`test_the_consumer_census_finds_its_named_members` drops `"env-secret LOKI_STREAM"`. It keeps
`"env-secret LOG_ERROR_SELECTOR"` and the rule-file member PR 3 added. `LOG_ERROR_SELECTOR`
stays until M6's A7, and `test_loki_selectors_use_real_stream_labels` stays with it.

### The tree

```diff
 ansible/
~  vars/secrets.yml                       loses three push tokens (PR 4a, operator-only)
~  secret_rotation.yml                    their three rows deleted (PR 4a, the operator's commit)
 ansible/roles/k8s/uptime-kuma/
~  templates/static-monitors.yaml.j2      the discord comment's reason for the name (PR 4a)
~  CLAUDE.md                              the same reason, in the Discord-template section (PR 4a)
 ansible/roles/k8s/loki-homelab/
~  defaults/main.yml                      loki_homelab_rule_groups; the readers comment (PR 4a)
~  CLAUDE.md                              the readers bullet names the ruler (PR 4a)
~  templates/configmap.yaml.j2            the ruler block
+  templates/rules-configmap.yaml.j2      loki-homelab-rules, a loop over that list
~  templates/deployment.yaml.j2           the /etc/loki-rules/fake mount
+  files/rules/homelab-logs.yaml          group homelab-logs, interval 5m
 ansible/roles/k8s/observability/
~  defaults/main.yml                      loki-homelab joins observability_rule_groups
+  files/rules/loki-homelab.yaml
+  tests/rules/loki-homelab.test.yaml
 ansible/roles/k8s/netpol-baseline/
+  templates/networkpolicy-alertmanager.yaml.j2
~  templates/networkpolicy-loki.yaml.j2   comment: the deleted check bodies no longer named
 ansible/roles/k8s/monitor-bridge/
~  files/check_table.py                   three rows removed; swallowed_verdicts gated by startup_grace
~  files/checks/logs.py                   three bodies removed
~  files/verdicts/logs.py                 the ingestion and notify-failure verdicts removed
~  files/gates.py                         the Loki gate's import, set, fields and gate_dependents key
~  files/check.py                         run_once's Loki gate call and its skip branch
~  files/bridge/types.py                  PushCheck's docstring lists three gates
~  files/bridge/config_io.py              six Config fields and their loads removed
~  files/bridge/config.py                 comment lists three gates
~  files/checks/service.py                the DECIDED marker no longer names LOKI_DEPENDENT
~  templates/env-secret.yaml.j2           the same six keys removed
~  tests/_check_gate_helpers.py           loki_result, loki_dependent and probe_loki removed
~  tests/test_check_*.py                  ten files: the Loki gate's arguments and assertions
-  tests/test_check_kuma_notify_failures.py
~  tests/test_check_loki.py               ingestion, reachability and Pi-boundary tests removed;
                                          two roster known-sets shrink (PR 4a)
 ansible/tests/services/
~  test_loki_stream_labels.py             the rule-file source and its four checks (PR 3);
                                          "env-secret LOKI_STREAM" leaves the named members (PR 4a)
~  test_alloy_pi_config_labels.py         docstring names LokiPiLogsSilent (PR 4a)
~  test_kuma_static_monitors.py           EMAIL_TIER loses Kuma Notification Delivery (PR 4a)
 scripts/secrets_mgmt/
~  git_dates.py                           RETIRED gains the three token names (PR 4a)
 scripts/diagnostics/probe_lib/
~  cli_parser.py                          loki-query --step
~  obs_api.py                             loki_query_url passes step
~  curl_pipeline.py                       the loki-query branch passes --step
 scripts/diagnostics/tests/
~  test_probe_curl_pipeline.py            the step reaches the URL
 scripts/docs/tests/
~  test_fragments_bridge.py               the gate-set names lose loki_reachable
 docs/
~  monitor-bridge-checks.md               three rows and the Loki gate out (PR 4a)
~  monitor-bridge-internals.md            the same (PR 4a)
~  uptime-kuma-autokuma-record.md         the drop line's reader is KumaNotificationDropped (PR 4a)
~  facts.lock                             only if a reworded CLAUDE.md section changes a citation (PR 4a)
 docs/assets/generated/fragments/
~  bridge-*.md                           rewritten by the regen-doc-fragments hook
~  secret-tiers.md                        the auto tier loses three rows (PR 4a)
 scripts/validate/
~  alert_tools.sh                         lokitool, at the loki_homelab_k8s_image tag
~  alert_rules.py                         the lokitool phase, its list agreement check, the Loki exemption
```

The ten `tests/test_check_*.py` files are `test_check_table.py` (`_GATES` and its probe map),
`test_check_gate_dependents.py`, `test_check_gates.py`, `test_check.py`,
`test_check_b2_gate.py`, `test_check_gates_exporters.py`, `test_check_streaks.py`,
`test_check_cli.py`, `test_check_ha.py` (a comment) and `test_check_swallowed_verdicts.py`,
whose gate assertion moves from `gates.LOKI_DEPENDENT` to `gates.STARTUP_GRACE`. The stream-label
helper that PR 3 adds is not in the tree above, because its path follows
`.claude/rules/python-layout.md` at implementation time.

## Steps, as PRs

PR 4 and PR 5 keep the numbers that M2 cites. The ruler and the grant are PR 2a and PR 2b. The
cutover is two PRs, PR 4a and PR 4b, for the reason M2's *The cutover is two PRs* gives. A
citation of M5's PR 4 in another slice means the pair, and PR 4a deletes the Loki gate. PR 4a
also retires the three push tokens, so a citation of M5's PR 5 means PR 4a's token part
(*PR 5*).

Before each deploy, a PR checks every tag it names against M1 convention 8. A flux-owned tag
is applied by the merge's `promote` run, and `land.sh` deploys the rest. Only PR 4a's order
depends on which tags are flux-owned, and PR 4a states each case.

### PR 1 — The Prometheus-side Loki rules (about 140 lines)

- Add `files/rules/loki-homelab.yaml` with `LokiHomelabDown` (shadow) and
  `LokiHomelabMetricsMissing`, and add `loki-homelab` to `observability_rule_groups` in the
  same commit.
- Add `loki-homelab.test.yaml`, with a firing case for each alert and the empty-input case,
  which fires `LokiHomelabMetricsMissing`.
- The three ruler-watching alerts wait for PR 3, which cites the names PR 2a records.
- Add `--step` to `probe.py loki-query`. `loki_query_url` gains an optional `step` that it
  omits when unset, so every other caller keeps Loki's default. Its test in
  `test_probe_curl_pipeline.py` asserts that `--step 5m` reaches the URL, and that the URL
  carries no `step` without the flag. PR 3's soak needs the flag (*Baseline*), and PR 1 is the
  first PR to land.
- Deploys `observability`.
- Two-writer check: `LokiHomelabDown` routes to the blackhole receiver. Kuma's `Loki
  Reachable` and `Cluster Scrape Targets` tiles stay the pagers for a Loki outage.
  `LokiHomelabMetricsMissing` pages, and no Kuma tile reads a missing scrape job.

### PR 2a — Turn on the ruler with one smoke rule (about 170 lines)

- Add the ruler block, `loki_homelab_rule_groups` naming `homelab-logs`,
  `loki-homelab-rules` holding `homelab-logs.yaml` with the single shadow rule
  `LokiRulerSmoke`, the mount, lokitool provisioning and `alert_rules.py`'s lokitool phase with
  its list agreement check (*Ruler configuration*). lokitool's version comes from
  `loki_homelab_k8s_image`, following M1's tool rule.
- Add `ansible/roles/k8s/loki-homelab/files/rules/` and
  `ansible/roles/k8s/loki-homelab/defaults/main.yml` to the `validate-alert-rules` hook's
  `files` regex. Otherwise a PR that edits only a LogQL rule skips the hook and merges a syntax
  error green.
- Requires the C7 row that *Open questions* 2 requests, in contracts.md's register or
  applied, because this PR sets `alertmanager_url`.
- Deploys `loki-homelab`. Loki and the Alloy DaemonSet restart once.
- Record in the PR, from `probe.py metric '{__name__=~"loki_prometheus_.*"}'`:
    - the full names of the group-rules, last-evaluation and evaluation-failure series, and of
      the notifier's sent, errors and discovered series;
    - the exact `user` and `rule_group` label values for `homelab-logs`;
    - the gap from the new pod's `process_start_time_seconds{job="loki-homelab"}` to the
      group's first non-zero last-evaluation timestamp;
    - the grant's red half: with no grant deployed,
      `increase(loki_prometheus_notifications_errors_total[10m])` is above 0;
    - how far the error counter steps per refused send while `LokiRulerSmoke` alone fires,
      which sets `LokiRulerNotificationsFailing`'s threshold in PR 3.
- Merge PR 2b soon after the red half is recorded. Nothing watches the refused sends yet.
- Two-writer check: the one rule is shadow, and its sends are refused, so nothing pages.

### PR 2b — The grant (about 50 lines)

- Add `networkpolicy-alertmanager.yaml.j2` and its deferred-file entry.
- Deploys `netpol-baseline`.
- The grant's green half is exit criterion 2. Read the error counter from two minutes after
  the deploy. Record `increase(loki_prometheus_notifications_sent_total[15m])` above 0 while
  `LokiRulerSmoke` fires. This is the slice's only reading of the sent counter, because no
  later rule is sure to fire. Record `alertmanager_alerts{state="active"}` before the deploy,
  and again once the first send lands. `LokiRulerSmoke` routes to the blackhole receiver.
- Two-writer check: no rule changes, so nothing new pages.

### PR 3 — The log rules in shadow (about 210 lines, 30 of them moved)

- Before the PR:
    - Time each 3h expression three times with `time uv run python scripts/diagnostics/probe.py
      loki-query '<expr>'`, and record the timings.
    - Evaluate each selector's 24h count, with `--since 1m`.
    - Evaluate `KumaNotificationDropped`'s inner `count_over_time`, with the rule's own filter
      `|= "Cannot send notification"`, over a 14-day window inside the 744h retention that
      holds known drops, and require a non-zero result. Evaluate `|= "Cannot send
      notification to "` over the same window, and require the same count.
    - Record each window and count in the PR.
- Replace `LokiRulerSmoke` with the four log alerts, all shadow, in `homelab-logs.yaml`.
- Add the census source, its named member and its four checks. Move the stream-label
  derivation, `_PI_STREAMS` and `_selector_matches` into the shared helper, and make
  `test_check_loki.py` import them from there.
- In `loki-homelab.yaml`, add the three ruler-watching alerts on the names and `rule_group`
  value PR 2a recorded, with their promtool cases. `LokiRulerNotEvaluating` and
  `LokiRulerEvaluationFailing` are shadow. `LokiRulerNotEvaluating`'s `for` is at least PR
  2a's measured gap. `LokiRulerNotificationsFailing`'s threshold comes from PR 2a's step per
  refused send (*The rules*).
- Deploys `loki-homelab` (another restart), then `observability`.
- After the deploy, `alertmanager_alerts{state="active"}` falls by one once `LokiRulerSmoke`
  resolves, unless a log rule fires. The live evaluation names any rule that does. Read the
  error counter only from two minutes after the new pod starts.
- Two-writer check: the four log alerts, `LokiHomelabDown`, `LokiRulerNotEvaluating` and
  `LokiRulerEvaluationFailing` route to the blackhole receiver. `LokiRulerEvaluationFailing`
  overlaps Kuma's `Loki Reachable` and `Loki Log Ingestion` tiles, which page a Loki that fails
  its queries until PR 4a (*Baseline*), so it stays shadow. `LokiRulerNotificationsFailing`
  pages, and no Kuma tile reads the ruler's sends.
- **Soak.** 7 days, and the window spans a Sunday reboot. Kuma's DOWN episodes for `Loki Log
  Ingestion`, `Loki Reachable` and `Kuma Notification Delivery` come from
  `probe.py metric 'monitor_status{monitor_name=~"Loki Log Ingestion|Loki Reachable|Kuma Notification Delivery"} == 0'`
  over a window that starts at this PR's deploy (M1 convention 6). `LokiRulerEvaluationFailing`
  is a Prometheus rule, so its shadow firing minutes are in `ALERTS`, and the soak compares
  them with `Loki Reachable`'s DOWN minutes under convention 6's two queries. A Loki rule's firing
  history is not in Prometheus (see *Baseline*), so evaluate each rule's expression over the
  same window as a Loki metric query with `probe.py loki-query --json --step 5m --since
  <soak length>`. The 5m step is the group's own interval, so the query samples as often as
  the ruler evaluates. A 7-day soak is then 2016 samples per rule. Without `--step`, Loki
  would sample every 2419 s, and a `LokiPodLogsSilent` episode shorter than that could fall
  between samples. Record each query's run time beside its result. Compare the episodes
  one by one in the PR, and explain each extra or missed one. Expect one episode per Sunday
  reboot: the first evaluations after Loki returns can read a window before Alloy's backlog
  lands. List it, and confirm that it falls inside M2's `weekly-reboot` mute. These tiles
  rarely go DOWN otherwise, so expect few episodes on either side.

### PR 4a — The Kuma side goes (about 60 lines added, about 710 removed)

- Every shadow rule stays shadow. Only `LokiHomelabMetricsMissing` and
  `LokiRulerNotificationsFailing` page, and no Kuma tile reads their subjects. So no tag order
  and no other session's deploy of master's tip can make a check page twice.
- Delete three rows from `check_table.py`: `loki_ingestion`, `kuma_notify_failures` and
  `loki_reachable`. Delete their bodies from `checks/logs.py`: `check_loki_ingestion`,
  `check_kuma_notify_failures` and `check_loki_reachable`. Delete their verdicts from
  `verdicts/logs.py`: `loki_ingestion_fresh`, `parse_notify_failure_line`,
  `parse_notify_reason_line` and `kuma_notify_failures`.
- In the same commit, remove six keys from `templates/env-secret.yaml.j2` and their `Config`
  fields and loads from `bridge/config_io.py`: `LOKI_STREAM`, `LOKI_DOCKER_STREAM`,
  `LOKI_PI_STREAM`, `LOKI_WINDOW`, `LOKI_FILETAIL_WINDOW` and `KUMA_NOTIFY_FAILURES_WINDOW_S`.
  These keys survive:
    - `LOKI_URL`;
    - `LOG_ERROR_SELECTOR` and the other `LOG_ERROR_*` keys, for `with_log_errors`;
    - `SWALLOWED_VERDICTS_WINDOW_S`, for `swallowed_verdicts`;
    - `BOOT_SETTLE_S`, which `check_swallowed_verdicts` and `check_shipper_dropped` read.
- Change `swallowed_verdicts`'s `gate` to `startup_grace`. It fails open on a Loki error by
  itself.
- Keep `with_log_errors`, which the workload check still calls (M2 narrows that row to
  `workload_log_errors`). It fails open and is not Loki-gated. M5 does not port it. M6's A7
  does, and *Open questions* 6 lists what M5 leaves for A7.
- Delete `test_check_kuma_notify_failures.py`. In `test_check_loki.py`, delete the tests of
  the ingestion and reachability bodies, and the four Pi-boundary tests:
  `test_both_pi_streams_are_in_the_census`, `test_cluster_arm_counts_its_cluster_stream`,
  `test_cluster_arm_counts_no_pi_stream` and `test_cluster_arm_that_counts_a_pi_stream_is_flagged`.
  They read the `LOKI_STREAM` and `LOKI_DOCKER_STREAM` fields this PR removes. PR 3's
  Pi-boundary census check holds the same property over the rule file, with the pre-#3739
  selector as its flagged fixture. Keep `test_loki_selectors_use_real_stream_labels` while a
  bridge selector remains.
- Shrink the three named-member rosters that hold the removed keys, or CI fails:
    - `test_check_loki.py:test_the_selector_roster_covers_the_known_selectors`: the known set
      becomes `HA_BAN_SELECTOR` and `LOG_ERROR_SELECTOR`.
    - `test_check_loki.py:test_the_deployed_selector_roster_covers_known_overrides`: the
      known set becomes `LOG_ERROR_SELECTOR`.
    - `test_loki_stream_labels.py:test_the_consumer_census_finds_its_named_members`: drop
      `"env-secret LOKI_STREAM"`, and keep `"env-secret LOG_ERROR_SELECTOR"` and the rule-file
      member PR 3 added.

  Each roster keeps at least one member, so none can pass on an empty set.
- Rewrite the text that names a removed key as a live consumer: the comments in
  `test_check_loki.py` and the stream-label helper that call `LOKI_STREAM` the checked
  selector, and the module docstring of `ansible/tests/services/test_alloy_pi_config_labels.py`,
  which names `LOKI_PI_STREAM` where `LokiPiLogsSilent` now selects `{job="pi"}`.
- Remove `"Kuma Notification Delivery"` and its comment from `EMAIL_TIER` in
  `ansible/tests/services/test_kuma_static_monitors.py`. The tile renders from the
  `kuma_notify_failures` row this PR deletes, and
  `test_email_tier_membership_is_exactly_declared` asserts that the rendered email-tier tiles
  equal `EMAIL_TIER`. The email leg moves to M1's receiver for `severity="critical"` alerts,
  which sends to email as well as Discord. `KumaNotificationDropped` is `critical` and reaches
  it from PR 4b.
- Rewrite the text that names the deleted check as the reader of Kuma's drop line or of Loki.
  Each site names `KumaNotificationDropped` on the loki-homelab ruler instead:
    - The comment above the `discord` notification in
      `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2`, and the matching
      sentence in the role's `CLAUDE.md` (*One Discord template for every monitor*). Both say
      the name stays `Homelab Alerts` because the bridge reads it out of Kuma's failure line.
      The rule filters `|= "Cannot send notification"`, which names no notification, so that
      reason no longer holds. Each keeps the `discord` id's reason and drops the name's.
    - `docs/uptime-kuma-autokuma-record.md`: the paragraph that says monitor-bridge reads the
      drop line out of Loki and pages the **Kuma Notification Delivery** tile, the bullet on
      the `discord` id and name, and the closing link that lists the tile.
    - The auto-deploy comment in `ansible/roles/k8s/loki-homelab/defaults/main.yml`, which
      names "Loki Log Ingestion" among the readers, and the role `CLAUDE.md`'s bullet on
      "every LogQL-backed monitor-bridge check". Both name the ruler's rules and the bridge's
      remaining LogQL checks.
    - The entries in `docs/monitor-bridge-checks.md` that compare themselves with Loki Log
      Ingestion, the shipper entry among them. They name the silence rules instead.

  `docs/facts.lock` moves only if a reworded `CLAUDE.md` sentence adds or drops a citation.
  The `facts-reverify-changed` hook then re-hashes it in the same commit.
- In `test_check_swallowed_verdicts.py`, the assertion that `swallowed_verdicts` is in
  `gates.LOKI_DEPENDENT` becomes one that it is in `gates.STARTUP_GRACE`.
- Delete the `loki_reachable` gate wiring in the same commit, or the bridge fails at import
  and CI fails with it:
    - `gates.py`: the `check_loki_reachable` import, `LOKI_DEPENDENT`,
      `Gates.loki_dependent`, `Gates.probe_loki` and the `loki_reachable` key of
      `Gates.gate_dependents`.
    - `check.py:run_once`: the Loki gate call and its `loki_dependent` skip branch.
    - The docstring of `bridge/types.py:PushCheck` and the comments in `check_table.py`,
      `bridge/config.py`, `checks/service.py` and `networkpolicy-loki.yaml.j2`.
    - The tests: the `loki_reachable` member of `test_check_table.py:_GATES` and its probe
      map entry, `probe_loki=` and `loki_dependent=` in the gate tests and
      `tests/_check_gate_helpers.py`, and `loki_reachable` in
      `test_fragments_bridge.py:test_every_non_gate_check_is_in_exactly_one_gate_set_row`.

  The `regen-doc-fragments` hook rewrites the bridge fragments on that commit, the gate-set
  fragment among them, and fails once. Stage the rewritten fragments and commit again.
- **Retire the three push tokens in the same PR**, or CI fails. `land.sh` merges with
  `merge_method=squash` (`scripts/deploy_tools/land_lib/merge.py:_merge_past_review`), so
  master holds PR 4a as one commit.
  `ansible/tests/k8s/test_secret_consumer_census.py:test_no_declared_consumer_tag_names_a_role_that_never_references_the_secret`
  runs over every key name in `secrets.yml` (`scripts/secrets_mgmt/rotation_tools.py:sops_names`).
  `scripts/secrets_mgmt/consumers.py:consumer_tags` routes each `monitor_bridge_*` token to
  `monitor-bridge` and `uptime-kuma`. `consumers.py:tree_consumers` credits a role only when one
  of its role files names the token, and it skips `secrets.yml` and `secret_rotation.yml`.
  `check_table.py` is the only role file that names these three tokens, and uptime-kuma's
  credit comes from that file through `k8s_lookup_map` (#3781). Once this PR deletes the three
  rows, a key left in `secrets.yml` fails the test for both tags, and CI's unscoped `pytest`
  job stops the merge. The PR therefore carries the four-part token recipe M6 uses:
    1. **Operator-only:** remove these three keys from `ansible/vars/secrets.yml` with `sops`:
        - `monitor_bridge_loki_push_token`
        - `monitor_bridge_kuma_notify_failures_push_token`
        - `monitor_bridge_loki_reachable_push_token`
    1. Delete the same three rows from `ansible/secret_rotation.yml` by hand. Running
       `secret_rotation.py sync` does not do it:
       `scripts/secrets_mgmt/secrets_lib/secret_registry.py:sync` adds missing names and only
       reports stale ones. Commit `6894e3523` ("Retire the E7-orphaned credentials") deleted
       its retired keys' rows the same way, in the commit that removed the keys.
    1. Add the three names to `scripts/secrets_mgmt/git_dates.py:RETIRED`.
       `scripts/secrets_mgmt/tests/test_departed_secrets_are_accounted_for.py` requires every
       name that has left `secrets.yml` to be in `RETIRED` or `RENAMED_FROM`.
    1. Stage the `secret-tiers` fragment that the `regen-doc-fragments` hook rewrites.
       `scripts/docs/gen_doc_fragments.py:_secrets` renders it from
       `scripts/docs/fragment_readers.py:registry_counts`, and the `auto` tier loses three rows.

  The four parts span two commits, because the `secret-rotation-registry-sync` prek hook
  gates each commit on its own. The hook runs `secret_rotation.py audit --check`.
  `secret_rotation.py:cmd_audit` returns 1 under `--check` on any missing or stale row.
  `secret_registry.py:registry_drift` returns both lists: a name in `secrets.yml` with no row
  is missing, and a row whose name has left `secrets.yml` is stale. A commit that deletes the
  rows while the keys remain fails on three missing names. A commit that removes the keys
  while the rows remain fails on three stale rows. So PR 4a has two commits, and parts 1, 2
  and 4 share the second:

    - **The agent's commit** carries the code, the tests, the docs and part 3, the `RETIRED`
      additions. The registry hook runs on it, because its `files` regex,
      `scripts/secrets_mgmt/.*\.py`, matches `git_dates.py`. It passes, because the registry
      and the store still agree.
    - **The operator's commit** carries part 1, the `sops` removal (**operator-only**), with
      parts 2 and 4. The `sops` edit decrypts the file, and C8 marks any step that decrypts
      operator-only. Deleting the rows touches `ansible/secret_rotation.yml`, which the
      `regen-doc-fragments` hook's `files` regex matches through `ansible/[^/]+\.ya?ml`. That
      hook rewrites the `secret-tiers` fragment and fails once, so the fragment is staged and
      the same commit runs again. The operator may make the whole commit, or run only `sops`
      in PR 4a's worktree and leave the agent to delete the rows, stage the fragment and
      commit all three together.

  The branch's CI is red between the two commits. `test_secret_consumer_census.py` fails on
  the three keys whose rows have left `check_table.py`.
  `test_departed_secrets_are_accounted_for.py:test_no_listed_departure_is_back_in_the_store`
  fails on the three `RETIRED` names that `secrets.yml` still holds. Neither test is in the
  `static-ratchet-tests` hook's list, so the agent's commit passes locally. `land.sh` waits
  on CI before it merges, so that red holds PR 4a until the operator's commit lands.

  `monitor_bridge_swallowed_verdicts_push_token` stays, because its row stays until M6's C-7.
  Because `ansible/vars/secrets.yml` changed, `land.sh` appends its rotation note
  (`land_lib/land_tags.py:_ROTATION_NOTE`). No role file names the three keys after this PR,
  so the note asks for no redeploy. The PR body quotes three reads, taken after the operator's
  commit:
    - `uv run python scripts/secrets_mgmt/secret_rotation.py audit --check` exiting 0.
    - `grep -c '_push_token:' ansible/secret_rotation.yml` printing three fewer than on
      `origin/master` before the PR. That is 80 from the 83 at `70591c307`, if no other slice's
      token PR lands first.
    - `git diff --stat origin/master -- ansible/vars/secrets.yml`. Plain `git diff` on that
      file prints plaintext.
- Requires M2's PR 3, because PR 4b must follow inside the no-pager bound below.
- **Order.** The tiles must go before the pushes stop, or each tile goes DOWN 1200s after its
  last push (*Baseline*). That is a false page, not a second pager, because every rule is
  shadow. By convention 8, four cases:
    - **Both tags deploy through Ansible.** `land.sh` deploys `uptime-kuma`, then
      `monitor-bridge`. The toposort puts `uptime-kuma` first (M6's *The cutover order*).
    - **`uptime-kuma` is flux-owned.** The merge's `promote` run deletes the tiles, then
      `land.sh` deploys `monitor-bridge`. The order is the same. Meanwhile the old bridge pushes
      into three deleted tokens, and no check reads those refusals (*Baseline*).
    - **`monitor-bridge` is flux-owned and `uptime-kuma` is not.** The `promote` run stops the
      three pushes at merge. Run `land.sh` at once, so that `uptime-kuma` deploys inside
      1200s. A later deploy sends one false DOWN page per tile.
    - **Both are flux-owned.** The `promote` run applies them in the order of D4's generated
      `dependsOn`. If that order puts `monitor-bridge` first, the 1200s bound of the case
      above applies.
- Record that `monitor_status{monitor_name=~"Loki Log Ingestion|Loki Reachable|Kuma Notification Delivery"}`
  returns nothing and that `probe.py health monitor-bridge` passes. Record the time the new
  `monitor-bridge` started, from its deploy or its `promote` run.
- **The no-pager bound.** PR 4b is open and green, stacked on this PR, before this PR merges.
  It lands as soon as the reads above pass. If PR 4b has not deployed within two hours of this
  PR's `monitor-bridge` start, revert this PR. The rules are still shadow, so the revert
  restores Kuma as the only pager. Two hours is M2's PR 4a bound, which M6's cutovers also
  use. Master holds this PR as one squash commit, so the revert restores the three keys'
  ciphertext and their registry rows together. If a later commit edited `secrets.yml`, the
  revert conflicts there, and the revert waits for the operator's `sops` step in *Rollback*.
- Two-writer check: from this commit until PR 4b deploys, the two ported checks and a Loki
  query fault page from neither side. That is the safe direction of convention 5. A Loki
  outage pages from Kuma's `Cluster Scrape Targets` tile only. `swallowed_verdicts` pages from
  Kuma only.

### PR 4b — The rules page (about 25 lines)

- Drop `shadow: "true"` from `homelab-logs.yaml`, `LokiRulerNotEvaluating` and
  `LokiRulerEvaluationFailing`. `LokiHomelabDown` keeps it until M6's A3 Cutover-b deletes
  the rule.
- Add to M2's routing pytest, which asserts that every route reached carries `weekly-reboot`,
  the label sets of the eight M5 alerts that page from this PR: the four log alerts,
  `LokiHomelabMetricsMissing`, `LokiRulerEvaluationFailing`, `LokiRulerNotEvaluating` and
  `LokiRulerNotificationsFailing`. Two of them page from PR 1 and PR 3. M2's PR 3 adds the
  pytest, and this PR is the first M5 PR that requires it.
- Requires PR 4a's recorded reads, M2's PR 3, and the C7 row *Open questions* 1 requests.
- Deploys `loki-homelab` and `observability`, in either order, through `land.sh` or the
  `promote` run. The Kuma side is already gone, so ownership and order do not matter here.
- Two-writer check: from this commit, the two ported checks and a Loki query fault page from
  Alertmanager only.

### PR 5 — Folded into PR 4a

PR 5 has no PR of its own. Its four-part token retirement is in PR 4a, because
`test_secret_consumer_census.py` fails on a key whose last row has left `check_table.py`
(PR 4a's *Retire the three push tokens* bullet). The four parts span PR 4a's two commits, the
agent's and the operator's, and master holds them as one squash commit. The number stays so that other slices'
citations of M5's PR 5 still resolve.

## Verification

```bash
# The ruler loaded the group and evaluates it (names and rule_group as PR 2a recorded them)
uv run python scripts/diagnostics/probe.py metric 'loki_prometheus_rule_group_rules'
uv run python scripts/diagnostics/probe.py metric 'time() - loki_prometheus_rule_group_last_evaluation_timestamp_seconds'
# The ruler's Alertmanager configuration: proves the config, not delivery (exit criterion 2).
# Read the errors from two minutes after a Loki pod starts.
uv run python scripts/diagnostics/probe.py metric 'loki_prometheus_notifications_alertmanagers_discovered'
uv run python scripts/diagnostics/probe.py metric 'increase(loki_prometheus_notifications_errors_total[5m])'
# PR 2b only, while LokiRulerSmoke fires (0 is expected once no log rule fires)
uv run python scripts/diagnostics/probe.py metric 'increase(loki_prometheus_notifications_sent_total[15m])'
uv run python scripts/diagnostics/probe.py metric 'alertmanager_alerts{state="active"}'
# Every selector matches a live stream (run one per selector in homelab-logs.yaml).
# --since 1m keeps the range to one minute, so each prints a few equal counts; read the last.
uv run python scripts/diagnostics/probe.py loki-query --since 1m 'sum(count_over_time({job="k8s"}[24h]))'
uv run python scripts/diagnostics/probe.py loki-query --since 1m 'sum(count_over_time({job="pi"}[24h]))'
uv run python scripts/diagnostics/probe.py loki-query --since 1m 'sum(count_over_time({job=~"authlog|syslog",machine!="daniel-pi"}[24h]))'
uv run python scripts/diagnostics/probe.py loki-query --since 1m 'sum(count_over_time({container="uptime-kuma"}[24h]))'
# KumaNotificationDropped's filter matches real lines (the rule's filter, the window PR 3 records)
uv run python scripts/diagnostics/probe.py loki-query --since 1m 'sum(count_over_time({container="uptime-kuma"} |= "Cannot send notification" [14d]))'
uv run python scripts/diagnostics/probe.py loki-query --since 1m 'sum(count_over_time({container="uptime-kuma"} |= "Cannot send notification to " [14d]))'
# After PR 4a: the tiles are gone
uv run python scripts/diagnostics/probe.py metric 'monitor_status{monitor_name=~"Loki Log Ingestion|Loki Reachable|Kuma Notification Delivery"}'
# PR 4a: the registry agrees with secrets.yml (exit 0), and three push-token rows are gone
uv run python scripts/secrets_mgmt/secret_rotation.py audit --check
grep -c '_push_token:' ansible/secret_rotation.yml
git diff --stat origin/master -- ansible/vars/secrets.yml
```

**The ruler-to-Alertmanager proof, red then green.** `alertmanager_alerts_received_total` is
not evidence, because the Watchdog and every Prometheus alert move it each minute. The proof
uses signals only the ruler moves:

1. PR 2a turns the ruler on with `LokiRulerSmoke` and no grant. The notifier discovers one
   Alertmanager, and its error counter rises while the push is refused.
2. PR 2b deploys the grant. The sent counter rises, the error counter holds at 0 from two
   minutes after the deploy, and `alertmanager_alerts{state="active"}` rises by one.
3. PR 3 removes `LokiRulerSmoke`, and `alertmanager_alerts{state="active"}` falls by one.

The sent counter proves delivery only together with zero errors. It is a PR 2b reading: once
no log rule fires, the ruler sends nothing and the counter holds still. After PR 3, no
reading proves delivery until a log rule fires. A break then raises the error counter, and
`LokiRulerNotificationsFailing` pages through Prometheus. This proof costs no extra Loki
restart: PR 2a and PR 3 restart Loki anyway.

**Red proofs for the new checks:**

- The Prometheus-side rules: their promtool firing cases, and the empty-input case that fires
  `LokiHomelabMetricsMissing`.
- The log rules: lokitool's parse, which `--prove-red` covers with a bad LogQL fixture. Then
  the live evaluation, and the non-zero historical count for `KumaNotificationDropped`'s
  filter.
- The selector census: `test_a_consumer_on_a_dead_job_is_flagged`, which already exists, now
  over a census that includes the rule file. Then the four new `_is_flagged` fixtures, the
  pre-#3739 host selector among them.
- The grant: PR 2a's refused sends.
- The rule-group list's agreement check: its flagged fixture, a rule file that
  `loki_homelab_rule_groups` does not name.

## Rollback

- **PR 1.** Revert and deploy `observability`. The revert also removes `loki-query --step`,
  so keep that part when PR 3's soak still needs it.
- **PR 2a.** Revert and deploy `loki-homelab`. The ruler turns off, and the rules ConfigMap is
  pruned.
- **PR 2b.** Revert. The outcome depends on who owns `netpol-baseline` (M1 convention 8):
    - Ansible-owned: deploy `netpol-baseline`. Its task "Apply the observability namespace's
      policies" runs `kubectl apply` without `--prune`, so the policy object stays behind. Its
      deletion is a kubectl write (**operator-only**). Until then it admits only loki-homelab
      to Alertmanager's port.
    - Flux-owned: the revert's `promote` run prunes the policy, because the
      `netpol-baseline-deferred` Kustomization sets `prune: true` (contracts.md C3). No
      operator step follows.
- **PR 3.** Revert and deploy `loki-homelab` and `observability`. The shadow log rules and the
  ruler-watching alerts disappear, and `LokiRulerSmoke` returns to the blackhole receiver. If
  the slice stops there, a follow-up removes the smoke rule.
- **PR 4b.** Revert, and deploy `loki-homelab` and `observability`. The rules return to
  shadow, and the checks have no pager until PR 4a is reverted too.
- **PR 4a.** Revert PR 4b first if it merged, or the restored bridge and the live rules page
  twice. Then revert PR 4a, and deploy `uptime-kuma` and `monitor-bridge`. That brings back
  the tiles under their old ids and the bridge checks. Master holds PR 4a as one squash
  commit, so its revert restores the three keys' ciphertext and registry rows in one commit
  and drops the `RETIRED` entries. `audit --check` therefore still exits 0 on the revert. A
  flux-owned tag is applied by the revert's `promote` run instead.
    - If a later commit edited `secrets.yml`, the revert conflicts there. The weekly
      secret-rotate cron commits to that file, so expect this after a week. The agent cannot
      resolve the conflict, because it cannot decrypt the file.
    - The operator then re-adds the three keys with `sops` (**operator-only**). Fresh values
      work: the bridge's env Secret and uptime-kuma's tiles render each token from the same
      key, and AutoKuma reconciles a tile's `push_token` from that Secret (the comment above
      `consumers.py:UPTIME_KUMA_TAG`).
    - In the same commit, the agent restores the three registry rows and drops the
      `RETIRED` entries. The re-added keys and the restored rows must share that commit,
      because the registry hook fails a commit that holds either one without the other.
- **A partial PR 4a.** If `monitor-bridge` failed to deploy after the tiles went, the old
  bridge pushes into deleted tokens, and the three checks have no pager. Re-run the deploy, or
  revert. The rules are shadow either way, so nothing pages twice.
- **A partial PR 4b.**
    - If `loki-homelab` failed, the two ported checks have no pager.
    - If `observability` failed, `LokiRulerEvaluationFailing` and `LokiRulerNotEvaluating`
      stay shadow. A Loki query fault then has no pager, because PR 4a deleted `Loki
      Reachable` and `Loki Log Ingestion`.

  In both cases, re-run the failed deploy inside PR 4a's two-hour bound. Otherwise revert PR
  4b, then PR 4a.
- **PR 5.** It has no commit of its own, so PR 4a's rollback covers the three tokens.

## Risks

- **Class 3, empty read as clean.** Four ways apply here:
    - A selector or a line filter matches nothing, so its count rule never fires.
    - A cluster silence rule also counts a Pi stream, so it never fires during a cluster
      outage (#3739).
    - A ruler-watching alert reads a metric name that does not exist.
    - The scrape job disappears and silences every alert on it.

  Mitigation: the census over label names, `job`, `machine` and `container` values and the
  Pi boundary; the live non-zero count per selector; the historical count for
  `KumaNotificationDropped`'s own filter; names measured in PR 2a before any rule cites them;
  `LokiHomelabMetricsMissing` and `LokiRulerNotEvaluating`'s `absent()` arm.
- **Class 4, a selector drifting from the hazard.** The Alloy pod streams carry no `app`
  label. The bridge's *Traps* section records that a selector written from the `-l app=` habit
  matches nothing. Mitigation: the census rejects a label name the Alloy streams do not carry,
  with an `app=` fixture as its red half. It also rejects a `container` value no template
  declares.
- **Class 2, a proxy for the property.** `LokiHomelabDown` reads the `/metrics` scrape, while
  `Loki Reachable` probed `/loki/api/v1/labels`. Mitigation: each ruler evaluation is a query,
  so `LokiRulerEvaluationFailing` catches a Loki that answers its scrape but fails queries. It
  pages from PR 4b. Until PR 4a, `Loki Reachable` and `Loki Log Ingestion` page that fault.
- **A delivery break is silent until a log rule fires.** After PR 3 no rule is sure to fire,
  so the ruler may send nothing for weeks, and a deleted grant or a renamed Service shows in
  no reading (exit criterion 2). Mitigation: the first firing rule's failed sends raise the
  error counter, and `LokiRulerNotificationsFailing` pages through Prometheus, whose path to
  Alertmanager the break does not share. The cost is that first log alert, which reaches no
  one until the threshold and the 5m `for` pass, about 20 minutes. A permanent firing
  heartbeat rule would remove that cost but would need a C7 and M6 exemption, so this plan
  does not add one.
- **The cutover's no-pager window.** From PR 4a's `monitor-bridge` start to PR 4b's deploy,
  the two ported checks and a Loki query fault have no pager. Mitigation: PR 4b is stacked and
  green before PR 4a merges, and the two-hour bound reverts PR 4a. If `monitor-bridge` is
  flux-owned, its pushes stop at merge, and a late `uptime-kuma` deploy sends one false DOWN
  page per tile, 1200s after the merge.
- **A new source pod is refused for a while.** The grant names the ruler by `podSelector`, and
  every rules edit makes a new Loki pod that kube-router admits only after a delay
  ([ADR 0009](../../adr/0009-networkpolicy-default-deny-ingress.md)). Mitigation:
  `LokiRulerNotificationsFailing` needs more than two errors held for 5m, and verification
  reads the error counter from two minutes after the pod starts.
- **A self-inflicted alert on every Loki restart.** Every loki-homelab rollout restarts the
  `Recreate` Loki: this slice's PRs, M6 and each image bump. Mitigation:
  `LokiRulerNotEvaluating` requires `up == 1` and holds a `for` longer than PR 2a's measured
  restart gap. A C7 health gate that reads `ALERTS` therefore sees no alert from a deploy.
- **The first evaluation after a long Loki outage.** A silence rule can read its window before
  Alloy's backlog lands. Mitigation: for the weekly reboot, M2's mute, which PR 4b's routing
  label sets prove. The soak spans a Sunday and records the episode.
- **Query load.** The ruler runs every 3h range query on Loki's own querier. Mitigation: the
  group interval equals the bridge's, so the load doubles only during the soak, and PR 3
  records each expression's timing first. The soak's comparison queries run once per rule at
  a 5m step, and the PR records each one's run time.
- **M6's cutovers page through `swallowed_verdicts`.** The row stays on the bridge, and M6
  planned `KumaRefusedPush` for each window from Cutover-1a to Cutover-2. In that window a host
  producer pushes into a deleted token, `kuma_push` logs `by=kuma`, and
  `verdicts/logs.py:swallowed_verdicts` pages the `Swallowed Push Verdicts` tile through Kuma.
  The page lasts until the failed line leaves the 3h window. Mitigation: M6 owns the row
  (*Open questions* 4). Its `verdicts/logs.py` gains `KUMA_RETIRING_TAGS`, and each B shadow
  PR deploys its producers' tags there before that batch's Cutover-1a (M6 *The cutover
  order*). No contracts.md row carries this, because contracts.md's Round 2 (X2-11) ruled
  that the handoff needs no contract text.
- **Transcripts.** C7 forbids log rules on the observability Loki, which holds Claude Code
  prompts. Mitigation: the ruler runs on loki-homelab only. No rule annotation templates a
  log line's text, only labels, so no line content reaches Discord or email.
- **Loki and Alloy restart on each rules edit.** Mitigation: rule edits are rare (PR 2a, PR 3
  and PR 4b here, and M6's A7, B7 and C-7). Alloy resumes from its positions file, so a restart
  costs an ingestion pause, not lost lines.
- **Health-gate blindness.** `probe.py health loki-homelab` cannot see a log rule's alert.
  It sees few of the Prometheus-side alerts either. contracts.md's *Requests on C7* (M6 Open
  questions 3 and 4) narrows the gate's selector to the soak's set, `severity="critical"` or
  `verify="true"`, from M6's B-1. Of this slice's alerts, only `LokiRulerNotificationsFailing`
  is in that set. `LokiRulerEvaluationFailing`, `LokiRulerNotEvaluating` and
  `LokiHomelabMetricsMissing` are `warning` with no `verify` label, and M6's A3 Cutover-b
  deletes `LokiHomelabDown`. Mitigation: those three warnings page through Alertmanager
  whatever the gate reads. A broken ruler deploy also shows in exit criterion 1's readings,
  the first two *Verification* commands, which each ruler PR runs after its deploy. This plan
  adds no `verify` label (*Open questions* 1).
- **A token retirement split the wrong way fails the commit hook.** The
  `secret-rotation-registry-sync` hook fails in both directions. A commit that removes the
  keys and runs only `sync` leaves three stale rows, because `sync` deletes none. A commit
  that deletes the rows while the keys remain leaves three missing names. Mitigation: the
  operator's commit holds the `sops` removal and the row deletion together, the agent's
  earlier commit touches neither, and the PR quotes `audit --check` exiting 0.
- **A token that outlives its row fails CI.** `test_secret_consumer_census.py` flags a
  `monitor_bridge_*` key whose last role reference has gone (PR 4a). Mitigation: PR 4a removes
  the keys in the same PR that deletes the rows, and master holds both as one squash commit.
  The cost is an operator step in two places. The branch's CI stays red from the agent's
  commit to the operator's, and `land.sh` will not merge it until then. A revert after a later
  `secrets.yml` commit also needs the operator to re-add the keys (*Rollback*), which can
  hold the two-hour revert of the no-pager bound.

## Tests and docs that change in the same PR

- **PR 1.**
    - `loki-homelab.test.yaml`, with its empty-input case, and the
      `observability_rule_groups` entry, which `alert_rules.py`'s list agreement check reads.
    - The `--step` cases in `test_probe_curl_pipeline.py`.
- **PR 2a.**
    - The lokitool fixture under `scripts/validate/tests/fixtures/alert_rules/` and its
      `--prove-red` leg.
    - The `_is_clean` and `_is_flagged` pair for the agreement check over
      `loki-homelab/files/rules/`, `loki_homelab_rule_groups` and the rendered
      `loki-homelab-rules` keys. The flagged fixture is a rule file the list does not name.
    - The loki-homelab role's `CLAUDE.md` gains a *Ruler* paragraph naming the tenant
      directory, the group interval and the restart cost.
- **PR 2b.**
    - The `bespoke-netpol` census row passes through the new marker.
    - The netpol-baseline role's `CLAUDE.md` lists `networkpolicy-alertmanager` among the
      deferred observability policies.
- **PR 3.**
    - The census source, its named member, its four checks and their `_is_clean` and
      `_is_flagged` pairs in `test_loki_stream_labels.py`.
    - The stream-label helper, with `_PI_STREAMS` and `_selector_matches`, moved out of
      `test_check_loki.py`.
    - The three ruler-watching cases in `loki-homelab.test.yaml`.
- **PR 4a.**
    - Deletes the bridge tests named above, the four Pi-boundary tests among them.
    - Shrinks the known sets of `test_check_loki.py:test_the_selector_roster_covers_the_known_selectors`
      and `test_check_loki.py:test_the_deployed_selector_roster_covers_known_overrides`, and
      drops `"env-secret LOKI_STREAM"` from
      `test_loki_stream_labels.py:test_the_consumer_census_finds_its_named_members`.
    - Rewrites the comments and the `test_alloy_pi_config_labels.py` docstring that name a
      removed key.
    - Removes the Loki gate from the gate tests, `tests/_check_gate_helpers.py` and
      `test_fragments_bridge.py`, and moves `test_check_swallowed_verdicts.py`'s gate
      assertion to `gates.STARTUP_GRACE`.
    - The bridge fragments under `docs/assets/generated/fragments/`, which the
      `regen-doc-fragments` hook rewrites.
    - `docs/monitor-bridge-checks.md` and `docs/monitor-bridge-internals.md` lose the three
      rows and the Loki gate, and show `swallowed_verdicts` gated by `startup_grace`.
    - `ansible/roles/k8s/monitor-bridge/CLAUDE.md`'s *Gates and hysteresis* section drops
      "Loki". Its *Traps* bullet on Alloy stream labels also points at the census.
    - `EMAIL_TIER` in `ansible/tests/services/test_kuma_static_monitors.py` loses
      `"Kuma Notification Delivery"` and its comment, so
      `test_email_tier_membership_is_exactly_declared` holds over the remaining tiles.
    - The comments and docs that name the deleted check as a reader: `static-monitors.yaml.j2`
      and `CLAUDE.md` in the uptime-kuma role, `defaults/main.yml` and `CLAUDE.md` in the
      loki-homelab role, `docs/uptime-kuma-autokuma-record.md`, and the other entries in
      `docs/monitor-bridge-checks.md` that name Loki Log Ingestion.
    - The token part: the three `RETIRED` entries in the agent's commit, then the three rows
      deleted from `ansible/secret_rotation.yml` and the rewritten `secret-tiers` fragment in
      the operator's commit. `test_secret_consumer_census.py` and
      `test_departed_secrets_are_accounted_for.py` pass unchanged on the PR's tip, because the
      keys leave `sops_names` in the same PR. Both are red on the agent's commit alone.
- **PR 4b.** M2's routing pytest gains the label sets of the eight alerts that page.
- **PR 5.** Nothing: its token part is in PR 4a.

## Complexity delta

- **Added:**
    - A ruler block of about 10 lines, one ConfigMap, one mount, and one role default listing
      the rule groups, with an agreement check in `alert_rules.py`.
    - The cutover is PR 4a and PR 4b, with a no-pager window of at most two hours between
      them. PR 5's token retirement folds into PR 4a, so the slice keeps six PRs.
    - An operator commit on PR 4a's branch, holding the `sops` removal and the deletion of
      the three registry rows, before PR 4a can merge. Another operator `sops` step follows for a revert of PR 4a
      after a later `secrets.yml` commit.
    - Four log alerts (about 40 lines of YAML) and five Prometheus alerts with their tests.
    - One bespoke NetworkPolicy.
    - lokitool provisioning, and a census source with four checks (about 75 lines).
    - A `--step` flag on `probe.py loki-query` (about 30 lines with its test).
    - One temporary smoke rule, from PR 2a to PR 3.
- **Removed:**
    - Three check bodies and four verdict functions (about 200 lines of `checks/logs.py` and
      `verdicts/logs.py`).
    - One test file and part of a second (about 390 lines, the four Pi-boundary tests among
      them).
    - Six env keys with their `Config` fields.
    - Three Kuma tiles, three push tokens, and the Loki gate with its wiring in `gates.py`,
      `check.py` and the gate tests.

## Open questions

1. **Contract gap, C7.** The health gate reads Prometheus `ALERTS`, and a loki-homelab ruler
   alert never reaches it, because Prometheus deliberately runs without a remote-write
   receiver. Resolution: C7 states that log rules are outside the health gate. After M6, the
   only lasting log rules are the three ingestion arms on `loki-homelab` and `daniel-pi`.
   Reopening the receiver is not proposed. The Prometheus-side alerts do not fill the gap in
   the gate. Under the selector M6's B-1 adopts, `severity="critical"` or `verify="true"`,
   only `LokiRulerNotificationsFailing` reaches `probe.py health loki-homelab`. The two
   warning ruler-watching alerts and `LokiHomelabMetricsMissing` page through Alertmanager
   but stay outside the gate. M6's A3 Cutover-b deletes `LokiHomelabDown` (*Risks*). This plan
   adds no `verify="true"` label to the two warnings. C7 gives that label to a rule that
   verifies a deploy, and each of the two fires on a Loki query fault from any cause.
   [contracts.md's *Requested amendments not yet applied*](../contracts.md#requested-amendments-not-yet-applied)
   holds this row under *Requests on C7*, gated before PR 4b merges, because PR 4b is the
   first PR whose log rules page. Its text says that the ruler-watching alerts cover the
   service in `ALERTS`. This plan asks contracts.md to say instead that only
   `LokiRulerNotificationsFailing` reaches the gate.
2. **Contract gap, C7.** The ruler's `alertmanager_url` and the cross-namespace grant are an
   interface between two roles that no contract pins. Resolution: C7 records that Alertmanager
   lives in `observability` and is reached on its Service at port 9093. D5's Kustomizations for
   `loki-homelab`, `observability` and `netpol-baseline` then keep that edge. The same list
   holds this row under *Requests on C7*, gated before PR 2a merges, because PR 2a sets
   `alertmanager_url`.
3. **The cutover's M2 edge. Resolved.** The slice index reads "M1. The cutover also M2's PR
   3". Kuma's maintenance window mutes the replaced tiles today, so the log rules need M2's
   `weekly-reboot` mute before they page. PR 1 to PR 3 need only M1.
4. **Which short-lived checks to port.** `KumaNotificationDropped` and `swallowed_verdicts`
   watch Kuma, which M6 deletes. Resolution: port `kuma_notify_failures`, whose drop lines a
   single line filter selects (*The rules*), so the bridge can drop its Loki gate. Leave
   `swallowed_verdicts` on the bridge until M6, for the reasons under *The rules*. This changes
   what M5 hands M6 in three ways:
    - M5 removes three bridge rows and three push tokens, not four. The bridge keeps 41 rows,
      38 checks and three gates, and the registry keeps 80 push tokens before M3's one.
    - The `swallowed_verdicts` row and `monitor_bridge_swallowed_verdicts_push_token` pass to
      M6, which deletes them.
    - M5 adds no `KumaRefusedPush` and no `HostPushVerdictSwallowed`. Until M6 deletes the row,
      a push into a deleted token pages through the bridge's `Swallowed Push Verdicts` tile
      (*Risks*).

   design.md's M5 row and M6 follow this answer. design.md ports two of the three checks. M6
   counts three rows and three tokens from M5, takes `swallowed_verdicts` and its token into
   its C-7, and adds neither dropped rule. M6's `KUMA_RETIRING_TAGS` exclusion closes the page
   that *Risks* names for its cutovers. contracts.md's Round 2 (X2-11) records this handoff
   as "applied with no contract text", so no register row gates M6's first Cutover-1a on M5.
5. **Who ends `LokiHomelabDown`'s shadow. Resolved by M6.** M1 convention 5 holds it shadow
   until M6's A3 cuts the `Cluster Scrape Targets` tile over. M6's A3 item 5 (*A3 and the
   handoffs it closes*) deletes the rule in Cutover-b, with its promtool and routing cases.
   A3's scrape-target rule pages the same `up{job="loki-homelab"}` series with
   `service: loki-homelab`, so keeping both would page one fault twice.
   `LokiHomelabMetricsMissing` stays, because it reads a missing scrape job, which an
   `up == 0` rule cannot see. A dead loki-homelab then pages after A3's 10 minutes rather than
   5. A3's rule is `warning` with no `verify` label, so `probe.py health loki-homelab` does not
   gate on it (*Risks*).

   M6's B8 still says that Loki's `/ready` probe retires because `LokiHomelabDown` covers it.
   This plan asks M6 to name A3's rule there. Until A3's Cutover-b, exit criterion 4's
   exception stays open.
6. **The log-pattern arm's port (M2 Open question 6). Declined.** M2 makes this question a
   gate on its PR 4a and names M6 as the fallback owner. M5 declines the port, and M6 owns it
   as its batch A7 (M6 *Open questions* 10). The reasons:
    - The port's soak compares against the `Workload Log Errors` tile, which exists only from
      M2's PR 4a, and its cutover deletes a row that exists only from then. M5's own steps wait
      on no M2 PR but PR 3. Adopting the port would hold M5's slice end on M2's PR 4a.
    - M6 has already adopted the port and counts its rows and tokens with A7 in them. M6 says
      that an adoption by M5 would drop A7 and change those counts, so declining keeps M6 as
      written.

   M5 leaves these for A7, and PR 4a deletes none of them:
    - `checks/logs.py:with_log_errors`, which the narrowed `workload_log_errors` row calls;
    - `LOKI_URL`, which `swallowed_verdicts` also reads until M6 deletes that row;
    - the `LOG_ERROR_*` keys in `templates/env-secret.yaml.j2`, `LOG_ERROR_SELECTOR` among
      them, with the `"env-secret LOG_ERROR_SELECTOR"` census member and
      `test_loki_selectors_use_real_stream_labels`;
    - `monitor_bridge_k8s_workloads_push_token`, which PR 4a does not retire.

   A7 adds `WorkloadLogErrors` to `homelab-logs.yaml`. M6's B7 adds a second rule there, and
   its C-7 deletes `KumaNotificationDropped`. So exit criterion 1's count of 4 and the
   census's named members hold at M5's end only. A rule added to `homelab-logs.yaml` needs no
   change to `loki_homelab_rule_groups`. A new rule file joins that list in the same commit
   (*Ruler configuration*).

   This decline does not close M2's gate on its own. M2's PR 4a gate and its *Open questions*
   6 read "M5's text adopts the port", which a decline cannot meet, and they name M6 only as
   a fallback owner. M6 *Open questions* 10 says that M2's gate reads M6's A7 as the
   adoption. M2's gate needs that wording, and this plan requests it. M2 also says that M5's
   PR 5 retires four tokens. PR 4a retires three, with PR 5 folded into it, and
   `monitor_bridge_swallowed_verdicts_push_token` passes to M6 (*Open questions* 4). A7's
   shadow soak compares against the `Workload Log Errors` tile, so it starts only after M2's
   PR 4a creates that tile.

## Review log

### Round c1 (per-slice adversarial review, 2026-10-11)

Raised: eleven findings, six high and five medium. Fixed: all eleven. M5-c1-05 takes the
alternative its finding offered, M5-c1-03 changes the requested shape for the reason given
there, and M5-c1-05's removal fixes M5-c1-10. Refuted: none.

- **M5-c1-01 (high). PR 2 could not confirm the ruler metric names that PR 3 depends on.**
  Fixed. Loki v3.7.8's `DefaultMultiTenantManager.newManager` creates the per-tenant registry
  and notifier only for a tenant with a rule group, confirmed with `gh api`. PR 2a ships
  `LokiRulerSmoke` and records every name, the `user` and `rule_group` values and the restart
  gap. The ruler-watching alerts land in PR 3 on those names, and exit criterion 1 reads them.
- **M5-c1-02 (high). `LokiRulerNotEvaluating` double-paged every Loki outage and fired on
  every restart.** Fixed. It is conjoined with `up{job="loki-homelab"} == 1`, holds a 10m
  `for` checked against PR 2a's measured gap, and stays shadow until PR 4. The "pages once"
  paragraph now names the real pager. PR 4 adds its label set to M2's routing pytest. Also
  found: an unshadowed `LokiHomelabDown` would duplicate Kuma's `Cluster Scrape Targets` tile,
  because `up{job="loki-homelab"}` has no `origin` label (`probe.py metric`) and M1 convention
  5 forbids that. It now stays shadow until M6's A3 (*Open questions* 5).
- **M5-c1-03 (high). The Prometheus group failed M1's zero-series rule and was blind to a
  dropped scrape job.** Fixed, with one change to the requested shape. The `absent()` arm is
  its own alert, `LokiHomelabMetricsMissing`, which pages from PR 1 and which the empty-input
  case fires. Folding it into `LokiHomelabDown` would keep it shadow until M6's A3, because
  that rule's `up == 0` arm must stay shadow under convention 5. PR 1 adds `loki-homelab` to
  `observability_rule_groups`, and the tree lists `observability/defaults/main.yml`.
- **M5-c1-04 (high). The delivery proof could not fail, and the grant's red proof could not
  run.** Fixed. The grant moves to PR 2b, after PR 2a turns the ruler on, so PR 2a observes
  the refused sends. Exit criterion 2 and the proof read the ruler's discovered, sent and error
  counters and `alertmanager_alerts{state="active"}`, not `alertmanager_alerts_received_total`.
- **M5-c1-05 (high). `HostPushVerdictSwallowed` brought back #2783's post-reboot page and
  double-paged `HC_ROUTED_TAGS`.** Fixed by the finding's alternative: *Open questions* 4's
  fallback is the default. `swallowed_verdicts` stays on the bridge, gated by `startup_grace`,
  because `checks/logs.py:check_swallowed_verdicts` fails open on a Loki error.
  `HostPushVerdictSwallowed` and `KumaRefusedPush` are dropped, and PR 5 keeps that row's
  token. The soak spans a Sunday reboot and lists the reboot episode.
- **M5-c1-06 (high). The census checked only `job` values.** Fixed.
  `test_loki_stream_labels.py:_selected_jobs` reads `job` alone (confirmed). The rule-file
  source adds checks for label names, `machine` values and `container` values, each with an
  `_is_clean` and `_is_flagged` pair and an `app=` fixture. The Goal and the Class 4
  mitigation now say what the census checks.
- **M5-c1-07 (medium). PR 4 removed env keys that `Config` still required.** Fixed. PR 4
  lists `bridge/config_io.py` and `templates/env-secret.yaml.j2` and removes six keys with
  their fields in one commit. `BOOT_SETTLE_S` survives, because
  `checks/logs.py:check_shipper_dropped` reads it.
- **M5-c1-08 (medium). The group interval was unstated.** Fixed. `homelab-logs` sets
  `interval: 5m`, the bridge's `INTERVAL`, and exit criterion 1 and `LokiRulerNotEvaluating`
  derive their 10m from it. PR 3 times each 3h expression three times first.
- **M5-c1-09 (medium). No proof showed that a line filter can match.** Fixed. PR 3 requires a
  non-zero count of `KumaNotificationDropped`'s filtered expression over a 14-day window
  inside retention. The bridge measured 148 lines over 14 days on 2026-09-17. It is the only
  rule left with a line filter.
- **M5-c1-10 (medium). `HostPushVerdictSwallowed` had no `service` derivation.** Fixed by
  removal (M5-c1-05). Every remaining rule carries a literal `service`.
- **M5-c1-11 (medium). A critical page fired on a single failed send.** Fixed.
  `LokiRulerNotificationsFailing` needs `> 2` errors over 15m held for 5m. Verification reads
  the error counter from two minutes after a Loki pod starts. *Risks* names the new-pod lag
  and cites ADR 0009.

### Round c2 (per-slice adversarial review, 2026-10-11)

Raised: six findings, one high and five medium. Fixed: all six. M5-c2-01's edits to
contracts.md, design.md and M6 are outside this file, so this plan requests them and names
their gate. Refuted: none.

- **M5-c2-01 (high). The fix for M5-c1-05 changed M5's output, but M6, design.md and
  contracts.md still describe the old one.** Fixed in this file, with the other edits
  requested. Confirmed at `70591c307`: M6 counts four removed rows and 78 tokens, names
  `HostPushVerdictSwallowed` and `KumaRefusedPush`, and pages the Cutover-1a to Cutover-2
  window through `KumaRefusedPush`. design.md's M5 row ports all three checks. contracts.md's
  amendment list has no M5 row. `check_table.py` has 44 rows, four of them gates, and
  `ansible/secret_rotation.yml` has 83 `_push_token` keys, so M5's three removals leave 41
  rows and 80 tokens. *Open questions* 4 states the three-part interface change and requests
  a contracts row gated before M6's first Cutover-1a. *Open questions* 1 and 2 request their
  C7 rows, gated before PR 4 and PR 2a. The *Unblocks* line names the M6 gate, and *Risks*
  names the `Swallowed Push Verdicts` page during M6's cutovers.
- **M5-c2-02 (medium). `KumaNotificationDropped`'s verbatim filter was wider than what the
  bridge pages on.** Fixed. Confirmed: `verdicts/logs.py:kuma_notify_failures` pages only on
  a line `parse_notify_failure_line` reads. The rule is
  `count_over_time({container="uptime-kuma"} |= "Cannot send notification" [3h]) > 0`. PR 3's
  14-day proof runs that filter and compares it with `|= "Cannot send notification to "`.
  On 2026-10-11 the rule's filter counted 41 lines over 14 days with `probe.py loki-query
  --since 1m`. *Open questions* 4 and *The rules* no longer say "verbatim" or "exact".
- **M5-c2-03 (medium). PR 4's file list left out the Loki gate wiring.** Fixed. A
  `grep -rlE` for the gate's names found the places the finding lists and more:
  `tests/_check_gate_helpers.py`, `test_check_streaks.py`, `test_check_cli.py`, the comments
  in `bridge/config.py`, `checks/service.py`, `check_table.py`, `test_check_ha.py` and
  `networkpolicy-loki.yaml.j2`, and `test_check_swallowed_verdicts.py`, which asserts that
  the row is in `gates.LOKI_DEPENDENT`. *Baseline*, the tree and PR 4 list them all and say
  what each loses. PR 4 notes the `regen-doc-fragments` rewrite.
- **M5-c2-04 (medium). PR 4 deleted the #3739 guard and nothing replaced it.** Fixed. PR 3
  moves `_PI_STREAMS` and `_selector_matches` into the stream-label helper and adds a fourth
  census check, the Pi boundary. It covers the Pi rule in the other direction too. Its flagged
  fixture is the pre-#3739 `{job=~"authlog|syslog"}`. PR 4's test-deletion bullet names the
  four tests it retires and the check that replaces them.
- **M5-c2-05 (medium). Exit criterion 2's sent-above-0 bullet could not hold at slice end.**
  Fixed. The bullet is a PR 2b record over `[15m]`, two group intervals. The end state reads
  `alertmanagers_discovered` and the error counter. *Verification* marks the sent-counter
  command PR-2b-only and says that 0 is expected afterwards.
- **M5-c2-06 (medium). `probe.py loki-query` could not produce the soak's evidence.** Fixed
  with the flag the finding offered. Confirmed: `loki_query_url` sends no `step`, and Loki
  v3.7.8's `defaultQueryRangeStep` gives `floor(range / 250)` seconds, 2419 s over seven
  days. PR 1 adds `--step` with its test, and the soak runs each rule at `--step 5m`, the
  group interval. Each *Verification* count command carries `--since 1m`.

### Round c3 (per-slice adversarial review, 2026-10-11)

Raised: six findings, one high and five medium. Fixed: all six. M5-c3-03 and M5-c3-04 are
fixed with a correction to the finding, stated below. M5-c3-06 takes the split the finding
offered. Refuted: none.

- **M5-c3-01 (high). `LokiRulerEvaluationFailing` paged unshadowed from PR 3 while two Kuma
  tiles paged the same Loki query fault.** Fixed. Confirmed at `70591c307`:
  `checks/logs.py:check_loki_reachable` raises when `/labels` is not `success`, and its
  docstring says `_evaluate` renders `Loki Reachable` down. `verdicts/logs.py:loki_ingestion_fresh`
  returns down on `None` or 0. The rule is shadow until PR 4b, and the table says so. PR 3's
  two-writer check names the overlap, and the soak compares the rule with `Loki Reachable`.
  *Baseline* records the two Kuma paths. PR 4b adds the label sets of all eight paging
  alerts, `LokiHomelabMetricsMissing` and `LokiRulerNotificationsFailing` among them, to M2's
  routing pytest.
- **M5-c3-02 (medium). M5 never answered M2 Open question 6.** Fixed. *Open questions* 6
  declines the port with its reasons and points at M6's A7 (M6 *Open questions* 10). It lists
  what M5 leaves for A7: `with_log_errors`, `LOKI_URL`, the `LOG_ERROR_*` keys with their
  census member, and `monitor_bridge_k8s_workloads_push_token`. It says that A7, B7 and C-7
  change exit criterion 1's count and the census's named members after M5.
- **M5-c3-03 (medium). PR 4 left three named-member rosters asserting deleted keys.** Fixed,
  with one correction. Confirmed: both roster tests in `test_check_loki.py` and
  `test_loki_stream_labels.py:test_the_consumer_census_finds_its_named_members` hold the
  removed keys. The finding said both roster sets shrink to `LOG_ERROR_SELECTOR`, but
  `test_the_selector_roster_covers_the_known_selectors` also holds `HA_BAN_SELECTOR`, which
  stays. PR 4a names each roster and its remaining set, and the tree lists both test files. A
  `grep -rlE` for the six removed keys also found the module docstring of
  `ansible/tests/services/test_alloy_pi_config_labels.py`, which names `LOKI_PI_STREAM`. PR 4a
  rewrites it.
- **M5-c3-04 (medium). The rules ConfigMap needed a `fileglob` the offline renderer lacks.**
  Fixed with a role default, `loki_homelab_rule_groups`, which `rules-configmap.yaml.j2` loops
  over in M1 convention 1's `lookup('file', ...)` form. `alert_rules.py`'s lokitool phase adds
  the three-way agreement check with its `_is_clean` and `_is_flagged` pair. One correction:
  M6's A7 adds a rule to `homelab-logs.yaml`, not a second file. The fix stands, because a
  later rule file would otherwise reach no ConfigMap.
- **M5-c3-05 (medium). The end-state half of exit criterion 2 could not go red.** Fixed by
  rewording, not by a heartbeat rule. Confirmed with `gh api` at `v3.7.8`:
  `pkg/ruler/base/notifier.go:amConfigFromURL` builds a `discovery.StaticConfig` from the URL
  when discovery is off. Criterion 2 now says that the PR records prove delivery and that the
  two end-state readings prove the configuration only. *Risks* names the silent-break cost.
  Checking that cost found that one firing rule may not cross `> 2` errors in 15m. PR 2a now
  records the counter's step per refused send, and PR 3 sets the threshold from it.
- **M5-c3-06 (medium). PR 4's order and PR 2b's rollback assumed Ansible deploys.** Fixed. PR
  4 is now PR 4a, the Kuma side with every rule shadow, and PR 4b, which drops `shadow`, as
  M2's PR 4a and PR 4b and M6's cutovers do. M6's *The cutover order* also shows that another
  session's deploy from master's tip broke the one-PR form without Flux. PR 4a gives the order
  for each ownership case. The 1200s is `uptime_kuma_k8s_bridge_push_interval`. It bounds only
  the case where `monitor-bridge` applies first, and there it costs a false DOWN page, not a
  second pager. The no-pager bound is M2's two hours. PR 2b's rollback gives the flux case:
  `netpol-baseline-deferred` sets `prune: true` (contracts.md C3), so the promote prunes the
  policy. *Rollback*, *Risks*, *Tests and docs*, *Complexity delta* and *Open questions* 1
  follow the split.

### Round integration-2 (integration review, 2026-10-11)

Raised: four findings that name M5, all medium: X2-10, X2-11, X2-12 and X2-16. Fixed: M5's
part of all four. Each also needs an edit in another page (M6, contracts.md, M2 and M3, in
that order), which this plan requests rather than makes. Refuted: none.

- **X2-10 (medium). M6's batch A3 owns none of the shadow drops and routing changes that M4,
  M5 and M1 hand to it.** Fixed for M5's part. Confirmed in M6's page: `LokiHomelabDown`
  appears only in B8's `/ready` line, and the A3 row lists `targets`, `cluster_targets` and
  `shipper_dropped` with no shadow drop. *Open questions* 5 stated the task as if M6 held it.
  It now requests one of two A3 tasks, gated before A3's Cutover-b merges. Cutover-b drops the
  shadow and adds the label set to M2's routing pytest, or A3 deletes the rule when its
  scrape-target rule pages job `loki-homelab`. The request rides the M5-to-M6 row. *The rules*,
  *A Loki outage pages once*, exit criterion 4 and the health-gate risk point at it. The M4
  and M1 tasks belong to those slices. Superseded in round i1 (M5-i1-04): M6's A3 item 5
  deletes `LokiHomelabDown` in Cutover-b, and no M5-to-M6 row carries the task.
- **X2-11 (medium). The register and slice index lack about 20 requested rows and sub-edges
  that slices name as merge gates.** Fixed for M5's part, with the contracts.md edit
  requested. Confirmed: contracts.md's *Requested amendments not yet applied* holds none of
  M5's three rows (*Open questions* 1, 2 and 4). The index's M5 row reads "M1. The cutover
  also M2's PR 3". The *Depends on* line now names both C7 rows with the PR each gates, and
  says neither PR merges before its row exists. PR 2a gains the *Requires* bullet that PR 4b
  already had. *Unblocks* names M6's A7 shadow PR waiting on PR 3. *Open questions* 1, 2 and 4
  each say that the row is absent and that its PR waits. Superseded in round i1 (M5-i1-02):
  contracts.md's Round 2 applied X2-11 by adding the two C7 rows and by ruling that *Open
  questions* 4's handoff needs no row. The M5-to-M6 gate is gone from *Unblocks* and *Open
  questions* 4 and 5.
- **X2-12 (medium). M2's PR 4a gate requires "M5's text adopts the port", but M5 declined
  and M6's A7 adopted it.** Fixed for M5's part. Confirmed in M2's page: the PR 4a gate and
  *Open questions* 6 read "M5's text adopts the port", and *Open questions* 6 says that M5's
  PR 5 retires four tokens. M5's *Open questions* 6 ended "This answer closes M2's gate on
  its PR 4a in M5's text", which was false. It now says that the decline cannot meet that
  wording, and cites M6's statement that M2's gate reads A7 as the adoption. It requests
  M2's rewording and the token count of three, and says that A7's soak starts only after
  M2's PR 4a.
- **X2-16 (medium). M3's and M5's token-retirement recipe leaves stale registry rows that the
  prek audit flags.** Fixed. Confirmed at `70591c307`: the docstring of
  `scripts/secrets_mgmt/secrets_lib/secret_registry.py:sync` reads "Add missing secrets
  ...; report stale registry entries", and its body only adds.
  `secret_rotation.py:cmd_audit` returns 1 under `--check` when a stale row exists, and the
  `secret-rotation-registry-sync` hook in `prek.toml` runs `audit --check`. Corrected in round
  f0 (M5-i2-01): `cmd_audit` returns 1 on any missing or stale row, and the missing half is
  the one that fails a commit deleting the rows before the keys leave.
  `ansible/secret_rotation.yml` holds the three rows among 83 `_push_token:` keys. Commit
  `6894e3523` deleted its retired rows by hand. PR 5 is now M6's four-part commit: the
  operator's `sops` removal, the deleted rows, the `RETIRED` entries and the `secret-tiers`
  fragment. Its body quotes `audit --check` exiting 0, a push-token count three below
  master's and `git diff --stat`. *Verification*, *Rollback*, *Risks* and *Tests and docs* follow.
  Superseded in round i1 (M5-i1-01): the four parts moved into PR 4a's commit.

### Round i1 (per-slice adversarial review, 2026-10-12)

Raised: five findings, two high and three medium. Fixed: all five. M5-i1-01 takes the
finding's option (a), and its cross-slice reach is narrower than the finding stated, as
recorded below. Refuted: none. Each finding was checked against the tree at `70591c307` or
against the sibling pages before the fix.

- **M5-i1-01 (high). PR 4a failed CI, because the secret consumer census flagged the three
  push tokens that PR 5 kept for another week.** Fixed by folding PR 5 into PR 4a. Confirmed:
  `consumers.py:consumer_tags` routes each `monitor_bridge_*` token to `monitor-bridge` and
  `uptime-kuma`. `consumers.py:_census_corpus` reads only role files and skips `secrets.yml`
  and `secret_rotation.yml`. `grep -rln` for the three names under `ansible/roles` prints only
  `monitor-bridge/files/check_table.py`. `_phantom_tags` therefore fails both tags once PR 4a
  deletes the rows. PR 4a's commit carries the four-part recipe, and its merge waits on the
  operator's `sops` edit. `land_lib/land_tags.py` only appends `_ROTATION_NOTE` on a
  `secrets.yml` change, so PR 4a's deploy path is unchanged. *Rollback* gives the conflict
  path: the operator re-adds the keys with `sops`, and fresh values work, because both
  halves render from the same key. *The tree*, *Verification*, *Risks*, *Tests and docs*
  and *Complexity delta* follow. PR 5 keeps its heading so that citations resolve.
  Option (b), a transitional set in `consumers.py`, was rejected. It would need its own
  `_is_clean` and `_is_flagged` pair and a known-manual entry for
  `test_secret_consumers.py:test_every_auto_tier_token_resolves_a_consumer_or_is_known_manual`,
  only to come out a week later. One correction to the finding's reach: M3's
  `daniel_box_disk_push_token` is in `consumers.py:CROSS_HOST_PUSH_TOKENS`, so
  `consumer_tags` returns `()` and the census flags nothing there. M2's and M6's token PRs
  for `monitor_bridge_*` or `PREFIX_EXCEPTION_CONSUMERS` tokens have the gap. Superseded in
  round f0 (M5-i2-01): the four parts stay in PR 4a, but they span two commits, and the
  operator's commit holds the `sops` removal with the row deletion.
- **M5-i1-02 (high). M6's first Cutover-1a and A3's Cutover-b waited on an M5-to-M6 register
  row that contracts.md decided never to add.** Fixed by aligning with contracts.md.
  Confirmed: contracts.md's Round 2 (X2-11) reads "*Applied with no contract text.* M5 Open
  question 4's handoff to M6 changes slice counts, not a contract row", and its *Requests on
  C7* holds M5's Open questions 1 and 2 only. *Unblocks*, *Open questions* 4 and 5 and the
  *Risks* bullet on M6's cutovers drop the gate and name the handoff M6's text carries: the
  `swallowed_verdicts` row and token, `KUMA_RETIRING_TAGS`, and A3 item 5. The *Depends on*
  line and *Open questions* 1 and 2 say that the register holds both C7 rows. The X2-10,
  X2-11 and X2-16 entries carry a superseded note. M6's "The M5 handoff gate" baseline
  bullet and the first half of its first *Order* bullet are M6's to delete.
- **M5-i1-03 (medium). PR 4a left the Kuma email-tier roster asserting the deleted tile.**
  Fixed. Confirmed: `ansible/tests/services/test_kuma_static_monitors.py:EMAIL_TIER` lists
  "Kuma Notification Delivery", and `test_email_tier_membership_is_exactly_declared` asserts
  equality with the rendered tiles. PR 4a removes the entry and its comment. The email leg
  moves to M1's receiver for `severity="critical"` alerts, which `KumaNotificationDropped`
  reaches from PR 4b. *The tree* and *Tests and docs* list the file. A grep for the deleted
  checks' names over `ansible/tests`, the role test directories and `scripts` found no other
  exact-set roster outside the files PR 4a already lists.
- **M5-i1-04 (medium). The X2-10 fix was stale, and the health-gate claims built on it were
  false.** Fixed by rewording, with no `verify` label. Confirmed: M6's A3 item 5 deletes
  `LokiHomelabDown` in Cutover-b. contracts.md's *Requests on C7* narrows the gate's
  selector to `severity="critical"` or `verify="true"` from M6's B-1, so of this slice's
  alerts only `LokiRulerNotificationsFailing` reaches the gate. Exit criterion 4, *The rules*,
  *A Loki outage pages once*, *Risks* and *Open questions* 1 and 5 say so. *Open questions* 1
  asks contracts.md to reword its M5 row, which claims the ruler-watching alerts cover the
  service in `ALERTS`.
- **M5-i1-05 (medium). PR 4a left docs and comments naming the deleted check as the reader.**
  Fixed. Confirmed with `git grep` at `70591c307`: the five sites in the finding, plus two
  more lines in `docs/uptime-kuma-autokuma-record.md` (the `discord` id bullet and the closing
  link) and the shipper entry in `docs/monitor-bridge-checks.md`. PR 4a rewrites each site
  to name `KumaNotificationDropped` on the ruler. It also says that the `Homelab Alerts` name
  no longer binds, because the rule's filter names no notification. *The tree* and *Tests and
  docs* list the files, and `docs/facts.lock` where a reworded `CLAUDE.md` citation moves.

### Round f0 (per-slice adversarial review, 2026-10-12)

Raised: one finding, high. Fixed: it, as the finding required. Refuted: none. The finding
was checked against the tree at `70591c307` before the fix.

- **M5-i2-01 (high). PR 4a's token-retirement order failed the registry-sync prek hook at
  both commits.** Fixed. Confirmed: the `secret-rotation-registry-sync` hook in `prek.toml`
  runs `secret_rotation.py audit --check` on any change matching
  `^(ansible/vars/secrets\.yml|ansible/secret_rotation\.yml|scripts/secrets_mgmt/.*\.py)$`.
  `secret_rotation.py:cmd_audit` returns 1 when `--check` is set and `missing or stale` is
  non-empty, and `secret_registry.py:registry_drift` returns both lists. An agent commit that
  deleted the rows first failed on three missing names, and an operator commit with the
  `sops` removal alone failed on three stale rows. The plan had named only the stale half.
  PR 4a now splits the four parts across two commits. The agent's commit carries the code,
  the tests, the docs and the `RETIRED` additions, and the hook passes on it. The operator's
  commit carries the **operator-only** `sops` removal, the row deletion and the
  `secret-tiers` fragment that `regen-doc-fragments` rewrites. That hook's `files` regex
  matches `ansible/secret_rotation.yml` through `ansible/[^/]+\.ya?ml`. Commit `6894e3523`
  put the removed keys and their deleted rows in one commit the same way. PR 4a states that
  the branch's CI is red between the two commits, on `test_secret_consumer_census.py` and on
  `test_departed_secrets_are_accounted_for.py:test_no_listed_departure_is_back_in_the_store`.
  Neither test is in the `static-ratchet-tests` hook's list, so the agent's commit passes
  locally, and `land.sh` holds the merge on the red CI. `land.sh` merges with
  `merge_method=squash` (`land_lib/merge.py:_merge_past_review`), so master holds PR 4a as one
  commit. The "same commit" wording in PR 4a's heading, PR 5, *Rollback*, *Risks*, *Tests and
  docs* and *Complexity delta* now says "same PR" and names the squash commit. The *Risks*
  bullet on `sync` names both failing directions, and the X2-16 and M5-i1-01 entries carry a
  correction note.
