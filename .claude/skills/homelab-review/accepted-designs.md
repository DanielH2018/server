# Accepted designs the reviewer agents must not re-flag

The design facts each reviewer agent used to carry inline, in one place. Three agents held
their own copy and the copies drifted (#2169). A reviewer reads its domain's section here
before it flags anything, alongside the *Settled findings* register in
`docs/reference/backlog.md`, which the docs-refresh cron renders from `findings.py`'s
`--accepted` and `--refuted` closes.

An entry here is a prior, not a verdict: contradict one with new evidence at a cited symbol
and name the entry you are contradicting. Each entry is also a candidate for the smaller
durable owner CLAUDE.md's *Review & Memory Hygiene* names: a `# DECIDED:` marker at the line
that makes the trade-off. When an entry moves there, delete it here.

## container

- qBittorrent must bind to `wg0`; its TCP healthcheck blind spot is known.
- configarr's Anime profile scope is deliberately minimal: only 2 local custom formats are
  managed, and the 52 bespoke ones are untouched.
- janitorr deletes for real.
- meili stays pinned until karakeep bumps its own pin.
- The LSIO "unable to set CAP_SETFCAP" warning is cosmetic.
- A doubled `$$` in a compose `healthcheck` or `command` is CORRECT (Compose `$` escaping),
  not a bug.

## cicd

- Discord urllib POSTs need a User-Agent; already fixed, so the rule applies only to a NEW
  direct-urllib POST.
- The Renovate LSIO regex rejects dev and legacy tags on purpose; silence is not up-to-date.
- The critical tier is PINNED, not auto-updated.
- meili stays pinned until karakeep bumps its own pin.
- pytest must NOT live under `ansible/filter_plugins/`; the plugin loader imports every `.py`
  there.

## backup-observability

- The B2 free tier IS the offsite.
- The no-backup volume tier is deliberate: TSDBs, uptime-kuma-data, crowdsec-db, and
  valheim's SteamCMD install volume are re-downloadable, while valheim's *world* volume IS
  backed up. `docs/longhorn-backup-tiering.md` is the tiering doc.
- The push-watchdog semantics are "down = no heartbeat".
- The Pi is monitored via static Kuma labels. The Pi's node-exporter (scrape job `node-pi`)
  feeds only Pi Pressure and Host Temp. Do NOT add the Pi to the instance-blind `node_*`
  disk/memory checks; `HOST_METRIC_ORIGIN_EXCLUDE` keeps it out of them.
