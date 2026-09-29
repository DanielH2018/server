# autofix-bridge actuators — how each plane works, and what it refuses to do

`ansible/roles/k8s/autofix-bridge/CLAUDE.md` is the role doc, and it keeps the contract: the
two planes stay separate, each is reversible by one env flip, and nothing deletes before a
replacement is verified genuine. This page is the working-out — each actuator's mechanics, the
valve that was retired and why, and the commands for running either cron by hand. A session
reads it when it edits an actuator (#2989).

## The sidecar's blast-radius valves

The sidecar (`files/autofix.py`) polls sonarr and radarr `/api/v3/queue` and auto-blocklists
stuck or poisoned items. It has been LIVE (`DRY_RUN=false`) since 2026-07-06: it blocklists,
removes and re-searches for real. Three valves bound it — `GRACE_CYCLES=3`, so an item must
stay a candidate for about 15 minutes first; `MAX_ACTIONS_PER_CYCLE=5`, so a mass flag reads
as a systemic cause and acts on NONE while alerting; and `DANGEROUS_MSG_PATTERNS`, the
poisoned-`.exe` class.

**A fourth valve was retired because it was inert.** `CLIENT_ERROR_PATTERNS` exempted a bare
`error` whose `errorMessage` named a download-client outage. It went on 2026-09-18 (#1951)
after all three of its premises were checked at the pinned Sonarr `4.0.19.2979` and Radarr
`6.3.0.10514`:

- A client outage returns no queue items rather than items at `error` —
  `DownloadMonitoringService.ProcessClientDownloads` catches `GetItems()` and records a client
  failure.
- `trackedDownloadStatus=error` is written only by `RejectedImportService`.
- None of the valve's five phrases is a string either app writes into `errorMessage`.

**A future *arr bump re-opens the question.** Re-run #1951's verify-by against the new tag
before trusting the bare-`error` branch.

## The host plane, and the prune that has no successor

Both host crons run as `sys_user` in the docker group, never root, and each reports through a
`{ts,ok,msg}` state file monitor-bridge reads.

**disk-autoprune retired 2026-08-14, with no successor.** It pruned daniel-server's Docker
daemon, which was uninstalled that day; the template survives only in git history,
`autofix_disk_threshold_pct` and `autofix_disk_dry_run` are set nowhere, and monitor-bridge
dropped the matching `disk_prune` check with it. **Nothing prunes disk on the cluster nodes**
— containerd's own image GC is the only reclaim, and monitor-bridge's Root Disk threshold
pager (`DISK_MAX_PCT=90`) is the only signal. That is alerting without remediation, which is a
deliberate state rather than an oversight: revisit it if `/` pressure ever becomes routine.

## fake-remux scan: what counts as fake

`files/fake_remux_scan.py` plus the pure `fake_remux_logic.py`, deployed to
`/opt/autofix-fake-remux/`, daily at `04:45`, configured from
`/etc/autofix-fake-remux/config.env` (0600). It is ffprobe-backed detection of files whose
quality claims a **Remux** but whose video stream is a re-encode, by either of two tells:

- **Long GOP.** A real remux keyframes about every 1-2s, so anything past `GOP_MAX_S`=5 is a
  re-encode.
- **A consumer re-encoder ENCODER tag** — `x264`, `x265`, `*_qsv`, `*_nvenc`, `Lavc`,
  handbrake and the like. This is the cheap metadata-only tell.

This **supersedes** the codec heuristic that lived in the sidecar until 2026-07-17: it is
definitive and independent of codec, resolution and size, so it catches an AVC remux that is
really an AVC re-encode and needs no 2160p exclusion.

It runs ffprobe via **`docker exec jellyfin`**, which matters twice: jellyfin mounts the media
read-only at `/data/media`, so Sonarr's absolute path resolves unchanged with no translation
and a probe cannot write; and jellyfin being down SKIPS files rather than flagging them, which
is fail-safe. The scan never deletes or re-searches itself — each newly found fake is seeded
into the ledger (`/var/lib/autofix-fake-remux/replacements.json`) for the reconciler.
`MAX_PER_SCAN`=5 is the blast valve, so a whole-library match acts on none and alerts. The
pure core is unit-tested in `test_fake_remux_logic.py`.

## fake-remux reconcile: search first, delete last

`files/fake_remux_replace.py` plus the pure `fake_remux_replace_logic.py`, every 20 minutes,
same config.env. It reads the ledger the scan seeded, interactive-searches Sonarr for a clean
replacement, grabs it, waits for the download, ffprobes it the same way the scan does, and
only deletes the fake and lets Sonarr import **once the replacement is verified genuine**.

`autofix_fake_remux_policy` renders `/etc/autofix-fake-remux/policy.json`, which picks the
candidate: denied and preferred release groups, preferred indexers, a depreferenced but not
banned codec list, and a size band.

The delete goes through Sonarr's episode-file DELETE API, so whether the fake lands in the OS
trash or is removed outright is Sonarr's own Media Management → Recycling Bin setting, not
something this policy controls.

`FAKE_REMUX_REPLACE_MODE` is the gate: `off` detects only, `shadow` logs intended grabs to
`outcomes.jsonl` with zero Sonarr mutations, and `live` grabs, deletes and imports.
**daniel-box runs `live`** (`autofix_fake_remux_replace_mode` in `host_vars/daniel-box.yml`),
so it deletes and re-grabs for real. The template default is `shadow`, but the inventory
override to `live` is the intended setting, confirmed by the operator — do not "restore" it to
shadow. Ledger and outcome state live under `/var/lib/autofix-fake-remux/`, and commit
`f99404c31` wired the reconciler.

## Deploy ordering and the first-deploy seed

The state dir `/var/lib/autofix-fake-remux` is created `sys_user`-owned, and both state files
are seeded on first deploy: `state.json` by running the scan once (`command:` with `creates:`),
`replace_state.json` by writing a neutral placeholder (`copy:` with `force: false`, mirroring
kopia's content-verify seed). The reason is the same one disk-prune had — monitor-bridge's
**Fake Remux Scan** and **Fake Remux Replace** checks would otherwise false-DOWN on a fresh
host before the first tick.

Deploy `autofix-bridge` before `monitor-bridge`, which bind-mounts the state dir `:ro`. Both
host crons import the shared `host_lib.py` (copied from `roles/setup/common`) and run via `uv
run --no-project --python <pin>`, the `host_python_version` pin in
`ansible/inventory/group_vars/all.yml`.

## Running either cron by hand

The two fake-remux crons live in `ansible/roles/setup/fake_remux/files/`; the paths below are
relative to that role.

Scan, live but report-only:

```bash
SONARR_API_KEY=… ARR_DISCORD_WEBHOOK_URL= STATE_FILE=/tmp/x.json \
  PYTHONPATH=ansible/roles/setup/common/files \
  /usr/local/bin/uv run --no-project --python 3.14.6 files/fake_remux_scan.py
```

Reconcile, in shadow so it has no side effects:

```bash
FAKE_REMUX_REPLACE_MODE=shadow SONARR_API_KEY=… LEDGER_FILE=/tmp/l.json \
  REPLACE_STATE_FILE=/tmp/rs.json OUTCOMES_FILE=/tmp/o.jsonl \
  PYTHONPATH=ansible/roles/setup/common/files \
  /usr/local/bin/uv run --no-project --python 3.14.6 files/fake_remux_replace.py
```

Unit tests: `uv run pytest ansible/roles/k8s/autofix-bridge/tests` for the sidecar, and `uv run
pytest ansible/roles/setup/fake_remux/files` for the two fake-remux logic suites.

## The survey that decided what this role does NOT fix

The *arr queue was the best-fit case in the fleet, and disk was the one other genuinely
additive one. prowlarr indexers, b2, recyclarr and targets were each evaluated and REJECTED —
they self-heal through backoff, or autoheal and watchtower already cover the restarts and
images, or they need a human. recyclarr itself was retired 2026-07-17 and replaced by
configarr. Do not re-propose these.

## Tunables, in host_vars

`autofix_fake_remux_gop_max_s` and `autofix_fake_remux_max_per_scan` bound detection;
`autofix_fake_remux_replace_mode` (off/shadow/live) gates the reconciler; and
`autofix_fake_remux_policy` is the git-tracked selection-policy dict rendered to
`policy.json`.
