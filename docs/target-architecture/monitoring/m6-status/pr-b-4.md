### PR B-4

PR B-4 ([#6798](https://github.com/DanielH2018/server/issues/6798)) replaces Homepage's
Uptime-Kuma tile with an Alertmanager tile in the same grid slot. The tile is a
`prometheusmetric` widget with three blocks, each read from Prometheus `ALERTS`:

- **Critical** counts firing `severity="critical"` alerts.
- **Warning** counts firing `severity="warning"` alerts.
- **Watchdog** counts the firing Watchdog, so it reads 1 while the alert plane evaluates.

The two severity counts drop `shadow="true"` and `drill="true"`, the two matchers the routing
tree sends to its blackhole receiver. The tile links to `https://alertmanager.local.<domain>`,
the `two_factor` UI from B-6b. The widget names its blocks with `label:`, so the two Kuma
heading renames in `custom.css.j2` are deleted rather than moved. Homepage no longer calls
`homepage_href('uptime-kuma', domain)`, which C-1a needs gone.

Before the merge, `probe.py metric` returned 0, 0 and 1 for the three queries. The Homepage
pod reaches Prometheus through the `prometheus-callers` NetworkPolicy, which names
`app=homepage` in the `homelab` namespace on port 9090; the Headlamp tile already reads the same
ClusterIP. The verify-by read through the `homelab-ui` MCP server waits for the promotion.
