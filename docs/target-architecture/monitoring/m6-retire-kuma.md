# M6 — Switch the readers and retire Kuma

Status: IN PROGRESS

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
- 37 other push producers, after D5 retires a 38th, registry-gc (*Baseline*);
- 14 hand-written probes.

No other slice owns them. M2's Open question 6 names M6 as the fallback owner of the
`workload_log_errors` port, and no other slice's text adopts it, so this page owns it (batch
A7). M5 Open question 4 passes `swallowed_verdicts` and its token to M6, which deletes them in
C-7. This plan therefore runs in three phases (A drains the producers, B switches the readers,
C deletes Kuma). *Open questions* proposes splitting it in the slice index.

## Goal and exit criterion

**Goal.** Every check pages through Alertmanager. `probe.py health <tag>` reads the alert plane
(Phase B item 1 states the exact selector and how it departs from C7). Homepage shows the alert
plane. No template, task, inventory key, script or skill calls or reads Kuma, and no
push-token secret remains.

**Exit criterion.** All five hold:

1. `ansible/roles/k8s/uptime-kuma/` does not exist. `probe.py metric 'up{job="uptime-kuma"}'`
   returns nothing. The read-only
   `kubectl get svc,ingressroute,cm,cronjob,deploy -n homelab -l homelab/role=uptime-kuma`
   returns nothing. That read holds only after C-2b and the **operator-only** delete that
   follows it, because C-1 leaves one tombstone ConfigMap (*Phase C*).
2. `grep -c '_push_token:' ansible/secret_rotation.yml` prints 0, and so does
   `grep -c '_push_token:' ansible/vars/secrets.yml`. Key names are plaintext in the encrypted
   file, so the second read prints a count and never a value. `secret_rotation.py audit --check`
   exits 0.
