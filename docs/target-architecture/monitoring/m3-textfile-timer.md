# M3 — One host timer to the textfile collector

Status: IN PROGRESS

PR 1, the textfile collector's live producer named: [#4869](https://github.com/DanielH2018/server/pull/4869) (issue #4855).

PR 2, disk-health's verdict written to the textfile collector: [#4871](https://github.com/DanielH2018/server/pull/4871) (issue #4856).

PR 3, the host-checks rules in shadow beside Kuma's disk tile: [#4937](https://github.com/DanielH2018/server/pull/4937) (issue #4936).

Soak read, [#4940](https://github.com/DanielH2018/server/issues/4940): day 7 passed on 2026-10-21, over 2026-10-13T20:55:58Z to 2026-10-20T20:55:58Z ([the read](https://github.com/DanielH2018/server/issues/4940#issuecomment-6326523797)). Query 1 returned 3 minutes, all at the edge of one episode (2026-10-18 19:10 to 19:30, the root filesystem at 76%) that `DiskHealthFailed` covered. Query 2 returned no minutes, and `TextfileCollectorError` never fired. PR 4 is unblocked. The disk crossed 75% inside the soak, so it exercised the positive case that the plan expected to leave untested.

Soak note, 2026-10-17: D5.7 ([#5423](https://github.com/DanielH2018/server/pull/5423)) moved node-exporter to Flux at 17:47:09Z, and the takeover rolled its DaemonSet once. The pods restarted at 17:47:10Z on daniel-server and 17:47:22Z on daniel-box. `DiskHealthNotReporting` is built to ride through such a roll (`unless on (origin) (up{job="node"} == 0)`). Under M1's convention 6, any episode at that time is explained, not a reset.

Depends on: M1, and M2's PR 3 (the `weekly-reboot` mute) before the cutover

Unblocks: M6 (with M2, M4 and M5)

This slice moves one host producer, daniel-box's root-disk check `disk-health`, from a Kuma
push to a node_exporter textfile metric that Prometheus alerts on. Its main output is the
metric contract every later host producer reuses: the 38 other push producers M6 drains, and
the host plane's `homelab_host_applied_info` (C6). It follows the conventions in
[M1](m1-alertmanager.md#conventions-every-monitoring-slice-follows), which this page calls
"M1 convention N".

## Goal and exit criterion

**Goal.** `disk-health.sh` writes `/var/lib/node-exporter-textfile/disk-health.prom`, and two
rules page on it. The first pages on a red verdict. The second pages on a file that stopped
being refreshed within a deadline derived from the cron's own schedule. The Kuma push and the
`daniel-box Disk` tile are gone. The Healthchecks.io ping stays as the leg that survives a
cluster outage.

**Exit criterion.** All four hold:

1. `probe.py metric 'homelab_check_last_run_timestamp_seconds{check="disk-health"}'` returns
   one series with `origin="daniel-box"`, no older than 600 seconds.
2. `probe.py metric 'monitor_status{monitor_name="daniel-box Disk"}'` returns nothing, and
   `disk-health.sh.j2` no longer calls `kuma_push` or reads a push token. It still sources
   `kuma-push-lib.sh` for `boot_grace_active`, until M6's B0 moves that function.
3. The deadline literal in `DiskHealthNotReporting` equals the `DEADLINE_S` that the script
   renders from `k3s_disk_health_cron_minute`, and a pytest asserts it.
4. The hand-written drill in *Verification* fired `DiskHealthFailed` and
   `DiskHealthNotReporting`. Each cleared on the next cron run.

## Baseline at `70591c307`

- **The producer is a cron, not a timer.** design.md calls it a "host timer". In fact
  `ansible/roles/setup/k3s/tasks/health-crons.yml` installs it as the cron `daniel-box disk
  health`, minute `k3s_disk_health_cron_minute` (`"*/10"`), as `{{ sys_user }}`, running
  `/usr/local/bin/disk-health.sh`. The script ships as the `k3s-disk-health` release group
  (`k3s_render_stamp_groups`), under the tag `disk-health`. It is none of the eight
  `kuma_check_timer.yml` callers that `KNOWN_CHECKS` names in
  `ansible/tests/setup/test_kuma_check_timer.py`. The textfile write is the same either way.
- **The script** (`ansible/roles/setup/k3s/templates/disk-health.sh.j2`):
    - It sources `/usr/local/lib/kuma-push-lib.sh` by absolute path and exits 1 without it.
    - It reads `DANIEL_BOX_DISK_PUSH_TOKEN` from `/etc/rancher/k3s/kuma-push.env`. An empty
      token logs and exits 0 before the boot-grace guard and before `df`, so a host without the
      token reads no disk at all.
    - Inside `k3s_health_cron_boot_grace_s` (420) it pushes `up` with a "still booting" message
      and stops.
    - It reads `df -P /` and compares the used share against `k3s_disk_max_pct` (75).
    - It logs `status=<up|down>` through `logger -t disk-health`, then pushes to Kuma.
      `kuma_push` in `ansible/roles/setup/initial_setup/files/kuma-push-lib.sh` retries twice
      at a 30s backoff, so one push can take over 60 seconds.
    - It pings Healthchecks.io slug `daniel-box-disk-health`, adding `/fail` when red.
    - Its header records why it exists apart from Prometheus: on 2026-08-23 it was the only
      thing watching daniel-box's root disk for 5.4 hours.
- **The Kuma tile.** `daniel-box-disk.json` in
  `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2`, named `daniel-box Disk`,
  has `interval: 1200`, `tag-severity: critical`, and notifies Discord and email. AutoKuma
  syncs every 60s (`uptime_kuma_k8s_autokuma_sync_interval`) with `ON_DELETE=delete`, so
  dropping the entity deletes the live monitor.
- **A tile deleted under a pushing script pages.** `swallowed_verdicts` in
  `ansible/roles/k8s/monitor-bridge/files/verdicts/logs.py` counts a push that Kuma rejected
  (`KUMA_REJECTED`, `by=kuma` on the line), whatever its status. It counts every tag outside
  `HC_ROUTED_TAGS`, and `disk-health` is outside it. The rejected line stays the tag's newest
  line until the script logs its next `status=` line.
- **The textfile collector already has a producer, and monitor-bridge pages on it.**
    - The node-exporter DaemonSet
      (`ansible/roles/k8s/node-exporter/templates/daemonset.yaml.j2`) mounts hostPath
      `/var/lib/node-exporter-textfile` at `/textfile` and passes
      `--collector.textfile.directory=/textfile`, on both k3s nodes. The hostPath has
      `type: DirectoryOrCreate`, since a789fc266
      ([#145](https://github.com/DanielH2018/server/pull/145)), so on first start the kubelet
      created it as root with mode 0755.
    - The `claude_code` role installs `files/claude-cgroup-metrics.sh` (task "Deploy the cgroup
      metrics script") and `claude-cgroup-metrics.timer`, which runs it as root every 30s. It
      writes `claude_cgroup.prom` on every host with `has_claude_code: true`, which is
      daniel-box and daniel-server. It writes through `mktemp
      "$TEXTFILE_DIR/claude_cgroup.prom.XXXXXX"`, `chmod 0644` and `mv`, and
      `ansible/tests/setup/test_claude_cgroup_metrics.py` drives it through a `TEXTFILE_DIR`
      override.
    - A live read on 2026-10-11, `probe.py metric 'node_textfile_mtime_seconds'`, returned
      `file=/textfile/claude_cgroup.prom` for `origin="daniel-box"` and
      `origin="daniel-server"`. `node_textfile_scrape_error` read 0 on both.
    - monitor-bridge reads those series. `checks/host.py:with_claude_cgroups` folds them into
      `check_mem`, for the cgroups `CLAUDE_CGROUPS` names in `bridge/config_host.py`. Arm 4 of
      `verdicts/host_cgroups.py:claude_cgroup_verdict` fails when an expected cgroup stops
      reporting. A missing or malformed `claude_cgroup.prom` therefore already pages, from
      Kuma.
    - Three statements say otherwise and are stale (failure class 8). The node-exporter role's
      `CLAUDE.md` calls the directory "an empty hook, not yet used". The DaemonSet's volume
      comment says "empty today". `claude-cgroup-metrics.sh`'s header quotes the `CLAUDE.md`.
- **A dead node-exporter already pages.** `checks/cluster.py:check_targets_down` pages on any
  `up == 0`, the `node` job included. The bridge's `EXPORTER_DEPENDENT` gate then holds the
  checks that read a dead exporter's series, so one dead exporter pages once.
- **The `node` scrape job** in `prometheus.yaml.j2` discovers node-exporter pods and relabels
  `origin` and `instance` to the node name, at a 1m interval.
- **Healthchecks.io's numbers mean something different from a rule's deadline.**
  `daniel-box-disk-health` in `monitor_bridge_healthchecks_expected` has `timeout: 600` and
  `grace: 1500`. `docs/healthchecks-io-deadman.md` records that a check flips at the last ping
  plus the period plus the grace, 2100s for this slug, measured to the second on 2026-09-09.
  The same page derives a 20-minute floor for the grace, because the first run after a boot is
  skipped. The extra 5 minutes are the operator's choice, kept at the 2026-09-25
  reconciliation. `ansible/tests/setup/test_health_cron_boot_grace.py` holds the boot grace
  under the cron period.
- **Every change to the k3s role is a hand apply, recorded in the owed ledger.**
  `deploy_changes.py:_BROAD_MANUAL_PREFIXES` (under
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/`) holds the playbook files only, so a
  change under `ansible/roles/setup/k3s/` is a setup-role change the tick cannot apply. The
  tick fast-forwards it and records a `manual_plane` line for `k3s` in
  `/var/lib/gitops-deploy/owed.jsonl` (`docs/gitops-pipeline.md`, *A role only a hand can apply
  is recorded, not parked*). `land.sh` prints the apply and the clear command, which
  `deploy_remediation.py:manual_plane_clear_for` builds with any `--applied` narrowing. **GitOps
  Deploy — Status** goes down once the oldest pending line is older than
  `GITOPS_BEHIND_MAX_S` (6 h). The play needs `become`, so the operator runs it.
- **The env file's task carries five tags.** "Deploy the Kuma push credentials for the
  heartbeat scripts" in `health-crons.yml` renders `kuma-push.env` under `backup-health`,
  `disk-health`, `manifest-prune`, `etcd-snapshot` and `release-staleness`.
- **Code and tests that name this script, its tile or its token:**
    - `ansible/tests/setup/test_kuma_env_renders_for_every_cron_tag.py`. The red proof
      `test_task_missing_the_env_tag_is_flagged` and its clean half
      `test_task_carrying_a_tag_the_env_task_has_is_clean` use `disk-health.sh.j2` as the
      fixture for a script that sources the env file. `_missing_env_tags` skips any script that
      does not.
    - `ansible/tests/services/test_healthchecks_pings.py`, which uses the script as its
      rendered sender.
    - `ansible/tests/setup/test_health_cron_boot_grace.py:FREQUENT_SCRIPTS` includes the script.
      `test_a_guarded_cron_still_beats_its_kuma_tile` requires `PUSH_URL="` before the guard and
      `kuma_push up "skipped — host still booting"` after it.
    - `ansible/tests/setup/test_cron_scripts_publish_via_pr.py:_EXPECTED_IN_CORPUS` names the
      script. `push_corpus` keeps every template that mentions `api/push` or `PUSH_URL`. It
      measured 35 templates at `70591c307` against `_MIN_CORPUS` 23. The comment beside
      `_MIN_CORPUS` says "measured 24", which is stale.
    - `ansible/tests/services/test_kuma_static_monitors.py:EMAIL_TIER` names `daniel-box Disk`,
      and `test_email_tier_membership_is_exactly_declared` asserts set equality.
    - `scripts/secrets_mgmt/consumers.py:CROSS_HOST_PUSH_TOKENS` names
      `daniel_box_disk_push_token`, and so does a comment in
      `scripts/secrets_mgmt/tests/test_secret_consumers.py`.
    - `ansible/secret_rotation.yml` holds a row for `daniel_box_disk_push_token` in the
      `auto` tier. `grep -c '_push_token:'` on that file prints 83.
      `scripts/secrets_mgmt/secrets_lib/secret_registry.py:sync` adds missing names and only
      reports stale rows. `scripts/secrets_mgmt/secret_rotation.py:cmd_audit` returns 1 under
      `--check` on any missing or stale row, and the `secret-rotation-registry-sync` prek hook runs that
      check. Commit `6894e3523` ("Retire the E7-orphaned credentials") deleted its retired
      keys' rows by hand, in the commit that removed the keys.
    - Three sibling tests already run a rendered push script hermetically. `_pi_health.py` and
      `_ruleset_drift.py` in `ansible/tests/setup/` replace `REAL_LIB` in the rendered body
      with a stub library, and `test_release_staleness_push_grouped.py` does the same with
      `body.replace`.

## Design

### The producer metric contract

M6 reuses this contract for every remaining host producer. The bridge exporter in M6 exposes
the same three series over HTTP.

- **Directory.** `/var/lib/node-exporter-textfile/`, the same path on every host. The k3s nodes
  already mount it. The Pi's host-unit node_exporter gains the flag in M6. *Open questions*
  raises the path as a C6 gap, because `homelab_host_applied_info` needs it too.
- **Two kinds of file live there.** A *check file* carries a verdict and follows this whole
  contract. A *gauge set* carries measurements only, such as `claude_cgroup.prom`. A gauge
  set is exempt from the three `homelab_check_*` series, because the check that reads it owns
  its absence: `check_mem`'s cgroup arm 4 today, and its rule after M6's A1. A gauge set still
  follows the write rule and the file mode below, which `claude_cgroup.prom` already does.
- **One check file per check:** `<check>.prom`, where `<check>` is the producer's syslog tag.
- **Atomic write**, in the pattern `claude-cgroup-metrics.sh` already uses:
    1. `mktemp "$TEXTFILE_DIR/<check>.prom.XXXXXX"` in the same directory. The random suffix
       means the name never ends in `.prom`, and node_exporter reads only `*.prom`.
    2. A `trap` removes the temp file on any exit.
    3. Write the series, then `chmod 0644`. mktemp creates 0600, and the collector reads as
       uid 65534.
    4. `mv` the temp file over `<check>.prom`. A reader never sees a half-written file.

    `TEXTFILE_DIR` overrides the directory and defaults to the real path, so a test needs no
    root. A failed write logs through `logger` and the run goes on to its pings.
- **Three series, each labelled `check="<check>"`:**
    - `homelab_check_success`: 1 when the verdict is up, 0 when it is down.
    - `homelab_check_last_run_timestamp_seconds`: the Unix time the verdict was taken. The
      producer takes it at the verdict and writes the file before any push or ping, so a slow
      push cannot age it.
    - `homelab_check_deadline_seconds`: the longest legitimate gap between two writes. The
      producer's template renders it from the same variable as its schedule. It is for readers,
      such as dashboards and a rule's `description`. No per-check rule's verdict reads it,
      because a file that lost the series would silently disable a rule built on it (class 3).
      Each per-check rule carries the deadline as a literal, and a pytest pins that literal to
      the rendered value (M1 convention 1). One rule does read it: `HostCheckOverdue` in
      `host-checks.yaml`, one per host, is a backstop that covers a producer before its own
      pair exists (#6166). An Alertmanager inhibit mutes it wherever the pair fires, so it
      pages only for a producer with no pair (#6186). It cannot replace the pairs, because it has no `absent_over_time`
      arm and inherits the class-3 gap.
- **A fourth series from the shared library.** `write_check_textfile` in
  `homelab-check-lib.sh` also writes `homelab_check_duration_seconds`, the run's seconds up to
  its verdict, for trend boards (#6166). It reads bash's `$SECONDS` unless the caller sets
  `CHECK_DURATION_S`, so every library caller gains it with no change. A producer that writes
  its own file may leave it out, and no rule reads it.
- **Optional detail gauges** carry what a message would have said, for example
  `homelab_disk_root_used_ratio`. A rule's `description` may read them. No rule depends on
  one.
- **The skipped first run after a boot writes nothing.** The deadline already covers one
  skipped slot, as the Healthchecks.io grace does.
- **Scrape labels.** The `node` job adds `origin` and `instance`, both the host name, which is
  C7's `service` for a host check.
- **Two rules per check**, the shape M6 copies:
    - `<Check>Failed`: `homelab_check_success{check="<check>",origin="<host>"} == 0`.
    - `<Check>NotReporting`: the timestamp is older than the deadline, or no sample at all
      arrived within the deadline. The second arm is `absent_over_time` over the deadline,
      never a bare `absent()`. Prometheus marks every series of a target stale when a scrape
      fails or the target leaves discovery, so a bare `absent()` would fire at the next
      evaluation through every node-exporter roll and every failed scrape. node-exporter is
      `k8s_autodeploy: true`, so Renovate rolls it unattended.
    - The `NotReporting` rule ends in `unless on (origin) (up{job="node"} == 0)`. A dead
      exporter already pages through the bridge's `targets` check, and the clause keeps it to
      one page, as `EXPORTER_DEPENDENT` does in the bridge. The equality matchers carry `check`
      and `origin` into the `absent_over_time` result, so an inhibit rule keyed on `origin`
      (M6's A1) also reaches it.

### The deadline for disk-health

`homelab_check_deadline_seconds{check="disk-health"}` = 2 × period + 300s. The period is
`k3s_disk_health_cron_minute` read as `*/N` (600s), so the deadline is 1500s. The terms:

- One period: the normal gap.
- One more period: the slot the boot grace skips. `test_health_cron_boot_grace.py` already
  keeps the boot grace under one period.
- 300s: cron start jitter, the 1m `node` scrape and the 1m rule evaluation, with margin.

The template computes the period only for a `*/N` minute, and the derivation pytest fails on
any other shape. That pytest, new in `ansible/tests/setup/`, renders the script and asserts
three relations:

- `DEADLINE_S` equals 2 × the cron's period + 300.
- `DEADLINE_S` exceeds one period plus `k3s_health_cron_boot_grace_s`.
- `DEADLINE_S` is at most the slug's `timeout` plus its `grace` in
  `monitor_bridge_healthchecks_expected` (2100s), so Prometheus pages no later than
  Healthchecks.io's dead-man.

The deadline is not equal to the slug's grace, and the two are not tied. The grace counts from
the expected ping and carries the operator's extra 5 minutes, while the deadline counts from
the last write. Lowering the grace to its 1200s floor still satisfies the third relation.

### The rules

`files/rules/host-checks.yaml`, group `host-checks`, all with `for` empty unless the table says
otherwise:

| Alert | Severity | `service` | Shadow until |
|---|---|---|---|
| `DiskHealthFailed` | `critical` | `daniel-box` | PR 5 |
| `DiskHealthNotReporting` | `critical` | `daniel-box` | PR 5 |
| `TextfileCollectorError`, one rule per host whose node_exporter runs the textfile collector (daniel-pi's since #6166), `for: 10m` | `warning` | the host name | M6's A1 cutover |

`DiskHealthFailed` is `homelab_check_success{check="disk-health",origin="daniel-box"} == 0`.
Its `description` quotes the detail gauge and the 75% Longhorn scheduling threshold from the
script's header. `DiskHealthNotReporting` is:

```promql
(
    time() - homelab_check_last_run_timestamp_seconds{check="disk-health",origin="daniel-box"} > 1500
  or
    absent_over_time(homelab_check_last_run_timestamp_seconds{check="disk-health",origin="daniel-box"}[1500s])
)
unless on (origin) (up{job="node"} == 0)
```

Both disk-health alerts are `critical`, matching the tile's critical tag and its email
notification.

**`TextfileCollectorError` ships in shadow.** Each of its two rules is
`node_textfile_scrape_error{origin="<node>"} == 1`, with a literal `service`. A malformed
`claude_cgroup.prom` already pages through `check_mem`'s cgroup arm 4, from Kuma. After the
cutover, a malformed `disk-health.prom` pages through `DiskHealthNotReporting`'s
`absent_over_time` arm. A non-shadow collector alert would page a second time for either
cause, against M1 convention 5. Until M6, its value is the diagnosis it records in `ALERTS`,
which names the plane that broke. M6's A1 batch moves `check_mem` and its cgroup arm, and it
decides then whether the collector alert pages.

**`service` is a literal per node, not derived from `origin`.** M1 convention 3 gives `service`
as a literal, or through M2's workload recording rules, and M1's `alert_rules.py` accepts only
those two forms. A node is not a workload. Deriving `service` from `origin` would need a
`label_replace`, which raises M1's `$1` question under Flux substitution (M1 convention 7).
*Open questions* raises the host-wide form for C7.

The weekly reboot covers about 07:35 to 07:50. The last write before it is 07:30, so
`DiskHealthNotReporting` could fire at 07:55. That lies inside the `weekly-reboot` mute from
M2's PR 3, which this slice requires before its cutover. An unplanned reboot longer than the
deadline pages, as Kuma's tile does today.

### The textfile directory's owner

A new task in `health-crons.yml`, tagged `disk-health` (and, as M6 adds producers, theirs),
sets `/var/lib/node-exporter-textfile` to owner root, group `{{ sys_user }}` and mode 2775.
Files are written 0644, so the node_exporter user (65534) can read them. The root-run cgroup
writer is unaffected, and its file stays root-owned. The task runs on daniel-box only, where
the k3s role's health crons run. daniel-server's directory keeps its kubelet-created root
0755. Only the root-run cgroup writer writes there, so it needs no change until a non-root
producer arrives. M6's B2 adds the task there for its two Loki-route witnesses.

### The tree

```diff
 ansible/roles/k8s/node-exporter/
~  CLAUDE.md                        names the directory's producers and the contract (PR 1)
~  templates/daemonset.yaml.j2      volume comment points at the CLAUDE.md (PR 1)
 ansible/roles/setup/claude_code/
~  files/claude-cgroup-metrics.sh   header stops quoting the CLAUDE.md (PR 1)
 ansible/roles/setup/k3s/
~  templates/disk-health.sh.j2      writes disk-health.prom (PR 2); drops the Kuma push (PR 5)
~  templates/kuma-push.env.j2       drops DANIEL_BOX_DISK_PUSH_TOKEN (PR 5)
~  tasks/health-crons.yml           the textfile-directory task (PR 2)
 ansible/roles/k8s/observability/
+  files/rules/host-checks.yaml     (PR 3; PR 5 drops shadow from two alerts)
+  tests/rules/host-checks.test.yaml
 ansible/roles/k8s/uptime-kuma/
~  templates/static-monitors.yaml.j2   widens daniel-box-disk.json (PR 4), deletes it (PR 5)
 ansible/
~  vars/secrets.yml                 loses daniel_box_disk_push_token (PR 6, operator-only)
~  secret_rotation.yml              loses the token's row, deleted by hand (PR 6)
 scripts/secrets_mgmt/
~  consumers.py                     drops the token from CROSS_HOST_PUSH_TOKENS (PR 6)
~  git_dates.py                     adds the token to RETIRED (PR 6)
 docs/assets/generated/fragments/
~  secret-tiers.md                  the auto tier's count drops by one (PR 6)
```

## Steps, as PRs

Each PR that deploys a k8s tag first applies M1 convention 8. It reads the tag's
`containers_list` entry. With `owner: flux`, the merge's `promote` run applies the change and
`land.sh` skips the tag (C4). Without it, `land.sh` deploys the tag after the merge. D5 can
move `node-exporter`, `observability` and `uptime-kuma` to Flux at any point in this slice,
because the slice index does not order D5 after M3. Each order below holds under both.

### PR 1 — Name the collector's live producer (about 30 lines)

- The node-exporter role's `CLAUDE.md` replaces "an empty hook" with what is true. The
  directory holds `claude_cgroup.prom`, which the `claude_code` role writes as root every 30s
  and `check_mem`'s cgroup arm reads. M3's contract governs any check file there.
- The DaemonSet's volume comment and `claude-cgroup-metrics.sh`'s header point at that
  `CLAUDE.md` instead of restating it, so neither can go stale again.
- Deploys `node-exporter`. `kubectl apply` reads a comment edit as `unchanged`, so nothing
  rolls (`ansible/roles/k8s/manifests/CLAUDE.md`, the restart rule).
- Applies `claude_code` on both nodes. The tick applies `initial_setup.yml --tags claude_code`
  on daniel-box itself. `land.sh` names daniel-server's apply (`docs/landing.md`, *A
  self-applied setup role reaching a host beyond the tick's own*). The operator runs it on
  daniel-server in the same session, because the deployed-script record under
  `setup-deployed-manifest.d` reads as drift there until then.
- Two-writer check: no paging changes.

### PR 2 — disk-health writes the textfile (about 150 lines)

- Add the directory task.
- Restructure `disk-health.sh.j2`. The Kuma push and the Healthchecks.io ping keep their
  behaviour.
    1. A missing token no longer exits. It skips the two `kuma_push` calls only, so a host
       without the token still reads its disk and writes the file.
    2. After the boot-grace guard, the order is: `df` and the verdict, the timestamp, the
       `logger` line, the textfile write, the Kuma push, the Healthchecks.io ping.
    3. `DEADLINE_S` renders from `k3s_disk_health_cron_minute`.
    4. The boot-grace path is unchanged. It still pushes `up` when a token exists, and it
       writes nothing.
- Add a script test in `ansible/tests/setup/`. Its harness follows the sibling tests named in
  the Baseline:
    - It renders the script and replaces the absolute library path with a stub library. The
      stub's `boot_grace_active` returns what the test sets, so the runner's own uptime never
      decides a case. The stub's `kuma_push` appends its arguments to a file.
    - It replaces the two env-file paths with temp files, one case with a token and one without.
    - It puts stub `df`, `curl` and `logger` first on `PATH`, and points `TEXTFILE_DIR` at a temp
      directory.
    - Cases: an up run writes `homelab_check_success 1`, the timestamp and the deadline. A down
      run writes `homelab_check_success 0`. A run without a token still writes the file and
      calls no `kuma_push`. A boot-grace run leaves an existing file byte-identical. The file is
      0644, and no temp file is left behind.
- Add the derivation pytest.
- **Apply.** The tick records `manual_plane` for `k3s`. The operator runs the apply command
  and the clear command exactly as `land.sh` prints them, from the primary checkout on
  daniel-box. The apply has the shape
  `uv run ansible-playbook ansible/k3s-bringup.yml --tags disk-health > /tmp/k3s-disk-health.log 2>&1 < /dev/null`,
  with stdio redirected because ansible-playbook refuses a non-blocking stdio. The clear
  carries `--applied` with the narrowed tags the tick recorded. Those can be wider than
  `disk-health`, because the env-file task carries five tags, so do not hard-code them. An
  uncleared line pages GitOps Deploy — Status after 6 h.
  While an H1 PR from H1.1h to H1.7b still has an open k3s `manual_plane` line, that line's
  tag set is empty, because H1's ranges are expected to refuse the narrowing. A narrowed
  apply against an empty row keeps the line (`scripts/deploy_tools/gitops_state.py:clear_manual_plane`),
  so this `--applied` clear may leave it standing. Clearing it then follows H1's *The owed
  `manual_plane` line*, whose range check sees this commit and takes its tags into the same
  hand apply.
- Two-writer check: the script now writes two outputs, but only Kuma pages. No rule reads the
  file yet.

### PR 3 — The host-checks rules in shadow (about 150 lines)

- Add `host-checks.yaml` with all four rules carrying `shadow: "true"`, and its promtool tests:
    - A firing case for each rule, asserting `service` and `severity`.
    - `DiskHealthNotReporting`'s zero-series case: no `disk-health` series for longer than
      1500s, and the alert fires through `absent_over_time`.
    - A short gap: one and then two missing samples, marked `stale`, between fresh timestamps.
      The alert does not fire.
    - An exporter-down case: `up{job="node",origin="daniel-box"}` reads 0 and the series is
      gone. The alert does not fire.
- Add the literal-pin pytest in `ansible/tests/services/`. It asserts that both literals in
  `DiskHealthNotReporting`, the `> 1500` and the `[1500s]` window, equal the rendered
  `DEADLINE_S`.
- Deploys `observability`. The rules reload in place (M1, `--config.auto-reload`), and no pod
  rolls.
- Two-writer check: all four rules route to the blackhole receiver.
- **Soak, shortened by the operator on 2026-10-17.** The soak ends after the 2026-10-18 Sunday
  reboot, about four and a half days in, rather than on day 7. The window still spans that
  reboot, and the closing reading is taken once its mute ends at 08:15 UTC. The rest of this
  bullet applies as written.
- **Soak.** 7 days, run as M1 convention 6 with `monitor_name="daniel-box Disk"` and
  `alertname=~"DiskHealth.*"`. The disk is far below 75% (Kuma's tile shows the share), so the
  soak mostly proves the negative: no `DiskHealthNotReporting` outside the reboot window. The
  same window reads `ALERTS{alertname="TextfileCollectorError"}`, and each episode is
  explained. The drill in *Verification* proves the positive.

### PR 4 — Widen the tile's deadline (about 10 lines)

- Lands after the soak, because a widened tile can no longer disagree with a shadow rule about
  a silent cron.
- `daniel-box-disk.json` changes `interval` from 1200 to 604800 (one week). Its template
  comment says that this is M3's cutover step and that PR 5 deletes the tile. The same template
  already declares `registry-gc.json` with `"interval": 608400`, so AutoKuma and Kuma accept a
  push deadline that long.
- The tile still pages a `down` push at once. A silent cron reaches the tile's deadline only
  after a week, so until PR 5, Healthchecks.io's dead-man (2100s) is the pager for a silent
  cron, as it already is for a cluster outage.
- **Why it exists.** PR 5 must stop the script's pushes before it deletes the tile, or
  `swallowed_verdicts` pages on the rejected pushes. The widened deadline means the gap between
  those two steps cannot page Kuma, however long the merge, the CI wait, the deploy or a Flux
  `promote` run take.
- Deploys `uptime-kuma`. `uptime-kuma` is `owner: flux`, so `land.sh` skips the tag and the
  merge's `promote` run applies the change (M1 convention 8, C4). AutoKuma applies it within
  its 60s sync after `promote` publishes the bundle.
- Two-writer check: no paging changes for a verdict. Kuma still pages it.

### PR 5 — Cut over (about 20 lines added, about 45 removed, plus test edits)

- In `disk-health.sh.j2`, drop the token read, the `PUSH_URL` and both `kuma_push` calls, and
  drop the Kuma reasoning from its header. Keep the Healthchecks.io ping and the `logger` line.
- Keep the `source` of `kuma-push-lib.sh`. The boot-grace skip still calls `boot_grace_active`
  from it, and now exits 0 without writing or pushing. M6's B0 moves `boot_grace_active` and
  the atomic write into a shared `homelab-check-lib.sh`, and that PR switches this script's
  `source` line.
- Drop `DANIEL_BOX_DISK_PUSH_TOKEN` from `kuma-push.env.j2`. The env-file task keeps its
  `disk-health` tag, so this PR's `--tags disk-health` apply re-renders the file without the
  line. M6's B1, which retires the other k3s crons' tokens, drops the tag.
- Drop `daniel-box-disk.json` from `static-monitors.yaml.j2`, and `shadow: "true"` from the two
  disk-health alerts. `TextfileCollectorError` keeps its `shadow`.
- **Preconditions.** PR 4 is deployed: `land.sh` printed `settled` for `uptime-kuma`, or
  `flux/uptime-kuma` reports success for PR 4's SHA. M2's PR 3 (the `weekly-reboot` mute) has
  landed. This PR's CI is green. Convention 8 is checked for `uptime-kuma` and
  `observability`.
- **Order.** Three rules decide it. The script never pushes to a token whose tile is deleted.
  The tile never sees a gap longer than its deadline. `shadow` never drops while the tile can
  still page a verdict. The operator runs step 1 and stays through step 3, so schedule the
  landing when the operator is present.
    1. On daniel-box, from this PR's worktree, the operator runs
       `uv run ansible-playbook ansible/k3s-bringup.yml --tags disk-health > /tmp/k3s-disk-health.log 2>&1 < /dev/null`.
       The script stops pushing, and the env file loses the token line. The tile stays green
       on its one-week deadline. The shadow rules still route to the blackhole receiver.
       Healthchecks.io's `/fail` ping is the pager for a red run.
    2. Immediately after, land the PR: `./scripts/deploy_tools/land.sh --pr <n> --await-merge
       --arm-merge` (the `land-after-merge` skill owns the full invocation). `land.sh` deploys
       `uptime-kuma`, which deletes the tile, and `observability`, which drops `shadow`. Either
       order is safe, because the tile no longer receives a verdict. When both tags are
       flux-owned, the merge's `promote` run applies them instead.
    3. The tick records `manual_plane` for `k3s`. From the primary checkout, run the printed
       apply, which changes nothing because the merge carries what step 1 applied. Then run the
       printed clear.
- **The no-pager window** runs from step 1 to the `observability` deploy. On a green master,
  `land.sh`'s CI wait (`--ci-timeout`, 900s by default) and the deploy bound it. A red master
  CI is one of the root `CLAUDE.md`'s *When to wait* stops, and then only the time to fix CI
  bounds the window. Healthchecks.io's `/fail` ping and its dead-man cover it either way. This
  is M1 convention 5's safe direction, bounded by the landing rather than by the tile's
  interval.
- **The revert window.** Between step 1 and the merge, another session's apply of the k3s
  role's `disk-health` block from master restores the push. That is harmless while the tile
  exists. Repeat step 1.
- Two-writer check: from this commit, the check pages from Alertmanager only, with
  Healthchecks.io beside it as before.

### PR 6 — Retire the push token (about 20 lines)

PR 6 is one commit with five parts. The first four are the token recipe M5's PR 5 and M6's
token PRs use:

1. **Operator-only:** remove `daniel_box_disk_push_token` from `ansible/vars/secrets.yml`
   with `sops`.
1. Delete the key's row from `ansible/secret_rotation.yml` by hand. Running
   `secret_rotation.py sync` does not delete it, because `secret_registry.py:sync` only
   reports a stale row. A commit that leaves the row fails the
   `secret-rotation-registry-sync` prek hook, and CI's prek run with it (*Baseline*).
1. Add the name to `scripts/secrets_mgmt/git_dates.py:RETIRED`.
   `scripts/secrets_mgmt/tests/test_departed_secrets_are_accounted_for.py` requires every
   name that has left `secrets.yml` to be in `RETIRED` or `RENAMED_FROM`.
1. Stage the `secret-tiers` fragment that the `regen-doc-fragments` hook rewrites.
   `scripts/docs/gen_doc_fragments.py:_secrets` renders it from
   `scripts/docs/fragment_readers.py:registry_counts`, and the `auto` tier loses one row.
1. Drop the token from `consumers.py:CROSS_HOST_PUSH_TOKENS` and from the comment in
   `test_secret_consumers.py` that names it.

The PR body quotes four reads:

- `uv run python scripts/secrets_mgmt/secret_rotation.py audit --check` exiting 0. The prek
  hook runs the same command at commit time. Under `--check`, `cmd_audit`'s exit code reads
  registry drift only. Where an age key exists, its push-token-shape arm decrypts in-process
  and prints key names, never values.
- `grep -c '_push_token:' ansible/secret_rotation.yml` printing one fewer than on
  `origin/master`. The count was 83 at `70591c307`, and M5 and D5 each retire tokens of
  their own, so the absolute number depends on the order the slices land.
- `grep -c '^daniel_box_disk_push_token:' ansible/vars/secrets.yml` printing 0. It prints a
  count, never a value.
- `git diff --stat origin/master -- ansible/vars/secrets.yml`. Plain `git diff` on that file
  prints plaintext.

Land this a week after PR 5, for the reason M2's PR 5 gives.

## Verification

```bash
# The file is scraped and fresh
uv run python scripts/diagnostics/probe.py metric 'homelab_check_last_run_timestamp_seconds{check="disk-health"}'
uv run python scripts/diagnostics/probe.py metric 'time() - homelab_check_last_run_timestamp_seconds{check="disk-health"}'
uv run python scripts/diagnostics/probe.py metric 'homelab_check_deadline_seconds{check="disk-health"}'
uv run python scripts/diagnostics/probe.py metric 'node_textfile_scrape_error'
uv run python scripts/diagnostics/probe.py metric 'node_textfile_mtime_seconds'
# The rules are loaded, and the collector alert has not fired
uv run python scripts/diagnostics/probe.py metric 'prometheus_rule_group_rules{rule_group=~".*host-checks"}'
uv run python scripts/diagnostics/probe.py metric 'ALERTS{alertname="TextfileCollectorError"}'
# After PR 5: the tile is gone
uv run python scripts/diagnostics/probe.py metric 'monitor_status{monitor_name="daniel-box Disk"}'
# After PR 6: the key and its registry row are gone, and the registry is in sync (exit 0)
grep -c '^daniel_box_disk_push_token:' ansible/vars/secrets.yml
grep -c '^  daniel_box_disk_push_token:' ansible/secret_rotation.yml
uv run python scripts/secrets_mgmt/secret_rotation.py audit --check
```

**The drill (in PR 3's shadow window, by whoever can write as `{{ sys_user }}` on daniel-box).**
It is safe because the alerts are still shadow and route to the blackhole receiver. The cron
rewrites the file every 10 minutes, so read each result inside that slot:

1. Write `disk-health.prom` by hand with `homelab_check_success 0`, through the same mktemp,
   `chmod` and `mv` path.
2. Within 2 minutes, `probe.py metric 'ALERTS{alertname="DiskHealthFailed"}'` shows it firing.
3. Write the file again with `homelab_check_last_run_timestamp_seconds` set an hour back.
   `DiskHealthNotReporting` fires.
4. The next cron run rewrites the file and both clear.

Kuma is untouched, because no push is involved.

**Red proofs for the new checks:**

- Each rule: its promtool firing case. `DiskHealthNotReporting` also has its zero-series case.
  Its short-gap and exporter-down cases are the accepting halves of the `absent_over_time` arm
  and the `unless` clause.
- The script: the down-run case of the script test, which must write `homelab_check_success 0`,
  and the no-token case, which must still write the file.
- The deadline derivation: its `..._is_flagged` half moves the cron to `*/30` without moving the
  slug. The deadline becomes 3900s, above the slug's 2100s, and the test must fail.
- The literal pin: its `..._is_flagged` half renders the script with `*/15`. The rendered
  deadline becomes 2100s against the rule's 1500, and the test must fail.
- End to end: the drill above.

## Rollback

- **PR 1.** Revert. The same `node-exporter` deploy and the two `claude_code` applies carry the
  revert.
- **PR 2.**
    - Revert, then run the printed apply and clear, as in PR 2.
    - The script stops writing, and the stale `disk-health.prom` stays in the directory.
      Delete it as `{{ sys_user }}`, or leave it. Before PR 3 no rule reads it, and with PR 3
      live the rules are still shadow.
    - The directory keeps its new owner and mode, which is harmless.
- **PR 3.** Revert and deploy `observability`. The rules disappear. Nothing paged on them.
- **PR 4.** Before PR 5 only: revert and deploy `uptime-kuma`. The tile returns to 1200s.
- **PR 5.** Revert, then restore the tile before the script pushes again:
    1. Deploy `uptime-kuma` and `observability`. The tile returns under the same AutoKuma id,
       with empty history, on PR 4's one-week deadline. `shadow` returns.
    2. Apply `k3s-bringup.yml --tags disk-health` and run the printed clear. The push resumes
       with the token, which PR 6 has not yet removed. After PR 6 has landed, roll PR 6 back
       first, as its entry below says.
- **PR 6.** Nothing reads the token after PR 5, so PR 6 needs a rollback only before a
  revert of PR 5. Revert PR 6 first. A clean revert restores the ciphertext, the registry row
  and the `RETIRED` entry together, so `audit --check` still exits 0. If a later commit
  edited `secrets.yml`, the revert conflicts there. M2's PR 5 calls a retired key
  unrevertable, which holds only in this case. The operator then adds the key again with `sops`
  (**operator-only**). The same commit drops the `RETIRED` entry, which
  `test_departed_secrets_are_accounted_for.py:test_no_listed_departure_is_back_in_the_store`
  requires, and restores the row with `secret_rotation.py sync`.
- **A partial PR 5.**
    - Step 1 ran, and the PR has not merged, because its CI failed or the merge stalled. The
      script is silent, the widened tile stays green and the rules are still shadow, so only
      Healthchecks.io pages. Re-run the apply from the primary checkout, which is still on the
      old master, to restore the push. Or fix CI and land.
    - The PR merged, and neither k8s tag deployed, because master CI went red or `land.sh`
      reached `--ci-timeout`. Only Healthchecks.io pages. Re-applying from master changes
      nothing, because master now holds the push-less script. Fix CI, then finish the
      `uptime-kuma` and `observability` deploys.
    - `uptime-kuma` deployed and `observability` did not. Only Healthchecks.io pages. Deploy
      `observability`.
    - `observability` deployed and `uptime-kuma` did not. The tile receives no verdict, so
      Alertmanager is the only pager. Deploy `uptime-kuma`.

## Risks

- **Class 3, empty read as clean.** A stopped producer leaves a stale file whose `success` still
  reads 1. Mitigation: `DiskHealthNotReporting` on the timestamp, and its `absent_over_time`
  arm for a deleted file.
- **Class 3, a malformed file.** One bad `.prom` file makes node_exporter's textfile collector
  report an error and drop that file's series. Mitigation: the atomic write. A malformed
  `disk-health.prom` pages through the `absent_over_time` arm. `TextfileCollectorError`
  records which plane broke, in shadow.
- **One cause, two pages.** Two overlaps exist. A malformed `claude_cgroup.prom` pages
  `check_mem` from Kuma. A dead daniel-box exporter pages `targets` from Kuma. Mitigation:
  `TextfileCollectorError` stays shadow, and `DiskHealthNotReporting` carries the `unless`
  clause. M6's A1 replaces both with inhibit rules keyed on `origin`.
- **Class 7, the execution context.** The cron runs as the deploy user, with cron's `PATH`, on a
  directory the kubelet created as root. Mitigation: the directory task, and the script test
  running the rendered script with the library path, the env files and the boot grace under
  the test's control. The memory note on cron's `PATH` applies to the tools the script calls,
  which are coreutils already resolved today.
- **Class 5, a sequence treated as a transaction.** PR 5 spans a hand apply and two deploys.
  Mitigation: PR 4 removes the time bound, the stated order holds under `land.sh` and under
  `promote`, and *Rollback* names each partial state. The Healthchecks.io leg keeps a pager for
  the root disk throughout.
- **Class 5, an additive sync read as a full one.** `secret_rotation.py sync` adds registry
  rows and never deletes one, the way `kubectl apply` leaves a stale Secret key. A PR 6 that
  only runs `sync` keeps the token's row, and the `secret-rotation-registry-sync` prek hook
  and CI both fail on it. Mitigation: PR 6 deletes the row by hand in the same commit, and
  its body quotes `audit --check` exiting 0.
- **A weaker Kuma dead-man between PR 4 and PR 5.** The tile notices a silent cron after a week
  instead of 20 minutes. Mitigation: Healthchecks.io's dead-man pages at 2100s throughout, and
  the gap is the time between two consecutive PRs.
- **Class 8, prose asserting facts.** Three statements called the directory empty while a
  producer wrote to it, and this plan's first draft inherited them. Mitigation: PR 1 corrects
  the `CLAUDE.md`, and points the two comments at it rather than restating it.
- **Cross-area: the host plane rewrites this script.** H2 and H3 move host crons into CronJobs
  or packages (C5). Mitigation: the metric contract above is the interface they keep, and *Open
  questions* asks C6 to pin the directory.
- **Cross-area: D5 moves the k8s tags to Flux mid-slice.** Mitigation: every PR checks
  convention 8 before it deploys, and PR 5's order does not depend on how `promote` sequences
  its applies.

## Tests and docs that change in the same PR

- **PR 1.** The node-exporter role's `CLAUDE.md`, the DaemonSet comment and the cgroup script's
  header. If the `facts-reverify-changed` hook rewrites `docs/facts.lock`, commit that too.
- **PR 2.**
    - The script test and the deadline derivation test.
    - `test_health_cron_boot_grace.py` is unchanged, because the boot-grace path is unchanged.
    - `ansible/roles/setup/k3s/CLAUDE.md` names the textfile directory under its health crons.
- **PR 3.** `host-checks.test.yaml` and the literal-pin pytest.
- **PR 4.** The tile's template comment.
- **PR 5.**
    - `test_health_cron_boot_grace.py`: `disk-health.sh.j2` stays in `FREQUENT_SCRIPTS`,
      because the guard test and the no-ping test still apply to it, and the census of four
      names still holds. `test_a_guarded_cron_still_beats_its_kuma_tile` narrows to the scripts
      that still push, and a new case asserts that disk-health's skip path calls no
      `kuma_push` and exits 0. Its comment about the 1200s window goes.
    - `test_kuma_env_renders_for_every_cron_tag.py`: both fixtures move to
      `longhorn-backup-health.sh.j2`, which still sources the env file. The red proof then
      still sees a violation, and the clean half still proves something.
    - `test_cron_scripts_publish_via_pr.py`: `disk-health.sh.j2` leaves `_EXPECTED_IN_CORPUS`.
      It leaves the corpus once neither `api/push` nor `PUSH_URL` appears in it, comments
      included. `_MIN_CORPUS` stays at 23, and its comment's stale "measured 24" becomes the
      count measured in this PR.
    - `test_kuma_static_monitors.py`: `EMAIL_TIER` loses `daniel-box Disk`.
    - `test_healthchecks_pings.py` keeps the script as its sender, because the ping stays.
    - `docs/healthchecks-io-deadman.md`'s *Watching the spine itself* section stops saying
      that the disk script pushes to Kuma.
    - `docs/k3s-node-plane-crons-and-incidents.md` records that the disk heartbeat is a
      textfile metric.
    - The comment in `ansible/roles/setup/nut_host/defaults/main.yml` that names `daniel-box
      Disk` keeps its ratio and drops the tile's name.
- **PR 6.** The `RETIRED` entry, the row deleted from `ansible/secret_rotation.yml`, the
  `secret-tiers` fragment that `regen-doc-fragments` rewrites, `CROSS_HOST_PUSH_TOKENS` and
  the test comment.

## Complexity delta

- **Added:** about 30 lines of shell for the atomic write and the reordered verdict, inline in
  this one script until M6's B0 moves the write to a shared library. Also one directory task,
  four alert rules with their promtool tests, three pytests, and one interim tile edit that PR
  5 deletes.
- **Removed:** the Kuma leg of one producer (about 30 lines with its comments), one token line,
  one Kuma tile, one push-token secret with its registry row and its `CROSS_HOST_PUSH_TOKENS`
  entry, and three stale statements about the collector.
- **Kept on purpose:** the Healthchecks.io ping, the only leg independent of the cluster.

## Open questions

1. **Contract gap, C6.** C6 says `homelab-updater` writes a node-exporter textfile metric, but
   it pins no directory. The Pi's host node_exporter has no textfile flag at all. Resolution:
   C6 pins `/var/lib/node-exporter-textfile/` on all three hosts, and M6's Pi batch adds
   `--collector.textfile.directory` to the Pi's unit, in `optimize_pi`'s `node-exporter-host`
   tag.
2. **Contract gap, C7, the producer half.** The `homelab_check_*` series are the producer half
   of the alert plane, and no contract names them. Resolution: C7 adopts the two kinds of file
   (a check file with the three series, a gauge set such as `claude_cgroup.prom` exempt from
   them), the mktemp-and-`mv` write with mode 0644, and the two-rule shape with
   `absent_over_time` and the `unless` clause. H2's CronJobs and H3's packages then keep them.
3. **Contract gap, C7, `service` for a host-wide rule.** C7 allows a host name as `service`,
   but M1's validator accepts only a literal or M2's workload join. This slice writes one rule
   per node. Resolution: M6's A1, which adds host-wide rules for three hosts, either keeps one
   rule per host or extends `alert_rules.py` (with its `..._is_clean` and `..._is_flagged`
   pair) to accept `service` derived from `origin`. The second choice waits on M1's open
   question about `$1` under Flux substitution.
4. **Contract gap, the slice index.** contracts.md says M3 depends on M1 alone. This plan's
   cutover (PR 5) also needs M2's PR 3, the `weekly-reboot` mute. Kuma's maintenance window
   mutes `daniel-box Disk` today, so cutting over without the mute could page during the
   Sunday reboot. Resolution: the slice index gains an M2 → M3 edge for the cutover only, or
   the mute moves into M1 as a fifth PR. PR 1 to PR 4 need only M1. No D5 edge is needed,
   because PR 4 makes PR 5's order hold under `promote`.
5. **"Timer" against cron.** design.md and the slice index say "host timer". The producer is a
   cron. Resolution: the textfile write is the same either way. M6 decides whether the producers
   that are `kuma-check-*` timers keep their rerun-while-red `Restart=on-failure`, which loses
   its purpose once no tile waits for an `up` push.
6. **When `TextfileCollectorError` pages.** Resolution: M6's A1 batch, when it moves
   `check_mem`'s cgroup arm to a rule, drops the collector alert's `shadow` with an inhibit
   that keeps one page per cause.

## Review log

### Round 1 (per-slice adversarial review)

Raised:

- M3-R1-01 (high): the Baseline called the collector empty, while `claude_cgroup.prom` has a
  live writer that monitor-bridge pages on.
- M3-R1-02 (high): PR 3's order paged `swallowed_verdicts` on rejected pushes.
- M3-R1-03 (high): `DiskHealthNotReporting`'s bare `absent()` paged on every exporter roll and
  failed scrape.
- M3-R1-04 (high): PR 3's order could not run through `land.sh`, nor under Flux, and the plan
  never ran the convention-8 check.
- M3-R1-05 (medium): PR 3 turned four existing tests red, and the plan listed none.
- M3-R1-06 (medium): the `manual_plane` handling was hedged, and the clear and the 6-hour page
  were missing.
- M3-R1-07 (medium): the script test as described never reached the textfile write.
- M3-R1-08 (medium): the deadline's equality with the Healthchecks.io grace was not "by
  construction", and the rule's deadline source was ambiguous.
- M3-R1-09 (medium): `TextfileCollectorError`'s `service` from `origin` departed from M1
  convention 3.

Fixed:

- **M3-R1-01.** Confirmed in the tree and live (the Baseline bullet lists the evidence). The
  Baseline names the producer, its root writer on both nodes and the `check_mem` arm that
  reads it. `TextfileCollectorError` ships in shadow until M6's A1. The contract classes
  `claude_cgroup.prom` as an exempt gauge set and adopts its mktemp, `chmod 0644` and `mv`
  write. A new PR 1 fixes the `CLAUDE.md`, the DaemonSet comment and the script header. The
  daniel-server statement now says why that directory needs no task yet.
- **M3-R1-02.** Confirmed by the docstring of `verdicts/logs.py:swallowed_verdicts` and by
  `HC_ROUTED_TAGS`. The fix differs from the proposed reorder. A hand apply first, then a
  deploy within 1200s, still races the merge, the CI wait and AutoKuma's sync, and nothing
  bounds a Flux `promote` run. A new PR 4 widens the tile's deadline to a week, and PR 5 (the
  old PR 3) stops the pushes before it deletes the tile. No push is ever rejected, and no step
  is time-critical. The rationale and the class 5 risk are rewritten.
- **M3-R1-03.** The arm is `absent_over_time` over the 1500s deadline, pinned by the same
  pytest as the timestamp literal. Promtool cases cover a one-sample and a two-sample gap that
  must not fire. The contract records the shape for M6. The review also showed that a dead
  exporter would page twice, so the rule gained the `unless on (origin)` clause and its case.
- **M3-R1-04.** Every deploying PR runs the convention-8 check. PR 5 gives the explicit
  `land.sh` invocation and the Flux variant. With PR 4's widened deadline, both hold without a
  slice-index edge before D5. *Open questions* 4 records that.
- **M3-R1-05.** All four tests are listed under PR 5 with their edits. `disk-health.sh.j2`
  stays in `FREQUENT_SCRIPTS`, and only the Kuma-beat assertion narrows, so the census of four
  holds. Both env-tag fixtures move, not just the red proof.
- **M3-R1-06.** The Baseline states that every k3s-role merge records `manual_plane`. PR 2 and
  PR 5 name the operator, the apply, the clear and the 6-hour page, and so does *Rollback*.
  The clear runs as printed rather than with a hard-coded `--applied disk-health`, because the
  env-file task carries five tags.
- **M3-R1-07.** PR 2 specifies the harness (a stub library, the env files, stub `df`, `curl`
  and `logger`) and the write's place: after the verdict, before the push. It also moves the
  token gate, which today exits before `df`.
- **M3-R1-08.** The rule carries literals pinned to the rendered `DEADLINE_S`. No rule's
  verdict reads the deadline series. The derivation pytest asserts
  2 × period < deadline ≤ timeout + grace, and the "by construction" wording is gone.
- **M3-R1-09.** `TextfileCollectorError` is one rule per node with a literal `service`, which
  M1's validator accepts as written. *Open questions* 3 raises the host-wide form for C7.

Refuted:

- **M3-R1-05, the `_MIN_CORPUS` sub-claim only.** The finding says the floor of 23 against "a
  measured 24 ... leaves headroom for exactly one retirement". That number comes from the
  test's own comment, which is stale. Running `push_corpus()` from
  `ansible/tests/setup/test_cron_scripts_publish_via_pr.py` at `70591c307` (a `uv run python`
  import of the module) returned 35 templates against `_MIN_CORPUS` 23. PR 5 therefore leaves
  the floor alone and corrects the comment. The rest of the finding stands and is fixed above.

### Round integration-2 (integration review, 2026-10-11)

Raised: one finding that names M3.

- X2-16 (medium): M3's and M5's token-retirement recipe leaves stale registry rows that the
  prek audit flags.

Fixed:

- **X2-16.** Confirmed in the tree at `70591c307`.
  `scripts/secrets_mgmt/secrets_lib/secret_registry.py:sync` adds missing names and builds
  its `stale` list without deleting a row. `scripts/secrets_mgmt/secret_rotation.py:cmd_audit`
  returns 1 under `--check` when `missing` or `stale` is non-empty. The `secret-rotation-registry-sync`
  hook in `prek.toml` runs that command. `ansible/secret_rotation.yml` holds the
  `daniel_box_disk_push_token` row, so the old recipe would have failed prek and CI. PR 6 is
  now one commit with five parts: the **operator-only** `sops` removal, the row deleted by
  hand, the `RETIRED` entry, the rewritten `secret-tiers` fragment and the
  `CROSS_HOST_PUSH_TOKENS` edit. It follows commit `6894e3523`. Its body quotes four reads:
  `audit --check` exiting 0, the `_push_token` count down by one, the key's count in
  `secrets.yml` at 0, and `git diff --stat` on `secrets.yml`. The old text's "regenerated
  secret-tiers fragment" could not happen under `sync` alone, because
  `scripts/docs/fragment_readers.py:registry_counts` counts the rows that `sync` left in
  place. The fix also updates *Baseline*, *The tree*, *Verification* (three reads after PR
  6), *Rollback*, *Risks* (a class 5 entry), *Tests and docs* and *Complexity delta*.
  *Rollback* now reverts PR 6 before a revert of PR 5, in the form M5's PR 5 rollback uses,
  and names the `sops` path for when the revert conflicts. M5's PR 5 and M6's token PRs
  already carry the same recipe, so the recipe needs no edit in another slice. This round
  requests one wording edit in M2: its PR 5 says a retired key cannot come back by revert,
  which holds only once a later commit has edited `secrets.yml`.

Refuted: none.

### Cross-slice follow-up (2026-10-12)

- Cross-slice note from M5: the Baseline bullet on `daniel_box_disk_push_token` and the X2-16
  entry now say that `cmd_audit` returns 1 under `--check` on any missing or stale row, as
  `secret_rotation.py:cmd_audit` and `secret_registry.py:registry_drift` read at `70591c307`.
  PR 6 is already one commit, so its order needed no change.
- Cross-slice note from H1: the PR 2 *Apply* bullet now says that the `--applied` clear may
  keep the k3s `manual_plane` line while an H1 line from H1.1h to H1.7b is open, and that
  the clear then follows H1's *The owed `manual_plane` line*. Confirmed at `70591c307`:
  `gitops_state.py:clear_manual_plane` keeps the line when a narrowed apply meets an empty
  row, and M3's `Depends on` names no H1 PR.
