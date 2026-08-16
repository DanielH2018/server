# autofix-bridge — generic auto-remediation (the writer twin of monitor-bridge)

> **This IS the k8s role since the Docker uninstall (2026-08-14).** The sidecar moved
> in-cluster at the Phase F drain; the Docker role's last piece — the disk-autoprune
> cron — retired with the daemon it pruned, and `files/autofix.py` (+ its tests) moved
> into this role. Sections below describing Docker-era plumbing (networks, compose,
> containers_list) are the loop's HISTORY; the behavior and contract they document are
> unchanged in the pod. Doc-path pointers: the Docker role is archived at
> `roles/containers/archive/autofix-bridge/`.

The homelab's **auto-remediation home** — where a read-only monitor-bridge signal earns a
sanctioned automatic *fix*. Renamed from `arr-autoblock` (2026-07-06) to stop proliferating a
sidecar per fix. See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `python:3.14-alpine` (stdlib only — no build, no extra deps) · **No web UI**, no Authelia
- **Host:** daniel-box — pinned there by `nodeSelector`, so the drain and cold boot of
  daniel-server cannot take the remediation loop down with it
- **Reaches:** `sonarr:8989` / `radarr:7878` (queue read + blocklist/search writes),
  `uptime-kuma:3001` (push), and the *arr Discord webhook (egress)
