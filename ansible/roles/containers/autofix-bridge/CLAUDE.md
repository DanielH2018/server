# autofix-bridge — generic auto-remediation (the writer twin of monitor-bridge)

The homelab's **auto-remediation home** — where a read-only monitor-bridge signal earns a
sanctioned automatic *fix*. Renamed from `arr-autoblock` (2026-07-06) to stop proliferating a
sidecar per fix. See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `python:3.14-alpine` (stdlib only — no build, no extra deps) · **No web UI**, no Authelia
- **Host:** daniel-server
- **Networks:** `media` (reach `sonarr:8989` / `radarr:7878` — queue read + blocklist/search
  writes) + `monitoring` (push to `uptime-kuma:3001` AND egress to the *arr Discord webhook)
- **Depends on:** sonarr, radarr, uptime-kuma (`meta/deps.yml`)
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`
- **Spec:** `docs/superpowers/specs/2026-07-06-autofix-bridge-disk-autoprune-design.md`

## Two actuator planes (the load-bearing design point — don't merge them)
1. **Containerized HTTP-API plane** — the zero-privilege sidecar (`files/autofix.py`). Polls
   sonarr/radarr `/api/v3/queue` and auto-blocklists stuck/poisoned items. Fully hardened
   (non-root, `read_only` + tmpfs `/tmp`, `cap_drop:[ALL]`, `no-new-privileges`). **LIVE
   (`DRY_RUN=false`)** since 2026-07-06 — it actually blocklists+removes+re-searches. Blast-radius
   valves: `GRACE_CYCLES=3` (an item must stay a candidate ~15 min first), `MAX_ACTIONS_PER_CYCLE=5`
   (a mass-flag = systemic cause → act on NONE + alert), `DANGEROUS_MSG_PATTERNS` (the poisoned-`.exe`
   class), and `CLIENT_ERROR_PATTERNS` — a download-client/VPN outage is EXCLUDED so a legit
   in-progress download isn't wrongly blocklisted (see [[qbittorrent-bind-wg0]]). Flip
   `DRY_RUN=true` + redeploy to return to report-only.
   **Second module — fake-remux scan (`run_fake_remux_scan`, daily):** sweeps the Sonarr library
   for files whose quality claims a **≤1080p Remux** but whose MediaInfo codec is **HEVC** — a
   re-encode mislabeled as a remux (a real ≤1080p BD remux is AVC; 2160p HEVC remuxes are excluded;
   unknown resolution fails safe). Unless `FAKEREMUX_DRY_RUN` — its **OWN** gate, **default `true`**
   because it DELETEs library files — it deletes each fake + re-searches the series (the Anime
   profile's NTRX block via the configarr role keeps the re-grab clean). `FAKEREMUX_MAX_PER_SCAN`
   is its blast valve (a whole-library match → act on none + alert). Left report-only on purpose so
   it flags the current mislabeled Mushoku S2 files without deleting them.
2. **Host plane** — the disk-autoprune cron (`templates/autofix-disk-prune.sh.j2` →
   `/usr/local/bin/`, hourly `minute:0`, runs as `sys_user` ∈ docker group, no root). Host work
   (docker daemon) **can't** run in the locked-down container, so it lives beside it as a cron
   reporting via a state file — like the fleet's other host crons. Rails: threshold-gated (prune
   only when `/` used% ≥ `autofix_disk_threshold_pct`, default 80, below monitor-bridge's
   `DISK_MAX_PCT=90` pager) → conservative `docker image/builder/container prune -f` (**never `-a`,
   never volumes**; `container prune` carries `--filter until=24h` so a stopped container kept for
   forensics survives a day) → jq state `{ts,ok,msg}` → `/var/lib/autofix-disk-prune/state.json`.

## Notable
- **Two Kuma monitors, on purpose:**
  - **docker-liveness** `{{ kuma('autofix-bridge') }}` (AutoKuma polls the socket ~60s,
    `maxretries=2`) surfaces a hard crash in ~2-3 min — the fast dead-man for this live writer.
  - **push** `{{ kuma('arr-autoblock', monitor_type='push', …, max_retries=0) }}` — the
    remediation loop's per-cycle heartbeat + descriptive alert; the slower 600s backstop.
- **RENAME GOTCHA — don't "fix" it:** the role/container/script are `autofix-bridge`/`autofix.py`,
  but the **push monitor id + token + env are deliberately kept** `arr-autoblock` /
  `arr_autoblock_push_token` / `KUMA_PUSH_ARR_AUTOBLOCK`. A monitor names the *check*, not the
  container (same as monitor-bridge pushing to "Root Disk"), so keeping them preserves the Kuma
  monitor's history. A compose grep hitting `arr-autoblock` here is CORRECT, not a missed rename.
- **journald cap is NOT owned here.** It lives SOLELY in initial_setup's `50-homelab.conf` (1G — a
  reasoned host-forensics window). A prior version of this role shipped a `60-` `SystemMaxUse=200M`
  drop-in that silently won (systemd merges drop-ins last-wins-by-filename), cutting the journal 5x
  and turning the 1G into dead config. The role now REMOVES any stale `60-autofix-journald.conf` so
  there is one source of truth for the journald cap. Don't reintroduce a journald drop-in here.
- **Deploy `autofix-bridge` before `monitor-bridge` on a fresh host** — monitor-bridge bind-mounts
  `/var/lib/autofix-disk-prune:/autofix-disk:ro` for its **Disk Autoprune** check; this role creates
  that dir `sys_user`-owned first (else Docker auto-creates the mount source root-owned and the
  non-root container can't read it). The state file is **seeded on first deploy** (a `command:` +
  `creates:` mirroring the kopia/wg-easy pattern) so Disk Autoprune doesn't false-DOWN for up to an
  hour on a fresh host / bare-metal DR, before the first hourly tick.
- **`disk_prune` reports only its OWN failure** (`ok=false` on a prune-command error). A disk still
  full of *real data* after a clean prune is **Root Disk's** alert — single-purpose monitors, no
  double-paging. At `/` ≈ 25% the prune is preventive standing hygiene (it takes the green no-op
  path today); it exists so Root Disk never needs a manual prune as image churn grows.
- **Auto-fix survey verdict (don't re-propose):** the *arr queue was the best-fit case in the
  fleet; disk was the one other genuinely-additive one. prowlarr indexers / b2 / recyclarr / targets
  were evaluated and REJECTED (self-heal via backoff, or autoheal/watchtower already cover
  restarts/images, or need a human). See [[autofix-bridge-auto-remediation]].
- **Tunables (host_vars):** `autofix_disk_threshold_pct`, `autofix_disk_dry_run`.

## Editing & testing
- Sidecar: `files/autofix.py` (bind-mounted `:ro`; a code edit needs a **recreate** — the role
  wires `common_config_changed` off the script's register so a script-only edit still recreates).
- Disk-prune cron: `templates/autofix-disk-prune.sh.j2` · Compose: `templates/docker-compose.yml.j2`
- Unit tests: `uv run pytest ansible/roles/containers/autofix-bridge` (`files/test_autofix.py`).
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "autofix-bridge"`
