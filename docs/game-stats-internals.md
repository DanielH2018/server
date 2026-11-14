# game-stats internals — the Valheim parser, and how `stats_lib.py` reaches a pod

`k8s/roles/game-stats/CLAUDE.md` is the role doc: what each exporter is, where its
SQLite lives and which knobs cross a role boundary. This page is what a session needs only
when it edits the code — the Valheim console lifecycle its parser reads, the three traps in
reading it, how far it has been verified against real play, and the two-halved shipment that
puts `stats_lib.py` beside each entry script. Kept off the role doc the inject hook loads on
every touch of the role (#2985).

## What it does that terraria-stats cannot

**Deaths.** Terraria's vanilla console emits no death events (verified in that service's
Phase 0 and stated in its docstring). Valheim's does, as a sentinel: the same line that
reports a character spawn reports a death with the ZDO id `0:0`.

## The console, and why the parser looks different

Documented lifecycle (corroborated across the image's own `valheim-logfilter`, adaliszk's
mtail program, and mbround18/valheim-docker):

```text
Got handshake from client 76561198108936133       <- connect
Got character ZDOID from Testvazz : 954855457:113  <- spawn (ALSO fires on respawn)
Got character ZDOID from Testvazz : 0:0            <- death
Closing socket 76561198108936133                   <- disconnect
Game - OnApplicationQuit                           <- shutdown
ZNet.LoadWorld: Midgard (Midgard), save number 1750 <- start
 Connections 0 ZDOS:919563  sent:0 recv:0          <- heartbeat, every ~10 min
```

The last three lines are observed on this server. A shutdown or a start closes every open
session, and so does a heartbeat that reports 0 connections. That heartbeat is what recovers
a session whose `Closing socket` line was missed. A heartbeat whose count is above 0 but
below the open sessions closes nothing, because the count cannot say which session is stale,
and a wrong guess would bank the live player's hours on the stale one. Until the next
heartbeat that settles it, the exporter caps `valheim_players_online` at the server's count
and stops each open session's playtime at that player's last evidence, never below the
value the counter already showed. The live player's own
`Closing socket` then banks their hours, and the next count of 0 closes the stale session.
Until #8528 the restart pattern searched
for `Load world: `, a line this server never logs, so a missed disconnect left two players
online for three weeks. A reconciling close ends each session at the later of the player's
last event and the last heartbeat whose count covered every open session. Another player's
presence never extends a stale session, so the time it sat stale is not counted as playtime.

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

## Verification status — read before trusting the numbers

The line formats above came from documentation and other projects, **not** from this
server: Valheim was archived in January, long before this Loki existed, so there is no
historical log to test against, and the exporter shipped before anyone had played. Real play
since the 2026-09-09 fresh world has exercised the parser: on 2026-09-26 the exporter reported
375 deaths and `valheim_stats_unmatched_player_lines_total` at 0.

Two things exist to make a mismatch visible rather than silent:

- `valheim_stats_unmatched_player_lines_total` — player-shaped lines that did not parse.
  Expect a flat zero; a step up with frozen player metrics means upstream reworded.
- `valheim_connections` vs `valheim_players_online` — the server's own ~10 min heartbeat
  against the session-derived count. The derived count is capped at the server's while the
  two disagree, so it never reads higher. It can still read lower: a gap in that direction
  means a spawn line was missed or reworded. Both are on the dashboard
  side by side.

## Notable

- **No seed and no backfill.** Unlike terraria-stats (whose Docker-era SQLite was copied
  in), totals genuinely started at zero. `BACKFILL_DAYS=28` is a ceiling that keeps the first
  query under Loki's `max_query_length`, not an expectation of finding anything.
- Heartbeat lines are parsed for the gauge but kept **out** of the SQLite audit table —
  one every 10 minutes would dwarf the events worth reading.

## Shipping `stats_lib.py` beside each entry script

Both exporters run as `python:3.15-alpine` pods with `stats_lib.py` mounted alongside their
own entry script by a ConfigMap. A script invoked directly gets only its own directory on
`sys.path`, so a copy has to sit beside each script. Each game's ConfigMap is a rendered
template, `templates/configmap-terraria.yaml.j2` and `templates/configmap-valheim.yaml.j2`,
and each embeds its entry script and `stats_lib.py` with `lookup('file')`. The scripts never
pass through Jinja themselves, so their `{…}` exposition strings need no escaping.

Because the ConfigMap is part of the render, a script edit changes the render. The
`checksum/restart` that the renderer stamps hashes each ConfigMap into the pod that mounts it.
`scripts/renderer/tests/test_renderer_game_stats_restart.py` is the guard: each game's
ConfigMap carries both keys its pod imports, the pod runs the entry script from the mount,
and an edit to either file moves the `checksum/restart` of every pod that loads it.
