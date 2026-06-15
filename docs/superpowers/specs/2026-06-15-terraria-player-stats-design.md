# terraria-player-stats — all-time player stats from the game console

**Date:** 2026-06-15
**Status:** Approved (design)
**Context:** PLANS.md backlog asked "Add stats?" alongside the Terraria healthcheck. The
operator wants **player gameplay stats** — primarily *deaths* and *time on server* — kept
**all-time / cumulative** per player. Vanilla Terraria keeps no player stats and exposes no
API; the only data source is the server console (joins/leaves/deaths/chat). A TShock
migration was evaluated and rejected for this goal: it's an admin-platform migration that
would re-touch the just-stabilized world-persistence setup and still needs SSC + a plugin +
a display surface to deliver deaths. This design gets the same stats with **zero changes to
the Terraria container** by reading the console logs already in Loki.

## Goal

A small, headless sidecar (`terraria-stats`) that reads the Terraria console lines already
ingested into **Loki**, parses player events, maintains **all-time cumulative** per-player
stats in **SQLite** (the durable source of truth), and exposes them as **Prometheus
metrics** for a **Grafana** dashboard.

**Non-goals (v1):** TShock / SSC / any change to the terraria container; a Homepage widget;
chat logging/analytics; a bespoke web UI; stable cross-rename player identity.

## Architecture

```text
 terraria (vanilla, UNCHANGED)
   │ stdout: joins / leaves / deaths / chat
   ▼
 Promtail ──► Loki                        (both already running, monitoring net)
                │  LogQL query_range API, polled ~20s, cursor-based
                ▼
 terraria-stats  (NEW, python:3.14-alpine, stdlib-only, headless, monitoring net)
   • poll Loki {container="terraria"} since cursor → parse → events
   • SQLite = source of truth (./data bind-mount, Kopia-backed)
   • session pairing (join→leave; server-restart closes open sessions)
   • serve :9420 /metrics (Prometheus text format, hand-emitted) + /healthz
                │ scrape
                ▼
 Prometheus ──► Grafana dashboard         (both already running)
```

- **New role** `ansible/roles/containers/terraria-stats/`, mirroring the `monitor-bridge`
  precedent (Python sidecar, `monitoring` net, no web route, no Authelia).
- **Network: `monitoring` only** — reaches `http://loki:3100`, scraped by Prometheus.
  No Traefik route, no host port, **no new external attack surface, no secret** (Loki is
  internal/unauthenticated on this net, same as its `/ready` probe today).
- **Image `python:3.14-alpine`, stdlib only** (`urllib`, `json`, `sqlite3`, `http.server`) —
  Loki query over HTTP, SQLite via stdlib, Prometheus exposition format emitted by hand. No
  pip deps, **no build step** — `copy` the `.py` like monitor-bridge.
- **Terraria container is never modified** at any point (the world-persistence fix is
  untouched). The feature is fully additive and trivially removable.

## Components

| File | Purpose |
|------|---------|
| `roles/containers/terraria-stats/files/stats.py` | **Static** sidecar: Loki poll loop + parser + SQLite state + `/metrics` & `/healthz` HTTP server. Env-configured. `--once` mode for verification; `--backfill` to (re)derive from Loki history. |
| `roles/containers/terraria-stats/templates/docker-compose.yml.j2` | Service + AutoKuma label + healthcheck (HTTP GET `:9420/healthz`) + env (Loki URL, poll interval, metrics port `9420`, DB path). Plain `#` YAML comments only (no Jinja `{# #}` in compose templates — corrupts macro indent). |
| `roles/containers/terraria-stats/tasks/main.yml` | `copy` stats.py → container dir, then `include_role: common` (`setup_dirs` + `docker_deploy`). |
| `roles/containers/terraria-stats/meta/deps.yml` | `role_deps: [grafana]` (Loki lives in the grafana role; ensures Loki/Promtail exist first). |
| `roles/containers/terraria-stats/meta/main.yml` | galaxy_info, matching sibling roles. |
| `roles/containers/terraria-stats/CLAUDE.md` | Role doc (At a glance / Notable / Editing). |
| `roles/containers/prometheus/templates/prometheus.yml.j2` | **Edit:** add scrape job `terraria-stats` → `targets: ["terraria-stats:9420"]`. (Prometheus recreates on config change via its registered `common_config_changed`.) |
| `roles/containers/grafana/files/dashboards/Terraria/player-stats.json` | Provisioned dashboard (datasource uid pinned to Prometheus `EGdsQqhVk`), own "Terraria" folder. |
| `tests/` (pyproject `testpaths`) | pytest over the pure parser/session logic. **NOT** under `ansible/filter_plugins/` (plugin loader would import it at deploy time). |

Registration: add to `inventory/host_vars/daniel-server.yml` → `containers_list`
(`name: terraria-stats`, `port: false`, `use_authelia: false`, `networks: [monitoring]`).
Deploy tags derive from `name`; no `deploy.yml` edit.

## Hardening (matches fleet conventions)

```yaml
user: "1000:1000"
cap_drop: [ALL]
security_opt: [no-new-privileges:true]
read_only: true
tmpfs: [/tmp]
# ./data (SQLite) is the only writable mount; it is in Kopia's containers/ scope.
deploy:
  resources:
    limits:       { cpus: '0.50', memory: 128M }
    reservations: { cpus: '0.05', memory: 32M }
```

## Parser & state model

**Event kinds:** `join`, `leave`, `death`, `server_restart`.

