# loki-homelab — the cluster log store (separate from claude-otel's Loki)

Grafana Loki plus a Grafana Alloy DaemonSet that ships every pod's logs to it. Deliberately
separate from the `claude-otel` Loki (decision KL1, `docs/archive/.../slice-7-phase-d-loki.md`)
— merging would give the verbatim-prompts store this role's LAN route, which it must not have.

## At a glance
- **Images:** `grafana/loki:3.7.6` + `grafana/alloy:v1.19.2` (`loki_homelab_image`,
  `loki_homelab_alloy_image`), both Renovate-tracked.
- **Deploy tag:** `--tags "loki-homelab"`.
- **Route:** `loki-homelab.<domain>` — no Authelia, public — plus a separate `ingressroute-push`
  router that is daniel-pi's push-only door.
- **Persists:** `loki-homelab-data` (`longhorn-nobackup`, 5Gi) — logs, `744h` retention; treated
  as bulky and reconstructible in spirit, so the retention window is the real bound, not backup.
- **`k8s_autodeploy: false`** — observability: the Alert History reconstruction and every
  LogQL-backed monitor-bridge check read from this store, and `Recreate` + a PVC compounds the
  risk of a broken deploy going unpaged.

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
