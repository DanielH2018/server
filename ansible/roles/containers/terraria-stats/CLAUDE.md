# terraria-stats — Terraria player playtime/presence stats

Headless sidecar that turns the Terraria console (via Loki) into all-time per-player
playtime/session/presence metrics for Grafana. See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `python:3.14-alpine` (stdlib only — no build, no deps)
- **Host:** daniel-server · **No web UI**, no Authelia · **Metrics:** `:9420/metrics`
- **Networks:** `monitoring` (read `loki:3100`, scraped by Prometheus)
- **Depends on:** grafana (ships Loki) — `meta/deps.yml`
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **Reads from Loki, never the docker socket and never the terraria container.** Polls
  `query_range` for `{container="terraria"}` every `POLL_INTERVAL` (20s), cursor-based.
- **Deaths are NOT tracked** — the vanilla console only emits `has joined`/`has left`
  (verified Phase 0, 2026-06-15); deaths/chat never reach it. Deaths would require a
  TShock+SSC migration (rejected). Do not re-add a death metric without that.
- `files/stats.py` is a **static** stdlib script (env-driven). SQLite (`./data/stats.db`,
  Kopia-backed) is the durable source of truth; Prometheus/Grafana are the display layer.
- Metrics: `terraria_player_playtime_seconds_total{player}` (incl. live session),
  `terraria_player_sessions_total{player}`, `terraria_players_online`,
  `terraria_stats_last_event_timestamp`, `terraria_stats_unmatched_player_lines_total`.
- A server restart closes all open sessions (players drop with no "has left"). The
  unmatched-lines counter is the safety net for future console-wording drift.

## Editing & testing
- Compose: `templates/docker-compose.yml.j2` · Logic: `files/stats.py`
- Unit tests: `uv run pytest ansible/roles/containers/terraria-stats/files`
- Smoke (one pass, no server): `docker exec terraria-stats python /app/stats.py --once`
- Rebuild all-time state from Loki history: `docker exec terraria-stats python /app/stats.py --backfill`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "terraria-stats"`
