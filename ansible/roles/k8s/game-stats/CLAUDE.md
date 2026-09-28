# game-stats — playtime exporters for the hand-run Terraria and Valheim servers

Two pure-stdlib Python exporters, `terraria-stats` (`files/stats.py`) and `valheim-stats`
(`files/valheim_stats.py`), each mounted from a ConfigMap rather than built into an image. Each
tails its game pod's console out of loki-homelab, folds it into SQLite (the source of truth),
and exposes Prometheus metrics on :9420. Both import `files/stats_lib.py`, the skeleton they
share. See repo-root `CLAUDE.md` for shared conventions.

Until #2813 this was three roles: `terraria-stats`, `valheim-stats` and `game-stats-lib`. Every
cluster object kept the name it had then, so the scrape jobs, the netpol-baseline Loki policy
and the claims read them unchanged.

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
  `terraria-stats` as the rollout and `valheim-stats` in `manifests_extra_rollouts`. A second
  include under the same service would prune the first one's staged files. The consequence is
  that a render change to either game restarts both pods.
- **Scraped by:** claude-otel prometheus, `job_name: terraria-stats` and
  `job_name: valheim-stats`.
- Stock `python:3.14-alpine`, not an `image-builder` build: both scripts and `stats_lib.py` are
  pure stdlib, so a build would add a layer and change nothing that runs.
  **Not because a built image could not be pulled here.** The in-cluster `registry` does
  serve a cross-node pull. `ansible/inventory/group_vars/all.yml:k8s_registry_pull_host` is
  one mirror KEY every node's `registries.yaml` carries, so one image reference is
  node-portable; what differs per node is the ENDPOINT behind it — daniel-box resolves it to
  the registry's loopback hostPort, an agent to
  `ansible/inventory/group_vars/all.yml:k8s_registry_cluster_ip` over flannel.
  `ansible/roles/k8s/ical-proxy/` is the live proof: it runs an `image-builder` build while
  preferring daniel-server. An earlier version of this bullet claimed the opposite and would
  have talked a sibling role out of a build it could have (#2703).
- The `checksum/stats-script` pod annotation restarts each Deployment when either of its
  staged modules changes, since a ConfigMap edit alone doesn't roll a pod. It hashes the
  game's own script and `stats_lib.py` together (see `tasks/terraria.yml` and
  `tasks/valheim.yml`).
- The scripts are Jinja-hostile (Prometheus exposition carries `{…}`), so each ConfigMap is
  built with `kubectl create --from-file`, never a template.

## terraria-stats

- **`terraria-stats-data`** is 1Gi, created by `k8s/volume-claim` and seeded once at cutover
  from the Docker-era DB, on the **weekly** B2 backup tier. It holds the all-time playtime
  SQLite DB — irreplaceable, since Loki's ~28-day backfill window can't fully reconstruct it.
- **What is Terraria's own** is `parse_line`, `StatsState`, and `Store`'s schema half — the
  `players` schema, `load_state` and `save`, over a `stats_lib.SqliteStore` subclass.

## valheim-stats

Added 2026-08-13 with the Valheim reactivation, deliberately the same shape as terraria-stats.

- **Reads:** `{container="valheim"}` from `loki-homelab`.
- **Storage:** `valheim-stats-data` (`longhorn`, **backed up**), declared by
  `templates/pvc-valheim.yaml.j2` rather than `k8s/volume-claim`, since there was nothing to
  seed. Loki keeps 31 days, so after that this DB is the only copy of the totals.
- **Dashboard:** `Apps/valheim-stats.json` → "Valheim — Player Stats".
- **What is Valheim's own** is `parse_line`, `StatsState`, and `Store`'s schema half — the
  `players` + `steam_names` schemas, `load_state` and `save`, over a `stats_lib.SqliteStore`
  subclass.

### What it does that terraria-stats cannot

**Deaths.** Terraria's vanilla console emits no death events (verified in that service's
Phase 0 and stated in its docstring). Valheim's does, as a sentinel: the same line that
reports a character spawn reports a death with the ZDO id `0:0`.

### The console, and why the parser looks different

Documented lifecycle (corroborated across the image's own `valheim-logfilter`, adaliszk's
mtail program, and mbround18/valheim-docker):

```text
Got handshake from client 76561198108936133       <- connect
Got character ZDOID from Testvazz : 954855457:113  <- spawn (ALSO fires on respawn)
Got character ZDOID from Testvazz : 0:0            <- death
Closing socket 76561198108936133                   <- disconnect
```

Three consequences, each of which is a trap:

