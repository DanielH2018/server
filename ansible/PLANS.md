# Future Plans

Lightweight idea backlog. Detailed, ready-to-execute work lives in
[`docs/superpowers/specs/`](../docs/superpowers/specs/) and
[`docs/superpowers/plans/`](../docs/superpowers/plans/); dependency upgrades are tracked by
the Renovate dependency dashboard.

## Backlog

- HA → Uptime-Kuma heartbeat (catch a wedged-but-running Home Assistant) — review item 4.1 in
  `docs/home-assistant-review-2026-06-19.md`. The AutoKuma label only proves the HA *container* is up;
  a wedged automation engine (stuck event loop / locked recorder) still serves HTTP `:8123` but fires
  no alerts, and nothing external notices. Fix = HA actively pushes a Kuma **Push** monitor every
  ~5 min, so the watchdog lives outside HA. Three steps: (1) create a Kuma Push monitor (heartbeat
  ~10 min, **retries 0** — a missed push parks in PENDING otherwise, per the kuma-push-down lesson),
  copy its push URL; (2) add the token as SOPS secret `ha_kuma_heartbeat_url` + a
  `rest_command: ha_kuma_heartbeat` (GET `<url>&status=up&msg=ok`) in
  `home-assistant/templates/configuration.yaml.j2`; (3) a `time_pattern: minutes:"/5"` automation in
  `home-assistant/files/automations.yaml` calling it. Pairs with the shipped 1.2 persistent_notification
  fallback (1.2 surfaces a critical alert if push fails; 4.1 catches HA being dead entirely).
  Ready-to-paste code in the review doc §4.1. Caveat: not a perfect liveness proof (a partial wedge
  may still tick the timer) but catches the common scheduler/loop-stuck failures the HTTP check misses.

- Tune bedroom air-quality alert thresholds (revisit ~2026-06-25, after ~1 week of baseline) —
  the four moderate + four SEVERE `threshold` binary-sensors in
  `home-assistant/templates/configuration.yaml.j2` (`binary_sensor.bedroom_{co2,pm2_5,voc,nox}_high`
  / `_severe`) shipped with STARTING values. Check the HA recorder history for
  `sensor.bedroom_airgradient_one_{carbon_dioxide,pm2_5,voc_index,nox_index}` (+ `_humidity`) and
  adjust `upper`/`lower`/`hysteresis` to the observed baseline — especially the Sensirion VOC
  (~100 baseline) and NOx (~1 + spikes) *index* sensors, which drift per room. Edit the config +
  redeploy `home-assistant`. Spec: `docs/superpowers/specs/2026-06-18-bedroom-air-quality-alerts-design.md`.

## Superseded

- HA setup review pass — done 2026-06-18 (full rationale in the home-assistant role `CLAUDE.md`):
  - **Tap Dial template-warning fix** — `bedroom_tap_dial_control` now gates on `action is defined`
    so Z2M state reports (battery/link-quality) no longer log `'dict object' has no attribute 'action'`.
  - **CO₂ calibration reminder** — `bedroom_co2_calibration_reminder` (quarterly notify-only nudge to
    recalibrate the AirGradient against the ~400 ppm outdoor baseline; replaces the paper backlog note).
  - **UPS power-event alert** — `ups_power_event` off the raw NUT flags (outage / low-battery /
    restored); nothing watched the UPS before.
  - **Dashboard** — air-quality card (CO₂ gauge + pollutant glance) + a Bedroom Controls card (lights,
    AL master, the three override booleans).
  - **Memory cap 1024M → 1536M** — HA idled at ~810M (79% of the old hard cap); measured right-size.
  - **Investigated the unclean-SQLite-shutdown warning** — found it's NOT fixable via
    `stop_grace_period` (HA is SIGKILLed at 30s AND 90s; the warning is benign, WAL auto-recovers);
    the briefly-added grace was reverted.

- Bedroom Home Assistant automation suite — done 2026-06-18 (16 changes; full rationale in
  `docs/superpowers/specs/2026-06-18-ha-*` + the home-assistant & zigbee2mqtt role `CLAUDE.md`):
  - **Unified `bedroom_threshold_alert` engine** — one `cfg`-map-driven automation over air-quality
    (4 pollutants + a SEVERE tier), battery, and humidity `threshold` sensors (replaced the separate
    air-quality + battery automations).
  - **`script.bedroom_notify`** — single cross-cutting notify layer: DND/sleep-aware channel +
    importance (routine goes silent while quiet; `pierce` → high-importance "Bedroom critical"
    channel that bypasses DND), `watch` mirroring to the Pixel Watch, and actionable buttons.
  - **Reliability alerts:** sensor-offline (enabled Z2M `availability`), low-battery, humidity.
  - **Presence & lighting:** home/away off `person.daniel` (every on-path gated on `== home`),
    night-time "got up" dim nightlight, bedtime routine (quiet fan cap + AL sleep mode + nightlight),
    dynamic morning wake off the **watch** alarm (`sensor.bedroom_wake_start`), and a
    sleep-quality-aware gentler wake after a short night.
  - **Security/maintenance:** unexpected-occupancy tripwire (watch + pierce), weekly
    update-available digest (notify-only).
  - **Fan:** smooth ~1-level-per-°F temperature curve (full 9 DREO levels); FP300 tuning
    (mmwave / high sensitivity / 60 s absence delay) so sitting still at the desk no longer drops the lights.
  - **Z2M device renames:** Lamp / Left Light / Right Light / Tap Dial / Aqara FP300 (entity_ids stay
    IEEE-based; only the Tap Dial automation's raw MQTT topic moved).

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
