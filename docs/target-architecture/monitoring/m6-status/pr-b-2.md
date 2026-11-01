### PR B-2

PR B-2, `probe.py monitors` and `probe.py alerts` on `ALERTS`, postflight's Kuma checks, and
`kuma-drift` deleted: [#6829](https://github.com/DanielH2018/server/pull/6829). The merge
commit and the post-merge reads follow once it lands.

- **`monitors`** rolls up `ALERTS{alertstate="firing"}` by severity and `service`, then lists
  active silences. A missing Watchdog exits 75. Shadow alerts print under their own heading and
  do not change the exit code, because five fired on 2026-11-01 while Kuma read 35/35 up.
- **`monitors` also reads Alertmanager's active alerts**, and folds in the ones `ALERTS`
  lacks. The loki-homelab ruler sends its alerts to Alertmanager directly, so a reader of
  `ALERTS` alone missed `LokiPiLogsSilent` while it fired on 2026-11-01. `alerts` has no
  history for them, because Alertmanager keeps none.
- **`monitors` keeps one Kuma arm**, `monitor_status{job="uptime-kuma"} != 1`, because A6's
  bridge rows, B2, B4, B5, B6b and B9 still page through Kuma. The arm is not in the plan's
  B-2 row. C-1, which deletes the `uptime-kuma` scrape, removes it with `format_kuma` and its
  two tests in `scripts/diagnostics/tests/test_probe_monitors.py`.
- **`firing_ingress.py` is deleted**, because the rollup reads the same `ALERTS` series. The
  plan's tree predates the module.
- **`kuma_pi.py` is deleted** with `kuma_live.py` and `kuma_table_loop.py`, because
  `kuma-drift --pi` was its only caller.
- **postflight** checks `up{job="alertmanager"} == 1` and the Watchdog firing as one §9.1
  check. `check_kuma_scrape` stays until C-1.
