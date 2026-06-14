# Future Plans

Lightweight idea backlog. Detailed, ready-to-execute work lives in
[`docs/superpowers/specs/`](../docs/superpowers/specs/) and
[`docs/superpowers/plans/`](../docs/superpowers/plans/); dependency upgrades are tracked by
the Renovate dependency dashboard.

## Backlog

- Player stats for Terraria (deaths, time on server, etc.) — NOT possible on vanilla
  natively: the server keeps no player stats and exposes no API/metrics. The only data
  source is the console log, which DOES print joins/leaves/deaths/chat — but only once
  players actually connect (none had yet as of 2026-06-14; external access was still
  timing out). Two realistic paths, each its own project — scope before building:
  - (a) a log-parsing sidecar tailing `docker logs terraria` → a small stateful store.
    Caveats: cumulative deaths/playtime are reconstructed forward-only (no history before
    the parser starts), reset-prone across restarts, and keyed on character name (not a
    stable id). Surface in Grafana / Homepage / Kuma.
  - (b) migrate to a TShock server (real player stats via a DB plugin) — but that's
    different server software and risks the just-stabilized vanilla world-persistence work.
  Revisit once external play works — nothing to measure until someone can connect.

## Superseded

- Add Healthcheck to Terraria — done 2026-06-14: probe reads `/proc/net/tcp` for a LISTEN
  on port 7777 (hex `1E61`, state `0A`) rather than opening a connection — Terraria's
  binary protocol logs every accepted socket as `<ip> is connecting...`, so a `/dev/tcp`
  connect probe would spam the game console ~2400×/day. `start_period=120s` covers the
  ~74s world-load before the port binds. Deployed + verified healthy with zero console
  noise; rationale lives in the terraria role's `CLAUDE.md`.

- Add a second SOPS age recipient — done 2026-06-11: operator generated the key
  off-box (private half lives only in the password manager), pubkey
  `age1npl…feyp` added to `ansible/.sops.yaml` as the third recipient (server, Pi,
  recovery), `sops updatekeys` re-wrapped secrets.yml, decrypt verified. Remaining
  operator step: test the recovery path once from a non-infra machine
  (`SOPS_AGE_KEY_FILE=… sops -d ansible/vars/secrets.yml`). NB `sops updatekeys`
  must run from `ansible/` — it resolves `.sops.yaml` from CWD, not the file path.

- Pi Ubuntu update — done by 2026-06-11: daniel-pi reports Ubuntu 24.04.4 LTS
  (`lsb_release -ds`, OpenSSH 9.6p1 noble build). Nothing left to run.

- Plan Meilisearch upgrade path — done 2026-06-10: policy = track the version karakeep
  officially tests (1.41.0), not latest; Renovate's newer offers deliberately sit in the
  manual group. Upgraded 1.37.0→1.41.0 via the upstream-blessed wipe-and-reindex (the
  index is disposable by design); full runbook lives as a comment on the meilisearch
  service in the karakeep compose template.

- Delete the dangling Docker volumes orphaned by retired services — done 2026-06-10
  (operator approved): `file-browser_filebrowser_{config,db,db_file}` + `promtail_config`
  removed with `docker volume rm`.
- Decide on pinning recyclarr — done 2026-06-10 (operator approved): pinned to 8.3.2,
  `watchtower.enable=false`, Renovate-managed via the existing compose-.j2 regex manager
  (plain semver tags, no extra packageRule needed).

- Fix Speedtest to use tz instead of UTC timezone — done 2026-06-10: results are stored
  UTC by design (APP_TIMEZONE deliberately left default); the UI was UTC because
  `DISPLAY_TIMEZONE` *also* defaults to Etc/UTC — now set to `{{ tz }}`
  (America/Chicago).

- Make sure n8n is up-to-date and redeployed often. — done 2026-06-10: it already was
  (`FROM n8nio/n8n:latest` + `build.pull: true` + the Sunday 06:05 redeploy cron), but the
  audit found the OTHER built images (crowdsec, code-server, peanut, ical-proxy) used bare
  `build: .` with no pull — their redeploy crons rebuilt on the cached base forever and
  delivered nothing. All four now set `build.pull: true` like n8n.
- Fix 'Email-to-RSS' not showing in VS Code file explorer. — done 2026-06-10: it was
  hidden by `files.exclude` in `.vscode/settings.json` (added back when it was an
  untracked foreign clone; it's a tracked submodule now). Explorer shows it; search still
  skips its `node_modules`/`.wrangler`.

- Optimize Pi Setup, connect this server to it for Claude. — done 2026-06-08: wired
  server→Pi SSH so deploys can be driven remotely; fixed the failing `initial_setup.yml`
  (apt-show-versions/AIDE OOM-killed mid-dpkg on the 512 MB Zero 2 W → disk swapfile before
  the heavy apt; `max-load=24` watchdog rebooting mid-apt → stop it during provisioning;
  `uv`/`prek` `become:false` tasks using root's HOME → resolve the connecting user's). Host
  now provisions clean (`failed=0`); container stack still via `deploy.yml`.
