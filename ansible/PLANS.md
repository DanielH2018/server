# Future Plans

Lightweight idea backlog. Detailed, ready-to-execute work lives in
[`docs/superpowers/specs/`](../docs/superpowers/specs/) and
[`docs/superpowers/plans/`](../docs/superpowers/plans/); dependency upgrades are tracked by
the Renovate dependency dashboard.

## Backlog

- Tune bedroom air-quality alert thresholds (revisit ~2026-06-25, after ~1 week of baseline) —
  the four moderate + four SEVERE `threshold` binary-sensors in
  `home-assistant/templates/configuration.yaml.j2` (`binary_sensor.bedroom_{co2,pm2_5,voc,nox}_high`
  / `_severe`) shipped with STARTING values. Check the HA recorder history for
  `sensor.bedroom_airgradient_one_{carbon_dioxide,pm2_5,voc_index,nox_index}` (+ `_humidity`) and
  adjust `upper`/`lower`/`hysteresis` to the observed baseline — especially the Sensirion VOC
  (~100 baseline) and NOx (~1 + spikes) *index* sensors, which drift per room. Edit the config +
  redeploy `home-assistant`. Spec: `docs/superpowers/specs/2026-06-18-bedroom-air-quality-alerts-design.md`.
  In the SAME pass, tune the OUTDOOR PM2.5 thresholds added 2026-06-20 —
  `binary_sensor.outdoor_pm2_5_high` (`upper: 35`) / `_severe` (`upper: 100`), source
  `sensor.outdoor_pm2_5` (Open-Meteo) — against the observed outdoor baseline, and revisit the
  window-advisor constants in `home-assistant/files/custom_templates/ventilation.jinja`
  (`comfort_lo/hi 55/78`, `cool_delta 5`, `pm_safe 25`) if the "Open the window?" nudges prove
  too eager or too quiet. Spec: `docs/superpowers/specs/2026-06-20-ha-outdoor-aqi-and-window-advisor-design.md`.

## Superseded

Completed plans are recorded in git history and in their authoritative homes — each shipped
feature's rationale lives in its role `CLAUDE.md` and its `docs/superpowers/specs/` design doc.
This file keeps only the live backlog scannable; `git log -- ansible/PLANS.md` recovers the
full done-log if needed.
