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
