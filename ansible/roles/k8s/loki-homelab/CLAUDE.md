# loki-homelab — the cluster log store (separate from claude-otel's Loki)

Grafana Loki plus a Grafana Alloy DaemonSet that ships every pod's logs to it. Deliberately
separate from the `claude-otel` Loki (decision KL1, `docs/archive/.../slice-7-phase-d-loki.md`)
— merging would give the verbatim-prompts store this role's LAN route, which it must not have.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "loki-homelab"`
- **Images:** `grafana/loki` (`loki_homelab_image`), `grafana/alloy`
  (`loki_homelab_alloy_image`)
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

## A departed pod's logs survive it — an empty result is usually the query

Loki keeps what a pod wrote after the pod is deleted, rescheduled or lost to a node reboot.
Measured 2026-09-10 over a 2-day window on `{namespace="homelab"}`: dozens of ReplicaSet
generations that no longer exist still answer — `artifacts-7989779d8b-29kwl`,
`authelia-558c86567-fv9cr`, eight `homepage-*` pods — several with lines continuing past their
successor's first line. `speedtest-685455d6d9-fb847`, the pod #1604 reported as holding
nothing, answers with its `Pinged hostname` line stamped `2026-09-09 11:00:04`.

So an empty result for a pod that went away is a claim about the query, not about the pod,
until you have ruled out both of these:

- **`probe.py loki-query` looks back one hour unless you pass `--since`.** Loki's own default,
  not the tool's. Anything older returns nothing, which reads as "the pod logged nothing".
- **`--limit` returns the NEWEST N lines, not the first N.** The query runs
  `direction=backward` (`probe_lib/metrics.py`), and the default is 100. A selector matching a
  busy stream alongside a quiet one spends the whole budget on the busy one, and the quiet
  pod's older lines never appear. Pin the pod in the selector, or raise `--limit`.

What is genuinely not durable is narrower than "the pod's logs": a line the container writes
in the seconds between Alloy's last read and the node going down. Nothing here bounds that
window, and no measurement in this repo has sized it.
