# Future Plans

Lightweight idea backlog. Detailed, ready-to-execute work lives in
[`docs/superpowers/specs/`](../docs/superpowers/specs/) and
[`docs/superpowers/plans/`](../docs/superpowers/plans/); dependency upgrades are tracked by
the Renovate dependency dashboard.

## Backlog

- Tune bedroom air-quality alert thresholds (revisit ~2026-06-25, after ~1 week of baseline) —
  the four `threshold` binary-sensors in `home-assistant/templates/configuration.yaml.j2`
  (`binary_sensor.bedroom_co2_high` / `_pm2_5_high` / `_voc_high` / `_nox_high`) shipped with
  STARTING values (CO₂ 1200/100, PM2.5 35/5, VOC 250/25, NOx 50/10). Check the HA recorder
  history for `sensor.bedroom_airgradient_one_carbon_dioxide` / `_pm2_5` / `_voc_index` /
  `_nox_index` and adjust `upper`/`hysteresis` to the observed baseline — especially the
  Sensirion VOC (~100 baseline) and NOx (~1 + spikes) *index* sensors, which drift per room.
  Edit the config + redeploy `home-assistant`. Spec:
  `docs/superpowers/specs/2026-06-18-bedroom-air-quality-alerts-design.md`. (2026-06-18)

- HA night-time "got up" dim nightlight — between ~00:00–05:00, presence/PIR
  (`binary_sensor.aqara_fp300_pir_detection` / `_presence`) → the warm dim `scene.bedroom_nightlight`
  *instead* of full lighting, so a night trip doesn't blast you. Drops in as another time-based
  exception in `script.bedroom_apply_natural` (above the morning-wake exception) — presence-on
  already routes through the dispatcher. (2026-06-18)

- HA distance-zoned lighting — use `sensor.aqara_fp300_target_distance` so full lighting only
  engages when you're actually up and across the room (e.g. >1.5 m), staying dim while you're at/near
  the bed. Refines `bedroom_presence_on` beyond binary on/off; tune the distance band against
  observed values. (2026-06-18)

- HA DND-aware notification routing — respect `sensor.pixel_9_pro_do_not_disturb_sensor`: hold or
  soften *routine* alerts (air quality, humidity) while DND/asleep, but let *critical* ones (UPS,
  sensor-offline) bypass via a high-priority Android notification channel (`data: {channel,
  importance: high}` on `notify.mobile_app_pixel_9_pro`). A cross-cutting layer over every alert. (2026-06-18)

- HA actionable notifications — add action buttons to existing/future alerts via the companion app
  (`data: {actions: [...]}` + a `mobile_app_notification_action` event handler): air-quality alert →
  "Turn on fan"; away alert → "Turn off lights"; low-battery → "Snooze". One tap instead of opening
  the app. (2026-06-18)

- HA update-available digest — notify when a device/HA update appears via the `update.*` entities
  (`update.aqara_fp300`, `update.0x001788010f0ccda4` Tap Dial, `update.bedroom_airgradient_one_firmware`,
  plus HA core/OS). Extends the repo's Renovate/IaC update discipline to the one corner it doesn't
  cover — Zigbee/sensor firmware. **Notify only — never auto-flash Zigbee firmware.** (2026-06-18)

- HA unexpected-occupancy tripwire — if `binary_sensor.aqara_fp300_presence` turns on while
  `device_tracker.pixel_9_pro` is **away** (short debounce; reuse the home/away logic), alert: someone
  in the bedroom when you're not home. Pure logic over two sensors already trusted elsewhere — no new
  hardware. Pairs with the home/away backlog item. (2026-06-18)

- HA sleep-quality-aware morning — read `sensor.pixel_9_pro_sleep_duration`; if you slept under ~6 h,
  soften or slightly delay the wake ramp and add a "you slept N h" note. Hooks the dispatcher's
  morning-wake exception in `files/scripts.yaml` (keep the `bedroom_presence_on` window template in
  sync). Depends on the Pixel feeding Health Connect sleep data into HA. (2026-06-18)

- HA AirGradient CO₂ calibration reminder — the SenseAir CO₂ sensor drifts; remind every few months to
  run `button.bedroom_airgradient_one_calibrate_co2_sensor`, ideally after the room's been aired to the
  ~400 ppm outdoor baseline. Keeps the air-quality alert thresholds honest. Hygiene — low excitement,
  real accuracy benefit. (2026-06-18)

- If I sit at my desk for a while, the lights turn off, please try to tune it to lessen that happening.
- Smooth out Fan curve for temperature(Currently goes in steps of 2)
- Rename Devices in Zigbee2MQTT

## Superseded

