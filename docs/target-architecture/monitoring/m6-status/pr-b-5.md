### PR B-5

B-5 ([#6819](https://github.com/DanielH2018/server/issues/6819)) changes two Grafana boards
in the `observability` role.

- `Apps/uptime-kuma-monitors.json` is deleted, with its line in
  `observability_dashboard_folders`.
- `Infrastructure/alert-history.json` reads Prometheus's `ALERTS` series instead of the Loki
  reconstruction of Kuma DOWN pushes. Every panel filters on firing state, leaves out
  `Watchdog`, and groups by `service` through a `$service` variable built on
  `label_values(ALERTS, service)`. The two tables carry the `shadow` label, so an alert whose
  batch has not cut over stays visible and reads as not paged by Alertmanager.
- `ansible/tests/services/test_alert_history_board_reads_alerts.py` holds every panel query to
  that shape. It replaces the test that parsed the bridge's DOWN line out of the old board.

The board's history spans Prometheus's 30-day retention, not Loki's 744 hours. Kuma episodes
from before a batch's cutover show only as far back as its shadow rule has fired.

Before the PR, the panel queries ran read-only against live Prometheus through
`probe.py metric`. Every `ALERTS` series carried a non-empty `service` label
(`count(ALERTS{service=""})` returned no data).

Nothing in B-5 stops or mutes a Kuma tile or push.

The post-apply read is owed at landing. It is a `uv run pytest -m ui -k grafana` run or a
`homelab-ui` render of the board after Flux applies `observability`, quoted here.
