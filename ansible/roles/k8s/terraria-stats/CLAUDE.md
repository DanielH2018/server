# terraria-stats — playtime exporter for the hand-run Terraria server

A pure-stdlib Python exporter (`files/stats.py`), mounted from a ConfigMap rather than
built into an image. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Deploy tag:** `--tags "terraria-stats"`.
- **No route** — its `:9420` Prometheus exporter is scraped in-cluster by the
  `claude-otel` `terraria-stats` job.
- **Claim:** 1Gi, `k8s/volume-claim`-seeded. Holds the all-time playtime SQLite DB —
  irreplaceable, since Loki's ~28-day backfill window can't fully reconstruct it — on
  the **daily** backup tier.
- **`k8s_autodeploy: false`** — grouped operationally with the hand-operated Terraria
  server, not deployed unattended; also independently matches the migrating-state shape
  (`Recreate` + an RWO PVC). Reason is in `defaults/main.yml`.

## Notable
- Stock `python:3.14-alpine`, not an `image-builder` build: `stats.py` has no
  dependencies, so the pod schedules on any node — the in-cluster `registry` is
  loopback-only and can't serve a cross-node pull.
- The `checksum/stats-script` pod annotation restarts the Deployment when either staged
  module changes, since a ConfigMap edit alone doesn't roll a pod (hashes `stats.py` +
  `stats_lib.py` together — see tasks/main.yml).
- The Loki fetch, cursor handling, metric rendering, the HTTP handler and the run loop live
  in `k8s/game-stats-lib`'s `stats_lib.py`, shared with valheim-stats — see that role's
  CLAUDE.md for how it ships (staged beside this script AND added to this role's own
  ConfigMap). `parse_line`, `StatsState` and `Store` stay here; they are the per-game part.
