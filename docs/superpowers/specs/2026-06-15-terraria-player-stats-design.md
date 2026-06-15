# terraria-player-stats — playtime & presence from the game console

**Date:** 2026-06-15
**Status:** Implemented (2026-06-15)
**Context:** PLANS.md backlog asked for player stats — originally *deaths + time on server*.
A **Phase 0 LAN capture on 2026-06-15** (operator joined, died, chatted, left) proved the
vanilla dedicated-server console emits **only connection events** (`has joined` / `has left`)
— **not deaths and not chat.** On a vanilla server without SSC, character data (including
death counts) lives **client-side**; the server never sees it. Deaths therefore require a
**TShock + SSC migration**, which the operator evaluated and declined. The feature is
**finalized to what vanilla actually exposes: playtime, sessions, and presence** — with
**zero changes to the Terraria container**, reading console lines already in Loki.

## Goal

A small, headless sidecar (`terraria-stats`) that reads the Terraria console lines already
ingested into **Loki**, parses connection events, maintains **all-time cumulative** per-player
**playtime + session counts + first/last seen** in **SQLite** (the durable source of truth),
and exposes them as **Prometheus metrics** for a **Grafana** dashboard.

**Non-goals:** **deaths** (unavailable on vanilla — would require a TShock+SSC migration; see
Context / Phase 0); chat logging; TShock / SSC / any change to the terraria container; a
Homepage widget; stable cross-rename player identity.

## Confirmed log grammar (Phase 0 — real samples, 2026-06-15)

```text
join          "DBoy has joined."          ->  ^(?P<name>.+) has joined\.$
leave         "DBoy has left."            ->  ^(?P<name>.+) has left\.$
server_restart "Listening on port 7777"   (and "Server started")
ignore (noise) "172.21.0.15:59682 is connecting..."   (TCP accepts, incl. healthcheck-era probes)
ignore (noise) "Saving world data: N%" / "Validating world save: N%" / "Backing up world file"
NOT EMITTED    deaths, chat   (verified: operator died & chatted -> zero console output)
```

These captured lines are the **test fixtures** for the parser. Join/leave formats are stable
across Terraria versions, so the parser is simple and robust (no death-template enumeration).

## Architecture

```text
 terraria (vanilla, UNCHANGED)
   │ stdout: "<name> has joined." / "<name> has left." / boot lines
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

**Event kinds:** `join`, `leave`, `server_restart`.

- `join` / `leave` match the Phase 0 grammar above (name capture before the literal suffix).
- **`server_restart`** is detected from the boot lines (`Listening on port 7777` /
  `Server started`) — it closes all open sessions (players drop with no "has left" line).
- Any line that looks player-event-shaped but matches none of the above increments
  `terraria_stats_unmatched_player_lines_total` and is logged — a safety net against future
  console-format drift.

**SQLite schema** (source of truth, `./data/stats.db`):

```sql
players(name TEXT PRIMARY KEY,
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
terraria_player_playtime_seconds_total{player="X"}    counter (incl. live session)
terraria_player_sessions_total{player="X"}            counter
terraria_players_online                               gauge
terraria_stats_last_event_timestamp                   gauge  (freshness)
terraria_stats_unmatched_player_lines_total           counter (parser health)
```

## Display (Grafana)

A provisioned dashboard in a new "Terraria" folder (datasource uid `EGdsQqhVk`):
- **Leaderboard table**: player · total playtime · sessions · last seen.
- **Playtime accrual** (per player) and **players online** (stat + timeseries).
- **Parser health**: `rate(terraria_stats_unmatched_player_lines_total)` and last-event
  freshness — so a broken parser is *visible*, not silently green.

## Error handling

- Loki query failure / unreachable → log, leave cursor unchanged, retry next loop (no data
  loss; events wait in Loki). `/healthz` reflects last successful poll.
- Unmatched player-shaped line → `terraria_stats_unmatched_player_lines_total`++ and log it
  (catches console-format drift).
- All SQLite writes are transactional with the cursor advance; a crash mid-batch re-processes
  the batch cleanly on restart (idempotent by `(ts, raw)` dedup).

## Secrets

**None.** Loki is internal/unauthenticated on the `monitoring` network (same posture as its
existing `/ready` Kuma probe). No tokens, no SOPS entries, no rotation registry change.

## Verification

1. `validate_compose_templates.py` renders the new compose (pre-commit).
2. `uv run pytest` — parser/session unit tests over the Phase 0 fixtures pass.
3. `stats.py --backfill --once` against live Loki: DB populates from history (the 2026-06-15
   join/leave cycles appear), `/metrics` serves expected series,
   `terraria_stats_unmatched_player_lines_total` stays ~0.
4. Deploy `--check` then for real; Prometheus shows the `terraria-stats` target **up**; the
   Grafana dashboard renders.
5. Play a short live session: playtime/sessions increment; restart the sidecar mid-session and
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

- **Phase 0 — DONE (2026-06-15):** real join/leave/restart grammar captured (see above);
  established deaths/chat are not console-available on vanilla.
- **Phase 1** — `stats.py`: parser + SQLite + `/metrics`, TDD against the fixtures.
- **Phase 2** — Ansible role + compose + registration + Prometheus scrape job; deploy.
- **Phase 3** — Grafana dashboard.

## Known limits (accepted)

- **Deaths are out of scope** — not available on vanilla; revisit only via TShock+SSC.
- Identity is character-name (not a stable ID); two players sharing a name merge.
- First-run backfill only reaches as far as Loki's retention window.
- Playtime granularity is bounded by the connection-event timestamps (join/leave), which is
  exactly the "time on server" the operator asked for.