1. **Patterns are searched, never anchored at `^`.** This image wraps every console line
   in its own supervisord prefix (`Aug 13 16:53:42 supervisord: valheim-server …`).
   Terraria's image logs bare lines, so its parser anchors; copying that here matches
   nothing. `test_valheim_stats.py` pushes every parse case through the prefix so a regression
   to anchoring fails loudly.
2. **A spawn while a session is already open is a RESPAWN, not a new session.** Otherwise
   every death inflates the session count.
3. **A disconnect names only the SteamID**, so playtime needs a SteamID→name map. It is
   built by ADJACENCY — the handshake, then the next spawn — because the console never puts
   the ID and the character name on one line, which is also how the other published parsers
   do it. Two players handshaking before either spawns can cross-bind; rare at homelab
   scale, but a cross-bind mis-attributes **that session's** playtime permanently in SQLite
   — only later sessions bind correctly. **Deaths are unaffected** — they key off the
   name directly, which is why they are the more trustworthy half of the board.

### Verification status — read before trusting the numbers

The line formats above came from documentation and other projects, **not** from this
server: Valheim was archived in January, long before this Loki existed, so there is no
historical log to test against, and the exporter shipped before anyone had played. Real play
since the 2026-09-09 fresh world has exercised the parser: on 2026-09-26 the exporter reported
375 deaths and `valheim_stats_unmatched_player_lines_total` at 0.

Two things exist to make a mismatch visible rather than silent:

- `valheim_stats_unmatched_player_lines_total` — player-shaped lines that did not parse.
  Expect a flat zero; a step up with frozen player metrics means upstream reworded.
- `valheim_connections` vs `valheim_players_online` — the server's own ~10 min heartbeat
  against the session-derived count. They should agree; a persistent gap means the session
  state machine has drifted. Both are on the dashboard side by side.

### Notable

- **No seed and no backfill.** Unlike terraria-stats (whose Docker-era SQLite was copied
  in), totals genuinely started at zero. `BACKFILL_DAYS=28` is a ceiling that keeps the first
  query under Loki's `max_query_length`, not an expectation of finding anything.
- Heartbeat lines are parsed for the gauge but kept **out** of the SQLite audit table —
  one every 10 minutes would dwarf the events worth reading.

## stats_lib.py — the skeleton both games share

`files/stats_lib.py` is the Loki-tail → Prometheus-exporter skeleton the two exporters were
built as two independent forks of, and later found to share ~250 non-comment lines of (see
stats_lib.py's own docstring for the exact function list). It has no per-game state and takes
every game-specific bit — URLs, the parser's `apply_fn`, the render/log callbacks — as an
argument, so it needs no test doubles patched onto it.

`SqliteStore` is the one class here a game extends rather than calls: it owns the connection
lifecycle and the `cursor`/`events` tables both games persist identically, and a game's
`Store` subclasses it to declare its own `players` schema in `_init_game_schema` and to
write `load_state`/`save`. `run` is the entry point both games' `main()` reduces to, reading
its tunables from a `RunConfig` the game builds from its own env constants.

### How it ships

Both exporters run as `python:3.14-alpine` pods with `stats_lib.py` mounted alongside their
own entry script by a ConfigMap, not on a host with a repo checkout — a directly-invoked
script gets only its own directory on `sys.path`, so a copy has to be staged beside each
script. `tasks/stage.yml` is that shared step (the same shape
`roles/setup/common/tasks/install_host_lib.yml` uses for `host_lib.py`, adapted for
ConfigMap staging rather than a host `/opt` install), and it owns both halves of the
shipment: `tasks/terraria.yml` and `tasks/valheim.yml` each `import_tasks` it with
`game_stats_lib_dest_dir` set to their own staging directory, and the include copies the file
there AND sets `game_stats_lib_from_file` to the `--from-file=stats_lib.py=<dir>/stats_lib.py`
argument for that copy. Each game's task file interpolates that fact into its own `kubectl
create configmap` command and never spells `stats_lib.py` itself — the node-local copy alone
is not enough, since what actually reaches the pod is the ConfigMap, and until #2055 a caller
could stage the file and forget the entry.

`ansible/tests/k8s/test_game_stats_lib_ships_through_the_include.py` is the guard: the fact
names exactly the file the copy stages, the task files that include `stage.yml` are exactly
the ones that stage a script importing `stats_lib`, and every one interpolates the fact.

## Editing

- Logic: `files/stats.py`, `files/valheim_stats.py`, `files/stats_lib.py`. Tests:
  `tests/` (`uv run pytest ansible/roles/k8s/game-stats/tests`). A behaviour change in
  `stats_lib.py` affects both games.