- **Depends on:** sonarr, radarr, uptime-kuma (`meta/deps.yml`)
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`
- **Spec:** `docs/superpowers/specs/2026-07-06-autofix-bridge-disk-autoprune-design.md`
  (historical — its disk-autoprune half retired 2026-08-14, see the host plane below)

## Autonomous-role contract (it changes state with no human in the loop)
This is a **change-producing autonomous role** — its authority is written down so a future edit can't
quietly widen it (harness-engineering's versioned-contract pattern). The facts below live in detail
elsewhere in this doc; this is the governed summary a change here must satisfy.
- **Scope / exclusions:** *arr queue remediation, host disk hygiene, fake-remux replacement — and
  nothing else. **Never** `docker prune -a`, **never** volumes, **never** a delete before the
  replacement is ffprobe-verified genuine, **never** a legit in-progress download (VPN/client-outage
  patterns are held out).
- **Mode (per actuator, explicit + reversible):** sidecar `DRY_RUN` (live) · fake-remux
  `FAKE_REMUX_REPLACE_MODE` (off/shadow/live; template default
  `shadow`, but **host_vars runs it `live`** — see the reconciler bullet below).
  Returning any plane to report-only is one env flip + redeploy — preserve that.
- **Authoritative sources:** sonarr/radarr `/api/v3/queue`, ffprobe truth (via `docker exec jellyfin`
  on daniel-server; directly on the host wherever `JELLYFIN_CONTAINER` is empty, e.g. daniel-box),
  `/` used-%. Never a doc or a cached guess.
- **Abort valves:** `GRACE_CYCLES`, `MAX_ACTIONS_PER_CYCLE` / `MAX_PER_SCAN` (a mass-match = systemic
  cause → act on **none** + alert), the disk threshold gate.
- **Required evidence:** every cycle writes a `{ts,ok,msg}` state file / push heartbeat monitor-bridge
  reads; a live action is Discord-alerted. No silent mutation.
- **Next-run review (the cumulative-judgment clause):** before widening scope or flipping a plane to
  `live`, reconcile the last run's outcomes (`outcomes.jsonl`, the Discord log) — don't repeat a
  class of mis-fire the previous run already surfaced.

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
2. **Host plane** — two daily/hourly crons doing work the locked-down container can't (docker
   daemon, `docker exec`, ffprobe), each reporting via a `{ts,ok,msg}` state file monitor-bridge
   reads. Both run as `sys_user` ∈ docker group (no root).
   - **disk-autoprune — RETIRED 2026-08-14, no successor.** It pruned daniel-server's Docker
     daemon, which was uninstalled that day; the template survives only under
     `roles/containers/archive/`, `autofix_disk_threshold_pct`/`autofix_disk_dry_run` are set
     nowhere, and monitor-bridge dropped the matching `disk_prune` check with it
     (`monitor-bridge/files/check.py:167-170`). **Nothing prunes disk on the cluster nodes now**
     — containerd's own image GC is the only reclaim, and monitor-bridge's Root Disk threshold
     pager (`DISK_MAX_PCT=90`) is the only signal. That is alerting without remediation, which
     is a deliberate state, not an oversight: revisit if `/` pressure ever becomes routine.
   - **fake-remux scan** (`files/fake_remux_scan.py` + pure `fake_remux_logic.py` →
     `/opt/autofix-fake-remux/`, daily `04:45`, config in `/etc/autofix-fake-remux/config.env`
     0600). ffprobe-backed detection of files whose quality claims a **Remux** but whose video
     stream is a re-encode — **long GOP** (a real remux keyframes ~every 1-2 s; > `GOP_MAX_S`=5 =
     a re-encode) **or a consumer re-encoder ENCODER tag** (`x264`/`x265`/`*_qsv`/`*_nvenc`/`Lavc`/
     handbrake…, the cheap metadata-only tell). This **supersedes** the old codec heuristic that
     lived in the sidecar (`autofix.py`, removed 2026-07-17): definitive + codec/resolution/size-
     independent, so it catches an AVC-remux-that's-really-an-AVC-reencode and needs no 2160p
     exclusion. It runs ffprobe via **`docker exec jellyfin`** — jellyfin mounts the media
     **read-only** at `/data/media`, so Sonarr's absolute path resolves **unchanged** (no
     translation) and a probe can't write; jellyfin being down just SKIPS files (fail-safe, never
     flags). It never deletes or re-searches itself — each newly found fake is **seeded into the
     ledger** (`/var/lib/autofix-fake-remux/replacements.json`) for the reconciler below to act on.
     `MAX_PER_SCAN`=5 blast valve (a whole-library match → act on none + alert). Pure core is
     unit-tested (`test_fake_remux_logic.py`).
   - **fake-remux reconcile** (`files/fake_remux_replace.py` + pure
     `fake_remux_replace_logic.py` → `/opt/autofix-fake-remux/`, every 20 min, same config.env).
     Search-first replacer: reads the ledger the scan seeded, interactive-searches Sonarr for a
     clean replacement (`autofix_fake_remux_policy` → rendered `/etc/autofix-fake-remux/
     policy.json` picks the candidate — deny/prefer release groups, preferred indexers, a
     depreferenced-but-not-banned codec list, a size band), grabs it, waits for the download,
     ffprobes it the same way the scan does, and only deletes the fake + lets Sonarr import once
     the replacement is verified genuine — never before. The delete goes through Sonarr's episode-
     file DELETE API, so whether the fake lands in the OS trash or is removed outright is entirely
     Sonarr's own Media Management → Recycling Bin setting, not something this policy controls.
     `FAKE_REMUX_REPLACE_MODE` is the gate: `off` = detect only, `shadow` = log intended grabs to
     `outcomes.jsonl` with zero Sonarr mutations, `live` = grab+delete+import. **daniel-box
     runs `live`** (`autofix_fake_remux_replace_mode` in `host_vars/daniel-box.yml`) — it deletes
     and re-grabs for real. (It moved there with the media stack on 2026-08-08; this line said
     daniel-server until 2026-08-16, which is a file that does not contain the key at all.) This line claimed it shipped as `shadow` until 2026-08-08; the template default is
     `shadow`, but the inventory has overridden it to `live` and that is the intended setting,
     confirmed by the operator. Don't "restore" it to shadow.
     Ledger/outcome state all live under `/var/lib/autofix-fake-remux/`. See
     `docs/superpowers/specs/2026-07-17-fake-remux-auto-replacer-design.md` for the full design.

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
- **Auto-fix survey verdict (don't re-propose):** the *arr queue was the best-fit case in the
  fleet; disk was the one other genuinely-additive one. prowlarr indexers / b2 / recyclarr / targets
  were evaluated and REJECTED (self-heal via backoff, or autoheal/watchtower already cover
  restarts/images, or need a human; recyclarr itself was later retired 2026-07-17, replaced by
  configarr). See [[autofix-bridge-auto-remediation]].
- **fake-remux deploy ordering / seed:** the state dir `/var/lib/autofix-fake-remux` is created
  `sys_user`-owned + both state files are **seeded on first deploy** — `state.json` by running the
  scan once (`command:` + `creates:`), `replace_state.json` by writing a neutral placeholder
  (`copy:` + `force: false`, mirroring kopia's content-verify seed) — for the same reason as
  disk-prune: so monitor-bridge's **Fake Remux Scan** / **Fake Remux Replace** checks don't
  false-DOWN on a fresh host before the first tick. Deploy `autofix-bridge` before `monitor-bridge`
  (it bind-mounts the state dir `:ro`). Both host crons import the shared `host_lib.py` (copied from
  `roles/setup/common`) and run via `uv run --no-project --python <pin>`, the
  `host_python_version` pin in `ansible/inventory/group_vars/all.yml`.
- **Tunables (host_vars):** `autofix_fake_remux_gop_max_s`, `autofix_fake_remux_max_per_scan`;
  `autofix_fake_remux_replace_mode` (off/shadow/live), `autofix_fake_remux_policy` (the
  git-tracked selection-policy dict rendered to `policy.json`).

## Editing & testing
- Sidecar: `files/autofix.py`, staged to the node and mounted from a ConfigMap; the role's
  `checksum/autofix-script` annotation rolls the pod on a script-only edit.
- Manifests: `templates/deployment.yaml.j2`, `templates/env-secret.yaml.j2`
- The disk-prune cron and its template are gone — they pruned daniel-server's Docker daemon,
  uninstalled 2026-08-14, and monitor-bridge dropped the matching `disk_prune` check with them.
- The two fake-remux crons now live in `ansible/roles/setup/fake_remux/files/`; the paths in the
  next two bullets are relative to that role.
- fake-remux scan cron: `files/fake_remux_scan.py` (I/O shell) + `files/fake_remux_logic.py` (pure
  core) · config `templates/fake-remux.config.env.j2`. Run it live report-only:
  `SONARR_API_KEY=… ARR_DISCORD_WEBHOOK_URL= STATE_FILE=/tmp/x.json
  PYTHONPATH=ansible/roles/setup/common/files /usr/local/bin/uv run --no-project --python 3.14.6 files/fake_remux_scan.py`.
- fake-remux reconcile cron: `files/fake_remux_replace.py` (I/O shell) + pure
  `files/fake_remux_replace_logic.py`, same config.env. Run it shadow (no side effects):
  `FAKE_REMUX_REPLACE_MODE=shadow SONARR_API_KEY=… LEDGER_FILE=/tmp/l.json
  REPLACE_STATE_FILE=/tmp/rs.json OUTCOMES_FILE=/tmp/o.jsonl
  PYTHONPATH=ansible/roles/setup/common/files /usr/local/bin/uv run --no-project --python 3.14.6 files/fake_remux_replace.py`.
- Unit tests: `uv run pytest ansible/roles/k8s/autofix-bridge/files` (`test_autofix.py`) and
  `uv run pytest ansible/roles/setup/fake_remux/files` (the two fake-remux logic suites).
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "autofix-bridge"`
