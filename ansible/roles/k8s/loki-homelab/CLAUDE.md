# loki-homelab — the cluster log store (separate from observability's Loki)

Grafana Loki plus a Grafana Alloy DaemonSet that ships every pod's logs to it. Deliberately
separate from the `observability` Loki (decision KL1, `docs/archive/.../slice-7-phase-d-loki.md`)
— merging would give the verbatim-prompts store this role's LAN route, which it must not have.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=loki-homelab` with exit 5
- **Images:** `grafana/loki` (`loki_homelab_k8s_image`), `grafana/alloy`
  (`loki_homelab_k8s_alloy_image`)
- **Route:** `loki-homelab.local.<domain>` (LAN only), no Authelia
- **Claim:** `loki-homelab-data` (no backup (StorageClass longhorn-nobackup))
<!-- /generated_from -->

- **A separate `ingressroute-push` router** is daniel-pi's push-only door.
- **Persists:** `loki-homelab-data` (`longhorn-nobackup`, 5Gi) — logs, `744h` retention; treated
  as bulky and reconstructible in spirit, so the retention window is the real bound, not backup.
- **The monitors that read from here:** the Alert History reconstruction, the ruler's own
  rules and monitor-bridge's remaining LogQL checks. A broken apply blinds the first and the
  last without paging.
