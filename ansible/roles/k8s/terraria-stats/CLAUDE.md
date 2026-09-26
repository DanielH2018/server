# terraria-stats — playtime exporter for the hand-run Terraria server

A pure-stdlib Python exporter (`files/stats.py`), mounted from a ConfigMap rather than
built into an image. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "terraria-stats"`
- **Image:** `python` (`terraria_stats_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `terraria-stats-data` (weekly -> B2 (default target))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — games — companion to the
  hand-operated terraria server. ALSO Recreate + RWO volume-claim PVC holding irreplaceable
  stats — two independent reasons
<!-- /generated_from -->

- **Its `:9420` Prometheus exporter** is scraped in-cluster by the `claude-otel`
  `terraria-stats` job.
- **`terraria-stats-data`** is 1Gi, created by `k8s/volume-claim` and seeded once at cutover from
  the Docker-era DB, on the **weekly** B2 backup tier.
  It holds the all-time playtime SQLite DB — irreplaceable, since Loki's ~28-day backfill
  window can't fully reconstruct it.

## Notable
- Stock `python:3.14-alpine`, not an `image-builder` build: `stats.py` and the
  `ansible/roles/k8s/game-stats-lib/` module it imports are pure stdlib, so a build would
  add a layer and change nothing that runs.
  **Not because a built image could not be pulled here.** The in-cluster `registry` does
  serve a cross-node pull. `ansible/inventory/group_vars/all.yml:k8s_registry_pull_host` is
  one mirror KEY every node's `registries.yaml` carries, so one image reference is
  node-portable; what differs per node is the ENDPOINT behind it — daniel-box resolves it to
  the registry's loopback hostPort, an agent to
  `ansible/inventory/group_vars/all.yml:k8s_registry_cluster_ip` over flannel.
  `ansible/roles/k8s/ical-proxy/` is the live proof: it runs an `image-builder` build while
  preferring daniel-server. An earlier version of this bullet claimed the opposite and would
  have talked a sibling role out of a build it could have (#2703).
- The `checksum/stats-script` pod annotation restarts the Deployment when either staged
  module changes, since a ConfigMap edit alone doesn't roll a pod (hashes `stats.py` +
  `stats_lib.py` together — see tasks/main.yml).
- **What stays in this role** is `parse_line`, `StatsState`, and `Store`'s schema half — the
  `players` schema, `load_state` and `save`, over a `stats_lib.SqliteStore` subclass. Everything
  else — the Loki fetch, cursor handling, metric rendering, the HTTP handler, the run loop —
  lives in `ansible/roles/k8s/game-stats-lib/`, and that role's `CLAUDE.md` owns how it ships.
