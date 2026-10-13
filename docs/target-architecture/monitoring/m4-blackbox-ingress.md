# M4 — blackbox_exporter for the ingress probes

Status: IN PROGRESS

PR 2, the validator's third `service` source: [#4798](https://github.com/DanielH2018/server/pull/4798).

Depends on: M1, and M2's PR 3 (the `weekly-reboot` mute) before this slice's PR 3

Unblocks: M6 (with M2, M3 and M5)

This slice replaces Kuma's 26 generated ingress tiles with blackbox_exporter probes that
Prometheus scrapes and alerts on. It keeps the targets derived from `containers_list`, as Kuma's
are. It runs the probes alongside Kuma for at least a week before Kuma's tiles go. It follows
the conventions in [M1](m1-alertmanager.md#conventions-every-monitoring-slice-follows), which
this page calls "M1 convention N".

## Goal and exit criterion

**Goal.** A `blackbox-exporter` role probes every ingress URL that `kuma_ingress_monitors`
yields at the baseline. `probe_success{service="<tag>"}` exists for each one, which is the half
of C7's health gate this slice owns. Each failing probe pages with the severity its service tier
earns. The `weekly-reboot` mute from M2's PR 3 covers every unshadowed alert this slice adds.

**Exit criterion.** All four hold:

1. `probe.py metric 'count(probe_success{job="blackbox-ingress"})'` equals the number of tiles
   `kuma_ingress_monitors` yields (26 at the baseline). Each series carries `service`. A pytest
   pins the job's targets, string for string, to `https://<hostname>.local.<domain>` built from
   each tile the filter yields.
2. After PR 4, `count(monitor_status{monitor_type="http",monitor_url=~"https://[^/]+"})` returns
   no series, while `count(monitor_status{monitor_type="http"})` returns a non-zero count (6 at
   the baseline, the hand-written HTTP monitors). The non-zero count proves that the Kuma
   scrape is alive, so the first query's empty result means the tiles are gone. The ingress
   loop is gone from `static-monitors.yaml.j2`.
3. The shadow soak spanned at least one Sunday. Soak query 1 recorded 0 unexplained
   miss-minutes, and every episode soak query 2 found is explained (M1 convention 6).
4. M2's routing pytest passes for the four alerts' label sets as PR 5 leaves them, each
   against its expected outcome:
    - The `IngressProbesMissing`, `IngressProbeFailed` and `IngressProbeFailedCritical` sets
      expect `muted`. Every route each set reaches in the rendered `alertmanager.yml` carries
      `weekly-reboot` in `mute_time_intervals`. The red half for this outcome is the
      `Watchdog` label set on the same rendered tree. It reaches an unmuted route, and the test
      flags it.
    - The `BlackboxExporterDown` set still carries `shadow: "true"` and expects `blackhole`
      until M6's batch A3 (*The rules*). The red half for this outcome is the
      `IngressProbesMissing` set expected as `blackhole`. It reaches routes that page, and the
      test flags it.

Criterion 4 is a static proof on purpose. Prometheus is down for most of the weekly reboot
(Baseline), so a Sunday cannot be relied on to make these alerts fire. A Sunday on which no
alert fired proves nothing about the mute, which is failure class 3. The first Sunday after
PR 5 is recorded as supporting evidence only, under the rule in *Verification*.

## Baseline at `70591c307`

- **The tiles are generated.** `ansible/filter_plugins/kuma_monitors.py:kuma_ingress_monitors`
  yields one tile per `containers_list` entry with a `hostname`, unless the entry sets
  `kuma: false`. `static-monitors.yaml.j2` renders each tile as an HTTP monitor on
  `https://<hostname>.local.<domain>`:
    - It runs `interval: 60` with `maxretries: 2`, so a tile turns DOWN on the third
      consecutive failure, about 2 minutes after the first.
    - When the entry has an `auth_tier`, the probe expects Authelia's `302` with
      `max_redirects: 0`.
    - A tile without an `auth_tier` sets neither key, so Kuma's defaults apply: a 2xx status,
      reached through up to 10 redirects.
- **The counts.** Running the filter over `ansible/inventory/host_vars/daniel-box.yml` yields 26
  tiles, 23 of them behind SSO. The three without SSO are `auth-k8s` (entry `authelia`),
  `littlelink-k8s` (hostname `www`) and `home-assistant-k8s`. Live, Kuma exports
  `monitor_status{monitor_type="http"}` for 32 monitors: those 26 plus 6 hand-written HTTP
  monitors that M6 moves.
- **The alerting follows the service tier.** The `service_alerting` macro in
  `static-monitors.yaml.j2` gives a `home-critical` entry the `critical` tag and the email leg.
  Every other entry pages Discord only (`ansible/filter_plugins/service_tier.py:in_service_tier`,
  decided 2026-10-04, #3480). The filter's signature is `(containers_list, name, tier)`.
- **Kuma pins every ingress name to the ingress VIP.**
  `ansible/roles/k8s/uptime-kuma/templates/deployment.yaml.j2` renders `hostAliases` that map
  `<hostname>.local.<domain>` for every entry in
  `hostvars[k8s_primary_node].containers_list | selectattr('hostname', 'defined')`, plus each
  entry's `extra_hostnames` and `edge-selfcheck.local.<domain>`, to `k3s_metallb_ingress_vip`.
  The comment above it gives the reason: CoreDNS forwards to resolvers that can answer the
  public wildcard with the wrong address.
  `ansible/tests/services/test_kuma_static_monitors.py::test_the_all_clear_probe_host_is_pinned_to_the_ingress_vip`
  asserts that the Deployment renders the block. A pod without `hostAliases` resolves through
  cluster CoreDNS (`ansible/roles/setup/k3s/templates/coredns-corefile.j2`,
  `k3s_coredns_upstreams`).
- **The network path.** Traefik's `:8443` admits the pod CIDR through
  `netpol_baseline_traefik_https_sources` in `networkpolicy-traefik.yaml.j2`. The `homelab`
  baseline (`networkpolicy.yaml.j2`) admits the `prometheus` pod from `observability`, so
  Prometheus can scrape any exporter in `homelab` without a policy change.
- **The 302 limitation is known.** A `302` from Authelia proves that the edge and the
  middleware answer, not that the backend is up. `docs/failure-classes.md` lists
  `authelia-302-does-not-prove-the-backend` under class 2. This slice keeps parity with Kuma
  and does not fix it.
- **The 302 depends on the request's Accept header.** Read-only GETs to
  `https://homepage.local.<domain>/` on 2026-10-11 gave two answers:
    - 302 with `Accept: */*`, which is curl's default, and with `Accept: text/html`;
    - 401 with no Accept header, and with `Accept: application/json`.

  Upstream blackbox_exporter `prober/http.go` at v0.29.0 sets a default `User-Agent`
  (`userAgentDefaultHeader`) and copies the module's `headers`. It sets no Accept, and Go's
  `net/http` adds none. Kuma's SSO tiles accept only 302 with `max_redirects: 0`, and
  `count(monitor_status{monitor_type="http",monitor_url=~"https://[^/]+"} == 1)` read 26 on
  2026-10-11. Whatever the pinned Kuma (`uptime_kuma_k8s_image`) sends therefore draws a 302.
  A blackbox module with no Accept header would draw 401 on all 23 SSO targets.
- **The weekly reboot takes Prometheus down with everything else.** The `Weekly system
  restart` cron in `ansible/roles/setup/initial_setup/tasks/crons.yml` runs on every host at
  Sunday 07:30 UTC with `shutdown -r +5`. Measured for 2026-10-04 with `probe.py metric`:
    - Prometheus's last self-scrape before the reboot:
      `max_over_time(timestamp(up{job="prometheus"})[20m:15s] @ 1791099900)` = 1791099297,
      that is 07:34:57.
    - Its first self-scrape after it:
      `min_over_time(timestamp(up{job="prometheus"})[20m:15s] @ 1791101100)` = 1791100255,
      that is 07:50:55. kube-state-metrics series were back by 07:51:15.
    - From then to 08:15, no `homelab` Deployment with replicas wanted read 0 available.
      `max_over_time((count((kube_deployment_status_replicas_available{namespace="homelab"} == 0) and on(namespace,deployment) (kube_deployment_spec_replicas > 0)))[50m:1m] @ 1791101700)`
      returned no data. The same window held the series:
      `max_over_time(count(kube_deployment_spec_replicas{namespace="homelab"} > 0)[50m:1m] @ 1791101700)`
      = 55.

  Most of the outage therefore falls inside Prometheus's own 16-minute gap, and an alert with
  `for: 2m` cannot fire on a failure Prometheus never evaluated.
- **The reboot window is Kuma's maintenance** until M2's PR 3 adds the `weekly-reboot` mute
  ([M2](m2-first-bridge-family.md#the-weekly-reboot-mute)). M1's routing sends
  `severity="critical"` to Discord and email with no mute until then.
- **Deploy ordering comes from three sources only.**
  `ansible/filter_plugins/toposort.py:build_k8s_dep_map` reads an entry's `depends_on`, its
  `use_authelia` flag and whether it is routed (for the traefik edge). It never reads
  `entry_port`. The sort is stable. `scripts/dev/new_k8s_service.py:append_entry` appends a new
  entry after the list's last item, which is after `observability`.
  `ansible/tests/deploy/test_k8s_toposort.py:DOCUMENTED_ORDERINGS` holds every ordering that
  prose states, each as a `depends_on` edge. A simulation loaded the k8s entries of
  `daniel-box.yml` and ran `build_k8s_dep_map`, then `toposort_containers`. It printed these
  indices out of 56 or 57 roles:
    - unchanged: `observability` 19, `uptime-kuma` 42, `monitor-bridge` 48;
    - with an appended `blackbox-exporter` entry and no edge: the same three indices, and the
      exporter at 56, last;
    - with the appended entry and `depends_on: [blackbox-exporter]` on `observability`:
      `uptime-kuma` 41, `monitor-bridge` 47, the exporter 55 and `observability` 56, last.
- **`land.sh` runs one deploy per landing.**
  `scripts/deploy_tools/land_lib/tools.py:run_deploy` builds one argv,
  `./scripts/deploy.sh --tags <a,b>`, and `ansible/deploy.yml` walks the toposorted list in
  batches. `observability` sits before `uptime-kuma` in the list and no edge joins them, so a
  landing that deploys both applies `observability` first. That order is list position, not a
  contract, and this plan relies on neither order. `land.sh --help` says that under
  `LAND_HANDOFF_UNIT`, which the `claude` agent user's profile sets, `--tags` is refused and
  that user cannot merge or deploy by hand.
- **Every role on the alert path is denylisted from auto-deploy.**
  `k8s_autodeploy: false` in the `defaults/main.yml` of `monitor-bridge` ("this IS the alert
  pipeline"), `uptime-kuma` ("the alerting spine") and `observability`.
- **M1's validator, as specified, takes `service` from two places.** M1's *CI and local
  tooling* says `alert_rules.py` accepts an alert whose `service` is a literal label or comes
  through M2's recording rules. M1 convention 3 says the same. A label set on a scrape target
  is neither.
- **`probe.py metric` has no time flag.** `probe.py metric --help` prints only
  `[--json] promql`, so every query runs at now unless the PromQL carries `@`.
- **Other consumers of the filter.** `git grep kuma_ingress_monitors` names, beyond the Kuma
  template:
    - `ansible/tests/services/test_kuma_ingress_monitors.py`, which renders Kuma's entities;
    - the `_INGRESS_LOOP_RE` arm of `scripts/diagnostics/probe_lib/kuma_table_loop.py`, and
      `scripts/diagnostics/tests/test_probe_monitors.py::test_the_real_template_declares_every_ingress_tile_ungated`;
    - a branch of `ansible/roles/k8s/uptime-kuma/tests/test_status_page_groups.py:declarations`,
      whose
      `ansible/roles/k8s/uptime-kuma/tests/test_status_page_groups.py::test_the_declaration_census_finds_every_known_monitor`
      names `grafana-k8s` and floors the census at 90;
    - `scripts/deploy_tools/narrow_lib/containers.py:FILTER_FIELDS` and
      `scripts/lib/ansible_jinja_env.py`, which stay valid while any template calls the filter.
- **`probe.py monitors` reads Kuma only.**
  `scripts/diagnostics/probe_lib/monitors.py:format_monitor_status` formats
  `monitor_status`. Root `CLAUDE.md` says "`monitors` answers 'what is down'".
- **Kuma already pages a new scrape job's `up == 0`.**
  `ansible/roles/k8s/monitor-bridge/files/checks/cluster.py:check_cluster_targets` reads
  `up{origin!="daniel-server"}`. That selector matches every job without an `origin` label,
  and its docstring says the two monitor-bridge selectors split every `up` series between
  them. Any member at 0 fails the check. Its row `cluster_targets` in
  `ansible/roles/k8s/monitor-bridge/files/check_table.py:CHECKS` feeds the Kuma tile
  `monitor-bridge-cluster-targets`, "Cluster Scrape Targets". The row sets no
  `critical=True`, so the tile pages Discord only. `CLUSTER_TARGETS_CONSECUTIVE` is `"3"` in
  `ansible/roles/k8s/monitor-bridge/templates/env-secret.yaml.j2`, and monitor-bridge runs
  every 300s. The row's description says the DOWN message names the target that stayed down
  15 minutes. The comment above the `coredns-host` job in `prometheus.yaml.j2` relies on the
  same tile for that job. M1 convention 5 forbids an unshadowed `up == 0` rule for such a job
  until M6's batch A3 cuts this tile over.
- **No blackbox_exporter exists in the tree.** Upstream's latest release is v0.29.0
  (2026-10-07, `gh release list --repo prometheus/blackbox_exporter`).
- **The scaffolder** `scripts/dev/new_k8s_service.py` writes a role skeleton and its
  `containers_list` entry. Its flags include `--no-route`, `--port`, `--uid`,
  `--priority-class`, `--no-autodeploy` and `--autodeploy-reason`. Its `main` prints the two
  censuses a new role joins and writes neither: `BORN_FENCED_ROLES` in
  `ansible/tests/k8s/test_netpol_baseline_labels.py` and `ROLES_WITH_A_DEFAULT_SERVICE` in
  `ansible/tests/k8s/test_shared_manifest_defaults.py`. The comment above `BORN_FENCED_ROLES`
  describes its members as roles whose only caller is Traefik and which dial nothing.
- **The baseline fences ingress.** `networkpolicy.yaml.j2` in `netpol-baseline` sets
  `policyTypes` to `Ingress` alone.
- **promtool compares whole label sets.** Upstream `cmd/promtool/unittest.go` (v3.5.0) builds
  the expected alerts from `exp_labels` plus `alertname`, and compares them with the fired
  alerts' full labels through `cmp.Equal` under `labels.Equal`. A fired alert that lacks a
  label its `exp_labels` lists fails the case.

## Design

### The exporter

- **Role and placement.** `ansible/roles/k8s/blackbox-exporter/`, deploy tag
  `blackbox-exporter`, in the `homelab` namespace. It does not go in `observability`, because
  that namespace's baseline relies on having one tenant (M1 baseline). In `homelab` it sits
  behind the same NetworkPolicy baseline as Kuma.
- **Name resolution matches Kuma's.** Kuma's `hostAliases` block moves into a shared macro,
  `ingress_host_aliases()`, in `ansible/templates/ingress-host-aliases.yaml.j2`. Both
  Deployments call it, so the two lists cannot drift and the probes resolve every ingress name
  exactly as Kuma does. A CoreDNS or Pi-hole fault therefore stays out of the probe path, as it
  is for Kuma. A shared macro is chosen over a copied loop plus a parity test because the macro
  makes drift impossible rather than detectable. M6's non-ingress probes inherit the same block.
- **Workload.** A stateless `RollingUpdate` Deployment, `homelab-standard`, running as uid
  65534, image `prom/blackbox-exporter` pinned by digest as `blackbox_exporter_k8s_image`, at
  v0.28.0 or newer. A readinessProbe hits `/-/healthy`.
- **Scaffold command:**

  ```bash
  uv run python scripts/dev/new_k8s_service.py blackbox-exporter --image <pin> --port 9115 \
    --no-route --priority-class homelab-standard --uid 65534 --no-autodeploy \
    --autodeploy-reason "observability — the ingress pager once M4 cuts over; a broken deploy cannot page about being broken. Its readinessProbe (/-/healthy) proves the process runs, not that a probe succeeds"
  ```

- **Auto-deploy: denylisted.** Every other role on the alert path is denylisted (Baseline),
  and after PR 5 this role is the only ingress pager. `/-/healthy` is a class 2 proxy: it
  proves the process answers, not that a module still probes correctly. A Renovate bump
  therefore lands by hand, through `land.sh`.
- **No deploy-order edge.** The entry stays where the scaffolder appends it, and this plan
  adds no `depends_on` between the exporter and `observability`. It states no order between
  them either, so failure class 8 has nothing to guard. On a running cluster, PR 1 deploys
  the exporter before PR 3 adds the job, so Prometheus never scrapes a missing exporter. An
  edge `depends_on: [blackbox-exporter]` on `observability` was rejected for three reasons:
    - It moves `observability` from 19th to last in the derived order (Baseline), after
      `uptime-kuma` and `monitor-bridge`. M2's Baseline states the present order.
    - Under Flux, C3 generates `dependsOn` from these edges, and each per-tag Kustomization
      sets `wait: true`. The `observability` Kustomization, which carries Prometheus and
      Alertmanager, would then not reconcile while the exporter's is not Ready. A bad
      exporter bump would hold back every change to the pager.
    - It helps only a cluster built from nothing, where Prometheus scrapes the job before the
      exporter exists. Until M6's batch A3, `BlackboxExporterDown` then fires in shadow, and
      Kuma's tile pages only if the exporter is still missing after its 15-minute hold. From
      A3 on, the alert pages once and resolves when the exporter applies. *Risks* accepts
      that.
- **Config.** `files/blackbox.yml` in a ConfigMap read with `lookup('file')`. The role-level
  restart rolls the pod when the file changes. Two modules for this slice:
    - `http_2xx`: GET, `headers: {Accept: "*/*"}`, `follow_redirects: true`, the default 2xx
      status set, TLS verified. This matches Kuma's default for the three tiles without SSO.
    - `http_sso_302`: GET, `headers: {Accept: "*/*"}`, `follow_redirects: false`,
      `valid_status_codes: [302]`, TLS verified. Kuma accepts only `302` with
      `max_redirects: 0`. Authelia answers that 302 only to a request whose Accept header
      admits it, and answers 401 to a request with none (Baseline). The header is therefore
      what makes this module see the status Kuma sees. `http_2xx` carries the same header so
      that both modules send what Kuma sends.

  A pytest in `ansible/tests/services/` loads `files/blackbox.yml` and asserts that both
  modules carry `Accept: */*`. It names `http_2xx` and `http_sso_302` as members it must find.
  Its `_is_flagged` half is a module without the header. M6's non-ingress probes add
  `tcp_connect`, `dns` and a keyword module here.
- **Kuma's `kuma:` keys stay.** The probe job reads the same filter. M6 renames the filter and
  the key once Kuma is gone, and *Open questions* holds that rename.

### The probe job

In `ansible/roles/k8s/observability/templates/prometheus.yaml.j2`, a hand-written job
`blackbox-ingress`, scraped every 1m:

```yaml
- job_name: blackbox-ingress
  scrape_interval: 1m
  metrics_path: /probe
  static_configs:   # one item per tile from containers_list | kuma_ingress_monitors
    - targets: ['https://<hostname>.local.<domain>']   # the exact string Kuma's tile uses
      labels:
        service: <tile.entry>              # the deploy tag (C7)
        module: http_sso_302               # or http_2xx when tile.behind_sso is false
        page_tier: critical                # or warning: containers_list | in_service_tier(tile.entry, 'home-critical')
  relabel_configs:   # the standard blackbox multi-target rewrite
    # __address__ -> __param_target, module -> __param_module, __param_target -> instance,
    # __address__ <- the blackbox-exporter Service, port from containers_list | entry_port
```

The job creates no toposort edge, and this plan adds none (*The exporter*). A change to the
tile list changes `prometheus.yml`, so Prometheus rolls through its
`checksum/config`, exactly as a new `metrics` item does at the baseline. The same change rolls
the exporter pod, because its `hostAliases` read the same entries. *Open questions* asks
whether `file_sd_configs` should end the Prometheus roll.

### Where `service` comes from

`IngressProbeFailed` and `IngressProbeFailedCritical` watch 26 services, and each alert takes
`service` from its series. The series carries it because the scrape target sets it. M1's
validator, as specified, accepts only a literal or the M2 recording-rule join (Baseline), so it
would refuse both rules.

PR 2 extends `alert_rules.py` with a third source. An alert with no literal `service` passes
only when all three conditions hold:

1. Every vector selector in its expression carries a `job` matcher by equality, and all of
   them name the same job. A regex matcher, a selector with no `job` matcher, or two jobs
   fail the alert.
2. Every target of that job in the rendered `prometheus.yml` sets `service` in its `labels`.
   The script reads `prometheus.yml` from the same `validate.k8s_manifests` render it already
   uses for `alertmanager.yml`. One target without `service` fails the alert, so a hand-added
   target cannot slip through.
3. Every `exp_alerts` entry for the alert in its promtool test lists `service` in
   `exp_labels`. promtool compares whole label sets (Baseline), so an expression that drops
   `service` fails that case in the tool phase. This covers the shapes a lexical check would
   have to enumerate: an aggregation whose `by` omits `service`, such as
   `max(probe_success{job="x"}) == 0`, a `without (service)`, an `absent()`, and vector
   matching with `on()`.

Conditions 1 and 2 tie the real input series to `service`. Condition 3 proves the expression
keeps it. M1 convention 2 asks each firing case to assert `service`. M1 enforces a weaker
form: `scripts/validate/alert_conventions.py:rule_test_problems` flags an alert only when no
firing case lists both `service` and `severity` in every `exp_alerts` entry, so one asserting
case passes the alert however many other cases omit `service`. PR 2 requires every
`exp_alerts` entry to list `service`, for this source only. *Open questions* item 5 raises the
matching gaps in M1 convention 3 and C7, and asks whether M1 should require every entry for
every alert.

### The rules

`files/rules/ingress-probes.yaml`, group `ingress-probes`. Severity is a literal (M1
convention 3), and the `page_tier` target label selects which rule fires.

| Alert | Expression, in words | `for` | Severity | `service` | `shadow` |
|---|---|---|---|---|---|
| `IngressProbeFailedCritical` | `probe_success{job="blackbox-ingress",page_tier="critical"} == 0` | 2m | `critical` | the target's | `"true"` until PR 5 |
| `IngressProbeFailed` | the same with `page_tier="warning"` | 2m | `warning` | the target's | `"true"` until PR 5 |
| `IngressProbesMissing` | `absent(up{job="blackbox-ingress"})`, so the job left the config | 10m | `warning` | `observability` | none |
| `BlackboxExporterDown` | every `up{job="blackbox-ingress"}` is 0 at once | 5m | `critical` | `blackbox-exporter` | `"true"` until M6's batch A3 |

`for: 2m` matches Kuma's three-strikes timing at a 60s interval.

The two meta alerts are the class 3 guards for the probes, and they differ in whether Kuma
already watches their failure.

- **`IngressProbesMissing` ships without `shadow` from PR 3.** Kuma's Cluster Scrape Targets
  tile fails on a member at 0, and a job removed from the config leaves no member, so no Kuma
  check pages for this failure. M1 convention 5 gives no reason to shadow it. It pages under
  the `weekly-reboot` mute.
- **`BlackboxExporterDown` ships with `shadow: "true"` from PR 3.** The job's 26 `up` series
  carry no `origin` label, so they join Kuma's Cluster Scrape Targets tile with no Kuma edit
  (Baseline). A dead exporter turns that tile red, so an unshadowed rule would page the same
  failure twice, against M1 convention 5. Until M6's batch A3, the tile is the exporter-down
  pager. It pages Discord, after its 3-cycle hold of about 15 minutes, with a DOWN message
  that names the job's targets. The shadow rule pages after 5 minutes, by email as well. The
  commit in M6's batch A3 that cuts the tile over makes three changes for this alert. It
  drops the `shadow`, flips the alert's routing outcome to `muted`, and adds an inhibit over
  A3's scrape-target alert for this job (*Open questions* item 8).

A dead exporter leaves `probe_success` stale, so the per-target rules go quiet and Kuma's
tile is what pages. Neither module sets `timeout`. The exporter therefore probes within the
scrape timeout, less its offset, and a slow backend reads `probe_success 0` rather than
failing the scrape. A failed scrape would set that one target's `up` to 0, and the tile would
page a single ingress failure a second time.

### The weekly reboot and the routing proof

Both nodes reboot at about 07:35, and Prometheus is down until about 07:51 (Baseline). The
probes may fail for a few minutes after that, or not at all. The reboot is therefore not a red
test of these rules, and a quiet Sunday says nothing about the mute.

The mute is proven statically instead, by M2's routing pytest
([M2](m2-first-bridge-family.md#the-weekly-reboot-mute)). M2's PR 3 writes it in
`ansible/tests/services/`. It renders the observability role's Alertmanager Secret through
`ansible/tests/_k8s_render.py:render_role_template` and walks the routing tree of
`alertmanager.yml` the way Alertmanager does: children in order, stopping at the first match
unless `continue: true`. Its red half is the `Watchdog` label set, which reaches the Watchdog
route, which never takes the mute; the test must flag it. M4 writes no second walk and owns
no part of it. PR 3 adds the four alerts' label sets to it, each with an expected outcome:

- **`muted`**: every route the set reaches carries `weekly-reboot` in `mute_time_intervals`.
  This is M2's assertion. It applies to `IngressProbesMissing` from PR 3, and to the two
  `IngressProbeFailed*` sets from PR 5. It applies to `BlackboxExporterDown` from M6's batch
  A3, which drops that alert's `shadow`.
- **`blackhole`**: every route the set reaches has the `blackhole` receiver. It applies to
  each set while it carries `shadow: "true"`: the two `IngressProbeFailed*` sets from PR 3 to
  PR 5, and `BlackboxExporterDown` from PR 3 until M6's batch A3. M1 has no test that
  `shadow="true"` reaches the blackhole route, so this outcome is that proof for this family.

A single predicate, "blackhole receiver or `weekly-reboot`", was rejected. A routing bug that
sent `IngressProbesMissing` to the blackhole would pass it, and that is a pager that never
pages (failure class 3). M2's text asserts `muted` for every set, so PR 3 also turns the
walk's one assertion into a per-set outcome, unless M2 has already shipped it that way. The
PR body records `amtool config routes test` on the rendered file for each label set, to show
that the walk and Alertmanager pick the same receivers. M2's PR 3 owns the proof that the
interval itself is Sunday 07:25 to 08:15 UTC.

### Soak queries, joined on the URL

Kuma's monitor URL is the probe target, so the two sides join on it. `<H>` is the whole hours
since the soak clock started, at most 168 (M1 convention 6). PR 3 says when the clock starts.

Query 1 counts the minutes Kuma read DOWN while no shadow alert fired:

```text
count_over_time((
  label_replace(monitor_status{monitor_type="http",monitor_url=~"https://[^/]+"} == 0,
                "instance", "$1", "monitor_url", "(.+)")
  unless on (instance)
  ALERTS{alertstate="firing",shadow="true",alertname=~"IngressProbeFailed.*"}
)[<H>h:1m])
```

Query 2 counts the minutes a shadow alert fired while Kuma read up:

```text
count_over_time((
  ALERTS{alertstate="firing",shadow="true",alertname=~"IngressProbeFailed.*"}
  unless on (instance)
  label_replace(monitor_status{monitor_type="http",monitor_url=~"https://[^/]+"} != 1,
                "instance", "$1", "monitor_url", "(.+)")
)[<H>h:1m])
```

The `monitor_url=~"https://[^/]+"` selector, which RE2 anchors at both ends, keeps only the
path-less ingress URLs. It drops the six hand-written HTTP monitors. Kuma reports maintenance as
3, not 0, so the reboot window falls out of query 1 by itself. In query 2, `!= 1` treats
maintenance and pending as not up, so a shadow alert inside the window does not count either.
A non-empty result from query 2 blocks PR 4 until each episode is explained or the rule is
tuned. The likely episodes:

- Kuma's retry timing against `for: 2m`.
- A timeout difference: Kuma's default against blackbox's scrape-bounded timeout.
- A redirect difference on the three tiles without SSO.
- An Accept-header difference. Authelia answers 401 to a request with no Accept header
  (Baseline), so a module that lost its `headers` fails every SSO target.

### The tree

```diff
+ansible/roles/k8s/blackbox-exporter/       scaffolded: defaults, tasks, templates, CLAUDE.md
+  files/blackbox.yml
+ansible/templates/ingress-host-aliases.yaml.j2  ingress_host_aliases(), moved from uptime-kuma
 ansible/inventory/host_vars/daniel-box.yml
~  containers_list: the blackbox-exporter entry, appended; no depends_on
 ansible/tests/k8s/
~  test_netpol_baseline_labels.py            BORN_FENCED_ROLES gains blackbox-exporter (PR 1)
~  test_shared_manifest_defaults.py          ROLES_WITH_A_DEFAULT_SERVICE gains it (PR 1)
 ansible/tests/services/
+  the Accept-header pytest                  both modules carry Accept: */* (PR 1)
~  M2's routing pytest                       four label sets with their outcomes (PR 3)
 ansible/roles/k8s/observability/
~  defaults/main.yml                         observability_rule_groups gains ingress-probes (PR 3)
~  templates/prometheus.yaml.j2              the blackbox-ingress job
+  files/rules/ingress-probes.yaml
+  tests/rules/ingress-probes.test.yaml
 ansible/roles/k8s/uptime-kuma/
~  templates/deployment.yaml.j2              calls ingress_host_aliases()
~  templates/static-monitors.yaml.j2         the ingress loop removed in PR 4
 scripts/validate/
~  alert_rules.py                            service from a scrape job; --prove-red runs a
                                             second rule test (PR 2)
~  tests/fixtures/alert_rules/               gains the aggregation rule test (PR 2)
 scripts/diagnostics/probe_lib/
~  monitors.py                               lists firing ingress alerts (PR 4)
~  kuma_table_loop.py                        the _INGRESS_LOOP_RE arm removed (PR 4)
```

## Steps, as PRs

### PR 1 — The blackbox-exporter role (about 200 lines)

- Run the scaffold command above, then add `files/blackbox.yml`, the ConfigMap and the two
  modules, each with `headers: {Accept: "*/*"}`. Add the Accept-header pytest that *The
  exporter* describes, with its `_is_flagged` half.
- Move Kuma's `hostAliases` block into `ingress_host_aliases()` and call it from both
  Deployments. The uptime-kuma render must stay byte-identical, and
  `test_the_all_clear_probe_host_is_pinned_to_the_ingress_vip` must stay green.
- Leave the entry where the scaffolder appends it, and add no `depends_on` (*No deploy-order
  edge*). The PR body quotes the Baseline's simulation re-run on the branch: `observability`,
  `uptime-kuma` and `monitor-bridge` keep 19, 42 and 48.
- Add the two census rows by hand, because the scaffolder only prints them (Baseline):
    - `BORN_FENCED_ROLES` gains `blackbox-exporter`. Its comment says how it departs from the
      set's preamble. The role has `--no-route`, so Traefik never calls it. Its only caller is
      the `prometheus` pod from `observability`, which the `homelab` baseline already admits.
      The pod dials every ingress name, but the baseline fences ingress only, so membership
      does not touch those probes. From PR 3, `up{job="blackbox-ingress"}` shows that the
      caller still gets through. When it does not, Kuma's Cluster Scrape Targets tile pages
      until M6's batch A3, and `BlackboxExporterDown` pages from A3 on.
    - `ROLES_WITH_A_DEFAULT_SERVICE` gains it, because Prometheus scrapes it through the
      shared default Service.
- The role's `CLAUDE.md` follows the scaffold's template.
- C4 check: after running the scaffolder, run
  `git diff ansible/inventory/host_vars/daniel-box.yml | grep -n 'owner: flux'`. A match means
  D5's first Wave A PR has landed and the scaffolder wrote `owner: flux` on the entry. Then this
  PR also adds the role's Kustomization (C3), and the Ansible k8s play, `deploy.sh`, the
  deployer and `land.sh` skip it (C4). No match means it is an ordinary Ansible role, and D5
  moves it with the rest. Key on the entry the scaffolder wrote, not on a slice name or on the
  scaffolder's source. At the baseline, `scripts/dev/new_k8s_service.py:entry_lines` writes
  only `name`, `platform`, `port` and the route keys.
- Deploys: about 24 tags, most of them roles this PR does not touch.
    - PR 1 edits `ansible/inventory/` and `ansible/templates/`, both in
      `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_changes.py:_BROAD_DEPLOY_PREFIXES`.
      The GitOps tick therefore applies it as `deploy.yml` with the tags that
      `deploy_tags.py narrow` derives (`docs/gitops-pipeline.md`, *A deploy-plane change is
      narrowed before it is applied*). That list is not filtered through the auto-deploy
      denylist, so denylisted roles run too.
    - An added entry moves every reader of the list.
      `scripts/deploy_tools/narrow_lib/containers.py:changed_fields` returns None for it, and
      `scripts/deploy_tools/narrow_lib/containers.py:reader_paths` then keeps every template
      that renders `containers_list`. Run at `70591c307` with `changed=None`,
      `scripts/deploy_tools/narrow_lib/broad.py:_list_reader_tags` named 23 tags of 59
      declared: `artifacts`, `authelia`, `autofix-bridge`, `cloudflare-ddns`, `crowdsec`,
      `game-stats`, `gpu-exporter`, `home-assistant`, `homepage`, `monitor-bridge`,
      `mosquitto`, `netpol-baseline`, `nut`, `nut-exporter`, `observability`,
      `pi-peer-backup`, `pihole`, `pihole-exporter`, `registry`, `traefik`, `uptime-kuma`,
      `wg-easy` and `zigbee2mqtt`. With the entry's own tag that is 24 of 60. The derivation
      refuses only when `len(tags) * 2 > len(ctx.declared)`, so it applies all 24.
    - Before merging, the PR body records the list that
      `uv run python scripts/deploy_tools/deploy_tags.py narrow origin/master HEAD` prints on
      the branch, and which renders moved, from a `--dry-run`. The entry carries no `metrics`
      and no `hostname`, so neither `prometheus.yml` nor Kuma's `hostAliases` changes, and
      Prometheus does not roll. No edge is added, so every other role keeps its place in the
      derived order.
    - Each role's own verify tasks run whether or not its render moved. A `crowdsec` deploy
      runs the ban gate in `ansible/roles/k8s/crowdsec/tasks/verify.yml`, which M2's PR 2
      describes, and `docs/crowdsec-waf-record.md` records that gate failing a deploy on
      2026-09-27.
    - Land PR 1 in a quiet window: no other landing in flight, and outside the Sunday 07:25 to
      08:15 UTC reboot window.
    - **What a gate failure in an untouched role leaves.** A broad arm is forward-only
      (`ansible/roles/setup/gitops_deploy/CLAUDE.md`, *Safety*). The tree stays merged, and the
      tick writes `hold_sha` and a `hold_plane` entry, which hold every later landing (root
      `CLAUDE.md`, *When to wait*). `land.sh` reports `deploy-failed` with "a playbook task
      failed AFTER applying; some changes are live", which the `land-after-merge` skill says
      is not a resume point. A role applied before the failing task whose render did not
      move rolled nothing. The exporter sorts last (Baseline), so it may not have applied at
      all.
    - **Recovery.** Read the failing gate's rescue message. crowdsec's names the hop that
      failed. Once its cause has cleared, the operator re-applies the held plane, which clears
      the hold (`docs/gitops-pipeline.md`, *Which apply clears a hold*). Then run
      `probe.py health blackbox-exporter`.
- Two-writer check: nothing scrapes the exporter yet, so no pager changes.

### PR 2 — The validator's third `service` source (about 110 lines)

- Extend `alert_rules.py` as *Where `service` comes from* describes.
- Its `_is_clean`/`_is_flagged` pairs join M1's `alert_rules` suite in
  `scripts/validate/tests/`. Each case pairs a rule with a fixture `prometheus.yml` and a
  promtool test:
    - clean: `probe_success{job="x"} == 0`, whose job's every target sets `service`, and whose
      firing case lists `service` in `exp_labels`;
    - flagged, condition 2: the same rule after one target loses `service`;
    - flagged, condition 1: the rule with `job=~"x"`;
    - flagged, condition 1: the rule with no `job` matcher;
    - flagged, condition 1: `probe_success{job="x"} == 0 and up{job="y"} == 1`, two jobs;
    - flagged, condition 3: `max(probe_success{job="x"}) == 0`, whose firing case omits
      `service` from `exp_labels`.
- Add the same aggregation with a firing case that lists `service` to
  `scripts/validate/tests/fixtures/alert_rules/`. M1's `--prove-red` exits 0 when each tool
  rejects its one fixture, so PR 2 extends it: `promtool test rules` must also reject this
  second rule test. It fails because the fired alert carries no `service`. This proves that
  condition 3 rests on promtool's whole-set comparison rather than on the Python phase alone.
- No deploy. No rule uses the path yet, so this PR needs only M1.

### PR 3 — The probe job and its rules (about 200 lines)

- Requires M2's PR 3, the `weekly-reboot` mute.
- Add the `blackbox-ingress` job, `ingress-probes.yaml` and its promtool tests, and add
  `ingress-probes` to `observability_rule_groups` (M1 convention 1). Each alert gets a firing
  case that asserts its `service`, `severity` and `shadow` labels as this PR ships them. The
  zero-series case removes the job's `up` and expects `IngressProbesMissing`.
- Add a pytest in `ansible/tests/services/` that renders the job and asserts:
    - its targets equal `https://<tile.hostname>.local.<domain>` for each tile the filter
      yields, string for string. The filter's output carries no URL (its keys are `id`,
      `name`, `hostname`, `behind_sso` and `entry`), so the test builds the string the way
      Kuma's template does. This assertion outlives PR 4;
    - the soak-join check: its targets equal the `url` of each ingress tile in the rendered
      `static-monitors.yaml.j2`, string for string, which the soak queries' join needs. PR 4
      deletes the loop this reads, and retires this one assertion;
    - each target's `service`, `module` and `page_tier` follow the tile's entry,
      `behind_sso` and tier;
    - an added entry with a `hostname` gets a target with no template edit, and `kuma: false`
      removes it;
    - each target's host appears in the exporter Deployment's rendered `hostAliases`.

  It names `homepage` and `authelia` as members it must find. Its `_is_flagged` half is an
  entry whose `hostname` the job lacks.
- Add the four alerts' label sets, as this PR ships them, to M2's routing pytest, with the
  outcomes *The weekly reboot and the routing proof* gives: `blackhole` for the three shadow
  alerts, the two `IngressProbeFailed*` and `BlackboxExporterDown`, and `muted` for
  `IngressProbesMissing`. Its red half for the `blackhole` outcome is the
  `IngressProbesMissing` set expected as `blackhole`. That set carries no `shadow` and
  reaches the routes that page, so the walk must flag it.
- Deploys `observability`. Prometheus rolls once, for the config.
- After the deploy, read `prometheus_config_last_reload_successful`, which must be 1, and
  `prometheus_config_last_reload_success_timestamp_seconds`, which must be later than the
  deploy's start, through `probe.py metric`. Record both in the PR. The pod rolls in this PR,
  so its startup load sets both, and a stale pod would fail the second read.
- After the deploy, before the soak's first query, run the live pint pass
  (`scripts/validate/pint/live.hcl`, from M2) on `ingress-probes.yaml`. The series it checks
  exist only once this job runs. A finding blocks PR 4.
- Two-writer check: the two `IngressProbeFailed*` alerts and `BlackboxExporterDown` route to
  the blackhole receiver. The job's `up` series join Kuma's Cluster Scrape Targets tile in
  this commit, so a dead exporter pages from that tile alone. `IngressProbesMissing` pages,
  under the mute, and no Kuma check watches a job that left the config.
- **Soak.** Start the clock only once
  `count(probe_success{job="blackbox-ingress"} == 1)` equals the tile count (26 at the
  baseline), or each target missing from that count is explained in the PR. A count of the
  failing series cannot serve as the gate: `== 0` returns no series both when nothing fails
  and when the job is absent. Soak at least 7 days from that start, including one Sunday. Run
  both soak queries on day 3 and after the Sunday, and record the start time and each result
  in the PR.

### PR 4 — The Kuma side of the cutover (about 40 lines added, about 90 removed)

- Remove the `kuma_ingress_monitors` loop and its comment block from
  `static-monitors.yaml.j2`.
- `probe.py monitors` gains a second instant query,
  `ALERTS{alertstate="firing",alertname=~"IngressProbe.*|BlackboxExporterDown"}`, with no
  `shadow` filter. It prints one line per firing alert, naming its `service`, and exits
  non-zero. A failed query prints a failure line rather than reading as clean. This keeps root
  `CLAUDE.md`'s "`monitors` answers what is down" true until M6 Phase B replaces the
  subcommand. Its `_is_clean`/`_is_flagged` pair joins
  `scripts/diagnostics/tests/test_probe_monitors.py`.
- Delete the `_INGRESS_LOOP_RE` arm of `kuma_table_loop.py`, and retire
  `test_the_real_template_declares_every_ingress_tile_ungated`. Its subject is gone.
- In PR 3's target pytest, retire the soak-join check, which compares the targets with the
  `url` that the deleted loop rendered. The assertion that builds each target from the filter
  stays.
- In `ansible/tests/services/test_kuma_ingress_monitors.py`, retire the six tests that render
  Kuma's entities or pin AutoKuma ids. PR 3's target pytest covers the three properties that
  carry over to the probe job: an added entry gets a probe, `auth_tier` picks the module, and
  `kuma: false` opts out. Keep the filter's own
  `test_kuma_ingress_monitors_is_flagged_on_a_malformed_kuma_key` and
  `test_kuma_ingress_monitors_is_clean_on_an_override_and_forces_the_suffix`, because the probe
  job still calls the filter. No `ENFORCED` citation names any retired test at the baseline.
- In `test_status_page_groups.py`, drop the `kuma_ingress_monitors` branch of
  `declarations`. Swap `grafana-k8s` in `KNOWN_IDS` for a hand-written id, and lower the
  `>= 90` floor to the count measured after the removal.
- `ansible/tests/services/test_kuma_static_monitors.py` names no generated ingress id at the
  baseline. Run it, and change nothing unless it fails.
- Deploys `uptime-kuma`. AutoKuma deletes the 26 monitors and their history.
- Two-writer check: in this commit the ingress probes have no pager, which is the safe
  direction of M1 convention 5. PR 5 closes the window.
- **Gate for PR 5.** AutoKuma reconciles asynchronously, so the deploy's exit is not the
  signal. Merge PR 5 only once exit criterion 2's first query reads no series and its second a
  non-zero count.

### PR 5 — Cut over the pager (about 20 lines)

- Drop `shadow: "true"` from `IngressProbeFailed` and `IngressProbeFailedCritical`, and update
  their promtool cases. In M2's routing pytest, drop `shadow` from their two label sets and
  change their outcome from `blackhole` to `muted`.
- Touch only observability's rule tree, its promtool tests and M2's routing pytest. The
  cutover's critical path then holds no deploy-plane path, such as `ansible/filter_plugins/`,
  that `land.sh` would have to narrow.
- Branch it from master, not from PR 4. The repo squash-merges, so a branch carrying PR 4's
  commits turns CONFLICTING with no CI once PR 4 lands. The two PRs touch different files, so
  PR 5's CI runs in parallel with PR 4's landing, and the gate above orders the two merges.
  Land PR 5 in the same session as PR 4. That bounds the no-pager window to PR 4's reconcile
  plus PR 5's landing.
- Deploys `observability`. Only the `prometheus-rules` ConfigMap changes, and it carries no
  checksum annotation (M1 *Prometheus side*), so no pod rolls. The deploy's success therefore
  does not show that Prometheus read the new file. Prometheus reads it after the kubelet
  propagates the ConfigMap into the mounted directory and the next `--config.auto-reload`
  pass runs.
- After the deploy, read `prometheus_config_last_reload_successful`, which must be 1, and
  `prometheus_config_last_reload_success_timestamp_seconds`, which must be later than the
  deploy's start, through `probe.py metric`. Record both in the PR. PR 4's no-pager window
  ends only when both reads pass. Until then the ingress probes page nowhere, as *A partial
  cutover* in *Rollback* describes.
- Two-writer check: once both reads pass, the ingress probes page from Alertmanager only.
  `BlackboxExporterDown` keeps `shadow`, because Kuma's Cluster Scrape Targets tile still
  pages a dead exporter until M6's batch A3.
- **The first Sunday after the merge** is recorded in the PR, under the rule in
  *Verification*.

PR 4 and PR 5 are separate because one landing cannot order two deploys, and neither order
would be safe. `land.sh` runs one `deploy.sh` over both tags, and the agent user cannot pass
`--tags` (Baseline). The split does not rest on which tag `ansible/deploy.yml` applies first:

- If `observability` applies first, as list position gives at the baseline, Kuma and
  Alertmanager both page until AutoKuma deletes the tiles.
- If `uptime-kuma` applies first, AutoKuma still deletes the tiles after its deploy returns,
  because it reconciles asynchronously. `shadow` can then drop while the tiles still page,
  with the same result.

One PR would be failure class 5, a sequence treated as a transaction. Because the reasoning
holds in either order, no ordering edge is needed to protect it (failure class 8). M2 splits
its own cutover into PR 4a and PR 4b for the same reason.

### PR 6 — The filter's docstring (about 10 lines)

- Rewrite the module docstring of `ansible/filter_plugins/kuma_monitors.py`, which names
  `static-monitors.yaml.j2` as the filter's consumer. Name the probe job instead.
- It is a separate PR because `land.sh` narrows an `ansible/filter_plugins/` change to every
  role that names either filter the module registers. That widening stays off the cutover.
  No render changes, so the deploy rolls nothing.

## Verification

```bash
# Every tile is probed, and each probe carries its service
uv run python scripts/diagnostics/probe.py metric 'count(probe_success{job="blackbox-ingress"})'
uv run python scripts/diagnostics/probe.py metric 'probe_success{job="blackbox-ingress"} == 0'
uv run python scripts/diagnostics/probe.py metric 'count by (page_tier) (probe_success{job="blackbox-ingress"})'
uv run python scripts/diagnostics/probe.py health blackbox-exporter
# The soak clock starts when this equals the tile count (26 at the baseline)
uv run python scripts/diagnostics/probe.py metric 'count(probe_success{job="blackbox-ingress"} == 1)'
# After PR 3 and PR 5: 1, and a timestamp later than the deploy's start
uv run python scripts/diagnostics/probe.py metric 'prometheus_config_last_reload_successful'
uv run python scripts/diagnostics/probe.py metric 'prometheus_config_last_reload_success_timestamp_seconds'
# After PR 4: no ingress tile is left in Kuma, and the Kuma scrape is alive
uv run python scripts/diagnostics/probe.py metric 'count(monitor_status{monitor_type="http",monitor_url=~"https://[^/]+"})'
uv run python scripts/diagnostics/probe.py metric 'count(monitor_status{monitor_type="http"})'
# The first Sunday after PR 5, anchored to the mute window. <T> is that Sunday's 08:15 UTC
# as a unix time (date -u -d 'YYYY-MM-DD 08:15' +%s), recorded in the PR.
uv run python scripts/diagnostics/probe.py metric 'increase(alertmanager_notifications_total{integration=~"discord|email"}[50m] @ <T>)'
uv run python scripts/diagnostics/probe.py metric 'count by (alertname) (max_over_time(ALERTS{alertstate="firing",alertname!="Watchdog"}[50m] @ <T>))'
```

How to read the Sunday record:

- Every Discord and email route takes the mute. Any rise in the first query inside 07:25 to
  08:15 is therefore a failure, whichever alert caused it.
- When the second query names at least one alert and the first reads 0, the mute held for a
  real alert.
- When the second query returns no data, nothing fired and the record proves nothing about the
  mute. The routing pytest (criterion 4) stands alone. That is not a pass.

**Red proofs for the new checks:**

- Each alert: its promtool firing case.
- The target pytest: its `_is_flagged` half, an entry whose `hostname` the job lacks.
- The Accept-header pytest: its `_is_flagged` half, a module without the header. The live
  counterpart is the Baseline's 401 for a request with no Accept header.
- M2's routing pytest, `muted` outcome: M2's `Watchdog` red half on the real rendered tree,
  which M4 inherits rather than writes.
- M2's routing pytest, `blackhole` outcome: the `IngressProbesMissing` set expected as
  `blackhole`, which carries no `shadow` and reaches the routes that page (PR 3).
- The validator's `service`-from-job rule, one flagged case per condition (PR 2):
    - condition 1: a regex `job` matcher, no `job` matcher, and two jobs;
    - condition 2: the fixture job with one target missing `service`;
    - condition 3: an aggregation whose firing case omits `service`, plus the `--prove-red`
      fixture whose firing case lists `service` and fails in promtool.
- The `probe.py monitors` addition: a firing `IngressProbeFailed` series in its fixture.
- `BlackboxExporterDown`: a promtool case with every target's `up` at 0, whose `exp_labels`
  list `shadow: "true"`. There is no live drill, because stopping the exporter is a cluster
  write. The same case proves nothing about Kuma's tile, which is the pager until M6's batch
  A3. That tile's own check is `check_cluster_targets`, and this slice does not change it.
  The inhibit that A3 adds over this alert has its proof and red half in M6 (*Open
  questions* item 8).
- No live red proof exists for the per-target rules. A real ingress failure during the soak,
  seen by both sides of query 1, is recorded when one happens.

## Rollback

Roll back in the reverse of the landing order: PR 5, PR 4, PR 3, then PR 2 and PR 1. PR 6
reverts at any point. PR 3 goes before PR 2, because M1's validator without the third source
refuses PR 3's two per-target rules. PR 3 goes before PR 1, because its job scrapes the
exporter through the entry's port. A job left behind would scrape a missing exporter, which
turns Kuma's Cluster Scrape Targets tile red and fires `BlackboxExporterDown`.

- **PR 5, then PR 4.** Revert PR 5 first and deploy `observability`, which restores `shadow`.
  Before reverting PR 4, confirm the reload with PR 5's two reads. Without them, Prometheus
  may still evaluate the unshadowed rules when the tiles return. Then revert PR 4 and deploy
  `uptime-kuma`. AutoKuma recreates the 26 tiles under their old
  ids, with empty history. This order never has both pagers live.
- **A partial cutover.** If PR 4 deployed and PR 5 cannot land, the ingress probes page
  nowhere. `probe.py monitors` still lists them. Land PR 5, or revert PR 4 and deploy
  `uptime-kuma`.
- **PR 3.** Revert and deploy `observability`. The job and the rules go, and the job's `up`
  series leave Kuma's Cluster Scrape Targets tile. Only `IngressProbesMissing` paged from
  this PR, so nothing else loses a pager.
- **PR 2, after PR 3's rollback.** Revert. Nothing deploys.
- **PR 6.** Revert. No render changes either way.
- **PR 1, after PR 3's rollback has deployed `observability`.** A plain revert does not
  remove the objects. It deletes the role and the entry, so the role's prune never runs again,
  and the Deployment, the ConfigMap and the shared default Service stay live. Retiring the
  objects through the role's own prune, in two PRs, does not work either:
    - The entry's `port` earns the shared default Service (`ansible/roles/k8s/manifests/CLAUDE.md`,
      *The file lists are derived*). A PR that removes only the templates keeps that Service
      rendered and live.
    - Dropping `port` as well leaves the derived file list empty. The manifests role's task
      "Check that a role arming the prune renders something to keep" asserts
      `manifests_files_resolved | length > 0` and fails the deploy before any prune runs.
    - The include's default `manifests_rollout` would queue a wait on a Deployment that the
      same run prunes.

  Retire the service instead, as `docs/deploying.md` *Retiring a k8s service* describes, in
  one PR:
    - Delete the role directory and its `containers_list` entry. The shared macro stays, with
      Kuma as its only caller.
    - Add `blackbox-exporter` to `k8s_retired_services` in
      `ansible/inventory/group_vars/all.yml`. The comment above that key says its test refuses
      an entry that is still a role or a `containers_list` entry, so the three changes land
      together.
    - Drop `blackbox-exporter` from `BORN_FENCED_ROLES` and from `ROLES_WITH_A_DEFAULT_SERVICE`,
      because the role is gone. `test_exactly_the_fenced_roles_carry_the_baseline_label`
      compares the first with the rendered pod labels in both directions, so that row cannot
      stay.

  The removed entry fans out to the same readers as PR 1's added one, because
  `changed_fields` returns None for a removed entry too. Land it in a quiet window, record the
  tag list as PR 1 does, and expect the same gate failure and recovery.

  After the PR lands, the next k8s deploy, whatever its `--tags`, runs
  `ansible/tasks/k8s_retire.yml` for the entry. It deletes every object the service's staged
  directory declares, the shared default Service included, then removes that directory and
  the release records. No role runs for the tag, so no rollout wait is queued. The PR edits
  `ansible/inventory/group_vars/all.yml`, which `land.sh` may treat as a broad change and not
  deploy itself. After that deploy runs, record on the PR the teardown's deletions and a
  read-only `kubectl -n homelab get deploy,service,configmap | grep blackbox-exporter` that
  prints nothing.

  If the Deployment must go at once, deleting it is a kubectl write (**operator-only**). Under
  Flux (PR 1's C4 check), deleting the role's Kustomization prunes all three objects (C3
  `prune: true`).

## Risks

- **Class 2, a proxy for the property.** A `302` behind SSO proves the edge, not the backend.
  Mitigation: parity with Kuma, stated in each rule's `description`. M6's `probe.py health`
  reads `ALERTS` too, so a dead backend behind a healthy edge still shows through its workload
  rules from M2.
- **Class 2, again.** `/-/healthy` proves the exporter runs, not that it probes correctly.
  Mitigation: the role is denylisted from auto-deploy, so a bump lands by hand, and a module
  change that fails every probe pages 26 times rather than staying quiet.
- **Class 3, empty read as clean.** A job dropped from the config reads as no failures.
  Mitigation: `IngressProbesMissing` on `absent(up)`, unshadowed from PR 3, and the target
  pytest.
- **Class 3, a quiet Sunday read as a working mute.** Mitigation: criterion 4 is M2's routing
  pytest, and *Verification* says that a Sunday with nothing firing proves nothing.
- **Class 3, a meta alert routed to the blackhole.** A routing check that accepted "blackhole
  or muted" for every set would pass an `IngressProbesMissing` that never pages. Mitigation:
  each label set carries one expected outcome, and the `blackhole` outcome has its own red
  half, the `IngressProbesMissing` set expected as `blackhole`.
- **A dead exporter pages later, and by Discord only, until M6's batch A3.** After PR 5 the
  exporter is the only ingress pager. While `BlackboxExporterDown` stays in shadow, Kuma's
  Cluster Scrape Targets tile pages instead, after about 15 minutes rather than 5, and its
  row is not `critical`, so no email goes out. An ingress failure inside those 15 minutes
  pages nobody.
  Mitigation: M1 convention 5 allows one pager per check, and the tile already is that pager
  (Baseline). The tile's DOWN message names the job's targets. M6's batch A3 drops the
  `shadow` in the commit that cuts the tile over, which restores the 5-minute critical page.
  The same commit adds the inhibit in *Open questions* item 8, so A3's per-target alert does
  not page the same outage a second time.
- **Class 5, a sequence treated as a transaction, at M6's batch A3.** The inhibit and the
  shadow drop are edits to two files. Both are in the observability role and deploy under
  one tag, but Prometheus reloads the rules and Alertmanager reads its config separately.
  An inhibit whose source still routes to the blackhole would silence A3's alert for this
  job, and a dead exporter would then page nobody. Alertmanager's inhibitor reads every
  alert, whatever its route (*Open questions* item 8). A shadow drop without the inhibit
  pages one outage from two rules. Mitigation: item 8 puts both edits in one commit, and
  its source matcher `shadow!="true"` keeps an inhibit that applies first inert. M6 owns
  the commit and its test.
- **Class 2, a job selector standing in for the alert's labels.** An expression can name the
  job and still aggregate `service` away. Mitigation: condition 3 of the third `service`
  source, which rests on promtool's whole-set comparison and has a `--prove-red` fixture.
- **Class 3, the readers.** Between PR 4 and M6 Phase B, Homepage's `uptimekuma` widget,
  homelab-mcp's Kuma tool and the Grafana board `Apps/uptime-kuma-monitors.json` show no
  ingress outage. Mitigation: `probe.py monitors` lists firing ingress alerts from PR 4, so the
  agent path and root `CLAUDE.md` stay right. The three human-facing readers wait for M6
  Phase B, and Alertmanager pages for every ingress failure in that time.
- **Class 4, a selector drifting from the hazard.** A routed service must be probed.
  Mitigation: the job and Kuma read the same filter until M6. The target pytest pins every
  target to the filter's output, and until PR 4 it also compares the targets with Kuma's
  rendered URLs.
- **Class 5, a sequence treated as a transaction.** Mitigation: PR 4 and PR 5 are separate
  landings with a gate between them, and the rollback runs in the reverse order.
- **Class 7, a different execution context.** Without `hostAliases`, every probe would depend
  on CoreDNS and its upstreams while Kuma does not. A request with no Accept header draws
  Authelia's 401 where Kuma's request draws 302 (Baseline). Mitigation: the shared macro, and
  `Accept: */*` on both modules, which the Accept-header pytest pins. The soak clock starts
  only once every target reads `probe_success 1`.
- **Class 3, a green deploy read as a loaded config.** PR 5 rolls no pod, so its deploy can
  succeed while Prometheus still runs the old rules, and PR 4's no-pager window stays open.
  Mitigation: PR 3 and PR 5 each read `prometheus_config_last_reload_successful` and its
  timestamp after the deploy, and PR 5's window closes only on both reads.
- **PR 1 deploys the gates of 23 roles it does not touch.** A gate failure there, such as
  crowdsec's ban gate, holds every later landing. Mitigation: the PR body records the derived
  tag list, PR 1 lands in a quiet window, and PR 1 states the recovery. The retirement PR in
  *Rollback* carries the same cost.
- **Class 8, prose asserting an edge.** The plan states no deploy order between the exporter
  and `observability`, and the PR 4/PR 5 split holds in either order of `observability` and
  `uptime-kuma`. Mitigation: no prose relies on list position, so no `DOCUMENTED_ORDERINGS`
  row is owed. A future slice that does need `observability` before `uptime-kuma` adds the
  edge and the row itself.
- **A cluster built from nothing fires `BlackboxExporterDown` once.** The appended entry
  sorts last, and no edge holds it before `observability`. After PR 3, a first deploy has
  Prometheus scrape the job before the exporter exists. Until M6's batch A3 the alert fires
  in shadow and pages nobody, and Kuma's tile pages only if the exporter is still missing
  after its 15-minute hold. From A3 on, the alert pages critical after 5 minutes. Either way it
  resolves once the exporter applies. Mitigation: none; this is accepted. The
  edge that would prevent it makes the `observability` Kustomization wait on the exporter's
  under Flux (C3 `dependsOn` with `wait: true`), so a bad exporter bump would hold back every
  pager change (*No deploy-order edge*).
- **A full edge outage pages 26 times.** `group_by: [alertname, service]` gives one group per
  service, as Kuma does. Mitigation: parity. *Open questions* offers grouping by `alertname`
  alone for this family.
- **Two rolls per routed-service change.** Each added hostname rolls the `Recreate` Prometheus
  and the exporter. Mitigation: deploys batch these already, and the exporter is a
  one-replica `RollingUpdate` whose replacement is ready before the old pod stops.
  `file_sd_configs` is the fix for Prometheus if it hurts.

## Tests and docs that change in the same PR

- **PR 1.**
    - The scaffold's tests.
    - The Accept-header pytest in `ansible/tests/services/`.
    - The two census rows, added by hand: `BORN_FENCED_ROLES` in
      `ansible/tests/k8s/test_netpol_baseline_labels.py`, with its comment, and
      `ROLES_WITH_A_DEFAULT_SERVICE` in `ansible/tests/k8s/test_shared_manifest_defaults.py`.
    - The role's `CLAUDE.md`, whose glance block regenerates and shows the denylist reason. Its
      prose says that no edge orders the role against `observability`, and why.
    - The uptime-kuma role's `CLAUDE.md` says that its `hostAliases` come from the shared
      macro.
    - `docs/reference/services.md` regenerates on the next docs-refresh run, so it needs no
      hand edit.
- **PR 2.** The validator's `_is_clean`/`_is_flagged` pairs and the `--prove-red` fixture.
  M1's `alert_rules.py` docstring names the third `service` source and its three conditions.
- **PR 3.**
    - `ingress-probes.test.yaml`, the target pytest, and the four label sets with their
      outcomes in M2's routing pytest.
    - `observability_rule_groups` in the observability role's `defaults/main.yml` gains
      `ingress-probes`.
    - The observability role's `CLAUDE.md` names the `blackbox-ingress` job beside the other
      hand-written jobs.
- **PR 4.**
    - The test changes listed in PR 4, including the target pytest's soak-join check.
    - The uptime-kuma role's `CLAUDE.md` loses the ingress-tile sentence and the clause "or a
      `kuma_ingress_monitors` tile has one".
    - `docs/uptime-kuma-autokuma-record.md` records the move.
- **PR 5.** The promtool cases, and the two shadow label sets in M2's routing pytest, which
  lose `shadow` and change outcome to `muted`.
- **PR 6.** The filter's module docstring.

## Complexity delta

- **Added:** one stateless role (about 120 lines with its config), one shared macro moved from
  uptime-kuma, one scrape job of about 25 lines, four rules with their tests, a target pytest,
  an Accept-header pytest,
  four label sets and a per-set outcome in M2's routing pytest, two census rows, about 50
  validator lines and about 30 `probe.py monitors` lines. M6 Phase B deletes the `monitors`
  addition. No routing walk and no ordering edge are added. The inhibit entry in *Open
  questions* item 8 counts toward M6's batch A3, not this slice.
- **Removed:** 26 Kuma HTTP monitors, which were generated and so cost no template lines beyond
  the loop; six tests that guarded Kuma's rendering of them; one dead arm of
  `kuma_table_loop.py` and its test.
- **Moved:** the ingress set from AutoKuma's SQLite state to Prometheus's targets. Both are
  derived from git, but the new one has no reconciler that can delete history.

## Open questions

1. **Renaming `kuma_ingress_monitors` and the `kuma:` key.** Both outlive Kuma after PR 4, and
   the probe job reads them. Resolution: M6 renames the filter to `ingress_probe_targets` and
   the key to `probe:` in one commit, touching every entry that sets it.
2. **Grouping an edge outage.** Resolution: after the soak, the operator decides whether the
   `ingress-probes` route groups by `alertname` alone, giving one message that lists every
   failed service.
3. **`file_sd_configs` for probe targets.** That would stop an added hostname from rolling
   Prometheus. It is a second exempted ConfigMap, under M1's `DECIDED rules reload in place`
   reasoning. Resolution: adopt it only if Prometheus rolls become a measured cost.
4. **Contract gap, the slice index.** contracts.md says M4 depends on M1 alone. This plan's
   PR 3 also needs M2's PR 3, for two reasons:
    - `IngressProbesMissing` pages from PR 3, and M1's routes carry no mute until M2's PR 3
      adds `weekly-reboot`.
    - PR 3 adds its four label sets to M2's routing pytest, which M2's PR 3 writes. The
      `muted` outcome asserts the interval that M2's PR 3 creates.

   Resolution: the slice index gains an M2 → M4 edge from PR 3 on, or the mute and the walk
   move into M1. PR 1 and PR 2 need only M1.
5. **Contract gap, C7 and M1 convention 3.** Both say every rule carries `service`, and M1
   convention 3 names two sources: a literal, or a join to M2's recording rules. A scrape
   target's label is a third, and PR 2 adds it to `alert_rules.py`. M6 raises the same gap for
   its exporters ([M6](m6-retire-kuma.md), *Open questions* item 2). M1 convention 2 asks each
   firing case to assert `service`. M1's `rule_test_problems` enforces it for SOME firing
   case: an alert passes once one case lists `service` and `severity` in every `exp_alerts`
   entry. It does not require every `exp_alerts` entry of every case to list `service`.
   Resolution: C7 and M1 convention 3 add "a target label that the scrape config sets on
   every target of the job". The open question is whether M1's `alert_rules.py` then
   requires `service` in every `exp_alerts` entry for every alert, the rule PR 2 applies to
   the third source only.
6. **M1 convention 5 and the single cutover commit.** The convention says the cutover drops
   `shadow` and deletes the Kuma producer in the same commit. One landing cannot order the two
   deploys (Baseline), so this plan cuts over in two PRs. Resolution: M1 amends convention 5 to
   allow a producer-first PR followed by a shadow-drop PR, gated on the Kuma side's live state.
7. **M2's routing pytest asserts one outcome for every set.** M2 writes "every route reached
   carries `weekly-reboot`", which this slice's two shadow sets fail by design from PR 3 to
   PR 5. Resolution: M2 writes the walk with a per-set expected outcome, `muted` or
   `blackhole`, from its PR 3. If it ships the single assertion instead, this slice's PR 3
   converts it, and M2's own sets keep `muted`.
8. **M6's batch A3 inherits `BlackboxExporterDown`.** This slice ships the alert with
   `shadow: "true"`, because Kuma's Cluster Scrape Targets tile pages the same failure until A3
   (*The rules*). Resolution: this plan requests that the commit in M6's batch A3 that cuts
   `cluster_targets` over make three changes:
    - It drops `shadow: "true"` from `BlackboxExporterDown`.
    - It changes the set's outcome in M2's routing pytest from `blackhole` to `muted`.
    - It adds one entry to `inhibit_rules` in M1's `alertmanager-secret.yaml.j2`:
        - `source_matchers`: `alertname="BlackboxExporterDown"` and `shadow!="true"`;
        - `target_matchers`: A3's scrape-target alertname and `job="blackbox-ingress"`;
        - no `equal`. The source fires once for the whole job, so no label has to match,
          and an aggregation without `by (job)` keeps no `job` label to compare. Upstream
          `inhibit/cache.go` at v0.34.1, M1's version floor, keys sources
          by `cache.fingerprintEquals`. With no `equal` label every alert gets the same key,
          so any firing source matches every target.

   The three changes land in one commit. Alertmanager's inhibitor caches every alert that
   matches the source matchers, whatever its route: `Inhibitor.run` subscribes to all
   alerts, and `Inhibitor.processAlert` caches each match (upstream `inhibit/inhibit.go` at
   v0.34.1). Without `shadow!="true"`, an inhibit that landed before the shadow drop would
   silence A3's alert while its source went to the blackhole. A shadow drop that landed
   without the inhibit would page one outage from both rules, against M1 convention 5.

   The inhibit holds A3's rule to two constraints:
    - Its alert keeps the `job` label of its `up` series, or the target matcher selects
      nothing.
    - Its `for` stays longer than `BlackboxExporterDown`'s 5m. The source must fire first,
      or A3's first notification goes out before the inhibit applies. M6's table gives
      `for: 10m`.

   M6 owns the proof, a pytest on the rendered `alertmanager.yml` that finds the entry with
   those matchers. It has two red halves:
    - A3's alert for another job, such as `alertmanager`, which the target matchers must not
      select.
    - A `BlackboxExporterDown` label set carrying `shadow="true"`, which the source matchers
      must not select. Without it, a later edit could drop `shadow!="true"` unnoticed.

   A3's alert for one of this job's targets carries the
   probed service's `service` label, because every target sets it (*Where `service` comes
   from*). That alert therefore reaches the probed service's health gate, and M6 decides
   whether that attribution is intended.

   Excluding `job="blackbox-ingress"` from A3's rule was rejected. A single target's
   `up == 0` would then be watched by nothing, because `BlackboxExporterDown` needs every
   target at 0.

## Review log

### Round 1 (adversarial review, 2026-10-11)

Findings raised: 15 (5 high, 10 medium). Fixed: 15. Refuted: none.

The findings cite the round-1 PR numbers. In this revision, PR 2 is the validator and the
routing walk, the old PR 2 is PR 3, the old PR 3 split into PR 4 and PR 5, and PR 6 carries
the filter's docstring.

- **M4-R1-01 (high).** The weekly reboot probably cannot fire the probes. **Fixed.** Re-ran
  the reviewer's queries. The Prometheus gap of 07:34:57 to 07:50:55 on 2026-10-04 reproduced.
  No `homelab` Deployment read 0 available after Prometheus returned, while 55 Deployment
  series existed in the window. The Baseline records both. Criterion 4 is now the routing
  pytest with a `Watchdog` red half. The red proofs no longer claim that the reboot fires the
  alerts, and *Verification* says a quiet Sunday proves nothing. A cross-slice note tells M2.
- **M4-R1-02 (high).** No toposort edge runs from observability to blackbox-exporter.
  **Fixed.** `build_k8s_dep_map` reads only `depends_on`, `use_authelia` and the routed test.
  PR 1 adds `depends_on: [blackbox-exporter]` to the observability entry and a
  `DOCUMENTED_ORDERINGS` row.
- **M4-R1-03 (high).** `land.sh` deploys PR 3 in the reverse of its stated order. **Fixed.**
  `run_deploy` builds one argv, and `land.sh --help` refuses `--tags` under
  `LAND_HANDOFF_UNIT`. The cutover is split into PR 4 (Kuma side) and PR 5 (drop `shadow`),
  gated on the live `monitor_status` count. The rollback runs PR 5 first. Cross-slice notes go
  to M1 (convention 5) and M2 (its PR 4).
- **M4-R1-04 (high).** M1's validator rejects the `IngressProbeFailed` rules. **Fixed.** PR 2
  extends `alert_rules.py` with a clean/flagged pair, and *Open questions* item 5 cites M6's
  open question 2.
- **M4-R1-05 (high).** Kuma pins `.local.` names through `hostAliases`. **Fixed.** The Baseline
  is corrected. Both Deployments call a shared `ingress_host_aliases()` macro, and the target
  pytest asserts that every probe host is aliased.
- **M4-R1-06 (medium).** `IngressProbesMissing` keeps `shadow` and never pages. **Fixed.** It
  ships without `shadow` from PR 3, and its promtool case asserts that label set.
- **M4-R1-07 (medium).** The soak defines no false-positive query. **Fixed.** Query 2 was
  added, and a non-empty result blocks PR 4.
- **M4-R1-08 (medium).** Kuma follows redirects on the three tiles without SSO. **Fixed.**
  `http_2xx` sets `follow_redirects: true`, and the target pytest asserts that the module
  follows `behind_sso`. The three tiles are `auth-k8s`, `littlelink-k8s` and
  `home-assistant-k8s`, from running the filter.
- **M4-R1-09 (medium).** The Sunday queries are not anchored in time. **Fixed.** Both use `@`
  at the Sunday's 08:15 UTC. The rule says any Discord or email rise in the window is a
  failure.
- **M4-R1-10 (medium).** `BlackboxExporterDown` pages un-muted from PR 2. **Fixed.** The rules
  PR (PR 3 after renumbering) depends on M2's PR 3. The Depends-on line and *Open questions*
  item 4 say so.
- **M4-R1-11 (medium).** The live pint pass cannot pass before the job exists. **Fixed.** It
  runs after PR 3 deploys, before the soak's first query, and a finding blocks PR 4.
- **M4-R1-12 (medium).** PR 3 missed consumers of the filter. **Fixed.** PR 4 lists each one,
  plus the status-page census, whose `grafana-k8s` member and `>= 90` floor would go red. It
  keeps the filter's own two tests.
- **M4-R1-13 (medium).** The diagnostic readers go blind between the cutover and M6 Phase B.
  **Fixed.** PR 4 makes `probe.py monitors` list firing ingress alerts. Risks names the three
  human-facing readers that wait for M6 Phase B. Sequencing the cutover after M6's change was
  rejected, because M6 depends on M4.
- **M4-R1-14 (medium).** Auto-deploy eligibility departs from the precedent. **Fixed.** The
  role is denylisted. The scaffold command carries the reason, including the class 2 limit of
  `/-/healthy`.
- **M4-R1-15 (medium).** The C4 trigger names D2. **Fixed.** PR 1 keys on whether the entry
  the scaffolder wrote carries `owner: flux`, which it does from D5's first Wave A PR on. At
  the baseline `scripts/dev/new_k8s_service.py:entry_lines` writes no `owner` key.

### Round c1 (per-slice review, 2026-10-11)

Findings raised: 5 (1 high, 4 medium). Fixed: 5. Refuted: none.

- **M4-C1-01 (high).** PR 1's `depends_on` edge moves `observability` to the end of the
  deploy order. **Fixed by dropping the edge.** The simulation reproduced: with the edge,
  `observability` goes from 19 to 56, after `uptime-kuma` (41) and `monitor-bridge` (47).
  With the appended entry and no edge, the three keep 19, 42 and 48. The Baseline records
  all three runs. Moving the entry above `observability` was rejected, because the edge
  itself makes the `observability` Kustomization wait on the exporter's under Flux. The
  edge's only benefit was avoiding one `BlackboxExporterDown` page on a cluster built from
  nothing, which *Risks* now accepts. The PR 4/PR 5 split no longer cites deploy order. It
  holds in either order, because AutoKuma deletes the tiles asynchronously. M4's and M2's
  statements of the present order stay true, so M2 needs no change for ordering.
  This supersedes the round-1 fix for M4-R1-02. That finding's complaint was prose asserting
  an order with no edge behind it. This revision asserts no order between the exporter and
  `observability`, so no `DOCUMENTED_ORDERINGS` row is owed.
- **M4-C1-02 (medium).** M4 PR 2 and M2 PR 3 both claim the routing walk. **Fixed.** M4
  writes no walk. PR 2 is now the validator alone, and PR 3 adds the four label sets to M2's
  routing pytest. The finding's single predicate, "blackhole receiver or `weekly-reboot`",
  was not adopted, because it would pass a meta alert routed to the blackhole. Each set
  instead carries one expected outcome, `muted` or `blackhole`, and `blackhole` gets its own
  red half. `grep -n blackhole` on M1 finds the route but no test that `shadow="true"`
  reaches it. *Open questions* item 7 and a cross-slice note ask M2 to write the walk that
  way.
- **M4-C1-03 (medium).** The target pytest compares with a URL that PR 4 deletes.
  **Fixed.** `ansible/filter_plugins/kuma_monitors.py:kuma_ingress_monitors` returns only
  `id`, `name`, `hostname`, `behind_sso` and `entry`. PR 3's pytest builds
  `https://<tile.hostname>.local.<domain>` from the filter, and that assertion outlives PR 4. The comparison with Kuma's rendered `url` is
  the soak-join check, which PR 4 retires. Exit criterion 1 and the class 4 risk say so.
- **M4-C1-04 (medium).** The scaffolder does not add the census rows. **Fixed.**
  `scripts/dev/new_k8s_service.py:main` only prints them. PR 1 adds both by hand. The
  `BORN_FENCED_ROLES` comment names Prometheus from `observability` as the only caller and
  says why the set's "dials nothing" preamble does not apply: the baseline's `policyTypes`
  is `Ingress` alone. *The tree* and *Tests and docs* list both test files, and PR 3 adds
  `ingress-probes` to `observability_rule_groups`.
- **M4-C1-05 (medium).** The red proof covers one condition of the third `service` source.
  **Fixed.** The rule has three conditions, each with flagged cases: a regex `job` matcher,
  no `job` matcher, two jobs, a target without `service`, and an aggregation. The aggregation
  case rests on condition 3: the firing case must list `service` in `exp_labels`. Upstream
  `cmd/promtool/unittest.go` compares whole label sets under `labels.Equal`, so promtool
  fails an expression that drops `service`. A second rule test for `--prove-red` proves
  that. M1's `--prove-red` runs one fixture per tool, so PR 2 extends it. No M1
  check enforces convention 2's `service` assertion, so PR 2 enforces it for this source,
  and *Open questions* item 5 asks M1 to enforce it for every alert.
  *Corrected 2026-10-13 (#4799):* that claim was stale. M1's `rule_test_problems` already
  requires some firing case to assert `service`. PR 2 adds the stronger rule, `service` in
  every `exp_alerts` entry, for this source only. Item 5 now asks whether M1 should require
  every entry for every alert.

### Round c2 (per-slice review, 2026-10-11)

Findings raised: 2 (1 high, 1 medium). Fixed: 2. Refuted: none.

- **M4-C2-01 (high).** From PR 3, `BlackboxExporterDown` pages while Kuma's Cluster Scrape
  Targets tile already pages for the same failure, which breaks M1 convention 5. **Fixed.**
  The premise "Kuma never watched the job or the exporter" was wrong.
  `ansible/roles/k8s/monitor-bridge/files/checks/cluster.py:check_cluster_targets` reads
  `up{origin!="daniel-server"}`, which matches every job without an `origin` label. The
  `cluster_targets` row of `ansible/roles/k8s/monitor-bridge/files/check_table.py:CHECKS`
  feeds the "Cluster Scrape Targets" tile with a 15-minute hold and no `critical=True`. The
  comment above the `coredns-host` job in `prometheus.yaml.j2` relies on that tile in the
  same way. M1 convention 5 forbids an unshadowed `up == 0` rule for such a job until M6's
  batch A3. `BlackboxExporterDown` now ships with `shadow: "true"`, and the tile is the
  exporter-down pager until A3. The routing pytest expects `blackhole` for that set. The
  `blackhole` red half is now the `IngressProbesMissing` set expected as `blackhole`. The
  Baseline, exit criterion 4, *The rules*, the routing-proof section, PR 1, PR 3 and PR 5,
  Verification, Rollback and Risks follow. *Open questions* item 4 now rests the M2 PR 3
  dependency on `IngressProbesMissing` and on M2's routing pytest. Item 8 and a cross-slice
  note hand the `shadow` drop and the duplicate-page choice to M6's batch A3. Keeping the
  rule unshadowed and asking M1 to amend convention 5 was rejected, because the tile already
  pages, and the cost of following the convention is latency, which *Risks* states.
- **M4-C2-02 (medium).** PR 1's two-step rollback leaves the default Service live, queues a
  wait on a pruned Deployment, and never says PR 3 must be reverted first. **Fixed, by a
  different route from the one the finding proposed.** Both defects reproduced:
  `ansible/roles/k8s/manifests/CLAUDE.md` derives the shared default Service from the entry's
  `port`, and its caller contract defaults `manifests_rollout` to `manifests_service`. The
  proposed step 1 (drop `port`, `manifests_rollout: ''`, keep the role) fails on its own
  terms. The role would then render no file, and the manifests task "Check that a role arming
  the prune renders something to keep" asserts `manifests_files_resolved | length > 0`
  before any prune runs. `BORN_FENCED_ROLES` also could not wait for step 2, because
  `ansible/tests/k8s/test_netpol_baseline_labels.py::test_exactly_the_fenced_roles_carry_the_baseline_label`
  compares it with the rendered pod labels in both directions. The rollback now uses the
  repo's own retirement path, `docs/deploying.md` *Retiring a k8s service*. One PR deletes the
  role and the entry, adds `blackbox-exporter` to `k8s_retired_services` in
  `ansible/inventory/group_vars/all.yml`, and drops both census rows. The next k8s deploy
  runs `ansible/tasks/k8s_retire.yml`, which deletes every staged object, the Service
  included, and queues no rollout wait. *Rollback* now opens with the reverse landing order,
  and PR 1's entry says it runs only after PR 3's rollback has deployed `observability`.

### Round c3 (per-slice review, 2026-10-11)

Findings raised: 3 (1 high, 2 medium). Fixed: 3. Refuted: none.

- **M4-C3-01 (high).** `http_sso_302` receives 401, not 302, because blackbox_exporter sends
  no Accept header. **Fixed.** The split reproduced. Read-only GETs to
  `https://homepage.local.<domain>/` returned 302 with `Accept: */*` and `Accept: text/html`,
  and 401 with no Accept and with `Accept: application/json`. Upstream `prober/http.go` at
  v0.29.0 sets only `User-Agent` (`userAgentDefaultHeader`) and the module's `headers`. Live,
  `count(monitor_status{monitor_type="http",monitor_url=~"https://[^/]+"} == 1)` read 26, so
  the pinned Kuma draws 302. The finding's evidence cites Kuma 2.0.0, while the tree pins
  2.5.5, whose `server/model/monitor.js` has no Accept literal. The plan therefore rests Kuma's
  side on the live read rather than on that source. Both modules now carry
  `headers: {Accept: "*/*"}`, and *The exporter* states why. An Accept-header pytest with a
  `_is_flagged` half joins PR 1, *The tree*, *Tests and docs*, the red proofs and the
  complexity delta. The finding's soak gate, `count(probe_success{...} == 0)` reading 0, was
  not adopted: that query returns no series both when nothing fails and when the job is
  absent, which is class 3. The clock now starts when `count(probe_success{...} == 1)` equals
  the tile count. The likely-episodes list and the class 7 risk name the header.
- **M4-C3-02 (medium).** PR 3 and PR 5 never verify that Prometheus loaded the changed config
  and rules. **Fixed.** M1 says the `prometheus-rules` ConfigMap carries no checksum
  annotation and reloads through `--config.auto-reload`, so PR 5 rolls no pod. PR 3 and PR 5
  now read `prometheus_config_last_reload_successful` and its success timestamp after the
  deploy, which read 1 live at the baseline. PR 5's no-pager window closes only on both reads.
  "Within 30s" is replaced by the kubelet's propagation plus the next auto-reload pass, with
  no unmeasured figure. Rollback confirms the reload before reverting PR 4, and *Risks* gains
  the class 3 entry.
- **M4-C3-03 (medium).** PR 1's landing deploys about 23 tags beyond the two it named.
  **Fixed, with the failure path corrected.** `changed_fields` returns None for an added
  entry. `broad._list_reader_tags` with `changed=None` at `70591c307` named 23 reader tags of
  59 declared, crowdsec, traefik, pihole and authelia among them. With the entry's own tag that
  is 24 of 60, under the half-the-fleet refusal. PR 1 edits `ansible/inventory/` and
  `ansible/templates/`, both in `_BROAD_DEPLOY_PREFIXES`, so the GitOps tick applies the
  narrowed list, and `docs/gitops-pipeline.md` says that list is not filtered through the
  denylist. The finding's "`deploy-failed` with nothing rolled" understated the result. A
  broad arm is forward-only and writes `hold_sha`, which holds every later landing. `land.sh`
  reports the non-resume `deploy-failed` variant, and the exporter, sorted last, may not have
  applied. PR 1 now records the derived list, names the verify gates that run, lands in a
  quiet window, and states the hold and the recovery. The retirement PR in *Rollback* fans
  out the same way and says so.

### Round integration-2 (integration review, 2026-10-11)

Findings raised: 1 (1 medium). Fixed: 1, for M4's part. Refuted: none.

- **X2-10 (medium).** M6 batch A3 owns none of the shadow drops and routing changes that M4,
  M5 and M1 hand to it. **Fixed for M4's part; M6's part is requested in a cross-slice
  note.** The finding holds. M6's A3 row lists only `targets`, `cluster_targets`,
  `shipper_dropped` and `CLUSTER_TARGETS_CONSECUTIVE` becoming `for: 10m`. The finding's
  grep count of 0 is off by two: M6 names `BlackboxExporterDown` once in its class 3
  mitigations and `LokiHomelabDown` once in B8. Neither assigns an A3 task. M4's Open
  question 8 left the choice between excluding the job and an inhibit to M6. It now
  specifies the three changes the A3 cutover commit makes for `BlackboxExporterDown`: the
  shadow drop, the routing outcome moving from `blackhole` to `muted`, and an
  `inhibit_rules` entry with stated source and target matchers and no `equal`. Two upstream
  reads at Alertmanager v0.34.1 back that entry:
    - `inhibit/inhibit.go`: `Inhibitor.run` subscribes to every alert, and
      `Inhibitor.processAlert` caches each source match whatever its route. A shadowed source
      would therefore still inhibit. The source matcher gains `shadow!="true"`, and the
      changes land in one commit.
    - `inhibit/cache.go`: `cache.fingerprintEquals` gives every alert the same key when
      `equal` is empty, so an empty `equal` matches every target.

  Item 8 also states the two constraints the inhibit puts on A3's rule: it keeps `job`, and
  its `for` stays above 5m. It names M6's proof and its red half, and records the job
  exclusion as rejected. *The rules*, *Verification* and *Complexity delta* point at item 8.
  *Risks* extends the dead-exporter entry and gains a class 5 entry for the ordering. The
  `LokiHomelabDown` and `alertmanager` handoffs belong to M5 and M1, and this plan does not
  restate them.
- **Not raised by the review: a red census.** `uv run pytest ansible/tests/repo` failed
  `test_census_rows_text.py` on the row `documented-macros-exist`. That row reads every
  `<name>.yml.j2` a doc names as a macro that must exist. This plan gave the future
  `ingress-host-aliases` macro file the `.yml.j2` suffix. **Fixed.** The plan now names it
  `ingress-host-aliases.yaml.j2`, the suffix that `service-default.yaml.j2` and three other
  macros in `ansible/templates/` already carry. The same run then passed 869 tests.