3. Nothing reads Kuma. This is a functional criterion, not a sweep of every mention. It is
   reached only after C-4b, which waits for D6.
    - None of these identifiers appears in a non-test file under `ansible/`, `scripts/` or
      `.claude/`, outside the allowlist below: `/api/push/`, `kuma_push`, `KUMA_URL`,
      `KUMA_PUSH_`, `monitor_status`, `entry_port('uptime-kuma')`, `kuma-push-lib`,
      `kuma_ingress_monitors`, `kuma-check-`. C-6 quotes the `git grep` that shows it
      (*Phase C* item 6). A read through an indirection escapes that grep:
      `netpol-probe-job.yaml.j2` reads the entry's port through `entry_port(t.service)`. The
      named-member case C-1 adds to `ansible/tests/k8s/test_netpol_probe_targets.py` holds
      that read instead.
    - A ratchet census row in `ansible/tests/repo/test_census_rows_text.py` holds every other
      mention of `kuma` in those trees to a frozen allowlist. Each allowlist entry names its
      file and its reason. The list only shrinks, and a file outside it that mentions `kuma`
      fails CI.
    - Both bullets share one allowlist. At C-6 it holds the entries below, and each entry
      names the identifiers it may still hold:
        - `scripts/secrets_mgmt/git_dates.py`: `RETIRED` names retired keys by design, so it
          holds `kuma_push` inside key names.
        - `scripts/dev/pytest_shard_weights.json`: a generated record keyed by test node
          ids, which can name deleted tests.
        - The census row itself and its red fixture.
        - `ansible/templates/autokuma.yml.j2` and the `kuma(...)` labels in the Pi's compose
          templates. H5 drops them (see *Open questions*).
        - Until C-4b lands: `ansible/filter_plugins/kuma_monitors.py`, its import and its
          `kuma_ingress_monitors` registration in `scripts/lib/ansible_jinja_env.py`, and
          the `kuma_ingress_monitors` row in
          `scripts/deploy_tools/narrow_lib/containers.py:FILTER_FIELDS`.
        - Any file the B-7 and C-6 PRs could not reword, with its reason.
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
      `shipper_dropped` (`checks/logs.py:with_export_failures`) read Prometheus too.
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
- **Other producers.** M3 removes 1, leaving 38. D5 retires one more, registry-gc, so M6
  moves 37. Most are host scripts; four run in pods. Where each lives:
    - **daniel-box host crons in the k3s role:** longhorn backup health, the etcd snapshot,
      manifest-prune, release staleness and live-drift. M6 owns the last three only until
      their Kuma side is gone. After C-2b, D6.5 disarms the checks and D6.5b deletes them
      (*Cross-slice follow-up*, #4609).
    - **The initial_setup role, on both nodes:** docs-refresh, the eval sweep, the two
      Loki-route witnesses, secret rotation and setup drift.
    - **The gitops_deploy role:** the ruleset-drift and interaction-limit timers.
    - **Other setup roles:** two etcd drills (hypervisor), three fake_remux pushes, two UPS
      secondaries (nut_host), render_records, renovate_agent and renovate_notify.
    - **The Pi:** SD-card health and container recovery (optimize_pi).
    - **daniel-box crons that k8s roles install:** three crowdsec scripts, artifacts sync,
      configarr health, janitorr health and the telemetry health check (observability).
    - **Not M6's: registry-gc.** D5's Wave D registry PR (`d5-bulk-move.md`, *registry*)
      retires the GC rather than moving it, and removes the `Registry GC` tile and
      `registry_gc_push_token`. D5 calls itself "the only owner of registry GC" (its Open
      question 15), and H2 records the same decision. Moving the cron here first would build
      a textfile rule that D5 then deletes. Its tile is the one push tile M6 waits for
      instead of draining (*Order*).
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
  `hypervisor`, `fake_remux`, `nut_host`, `render_records`, `renovate_agent` and
  `renovate_notify`. The tick applies them before `land.sh` deploys any k8s role.
- **A hand, never the tick:**
    - `k3s` routes to `k3s-bringup.yml`, which `deploy_changes.py:_BROAD_MANUAL_PREFIXES`
      keeps out of the tick. The tick records it in `manual_plane`.
    - `optimize_pi` has `host: daniel-pi`. Its apply is
      `initial_setup.yml --tags optimize_pi -e target=daniel-pi`, one play at a time on the Pi.
    - Every daniel-server half (initial_setup's second Loki witness, nut_host's daniel-server
      secondary). No tick runs on daniel-server, and `land.sh`'s note on the hosts the tick
      does not reach names that run.
- **`land.sh`, as k8s role deploys:** every k8s role, including the five that install the
  B6 daniel-box crons and the pods (B7). The sixth cron-installing role, registry, is D5's
  (*What M2 to M5 leave*).

### When D5 flips a role M6 edits

The planes above name the apply while a role is Ansible-owned. D5's Wave D flips most roles
M6 edits, in the order of its wave table, and from each flip on, C4 has `land.sh`, the tick
and `deploy.sh` skip the role. For an `owner: flux` role the merge is the apply. Flux
reconciles the commit, and a PR's live read waits until the role's Kustomization reports that
commit (C6), not for a `land.sh` `VERDICT:` line. C-1 waits for D5's registry PR, a Wave D
conversion PR, so Phase C runs inside Wave D.

Most M6 PRs change one pager side, so the owner decides only who applies them. Three steps
need one role's apply to land before another's, and only the toposort inside one `land.sh` run
gives that order. A Flux `dependsOn` cannot replace it: it orders two Kustomizations, never a
Flux apply against a `land.sh` deploy, and a mixed state is one of each. So these roles stay
Ansible-owned until the step has deployed, by index sub-edges (Open question 1):

- **monitor-bridge flips after A6-7a has deployed.** Every bridge-row Cutover-a, A6-7a
  included, deletes a tile and its push in one edit. Only `uptime-kuma` deploying before
  `monitor-bridge` keeps the tile from going DOWN (*The cutover order*). After A6-7a no row
  pushes, and every later monitor-bridge change is order-free, C-7's included.
- **observability flips after C-1 has deployed.** C-1 removes `KumaDown` in the
  `observability` deploy that runs before Kuma's prune. A Kuma pruned first fires `KumaDown`'s
  `absent()` arm 5 minutes later.
- **netpol-baseline flips after C-1 has deployed.** C-1's `netpol-baseline` deploy removes
  the callers policy that the status-page sync CronJob's push crosses. Applied before the
  `uptime-kuma` prune deletes that CronJob, it refuses the push, and Kuma pages its
  `Status Page Sync` tile. The tile's `interval` is 1800 s and the CronJob runs every
  15 minutes, so the page comes 15 to 30 minutes later. C-2a's order is free, because no Kuma
  pod is left by then.
- **uptime-kuma never flips.** It leaves the list at C-2b. A flip would cost a Wave D PR and a
  soak for a role C-1 empties, and C-1's tombstone path is Ansible-only. The cost falls on
  D6, whose every-entry-flux-owned precondition then waits for C-2b. The observability edge
  already puts D5's tail after C-1.

Every other role M6 edits needs no edge:

- **authelia.** C-1's bypass removal is safe in either order against the route's prune. Each
  order leaves `uptime-kuma.local` under a tier at least as strict as the bypass.
- **loki-homelab (Wave A'), autofix-bridge, cloudflare-ddns, crowdsec, artifacts and B6's
  roles.** Their shadow PRs, their Cutover-b or Cutover-1b, their Cutover-2 and C-7 each change
  one side after a Cutover-a or Cutover-1a has removed the tile, so either plane can apply
  them. A B batch's `KUMA_RETIRING_TAGS` read and the two-hour bound read the applied commit,
  whoever applied it.
- **configarr and janitorr.** D5 flips each only after its H2 family PR (H2.5b, H2.5c) and its
  `k8s_host_files` conversion PR (`d5-bulk-move.md` item 26). H2.5b removes configarr's
  `install_host_lib.yml` and `stamp_deployed.yml` imports, and H2's janitorr PR does the same
  for janitorr. A role whose `kuma_push.py` pair C-3a must delete is therefore still
  Ansible-owned. A flux-owned one has no pair left, and its `/opt` copy is an absent arm in
  `k8s_host_files`, which the tick applies (C-3a).

If the index declines an edge, the fallback is the owner-independent form, at a cost. A
flux-owned monitor-bridge splits each remaining bridge-row Cutover-a in two: an exclusion in
the `CHECKS` loop of `static-monitors.yaml.j2` drops the tile first, and the row PR follows a
read that the tile is gone. A flux-owned observability or netpol-baseline moves `KumaDown`'s
removal, or the callers-policy change, into its own PR before C-1, each with a live read of
the step before it.

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
      (`deploy_cross_role.K8S_ROLES_IMPORTING_SETUP_FILES`).
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
  `ansible/tests/setup/test_health_cron_boot_grace.py` and
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
- **Every other producer (B1 to B7).** Three PRs, because the producer's plane (above) decides
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
      shadow PRs add those tags to `KUMA_RETIRING_TAGS` as well, so the refused pushes still
      inside the 3h window do not page once `HC_ROUTED_TAGS` loses them. So `monitor-bridge`
      applies by its owner, in either order against B5's hand apply on the Pi, and the
      class-2 residual in *Risks* covers the window between them.

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
- **`CheckWithoutPager`, a temporary rule.** It lives in its own group, `kuma-cutover`, in
  `files/rules/kuma-cutover.yaml`.
    - A rule that does not fire has no series, so PromQL cannot read its `shadow` label. The
      rule therefore lists the tiles itself. Each shadow PR adds one term per tile its batch
      retires, `absent(monitor_status{monitor_name="<tile>"})`. Each shadow-dropping PR
      (Cutover-b, Cutover-1b, A6-7b) removes its batch's terms. A term exists exactly while
      its rule is `shadow`, and it reads 1 once the tile is gone.
    - The expression joins the terms with `or`, then applies
      `unless on() (up{job="uptime-kuma"} == 0)`, with `for: 2h`. A Kuma outage fires no
      term, because Kuma's own liveness pager covers it. `absent()` carries the tile's name
      as a label, so the page names the tile. A misspelt tile name fires two hours after its
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
| A3 scrape | `targets`, `cluster_targets`, `shipper_dropped` | M2 | monitor-bridge | `CLUSTER_TARGETS_CONSECUTIVE` becomes `for: 10m`. Closes M1's, M4's and M5's handoffs: see *A3 and the handoffs it closes*. | 3 |
| A4 edge | `cert`, `traefik5xx`, `traefik_latency`, `traefik_404`, `traefik_421` | M2 | monitor-bridge | The volume floors (`TRAEFIK_MIN_RPS`) become `and` clauses. | 5 |
| A5 storage | `longhorn_volumes`, `pvc_fullness`, `snapshot_headroom`, `kubelet_plugin_readonly`, `etcd_db_size` | M2 | monitor-bridge | All critical. Longhorn's one-hot `state` label is selected explicitly. | 5 |
| A6 bridge exporter | the 15 other-source checks and `swallowed_verdicts`, plus the `b2_reachable` and `wan_reachable` gates | Exporter | monitor-bridge | See *The bridge as an exporter*. The `prometheus` gate retires with its token, because the Watchdog covers a dead Prometheus. `swallowed_verdicts` is exported until C-7 deletes it. | 19 |
| A7 log arm | `workload_log_errors` | M5 | monitor-bridge (`land.sh`); the rule deploys `loki-homelab` | `startup_grace` becomes `for: 5m`, as in A6. See *A7*. | 1 |
| B0 shared library | No check. Moves `boot_grace_active`, `wan_reachable` and `reachout_verdict` into `homelab-check-lib.sh`, which also gains `write_check_textfile` (M3's atomic write). Its copy task and its `stamp_deployed` record in `initial_setup/tasks/crons.yml` are `tags: [always]`, like the `kuma-push-lib.sh` pair beside them (*B0, B5, B7 and B8*). | — | the tick (initial_setup) on daniel-box; a hand on daniel-server and the Pi | — | 0 |
| B1 k3s crons | longhorn backup health, etcd snapshot, manifest-prune, release staleness, live-drift | M3 | a hand: `k3s-bringup.yml --tags "backup-health,etcd-snapshot,manifest-prune,release-staleness,live-drift"` (**operator-only**) | `KumaDown` replaces `uptime-kuma-alive` (see below). B1 retires the tiles and tokens of manifest-prune, release staleness and live-drift; D6.5 disarms those three checks and deletes B1's rules for them, and D6.5b deletes their code. | 5 |
| B2 initial_setup | docs-refresh, eval sweep, secret rotation, setup drift, the two Loki-route witnesses | M3 | the tick on daniel-box; a hand on daniel-server | daniel-server gets the textfile-directory task. | 6 |
| B3 gitops_deploy | ruleset drift, interaction limit | M3 | the tick | — | 2 |
| B4 other setup roles | two etcd drills, three fake_remux, two UPS secondaries, render_records, renovate_agent, renovate_notify | M3 | the tick on daniel-box; a hand on daniel-server for its UPS secondary | — | 10 |
| B5 Pi | SD-card health, container recovery | M3 | a hand (`-e target=daniel-pi`, **operator-only**) | Enables the textfile collector on the Pi (see below). | 2 |
| B6 k8s-role crons | crowdsec allowlists and AppSec check, artifacts sync, configarr health, janitorr health, telemetry health | M3 | `land.sh` (the five k8s roles) | They run on daniel-box's host, so M3's recipe applies as is. registry-gc is D5's (*Baseline*). | 7 |
| B7 pods | autofix-bridge; cloudflare-ddns direct and proxied; pi-peer-backup | Exporter, CronJob, a Prometheus rule plus a LogQL rule | `land.sh` | See *B7*. | 4 |
| B8 probes | the 14 hand-written monitors, plus one `tcp_connect` probe per Pi published port | M4 | `land.sh` | See *B8*. Cuts over the `pi_pressure` row. | 1 |

Phase A retires 77 push tokens: 41 from bridge rows and 36 from other producers. Phase C
retires the 78th, `kuma_status_page_sync_push_token`, together with Kuma's three other
secrets. The arithmetic: 83 at the baseline, less M5's 3, M3's 1 and D5's
`registry_gc_push_token`, leaves 78.

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
    - Its D5 row holds only "the PR that flips the last Ansible-owned stateless leaf", not
      littlelink's. If littlelink has flipped by then, B-1d's PR names the stateless leaf D5
      still holds back, and quotes its entry carrying no `owner: flux`.
    - The stall: D5's last stateless-leaf flip, and with it D5's Wave A, waits for most of
      Phase A. *Risks* names the operator's choice that follows.
- C-1 waits for D5's registry PR, which deletes the `Registry GC` tile and its push. Until
  then, C-1's gate of one `monitor_status` series cannot pass (Open question 13). The index
  does not carry this edge yet. C-1's quoted `registry_gc_push_token` grep and its gate
  enforce it inside this slice, so the missing edge causes no stall.

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
  survive until C-1. The task keeps its name, because R4's precondition test names it.
- Its test evaluates the role's own assert expression, the way
  `ansible/tests/services/test_kuma_push_token_format.py` evaluates the token-format assert.
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
      `bridge.streaks.apply_startup_grace` runs. Its rule's `for: 5m` carries the grace (*Gates
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
    - **`startup_grace` becomes `for: 5m`** ((`GRACE_CYCLES` − 1) × 300s, with `GRACE_CYCLES`
      at 2). The baseline shows the bridge applies that grace on every down cycle, so `for:`
      keeps today's timing. The exported value is taken before the grace from A6-1 on, so the
      grace never applies twice. A6-7a deletes `apply_startup_grace` and the `grace_streaks`
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
      in-body streaks, so `for: 5m` on their rules keeps that timing and adds no delay.
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
       The `startup_grace` family's rules carry `for: 5m`, and no other A6 rule has a `for:`
       (*Gates and grace*). The WAN and B2 PRs add a test that derives each `requires` label
       from the row's `gate` column, so a gated row whose rule lacks the label fails. The
       same test checks that each such rule shares a rule group with its gate's rule. It also
       derives each rule's `for:` from the same column: `for: 5m` for a `startup_grace` row,
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
- **B5, the Pi's textfile collector.** In `optimize_pi`, under the `node-exporter-host` tag:
    - replace `--no-collector.textfile` in `templates/node_exporter.service.j2` with
      `--collector.textfile.directory=/var/lib/node-exporter-textfile`;
    - add the directory task with M3's owner contract: owner root, group `{{ sys_user }}`,
      mode 2775, files written 0644.

  A test in `ansible/tests/setup/` asserts that the unit's flag and the directory task name the
  same path, and that the unit no longer carries `--no-collector.textfile`.
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
    - **pi-peer-backup** uses the CronJob recipe, which adds `cronjobs,jobs` to
      kube-state-metrics.
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
      without them. B-2 runs `fact_status.py verify` on each edited `CLAUDE.md` section.
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

### Phase C — delete Kuma

**C-0, the ADR (operator-only).** The agent writes "One alert plane on Prometheus and
Alertmanager; Uptime Kuma retired" under `docs/adr/` at the next free number when it is
written, as Proposed, with or before B-7. D6 and G2 also take numbers, so none is reserved
here. C-1 waits for the operator's acceptance.

1. **C-1, empty the role.** Preconditions, each checked and quoted in the PR:
    - the ADR is accepted;
    - every Phase A cutover, B8's cutover and M4's cutover have landed, and Phase A has run a
      week with no Kuma page;
    - `probe.py metric 'count by (monitor_type, monitor_name) (monitor_status)'` returns
      exactly one series, the `Status Page Sync` push tile. Any other series, of any type,
      fails the gate.
    - B-2 and B-4 have landed and deployed. B-4 removes homepage's
      `homepage_href('uptime-kuma', domain)` call, which raises once the entry loses
      `hostname`, and B-2 replaces postflight's Kuma reads.
    - `git grep -n -w uptime-kuma -- ansible ':!*/tests/*' ':!*.md' ':!ansible/roles/k8s/uptime-kuma/*'`
      lists only files C-1 edits and C-2a's grants: the four bespoke policies and
      ical-proxy's `netpol_from` in `host_vars/daniel-box.yml`. The grep uses `-w` rather
      than a quoted `'uptime-kuma'`, because an unquoted YAML value such as
      `service: uptime-kuma` is a reader too. C-1 handles any other hit first.

   Scope, under Ansible:
    - Delete `KumaDown` with its inhibit, Prometheus's `uptime-kuma` scrape job, the
      `kuma_api_key` file in `scrape-credentials-secret.yaml.j2`, and `postflight.py:check_kuma_scrape`. The scrape job
      calls `entry_port('uptime-kuma')`, so it must go before `port` does.
    - Delete the role's three tasks ahead of the include: "Read the AutoKuma declarations
      back as data", whose `lookup('template', ...)` reads `static-monitors.yaml.j2`, and the
      two asserts on `kuma_declarations` ("Check the declaration index is populated", as A0
      left it, and "Check every push token is in the format AutoKuma accepts").
    - Delete the seven templates, and add one inert template,
      `templates/tombstone-configmap.yaml.j2`. It renders one ConfigMap whose data says the
      role is retired, and its name holds no `secret`, so the secrecy derivation leaves it
      alone. The shared prune guard requires it:
        - `ansible/roles/k8s/manifests/defaults/main.yml:manifests_prune` is armed by default,
          and the uptime-kuma include passes no `manifests_prune: false`.
        - `ansible/roles/k8s/manifests/tasks/main.yml` holds the `always`-tagged assert
          "Check that a role arming the prune renders something to keep", which refuses an
          empty `manifests_files_resolved`. That list is the role's own templates, less the
          Secret files, plus `service.yaml` and `ingressroute.yaml` when the entry has `port`
          and `hostname`.
        - Without the tombstone the list is empty, and the deploy fails before the prune
          runs. With it, the armed prune deletes every labelled Deployment, Service, CronJob,
          ConfigMap and IngressRoute that the apply no longer declares.
    - Edit the include. Drop `manifests_secret_files`, which names the two deleted Secret
      templates and, as a passed list, wins whole. Set `manifests_rollout: ''`, so the drain
      does not wait on the Deployment the prune deletes. The role's defaults set no
      `manifests_extra_rollouts`, `manifests_self_rollouts` or
      `k8s_autodeploy_snapshot_pvcs`. `k8s_claims` stays until C-2b, so the claims stay
      declared.
    - Drop `port`, `hostname`, `use_authelia`, `auth_tier` and `netpol_from` from the entry, so
      the shared default Service and IngressRoute stop rendering and the prune deletes them.
      `netpol_from` goes in the same commit because `networkpolicy-callers.yaml.j2` renders
      `entry.port` for every entry with callers. Its seven callers are gone by then: Phase A
      stopped the five pushers, and this PR prunes the two CronJobs. The `# KUMA_URL`
      comments on that list in `host_vars/daniel-box.yml` go with it.
    - Delete the `uptime-kuma` row from `netpol_baseline_probe_targets` in the same commit,
      for the same reason. The row has no `port`, so `netpol-probe-job.yaml.j2` reads the
      entry's through `entry_port(t.service)`, which raises once `port` is gone. With the
      port patched in, the task "Fail if any probe target has no ready endpoint" would still
      fail the play, because this PR's `uptime-kuma` deploy prunes the Service. Left for
      C-2a, the row would fail every `netpol-baseline` deploy by any session from C-1's
      merge on. netpol-baseline is auto-deploy eligible, so a deployer-applied probe-image
      bump would fail too. The comment above the row's fencing-shape group drops
      `uptime-kuma`.
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
      maintenance CronJob go with the role.
    - Deploys, in the toposorted order `land.sh` uses: `authelia` (the bypass and the generated
      rule go, so `uptime-kuma.local` falls to the `*.local` wildcard tier for the minutes
      until its route is pruned; nothing pushes through that route by then), `observability`
      (so `KumaDown` is gone before Kuma), `uptime-kuma` (the prune deletes the Deployment, the
      Services, every ConfigMap but the tombstone, the CronJobs and the IngressRoute), then
      `netpol-baseline` last (its prune deletes the `uptime-kuma` callers policy).
    - **The dry run.** Before merging, the PR quotes
      `./scripts/deploy.sh --dry-run --tags "uptime-kuma,netpol-baseline"` run from its
      worktree. The prune guard is `always`-tagged, so it runs in a dry run. A green dry run
      shows that the emptied role passes it and that the probe Job renders without the row.
      The readiness task is `when: not k8s_no_mutate` and skips in a dry run, so the dry run
      proves the render only. The readiness half is safe because the row is deleted, not
      because the dry run read it.
    - **Tests.** `ansible/tests/k8s/test_netpol_probe_targets.py` runs in this PR's CI.
      `test_every_expecting_row_renders_a_leg` renders the probe Job and goes red on the
      `entry_port` raise, and the new named-member case holds the table to the entries.
    - **The health gate.** `land.sh`'s gate for `uptime-kuma` passes, half by half:
        - The rollout half reports nothing to gate. `health.py` treats a role with no
          Deployment, DaemonSet or StatefulSet that way
          (`scripts/diagnostics/probe_lib/health.py:DECIDED exit 0, a pass with a note`).
        - The alert half finds nothing that gates. `KumaDown` went in the earlier
          `observability` deploy. M5's `KumaNotificationDropped` is `critical` with
          `service: uptime-kuma`, but it is a loki-homelab rule, so it never reaches
          `ALERTS` and the gate cannot read it. `swallowed_verdicts`'s rule is a
          `warning` with `service: monitor-bridge` and no `verify`, so it gates no tag.
        - The probe half skips the tag, because its rendered entry has no route (Phase B
          item 1). The probe series that stopped when `observability` dropped the target
          therefore does not fail it.
    - After the deploys, the read-only
      `kubectl get svc,ingressroute,cm,cronjob,deploy -n homelab -l homelab/role=uptime-kuma`
      returns only the tombstone ConfigMap. The two claims and the two Secrets are never
      pruned (`manifests_prune_allowlist`). The operator deletes them after C-2b.

   C-1's four deploys are `land.sh` deploys because the index edges keep `observability`,
   `uptime-kuma` and `netpol-baseline` Ansible-owned until C-1 has deployed (*When D5 flips a
   role M6 edits*). `authelia` may be flux-owned, in which case the merge applies its half, in
   either order against the rest. C-1's PR quotes each of the three entries carrying no
   `owner: flux`. If the index declined the uptime-kuma edge and the role is `owner: flux` by
   then, C-1 takes D2's two-PR retirement path, which `d5-bulk-move.md` names for this case.
   Emptying the templates lets Flux prune everything except the claims, which C3 exempts, and
   the operator deletes those. C4 applies: Ansible must not touch it.
2. **C-2, the grants and the entry**, in two PRs:
    - **C-2a.** Delete `uptime-kuma` from the four bespoke policies and from ical-proxy's
      `netpol_from`. Deploy the roles, then `netpol-baseline` last, per its `CLAUDE.md`. The
      probe row went in C-1.
    - **C-2b.** Delete the role directory and its `containers_list` entry. Removing an entry
      moves every reader that iterates the list (*Baseline*, *Narrowing*), so record the
      narrowing's answer in the PR and follow *Steps* if it refuses. The tombstone template
      goes with the directory. After the deploy, the operator deletes with kubectl
      (**operator-only**) the objects no prune removes: the tombstone ConfigMap, the two
      claims and the two Secrets. No deploy re-creates them, because the entry is gone. C-6's
      PR quotes exit criterion 1's `kubectl get` returning nothing.
3. **C-3, the plumbing**, in two PRs:
    - **C-3a, the push libraries.** Delete `kuma-push-lib.sh`, and `kuma_push.py` with its
      re-export from `host_lib.py`. Delete the `kuma_push.py` rows from
      `deploy_cross_role.py`'s two tables, including the k8s row's `uptime-kuma` member. C-3a
      lands after C-2b, which deletes `uptime-kuma/files/push_heartbeat.py`, whose docstring
      names `kuma_push.py`. Every reader of `kuma_push.py` changes in the same commit, or a
      copy of a missing `src` fails the role that holds it:
        - `install_host_lib.yml` drops `kuma_push.py` from its loop and its header comment,
          and gains a `state: absent` task for the old copy beside each `host_lib.py`.
          `test_host_lib_sibling_copies.py` changes with it.
        - The eight `stamp_deployed` pairs for `kuma_push.py` go (*Baseline*, the push
          plumbing): `gitops_deploy`, `renovate_agent`, `renovate_notify`, `fake_remux`,
          `configarr`, `janitorr` and the two in `k3s/tasks/health-crons.yml`. The configarr
          and janitorr pairs go here only if H2.5b and H2.5c have not removed them already.
          D5 flips neither role before its H2 PR, so a pair C-3a deletes sits in an
          Ansible-owned role. If H2.5b landed first, configarr's CronJob ConfigMap carries
          `kuma_push.py` until B6's configarr Cutover-2 drops it, so that Cutover-2 precedes
          C-3a, and the precondition grep below covers its ConfigMap template.
        - The setup/common `CLAUDE.md` rows that name `kuma_push.py` are reworded. B7's
          autofix-bridge Cutover-2 has already reworded that role's line.
        - **Precondition, quoted in the PR.** B7's autofix-bridge Cutover-2 has removed the
          staging entry from `autofix-bridge/defaults/main.yml`, and C-1 has deleted the
          status-page sync's copy. `git grep -l kuma_push.py -- ansible scripts ':!*/tests/*'`
          lists only files this PR edits or deletes.
        - **Precondition, quoted in the PR.** Every host producer's Cutover-2 has applied on
          its own host. Three reads show it. First, `homelab_check_last_run_timestamp_seconds`
          for every B1 check, every daniel-server check (B2's witness, B4's UPS secondary) and
          every Pi check (B5) is newer than that batch's recorded Cutover-2 apply. That alone
          is also true of a shadow-phase script, which still sources the library. Second,
          `git grep -n /usr/local/lib/kuma-push-lib.sh -- 'ansible/roles/*/templates/*' 'ansible/roles/*/files/*'`
          prints no script that sources the file. Third, on each host, a read-only
          `grep -rl kuma-push-lib /usr/local/bin /usr/local/lib /opt` lists only the library
          itself: locally on daniel-box, and over `ssh` on daniel-server and the Pi.
        - **The host file.** `initial_setup/tasks/crons.yml`'s copy task is `tags: [always]`
          behind a `DECIDED` marker, and it runs on every host. C-3a replaces it with an
          `always`-tagged `state: absent` task for `/usr/local/lib/kuma-push-lib.sh`, and
          drops the `stamp_deployed` record task for `kuma-push-lib`. The tick applies it on
          daniel-box. On daniel-server it is the hand run `land.sh`'s note names, run on that
          host. On the Pi it is `initial_setup.yml --tags optimize_pi -e target=daniel-pi`
          (**operator-only**), because `always` rides on any tag. C-6 deletes the absent task
          once a read-only `ls /usr/local/lib/kuma-push-lib.sh` finds no file on daniel-box,
          and over `ssh` on daniel-server and the Pi.
        - **Deploys.** The tick applies `initial_setup`, `gitops_deploy`, `renovate_agent`,
          `renovate_notify` and `fake_remux` on daniel-box. `land.sh` deploys `configarr` and
          `janitorr` while each still imports `install_host_lib.yml`, which
          `deploy_cross_role.K8S_ROLES_IMPORTING_SETUP_FILES` defers and alerts on. Once H2
          has removed a role's import, C-3a does not edit it, and its `/opt` copy is H2's
          absent arm: in the role while it is Ansible-owned, and in `k8s_host_files`, applied
          by the tick, after D5's conversion PR. The `k3s` hand apply is
          `--tags "backup-health,live-drift"` (**operator-only**), the tags on the two `k3s` stamps and on the tasks that install
          `host_lib.py` beside the backup health reader and the live-drift check.
        - **The `/opt` copies.** C-6 deletes the `install_host_lib.yml` absent task once a
          read-only `ls /opt/*/kuma_push.py` finds no file on daniel-box and, over `ssh`, on
          daniel-server.
    - **C-3b, the timer rename.** Rename `kuma_check_timer.yml` to `check_timer.yml`, its units
      to `homelab-check-<name>`, its variables `kuma_check_name`, `kuma_check_on_calendar` and
      `kuma_check_state` to `check_timer_name`, `check_timer_on_calendar` and
      `check_timer_state` at every importer, and `test_kuma_check_timer.py` with them. Without
      the variable rename, every importer fails the census row. The task file
      gains a legacy-unit removal, mirroring its legacy-cron removal: stop, disable and delete
      `kuma-check-<name>.{service,timer}`, so the old and new timers never both run. The
      removal runs in both arms, not only the present arm. An absent import must also remove
      the old name, or a `kuma-check-*` unit that a hand apply has not yet removed outlives
      the rename (*When D6.5 or D6.5b has landed first*). A test case asserts each arm. The removal names `kuma-check-`, which exit criterion 3 forbids, so
      C-6 deletes it once a read-only `systemctl list-unit-files 'kuma-check-*'` lists nothing
      on daniel-box, daniel-server and the Pi. Update `SETUP_FILES_SHIPPED_BY_OTHER_ROLES`, `narrow_setup.py`,
      `setup_role_chains.py` and `test_setup_cross_role_files.py`, unless D6 has deleted them.
      Regenerate the glance blocks that name the timers, after fixing their reader:
        - `scripts/docs/catalog_lib/glance_facts.py:timer_units` finds a timer import only
          when the target ends in `common/tasks/kuma_check_timer.yml`. It reads the
          `kuma_check_*` variables and emits `kuma-check-<name>.timer`.
        - Unchanged, it finds nothing after the rename. The `regen-role-glance` hook would
          then rewrite the k3s, initial_setup, gitops_deploy and render_records blocks
          without their timers, and CI would agree with the generator (class 3).
        - C-3b points it at `check_timer.yml` and the new variables, and makes it emit
          `homelab-check-<name>.timer`. A named-member case in
          `scripts/docs/tests/test_gen_role_glance.py` asserts that the k3s role yields
          `homelab-check-manifest-prune.timer` and `homelab-check-live-drift.timer`, and it
          fails when the generator finds none.

      The rerun-while-red contract stays,
      because it still sets how fast a red check clears. The `k3s` hand apply is
      `--tags "manifest-prune,live-drift"`, the tags on its two timer imports.
    - **When D6.5 or D6.5b has landed first.** D6 opens after C-2b, so D6.5 and D6.5b can
      land before C-3a or C-3b. D6.5 disarms the two k3s timers and the live-drift check. It
      keeps the two timer imports and sets their `kuma_check_state` to `absent`. It turns the
      live-drift tasks into absent tasks that keep their `live-drift` tag. D6.5b deletes
      those imports and those absent tasks (#4609, #4635). Each PR therefore reads the k3s
      role at its base:
        - If D6.5 has set the two timer imports absent, C-3b still renames their variables.
          C-3b carries the literal `check_timer_state: absent` across the rename. Dropping it
          would re-arm the units that D6.5 disarmed. `glance_facts.py:timer_units` skips an
          import whose state is the literal `absent`, and C-3b keeps that skip under
          `check_timer_state`. Without the skip, the glance block would list two timers that
          are not installed. The named-member case therefore excludes the two k3s imports.
          It asserts that initial_setup yields `homelab-check-setup-drift.timer` instead. The
          `k3s` hand apply stays `--tags "manifest-prune,live-drift"`.
        - If D6.5 has merged but its hand apply has not run, C-3b can still merge first.
          D6.5's apply then runs on a tree that holds C-3b's rename, so the k3s absent arm
          is the renamed one. That arm removes the legacy `kuma-check-<name>` units as well
          as the `homelab-check-<name>` ones, so the apply still stops
          `kuma-check-manifest-prune` and `kuma-check-live-drift`. An absent arm that removed
          only `homelab-check-*` would leave both old units running behind a green recap,
          and nothing would catch them before D6.5b's step 4 (#4636).
        - If D6.5b has deleted the two timer imports, C-3b renames no k3s import and has no
          `k3s` hand apply. Its named-member case asserts the same initial_setup timer.
        - C-3a's `k3s` hand apply stays `--tags "backup-health,live-drift"` until D6.5b
          deletes D6.5's absent live-drift tasks. If D6.5b has deleted them, the apply is
          `--tags "backup-health"` alone.

      The PR body quotes `git grep -n -E 'kuma_check_timer|kuma_check_state|live-drift' ansible/roles/setup/k3s/tasks/`
      at its base to show which case applies. A `kuma_check_state: absent` line on a k3s
      timer import means that D6.5 has landed. No `kuma_check_timer` import means that D6.5b
      has landed.
4. **C-4, the names**, in two PRs:
    - **C-4a.** Add `ansible/filter_plugins/ingress_probes.py` with `ingress_probe_targets`,
      rename the `kuma:` key on `containers_list` entries to `probe:`, switch M4's probe job to
      the new filter, and add its row to `narrow_lib/containers.py:FILTER_FIELDS`. It also
      registers `ingress_probe_targets` in `scripts/lib/ansible_jinja_env.py`, beside the
      filters that file already imports, so a repo render of the probe job resolves it. No
      plugin is deleted, so the narrowing can answer (M4 open question 1).
    - **C-4b.** Delete `kuma_monitors.py`, which no template calls after C-4a. In the same
      commit, remove its import and the `kuma_ingress_monitors` registration from
      `scripts/lib/ansible_jinja_env.py`, which would otherwise raise ImportError on every
      repo render. Remove the `kuma_ingress_monitors` row from
      `scripts/deploy_tools/narrow_lib/containers.py:FILTER_FIELDS` too, unless D6 has
      deleted the narrowing. `narrow_lib/filters.py` refuses any deleted plugin, so this PR
      waits for D6, which deletes the narrowing. Until then the file, its registration and
      that row are on the allowlist.
5. **C-5, secrets.** **Operator-only:** remove every remaining push token,
   `uptime_kuma_username`, `uptime_kuma_password` and `prometheus_kuma_api_key` from
   `secrets.yml`. The same commit deletes their rows from `ansible/secret_rotation.yml`, adds
   them to `RETIRED`, and carries the regenerated fragment. That is the token-PR rule in
   *Steps*.
6. **C-6, the census row.** It holds `kuma` to its allowlist from then on (exit criterion 3).
   Its precondition, quoted in the PR, is the read for exit criterion 3's first bullet. It
   must list allowlist entries only:
   `git grep -l -E "/api/push/|kuma_push|KUMA_URL|KUMA_PUSH_|monitor_status|entry_port\('uptime-kuma'\)|kuma-push-lib|kuma_ingress_monitors|kuma-check-" -- ansible scripts .claude ':!*/tests/*' ':!*/test_*'`.
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
    - **Its deploys.** `observability`, `monitor-bridge` and `loki-homelab`, each by its
      owner: `land.sh` while Ansible-owned, the merge once `owner: flux`. loki-homelab is
      flux-owned from D5's Wave A', and monitor-bridge may be by then too. The row's gauge
      and `homelab_check_rows` leave together, so `BridgeCheckStale` never reads a short
      count, in either deploy order, and `KumaNotificationDropped`'s deletion is order-free.

### The tree

```diff
-ansible/roles/k8s/uptime-kuma/                        the whole role (3,655 lines), C-1 and C-2b
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
~  verdicts/logs.py                                    KUMA_RETIRING_TAGS (B1 to B7 shadow PRs); out with swallowed_verdicts (C-7)
~  checks/*.py, verdicts/*.py                          the Prometheus-reading bodies out (A1 to A5, B8); the log arm out (A7)
 ansible/roles/k8s/uptime-kuma/templates/
~  static-monitors.yaml.j2                             CHECKS loop out (A6-7a); B tiles out (each Cutover-1a)
~  status-page-sync-configmap.yaml.j2                  bridge pins out (A6-7a)
+  tombstone-configmap.yaml.j2                        C-1, out with the role in C-2b
 ansible/roles/k8s/observability/
+  files/rules/hosts.yaml, containers.yaml, scrape.yaml, edge.yaml, storage.yaml   A1 to A5
+  files/rules/bridge-exporter.yaml                    A6-2 to A6-6
~  files/rules/host-checks.yaml                        B1 to B6, two rules per producer; KumaDown (B1, out at C-1)
+  files/rules/pods.yaml, extra-probes.yaml            B7, B8
+  tests/rules/<each new group>.test.yaml
+  templates/alertmanager-ingressroute.yaml.j2         B-6b
~  templates/prometheus.yaml.j2                        probe jobs in (B8); uptime-kuma job out (C-1)
~  templates/scrape-credentials-secret.yaml.j2         kuma_api_key out (C-1)
~  templates/alertmanager-secret.yaml.j2               inhibit_rules for WAN and B2 (A6)
~  templates/kube-state-metrics.yaml.j2                cronjobs, jobs (B7)
 ansible/roles/k8s/authelia/templates/config-secret.yaml.j2   alertmanager two_factor (B-6a); kuma bypass out (C-1)
 ansible/roles/k8s/loki-homelab/files/rules/homelab-logs.yaml   WorkloadLogErrors (A7); cloudflare-ddns rule (B7); KumaNotificationDropped out (C-7)
 ansible/roles/k8s/netpol-baseline/defaults/main.yml   the uptime-kuma probe row out (C-1)
 ansible/roles/setup/common/tasks/install_host_lib.yml   kuma_push.py out of the loop, absent task in (C-3a), absent task out (C-6)
 ansible/roles/k8s/blackbox-exporter/files/blackbox.yml   tcp_connect, dns, keyword modules (B8)
 ansible/roles/setup/optimize_pi/templates/node_exporter.service.j2   textfile collector on (B5)
 ansible/roles/setup/initial_setup/files/
-  kuma-push-lib.sh                                    C-3a
+  homelab-check-lib.sh                                B0
 ansible/roles/setup/common/
-  files/kuma_push.py                                  C-3a
-  tasks/kuma_check_timer.yml                          C-3b, renamed
+  tasks/check_timer.yml                               C-3b
~  files/host_lib.py                                   the kuma_push re-export out (C-3a)
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
batches, B8); or Cutover-1a, Cutover-1b and Cutover-2 (B1 to B7); then the token PR a week
after the last cutover. A6 runs as A6-1 to A6-9, with the refactor A6-7r before A6-7a.
*The cutover order* above names each deploy.
Phase A is about 75 PRs, and A0 lands before any Kuma-side PR. Each shadow PR adds its
tiles' terms to the `kuma-cutover` group, and each shadow-dropping PR removes them. A PR
that removes the last term deletes the group, and a PR that adds a term to a missing group
re-creates it, with its test, in about 40 lines (*No-pager windows*).

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
| C-1 | empty the uptime-kuma role behind a tombstone ConfigMap, with its three pre-include tasks and the include's secret list and rollout; `KumaDown`, the scrape job and its credential; the Authelia bypass; the entry's routed fields and the probe row; the probe-row named-member case; a quoted `--dry-run --tags "uptime-kuma,netpol-baseline"`; after B-2, B-4 and D5's registry PR (a quoted `git grep -n registry_gc_push_token` over `ansible/roles/k8s/uptime-kuma/` returns nothing) | ~3,100 removed, ~40 added | `authelia`, `observability`, `uptime-kuma`, `netpol-baseline` |
| C-2a | the bespoke grants | ~55 removed | the edited roles, then `netpol-baseline` |
| C-2b | the role directory, the tombstone and the entry; then the **operator-only** delete of the tombstone, the claims and the Secrets | ~100 removed | per the narrowing |
| C-3a | `kuma-push-lib.sh` (an absent task on every host), `kuma_push.py`, the re-export, the `install_host_lib.yml` loop and an absent task for the `/opt` copies, the eight stamp pairs, the cross-role table rows; after B7's autofix-bridge Cutover-2 and after C-2b | ~500 removed | the tick (initial_setup, gitops_deploy, renovate_agent, renovate_notify, fake_remux on daniel-box); `land.sh` for `configarr` and `janitorr` only while H2 has not removed their `install_host_lib.yml` import (*When D5 flips a role M6 edits*); by hand on daniel-server and the Pi; `k3s --tags "backup-health,live-drift"` by hand |
| C-3b | the timer rename with the legacy-unit removal; the variable rename; `glance_facts.py:timer_units` and its named-member case | ~300 changed | the tick; `k3s --tags "manifest-prune,live-drift"` by hand |
| C-4a | the key rename, `ingress_probes.py` and its `ansible_jinja_env.py` registration | ~160 | per the narrowing |
| C-4b | delete `kuma_monitors.py`, its `ansible_jinja_env.py` import and registration, and its `FILTER_FIELDS` row, after D6 | ~70 removed | none once D6 has landed |
| C-5 | secrets (**operator-only** half first) | ~100 | none |
| C-6 | the census row, after the quoted identifier `git grep`; C-3a's two absent tasks out; after C-7 | ~60 | the tick (initial_setup and the `install_host_lib.yml` importers); `land.sh` for `configarr` and `janitorr` only while they import `install_host_lib.yml`; `k3s --tags "backup-health,live-drift"` by hand |
| C-7 | M5's `KumaNotificationDropped`; the `swallowed_verdicts` row with its bodies, rule, tests and `KUMA_RETIRING_TAGS` | ~350 removed | `observability`, `monitor-bridge`, `loki-homelab` |

B-2 and the old B-3 (postflight) are one PR, because `postflight.py` imports the modules B-2
deletes. Reverting B-2 restores both together.

**Two-writer check.** For every Phase B and C PR, no check changes its pager: Phase A moved
every pager already. The C-1 gate (exactly one `monitor_status` series, the status-page sync
tile) refuses a C-1 that would remove a live pager, whatever the pager's monitor type.

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
# Before C-1: exactly one series, the status-page sync tile
uv run python scripts/diagnostics/probe.py metric 'count by (monitor_type, monitor_name) (monitor_status)'
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
- `KUMA_RETIRING_TAGS` (each B1 to B7 shadow PR): a `rejected` line from a listed tag returns
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
- B5: the unit-flag-and-directory test, red on a unit that keeps `--no-collector.textfile`.
- The B-6a render test: `alertmanager.local.<domain>` resolves to `two_factor` at first
  match, and it fails if the rule sits behind the `*.local` wildcard.
- C-3b: two test cases in which a host carries a `kuma-check-<name>` unit, one per arm.
  The present arm removes it, and so does the absent arm. Also the glance case that fails when `timer_units` finds no
  `homelab-check-*` timer in the k3s role.
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
      history is gone either way. A revert of C-1 restores `KumaDown` with the scrape job, and
      the probe row with the entry's `port` in one commit, so the probe Job renders again.
    - After C-5, restoring Kuma means new tokens: a rebuild, not a revert. C-1 therefore waits
      until Phase A has run a week with no Kuma page.
    - Reverting C-3a restores both libraries, the loop entry and the stamp pairs. Each
      consumer copies `kuma_push.py` again on its next apply, through the same planes C-3a
      names. Nothing pushes through it by then, because every Cutover-2 has applied.
    - Reverting C-3b alone would leave the `homelab-check-*` units running beside the
      restored `kuma-check-*` ones, because the restored task file does not know the new
      names. The revert therefore adds a removal of the `homelab-check-*` units.
- **A0.** Revert it only while no Kuma-side PR has landed. After the first one, the restored
  numeric floor can refuse every `uptime-kuma` deploy.
- **A partial C-1.** If `observability` deployed and `uptime-kuma` has not, Kuma runs
  unwatched and unscraped. Nothing pages from it, because every pager moved in Phase A.
  Re-run the `uptime-kuma` deploy. The tombstone keeps the prune guard satisfied, and the
  dry run quoted in the PR showed the emptied role passes it. If the deploy still fails,
  revert C-1 whole, which restores `KumaDown`, the scrape job and the role. If `authelia` deployed and nothing after it did, Kuma's
  route sits under the `*.local` wildcard tier, which is stricter than the deleted bypass, and
  Kuma still pages. Finish the run.

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
  Cutover-2's `monitor-bridge` deploy until its push stops and the 3h window passes. Either
  needs a network fault during a DOWN verdict inside a same-day window.
- **Class 5, a sequence treated as a transaction.** Phase C deletes in an order that the
  prune, the claims and the grants each constrain. Mitigation: the C-1 gate, the deploy order
  inside C-1, the two-PR role deletion, and `netpol-baseline` deploying last.
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
  eight stamp pairs, one loop entry and one pod staging entry. Mitigation: C-3a's quoted
  `git grep -l kuma_push.py` precondition and its deploy list.
- **The token registry blocks a retirement commit.** `sync` keeps stale rows, so a retirement
  that only adds `RETIRED` fails prek and CI. Mitigation: the four-part token commit in
  *Steps*, with `audit --check` quoted.
- **Class 6, restart is not recreate.** Its one detector is `pi_pressure`'s port arm. B8
  replaces it with `PiPortDead`, and `docs/failure-classes.md` row 6 moves to that rule's
  firing case in the same PR, so `ansible/tests/repo/test_failure_class_detectors.py` keeps
  resolving.
- **An `always`-tagged guard refuses a role that renders nothing.** C-1 empties a role whose
  prune is armed, and the shared guard refuses an empty file list, in a dry run too.
  Mitigation: the tombstone ConfigMap, the include edit, and the dry run C-1 quotes. A0
  removes the numeric declaration floor that Phase A's shrinking index would cross.
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
- **Registry GC still pushes to Kuma until D5's registry PR.** C-1's gate of one
  `monitor_status` series refuses a C-1 that would delete that live tile. Mitigation: C-1
  waits for D5's PR and quotes the `registry_gc_push_token` grep. If D5's PR stalls, Kuma
  stays up with one push tile and the status-page sync.
- **D5 flips an M6 target role mid-slice** (class 5). C-1 waits for a Wave D conversion PR,
  so Phase C, and the late B6 and B7 batches, run while D5 flips the roles they edit. A flip
  moves the role's apply from `land.sh` to Flux, and three M6 steps rely on the toposort
  inside one `land.sh` run. Mitigation: *When D5 flips a role M6 edits* names the three, and
  index sub-edges keep monitor-bridge, observability, netpol-baseline and uptime-kuma
  Ansible-owned until each step has deployed. C-1's PR quotes the three entries without
  `owner: flux`. Every other M6 PR changes one pager side, so either owner can apply it.
  Residual: D5 runs Wave D in the order of its wave table, so holding monitor-bridge holds
  every role listed after it until A6-7a, and holding observability holds them until C-1. A
  cross-slice note asks D5 to let only the held roles, with headlamp behind observability,
  leave the table order. If the index declines the edges, the fallback in that section costs
  one extra PR per remaining bridge-row Cutover-a and one before C-1.
- **Homepage loses a familiar tile.** Mitigation: the replacement tile shows the same three
  counts in the same grid slot. The *Homepage* `CLAUDE.md` note on positional block labels
  applies.

## Tests and docs that change in the same PR

Each PR that edits a cited path also edits the `CLAUDE.md` section citing it, and runs
`uv run python scripts/dev/fact_status.py verify '<doc>#<heading>'` (or `forget` for a removed
heading), unless G2 has retired `docs/facts.lock` by then. A PR that changes a role's tasks,
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
  test, and `monitor-bridge` applies by its owner, in either order against the Pi's hand
  apply.
- **B5.** The unit-flag-and-directory test in `ansible/tests/setup/`, and the `optimize_pi`
  `CLAUDE.md` line about the health crons sourcing `kuma-push-lib.sh`.
- **B1 to B7, each shadow PR.** The batch's `KUMA_RETIRING_TAGS` entries and their cases in
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
  add-secret skill's `kuma-drift` steps change here, with `fact_status.py verify` on the
  edited `CLAUDE.md` section.
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
    - C-1: `ansible/tests/services/test_kuma_push_token_format.py`, which evaluates the
      deleted token-format assert, is deleted, with A0's floor test. Every other
      `test_kuma_*.py` in `ansible/tests/services/` that reads a deleted template or task,
      such as `test_kuma_static_monitors.py` and `test_kuma_entities_parse_for_autokuma.py`,
      is deleted or cut to what still exists.
    - C-3a: `test_setup_cross_role_files.py`, `test_host_lib_sibling_copies.py`, and the
      setup/common `CLAUDE.md` rows for `kuma_push.py`.
    - C-3b: `test_kuma_check_timer.py` (in `ansible/tests/setup/`) is renamed with its task
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
    - 78 push tokens and 3 Kuma secrets, with their rotation-registry rows.
      `registry_gc_push_token` is not among them, because D5's registry PR retires it.
    - M5's `LokiHomelabDown`, which A3's scrape-target rule covers.
    - `uptime-kuma-alive` in Healthchecks.io, and one hand-written Authelia bypass.
    - The `swallowed_verdicts` row, its bodies and `KUMA_RETIRING_TAGS` (C-7), and M5's
      `KumaNotificationDropped`.
- **Added:**
    - About 75 rules with their tests, including `KumaDown` (deleted again in C-1).
    - The bridge exporter loop, about 120 lines, with the `homelab_check_rows` gauge, and the
      autofix-bridge exporter.
    - `KUMA_RETIRING_TAGS`, one set that grows over B1 to B7 and goes in C-7.
    - `homelab-check-lib.sh`, about 60 lines.
    - Four blackbox modules and one `tcp_connect` target per Pi publisher.
    - `health_alerts.py`, about 150 lines.
    - Two census tests, one Alertmanager route, seven Alertmanager inhibits (A1's; A3's for
      `BlackboxExporterDown`, `KumaDown`, `MonitorBridgeDown` and `AutofixBridgeDown`, the
      `KumaDown` one deleted again in C-1; and A6's for `WANUnreachable` and
      `B2Unreachable`) with one config test, one
      hand-written Authelia rule, and one ADR.
    - One tombstone ConfigMap, from C-1 until C-2b and the operator's delete.
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
    - C-1 after D5's registry PR (Open question 13);
    - D5's monitor-bridge flip after A6-7a, live; D5's observability and netpol-baseline
      flips after C-1, live; D5 never flips uptime-kuma, which leaves at C-2b (*When D5 flips
      a role M6 edits*).

   No row gate sits before the first Cutover-1a of B1 to B7. contracts.md's *Round 2 —
   integration review (X2-series)* entry *Applied with no contract text.* records that M5's
   handoff leaves none.

   The new D5 edges add no cycle, which the index states the way it does for B-1e. A6-7a
   waits on Phase A batches and on M2 to M5, and none of those waits on D5. C-1 waits on D5's
   registry conversion PR, which precedes the held flips and waits on none of them.
   C-4b waits on D6, and D6 waits on D5's tail, but nothing from C-1 to C-7 waits on C-4b.
   D6's every-entry-flux-owned precondition then waits for C-2b, through the uptime-kuma
   edge.

   **Applied on 2026-10-16.** The slice index carries M6a, M6b with the B-1 exception, and
   M6c, with every sub-edge above and the no-cycle paragraph. No gate was named.
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
   gain, and C-3b needs a legacy-unit removal to be safe. Resolution: C-3b renames them. If the
   operator prefers, the names stay, C-3b shrinks to the task-file rename, and `kuma-check-`
   joins the census allowlist.
7. **The Pi's AutoKuma labels.** `ansible/templates/autokuma.yml.j2` and the `kuma(...)` labels
   in the compose templates of `alloy`, `autoheal`, `docker-proxy` and `wg-easy` are inert once
   Kuma is gone. Changing them recreates every Pi container, which is a Pi deploy this slice
   does not schedule. Resolution: H5 drops them when it rewrites the Pi compose files. Until
   then they stay on the census allowlist.
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
    one's expression and keeps its title. C-1 deletes both with the declaration lookup.
    Resolution: C-1 lands first, and R4's test drops the two names. R4 waits on D6, and D6's
    every-entry-flux-owned precondition waits for C-2b through the uptime-kuma edge (Open
    question 1). If the index declines that edge, whichever slice lands second drops the
    tasks: an R4 PR 4 that lands first moves A0's named-member form, and C-1 deletes the
    moved copies.
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
13. **Registry GC belongs to D5.** D5's Wave D registry PR retires the GC, its tile and its
    token, and D5's Open question 15 names D5 the only owner. Resolution: B6 drops
    registry-gc, and M6 counts 78 tokens. C-1 waits for D5's registry PR, which the index
    should carry as a sub-edge. If the operator wants Kuma gone before D5's Wave D, the
    fallback is a B6 PR that moves registry-gc under M3's recipe. D5's registry PR then
    deletes that textfile rule instead of the tile. Whichever lands second quotes
    `git grep -n -i 'registry.gc'` over `ansible/roles/k8s/uptime-kuma/` and
    `ansible/roles/k8s/observability/` to show it removed the other's copy.
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
  `fact_status.py verify`. The facts lint's `vanished-identifier` rule would block B-2
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
- **#4609 (medium): B1 and D6.5 both retired the three drift checks' tiles and tokens.**
  B1 converts manifest-prune, release staleness and live-drift to textfile checks, and
  D6.5 removed the same tiles and tokens before deleting the checks. Neither page cited the
  other. **Fixed by giving the Kuma side to B1 and the checks to D6.** X2-13's remedy, which
  dropped registry-gc from B6, does not fit here. C-1 needs exactly one Kuma series, so the
  three tiles must be gone before C-1. D6 waits for C-2b through the uptime-kuma edge
  (*When D5 flips a role M6 edits*), so D6.5 cannot remove them first. B1 therefore keeps
  all three, and the B1 row and *Other producers* name D6.5 and D6.5b as the slices that
  delete the checks. D6.5 deletes B1's `<Check>Failed` and `<Check>NotReporting` rules for
  them in the PR that disarms their crons. D6.5 can also land before C-3a or C-3b. C-3b
  therefore gains *When D6.5 or D6.5b has landed first*: each PR reads the k3s role at its
  base, and C-3b's named member falls back to initial_setup's setup-drift timer.
- **#4635 (medium): *When D6.5 has landed first* assumed that D6.5 deletes the k3s timer
  imports and the live-drift tasks.** D6.5 keeps the imports and sets their
  `kuma_check_state` to `absent`. It turns the live-drift tasks into absent tasks that keep
  their tag. D6.5b deletes both. The section's fallback therefore never triggered between
  D6.5 and D6.5b. In that window C-3b renamed imports whose units were already absent, and
  its named member asserted two timers that were not installed. Nothing told C-3b to carry
  the absent state across the variable rename, so the rename could re-arm the units that
  D6.5 disarmed. **Fixed.** The section is renamed *When D6.5 or D6.5b has landed first*, and
  its conditions name D6.5b as the PR that deletes. C-3b carries `check_timer_state: absent`
  across the rename and keeps `timer_units`'s skip of a literal `absent`. Its named member
  excludes those imports. The PR body's grep adds `kuma_check_state`, so it shows which
  case applies. The B1 row and *Other producers* say that D6.5 disarms the checks.

- **#4636 (medium): C-3b could merge between D6.5's merge and D6.5's hand apply.** The
  apply then ran on the renamed task file, whose absent arm removed only `homelab-check-*`
  units. The `kuma-check-manifest-prune` and `kuma-check-live-drift` units kept running after
  a green apply, until D6.5b's step 4 or C-6's `systemctl list-unit-files 'kuma-check-*'`
  caught them. A merge gate on C-3b was rejected: it depends on a session reading
  `gitops_state.py` at the right moment, and it still misses an apply that runs late.
  **Fixed.** C-3b's legacy-unit removal runs in both arms, with a test case per arm, and
  *When D6.5 or D6.5b has landed first* names the ordering. C-6 deletes the removal on the
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