- HA dynamic morning wake to the real alarm — done 2026-06-18: new `sensor.bedroom_wake_start`
  (template timestamp = `sensor.pixel_watch_3_next_alarm − 15 min`, availability-gated to morning
  alarms 03:00–11:00) is the single source of truth for the wake window. `bedroom_morning_reset`
  now time-triggers `at: sensor.bedroom_wake_start` (+ a 09:00 fallback for no-alarm-day override
  hygiene), and both `bedroom_apply_natural`'s morning exception and `bedroom_presence_on`'s window
  read it — killing the triplicated 06:00/07:00 formula. Uses the WATCH alarm (per operator), not
  the phone's. Template sensors now live in a copy'd `files/templates.yaml` (HA Jinja can't go in the
  Ansible-templated `configuration.yaml.j2`). Spec:
  `docs/superpowers/specs/2026-06-18-ha-dynamic-morning-wake-design.md`.

- HA automatic bedtime / sleep routine — done 2026-06-18: `script.bedroom_bedtime` (shared by
  `automation.bedroom_bedtime` off `binary_sensor.pixel_watch_3_bedtime_mode` + Tap Dial button-1
  hold) engages `input_boolean.bedroom_sleep_mode` (a quiet fan cap), AL sleep mode, and
  `scene.bedroom_nightlight`. **Fan stays temperature-responsive but quieter** — `bedroom_apply_fan`
  caps the band to Low when sleep_mode is on (layered on the Medium night cap), NOT a frozen speed.
  Charging deliberately not used (operator charges in-room). Morning reset unwinds sleep_mode + AL
  sleep mode. Spec: `docs/superpowers/specs/2026-06-18-ha-bedtime-sleep-routine-design.md`. Wake
  alarm will use `sensor.pixel_watch_3_next_alarm` (watch, not phone) when the morning-wake item lands.

- HA humidity comfort alerts + unified threshold engine — done 2026-06-18: two one-sided humidity
  `threshold` sensors (high `upper:60`, low `lower:30`) over `sensor.bedroom_airgradient_one_humidity`,
  folded into a NEW unified `bedroom_threshold_alert` automation that **replaced** the separate
  `bedroom_air_quality_alert` + `bedroom_battery_low_alert` (the "full unification" option). One
  engine over three categories (air quality / battery / humidity), category encoded in the trigger
  `id`, with a `cfg` map for the only per-category differences (title + whether to pulse the lights
  — air quality only). Spec:
  `docs/superpowers/specs/2026-06-18-ha-humidity-and-unified-threshold-alerts-design.md`. Humidity
  thresholds join the ~2026-06-25 air-quality tuning pass.

- HA home/away automations — done 2026-06-18: off `person.daniel` (HA person entity over the
  Pixel tracker). `bedroom_away` (two-stage: `leave` at 10 min away, `failsafe` at 30 min) turns
  off the bedroom lights + fan and notifies what was on; `bedroom_arrive_home` nudges the fan back
  and re-checks lights if already in the room (no forced-on). The load-bearing work was gating
  every on-path on `person.daniel == home` (`bedroom_fan_temperature`, `bedroom_presence_on`, and
  the morning reset's direct `apply_fan`) so nothing switches on in an empty house; the override
  booleans are never written by home/away logic. Spec:
  `docs/superpowers/specs/2026-06-18-ha-home-away-automations-design.md`. Prereq for the
  unexpected-occupancy tripwire item.

- HA low-battery alerts — done 2026-06-18: `bedroom_battery_low_alert` notifies when the FP300 or
  Tap Dial battery crosses ~15% (with a recovery notice on a fresh battery), via two lower-bound
  `threshold` binary-sensors (`lower: 20, hysteresis: 5`) + one generic notify automation — a
  near-exact twin of `bedroom_air_quality_alert`, notify-only (no light pulse). Anchored on off↔on
  so an offline device (owned by the sensor-offline alert) can't false battery-alert. Spec:
  `docs/superpowers/specs/2026-06-18-ha-low-battery-alerts-design.md`. Noted the refactor point:
  unify air-quality + battery + future humidity into one threshold-alert engine.

- HA sensor-offline alerts — done 2026-06-18: `bedroom_sensor_offline_alert` notifies when a
  bedroom-automation dependency (AirGradient ONE, FP300, Tap Dial, DREO fan) goes `unavailable`
  for 5 min, with a coalescing-tag recovery notice — a structural twin of the air-quality alert.
  **Root finding:** the two battery Zigbee devices (FP300, Tap Dial) couldn't fail loudly because
  Z2M availability was OFF (their entities never went `unavailable`); enabling
  `availability.enabled: true` in the zigbee2mqtt role (passive timeout 60 min — battery radios
  can't be actively pinged, so detection is ~hour-coarse, not minutes) is the load-bearing fix.
  Spec: `docs/superpowers/specs/2026-06-18-ha-sensor-offline-alerts-design.md`. Scope added the
  DREO fan beyond the original three. Out of scope (separate backlog items): DND/critical routing,
  actionable buttons.

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
