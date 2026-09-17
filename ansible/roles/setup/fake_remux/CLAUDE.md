# `setup/fake_remux` — the fake-remux detector, reconciler and mkv repair on `daniel-box`

Three host crons and a heartbeat that find mislabeled re-encodes in the media library,
replace them through Sonarr, and repair mkv font-attachment names that break burned-in
subtitles. Applied by `initial_setup.yml` (`--tags fake_remux`) on `fake_remux_host`
(`group_vars/all.yml`, daniel-box); it is not in `containers_list`, so
`./scripts/deploy.sh --tags fake_remux` exits 2 on an unmatched tag:

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags fake_remux
```

The scripts and the config template moved INTO this role at the Docker uninstall
(2026-08-14) — `containers/autofix-bridge` had owned them since the sidecar era, and this
became their only consumer. Detail that predates the move — the design, the ledger shape, the
tunables, how to run either script by hand in report-only or shadow mode — is in
`roles/k8s/autofix-bridge/CLAUDE.md`, whose contract section names fake-remux replacement as
one of its actuator planes. This file carries what is specific to the host cron: the
schedule, the lock, and the contract below.

## Autonomous-role contract (it deletes media files with no human in the loop)

- **Scope / exclusions:**
  - **fake-remux scan** (`files/fake_remux_scan.py`, daily `fake_remux_scan_cron_hour`):
    ffprobes the library for files whose encoder tag or keyframe interval contradicts a
    "remux" filename, and seeds
    the ledger (`replacements.json` under `fake_remux_state_dir`). It writes the ledger and
    nothing else.
  - **fake-remux reconcile** (`files/fake_remux_replace.py`, every
    `fake_remux_replace_cron_minute`): for each ledger entry, asks Sonarr for a replacement,
    ffprobe-verifies the grab is genuine on this host, then deletes the fake. **Never** a
    delete before the replacement is verified; **never** an unmonitored episode; the
    per-scan cap (`autofix_fake_remux_max_per_scan`) bounds one run.
  - **mkv attachment repair** (`files/mkv_attachment_repair.py`, every
    `fake_remux_mkv_attachment_cron_minute`): rewrites an mkv header in place with
    `mkvpropedit` to rename a font attachment jellyfin-ffmpeg refuses. Header only, never
    a stream; ASS scripts reference fonts by family name, not attachment filename.
- **Mode (explicit + reversible):** `FAKE_REMUX_REPLACE_MODE` — `off` detects only,
  `shadow` logs the grabs it would make, `live` replaces. The template default is `shadow`;
  **`host_vars/daniel-box.yml` runs it `live`** (`autofix_fake_remux_replace_mode`).
  Returning to report-only is one var flip and a re-run.
- **Authoritative sources:** ffprobe on the file itself (host binary, `fake_remux_host_data_root`),
  Sonarr's API for monitored state and the grab. Never the filename alone — the filename is
  the thing that lies.
- **Abort valves:** the per-scan cap; the shared `fake_remux_lock` (`flock -w 600`, so the
  daily scan and the 20-minute reconcile never race on the ledger — they wait rather than
  skip, because a skipped tick is invisible); an empty Sonarr API key disables the scan
  outright (exit 0, monitor green — the failure mode of a wrong config path is silence, see
  `defaults/main.yml`).
- **Required evidence:** every action lands in the ledger and `outcomes.jsonl`; a live
  replacement is Discord-alerted. `fake-remux-health.sh` (every `fake_remux_health_cron_minute`)
  pushes three Kuma tiles from the scripts' state files, with max-ages derived from each
  cron's own cadence (`fake_remux_*_max_age_hours` — a grace shorter than the gap flaps, one
  much longer clears the DOWN it exists to make sticky).
- **Next-run review:** before widening scope (a looser detection threshold, a higher cap,
  a second library root), read `outcomes.jsonl` and the Discord log for what the previous
  runs actually replaced and what they got wrong.

## Notable

- **The two crons move together.** Scan and reconcile share one ledger; repointing one half
  without the other leaves entries nobody acts on, or actions with no seed.
- **Config paths are the ones autofix-bridge used**, on purpose: both scripts fall back to
  `/etc/autofix-fake-remux/config.env`, so the cron lines carry no override. A first attempt
  at renaming them read a config that did not exist and reported `disabled (no Sonarr API
  key)` — green, scanning nothing.
- **Sonarr is reached by ClusterIP, resolved at deploy time**, not by name: the host has no
  cluster DNS, and the `-k8s` ingress name mis-resolves on this node.
- Tests: `uv run pytest ansible/roles/setup/fake_remux/tests` runs the pure-logic suites.
