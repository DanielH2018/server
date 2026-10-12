# M1 — Alertmanager and its watcher

Status: IN PROGRESS

PR 1, the promtool and amtool provisioning and its red proof: [#4703](https://github.com/DanielH2018/server/pull/4703).
PR 2, Alertmanager with its receivers: [#4728](https://github.com/DanielH2018/server/pull/4728).

Depends on: —

Unblocks: M2, M3, M4, M5, and D5 (through M2)

This slice adds Alertmanager to the `observability` role, loads Prometheus rules from git, and
sends an always-firing `Watchdog` alert to Healthchecks.io, so an Alertmanager outage pages
off-site. Its Design section also holds the conventions every other monitoring slice follows
(M2 to M6 link here rather than restate them). [contracts.md](../contracts.md) C7 holds the
labels and the health gate. [design.md](../design.md) explains why the alert plane replaces
Uptime Kuma.

## Goal and exit criterion

**Goal.** Alertmanager runs in the `observability` namespace with a Discord receiver, an email
receiver for critical alerts, a blackhole receiver for shadow alerts, and a Healthchecks.io
receiver for the Watchdog. Prometheus evaluates rule files from git and reloads them without a
restart. CI rejects a broken rule, a broken rule test, a broken Alertmanager config or a
broken `prometheus.yml`. An edit to the Alertmanager config restarts Alertmanager and nothing
else.

**Exit criterion.** All four hold:

1. The Watchdog reaches Healthchecks.io. `probe.py metric 'ALERTS{alertname="Watchdog"}'`
   returns one firing series, and `alertmanager_notifications_total{integration="webhook"}`
   rises between two reads taken two minutes apart. The `alertmanager-watchdog` check in the
   Healthchecks.io console reads up (**operator-only**: the console needs the operator's login).
2. Alertmanager scaled to 0 pages through Healthchecks.io within the check's period plus grace
   (30 minutes), and scaling it back to 1 clears the page. The drill is in *Verification*.
3. The CI hooks job's `alert_rules.py --prove-red` step passes. Each tool rejects its committed
   bad fixture, so the gate is shown able to fail.
4. PR 5's deploy moves the staged bytes of `alertmanager-secret.yaml` and of no other Secret
   file. It restarts Alertmanager and neither Prometheus nor Grafana. `kube_pod_start_time`
   moves for the Alertmanager pod alone, and `probe.py releases` shows `restart: true` for
   Alertmanager alone in the `observability` record.

## Baseline at `70591c307`

- **Prometheus has no rules and no Alertmanager.** `prometheus.yml` in
  `ansible/roles/k8s/observability/templates/prometheus.yaml.j2` carries no `rule_files:` and no
  `alerting:` block.
- **Three pieces of prose state that no Alertmanager or rule exists, and M1 makes each false.**
  `git grep -il alertmanager 70591c307` returns four files. Two of them hold such prose:
    - `docs/healthchecks-io-deadman.md`, *Watching the spine itself*: "there is no
      Alertmanager, so nothing else pages", followed by "all six checks above". Its *What is
      wired* table already has nine rows, so the count is stale too.
    - `ansible/roles/setup/k3s/templates/longhorn-backup-health.sh.j2`, the comment above the
      `uptime-kuma-alive` ping: "Every alert in this homelab terminates in Kuma — there is no
      Alertmanager".

  The other two are `docs/archive/k3s-migration/slice-3-monitoring-plane.md`, a historical
  record, and `styles/config/vocabularies/Homelab/accept.txt`, a Vale word list. The grep
  misses a third piece of prose, because it says "alerting rules". The comment above the
  `kube-state-metrics` job in `prometheus.yaml.j2` justifies that job's 1m scrape with "no
  recording or alerting rules exist (no `rule_files` in this role ...)".
- **A seventh workload makes a second group of passages false: the ones that count.** They
  count the role's workloads, its manifests, its Secret readers or its tier-1 templates.
  `git grep -n -w -E 'six|seven|eight|two workloads'` over the observability role, its three
  tests and the netpol-baseline role lists them:
    - `ansible/roles/k8s/observability/CLAUDE.md`, *One config change, one restart*: "The
      snapshot handing these six to the play gate". PR 2 runs `fact_status.py verify` on that
      section.
    - `ansible/roles/k8s/observability/tasks/main.yml`: the restart task's comment ("would
      restart all six", "this task runs for exactly the two workloads that read one"), the
      comment at the `manifests_self_rollouts` include ("these six live in"), and the
      comments around the snapshot task and the stabilisation hand-off ("these six", "All six
      carry", "these six are the only workloads").
    - `ansible/roles/k8s/observability/defaults/main.yml`: the comment above
      `observability_stabilise_workloads` ("The six telemetry workloads", "would restart all
      six", "ANY of this role's eight manifests"), and `k8s_autodeploy_reason` ("six
      sub-images"), which feeds the generated *At a glance* block.
    - The comment "Tier 4, with prometheus.yaml.j2 the one exception — see `DECIDED
      observability tiers`" in five templates: `collector`, `grafana`, `kube-state-metrics`,
      `loki` and `tempo` `.yaml.j2`. `prometheus.yaml.j2`'s own tier-1 comment stays true.
    - `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-observability.yaml.j2`: "all
      seven pods carry the opt-in label already".
    - Test docstrings and failure messages: "six workloads" and "these six" in
      `ansible/tests/services/test_observability_stabilise_coverage.py`; "its six workloads",
      "the two workloads that read one", "names the six", "restarts all six" and "Five of the
      six" in `ansible/tests/k8s/test_self_rollouts_follow_the_apply.py`; "eight manifests"
      in `ansible/tests/services/test_observability_config_rolls_one_workload.py`.

  A past-tense account of an incident, such as "snapshotted all six workloads" (#3133), stays
  as written. "The seven routeless workloads" in `defaults/main.yml` and "the seven
  previously-unmonitorable workloads" in `prometheus.yaml.j2` name the cluster-wide set from
  the migration's slice 3, not this role's workloads, so M1 leaves them.
- **Prometheus is v3.15.0** (`observability_k8s_prometheus_image` in
  `ansible/roles/k8s/observability/defaults/main.yml`). It runs as a `Recreate` Deployment on
  the `prometheus-data` claim. Its pod template carries `checksum/config`, a hash of the one
  `prometheus.yml` key, so a config edit rolls the pod. Its pod shell is
  `pod_shell('homelab-critical', service_account='prometheus', run_as_user=65534,
  run_as_group=65534, fs_group=65534)` with `hardened_security_context(non_root=true)`, and it
  mounts the `prometheus-scrape-credentials` Secret with `defaultMode: 0o444`.
- **The rendered `prometheus.yml` holds no stubbed value.** Rendered through
  `ansible/tests/_k8s_render.py:rendered_docs` at `70591c307`, the `prometheus-config`
  ConfigMap's `prometheus.yml` is 658 lines with no `STUB` in it. Its loops read
  `containers_list` through `ansible/filter_plugins/scrape_jobs.py`, plus
  `crowdsec_k8s_sidecar_agents` and `k3s_etcd_expose_metrics` from
  `ansible/inventory/group_vars/all.yml`.
- **Upstream Prometheus v3.15.0 can reload its own rules.** `cmd/prometheus/main.go` defines
  `--config.auto-reload` (interval flag default 30s). `config/reload.go`, function
  `GenerateChecksum`, hashes the config file plus every file each `rule_files` pattern
  matches through `filepath.Glob`, so a new or edited rule file triggers a reload. Read
  through `gh api` at tag `v3.15.0`.
- **Upstream Alertmanager is v0.34.1** (released 2026-09-17, `gh release list`). Read through
  `gh api` at that tag:
    - `notify/discord/config.go` has `webhook_url_file`;
    - `notify/webhook/config.go` has `url_file` and a templated `payload`;
    - `config/notifiers.go` has `auth_password_file` for email;
    - `config/config.go` has `time_intervals` and `mute_time_intervals`;
    - `notify/metrics.go`, function `InitializeFor`, creates
      `alertmanager_notifications_failed_total` at 0 for every configured integration and
      failure reason, so the series exists before any send fails;
    - the `Dockerfile` sets `USER nobody`, `VOLUME [ "/alertmanager" ]` and a default
      `--config.file=/etc/alertmanager/alertmanager.yml`.
- **The `observability` namespace has one tenant, and its baseline depends on that.**
  `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-observability.yaml.j2` admits
  `podSelector: {}` inside the namespace because only the `observability` role renders into
  it. `ansible/tests/k8s/test_rendered_properties.py` guards that invariant. A new role in
  this namespace would break it. Alertmanager therefore joins the `observability` role.
- **Observability's tests pin its workload set.**
  `ansible/tests/services/test_observability_config_rolls_one_workload.py` pins
  `_EXPECTED_WORKLOADS` (six names), `_EXPECTED_SECRET_READERS` (`prometheus`, `grafana`) and
  `_CONFIGMAP_DATA_KEYS`. Its
  `test_a_workload_mounting_a_role_configmap_carries_the_checksum_annotation` asserts that the
  workloads it checked are a superset of `_EXPECTED_WORKLOADS` minus `kube-state-metrics`, the
  one workload that mounts no role ConfigMap. `observability_stabilise_workloads` in the role
  defaults lists the same six. `ansible/tests/services/test_observability_stabilise_coverage.py:test_the_role_lists_its_workloads_the_same_way_everywhere`
  requires every literal `{kind, name}` loop in the role's `tasks/main.yml` to equal that
  list as a set. The task "Wait for the telemetry workloads to roll out" is such a literal
  six-item loop. The restart-count snapshot after it must run after that wait, or it reads
  the outgoing pod.
- **One Secret change restarts every Secret reader in the role.** In
  `ansible/roles/k8s/observability/tasks/main.yml`, the task "Restart the telemetry workloads
  whose secret changed" restarts each `observability_stabilise_workloads` entry whose
  `restart_on` holds `secret` when `manifests_secret_render is changed`. That register comes
  from one looped `no_log` task in `ansible/roles/k8s/manifests/tasks/main.yml`, "Render secret
  manifests", over the `manifests_secret_files_resolved` fact. The role renders three Secret
  files: `grafana-secret.yaml`, `scrape-credentials-secret.yaml` and `origin-pull-secret.yaml`.
  A Traefik middleware reads the third, and no pod does. The release record decides each
  rollout's `restart` from the same register, in
  `ansible/roles/k8s/manifests/tasks/release_stamp.yml`.
  `ansible/tests/k8s/test_self_rollouts_follow_the_apply.py` pins that behaviour in
  `test_observability_a_changed_secret_expects_only_its_two_readers_to_roll` and
  `test_observability_private_restart_reads_the_same_facts_as_the_record`. The first one
  asserts that the rollouts marked `restart: true` are exactly `{"prometheus", "grafana"}`. It
  reads the role's real defaults through
  `ansible/tests/_release_expectation.py:observability_self_rollouts`, so the defaults entry
  PR 2 adds turns it red unless PR 2 edits it too.
- **An armed role's manifests are re-serialised before they are staged.** `manifests_prune`
  is `true` in `ansible/roles/k8s/manifests/defaults/main.yml`. The observability include
  does not override it, and `ansible/tests/deploy/test_manifests_prune.py:_UNARMED_ROLES` is
  `{authelia, nut}`. For an armed role, "Render secret manifests" takes
  `ansible/templates/role-labelled.yaml.j2` as its `src`, as the ordinary render does. That
  wrapper pipes the render through `ansible/filter_plugins/role_label.py:homelab_role_label`,
  which runs `yaml.load_all` and then `yaml.dump_all`. Only what survives that round trip
  reaches the staged file. A run of the function on a Secret dropped a top-level `#` line. It
  kept the same line inside a `stringData` block-scalar value, and it kept a metadata
  annotation. A top-level YAML comment in an observability template therefore moves no staged
  bytes and restarts nothing. A comment inside a ConfigMap or Secret value does move them. A
  deferred render, such as netpol-baseline's observability policies, skips the wrapper and
  keeps its comments.
- **A role can decide a rollout's `restart` itself.** A `manifests_self_rollouts` entry with
  `rolled_by_role: true` is stamped `restart: false`. The owning role raises it afterwards
  through `ansible/roles/k8s/manifests/tasks/rollout_amend.yml`, only where it issued a
  `rollout restart`. `ansible/roles/k8s/pihole/tasks/roll_one.yml` is the one caller.
- **The notifier credentials are SOPS keys.** `grep -c '^<name>:' ansible/vars/secrets.yml`
  returns 1 for `email`, `monitor_discord_webhook_url`, `smtp_notify_app_password` and
  `healthchecks_ping_key`. Kuma's `discord.json` and `email.json` declarations in
  `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2` read the first three.
  Because `email` is a secret, the Alertmanager config file must be a Secret, not a ConfigMap.
- **Every k8s Secret template writes `stringData`, never `data`.** Over the pathspec
  `ansible/roles/k8s/*secret*.j2`, `git grep -l -E '^stringData:'` lists 38 files, and the
  same grep for `^data:` lists none. R2's placeholders therefore stay readable to Flux's
  substitution.
- **The manifest validator stubs secrets as `STUB`.** `scripts/lib/render_guard.py:StubUndefined`
  backs `scripts/validate/k8s_manifests.py`. A webhook URL rendered as `STUB` fails
  Alertmanager's URL parsing, so the config reads every secret through a `*_file` field.
  The same undefined class turns a missing dict key into `STUB` instead of an error, so an
  offline render cannot catch a key the real Ansible render would refuse.
- **The offline renderer supports four lookups.** `scripts/lib/k8s_yaml.py:make_lookup`
  resolves `file`, `pipe` (base64 only), `template` and `vars`, and raises `ValueError` for any
  other kind, `fileglob` included. C1 has the renderer reuse the same Jinja environment. A
  template that lists files therefore needs the list as data. The precedent is home-assistant:
  "A NEW file ships nothing until its list in `defaults/main.yml` names it", and its validator
  checks that the directory and the list agree.
- **Every refused netpol probe row names its policy.** In
  `ansible/roles/k8s/netpol-baseline/defaults/main.yml`, every `expect: refused` row of
  `netpol_baseline_probe_targets` carries `policy:`. `templates/netpol-probe-job.yaml.j2` in
  that role prints `{{ t.policy }}` in the refused leg's failure message.
  `ansible/tests/k8s/test_netpol_probe_targets.py` has no test that a row carries the key. No
  refused row exercises `networkpolicy-observability.yaml.j2` itself: its Prometheus and Loki
  rows test their own bespoke policies, and the Grafana and Tempo rows carry no `expect`. That
  baseline admits Traefik from the `homelab` namespace, any pod in its own namespace and the
  node CIDRs, so the probe pod (`app: netpol-baseline-probe`) is refused.
- **A probe target with no ready endpoint fails every netpol-baseline deploy.** The task "Fail
  if any probe target has no ready endpoint" in
  `ansible/roles/k8s/netpol-baseline/tasks/main.yml` asserts a ready address for each row.
  `ansible/tests/k8s/test_netpol_probe_targets.py:test_terraria_is_absent_while_it_is_scaled_to_zero`
  states the consequence for a row whose workload sits at 0 replicas: "every full deploy.yml
  fails its readiness gate". netpol-baseline is auto-deploy-eligible, and the GitOps deployer
  applies a deploy-plane change as a full `deploy.yml`. A failed apply writes `hold_sha`, and
  `scripts/deploy_tools/gitops_state.py` offers only `clear-*` subcommands, so no tool sets a
  hold ahead of time. The deployer's tick is `gitops-deploy.timer`. Its liveness alert,
  GitOps-Alive, fires once `last_run` is older than `GITOPS_MAX_AGE_MIN`, 90 in
  `ansible/roles/k8s/monitor-bridge/templates/env-secret.yaml.j2`.
- **Two readers of `ALERTS` are planned, and they filter differently.** M6 Phase B gives
  `probe.py health` the C7 gate query `ALERTS{alertstate="firing",service="<tag>",shadow!="true"}`.
  D5's soak Job counts only critical alerts on a flux-owned service that became active after
  the Job started ([D5](../deploy/d5-bulk-move.md), *The Job*).
- **Kuma's watcher sets the precedent for the Watchdog's sizing.** `uptime-kuma-alive` in
  `monitor_bridge_healthchecks_expected` (`ansible/roles/k8s/monitor-bridge/defaults/main.yml`)
  is a Simple check with `timeout: 600` and `grace: 1200`.
  `ansible/tests/services/test_monitor_bridge_healthchecks_expected.py` derives every slug from
  `scripts/docs/fragment_renderers.py:deadman_crons`, which reads cron variables only and
  returns `(slug, cron, source)` triples from six positional arguments. The test's
  `cron_mismatches` holds a `kind: simple` slug's cron to `_EVERY_N_MINUTES`
  (`^\*/(\d+) \* \* \* \*$`) and requires `N*60 == timeout`. Anything else is flagged.
  `scripts/docs/gen_doc_fragments.py:_deadman` returns the fragment with its own literal list
  of sources, which becomes the fragment's `generated_from` header.
- **The weekly reboot outage is about 10 minutes.** The reboot cron fires at 07:30 UTC on
  Sunday with a 5-minute shutdown delay (`weekly_reboot_*` in
  `ansible/inventory/group_vars/all.yml`). `docs/healthchecks-io-deadman.md` records daniel-box
  back up at 07:39:48 and the last pod Ready at 07:45:06 on 2026-08-30.
- **CI installs no Prometheus tooling.** The `hooks` job in `.github/workflows/ci.yml` installs
  Vale from a pinned tarball and nothing else of this kind.
- **Two readers see an untracked path at the repo root.** `.gitignore` opens with `/*`, and
  `git check-ignore -v --no-index -- .cache` prints `.gitignore:2:/*`. The porcelain status
  that the GitOps deployer reads omits an ignored path. The `validate-root-ignored-files` prek
  hook does not: `scripts/validate/root_ignored_files.py:problems` flags every untracked root
  entry whose deciding rule is `/*` and whose name is not in `LOCAL_ONLY`. `LOCAL_ONLY` holds
  `.venv`, `.ruff_cache`, `.ansible`, `ansible.log`, `.remember`, `.mkdocs-strict-check`,
  `site` and `.fanout`, and no `.cache`. `prek.toml` sets `always_run = true` on that hook. CI's
  `hooks` job skips only `ansible-lint` and `static-ratchet-tests`, in the scoped run and in
  the full sweep alike.
- **`vale.sh`'s lock-and-stamp guard has its own test.**
  `scripts/validate/tests/test_vale_sync_guard.py` fakes `vale` on PATH and covers a
  half-unpacked package and a provisioned worktree. `ansible/tests/leakguard.py:SHIMMED_BINARIES`
  names `curl`, so a test that runs a script calling `curl` stubs it itself.
- **`deploy.sh` forwards `-e` to ansible-playbook.**
  `scripts/deploy_tools/deploy_lib/flags.py:usage_error` refuses only a usage error.
- **The `observability` role is denylisted from auto-deploy** (`k8s_autodeploy: false`), so
  every PR here deploys through `land.sh` and `./scripts/deploy.sh --tags observability`.

## Design

### What M1 adds

```diff
 ansible/roles/k8s/observability/
+  templates/alertmanager.yaml.j2          Deployment and Service
+  templates/alertmanager-secret.yaml.j2   Secret: alertmanager.yml, homelab.tmpl, 3 credential files
+  templates/prometheus-rules.yaml.j2      ConfigMap: one key per observability_rule_groups entry
+  files/alertmanager/homelab.tmpl         Go templates for the Discord and email bodies
+  files/rules/alerting-meta.yaml          Watchdog and three meta alerts
+  tests/rules/alerting-meta.test.yaml     promtool unit tests for that group
~  templates/prometheus.yaml.j2            alerting:, rule_files:, the alertmanager job, --config.auto-reload,
                                           rules mount, the kube-state-metrics comment
~  tasks/main.yml                          alertmanager in the wait loop; the per-Secret restart and its amend
~  defaults/main.yml                       image pin, claim, watchdog repeat seconds, observability_rule_groups,
                                           stabilise entry, per-Secret restart keys
 ansible/roles/k8s/netpol-baseline/
~  defaults/main.yml                       the alertmanager probe row
 scripts/validate/
+  alert_tools.sh                          provisions promtool and amtool at the image tags
+  alert_rules.py                          the validate-alert-rules hook
~  root_ignored_files.py                   LOCAL_ONLY gains .cache
+  tests/test_validate_alert_rules.py
+  tests/test_alert_tools_guard.py         the checksum and stamp cases for alert_tools.sh
+  tests/fixtures/alert_rules/             a bad rule, a bad rule test, a bad Alertmanager config,
                                           a bad prometheus.yml
 scripts/docs/
~  fragment_renderers.py                   deadman_crons: the alertmanager-watchdog row
~  gen_doc_fragments.py                    deadman_inputs and _deadman read observability defaults
~ prek.toml                               the validate-alert-rules hook
~ .github/workflows/ci.yml                provision step and the --prove-red step
```

### Conventions every monitoring slice follows

M2 to M6 build on these rules and do not restate them.

1. **Rule files are plain YAML, never Jinja, and a list names each one.** Each family is one
   file, `ansible/roles/k8s/observability/files/rules/<group>.yaml`, holding one group named
   like the file. The role default `observability_rule_groups` lists the group names.
   `templates/prometheus-rules.yaml.j2` loops over that list and reads each file verbatim with
   `lookup('file', playbook_dir + '/roles/k8s/observability/files/rules/' + group + '.yaml')`.
   It never globs, because the offline renderer supports no `fileglob` (*Baseline*). A file
   that the list does not name never reaches the ConfigMap, so `alert_rules.py` fails when
   the stems in `files/rules/`, the list and the rendered `prometheus-rules` keys disagree.
   A PR that adds a rule file therefore adds its group to the list in the same commit. Rule
   annotations use Go templates (`{{ $labels.namespace }}`), which a Jinja render would
   consume. A threshold that mirrors an inventory value is a literal in the rule. A pytest pins
   it to its source, in the role's test directory or `ansible/tests/services/`.
2. **Every alert ships with a test that makes it fire.** Tests are promtool unit-test files,
   `ansible/roles/k8s/observability/tests/rules/<group>.test.yaml`. Each alert has at least one
   case with non-empty `exp_alerts`, and that case asserts the `service` and `severity`
   labels. Each family has one zero-series case: input series absent, and the family's
   `absent()` or census alert fires (defect class D4 in [design.md](../design.md)).
   `alert_rules.py` refuses an alert that no test fires. In the Prometheus rule directory, it
   also refuses a test file whose rule file holds an alert but which has no test group with
   empty `input_series` and an `exp_alerts` naming an alert other than the `Watchdog`. The Watchdog fires on any input,
   so an empty-input case that fires only the Watchdog proves nothing about the family. A
   group of recording rules alone, such as M2's `workload-service`, has no alert to fire and
   is outside this check.
3. **Labels (C7).** `severity` is a literal in every rule's `labels:`, one of `critical`,
   `warning` or `info`. `service` is a literal when the rule watches one service. When it
   watches many, `service` comes from the expression, through a join to the workload-service
   recording rules M2 adds. A series from a static scrape job has no `namespace`,
   `deployment` or `pod` label to join on, so that join maps it to `unmapped`. A rule over
   such series takes `service` from a literal job-to-service table in its expression instead.
   M1's row in that table is `alertmanager` → `observability`. During a parallel run with
   Kuma, a rule also carries `shadow: "true"`. That label is outside C7, and *Open questions* lists it as a contract gap.
   The C7 health gate excludes the `Watchdog` by name (*Open questions* 5). `alert_rules.py`
   therefore allows one rule named `Watchdog` across every rule file, in `alerting-meta`, with
   the expression `vector(1)`, and requires it from PR 4 on. A second rule of that name would
   be hidden from every health gate. In Prometheus rule files, it also refuses any other alert
   whose expression names no metric, such as `vector(1)` or `time() > 0`, because such an
   alert fires forever. A LogQL rule (M5) has a stream selector and no metric name, so that
   check and the zero-series check in convention 2 cover PromQL only.
4. **Annotations.** `summary` (one line), `description` (what fired and where to look) and,
   where a runbook exists, `runbook_url` pointing at `https://docs.local.<domain>/<page>/`.
   These replace the Kuma tile `description` and the `runbook` tag.
5. **One pager per check at every commit.** This is C4's two-writer rule applied to paging. At
   every commit, each check pages from Kuma or from Alertmanager, never both. A migrated rule
   lands with `shadow: "true"`, which routes to the blackhole receiver. The cutover PR drops
   the label and deletes the Kuma producer and its tile in the same commit. Between the
   cutover's deploys a check can briefly have no pager. That is the safe direction, and each
   cutover bounds the window by the tile's push interval. A new scrape job joins a Kuma tile
   with no Kuma edit. `check_cluster_targets` in monitor-bridge selects
   `up{origin!="daniel-server"}`, which matches every job without an `origin` label. Until
   M6's batch A3 cuts the `Cluster Scrape Targets` tile over, no unshadowed rule may page
   `up == 0` for such a job, and an `absent()` over its other series is conjoined with
   `up == 1`. `AlertmanagerMetricsMissing` is the worked example. A3's cutover commit then
   gives every such job its `up == 0` pager. Its scrape-target rule exempts none of them,
   maps each to a deploy tag through convention 3, and carries no `shadow`. *What M1 hands to
   M6's batch A3* lists M1's share.
6. **A soak is gated on a query, not on a feeling.** During a parallel run, Prometheus already
   scrapes Kuma's `monitor_status` (job `uptime-kuma`, labels `monitor_name` and
   `monitor_type`, with 0 meaning DOWN). Each soak runs two queries over its window through
   `probe.py metric`. The first counts the minutes Kuma read DOWN while no shadow alert fired.
   It must be 0, or each minute must be explained in the PR. The second counts the minutes a
   shadow alert fired while Kuma read up, and each such episode is explained or the rule is
   tuned. The window starts at the shadow PR's deploy, never before it. A fixed `[7d:1m]` run
   on day 3 would count four days in which no shadow rule existed, and would read every Kuma
   DOWN minute there as a miss. Use `[<H>h:1m]`, where `<H>` is the whole hours since the
   deploy, at most 168. The PR records the deploy time. First confirm that retention covers
   the window: `(time() - prometheus_tsdb_lowest_timestamp_seconds) / 86400`. Retention is
   capped at 7GB by `observability_prometheus_retention_size`, not by days.
7. **No `${` in a rule or template source.** `alert_rules.py` refuses `${` in the source
   files only: `files/rules/*.yaml`, `tests/rules/*.yaml` and `files/alertmanager/homelab.tmpl`.
   PromQL, promtool tests and Alertmanager's Go templates have no `${` syntax, so one there is
   a mistake. R2's escape pass would also rewrite it to `$${`, so the rendered ConfigMap would
   differ from the file promtool checked. The rendered `alertmanager.yml` is outside the
   refusal, because it carries C1's `${<key>}` placeholders by design once R2 lands: `email`
   renders as `${email}`. `alertmanager-secret.yaml.j2` writes `stringData`, as every k8s
   Secret template does (*Baseline*), so Flux reads those placeholders as text. A bare `$1` from
   PromQL's `label_replace` is safe, as *Open questions* 7 records.
8. **Deploying.** Each PR names its deploy tags. A tag deploys with
   `./scripts/deploy.sh --tags <tag>` (run by `land.sh` after the merge), unless its
   `containers_list` entry carries `owner: flux`. Then the merge's `promote` run applies it
   (C2), and Ansible must not touch it (C4). M2 to M6 run while D-slices move roles to Flux,
   so each PR checks the entry before it deploys.
9. **Verification goes through `probe.py metric`.** Alertmanager has no route. The read-only
   ServiceAccount cannot exec into a pod or port-forward. Alertmanager's own state is read from
   its scrape job (`alertmanager_*` series) and from `ALERTS` in Prometheus.

### The Alertmanager workload

- **Where.** A `Recreate` Deployment `alertmanager` in the `observability` role and namespace,
  image `prom/alertmanager` pinned by digest as `observability_k8s_alertmanager_image` at
  v0.34.1 or newer. Renovate's existing `_image:` manager tracks it. One replica, gossip off
  (`--cluster.listen-address=` empty), `--storage.path=/alertmanager`.
- **Pod shell.** The image runs as `nobody` (*Baseline*), and a fresh Longhorn volume's root is
  owned by root. The pod therefore copies Prometheus's shell:
  `pod_shell('homelab-critical', run_as_user=65534, run_as_group=65534, fs_group=65534)` and
  `hardened_security_context(non_root=true)`. With no `service_account`, `pod_shell` sets
  `automountServiceAccountToken: false`. Without `fs_group`, Alertmanager cannot write its
  silences or its notification log. The Secret volume carries `defaultMode: 0o444`, as
  Prometheus's scrape credentials do. The pod template carries `app: alertmanager`, because
  the role's restart-count snapshot and its "selector matched no pods" assert select on
  `app=<name>`.
- **Storage.** A `k8s_claims` entry `alertmanager-data`, 1Gi, on
  `observability_storage_class` (`longhorn-nobackup`). It holds silences and the notification
  log, so a restart does not re-send every open alert or lose a silence. Losing the volume
  costs only those two.
- **Config.** Secret `alertmanager-config` (`templates/alertmanager-secret.yaml.j2`, rendered
  0600 under `no_log`) with five keys: `alertmanager.yml`, `homelab.tmpl`,
  `discord-webhook-url`, `smtp-password` and `watchdog-url`. The pod mounts all five at
  `/etc/alertmanager/`. The config reads the three credentials through `webhook_url_file`,
  `auth_password_file` and `url_file`. It names its template as the relative path
  `homelab.tmpl`, so amtool resolves it beside the config in CI.
- **Go templates inside a Jinja template.** `homelab.tmpl` lives in `files/` and reaches the
  Secret through `lookup('file')`, which does not template it. `alertmanager.yml` is part of a
  Jinja template, because it reads `email` and, after M2, the `weekly_reboot_*` variables.
  Every Go expression in it sits inside `{% raw %}` … `{% endraw %}`: the Watchdog's
  `payload` (`{{ .Status }}`) and the Discord and email `title` and `message` references
  (`{{ template "homelab.title" . }}`). Without the raw block, Jinja refuses both forms as a
  `TemplateSyntaxError`, so that render fails outright. A Go expression that Jinja can parse
  is consumed instead, and the rendered file still passes amtool while it sends an empty
  field. The survival check in `alert_rules.py` catches that second case.
- **Restart, PR 2.** Alertmanager reloads its config only on SIGHUP or a request to
  `/-/reload` (`cmd/alertmanager/main.go` at v0.34.1), and nothing in the pod sends either. The
  pod reads a role Secret, so `observability_stabilise_workloads` gains
  `{kind: deploy, name: alertmanager, restart_on: [secret]}`, appended last. The PVC keeps
  silences and the notification log through a restart.
- **Restart, PR 5: one Secret, one restart.** Under the one-register signal (*Baseline*), every
  later edit to `alertmanager.yml` or `homelab.tmpl` would also restart the `Recreate`
  Prometheus and Grafana. That costs the evaluation gap and WAL replay that
  `--config.auto-reload` exists to avoid, and M2 to M6 edit that config often: mute
  intervals, receivers, routes and the `inhibit_rules` that replace the bridge's gates. PR 5
  narrows the restart to the Secret each workload reads:
    - Each Secret reader's `observability_stabilise_workloads` entry gains
      `restart_secrets`, the rendered Secret files it mounts: `scrape-credentials-secret.yaml`
      for Prometheus, `grafana-secret.yaml` for Grafana and `alertmanager-secret.yaml` for
      Alertmanager. `origin-pull-secret.yaml` then restarts nothing, which is correct, because
      no pod reads it.
    - One `set_fact`, `observability_changed_secret_files`, derives the changed files by
      pairing each `manifests_secret_render` result's `changed` flag, by position, with
      `manifests_secret_files_resolved`. It never reads a result's `item`. The render task is `no_log`. The repo already relies on a
      `no_log` register's `changed` flag: in
      `ansible/roles/k8s/image-builder/tasks/main.yml`, the comment on the build gate says
      `no_log` "censors the register's CONTENT, not its `changed`". Nothing in the repo shows
      that a result's `item` survives. A selection that matched nothing would
      skip every restart and leave a pod on stale config behind a green deploy.
    - Two other tasks key on the same one-register signal at `70591c307`, and PR 5 moves
      both to the fact. The restart-count snapshot's secret arm hands every workload to the
      play's stabilisation pause. "Sync the live Grafana admin password with SOPS" execs the
      Grafana CLI against Grafana's live database. Left alone, each would fire on every
      Alertmanager config edit. The snapshot then reads the per-entry `restart_secrets`
      condition, and the sync reads `grafana-secret.yaml`.
    - The three entries carry `rolled_by_role: true`, so `release_stamp.yml` stamps
      `restart: false` for them. A task after the restart includes `rollout_amend.yml` for each
      workload the restart task actually restarted, as pihole does. The release record then
      states what the role did, and `release_stamp.yml`, which every k8s role runs, stays
      unchanged.

  The rejected alternative is a per-file key that `release_stamp.yml` reads. It changes the
  shared role every service runs, for one role's need. Until PR 5 deploys, an
  `alertmanager-secret.yaml` change restarts Prometheus and Grafana too. No M1 PR after PR 2
  changes that Secret, and M2 depends on all of M1, so no Alertmanager config edit lands in
  that window.
- **Network.** The pod carries `netpol-baseline: enforced`. The observability baseline admits
  Prometheus, which sits in the same namespace. M5 adds the loki-homelab ruler as a
  cross-namespace caller. A `netpol_baseline_probe_targets` row
  `{service: alertmanager, plane: observability, port: 9093, expect: refused, policy: networkpolicy-observability.yaml.j2}`
  proves that a pod in the `homelab` namespace is refused. It is the first refused row against
  the observability baseline itself. The refused leg prints `t.policy`. The offline render
  turns a missing `policy` into `STUB` and passes, while the real deploy fails on the missing
  key. PR 1 therefore adds a test that every row carrying `expect: refused` carries
  `policy`. The table passes it at `70591c307`, so the test is in place before PR 2 adds
  the row.
- **Eviction tier.** `homelab-critical`, beside Prometheus, because it is the paging path.
  That amends the `DECIDED observability tiers` marker in the role's `CLAUDE.md`, and
  *Open questions* asks the operator to confirm it.
- **Resources.** A 10m CPU request and a 64Mi memory request; limits of 200m and 128Mi. A
  week after PR 2 deploys, measure
  `max_over_time(container_memory_working_set_bytes{namespace="observability",container="alertmanager"}[7d])`
  and correct the request in a follow-up PR if the peak sits above it.

### The routing tree

```yaml
route:
  receiver: discord            # root default; the catch-all child below is what matches
  group_by: [alertname, service]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 6h          # Kuma resends a DOWN tile every 90 beats, 6h for a bridge tile
  routes:
    - matchers: [alertname="Watchdog"]
      receiver: healthchecks
      group_wait: 0s
      group_interval: 1m
      repeat_interval: 60s     # observability_alertmanager_watchdog_repeat_seconds
    - matchers: [shadow="true"]
      receiver: blackhole
    - matchers: [severity="critical"]
      receiver: discord
      continue: true
    - matchers: [severity="critical"]
      receiver: email
    - receiver: discord        # catch-all; M2 adds mute_time_intervals here and above
receivers:
  - name: blackhole
  - name: healthchecks         # url_file: https://hc-ping.com/<ping key>/alertmanager-watchdog
  - name: discord              # webhook_url_file, title and message from homelab.tmpl
  - name: email                # Gmail SMTP on 465, Kuma's account, auth_password_file
```

The Watchdog route comes first and never takes a mute interval, so the weekly reboot window
cannot silence the off-site watcher. The healthchecks receiver sets `send_resolved: false`.
When Prometheus stops sending the Watchdog, Alertmanager resolves it after `resolve_timeout`.
A resolved notification would count as a ping and hide the outage. Its `payload` is
`{"status": "{{ .Status }}"}`, so Healthchecks.io stores no labels and no URL.
`docs/healthchecks-io-deadman.md`'s *What leaves the house* table gains that row.

### Prometheus side

- `alerting.alertmanagers` names the `alertmanager` Service by its static in-namespace name,
  port 9093.
- `rule_files: ['/etc/prometheus-rules/*.yaml']`. The `prometheus-rules` ConfigMap mounts there
  as a whole directory, never with `subPath`, because a `subPath` mount never sees an update. It
  sits beside `/etc/prometheus`, not under it, because that path is the `prometheus-config`
  volume.
- `--config.auto-reload` (interval 30s) reloads a changed rule file without rolling the pod.
  Rolling the `Recreate` Prometheus would leave an evaluation gap and replay the WAL, and a
  rule edit should cost neither. The ConfigMap therefore carries no checksum annotation. The
  role's `CLAUDE.md` gets a `DECIDED rules reload in place` marker at the mount, and
  `_CONFIGMAP_DATA_KEYS` exempts `prometheus-rules` by name, with a positive assertion in its
  place. If Prometheus mounts `prometheus-rules`, its args must carry `--config.auto-reload`.
  `prometheus.yml` keeps its checksum and still rolls the pod.
- A hand-written `alertmanager` scrape job (static target, the in-namespace Service on port
  9093). It makes `alertmanager_notifications_total`, `alertmanager_notifications_failed_total`
  and `alertmanager_alerts` queryable, which is how every slice verifies Alertmanager. The job
  carries no `origin` label. Kuma's `Cluster Scrape Targets` tile therefore watches its `up`
  series from PR 2 on, as *The rules M1 ships* explains.

### The rules M1 ships

`files/rules/alerting-meta.yaml`, group `alerting-meta`, all with `service: observability`:

| Alert | Expression, in words | `for` | Severity |
|---|---|---|---|
| `Watchdog` | always 1 (`vector(1)`) | — | `info` |
| `PrometheusRuleReloadFailed` | `prometheus_config_last_reload_successful == 0` | 10m | `warning` |
| `AlertmanagerNotificationsFailing` | `increase(alertmanager_notifications_failed_total[30m]) > 0`, by integration | — | `critical` |
| `AlertmanagerMetricsMissing` | `absent(up{job="alertmanager"})` or (`absent(alertmanager_notifications_failed_total{job="alertmanager"})` and on() `up{job="alertmanager"} == 1`) | 10m | `warning` |

`AlertmanagerNotificationsFailing` is Alertmanager's counterpart of Kuma's `Kuma Notification
Delivery` tile (`kuma_notify_failures` in `check_table.py`). It is critical, so it goes to both
Discord and email, and a dropped Discord send still reaches the operator by email. M1 ships it
before any family cuts over. Once M2 cuts over, Alertmanager pages for real, and a dropped page
must not be silent.

`AlertmanagerMetricsMissing` is the family's zero-series alert (convention 2). If
Alertmanager's metrics stop reaching Prometheus, `AlertmanagerNotificationsFailing` evaluates
to nothing and stays quiet, which is failure class 3. That can happen three ways, and each has
one pager:

- **The scrape job is missing from the config.** No `up{job="alertmanager"}` series exists,
  so no Kuma tile can see the loss. The first arm, `absent(up{job="alertmanager"})`, pages it.
  It is also the arm that convention 2's empty-input case fires.
- **The job is configured, but the scrape fails.** `up{job="alertmanager"}` reads 0. Kuma
  already pages this case. `ansible/roles/k8s/monitor-bridge/files/checks/cluster.py:check_cluster_targets`
  selects `up{origin!="daniel-server"}`, which matches every series with no `origin` label,
  and the job has none. The `Cluster Scrape Targets` tile goes DOWN after
  `CLUSTER_TARGETS_CONSECUTIVE` (3) bridge cycles. The rule therefore leaves this case alone,
  as convention 5 requires. The second arm conjoins with `up{job="alertmanager"} == 1`,
  because the counter series go stale on a failed scrape and an unconjoined `absent()` would
  page the tile's condition a second time.
- **The scrape works, but the counter is gone.** An Alertmanager upgrade that renamed
  `alertmanager_notifications_failed_total` would do this. Alertmanager creates the counter at
  0 for every integration (*Baseline*), so the series is present whenever the scrape works and
  the metric keeps its name. No tile reads Alertmanager's own metrics, so the second arm pages
  this case.

An Alertmanager that is down cannot deliver an alert about itself. That case pages through
the Kuma tile, because its scrape fails, and through the Watchdog's Healthchecks.io check.
Neither route passes through Alertmanager, so convention 5 still holds. Once M6's batch A3
cuts the `Cluster Scrape Targets` tile over, A3's scrape-target rule pages a failed scrape
instead, and `AlertmanagerMetricsMissing` keeps its narrow scope. The 10-minute `for` covers
a `Recreate` restart and the pod order after the weekly reboot.

**What M1 hands to M6's batch A3.** A3's cutover deletes the `Cluster Scrape Targets` tile,
so the failed Alertmanager scrape needs a new pager in the same commit. M6's A3 row names
only `CLUSTER_TARGETS_CONSECUTIVE` becoming `for: 10m`. The four obligations below therefore
travel to M6 as a cross-slice request, and *Open questions* 8 tracks them:

- **A3's scrape-target rule covers job `alertmanager`, with no exemption.** An exemption
  would leave "job configured, scrape failing" with no pager at all, because the second arm
  of `AlertmanagerMetricsMissing` requires `up == 1`. That is failure class 3.
- **It maps that job to `service="observability"`** through the job-to-service table of
  convention 3. The job's `up` series carries only `job` and `instance`, like the `prometheus`
  job's static target in `prometheus.yaml.j2`. M2's workload-service join would map it to
  `unmapped`, and `observability`'s C7 gate would never see it.
- **It lands without `shadow`,** in the commit that deletes the tile (convention 5).
- **A3 records the slower page for a down Alertmanager.** A3's rule fires in Prometheus, but
  its notification would pass through the Alertmanager that is down. From A3 on, that case
  pages only through the Watchdog's Healthchecks.io check, up to 30 minutes in (*Healthchecks.io
  sizing for the Watchdog*). The tile pages it 10 to 15 minutes in until then.

`AlertmanagerMetricsMissing` needs no edit at A3. Its first arm still pages a missing job,
and its second arm a lost counter. PR 3's comment above the rule names the tile as the
failed scrape's pager until A3, and A3's rule after it, so A3's author meets the obligation
at the rule that depends on it.

**These alerts and the C7 health gate.** The Watchdog is excluded from the gate by name
(*Open questions* 5). The other three are meant to fail `observability`'s gate: the role owns
the paging path, and a deploy is not healthy while that path drops sends, cannot reload its
rules or has lost its scrape job or its metrics. A scrape that fails with the job in place
reaches no M1 alert, because the Kuma tile owns it. It reaches the gate from M6's batch A3
on, and only if A3's rule carries `service="observability"` for the job. The gate reads
`ALERTS` in Prometheus, so it sees that alert even while Alertmanager is down. The cost is
that one transient failed send keeps `AlertmanagerNotificationsFailing` firing, and the gate
red, for up to 30 minutes after the cause clears. The gate names the alert, so the operator can tell such a send from a deploy
fault. Once `observability` is flux-owned, the same critical alert also fails D5's soak when
it becomes active during the soak. That is intended too, because a promote is not safe while
pages are dropped.

### Rules other slices add (C7)

C7 adds three rule sources that join this rules directory and page through Alertmanager.
Each follows conventions 1 to 9: its own file and group, its group in
`observability_rule_groups`, a promtool test that fires every alert, and a zero-series case.
Each PR that adds one waits on M1.

- **The `flux` rule group, from D1's PR D1.6b.** It fires on a controller that is down, on a
  Kustomization or the OCIRepository not Ready for 15 minutes, and on a Kustomization
  suspended for more than 24 hours. D4 adds a rule for saturated reconcile workers.
- **The deploy-pipeline exporter's rules, from D2's PR D2.6d** (about 260 lines).
  `DeployHoldActive` is a warning, `PromotionStalled` fires on an unpromoted green commit
  older than 2 hours with no hold, and `ProdMovedOutsidePromote` is critical.
  `DeployPipelineExporterAbsent` is the family's zero-series alert (convention 2). The rules
  carry `service="deploy-pipeline"`. C7 *Rule labels* admits that literal as its one
  exception to the deploy-tag-or-host rule, because it names no deploy tag and so no health
  gate reads it. `alert_rules.py` checks only that `service` is present. D2.6d also
  adds a 24-hour repeat route for `DeployHoldActive` to *The routing tree*, with a case in
  M1's routing test. It adds about 9 series against *The series budget*.
- **H3's host rules.** `HostReconcileFailed` fires on `homelab_host_reconcile_ok` at 0 for 15
  minutes, and `HostUpdateStale` sits beside it. They survive M6, and H5 relies on them.

Convention 5 binds all three until M6's batch A3 cuts the `Cluster Scrape Targets` tile
over. Until then no unshadowed rule among them may page `up == 0` for a scrape job without an
`origin` label, and an `absent()` arm over such a job's series is conjoined with `up == 1`.
A controller-down arm in the `flux` group, and an exporter-absent arm in D2's or H3's, is
where that rule applies.

### CI and local tooling

- **`scripts/validate/alert_tools.sh`** provisions `promtool` and `amtool` into
  `.cache/alert-tools/<version>/`. Two readers see that path (*Baseline*):
    - The porcelain status the GitOps deployer reads omits it, because the root `/*` rule in
      `.gitignore` ignores it (`git check-ignore -v .cache/alert-tools/x` names that rule at
      `70591c307`).
    - `scripts/validate/root_ignored_files.py` flags it, because `/*` is its deciding rule.
      PR 1 therefore adds `.cache` to that module's `LOCAL_ONLY`, with a one-line reason
      comment, as `.fanout` has. Without it, the hooks job goes red from PR 1 on, since CI's
      provision step runs before "Run hooks". Every later commit in a provisioned worktree
      fails the same way.

  **HISTORY —** #4705 moved the cache to `alert-tools/` under the git common dir, so every
  worktree of a clone shares one provision. Neither reader above sees it, and `.cache` left
  `LOCAL_ONLY`.

  The script reads the versions from `observability_k8s_prometheus_image` and
  `observability_k8s_alertmanager_image` rather than pinning its own. A Renovate image bump
  therefore moves the tool in the same PR, and no agreement test can go red between two
  managers. It fetches with `curl -fsSL` and checks each tarball against the release's
  `sha256sums.txt`. It locks with `flock` on the cache directory and writes a stamp only after
  a clean unpack, following the `scripts/validate/vale.sh` pattern. With no arguments it
  provisions and exits 0, which is how CI provisions before the hooks run.
  `scripts/validate/tests/test_alert_tools_guard.py` is its red proof, as
  `test_vale_sync_guard.py` is `vale.sh`'s. The test puts a fake `curl` on PATH, ahead of
  leakguard's recording shim. The fake copies fixture tarballs and a sums file from a
  temporary directory and logs each fetch. Three cases:
    - a tarball whose digest is not in the sums file makes the script exit non-zero and
      write no stamp (flagged);
    - a version directory that holds files but no stamp is fetched again (flagged);
    - a complete, stamped cache is reused with no fetch (clean).
- **`scripts/validate/alert_rules.py`**, the `validate-alert-rules` prek hook, does two phases:
    - In Python, tested in pytest:
        - every rule file parses;
        - the stems in `files/rules/`, `observability_rule_groups` and the rendered
          `prometheus-rules` keys are one set (convention 1);
        - every alert has a literal `severity` from the allowed set;
        - every alert has `service`, as a literal label or through the M2 recording rules;
        - `shadow` is absent or `"true"`;
        - at most one rule is named `Watchdog`, in `alerting-meta`, with expression
          `vector(1)`, and from PR 4 exactly one; in Prometheus rule files, every other
          alert's expression names a metric (convention 3);
        - no source file contains `${` (convention 7);
        - a test case fires every alert;
        - in the Prometheus rule directory, each test file whose rule file holds an alert
          has an empty-input case that fires an alert other than the Watchdog (convention 2);
        - the rendered `alertmanager.yml` still contains `{{ .Status }}` and every
          `{{ template "homelab.` reference its source template holds, so a render that lost
          its `{% raw %}` block fails.

      The metric-name check is lexical: an expression whose every identifier is a PromQL
      function, keyword or label name is refused. It can miss a constant expression. The
      gate's safety does not depend on it, because it rests on the name check, which is
      exact. Each rule is a
      `..._is_clean` / `..._is_flagged` pair. The non-vacuity floor names concrete members,
      so an empty glob fails instead of passing: `prometheus.yml` from PR 1,
      `alertmanager.yml` from PR 2, the `alerting-meta` group from PR 3 and the `Watchdog`
      from PR 4.
    - With the tools:
        - `promtool check rules` on every rule file;
        - `promtool test rules` on every test file;
        - `amtool check-config` on `alertmanager.yml` and `homelab.tmpl`;
        - `promtool check config --syntax-only` on the rendered `prometheus.yml`. A config
          Prometheus rejects at startup leaves the `Recreate` Deployment with no running
          Prometheus, and monitor-bridge reads that Prometheus every cycle. `--syntax-only`
          skips the referenced files, such as the scrape-credential and service-account paths,
          which do not exist in CI.

      The script extracts the three rendered files from the role's ConfigMaps and Secret
      through `validate.k8s_manifests`' render, the same path `probe.py health` uses, with
      secrets stubbed. The rendered `prometheus.yml` holds no stubbed value (*Baseline*).
- **`--prove-red`** runs each tool against `scripts/validate/tests/fixtures/alert_rules/` and
  exits 0 only when every tool rejects its fixture. The fixtures are a bad rule, a bad rule
  test, a bad Alertmanager config and a bad `prometheus.yml`. The `hooks` job runs it after
  provisioning. The pytest shards install no binaries, so this step is the tools' red proof.
  The suite `scripts/validate/tests/test_validate_alert_rules.py` is the Python phase's red
  proof, as `scripts/validate/tests/test_every_validator_has_a_red_proof.py` requires.

### Healthchecks.io sizing for the Watchdog

The slug is `alertmanager-watchdog`, a Simple check with `timeout: 600` and `grace: 1200`,
matching `uptime-kuma-alive`. Pings arrive every 60s
(`observability_alertmanager_watchdog_repeat_seconds`). Healthchecks.io has no maintenance
window, so the 30-minute bound has to absorb the weekly reboot. The last ping leaves at about
07:35, and the next one waits for daniel-box to boot, Prometheus to replay its WAL and evaluate,
and Alertmanager to start. The 2026-08-30 record has the last pod Ready at 07:45, so the gap is
about 15 minutes against 30. A reboot that takes longer than 30 minutes pages, and it should.

**The down-Alertmanager window (RA-8).** From M6's batch A3 on, a down Alertmanager pages only
through this check, up to 30 minutes in. Kuma's tile paged it 10 to 15 minutes in. A shorter
check would page sooner: a 5-minute period with a 10-minute grace pages about 15 minutes in.
The weekly reboot's gap above is also about 15 minutes, so that check would page on most
reboots. M1 therefore keeps 600s and 1200s and records the slower page. Accepting it is
**operator-only** (*Open questions* 10).

`test_monitor_bridge_healthchecks_expected.py` derives slugs from cron variables, and this slug
has no cron. The slug rests on five pieces. PR 2 adds the first, and PR 4 adds the rest:

- **The variable.** `observability_alertmanager_watchdog_repeat_seconds: 60` holds whole
  seconds. The Secret template renders `repeat_interval: {{ … }}s`, so no reader parses a
  duration string.
- **The row.** `scripts/docs/fragment_renderers.py:deadman_crons` and
  `render_deadman_cadences` gain a seventh positional argument, `observability`, the role's
  defaults. It has no default value, so a missing key still raises `KeyError`. The row's slug
  is `alertmanager-watchdog`, its cadence is `every 60s`, and its source names
  `observability_alertmanager_watchdog_repeat_seconds` as Alertmanager's Watchdog repeat, not
  a cron. The cadence field takes exactly the form `every <N>s`, which no cron expression
  matches.
- **The inputs and the header.** `scripts/docs/gen_doc_fragments.py:deadman_inputs` returns
  a seven-tuple, adding `estate.role_vars` of the observability role. Its return annotation
  follows, and its docstring loses "The six arguments". `_deadman`'s sources list gains
  `ansible/roles/k8s/observability/defaults/main.yml`, so the fragment's `generated_from`
  header names the file the row reads. The `regen-doc-fragments` hook's `files` regex already
  covers `ansible/roles/`.
- **The test branch.** For a `kind: simple` slug, `cron_mismatches` first matches the
  cadence against `^every (\d+)s$`. The row is clean when N is below the slug's `timeout` and
  flagged when N is at or above it. A string that matches neither that pattern nor
  `_EVERY_N_MINUTES` stays flagged, as a malformed cron is at `70591c307`. The pair is the live
  row (clean) and a fixture `every 600s` against `timeout: 600` (flagged).
- **The rendered table.** The deadman-cadences fragment shows the row as `every 60s` in its
  cron column. `docs/healthchecks-io-deadman.md` says that "a `*/10` minute cron is a Simple
  check with a 10-minute period, and every other slug is a Cron check carrying that
  expression". PR 4 rewrites that sentence: an `every <N>s` row is a Simple check too, and its
  period is the slug's `timeout`, not N.

### The series budget (C7)

C7 has M1 set one series budget for the fleet. Each slice that adds a scrape job, a probe
target or a label states its delta against it.

- **The reading.** The 2026-10-12 pillar review read 137,084 head series through
  `probe.py metric`, with 6.21 GB of the 7 GB `observability_prometheus_retention_size` in use
  and 27.3 days of retention.
- **The rate.** 6.21 GB over 27.3 days is about 227 MB a day, or about 1.66 kB per head
  series per day at the scrape mix of that day. The model in the observability defaults'
  comment on the size cap predicts 219 MB a day, within 4%. Bytes scale with samples per
  second, so a series scraped every 15 seconds costs four times a 1-minute one.
- **The budget is 140,000 head series.** At the measured rate, the 7 GB cap holds about 30
  days at that count, which is the `observability_prometheus_retention` the role sets. Above
  it, the size cap binds before the time cap, and retention shrinks with nothing reporting
  it, as the `DECIDED` comment on the size cap warns. That comment holds the cap near 58% of
  the 12 Gi claim, so raising the cap alone does not raise the budget.
- **The headroom is about 2,900 series.** The longest window a monitoring slice reads is
  convention 6's 168-hour soak, which the cap covers at any count the plan reaches. The
  budget therefore protects the dashboards' 30-day history, not the soaks.
- **How a slice states its delta.** Its *Complexity delta* gives the head series it adds, as
  targets times series per target, with the scrape interval when it is not 1 minute. After
  its deploy, its PR records `prometheus_tsdb_head_series` before and after. A delta that
  would cross the budget names its offset in the same slice: a dropped family, as
  `prometheus.yaml.j2` drops the `_bucket` families, or a shorter label set.
- **M1's own delta.** PR 2's `alertmanager` scrape job adds one target's Go runtime,
  process and `alertmanager_*` families, a few hundred series. The rules add `ALERTS` series
  only while they fire, and the Watchdog's one always. D2.6d adds about 9.
- **Whether the cap already binds** is *Open questions* 9. The reading's 27.3 days is under
  30, so either the store is younger than 30 days or the size cap has already deleted a
  block.

## Steps, as PRs

Every PR below deploys `observability` unless it says otherwise. Before each deploy, check the
entry against convention 8.

### PR 1 — Provision promtool and amtool, and the validator that can go red (about 270 lines)

PR 1 runs over the 200-line target because the provisioning script, its guard test and the
`LOCAL_ONLY` entry must land with the validator that needs the tools. Split off, the validator
would have no tool to run, or CI would provision a cache that the root-file hook flags.

- Add `scripts/validate/alert_tools.sh`, `scripts/validate/alert_rules.py` (tool phase plus
  discovery), the fixtures and `scripts/validate/tests/test_validate_alert_rules.py`.
- Add `scripts/validate/tests/test_alert_tools_guard.py` with the three cases in *CI and local
  tooling*.
- In `scripts/validate/root_ignored_files.py`, add `.cache` to `LOCAL_ONLY` with a comment
  naming `alert_tools.sh` as its writer. The fixture of
  `test_a_reincluded_or_local_only_root_entry_is_clean` in
  `scripts/validate/tests/test_validate_root_ignored_files.py` gains `.cache/`, so dropping
  the name turns that test red.
- Add the `validate-alert-rules` hook to `prek.toml`. On a PR, CI runs a hook only when a
  changed file matches its `files` regex, so the regex must name every input the hook reads.
  In this PR it covers:
    - `ansible/roles/k8s/observability/(files/rules|tests/rules|files/alertmanager)/`;
    - the role's `prometheus`, `prometheus-rules` and `alertmanager` templates;
    - `ansible/roles/k8s/observability/defaults/main.yml`, whose image tags set the tool
      versions and which holds `observability_rule_groups`;
    - `ansible/inventory/(host_vars/[^/]+|group_vars/all)\.yml`, which feed the
      `prometheus.yml` loops and the Alertmanager template. The `validate-compose-templates`
      hook in `prek.toml` uses the same alternation;
    - `ansible/filter_plugins/scrape_jobs.py`, which builds the scrape jobs;
    - `scripts/validate/alert_`.

  M2 adds `scripts/validate/pint/`. M5 adds `ansible/roles/k8s/loki-homelab/files/rules/` and
  that role's `defaults/main.yml`. Each in the PR that adds the input.
- In `.github/workflows/ci.yml`'s `hooks` job, add a provision step after Vale and a
  `uv run python scripts/validate/alert_rules.py --prove-red` step.
- The tool phase runs `promtool check config --syntax-only` on the `prometheus.yml` the tree
  already renders, so the floor names `prometheus.yml` from this PR. The Python phase has no
  rule file to read yet. PR 2 and PR 3 raise the floor.
- A test joins `ansible/tests/k8s/test_netpol_probe_targets.py`: every row carrying
  `expect: refused` carries `policy`. Its pair is the live table (clean) and a fixture row
  without `policy` (flagged). It does not depend on Alertmanager, so it lands here and holds
  PR 2's row.
- No deploy.
- Two-writer check: no ownership moves.

### PR 2 — Alertmanager with its receivers (about 290 lines)

PR 2 runs over the 200-line target because the Secret carries the whole routing tree. Split
from the workload, either half would deploy an Alertmanager with no config or a config with
no Alertmanager. About 40 of its lines drop the counts that a seventh workload makes false,
and those edits must land with the workload.

- Add `templates/alertmanager.yaml.j2` with the pod shell in *The Alertmanager workload*,
  `templates/alertmanager-secret.yaml.j2` (`stringData`), `files/alertmanager/homelab.tmpl`,
  the `alertmanager-data` claim, `observability_alertmanager_watchdog_repeat_seconds: 60` and
  the stabilise entry. The image pin `observability_k8s_alertmanager_image` landed in PR 1,
  because `alert_tools.sh` reads amtool's version from it.
- Make room in the observability role's `CLAUDE.md` first. After PR 1 it is 7065 characters
  against the 7070-character ceiling that `ansible/tests/repo/test_role_claude_md.py` enforces.
  The new claim grows the generated *At a glance* block, and the *Alert plane* paragraph adds
  more, so move text to `docs/observability-dashboards.md` before adding either.
- Correct the one-register prose for the new Secret reader: the comment above
  `observability_stabilise_workloads` and the role `CLAUDE.md`'s *One config change, one
  restart* section, as *Tests and docs that change in the same PR* lists. The stabilise entry
  moves a recorded fact atom, so PR 2 runs `fact_status.py verify` on that section until G2
  retires `docs/facts.lock`. The section's "these six" goes in the same edit, before the
  `verify`.
- Drop the count from every passage that *Baseline* lists under "A seventh workload". A count
  rewritten as "seven" would go stale again with the next workload, so each passage names the
  set instead: "these workloads", "the workloads that read one", "this role's manifests".
  `k8s_autodeploy_reason` loses "six sub-images", and the `regen-role-glance` hook rewrites
  the *At a glance* block from it in the same commit. The five tier comments say "Tier 4;
  `DECIDED observability tiers` names the tier-1 exceptions", and `alertmanager.yaml.j2`
  carries its own tier-1 comment that cites the marker. The tier comments sit in pod specs
  outside any value, so `homelab_role_label` strips them and they move no staged bytes.
  netpol-baseline's observability policy is a deferred render, so its comment edit moves
  staged bytes, and `kubectl apply` reports the object `unchanged`.
- In `tasks/main.yml`, add `{kind: deploy, name: alertmanager}` to the literal loop of "Wait
  for the telemetry workloads to roll out", after `grafana`. The restart-count snapshot still
  runs after that wait.
- In `prometheus.yaml.j2`, add the `alerting:` block and the `alertmanager` scrape job.
  Prometheus rolls once, through its config checksum.
- Test edits in the same PR, in
  `ansible/tests/services/test_observability_config_rolls_one_workload.py`:
    - `_EXPECTED_WORKLOADS` and `_EXPECTED_SECRET_READERS` gain `alertmanager`;
    - the superset assertion in
      `test_a_workload_mounting_a_role_configmap_carries_the_checksum_annotation` exempts
      `alertmanager` beside `kube-state-metrics`, because it mounts a Secret and no role
      ConfigMap. Its failure message names both reasons.

  In `ansible/tests/k8s/test_self_rollouts_follow_the_apply.py`,
  `test_observability_a_changed_secret_expects_only_its_two_readers_to_roll` becomes
  `test_observability_a_changed_secret_expects_only_its_secret_readers_to_roll`. Its expected
  set gains `alertmanager`, because it reads the real defaults (*Baseline*), and its docstring
  loses "two". No doc cites the old name.
  `test_the_role_lists_its_workloads_the_same_way_everywhere` passes once the defaults entry
  and the wait loop both name `alertmanager`.
- `netpol_baseline_probe_targets` gains the `alertmanager` row, with its `policy` key, so this
  PR also deploys `netpol-baseline`, after `observability`. PR 1's `policy` test holds the
  row.
    - **D1.6 edits the same list.** It adds a `flux-system` plane with Service-port rows,
      because Flux's Services map port 80 to 9090 and 9292, and it widens the probe table to
      dial the Service port. Whichever of PR 2 and D1.6 lands second rebases its rows onto the
      other's form. Alertmanager's Service port and container port are both 9093, so its row
      reads the same under either form.
- The PR body records `prometheus_tsdb_head_series` before and after its deploy. That is
  M1's own delta against the series budget (*The series budget*).
- From this PR the validator's floor requires `alertmanager.yml`. Add its Go-template survival check
  and that check's `..._is_clean` / `..._is_flagged` pair: a fixture template with its
  `{% raw %}` block removed must be flagged.
- No rule exists yet, so nothing fires, and every receiver stays idle until PR 3.
- Two-writer check: no ownership moves. Kuma still owns every page. The new `alertmanager`
  job carries no `origin` label, so from this PR the `Cluster Scrape Targets` tile also pages
  a failed Alertmanager scrape. Kuma is that condition's one pager, and PR 3's rule leaves it
  alone.

### PR 3 — The rules pipeline and three meta rules (about 200 lines)

- Add `templates/prometheus-rules.yaml.j2`, `observability_rule_groups: [alerting-meta]`,
  `files/rules/alerting-meta.yaml` without the Watchdog, and its test file. The test file
  has three cases for `AlertmanagerMetricsMissing`:
    - the zero-series case fires it, through the first arm;
    - `up{job="alertmanager"}` at 1 with no `alertmanager_notifications_failed_total` series
      fires it, through the second arm;
    - `up{job="alertmanager"}` at 0 for 30 minutes, with no counter series, as on a failed
      scrape, fires nothing (`exp_alerts: []`). This case pins the rule off the
      `Cluster Scrape Targets` tile's condition.

  A YAML comment above `AlertmanagerMetricsMissing` in `alerting-meta.yaml` names the failed
  scrape's pager: the `Cluster Scrape Targets` tile until M6's batch A3, then A3's
  scrape-target rule with `service="observability"` for job `alertmanager`.
- In `prometheus.yaml.j2`, add `rule_files:`, the directory mount and `--config.auto-reload`.
  Prometheus rolls once more, for the args change.
- In the same file, rewrite the comment above the `kube-state-metrics` job. Rules exist after
  this PR, and none of M1's reads a kube-state-metrics series, so the 1m scrape stays safe.
  The comment states the condition later rules must meet: no `irate()` over these families,
  and every range window at least four scrape intervals (4m).
- Add the Python phase of `alert_rules.py` with its `..._is_clean` / `..._is_flagged` pairs.
  Exempt `prometheus-rules` in `_CONFIGMAP_DATA_KEYS` with the positive assertion, and add the
  `DECIDED rules reload in place` marker.
- Two-writer check: no check gains a second pager. No Kuma tile watches Prometheus's rule
  reloads or Alertmanager's sends. A failed Alertmanager scrape is the one overlap the rules
  could have made: `check_cluster_targets` pages it from PR 2 on. `AlertmanagerMetricsMissing`
  therefore fires only when the job is missing or when the scrape works and the counter is
  gone, and the test file's `up == 0` case holds that. From M6's batch A3 on, A3's
  scrape-target rule is that condition's one pager (*What M1 hands to M6's batch A3*).

### PR 4 — Watchdog to Healthchecks.io (about 120 lines)

- **Operator-only, before the merge:** create the `alertmanager-watchdog` check in the
  Healthchecks.io console, as a Simple check with a 10-minute period and a 20-minute grace and
  the same integrations as `uptime-kuma-alive`. Merge only after it exists. Otherwise the first
  ping returns 404, and `AlertmanagerNotificationsFailing` pages.
- Add the `Watchdog` rule and its test case. From this PR the validator's floor requires the
  `Watchdog`, and its uniqueness check has its subject.
- Add the row to `monitor_bridge_healthchecks_expected`, and add the four pieces after the
  variable in *Healthchecks.io sizing for the Watchdog*: the `deadman_crons` row and its
  seventh argument, `deadman_inputs` and `_deadman`'s sources list, the `every <N>s` branch
  of `cron_mismatches` with its pair, and the rewritten sentence in
  `docs/healthchecks-io-deadman.md`. The test's `WIRED_SLUGS` gains the slug.
- Each of the three `render_deadman_cadences` calls in
  `scripts/docs/tests/test_gen_doc_fragments.py` gains the seventh argument, and the first
  one's assertions gain the `alertmanager-watchdog` row with the cadence `every 60s`. The
  `regen-doc-fragments` hook rewrites the deadman fragments, so stage them and commit again.
- In `docs/healthchecks-io-deadman.md`, *Watching the spine itself*, replace "there is no
  Alertmanager, so nothing else pages". Kuma remains the only pager for its tiles until M2
  cuts a family over, and Alertmanager pages only its own meta alerts. Replace "all six checks
  above" with wording that carries no count, because the table grows with every slug.
- The comment in `ansible/roles/setup/k3s/templates/longhorn-backup-health.sh.j2` that says
  "there is no Alertmanager" stays as it is in M1. A `roles/setup/k3s` change ships only
  through a hand run of `k3s-bringup.yml`, which the GitOps deployer never runs
  (`deploy_changes.py:_BROAD_MANUAL_PREFIXES`). A comment-only edit would therefore leave
  owed hand-applied work for every other session's landing. M6's B1 rewrites the block that
  holds the comment when it retires `uptime-kuma-alive` and applies that script by hand.
- Deploys `observability` and then `monitor-bridge`. The bridge's
  `healthchecks_drift` check compares the console against the new list.
- Two-writer check: Healthchecks.io now watches two pingers, Kuma's backup-health script and
  Alertmanager. They watch different things on different slugs.

### PR 5 — One Secret, one restart (about 190 lines)

- In `defaults/main.yml`, give Prometheus, Grafana and Alertmanager's
  `observability_stabilise_workloads` entries `restart_secrets` and `rolled_by_role: true`, as
  *The Alertmanager workload* sets out.
- In `tasks/main.yml`, a `set_fact` after the `k8s/manifests` include computes
  `observability_changed_secret_files` once: each `manifests_secret_render` result's `changed`
  flag, paired by position with `manifests_secret_files_resolved`. Three tasks keyed on
  `manifests_secret_render is changed` at `70591c307` read it instead:
    - "Restart the telemetry workloads whose secret changed" runs for an entry whose
      `restart_secrets` meets the changed files. Register that task under a name of its own.
      After it, include `k8s/manifests`' `rollout_amend.yml` once per result that was not
      skipped. Filtering out the skipped results is what
      `ansible/tests/deploy/test_conditional_register_consumers.py` requires of a conditional
      register's consumer.
    - "Snapshot the telemetry workloads' restart counts for the end-of-play gate" keeps its
      config arm. Its secret arm becomes the same per-entry condition as the restart's, so a
      Secret edit soaks only the workloads it restarted. Its comment's "The secret arm stays
      on the render alone" sentence is rewritten for the per-file arm. A stale key that
      `verify_secret_keys` patches out still moves its own file's render, so that restart is
      still soaked.
    - "Sync the live Grafana admin password with SOPS" runs only when `grafana-secret.yaml` is
      among the changed files. That file is the only Secret template that renders
      `grafana_admin_password`. The task execs the Grafana CLI, which runs its own `Migrate`
      pass on the SQLite file the live server holds open, as the task's comment records. At
      `70591c307` it runs on any Secret change, and the comment calls that harmless because
      only a scrape-credentials rotation reached it. After PR 2, every edit to
      `alertmanager.yml` would reach it too. The comment's parenthesis about the
      scrape-credentials rotation goes.
- In `templates/alertmanager-secret.yaml.j2`, add a `#` comment line inside the
  `alertmanager.yml` value of `stringData`. It says that `restart_secrets` names this file, so
  an edit here restarts Alertmanager alone. Alertmanager's YAML parser skips the line, and
  amtool checks it with the rest of the file. The marker must sit inside the value, for two
  reasons:
    - `homelab_role_label` re-serialises the rendered Secret and drops a top-level YAML
      comment (*Baseline*). A top-level `#` line would move no staged bytes, so
      `manifests_secret_render` would not read `changed`, and the deploy would restart
      nothing.
    - A Jinja `{# #}` comment renders nothing.

  Inside the value, the line moves the staged bytes of `alertmanager-secret.yaml` and of no
  other Secret file, so PR 5's own deploy exercises the Alertmanager-only path. That deploy is
  exit criterion 4. It is also the shape of every later edit, because M2 to M6 edit that
  value.
- Test edits in the same PR:
    - In `ansible/tests/k8s/test_self_rollouts_follow_the_apply.py`, rewrite two tests for
      the stamp's `restart: false` and the amend:
      `test_observability_a_changed_secret_expects_only_its_secret_readers_to_roll`, under
      PR 2's name, and `test_observability_private_restart_reads_the_same_facts_as_the_record`.
      A pair joins them, rendered through the restart task's
      `when` with a fixture register: a change to `alertmanager-secret.yaml` alone selects
      Alertmanager and not Prometheus, and a change to `scrape-credentials-secret.yaml`
      selects Prometheus.
    - Two more pairs join it in that file, rendered the same way through the other two
      tasks' `when`. For the snapshot, a change to `alertmanager-secret.yaml` alone
      snapshots Alertmanager and no other workload. Its flagged half is a Prometheus or
      Grafana snapshot on that change. For the Grafana password sync, a change to
      `grafana-secret.yaml` runs it, and a change to `alertmanager-secret.yaml` alone skips it.
      A third assertion holds the file name the sync's `when` names to the one Secret
      template that renders `grafana_admin_password`.
    - In `ansible/tests/services/test_observability_config_rolls_one_workload.py`,
      `test_every_workload_reading_a_role_secret_declares_restart_on_secret` also requires each
      reader's `restart_secrets` to name exactly the Secret files whose Secrets its pod reads.
- Docs: the observability role's `CLAUDE.md`, *One config change, one restart*, replaces the
  one-register sentence that PR 2 leaves with the per-Secret rule. The rule covers the
  restart, the snapshot and the Grafana password sync, so an Alertmanager config edit restarts
  and soaks Alertmanager alone and never execs the Grafana CLI. The `defaults/main.yml`
  edit moves the same recorded fact atom as PR 2's, so PR 5 also runs `fact_status.py verify`
  on that section until G2 retires `docs/facts.lock`.
- PR 5 lands and deploys before M2's first edit to `alertmanager.yml`.
- Two-writer check: no ownership moves.

## Verification

Each command runs on daniel-box from the repo checkout.

```bash
# Prometheus found Alertmanager and loaded the rules
uv run python scripts/diagnostics/probe.py metric 'prometheus_notifications_alertmanagers_discovered'
uv run python scripts/diagnostics/probe.py metric 'prometheus_config_last_reload_successful'
uv run python scripts/diagnostics/probe.py metric 'prometheus_rule_group_rules'
# Alertmanager is scraped, and the Watchdog fires and is delivered
uv run python scripts/diagnostics/probe.py metric 'up{job="alertmanager"}'
uv run python scripts/diagnostics/probe.py metric 'ALERTS{alertname="Watchdog"}'
uv run python scripts/diagnostics/probe.py metric 'alertmanager_notifications_total{integration="webhook"}'
uv run python scripts/diagnostics/probe.py metric 'alertmanager_notifications_failed_total'
uv run python scripts/diagnostics/probe.py metric 'ALERTS{alertname="AlertmanagerMetricsMissing"}'
uv run python scripts/diagnostics/probe.py health observability
# Exit criterion 4: read before and after PR 5's deploy. Alertmanager's start time moves,
# Prometheus's and Grafana's do not
uv run python scripts/diagnostics/probe.py metric 'kube_pod_start_time{namespace="observability"}'
uv run python scripts/diagnostics/probe.py releases
# The series budget (C7), and how far back the store reaches, in days
uv run python scripts/diagnostics/probe.py metric 'prometheus_tsdb_head_series > 140000'
uv run python scripts/diagnostics/probe.py metric '(time() - prometheus_tsdb_lowest_timestamp_seconds) / 86400'
```

The `AlertmanagerMetricsMissing` read must return nothing: the scrape works, so the alert is
quiet. The series-budget read must return nothing too. After PR 5's deploy, `probe.py releases` shows the `observability` record's
`rollouts[]` with `restart: true` for Alertmanager alone.

**The scale-to-zero drill (operator-only, and it pages for real).** It runs after PR 4 deploys.
M2 depends on all of M1, so it runs before any family cuts over, while Kuma owns every page.

- **Precondition.** No deploy of `netpol-baseline` or `observability` may run during the
  drill. The operator stops the deployer's timer with
  `sudo systemctl stop gitops-deploy.timer`, then waits until
  `systemctl is-active gitops-deploy.service` reads `inactive`, because stopping a timer does
  not stop a tick already running. A `land.sh` run or a hand tick starts the service directly
  and bypasses the stopped timer, so the operator also announces the drill, and no session runs
  `deploy.sh`, `land.sh` or a tick meanwhile. The drill takes about 40 minutes, under
  GitOps-Alive's 90-minute bound (*Baseline*), so a timer left stopped still pages.
- **Steps.** Run
  `sudo k3s kubectl -n observability scale deployment/alertmanager --replicas=0`. The
  read-only ServiceAccount cannot scale, and Ansible is the only other write path. Within 30
  minutes Healthchecks.io sends its alert through its own integrations. Kuma's
  `Cluster Scrape Targets` tile pages first, 10 to 15 minutes in, as *Blast radius* explains.
  Before the restore, run
  `uv run python scripts/diagnostics/probe.py metric 'up{origin!="daniel-server"} == 0'`. It
  must return the `alertmanager` series alone. Any other series is a target loss that the
  DOWN tile masked, and the operator handles it as a page. Then run
  `./scripts/deploy.sh --tags observability`, which restores one replica declaratively. Check
  that the console clears and that
  `alertmanager_notifications_total{integration="webhook"}` rises again. Check that the
  `Cluster Scrape Targets` tile returns to up within one bridge cycle (5 minutes) of
  `up{job="alertmanager"}` reading 1. Then run `sudo systemctl start gitops-deploy.timer`.
- **Blast radius.**
    - Objects touched: the replica count of the `alertmanager` Deployment, and nothing else.
    - What keeps serving: Kuma owns every page, so no page is lost. Prometheus keeps
      evaluating, and its sends to Alertmanager fail until the restore. M1's three meta alerts
      cannot be delivered in the window.
    - The page the drill raises in Kuma: at 0 replicas `up{job="alertmanager"}` reads 0.
      `check_cluster_targets` holds that for `CLUSTER_TARGETS_CONSECUTIVE` (3) bridge cycles
      of 300 seconds. The `Cluster Scrape Targets` tile then goes DOWN and pages through
      Kuma's Discord and email, 10 to 15 minutes in. The operator should expect that page,
      and the drill's announcement names it. A page from any other tile is not the drill's.
      `check_k8s_workloads` excludes a Deployment scaled to 0 on purpose, so the
      `k3s Workload Health` tile stays up.
    - What the drill masks: while the tile is DOWN, a second cluster target that dies changes
      only the tile's message. Kuma pages on a change of state, so that loss raises no new
      page until the tile clears. The `probe.py metric` read before the restore covers that
      window. The plan does not pause the tile instead. A pause is a second operator step
      that must be lifted afterwards, and it would silence the masked case too.
    - The coupling: while Alertmanager has no ready endpoint, its probe row fails
      netpol-baseline's readiness gate (*Baseline*). A netpol-baseline deploy, by hand or by
      auto-deploy, or a full `deploy.yml` for a deploy-plane change, would fail, and the
      deployer would write `hold_sha`. The stopped timer prevents the scheduled ticks, and the
      announcement covers hand deploys, `land.sh` and hand ticks.
    - Any other session's `observability` deploy restores the replica and ends the drill
      early. The drill is then repeated.
- **Rollback.** `./scripts/deploy.sh --tags observability` restores the replica. If that
  deploy fails, `sudo k3s kubectl -n observability scale deployment/alertmanager --replicas=1`
  restores it by hand. If a hold was written anyway, the operator re-applies the held plane
  once Alertmanager is Ready, then runs `gitops_state.py clear-hold <sha>`. Every step here is
  **operator-only**.

**Red proofs for the new checks:**

- The validator: the `--prove-red` CI step, which includes a bad `prometheus.yml`, plus the
  `..._is_flagged` cases in the pytest suite. The cases feed:
    - an alert with no test, and a `severity` outside the set;
    - a `${` in a rule file, while a `${email}` in a rendered `alertmanager.yml` stays clean;
    - an empty rule directory;
    - a rule file that `observability_rule_groups` does not name, and a listed group with no
      file;
    - a second rule named `Watchdog`, and an alert named `AlwaysOn` with expression
      `vector(1)`;
    - a test file whose only empty-input case fires the Watchdog alone;
    - an Alertmanager template whose `{% raw %}` block is missing.
- Each rule: its promtool case with non-empty `exp_alerts`. To see one go red, change
  `PrometheusRuleReloadFailed`'s threshold locally and watch `promtool test rules` fail.
- `AlertmanagerMetricsMissing`'s scope: the case with `up{job="alertmanager"}` at 0 and empty
  `exp_alerts`. Dropping the second arm's `and on() (up{job="alertmanager"} == 1)` turns it
  red, and so does reverting the first arm to `absent(up{job="alertmanager"} == 1)`.
- The Watchdog as a watcher: the drill above.
- The `alertmanager` netpol probe row: the netpol-baseline probe Job's refused leg, which
  `netpol-baseline` deploys. The `policy` test's flagged half is a row without the key.
- PR 5's per-Secret restart: the pair in `test_self_rollouts_follow_the_apply.py`, whose
  flagged half is a Prometheus restart on an Alertmanager-only Secret change. The snapshot
  pair's flagged half is a Prometheus or Grafana snapshot on that change, and the password
  sync pair's is a sync run on it. PR 5's marker
  line sits inside the `alertmanager.yml` value, so its deploy moves the staged bytes, and
  exit criterion 4 reads the restart itself.
- `alert_tools.sh`: `test_alert_tools_guard.py`. Its flagged cases are a tarball whose digest
  the sums file lacks, which must exit non-zero with no stamp, and an unstamped cache, which
  must be fetched again.
- The `.cache` entry in `LOCAL_ONLY`: the `.cache/` fixture in
  `test_a_reincluded_or_local_only_root_entry_is_clean`, which goes red if the name is
  dropped.
- The Watchdog's deadman row: the `every <N>s` pair in `cron_mismatches`, whose flagged half
  is `every 600s` against `timeout: 600`.
- The series budget: the same `probe.py metric` read with a threshold below the live reading,
  such as `> 100000`, returns the `prometheus` series. If that read returns nothing, the
  metric name has moved, and the budget read proves nothing until it is fixed.

## Rollback

- **PR 1.** Revert the PR. Nothing is deployed. The revert also drops `.cache` from
  `LOCAL_ONLY`, so `validate-root-ignored-files` flags the cache in every worktree that
  provisioned it. Delete `.cache/` in each such worktree after the revert.
- **PR 2.** Revert and deploy `observability`. The prune removes the Deployment and Service.
  The Secret and the claim are never pruned (`manifests_prune_allowlist` excludes both), so
  `alertmanager-config` and `alertmanager-data` stay behind. Deleting them is a kubectl write
  (**operator-only**). Prometheus rolls back to a config with no `alerting:` block.
- **PR 3.** Revert and deploy `observability`. The `prometheus-rules` ConfigMap is pruned and
  Prometheus rolls without `--config.auto-reload`. The meta rules vanish, so nothing fires.
- **PR 4.** Revert and deploy `observability` and `monitor-bridge`. The Watchdog stops, and the
  console check goes down after 30 minutes. The operator pauses or deletes that console check
  (**operator-only**).
- **PR 5.** Revert and deploy `observability`. The restart task returns to the one-register
  signal, and the stamp decides `restart` for the three entries again. The revert removes the
  marker line from the `alertmanager.yml` value, which moves the staged bytes of
  `alertmanager-secret.yaml`. Under the old signal, one changed Secret file restarts every
  reader, so that deploy restarts Prometheus, Grafana and Alertmanager once. The snapshot and
  the Grafana password sync return to the same signal, so the deploy also soaks every
  workload and runs the Grafana CLI once, which re-sets the same password. Deploy the revert
  in a quiet window. A partial
  failure between the restart and the amend leaves the record at `restart: false`, which
  `rollout_amend.yml` names the safe direction: the gate keeps its rollout-complete and
  restart-count halves.
- **A partial failure.** If PR 2's deploy fails after the apply, Alertmanager may run with no
  rules feeding it, which pages nothing. If PR 3's rules load but PR 4 has not landed, the meta
  rules page through Discord and email, and nothing watches Alertmanager off-site yet. Kuma
  still owns every existing page throughout.

## Risks

Each risk names its class from `docs/failure-classes.md`.

- **Class 3, empty read as clean.** A rule whose series vanish evaluates to nothing and stays
  quiet. Mitigation: convention 2's zero-series case, which `alert_rules.py` enforces per test
  file; `AlertmanagerMetricsMissing` for M1's own family; and the Watchdog for "Prometheus
  evaluates nothing at all".
- **Class 4, a gate selector wider than the hazard.** The Watchdog always fires with
  `service: observability`, so a gate that counted it would read `observability` red forever.
  Mitigation: the gate excludes it by name (*Open questions* 5), and `alert_rules.py` holds the
  name to one rule, so the exclusion cannot hide a real alert.
- **Class 7, an offline render that passes what the deploy refuses.** `StubUndefined` turns a
  missing key into `STUB`, so a netpol probe row without `policy` renders green and fails
  `netpol-baseline`'s real deploy. Mitigation: the `policy` test PR 1 adds.
- **Class 8, prose that M1 makes false.** Three passages say no Alertmanager or rule exists,
  and a seventh workload falsifies every passage that counts the role's workloads, manifests,
  Secret readers or tier-1 templates (*Baseline*). Mitigation: PR 2 drops each count rather
  than raising it, before it runs `verify` on the one recorded section. PR 3 rewrites the
  `prometheus.yaml.j2` comment and PR 4 the deadman doc. The k3s script comment stays stale
  until M6's B1, for the reason PR 4 gives.
- **Class 2, the template's bytes as a proxy for the staged bytes.** `homelab_role_label`
  re-serialises an armed role's render, so a top-level YAML comment never reaches the staged
  file (*Baseline*). A marker placed there would leave PR 5's deploy restarting nothing.
  Mitigation: PR 5's marker sits inside the `alertmanager.yml` value, and exit criterion 4
  reads the restart from `kube_pod_start_time` and the release record, not from the diff.
- **Class 7, a provisioned cache trips a repo-wide hook.** `validate-root-ignored-files`
  always runs and flags an untracked root entry that only `/*` ignores. Mitigation: PR 1 adds
  `.cache` to `LOCAL_ONLY` in the same commit that first writes the cache, with a fixture that
  goes red if the name is dropped.
- **Class 5, the drill holds the deployer.** A netpol-baseline deploy fails while Alertmanager
  sits at 0 replicas. Mitigation: the drill's precondition stops `gitops-deploy.timer`, and its
  rollback names the `clear-hold` step.
- **Class 3, the drill masks a cluster target loss.** The drill turns the
  `Cluster Scrape Targets` tile DOWN for up to 30 minutes, so a second target that dies in the
  window raises no new Kuma page. Mitigation: the drill announces the tile's page, and the
  `up{origin!="daniel-server"} == 0` read before the restore must name `alertmanager` alone.
- **Class 4, a tile's selector reaches a job M1 adds.** `check_cluster_targets` selects every
  `up` series without an `origin` label, so PR 2's scrape job joins a Kuma tile with no Kuma
  edit. A rule paging `up{job="alertmanager"} == 0` would page that condition twice, against
  convention 5. Mitigation: `AlertmanagerMetricsMissing` fires only when the job is missing
  or the counter is gone, and the promtool `up == 0` case with empty `exp_alerts` holds it
  there. Convention 5 states the rule for every later slice's scrape jobs.
- **Class 4, the tile's successor narrows the set it replaces.** At M6's batch A3, a
  scrape-target rule that exempted `alertmanager`, or mapped it to `unmapped`, would leave a
  failed Alertmanager scrape with no pager or outside `observability`'s gate. The deleted
  tile covered every `up` series without an `origin` label. Mitigation: *What M1 hands to
  M6's batch A3* lists the obligations, PR 3's comment puts them at the rule, and *Open
  questions* 8 stays open until M6's A3 row names them. The request includes a promtool case
  in A3's test file: `up{job="alertmanager"}` at 0 fires A3's rule with
  `service: observability`.
- **A down Alertmanager pages later from M6's batch A3 on.** The tile pages it 10 to 15
  minutes in. Once the tile goes, only the Watchdog's Healthchecks.io check pages it, up to
  30 minutes in, because A3's own rule would notify through the Alertmanager that is down.
  Mitigation: A3 records that parity change in its cutover PR, as *What M1 hands to M6's
  batch A3* requests. *Open questions* 1's eviction tier bears on how often that case
  arises.
- **A broken `prometheus.yml` leaves no Prometheus.** The `Recreate` pod that rejects its
  config at startup does not come back. Mitigation: `promtool check config` in the hook from
  PR 1, with the inventory and the `scrape_jobs` filter in its `files` regex.
- **Class 2, a proxy for the property.** A Healthchecks.io ping proves that Alertmanager can
  send a webhook. It does not prove Discord or email delivery. Mitigation:
  `AlertmanagerNotificationsFailing`, which routes critical to both channels.
- **Class 5, a sequence treated as a transaction.** PR 4 needs the console check before the
  merge. Mitigation: the operator step is the PR's first checklist item, and the 404 it
  prevents is itself paged by `AlertmanagerNotificationsFailing`.
- **Class 7, the execution context.** The hook provisions binaries over the network on first
  use. Concurrent prek invocations race in a fresh worktree. Mitigation: the `flock` and stamp
  pattern from `vale.sh`, `test_alert_tools_guard.py`'s three cases, and a clear failure when
  the network is down rather than a skip.
- **A Prometheus roll in PR 2 and PR 3.** Each `Recreate` restart blinds every bridge check for
  a minute and may light Kuma's `Prometheus Reachable` tile. Mitigation: deploy in a quiet
  window. Afterwards, `--config.auto-reload` removes the restart from every later rule PR.
- **A weekly reboot longer than 30 minutes pages through Healthchecks.io.** That is the
  intended behaviour, but a slow Longhorn attach could trip it. Mitigation: the first Sunday
  after PR 4 is checked against the console's ping log.
- **An Alertmanager config edit restarts Prometheus and Grafana until PR 5 deploys.** The
  same edit soaks every workload and execs the Grafana CLI against Grafana's live database,
  because the snapshot and the password sync read the same one-register signal. PR 2's
  own deploy restarts Grafana once this way, because the Secret file is new. Prometheus is
  rolled by its config change in that PR, so the restart task skips it. Mitigation: no M1 PR
  after PR 2 edits `alertmanager-secret.yaml`, and PR 5 deploys before M2's first edit. If
  PR 5 slips, every M2 to M6 PR that edits the Alertmanager config states the cost and
  deploys in a quiet window.
- **Class 3: PR 5 selects no Secret file and restarts nothing.** That leaves a pod on stale
  config behind a green deploy. Mitigation: the position pairing never reads a `no_log` result's
  `item`; the pair in `test_self_rollouts_follow_the_apply.py`; and PR 5's own deploy, which
  moves the staged bytes of the Alertmanager Secret alone and is exit criterion 4.
- **A Secret rotation restarts the workload that reads it.** Mitigation: Alertmanager's
  silences and notification log persist on `alertmanager-data`.

## Tests and docs that change in the same PR

- **PR 1.** Adds `scripts/validate/tests/test_validate_alert_rules.py`, the fixtures and
  `scripts/validate/tests/test_alert_tools_guard.py`.
  `ansible/tests/k8s/test_netpol_probe_targets.py` gains the `policy` test. The fixture of
  `test_a_reincluded_or_local_only_root_entry_is_clean` gains `.cache/`, beside the new
  `LOCAL_ONLY` name and its reason comment. `docs/claude-tooling.md` gains a line naming the
  hook.
- **PR 2.**
    - Tests, in `ansible/tests/services/test_observability_config_rolls_one_workload.py`:
      `_EXPECTED_WORKLOADS` and `_EXPECTED_SECRET_READERS` gain `alertmanager`, and the
      checksum census's superset assertion exempts it. The wait loop in `tasks/main.yml`
      gains `alertmanager`, which keeps
      `ansible/tests/services/test_observability_stabilise_coverage.py` green. The
      `alertmanager` row joins `netpol_baseline_probe_targets`, under PR 1's `policy` test.
      `ansible/tests/k8s/test_pod_template_hygiene.py` passes unchanged, because the tier is
      one of its four classes. The validator gains the Go-template survival pair.
      In `ansible/tests/k8s/test_self_rollouts_follow_the_apply.py`, the two-readers test is
      renamed for its secret readers and its expected set gains `alertmanager`. The count
      words in the docstrings and failure messages of that file and of
      `test_observability_stabilise_coverage.py` and
      `test_observability_config_rolls_one_workload.py` go (*Baseline*).
    - Docs: the observability role's `CLAUDE.md` gains an *Alert plane* paragraph and the
      amended `DECIDED observability tiers` marker. Beside the marker, the five tier-4
      comments in `collector`, `grafana`, `kube-state-metrics`, `loki` and `tempo`
      `.yaml.j2` stop calling `prometheus.yaml.j2` "the one exception", and
      `alertmanager.yaml.j2` carries its own tier-1 comment. The `regen-role-glance` hook
      rewrites the *At a glance* block for the new image, the new claim and the count-free
      `k8s_autodeploy_reason`. Its *One config change, one restart* section names Alertmanager
      beside Prometheus and Grafana as a `restart_on: [secret]` reader. It says the one
      register covers four Secret manifests, not three, so a rotation of Grafana's admin
      password restarts Prometheus and Alertmanager too. The same section loses "these six".
      The comment above `observability_stabilise_workloads` in `defaults/main.yml` gets the
      same correction. The comments in `tasks/main.yml` and the netpol-baseline observability
      policy lose their counts. PR 2 then re-verifies that section, as the `docs/facts.lock`
      item below says.
- **PR 3.**
    - Tests: the promtool test file with its zero-series case, the validator's Python-phase
      pairs (the rule-list agreement and zero-series checks among them), and the
      `_CONFIGMAP_DATA_KEYS` exemption with its positive assertion.
    - Docs: the observability role's `CLAUDE.md` *One config change, one restart* section
      gains the reload exception and its marker. The `kube-state-metrics` comment in
      `prometheus.yaml.j2` states the scrape condition rules must meet.
- **PR 4.**
    - Tests: `WIRED_SLUGS` and the `every <N>s` branch of `cron_mismatches`, with its pair, in
      `ansible/tests/services/test_monitor_bridge_healthchecks_expected.py`. The three
      `render_deadman_cadences` calls in `scripts/docs/tests/test_gen_doc_fragments.py` gain
      the seventh argument, and the first one asserts the `alertmanager-watchdog` row. The
      validator's Watchdog uniqueness and constant-expression pairs.
    - Docs: `docs/healthchecks-io-deadman.md` gains three things: its table row, its *What
      leaves the house* row, and a *Watching the spine itself* paragraph naming the Watchdog
      as Alertmanager's watcher. The same section loses "there is no Alertmanager" and its
      "six checks" count. The sentence that maps every non-`*/10` slug to a Cron check
      gains the `every <N>s` case. The deadman fragments regenerate, and the cadences
      fragment's `generated_from` header names the observability defaults.
- **PR 5.**
    - Tests: the two rewritten tests (the secret-readers test under PR 2's name), the
      per-Secret pair, the snapshot pair and the Grafana password sync pair in
      `ansible/tests/k8s/test_self_rollouts_follow_the_apply.py`, and the `restart_secrets`
      census in `test_every_workload_reading_a_role_secret_declares_restart_on_secret`.
    - Docs: the observability role's `CLAUDE.md`, *One config change, one restart*. In
      `tasks/main.yml`, the comments above the snapshot and the Grafana password sync describe
      the per-file condition.
- **`docs/facts.lock`, until G2 retires it.**
    - Until G2 retires `docs/facts.lock`, PR 2 and PR 5 each run
      `uv run python scripts/dev/fact_status.py verify 'ansible/roles/k8s/observability/CLAUDE.md#One config change, one restart'`
      and commit the lock. That section cites
      `ansible/roles/k8s/observability/defaults/main.yml:observability_stabilise_workloads`
      as a YAML atom, and a YAML atom hashes the key's whole value
      (`scripts/lib/facts/atoms.py:atom_content`). PR 2 appends the `alertmanager` entry, and
      PR 5 adds two keys to three entries, so the atom reads as `moved` after each PR. The
      `facts-reverify-changed` hook refuses to fold a moved atom, because `moved` is not in
      `scripts/lib/facts/lock.py:BENIGN_KINDS`.
    - Until G2 retires `docs/facts.lock`, M1's other `CLAUDE.md` edits move no recorded atom.
      At most they add or drop a citation, as PR 2's *Alert plane* paragraph or PR 3's reload
      marker may. The `facts-reverify-changed` hook re-hashes such a section and exits 1 like
      a formatter, so stage `docs/facts.lock` and commit again.
    - Once G2's PR 3 has merged, neither step runs. That PR deletes the lock and the hook, and
      G2's `lock-retired` rule rejects a recreated lock. The G1 notice on the PR then names any
      section whose cited atom moved, and the PR edits that section if its prose is stale.

## Complexity delta

- **Added:** one Deployment, one claim, one Secret and one ConfigMap in an existing role. Four
  rules and their tests. A validator of about 200 lines with its test and fixtures, and a
  provisioning script of about 60 lines with a guard test of three cases. Two CI steps. One
  name in `LOCAL_ONLY`. One list variable, `observability_rule_groups`, one integer
  variable, `observability_alertmanager_watchdog_repeat_seconds`, and two keys on three
  stabilise entries, `restart_secrets` and `rolled_by_role`. One fact,
  `observability_changed_secret_files`, read by three tasks. A seventh argument to
  `deadman_crons` and one branch in `cron_mismatches`. One test for the netpol probe table.
  A series budget of 140,000 head series with two reads in *Verification*. M1's own series
  delta is a few hundred, from the `alertmanager` scrape job.
- **Removed:** nothing yet. M1 is the foundation the removals in M2 to M6 stand on. PR 5
  removes a coupling: a Secret rotation restarts only the workloads that read that Secret. At
  `70591c307`, a rotation of Grafana's admin password also restarts Prometheus, and any Secret
  change execs the Grafana CLI and soaks every workload. PR 2 removes
  the workload counts from the role's comments and tests, so the next workload edits none of
  them.
- **Not added:** no new role, no new namespace, no new route and no new secret. Every
  credential Alertmanager reads already exists in `secrets.yml`.

## Open questions

1. **Alertmanager's eviction tier.** The proposal is `homelab-critical`. Until M6's batch A3,
   Kuma's `Cluster Scrape Targets` tile pages an evicted Alertmanager 10 to 15 minutes in.
   From A3 on, only the Watchdog pages it, after up to 30 minutes. Resolution: the
   operator confirms or rejects the amended `DECIDED observability tiers` marker in PR 2
   (**operator-only**, since it reverses a recorded decision).
2. **Silences before a route exists.** M1 adds no route, so until one exists a silence is an
   `amtool silence add` run inside the pod
   (`sudo k3s kubectl -n observability exec deploy/alertmanager -- amtool ...`,
   **operator-only**). Resolution: M6 decides on a LAN route behind Authelia, when Kuma's UI
   (the pause button it replaces) goes. If the operator needs silences sooner, M2's cutover PR
   carries the route instead.
3. **Home Assistant mobile receiver.** design.md's end state lists it. Kuma has no such channel
   today, so parity does not need it. Resolution: deferred to after M6, as its own PR.
4. **Contract gap, C7.** C7 pins neither where rule files live nor how they are tested, and D5
   adds its soak rule to the same directory. Resolution: adopt conventions 1 and 2 into C7.
5. **Contract gap, C7: two alerts the health gate must not count.**
    - **Shadow alerts.** The `shadow` label sits outside C7's `service` and `severity`, and a
      shadow alert pages nobody.
    - **The Watchdog.** It always fires and carries `service: observability`, a deploy tag
      as C7 requires. A gate that counted it would read `observability` red forever. Relabelling
      it is no escape, because C7 forbids a `service` outside the deploy tags and host names.

   Resolution: C7 gains `shadow`, and the C7 gate reads
   `ALERTS{alertstate="firing",service="<tag>",shadow!="true",alertname!="Watchdog"}`. M6's
   `probe.py health` uses that query. The exclusion is by name, not by `severity="info"`: a
   severity exclusion would also exempt every future `info` alert from every gate, while the
   name reaches one rule that `alert_rules.py` pins (convention 3). D5's soak needs no change,
   because it counts only critical alerts, and the Watchdog is `info`. Its age filter alone
   would not exclude the Watchdog: the rule has no `for`, so a Prometheus restart during a soak
   makes it active again. If D5 widens past critical, it adds `alertname!="Watchdog"`. The
   other three `alerting-meta` alerts are meant to fail `observability`'s gate, as *The rules
   M1 ships* explains.
6. **Contract gap, C7.** C7 says the Watchdog routes to the Healthchecks.io ping, but it pins no
   slug, period or grace. Resolution: C7 adopts `alertmanager-watchdog`, a 600s period and a
   1200s grace.
7. **Contract interaction, C1, C3 and R2: Flux substitution and PromQL's `$1`.** R2's
   *Baseline* reads Flux's scanner at source: a reference opens only on `${`, and a bare
   `$name` is never substituted (`envsubst/parse/scan.go`, function `scanLbrack`). R2's
   escape pass doubles only a `$` that is followed by `$`, by `{` or by a sentinel. A bare
   `$1` or `$labels` therefore survives both passes unchanged. Resolution: settled on that
   evidence. R2's CI secret check still adds a fixture holding `label_replace(..., "$1", ...)`
   to confirm it for the pinned CLI.
8. **Who pages `up{job="alertmanager"} == 0` from M6's batch A3 on.** M1 leaves that
   condition to Kuma's `Cluster Scrape Targets` tile, and A3 deletes the tile. At the
   integration-2 review (2026-10-11), M6's A3 row named no successor for this job. Resolution: M6's A3 row adopts the four
   obligations in *What M1 hands to M6's batch A3*. The integration-2 cross-slice note
   carries them to M6. Until M6 records them, M1's statements about A3 are a request, not
   a fact.
9. **Does the 7 GB size cap already bind?** The 2026-10-12 reading's 27.3 days is under the
   30-day setting. If the store is older than 30 days, the size cap binds at that count, and
   the 140,000 budget sits above what keeps 30 days. Resolution: the retention read in
   *Verification* runs once a day for seven days from PR 1's merge, and PR 5's body quotes
   the seven values. A value that climbs past 30
   means the time cap binds, and the budget stands. A value that never passes 30 means the
   size cap binds. Then the operator chooses among a lower budget with a cardinality cut, a
   larger claim with the cap at the same ratio, and a shorter retention (**operator-only**).
   Until that ruling, the budget stays 140,000.
10. **A down Alertmanager pages 30 minutes in.** Kuma paged it 10 to 15 minutes in, and the
    Watchdog check's 600s period and 1200s grace page it up to 30 minutes in (RA-8).
    Recommendation: accept the 30 minutes, because a shorter check would page on most weekly
    reboots (*Healthchecks.io sizing for the Watchdog*). Resolution: the operator rules
    before PR 4 merges (**operator-only**). To shorten it instead, read the reboot gap from
    the 2026-08-30 record and every later reboot record. Shorten the check only when the
    worst gap plus a margin fits under 30 minutes. *Open questions* 6 and exit criterion 2
    stand until then.

## Review log

### Round 1 (per-slice adversarial review, 2026-10-11)

Ten findings: one high and nine medium. All ten are fixed. One claim inside M1-R1-01 is
refuted. Each finding was checked against the tree at `70591c307` before the fix.

- **M1-R1-01, high.** The always-firing Watchdog carries `service=observability`, so the C7
  health gate and D5's promote soak read it as a permanent failure. **Fixed** in *Open
  questions* 5, convention 3, *The rules M1 ships* and the validator: the gate excludes
  `alertname="Watchdog"`, `alert_rules.py` pins that name to one rule and refuses any other
  constant expression, and the plan states that `AlertmanagerNotificationsFailing` is meant to
  fail `observability`'s gate, with its 30-minute cost. **Refuted in part:** D5's soak is not
  affected. [D5](../deploy/d5-bulk-move.md), *The Job*, counts only "critical alerts on a
  flux-owned service that became active after the Job started", and the Watchdog is `info`.
  M6 Phase B's query is the one that would break, and the cross-slice notes carry the fix to
  M6 and C7.
- **M1-R1-02, medium.** Every later Alertmanager config edit restarts the `Recreate`
  Prometheus and Grafana through the shared secret-restart signal. **Fixed** by PR 5, which
  narrows the restart to the Secret each workload reads. Confirmed in
  `ansible/roles/k8s/observability/tasks/main.yml` ("Restart the telemetry workloads whose
  secret changed") and `release_stamp.yml`. The design pairs results by position because the
  render task is `no_log`, and uses `rolled_by_role` with `rollout_amend.yml` so the shared
  role stays unchanged.
- **M1-R1-03, medium.** The netpol probe row has no `policy` key, which the refused leg
  dereferences. **Fixed**: the row carries
  `policy: networkpolicy-observability.yaml.j2`, and PR 1 adds a test that every refused row
  carries `policy`. Confirmed in `netpol-probe-job.yaml.j2`, which prints `{{ t.policy }}`.
- **M1-R1-04, medium.** PR 2's test claims were wrong, and `tasks/main.yml` was missing from
  the Design. **Fixed**: the Design lists `tasks/main.yml`, and PR 2 adds Alertmanager to the
  literal wait loop and exempts it in the checksum census's superset assertion. Confirmed
  that `_pairs` compares as a set, so the loop position is free.
- **M1-R1-05, medium.** The plan never said how the rules ConfigMap lists its files.
  **Fixed** in convention 1: `observability_rule_groups` and an agreement check with its
  pair. Confirmed that `scripts/lib/k8s_yaml.py:make_lookup` raises on any lookup but four.
- **M1-R1-06, medium.** M1's own family had no zero-series case, and the validator did not
  enforce one. **Fixed**: `AlertmanagerMetricsMissing` joins `alerting-meta`, and
  `alert_rules.py` requires an empty-input case that fires an alert other than the Watchdog.
  Upstream `notify/metrics.go`, function `InitializeFor`, at v0.34.1 makes the `absent()`
  reading sound.
- **M1-R1-07, medium.** Convention 7's `${` refusal conflicted with C1's placeholders.
  **Fixed**: the refusal covers source files only, and the rendered `alertmanager.yml` carries
  `${<key>}` by design. The `stringData` count (38 to 0) backs the R2 note. *Open questions* 7
  is settled from R2's own reading of Flux's scanner.
- **M1-R1-08, medium.** Nothing validated the `prometheus.yml` edits. **Fixed**:
  `promtool check config --syntax-only` joins the tool phase from PR 1, with a bad-config
  fixture, and the hook's `files` regex covers the template, the inventory and the
  `scrape_jobs` filter. A render at `70591c307` showed no `STUB` in `prometheus.yml`.
- **M1-R1-09, medium.** The pod's user and `fsGroup` were not specified. **Fixed** in *The
  Alertmanager workload*, copying Prometheus's shell. Confirmed `USER nobody` and
  `VOLUME [ "/alertmanager" ]` in the upstream `Dockerfile` at v0.34.1.
- **M1-R1-10, medium.** The baseline grep claim was wrong and hid prose that M1 makes false.
  **Fixed**: the *Baseline* lists the four files the grep returns and the third passage it
  misses. PR 3 rewrites the `kube-state-metrics` comment, and PR 4 the deadman doc. The k3s
  script comment moves to M6's B1, because a `roles/setup/k3s` edit ships only by a hand
  `k3s-bringup.yml` run.

Also changed this round, outside the findings:
- The lexical and zero-series checks cover PromQL only, so M5's LogQL rules pass them.
- PR 5's own deploy proves the per-Secret restart, as exit criterion 4.
- The netpol `policy` test moves to PR 1.
- PR 2's estimate rises to about 250 lines, with the reason stated.

### Round 2 (integration review, 2026-10-11)

One finding, medium. It is fixed. One claim in it is refuted in part, and fixing it exposed a
second gap in the same step. Each claim was checked against the tree at `70591c307`.

- **X-21, medium.** Many slices commit `docs/facts.lock` unconditionally, although G2 deletes
  the lock and the `facts-reverify-changed` hook. **Fixed** in *Tests and docs that change in
  the same PR*, and in PR 2's and PR 5's *Steps*. Every lock step carries R4's condition,
  "Until G2 retires `docs/facts.lock`". The item also says that neither step runs once G2's
  PR 3 has merged.
    - **Refuted in part.** M1 never ran `fact_status.py verify`. Its only lock step committed
      what the `facts-reverify-changed` hook writes. The step was still unconditional, so the
      fix stands.
    - **A second gap in the same step.** The hook alone cannot keep the lock in step for PR 2
      and PR 5. `ansible/roles/k8s/observability/CLAUDE.md#One config change, one restart`
      records the YAML atom
      `ansible/roles/k8s/observability/defaults/main.yml:observability_stabilise_workloads`.
      `scripts/lib/facts/atoms.py:atom_content` hashes a YAML atom as the key's whole value,
      and PR 2 and PR 5 both change that value. `moved` is not in
      `scripts/lib/facts/lock.py:BENIGN_KINDS`, and the docstring of
      `scripts/lib/facts/lock.py:reverify_benign` says a moved atom "stays a person's
      `verify`". Both PRs therefore run `fact_status.py verify` on that section. A grep of
      `docs/facts.lock` found no other content atom that M1 changes. The other atoms M1 touches
      are path atoms, which hash existence, and M1 deletes none of their files.

Also changed this round, outside the findings:
- After PR 2, the section that PR 2 re-verifies would have been false. It says only Prometheus
  and Grafana declare `restart_on: [secret]`, and that one register covers three Secret
  manifests. With Alertmanager as a third reader, a Grafana password rotation also restarts
  Alertmanager. PR 2 corrects all three claims, and the same comment in `defaults/main.yml`,
  before it runs `verify`. PR 2's and PR 5's *Steps* now name the `verify` too, so each PR's
  scope matches *Tests and docs that change in the same PR*.

### Round 10 (per-slice adversarial review, 2026-10-11)

Six findings: two high and four medium. All six are fixed, and none is refuted. Each claim
was checked against the tree at `70591c307` before the fix.

- **M1-R10-01, high. PR 5's rendered YAML comment is stripped before staging, so exit
  criterion 4 cannot be reached.** Confirmed. `manifests_prune` is `true`, observability does
  not override it, and `_UNARMED_ROLES` is `{authelia, nut}`. "Render secret manifests"
  therefore takes `role-labelled.yaml.j2` as its `src`, and `homelab_role_label` runs
  `yaml.load_all` and then `yaml.dump_all`. A run of that function on a Secret printed it
  without its top-level `# restart_secrets names this file` line. The same line inside the
  `alertmanager.yml` block-scalar value survived, and so did a metadata annotation. **Fixed**:
    - *Baseline* states the re-serialisation rule for armed roles.
    - PR 5's marker is a `#` line inside the `alertmanager.yml` value. That is the edit shape
      M2 to M6 make.
    - Exit criterion 4, the Class 3 risk and the PR 5 rollback now speak of staged bytes.
      The rollback sentence still holds: the revert removes the line from the value, which
      moves that file's staged bytes, and the old one-register signal restarts all three
      readers once.
    - A new Class 2 risk records the trap.
- **M1-R10-02, high. Provisioning into `.cache/alert-tools/` trips the always-run
  `validate-root-ignored-files` hook.** Confirmed. `git check-ignore -v --no-index -- .cache`
  prints `.gitignore:2:/*`, `LOCAL_ONLY` has no `.cache`, the hook sets `always_run = true`,
  and CI's hooks job skips only `ansible-lint` and `static-ratchet-tests`. **Fixed** with the
  first option:
    - PR 1 adds `.cache` to `scripts/validate/root_ignored_files.py:LOCAL_ONLY` with a reason
      comment, and the clean fixture of `test_a_reincluded_or_local_only_root_entry_is_clean`
      gains `.cache/`.
    - *Baseline* and *CI and local tooling* name both readers of an ignored root path.
    - The *Design* diff lists the file.
    - The PR 1 rollback now deletes `.cache/` in each provisioned worktree, because the
      revert drops the name again.

  M2's pint and M5's lokitool provision under the same `.cache/alert-tools/`, so the one name
  covers them.
- **M1-R10-03, medium. Prose and comments that M1 makes false are not listed.** Confirmed by
  `git grep -n -w -E 'six|seven|eight|two workloads'` over the role, its three tests and
  netpol-baseline. **Fixed**: *Baseline* lists the counting passages. PR 2 drops each count
  rather than raising it, and fixes the section's "these six" before its `verify`. The five
  tier-4 comments stop calling Prometheus "the one exception". Two passages stay: the "seven"
  workloads in `defaults/main.yml` and `prometheus.yaml.j2` are the cluster-wide set that
  `kube-state-metrics.yaml.j2`'s header names from the migration's slice 3, not this role's.
- **M1-R10-04, medium. The drill omits that every netpol-baseline deploy fails while
  Alertmanager has no ready endpoint.** Confirmed in netpol-baseline's "Fail if any probe
  target has no ready endpoint" and
  `test_terraria_is_absent_while_it_is_scaled_to_zero`. The suggested "hold the operator sets"
  does not exist: `gitops_state.py` offers only `clear-*` subcommands. **Fixed**: the drill
  states its precondition, its blast radius and its rollback. The precondition stops
  `gitops-deploy.timer` for about 40 minutes, under GitOps-Alive's 90-minute
  `GITOPS_MAX_AGE_MIN`. The blast radius covers the objects touched, what keeps serving, the
  coupling, and an early end from another `observability` deploy. The rollback includes
  `clear-hold`, all **operator-only**. A Class 5 risk joins *Risks*.
- **M1-R10-05, medium. PR 4's Watchdog deadman wiring is underspecified.** Confirmed:
  `_deadman` carries its own sources list, `deadman_crons` returns triples from six
  positional arguments, and `cron_mismatches` accepts only `*/N`. **Fixed** in
  *Healthchecks.io sizing for the Watchdog*:
    - the variable holds integer seconds;
    - the cadence field takes the exact form `every <N>s`;
    - the seventh argument has no default value;
    - `_deadman`'s sources gain the observability defaults;
    - the new branch accepts N below `timeout` and flags N at or above it, with its pair;
    - the deadman doc's Simple-or-Cron sentence gains the case;
    - the three `render_deadman_cadences` calls in `test_gen_doc_fragments.py` take the new
      argument.

  No other code reads the cadence strings. The deadman-graces fragment,
  `scripts/docs/fragments_lib/fragments_storage.py:render_deadman_graces`, reads `kind` and
  `grace` from `monitor_bridge_healthchecks_expected`, which the new row already fits.
- **M1-R10-06, medium. `alert_tools.sh` has no red proof.** Confirmed: only `alert_rules.py`
  had one. **Fixed**: PR 1 adds `scripts/validate/tests/test_alert_tools_guard.py` beside
  `test_vale_sync_guard.py`, with three cases: a bad digest exits non-zero with no stamp, an
  unstamped cache is fetched again, and a stamped cache is reused. It fakes `curl` itself,
  because `ansible/tests/leakguard.py:SHIMMED_BINARIES` names `curl`. PR 1's estimate rises
  to about 270 lines, with the reason stated. The test's name is free:
  `scripts/validate/tests/test_every_validator_has_a_red_proof.py` globs only `*.py` under
  `scripts/validate/`, so the shell script is outside its census.

Also changed this round, outside the findings:
- PR 2 would have turned CI red.
  `test_observability_a_changed_secret_expects_only_its_two_readers_to_roll` asserts exactly
  `{"prometheus", "grafana"}`, and it reads the real defaults through
  `ansible/tests/_release_expectation.py:observability_self_rollouts`. PR 2 adds
  `alertmanager` with `restart_on: [secret]`, yet the plan touched that test only in PR 5.
  PR 2 now renames the test for its secret readers and adds `alertmanager` to its expected
  set. No doc cites the old name.
- PR 2's estimate rises to about 290 lines, for the count edits that must land with the
  workload.

### Round c1 (per-slice adversarial review, 2026-10-11)

Three findings: one high and two medium. All three are fixed, and none is refuted. Each claim
was checked against the tree at `70591c307` before the fix.

- **M1-C1-01, high. `AlertmanagerMetricsMissing` double-pages a condition that Kuma's
  `Cluster Scrape Targets` tile already pages, and PR 3's two-writer check said otherwise.**
  Confirmed. `ansible/roles/k8s/monitor-bridge/files/checks/cluster.py:check_cluster_targets`
  selects `up{origin!="daniel-server"}`, and its docstring says that every `up` series belongs
  to exactly one of the two target checks. `ansible/roles/k8s/monitor-bridge/files/verdicts/cluster.py:targets_verdict`
  names each series at 0 as down. The `cluster_targets` row in
  `ansible/roles/k8s/monitor-bridge/files/check_table.py:CHECKS` is a Kuma push tile, and
  `CLUSTER_TARGETS_CONSECUTIVE` is `"3"` in monitor-bridge's `templates/env-secret.yaml.j2`.
  PR 2's scrape job carries no `origin` label. **Fixed** with
  option (a), keeping a narrowed form of the second arm:
    - The first arm is `absent(up{job="alertmanager"})`, which fires only when the job is
      missing. Convention 2's empty-input case still fires it.
    - The second arm is conjoined with `up{job="alertmanager"} == 1`, so it fires only when
      the scrape works and the counter is gone, as after a metric rename. No tile reads
      Alertmanager's own metrics. Dropping the arm outright would have left that case quiet,
      which is failure class 3.
    - PR 3's test file gains an `up == 0` case with empty `exp_alerts`, which pins the rule
      off the tile's condition. *Verification* lists it as a red proof.
    - PR 3's two-writer line, PR 2's two-writer line, *Prometheus side*, *The rules M1 ships*
      and its health-gate paragraph are corrected. Convention 5 states the rule for any scrape
      job without an `origin` label until M6's batch A3 cuts the tile over. A Class 4 risk
      joins *Risks*.
- **M1-C1-02, medium. The drill's blast radius omits the Kuma page it raises and the masking
  window after it.** Confirmed from the same check, its 3-cycle streak and monitor-bridge's
  300-second `INTERVAL`. Also checked:
  `ansible/roles/k8s/monitor-bridge/files/checks/cluster_zero.py:zero_available_offenders`
  excludes a Deployment scaled to 0 on purpose, so `k3s Workload Health` raises no page.
  **Fixed**: *Blast radius* names the `Cluster Scrape Targets` page at 10 to 15 minutes and
  the masked window. *Steps* adds a `probe.py metric 'up{origin!="daniel-server"} == 0'` read
  before the restore, which must name `alertmanager` alone, and a check that the tile returns
  to up afterwards. The plan does not pause the tile and says why. A Class 3 risk joins
  *Risks*.
- **M1-C1-03, medium. PR 5 narrowed only the restart. Two other consumers of the one-register
  Secret signal still fired on every Alertmanager config edit.** Confirmed in
  `ansible/roles/k8s/observability/tasks/main.yml`. "Sync the live Grafana admin password
  with SOPS" has `when: manifests_secret_render is changed and not k8s_no_mutate`. "Snapshot
  the telemetry workloads' restart counts for the end-of-play gate" ends its `when` with
  `or manifests_secret_render is changed`. `grafana-secret.yaml.j2` is the only Secret
  template in the role that renders `grafana_admin_password`. **Fixed**:
    - PR 5 computes `observability_changed_secret_files` once, and all three tasks read it.
    - The snapshot's secret arm takes the restart's per-entry condition.
    - The password sync runs only when `grafana-secret.yaml` changed.
    - Two pairs join `test_self_rollouts_follow_the_apply.py`.
    - The design bullet, PR 5's docs line, *Rollback*, *Risks* and *Complexity delta* are
      updated. PR 5's estimate rises to about 190 lines, for the two gates and their pairs.

### Round integration-2 (integration review, 2026-10-11)

One finding, medium. M1's share is fixed, the rest is carried to M6 as a cross-slice note,
and nothing is refuted. Each claim was checked against the tree at `70591c307` and against
the working copy of `m6-retire-kuma.md`.

- **X2-10, medium. M6 batch A3 owns none of the shadow drops and routing changes that M4,
  M5 and M1 hand to it.** Confirmed for M1. A grep of `m6-retire-kuma.md` finds
  `AlertmanagerMetricsMissing` 0 times. Its one `job="alertmanager"` hit is an `up == 1`
  read outside Phase A, not a pager. The A3 row reads only "`CLUSTER_TARGETS_CONSECUTIVE`
  becomes `for: 10m`". M1 still
  said "A3's rule pages a failed scrape instead" without naming what that rule must cover.
  Two more gaps sat behind that sentence:
    - **The `service` mapping could not reach the job.** Convention 3 derived a many-service
      rule's `service` from M2's workload-service join. The `alertmanager` job is a static
      target, and the `prometheus` job's static target in
      `ansible/roles/k8s/observability/templates/prometheus.yaml.j2` shows that such an `up`
      series carries only `job` and `instance`. M1's own convention would therefore have
      mapped the job to `unmapped`, outside `observability`'s gate.
    - **The tile pages a down Alertmanager, and its successor cannot.** A3's rule would
      notify through the Alertmanager that is down. Open question 1 said the Watchdog was
      the only pager for that case, which is untrue until A3 and true after it.

  **Fixed in M1:**
    - Convention 3 adds the literal job-to-service table for static scrape jobs, with M1's
      row `alertmanager` → `observability`.
    - Convention 5 says that A3's cutover gives every job without an `origin` label its
      `up == 0` pager, with no exemption and no `shadow`.
    - *The rules M1 ships* gains *What M1 hands to M6's batch A3*, with four obligations: cover
      job `alertmanager`, map it to `observability`, land unshadowed, and record the slower
      page for a down Alertmanager. The health-gate paragraph makes the gate claim
      conditional on the mapping.
    - PR 3 adds a YAML comment above `AlertmanagerMetricsMissing` that names the failed
      scrape's pager before and after A3. PR 3's two-writer line names A3's rule as the pager
      from A3 on.
    - *Risks* gains a Class 4 risk for a successor rule that narrows the tile's set, and a
      risk for the slower page. Open question 1 states both detection times. Open question 8
      tracks the request until M6's A3 row adopts it.

  **Carried to M6:** the four obligations, plus a promtool case in A3's test file in which
  `up{job="alertmanager"}` at 0 fires A3's rule with `service: observability`. The M4 and M5
  tasks in X2-10 belong to those slices' own revisions and are not repeated here.
  *Verification*, *Rollback*, *Tests and docs that change in the same PR* and *Complexity
  delta* need no change. PR 3's comment adds no test, no rollback step and nothing to count.

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review round covers it.

- **F-02 (C7, the rules directory).** *Design* gains *Rules other slices add (C7)*: D1's
  `flux` group from PR D1.6b, D2.6d's deploy-pipeline rules with
  `DeployPipelineExporterAbsent` and its 24-hour `DeployHoldActive` route, and H3's two host
  rules. D2's `service="deploy-pipeline"` is the one literal that C7 *Rule labels* admits.
  The section binds all three to conventions 1 to 9 and names
  convention 5's constraint before M6's A3. PR 2 notes that D1.6 edits
  `netpol_baseline_probe_targets` too, and which form the second PR rebases onto.
- **P9 (C7, the series budget).** *Design* gains *The series budget (C7)*: 140,000 head
  series, derived from the review's reading of 137,084 series, 6.21 of 7 GB and 27.3 days.
  It says how each slice states its delta, and gives M1's own. PR 2 records the head-series
  read. *Verification* gains the budget read and a retention read, with a red proof. The
  *Complexity delta* gains the budget. *Open questions* 9 asks whether the size cap already
  binds (**operator-only** if it does).
- **RA-8 (the down-Alertmanager window).** Not shortened. The 5-minute period with a
  10-minute grace pages about 15 minutes in, which is also the measured weekly-reboot gap,
  so it would page on most reboots. *Healthchecks.io sizing for the Watchdog* records the
  30 minutes, and *Open questions* 10 asks the operator to accept it, with a resolution
  path through the reboot records.
