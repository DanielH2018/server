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
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=game-stats` with exit 5
- **Image:** `python` (`game_stats_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** `terraria-stats-data` (weekly -> B2 (default target)), `valheim-stats-data`
  (weekly -> B2 (default target))
<!-- /generated_from -->

- **Flux applies it, not Ansible** (D5.19, the first Wave D PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. To
  give it back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain
  revert of the flag leaves two writers on the objects. Both Deployments are `Recreate` over
  an RWO claim, so a roll stops the old pod before the new one starts and the exporter's
  metrics gap for that interval, as they did under Ansible. The entry's old
  `k8s_autodeploy: false` stance has no reader once the flag is set: the GitOps deployer skips
  every Flux-owned role.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`
  with its `manifests_rollout` and `manifests_extra_rollouts`, and no Flux path runs it. It
  stays for the handback. The Kustomization's `wait` replaces the rollout wait, and the
  restart checksum the renderer stamps on each Deployment replaces the shared restart.

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
- **Each game's script ConfigMap is a rendered template** (#5444):
  `templates/configmap-terraria.yaml.j2` and `templates/configmap-valheim.yaml.j2` embed the
  entry script and `stats_lib.py` with `lookup('file')`. The scripts are Jinja-hostile, since
  Prometheus exposition carries `{…}`, and the lookup's text is never templated again.
- **A script edit rolls the pods with no annotation of the role's own.** The edit changes the
  render, so `k8s/manifests`' rollout restart rolls both Deployments while Ansible owns the
  role. Under Flux, the renderer stamps a restart checksum that hashes the ConfigMap each pod
  mounts (`docs/flux-deploy-plane.md`, *Restart triggers*).
  `scripts/renderer/tests/test_renderer_game_stats_restart.py` holds both ConfigMaps to that.

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
directory on `sys.path`. Each game's ConfigMap template carries the copy as a second key.
`docs/game-stats-internals.md` has the mechanism and the guard that holds it,
`scripts/renderer/tests/test_renderer_game_stats_restart.py`.

## Editing

- Logic: `files/stats.py`, `files/valheim_stats.py`, `files/stats_lib.py`. Tests: `tests/`
  (`uv run pytest ansible/roles/k8s/game-stats/tests`). A change in `stats_lib.py` affects
  both games.
