# valheim-stats (k8s) — all-time Valheim player stats

Added 2026-08-13 with the Valheim reactivation. Sibling of `k8s/terraria-stats` and
deliberately the same shape: tail the game console out of loki-homelab → fold into SQLite
(the source of truth) → expose Prometheus metrics on :9420 → Grafana.

## At a glance
- **Image:** `python:3.14-alpine` running `valheim_stats.py` from a ConfigMap (pure stdlib, no deps)
- **Reads:** `{container="valheim"}` from `loki-homelab`
- **Storage:** `valheim-stats-data` (`longhorn`, **backed up**) — Loki keeps 31 days, so
  after that this DB is the only copy of the totals
- **Scraped by:** claude-otel prometheus, `job_name: valheim-stats`
- **Dashboard:** `Apps/valheim-stats.json` → "Valheim — Player Stats"
- **Config in:** `daniel-box.yml` `containers_list` (must sort AFTER `loki-homelab`)

## What it does that terraria-stats cannot
**Deaths.** Terraria's vanilla console emits no death events (verified in that service's
Phase 0 and stated in its docstring). Valheim's does, as a sentinel: the same line that
reports a character spawn reports a death with the ZDO id `0:0`.

## The console, and why the parser looks different
Documented lifecycle (corroborated across the image's own `valheim-logfilter`, adaliszk's
mtail program, and mbround18/valheim-docker):

    Got handshake from client 76561198108936133       <- connect
    Got character ZDOID from Testvazz : 954855457:113  <- spawn (ALSO fires on respawn)
    Got character ZDOID from Testvazz : 0:0            <- death
    Closing socket 76561198108936133                   <- disconnect

Three consequences, each of which is a trap:

1. **Patterns are searched, never anchored at `^`.** This image wraps every console line
   in its own supervisord prefix (`Aug 13 16:53:42 supervisord: valheim-server …`).
   Terraria's image logs bare lines, so its parser anchors; copying that here matches
   nothing. `test_valheim_stats.py` pushes every parse case through the prefix so a regression to
   anchoring fails loudly.
2. **A spawn while a session is already open is a RESPAWN, not a new session.** Otherwise
   every death inflates the session count.
3. **A disconnect names only the SteamID**, so playtime needs a SteamID→name map. It is
   built by ADJACENCY — the handshake, then the next spawn — because the console never puts
   the ID and the character name on one line, which is also how the other published parsers
   do it. Two players handshaking before either spawns can cross-bind; rare at homelab
   scale, but a cross-bind mis-attributes **that session's** playtime permanently in SQLite
   — only later sessions bind correctly. **Deaths are unaffected** — they key off the
   name directly, which is why they are the more trustworthy half of the board.

## Verification status — read before trusting the numbers
The line formats above came from documentation and other projects, **not** from this
server: Valheim was archived in January, long before this Loki existed, so there is no
historical log anywhere to test against and there had been no players at the time of
writing. **The first real play session is the acceptance test.**

Two things exist to make a mismatch visible rather than silent:
- `valheim_stats_unmatched_player_lines_total` — player-shaped lines that did not parse.
  Expect a flat zero; a step up with frozen player metrics means upstream reworded.
- `valheim_connections` vs `valheim_players_online` — the server's own ~10 min heartbeat
  against the session-derived count. They should agree; a persistent gap means the session
  state machine has drifted. Both are on the dashboard side by side.

## Notable
- **No seed and no backfill.** Unlike terraria-stats (whose Docker-era SQLite was copied
  in), totals genuinely start at zero. `BACKFILL_DAYS=28` is a ceiling that keeps the first
  query under Loki's `max_query_length`, not an expectation of finding anything.
- Heartbeat lines are parsed for the gauge but kept **out** of the SQLite audit table —
  one every 10 minutes would dwarf the events worth reading.
- The script is Jinja-hostile (Prometheus exposition carries `{…}`), so the ConfigMap is
  built with `kubectl create --from-file`, never a template — as in terraria-stats.

## Editing
- Logic: `files/valheim_stats.py` · Tests: `tests/test_valheim_stats.py` (`uv run pytest ansible/roles/k8s/valheim-stats/tests`)
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "valheim-stats" -e target=daniel-box`