- **`server_restart`** is detected from the existing boot lines (`Listening on port 7777` /
  `Server started`) — it closes all open sessions (players drop with no "has left" line).
- **Death detection anchors on the online-player set, not on enumerating death messages.**
  Terraria has dozens of death templates; instead, joins tell us who is online, and a
  broadcast line that *starts with an online player's name* and is not chat counts as a
  death. We match the *player*, not the *cause*.

**SQLite schema** (source of truth, `./data/stats.db`):

```sql
players(name TEXT PRIMARY KEY,
        total_deaths INTEGER NOT NULL DEFAULT 0,
        total_playtime_seconds INTEGER NOT NULL DEFAULT 0,
        session_count INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT, last_seen TEXT,
        current_session_start TEXT)         -- NULL = offline
cursor(id INTEGER PRIMARY KEY CHECK(id=1), last_ts TEXT NOT NULL)
events(ts TEXT, player TEXT, kind TEXT, raw TEXT)  -- audit; allows re-derivation
```

**Session pairing for playtime:**
- `join` → set `current_session_start` (if already set, close prior session first).
- `leave` → `total_playtime_seconds += leave − start`; clear it; `session_count++`.
- `server_restart` → close every open session at the boot timestamp.
- Exported playtime **includes the live in-progress session** so Grafana ticks up in real time.

**Cursor mechanics:** poll Loki `query_range` for `{container="terraria"}` since `last_ts`,
process in time order, advance `last_ts`, and persist it **in the same transaction** as the
state updates so events are never double-counted or skipped. Dedup colliding timestamps by
`(ts, raw)`. Sidecar restart is safe: it reloads SQLite and replays from the cursor, so any
missed leaves/restarts reconcile.

## Metrics exposed (`/metrics`)

```text
terraria_player_deaths_total{player="X"}              counter
terraria_player_playtime_seconds_total{player="X"}    counter (incl. live session)
terraria_player_sessions_total{player="X"}            counter
terraria_players_online                               gauge
terraria_stats_last_event_timestamp                   gauge  (freshness)
terraria_stats_unmatched_player_lines_total           counter (parser health)
```

## Display (Grafana)

A provisioned dashboard in a new "Terraria" folder (datasource uid `EGdsQqhVk`):
- **Leaderboard table**: player · deaths · total playtime · sessions · last seen.
- **Deaths over time** and **playtime accrual** (per player).
- **Players online** (stat + timeseries).
- **Parser health**: `rate(terraria_stats_unmatched_player_lines_total)` and last-event
  freshness — so a broken parser is *visible*, not silently green.

## Error handling

- Loki query failure / unreachable → log, leave cursor unchanged, retry next loop (no data
  loss; events wait in Loki). `/healthz` reflects last successful poll.
- A line that looks like a player event but matches no known pattern → increment
  `terraria_stats_unmatched_player_lines_total` and log it, so missed death templates surface
  for iterative pattern extension.
- All SQLite writes are transactional with the cursor advance; a crash mid-batch re-processes
  the batch cleanly on restart (idempotent by `(ts, raw)` dedup).

## Secrets

**None.** Loki is internal/unauthenticated on the `monitoring` network (same posture as its
existing `/ready` Kuma probe). No tokens, no SOPS entries, no rotation registry change.

## Operator prerequisites — Phase 0 (GATE for all parser work)

We have **zero real samples** of player events: nobody has successfully joined this server
(38k log lines, no joins/deaths), and external access was still timing out as of 2026-06-14.
Before the parser patterns can be finalized:

1. Connect a Terraria client over LAN to `daniel-server:7778`, **join → die several ways →
   leave**, while capturing `docker logs terraria`.
2. Save the real `join` / `leave` / `death` / chat lines as **test fixtures**.
3. (Independent, optional) fix external access (`terraria.daniel-hunter.com:7777` TCP
   timeout) — stats are only meaningful once people can actually play.

## Verification

1. `validate_compose_templates.py` renders the new compose (pre-commit).
2. `uv run pytest` — parser/session unit tests over the captured fixtures pass.
3. `stats.py --backfill --once` against live Loki: DB populates, `/metrics` serves expected
   series, `terraria_stats_unmatched_player_lines_total` stays ~0 on the sample set.
4. Deploy `--check` then for real; Prometheus shows the `terraria-stats` target **up**; the
   Grafana dashboard renders.
5. Play a short live session: deaths/playtime increment; restart the sidecar mid-session and
   confirm no double-count and the open session is preserved.

## Testing approach

The parser and session-pairing logic are **pure functions** (line → event; event stream →
state deltas), unit-tested with pytest against the Phase 0 fixtures — the heart of correctness.
The Loki client, SQLite layer, and HTTP server are thin I/O wrappers exercised by the `--once`
/ `--backfill` modes and the live verification above.

## Rollback

Fully additive and isolated: `docker compose down` the sidecar, drop the `containers_list`
entry, revert the Prometheus scrape-job line and the dashboard file. **Terraria is never
touched**, so there is nothing to undo on the game server.

## Phasing

- **Phase 0** — capture real log samples (LAN play session). *Gate for everything below.*
- **Phase 1** — `stats.py`: parser + SQLite + `/metrics`, TDD against fixtures.
- **Phase 2** — Ansible role + compose + registration + Prometheus scrape job; deploy.
- **Phase 3** — Grafana dashboard.

## Known limits (accepted)

- Identity is character-name (not a stable ID); two players sharing a name merge.
- First-run backfill only reaches as far as Loki's retention window.
- Death detection depends on console wording; the unmatched-line counter is the safety net.
