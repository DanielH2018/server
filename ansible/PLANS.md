# Future Plans

Lightweight idea backlog. Detailed, ready-to-execute work lives in
[`docs/superpowers/specs/`](../docs/superpowers/specs/) and
[`docs/superpowers/plans/`](../docs/superpowers/plans/); dependency upgrades are tracked by
the Renovate dependency dashboard.

## Backlog

- AirGradient air-quality automations — use the `airgradient` integration (CO₂, PM1/2.5/10,
  TVOC/NOx) to drive the bedroom bulbs (e.g. tint red on high CO₂/PM) and/or push a phone
  notification. Reuse the existing `light.bedroom_lights` group + scenes; respect the
  `input_boolean.bedroom_manual_off` override and avoid fighting Adaptive Lighting (likely use a
  distinct alert scene/flash rather than a steady color). Brainstorm → spec/plan when picked up.
  (2026-06-18)

## Superseded

- Player stats for Terraria — done 2026-06-15: shipped the `terraria-stats` sidecar
  (Loki → SQLite → Prometheus → Grafana) tracking all-time per-player playtime, sessions,
  and presence. **Deaths were dropped:** a Phase 0 LAN capture proved the vanilla console
  emits only `<name> has joined.`/`has left.` (no deaths/chat — character data is
  client-side without SSC), and deaths would need a TShock+SSC migration (evaluated,
  rejected). The Terraria container is untouched. First deploy backfilled 30d of history
  (found players Ben + DBoy). Spec + plan under `docs/superpowers/`; role docs in
  `ansible/roles/containers/terraria-stats/CLAUDE.md`.

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
