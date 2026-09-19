# terraria-stats — playtime exporter for the hand-run Terraria server

A pure-stdlib Python exporter (`files/stats.py`), mounted from a ConfigMap rather than
built into an image. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry. -->
- **Deploy tag:** `--tags "terraria-stats"`
- **Image:** `python` (`terraria_stats_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `terraria-stats-data`
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — games — companion to the
  hand-operated terraria server. ALSO Recreate + RWO volume-claim PVC holding irreplaceable
  stats — two independent reasons
<!-- /generated_from -->

- **Its `:9420` Prometheus exporter** is scraped in-cluster by the `claude-otel`
  `terraria-stats` job.
- **`terraria-stats-data`** is 1Gi, `k8s/volume-claim`-seeded, on the **daily** backup tier.
  It holds the all-time playtime SQLite DB — irreplaceable, since Loki's ~28-day backfill
  window can't fully reconstruct it.

## Notable
- Stock `python:3.14-alpine`, not an `image-builder` build: `stats.py` has no
  dependencies, so the pod schedules on any node — the in-cluster `registry` is
  loopback-only and can't serve a cross-node pull.
- The `checksum/stats-script` pod annotation restarts the Deployment when either staged
  module changes, since a ConfigMap edit alone doesn't roll a pod (hashes `stats.py` +
  `stats_lib.py` together — see tasks/main.yml).
- The Loki fetch, cursor handling, metric rendering, the HTTP handler and the run loop live
  in `k8s/game-stats-lib`'s `stats_lib.py`, shared with valheim-stats — see that role's
  CLAUDE.md for how it ships (its include stages the copy beside this script and hands this
  role the `--from-file` entry as `game_stats_lib_from_file`). `parse_line`, `StatsState`
  and `Store` stay here; they are the per-game part.
