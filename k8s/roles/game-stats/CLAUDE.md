# game-stats — playtime exporters for the hand-run Terraria and Valheim servers

Two pure-stdlib Python exporters, `terraria-stats` (`k8s/roles/game-stats/files/stats.py`) and `valheim-stats`
(`k8s/roles/game-stats/files/valheim_stats.py`), each mounted from a ConfigMap rather than built into an image. Each
tails its game pod's console out of loki-homelab, folds it into SQLite (the source of truth),
and exposes Prometheus metrics on :9420. Both import `k8s/roles/game-stats/files/stats_lib.py`, the skeleton they
share. See repo-root `CLAUDE.md`.

**HISTORY — until #2813 this was three roles:** `terraria-stats`, `valheim-stats` and `game-stats-lib`. Every
cluster object kept its name, so the scrape jobs, the netpol-baseline Loki policy and the
claims are unchanged.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=game-stats` gates the rollout
- **Image:** `python` (`game_stats_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** `terraria-stats-data` (weekly -> B2 (default target)), `valheim-stats-data`
  (weekly -> B2 (default target))
<!-- /generated_from -->

- **Flux applies it.** Its `containers_list` entry
  is in `k8s/services.yml`, so nothing applies it by hand, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. Both
  Deployments are `Recreate` over
  an RWO claim, so a roll stops the old pod before the new one starts and the exporter's
  metrics gap for that interval. `k8s_autodeploy: false` gates no
  apply. It stops Renovate automerging the image bump.
- **Its rollout.** The Kustomization's `wait` holds the rollout. The restart checksum the
  renderer stamps on each Deployment rolls the pods.

- **Must sort AFTER `loki-homelab`**: `depends_on: [loki-homelab]` on the `containers_list`
  entry, pinned by
  `tests/deploy/test_k8s_toposort.py::test_documented_pairwise_ordering_survives_an_adversarial_list`.
- **One Flux tag for both games.** `defaults/main.yml` names
  `terraria-stats` as the rollout and `valheim-stats` in `manifests_extra_rollouts`. A render
  change to either game therefore restarts both pods.
- **Scraped by:** observability prometheus, `job_name: terraria-stats` and `valheim-stats`.
  Both jobs render from the `metrics` items on this role's `containers_list` entry, which is
  where `:9420` is defined; the Deployments and Services read it back through `metrics_port`.
  Both scripts read `METRICS_PORT` with no default, so the port has no second copy (#3816).
- Stock `python` (`game_stats_k8s_image`), not an `image-builder` build: both scripts and `stats_lib.py` are
  pure stdlib, so a build would add a layer and change nothing that runs.
  **Not because a built image could not be pulled here.** The in-cluster `registry` serves a
  cross-node pull — `k8s/vars.yml:k8s_registry_pull_host` is one
  mirror key every node's `registries.yaml` carries, and `k8s/roles/ical-proxy/` is
  the live proof. An earlier version of this bullet claimed the opposite and would have
  talked a sibling role out of a build it could have (#2703).
- **Each game's script ConfigMap is a rendered template** (#5444):
  `templates/configmap-terraria.yaml.j2` and `templates/configmap-valheim.yaml.j2` embed the
  entry script and `stats_lib.py` with `lookup('file')`. The scripts are Jinja-hostile, since
  Prometheus exposition carries `{…}`, and the lookup's text is never templated again.
- **A script edit rolls the pods with no annotation of the role's own.** The edit changes the
  render, and the renderer's restart checksum hashes the ConfigMap each pod
  mounts (`docs/flux-deploy-plane.md`, *Restart triggers*).
  `scripts/renderer/tests/manifests/test_renderer_game_stats_restart.py` holds both ConfigMaps to that.

## terraria-stats

- **`terraria-stats-data`** is 1Gi, a `k8s_claims` entry in `defaults/main.yml`, seeded once at
  cutover from the Docker-era DB, on the **weekly** B2 backup tier. It holds the all-time playtime
  SQLite DB — irreplaceable, since Loki keeps 31 days and the exporter backfills only 28.
- **What is Terraria's own** is `parse_line`, `StatsState`, and `Store`'s schema half — the
  `players` schema, `load_state` and `save`, over a `stats_lib.SqliteStore` subclass.
- **It runs while the game server does.** Neither Deployment renders `replicas:`; deploy.local's
  Games panel starts and stops each exporter with its server, from the `game_servers` registry
  in `estate/vars.yml` (ADR-0033). The scrape job renders either way, and its
  `ScrapeTargetDown` rule mutes while kube-state-metrics reads 0 replicas, because a Service
  with no endpoint reads `up == 0`.

## valheim-stats

Added 2026-08-13 with the Valheim reactivation, the same shape as terraria-stats.

- **It runs while the valheim server does**, started and stopped with it from the Games panel,
  as terraria-stats is. The claim stays, so the totals survive a stopped window and miss only
  what the server did not log.
- **Reads:** `{container="valheim"}` from `loki-homelab`. **Dashboard:**
  `Apps/valheim-player-stats.json` → "Valheim — Player Stats".
- **Storage:** `valheim-stats-data` (`longhorn`, **backed up**), a `k8s_claims` entry
  in `defaults/main.yml`, created empty since there was nothing to seed. Loki keeps 31 days; after that this DB is the only copy of the totals.
- **What is Valheim's own** is `parse_line`, `StatsState`, and `Store`'s schema half — the
  `players` + `steam_names` schemas, `load_state` and `save`, over a `stats_lib.SqliteStore`
  subclass.
- **Read `docs/game-stats-internals.md` before editing the parser.** It carries the
  console lifecycle, the three traps in reading it (patterns are searched, never anchored at
  `^`; a spawn during an open session is a respawn; the SteamID→name map is built by adjacency
  and can cross-bind), and the two metrics that make a mismatch visible.

## stats_lib.py — the skeleton both games share

`files/stats_lib.py` is the Loki-tail → Prometheus-exporter skeleton the two exporters share
(its own docstring lists the functions). It holds no per-game state
and takes every game-specific bit as an argument, so it needs no test doubles patched onto it.

`k8s/roles/game-stats/files/stats_lib.py:SqliteStore` is the one class a game extends rather than calls: it owns the connection
lifecycle and the `cursor`/`events` tables both games persist identically, and a game's
`Store` subclasses it to declare its own `players` schema in `_init_game_schema` and to write
`load_state`/`save`. `run` (`k8s/roles/game-stats/files/stats_lib.py:run`) is the entry point both games' `main()` reduces to, reading its
tunables from a `RunConfig` (`k8s/roles/game-stats/files/stats_lib.py:RunConfig`) the game builds from its own env constants.

### How it ships

Both exporters run as stock `python` pods with `stats_lib.py` mounted alongside their
own entry script by a ConfigMap, because a directly-invoked script gets only its own
directory on `sys.path`. Each game's ConfigMap template carries the copy as a second key.
`docs/game-stats-internals.md` has the mechanism and the guard that holds it,
`scripts/renderer/tests/manifests/test_renderer_game_stats_restart.py`.

## Editing

- Logic: `k8s/roles/game-stats/files/stats.py`, `k8s/roles/game-stats/files/valheim_stats.py`,
  `k8s/roles/game-stats/files/stats_lib.py`. Tests: `k8s/roles/game-stats/tests/`
  (`uv run pytest k8s/roles/game-stats/tests`). A change in `stats_lib.py` affects
  both games
  (`scripts/renderer/tests/manifests/test_renderer_game_stats_restart.py::test_a_stats_lib_edit_moves_both_pods_checksums`).
