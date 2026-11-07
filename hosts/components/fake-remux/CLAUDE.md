# `hosts/components/fake-remux` — the fake-remux detector, reconciler and mkv repair on `daniel-box`

Three host timers and a heartbeat that find mislabeled re-encodes in the media library,
replace them through Sonarr, and repair mkv font-attachment names that break burned-in
subtitles. `homelab-daniel-box` ships it. H7.37 moved it from the `fake_remux` role.

Detail that predates the move — the design, the ledger shape, the tunables, how to run either
script by hand in report-only or shadow mode — is in `roles/k8s/autofix-bridge/CLAUDE.md`, whose
contract section names fake-remux replacement as one of its actuator planes. This file carries
what is specific to the host timers: the schedule, the lock, and the contract below.

## Autonomous-role contract (it deletes media files with no human in the loop)

- **Scope / exclusions:**
  - **fake-remux scan** (`fake_remux.py scan`, shell in `files/fake_remux_lib/fake_remux_scan.py`, daily `fake_remux_scan_cron_hour`):
    ffprobes the library for files whose encoder tag or keyframe interval contradicts a
    "remux" filename, and seeds
    the ledger (`replacements.json` under `fake_remux_state_dir`). It writes the ledger and
    nothing else.
  - **fake-remux reconcile** (`fake_remux.py replace`, shell in
    `files/fake_remux_lib/fake_remux_replace.py`, every
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
  Returning to report-only is one var flip and the next package version.
- **Authoritative sources:** ffprobe on the file itself (host binary, `fake_remux_host_data_root`),
  Sonarr's API for monitored state and the grab. Never the filename alone — the filename is
  the thing that lies.
- **Abort valves:** the per-scan cap; the shared `fake_remux_lock` (`flock -w 600`, so the
  daily scan and the 20-minute reconcile never race on the ledger — they wait rather than
  skip, because a skipped tick is invisible); an empty Sonarr API key disables the scan
  outright (exit 0, monitor green — the failure mode of a wrong config path is silence, see
  `defaults/main.yml`).
- **Required evidence:** every action lands in the ledger and `outcomes.jsonl`; a live
  replacement is Discord-alerted. A post the host could not deliver waits in
  `/var/lib/autofix-fake-remux/discord-spool/` and goes out, marked as delayed, at the end of
  the next run of any of the three crons, so within 15 minutes of the network returning (#3905). `fake-remux-health` (every `fake_remux_health_cron_minute`)
  writes one textfile verdict per state file (`fake-remux-scan`, `fake-remux-replace`,
  `mkv-attachment-repair`) to node_exporter's textfile directory, with max-ages derived from
  each cron's own cadence (`fake_remux_*_max_age_hours` — a grace shorter than the gap flaps,
  one much longer clears the DOWN it exists to make sticky) and a deadline of two health periods
  plus 300 s rendered from `fake_remux_health_cron_minute`. The rules in
  `ansible/roles/k8s/observability/files/rules/other-setup-checks.yaml` read them (M6 batch B4),
  and a `status=` syslog line per state file carries the message.
- **Next-run review:** before widening scope (a looser detection threshold, a higher cap,
  a second library root), read `outcomes.jsonl` and the Discord log for what the previous
  runs actually replaced and what they got wrong.

## Notable

- **One entrypoint, two subcommands.** `files/fake_remux.py` dispatches to the two shells in
  `files/fake_remux_lib/`, a namespace package with no `__init__.py`, which also holds their
  pure cores. The package installs it as `fake_remux_lib/` beside the entrypoint.
- **The scan and the reconcile move together.** Scan and reconcile share one ledger; repointing one half
  without the other leaves entries nobody acts on, or actions with no seed.
- **Config paths are the ones autofix-bridge used**, on purpose: both scripts fall back to
  `/etc/autofix-fake-remux/config.env`, so the units carry no override. A first attempt
  at renaming them read a config that did not exist and reported `disabled (no Sonarr API
  key)` — green, scanning nothing.
- **Sonarr is reached by its pinned ClusterIP**, not by name: the host has no cluster DNS, and
  the `-k8s` ingress name mis-resolves on this node. A package cannot read the cluster when it
  renders, so `sonarr_k8s_cluster_ip` in `ansible/roles/k8s/sonarr/defaults/main.yml` and
  `fake_remux_sonarr_cluster_ip` in `defaults/main.yml` hold the same address, and
  `hosts/lib/tests/test_h737_box_components.py` holds the two equal.
- Tests: `uv run pytest hosts/components/fake-remux/tests` runs the pure-logic suites.
