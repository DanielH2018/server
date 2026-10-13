# loki-homelab — the cluster log store (separate from observability's Loki)

Grafana Loki plus a Grafana Alloy DaemonSet that ships every pod's logs to it. Deliberately
separate from the `observability` Loki (decision KL1, `docs/archive/.../slice-7-phase-d-loki.md`)
— merging would give the verbatim-prompts store this role's LAN route, which it must not have.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "loki-homelab"`
- **Images:** `grafana/loki` (`loki_homelab_k8s_image`), `grafana/alloy`
  (`loki_homelab_k8s_alloy_image`)
- **Route:** `loki-homelab.local.<domain>` (LAN only), no Authelia
- **Claim:** `loki-homelab-data` (no backup (StorageClass longhorn-nobackup))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — observability — log store other
  monitors read from; a broken deploy blinds them without paging. ALSO Recreate + PVC on its
  own log data
<!-- /generated_from -->

- **A separate `ingressroute-push` router** is daniel-pi's push-only door.
- **Persists:** `loki-homelab-data` (`longhorn-nobackup`, 5Gi) — logs, `744h` retention; treated
  as bulky and reconstructible in spirit, so the retention window is the real bound, not backup.
- **The monitors that read from here:** the Alert History reconstruction and every
  LogQL-backed monitor-bridge check — which is what "blinds them" in the denylist reason
  means.

## Notable
- The Alloy shipper (`manifests_extra_rollouts`, kind `daemonset`) rolls whenever this role's
  ConfigMap changes, alongside the primary `loki-homelab` Deployment `manifests_rollout` names.
- Alloy replaced Promtail on 2026-09-02 (Promtail reached end of life 2026-03-02).
- **The `job` label values are not this role's to choose.** They render from `loki_streams` in
  `group_vars/all.yml`, shared with the Pi's Alloy and with every selector that reads them
  (#3740). `ansible/tests/services/test_loki_stream_labels.py` fails when an emitted value
  and the consumers' values drift apart.
- **One `stage.replace` redacts a credential on the way in:** Home Assistant's cast
  `refresh_token`, which HA logs in plaintext on a failed cast (issue #3015). It substitutes the
  capture group only, so the `_handle_signal_show_view` marker the fault is detected by survives.
  `ansible/tests/k8s/test_alloy_redacts_ha_refresh_token.py` fails on either half. The stage is
  prospective: a record shipped before it still carries the token for the 744h retention window.

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
- **Every rules edit restarts Loki and Alloy.** The rules ConfigMap is part of this role's
  rendered manifests, so the shared rollout restarts the `Recreate` Loki Deployment and the
  Alloy DaemonSet. Alloy resumes from its positions file, so a restart costs an ingestion
  pause, not lost lines.
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
