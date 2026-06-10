# Future Plans

Lightweight idea backlog. Detailed, ready-to-execute work lives in
[`docs/superpowers/specs/`](../docs/superpowers/specs/) and
[`docs/superpowers/plans/`](../docs/superpowers/plans/); dependency upgrades are tracked by
the Renovate dependency dashboard.

## Backlog

- Delete the dangling Docker volumes orphaned by retired services (data loss is the
  point — operator call): `file-browser_filebrowser_config`, `file-browser_filebrowser_db`,
  `file-browser_filebrowser_db_file` (file-browser role is archived), `promtail_config`
  (promtail's config is templated now). Found 2026-06-10; `docker system prune` never
  touches volumes, so they persist until an explicit
  `docker volume rm <name>...`.

## Superseded

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
