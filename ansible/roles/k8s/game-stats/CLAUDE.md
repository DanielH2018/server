# game-stats — playtime exporters for the hand-run Terraria and Valheim servers

Two pure-stdlib Python exporters, `terraria-stats` (`files/stats.py`) and `valheim-stats`
(`files/valheim_stats.py`), each mounted from a ConfigMap rather than built into an image. Each
tails its game pod's console out of loki-homelab, folds it into SQLite (the source of truth),
and exposes Prometheus metrics on :9420. Both import `files/stats_lib.py`, the skeleton they
share. See repo-root `CLAUDE.md`.

Until #2813 this was three roles: `terraria-stats`, `valheim-stats` and `game-stats-lib`. Every
cluster object kept its name, so the scrape jobs, the netpol-baseline Loki policy and the
claims are unchanged.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "game-stats"`
- **Image:** `python` (`game_stats_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** `terraria-stats-data` (weekly -> B2 (default target)), `valheim-stats-data`
  (weekly -> B2 (default target))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — games — companions to the
  hand-operated terraria and valheim servers. ALSO Recreate + RWO PVCs holding irreplaceable
  stats — two independent reasons
<!-- /generated_from -->

- **Must sort AFTER `loki-homelab`**: `depends_on: [loki-homelab]` on the `containers_list`
  entry, pinned by
  `ansible/tests/deploy/test_k8s_toposort.py::test_documented_pairwise_ordering_survives_an_adversarial_list`.
- **One `k8s/manifests` include for both games.** `manifests_service: game-stats` names
  `terraria-stats` as the rollout and `valheim-stats` in `manifests_extra_rollouts`; a second
  include under the same service would prune the first one's staged files. A render change to
  either game therefore restarts both pods.
- **Scraped by:** observability prometheus, `job_name: terraria-stats` and `valheim-stats`.
  Both jobs render from the `metrics` items on this role's `containers_list` entry, which is
  where `:9420` is defined; the Deployments and Services read it back through `metrics_port`.
  Both scripts read `METRICS_PORT` with no default, so the port has no second copy (#3816).
- Stock `python` (`game_stats_k8s_image`), not an `image-builder` build: both scripts and `stats_lib.py` are
  pure stdlib, so a build would add a layer and change nothing that runs.
  **Not because a built image could not be pulled here.** The in-cluster `registry` serves a
  cross-node pull — `ansible/inventory/group_vars/all.yml:k8s_registry_pull_host` is one
  mirror key every node's `registries.yaml` carries, and `ansible/roles/k8s/ical-proxy/` is
  the live proof. An earlier version of this bullet claimed the opposite and would have
  talked a sibling role out of a build it could have (#2703).
- The `checksum/stats-script` pod annotation restarts each Deployment when either of its
  staged modules changes, since a ConfigMap edit alone doesn't roll a pod. It hashes the game's
  own script and `stats_lib.py` together (`tasks/terraria.yml`, `tasks/valheim.yml`).
- The scripts are Jinja-hostile (Prometheus exposition carries `{…}`), so each ConfigMap is
  built built with `kubectl create --from-file`, never a template.

## terraria-stats

- **`terraria-stats-data`** is 1Gi, a `k8s_claims` entry in `defaults/main.yml`, seeded once at
  cutover from the Docker-era DB, on the **weekly** B2 backup tier. It holds the all-time playtime
  SQLite DB — irreplaceable, since Loki keeps 31 days and the exporter backfills only 28.
- **What is Terraria's own** is `parse_line`, `StatsState`, and `Store`'s schema half — the
  `players` schema, `load_state` and `save`, over a `stats_lib.SqliteStore` subclass.
- **It runs as many pods as the game server does**, reading `terraria_k8s_replicas` from
  `ansible/inventory/group_vars/all.yml` — not from terraria's defaults, since a role default
  does not cross a role boundary and `| default(1)` here would have rendered 1 forever
  (#2877). observability's `terraria-stats` scrape job reads the same value, named by its
  `replicas_var`, because a Service with no endpoint reads `up == 0` and pages the
  scrape-target check.
  `ansible/tests/services/test_terraria_stats_follows_the_server.py` holds the three renders
  together. valheim-stats has no such knob.

## valheim-stats

Added 2026-08-13 with the Valheim reactivation, the same shape as terraria-stats.

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
(~250 non-comment lines; its own docstring lists the functions). It holds no per-game state
and takes every game-specific bit as an argument, so it needs no test doubles patched onto it.

`SqliteStore` is the one class a game extends rather than calls: it owns the connection
lifecycle and the `cursor`/`events` tables both games persist identically, and a game's
`Store` subclasses it to declare its own `players` schema in `_init_game_schema` and to write
`load_state`/`save`. `run` is the entry point both games' `main()` reduces to, reading its
tunables from a `RunConfig` the game builds from its own env constants.

### How it ships

Both exporters run as stock `python` pods with `stats_lib.py` mounted alongside their
own entry script by a ConfigMap, because a directly-invoked script gets only its own
directory on `sys.path`. `tasks/stage.yml` stages the copy AND hands each game the
`--from-file` argument for it, so no task file spells `stats_lib.py` itself.
`docs/game-stats-internals.md` has both halves and the guard that holds them together,
`ansible/tests/k8s/test_game_stats_lib_ships_through_the_include.py`.

## Editing

- Logic: `files/stats.py`, `files/valheim_stats.py`, `files/stats_lib.py`. Tests: `tests/`
  (`uv run pytest ansible/roles/k8s/game-stats/tests`). A change in `stats_lib.py` affects
  both games.