- **Flux applies it, not Ansible** (D5.8, Wave A'). Its `containers_list` entry carries
  `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a merged
  change reaches the cluster when `promote` publishes and verifies the next bundle. Its
  image-bump PRs therefore draw no `land-wait` and no `probe.py health` run. To give it back to
  Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects. traefik stays Ansible-owned, so the Kustomization
  records it in its `homelab/ansible-owned-deps` annotation, not in `dependsOn`. The Loki
  Deployment is `Recreate`, so a change that rolls it takes log queries, pushes and the ruler
  down briefly.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`,
  and no Flux path runs it. It stays for the handback. Its `manifests_rollout: loki-homelab`
  and the `alloy` entry in `manifests_extra_rollouts` named the two workloads for the
  stabilisation gate and the shared restart. The Kustomization's `wait` replaces the gate, and
  the restart checksum the renderer stamps replaces the restart. `k8s_autodeploy: false` no longer
  gates an apply, because the deployer skips the tag. It still stops Renovate automerging the
  image bump.
- **Flux prunes the cluster-scoped RBAC.** The task's comment says the namespaced prune
  allowlist in `k8s/manifests` never selects the Alloy ClusterRole and ClusterRoleBinding.
  Flux prunes by its own inventory instead, and that inventory holds both, so dropping one
  from `alloy-daemonset.yaml.j2` deletes it live at the next promotion.
  `deletionPolicy: Orphan` applies only when the Kustomization itself is deleted.

## Notable
- **The renderer's restart checksum rolls each workload when a ConfigMap it mounts changes.**
  The Loki Deployment mounts `loki-homelab-config` and `loki-homelab-rules`. The Alloy
  DaemonSet mounts `loki-homelab-config` only, so a config edit rolls both and a rules edit
  rolls Loki alone.
- Alloy replaced Promtail on 2026-09-02 (Promtail reached end of life 2026-03-02).
- **The `job` label values are not this role's to choose.** They render from `loki_streams` in
  `group_vars/all.yml`, shared with the Pi's Alloy and with every selector that reads them
  (#3740). `ansible/tests/services/test_loki_stream_labels.py` fails when an emitted value
  and the consumers' values drift apart.
- **Two `stage.replace` stages redact credentials on the way in:** Home Assistant's cast
  `refresh_token`, which HA logs on a failed cast (issue #3015), and Prowlarr's `apikey=` in the
  *arr Torznab error URLs (#6276). Each substitutes the capture group only, so the rest of the
  record survives. `ansible/tests/k8s/test_alloy_redacts_ha_refresh_token.py` and
  `ansible/tests/k8s/test_alloy_redacts_arr_apikey.py` fail on either half. The stage is
  prospective: a record shipped before it still carries the token for the 744h retention window.
- **Host lines carry a `program` label, and the line itself is untouched.** The
  `loki.process "host"` stage extracts the rsyslog tag from syslog and authlog lines (#6163).
  `HostLogErrors` counts syslog's fatal-pattern lines per machine and program over 6h, and
  `HostOutOfMemoryKill` pages on the first host-wide OOM kill. The stage runs
  only `stage.regex` and `stage.labels`, because probe.py and the alert-history dashboard parse
  the raw rsyslog line. `ansible/tests/k8s/test_alloy_labels_host_program.py` fails on a missing
  label or an added stage. A `uv run` cron logs under the tag `uv`, so the line names the script.
- **k3s's own log ships warnings and worse only, as `{job="k3s"}` on each node** (#6177).
  `/var/log/k3s.log` is kept out of the journal and syslog on purpose, and Alloy tails the file
  directly. The `loki.process "k3s"` stage drops every line without a warn, error or fatal
  level, and it drops kube-router's per-sync IPv6 ipBlock warning by name. That warning was
  95% of the remaining volume. An empty stream on a quiet day is the filter working, so nothing
  watches this stream for freshness. `ansible/tests/k8s/test_alloy_ships_k3s_log.py` runs the
  filter over real line shapes and holds the path to setup/k3s's `k3s_log_file`.
  **A selector is a double-quoted LogQL string, never a backtick one.** Alloy's match-stage
  parser rejects a backtick string that Loki accepts, and that crash-looped the DaemonSet on
  2026-10-29. The `validate-alert-rules` hook boots the pinned Alloy on the rendered config
  (`scripts/validate/alert_rules.py:run_alloy_load`), because `alloy validate` never builds
  the stages and so accepts the bad selector.

## Ruler

Loki's ruler evaluates the LogQL rules in `files/rules/` and sends each firing alert to
Alertmanager in the observability namespace, on its Service at port 9093.

- **The tenant directory is a ConfigMap mount.** `templates/rules-configmap.yaml.j2` renders
  the `loki-homelab-rules` ConfigMap, one key per group in `loki_homelab_rule_groups`. The
  Deployment mounts it at `/etc/loki-rules/fake`, because `fake` is the tenant Loki uses when
  `auth_enabled` is false. The ruler's scratch copy, `rule_path`, sits on the claim.
- **A new rule file joins the list in the same commit.** The template names each file rather
  than globbing, so a file the list omits never reaches the ruler. The `validate-alert-rules`
  hook fails when the files, the list and the rendered keys disagree, and it parses every
  expression with `lokitool`.
- **The group interval sets the query load.** `homelab-logs` evaluates every 5m, the
  monitor-bridge `INTERVAL`, rather than Loki's 1m default. Each evaluation is a range query
  on this Loki.
- **Every rules edit restarts Loki.** The Loki Deployment mounts the rules ConfigMap, so the
  renderer's restart checksum moves and the `Recreate` pod rolls, which restarts the ruler.
  Alloy does not mount it and does not roll. When a config edit rolls Alloy, it resumes from
  its positions file, so a restart costs an ingestion pause, not lost lines.
- **The ruler's alerts never reach Prometheus.** It writes `ALERTS` only to a remote-write
  target, and Prometheus runs without a remote-write receiver. So `probe.py health
  loki-homelab` cannot see a log rule's alert; read the ruler's own `loki_prometheus_*`
  metrics instead.

## A departed pod's logs survive it — an empty result is usually the query

Loki keeps what a pod wrote after the pod is deleted, rescheduled or lost to a node reboot.
Measured 2026-09-10 over a 2-day window on `{namespace="homelab"}`: dozens of ReplicaSet
generations that no longer exist still answer — `artifacts-7989779d8b-29kwl`,
`authelia-558c86567-fv9cr`, eight `homepage-*` pods — several with lines continuing past their
successor's first line. `speedtest-685455d6d9-fb847`, the pod #1604 reported as holding
nothing, answers with its `Pinged hostname` line stamped `2026-09-09 11:00:04`.

So an empty result for a pod that went away is a claim about the query, not about the pod,
until you have ruled out the query's window and its line budget. `probe.py loki-query --help`
gives each default under `--since` and `--limit`, and says how each one returns nothing for a
pod that did log.

What is genuinely not durable is narrower than "the pod's logs": a line the container writes
in the seconds between Alloy's last read and the node going down. Nothing here bounds that
window, and no measurement in this repo has sized it.
