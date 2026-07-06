# Auto-Remediation Consolidation: `autofix-bridge` + Disk Autoprune — Design

**Date:** 2026-07-06
**Status:** Design — pending user review
**Related:** `docs/superpowers/specs/2026-07-06-arr-autoblock-queue-warnings-design.md` (the arr remediation this generalizes), `ansible/roles/containers/monitor-bridge/` (the read-only twin this mirrors)

## Goal

Establish **one** generically-named home for automated remediations — the writer twin of
monitor-bridge — instead of spawning a new sidecar per fix. Concretely: rename the just-shipped
`arr-autoblock` role/container to `autofix-bridge`, and add **disk reclaim** as the first
*host-plane* remediation living in that same role. monitor-bridge stays the single reader and gains
one new check.

## Motivation & the two-plane constraint

monitor-bridge centralizes 31 read-only checks in one container because every check is an HTTP/metric
read doable from a locked-down, zero-privilege container. Its writer twin should centralize
remediations the same way — but remediations split across **two actuator planes**, and that split is
load-bearing for this design:

- **Containerized HTTP-API plane** — the existing `arr-autoblock` sidecar: a `read_only`,
  `cap_drop: ALL`, `no-new-privileges`, non-root loop that only makes HTTP calls (Sonarr/Radarr
  queue API) and **self-pushes** to its own Kuma push monitor. Future API-based auto-fixes slot in
  beside it.
- **Host plane** — disk reclaim needs the **docker daemon** (prune) and **host journald** (a config
  file + `systemctl restart`). journald config is *impossible* from any container; docker prune is
  only possible by mounting the docker socket, which would throw away the exact zero-privilege
  posture that makes the sidecar safe — and would *still* leave journald as a host task. So disk
  reclaim runs as a **non-root host cron** (the `ubuntu` user is in the `docker` group) that writes
  a state file, which monitor-bridge reads and pushes — identical to the repo's six existing host
  crons (kopia verify/b2/restore-drill, wg-easy pi-peers, crowdsec home-allowlist).

Both planes live in the one `autofix-bridge` role, so the *concept* is centralized even though the
*process* correctly differs by plane.

### Honest scope note (current pressure)

`/` is at **25 %** (108 G / 455 G, 328 G free); the "Root Disk" monitor pages at `DISK_MAX_PCT=90`.
Journald is 963 MB uncapped, but that is 0.16 % of the disk. **Neither remediation addresses any
current pressure.** Their value is preventive: a *standing* auto-fix that keeps the Root Disk monitor
from ever requiring manual `docker prune` as the media library and image churn grow, plus bounding
unbounded journald growth. This is hygiene, not a fix for a live problem — build it as a safety net,
not because the disk is filling.

---

## Part A — Rename `arr-autoblock` → `autofix-bridge` (behavior-preserving)

A pure rename of the role/container/script/tooling identifiers. **No remediation logic changes**;
the arr sidecar keeps `DRY_RUN=false` and every env/threshold it has today.

**Renamed:**
- Role dir `ansible/roles/containers/arr-autoblock/` → `ansible/roles/containers/autofix-bridge/`
- `container_name: arr-autoblock` → `autofix-bridge`; compose service key likewise
- Script `files/autoblock.py` → `files/autofix.py`; tests `files/test_autoblock.py` →
  `files/test_autofix.py` (module alias `autoblock` → `autofix` throughout the tests); the
  container `command`, the `:/app/autofix.py` bind mount, the `User-Agent`, and the startup/error
  log strings follow
- `containers_list` entry `name: arr-autoblock` → `autofix-bridge`
  (`ansible/inventory/host_vars/daniel-server.yml`)
- tasks/main.yml: task names + the `register:` var (`arr_autoblock_script` → `autofix_script`)
- Tooling refs: `pyproject.toml` testpaths, the `prek.toml` pytest `files` regex alternation, and
  the `WATCHTOWER_AUTOUPDATE` frozenset in `scripts/validate_compose_templates.py`

**Preserved deliberately (no churn):**
- The arr Kuma monitor keeps `kuma('arr-autoblock', name='Arr Auto-Block', …)` **and** the
  `arr_autoblock_push_token` secret. Monitor names describe the **check**, not the container —
  monitor-bridge (the container) pushes to monitors named "Root Disk", "Backup Freshness", etc. So
  `autofix-bridge` pushing to an "Arr Auto-Block" monitor is exactly the established model, and
  preserving the id avoids orphaning the live monitor + its push token.
- All arr remediation env/logic: `DANGEROUS_MSG_PATTERNS`, `CLIENT_ERROR_PATTERNS`, grace/blast
  caps, `DRY_RUN=false`.

**Cutover:** the rename creates a new container `autofix-bridge`; Ansible manages containers by name,
so the old `arr-autoblock` container is left orphaned and must be removed once:
`docker rm -f arr-autoblock`. Because the Kuma monitor is label-preserved, **no monitor cleanup is
needed**. (Documented as a one-time deploy step in the plan.)

**No premature framework (YAGNI):** `autofix.py` remains the arr-queue remediation module — we do
**not** build a plugin/orchestrator loop now. The role is the generic *home*; when a second
*containerized API* remediation actually arrives, that work extracts the shared loop. The file is
named after its container (a common, honest convention), not as a claim of present generality.

---

## Part B — Disk autoprune (host-plane remediation, in the `autofix-bridge` role)

### B1. Prune script — `files/autofix-disk-prune.sh.j2` → `/usr/local/bin/autofix-disk-prune.sh`

Bash, `set -euo pipefail`, shellcheck-clean, `0750`, owned by `sys_user`. Mirrors
`kopia/files/verify.sh` (capture-exit-code + `jq` state, never `| logger`-masked). Runs as
`sys_user` (in the `docker` group — no root needed for docker prune).

Logic:
1. Read `/` used-percent via `df -P /` (the filesystem holding `/var/lib/docker`).
2. **Threshold gate:** if used% `< {{ autofix_disk_threshold_pct | default(80) }}` (below the 90
   pager, so reclaim happens *before* Root Disk fires) → write state `ok`,
   `"N% < 80%, no prune needed"`, exit 0. A healthy no-op keeps the monitor green — the
   home-allowlist "IP unchanged" fast-path idiom. (At 25% today this is the normal path.)
3. Otherwise run the **conservative** reclaim set, each capturing reclaimed bytes:
   - `docker image prune -f` (dangling layers only — **never `-a`**; `-a` would delete pinned
     rollback images and images of stopped-but-kept containers, fighting the Watchtower/Renovate
     pinning strategy and forcing re-pulls)
   - `docker builder prune -f` (unused build cache)
   - `docker container prune -f` (stopped containers)
   - Never touches named volumes or tagged/in-use images.
4. Re-measure used%; write `{ts, ok, msg}` via `jq` with a `before% → after%, reclaimed …` summary.
5. `ok=false` (fail-loud) **only** on a prune command error. A disk that stays high after a clean
   prune is *real data*, which is **Root Disk's** alert, not this one — single-purpose monitors, no
   double-paging.
6. **`DRY_RUN`** (`{{ autofix_disk_dry_run | default(false) }}`): when true, log
   `docker system df` reclaimable + would-prune targets and write an `ok` state **without pruning**.
   Default `false` (dangling/cache/stopped/journal reclaim is far lower-risk than the arr media
   writes, and reversible); the toggle exists for a report-only first look.

### B2. journald cap — `files/60-autofix-journald.conf` drop-in

A **standing** host config (not a cron): `/etc/systemd/journald.conf.d/60-autofix-journald.conf`
with `SystemMaxUse={{ autofix_journald_max | default('200M') }}`, applied with `become: true` and a
`systemctl restart systemd-journald`. Mirrors the Pi's `60-homelab-pi.conf` (`optimize_pi`). Bounds
journald growth permanently (reclaims ~760 MB on first restart). rsyslog stays enabled — Loki holds
the queryable long-term logs, so a 200 MB journald retention is ample; the value is tunable per host.

*Placement rationale:* a `containers/` role editing host journald is unusual, but this role's remit
is now "auto-remediation incl. host-plane disk hygiene," and keeping the cap beside the prune cron
makes the disk work cohesive and iterable via `deploy.yml --tags autofix-bridge` (vs. splitting it
into the `initial_setup.yml`-only `setup/` path). Justified inline in the task.

### B3. Wiring — `tasks/main.yml` (additions alongside the arr sidecar)

- Create `/var/lib/autofix-disk-prune` (`sys_user`-owned, `0755`) so the non-root cron writes
  `state.json` and monitor-bridge reads it RO — the home-allowlist ownership pattern.
- Template the prune script; install the journald drop-in (`become: true`) + restart.
- `ansible.builtin.cron` **hourly**, `user: {{ sys_user }}`, job `/usr/local/bin/autofix-disk-prune.sh`.
- Block-tag the new tasks `config` / `deploy` / `cron` per repo convention.
- Templated tunables (host_vars-overridable): `autofix_disk_threshold_pct` (80),
  `autofix_journald_max` (200M), `autofix_disk_dry_run` (false).

---

## Part C — monitor-bridge gains one check (the single reader)

Mirrors `check_home_allowlist` / `pi_peers` exactly (state-file freshness check):

- Pure `disk_prune(state, age_s, max_age_s)` → `(ok, msg)`: `ok=False` if `not state["ok"]`
  (last prune errored) or `age_s > max_age_s` (cron stalled / never ran); else `ok=True` with the
  before→after summary. Plus `check_disk_prune()` reading `/autofix-disk/state.json`
  (`FileNotFoundError` → "no state (never ran?)"; parse error → "state unparseable").
- Compose: RO bind mount `/var/lib/autofix-disk-prune:/autofix-disk:ro`; new **"Disk Autoprune"**
  Kuma push monitor via `kuma(…, monitor_type='push', max_retries=0)` +
  `monitor_bridge_disk_prune_push_token` (32-alnum, `auto` tier) added to env and the label.
- Register in `CHECKS`. **Not** in `PROM_DEPENDENT`/`LOKI_DEPENDENT` (pure state-file read, like
  verify/pi_peers). Staleness `DISK_PRUNE_MAX_AGE` ≈ 3 h (3× the hourly cron + slack).
- **Ordering (documented, not a hard dep):** on a fresh host deploy `autofix-bridge` before
  `monitor-bridge` so `/var/lib/autofix-disk-prune` exists sys_user-owned before the bind mount is
  created (else Docker auto-creates it root-owned and the non-root reader can't read it). Do **not**
  add it to monitor-bridge `role_deps` — those are only prometheus/uptime-kuma/kopia; the other
  state-dir providers (gitops_deploy/renovate_notify/wg-easy) are documented orderings in
  `monitor-bridge/CLAUDE.md`, relying on dir persistence, precisely so a `--tags monitor-bridge`
  partial deploy doesn't drag the arr container (and sonarr/radarr) in via dep-closure. Add the same
  one-line ordering note to that CLAUDE.md.
- Update `monitor-bridge/CLAUDE.md`: 31 → 32 checks, add the token to the push-token list, describe
  the "Disk Autoprune" monitor.

---

## Safety rails (the arr-autoblock parallels)

- **Threshold gate** = the grace analog (act only under sustained/real pressure).
- **Conservative scope** = bounded blast radius (never `-a`, never volumes/tagged images).
- **Fail-loud + dead-man**: any prune command error → `ok=false` → Kuma down; a dead cron → stale
  state → Kuma down.
- **Fully reversible**: pruned dangling layers / build cache / stopped containers / trimmed journal
  all regenerate. No live container, named volume, or tagged image is touched.
- **Single-purpose monitors**: "disk still full of real media" stays Root Disk's alert; "the reclaim
  job is unhealthy" is Disk Autoprune's. No double-paging.

## Testing

- monitor-bridge `test_check.py`: pure `disk_prune()` cases — ok / last-run-failed / stale / missing
  state (mirror the `verify`/`pi_peers` tests); the `CHECKS`-membership + `PROM/LOKI_DEPENDENT`
  guards already assert new checks are wired correctly.
- The arr rename is behavior-preserving — the existing (renamed) `test_autofix.py` suite is the
  guard; a green run proves the rename didn't alter logic.
- Prek `test_prek_pytest_files_cover_testpaths` guard must stay green after the `pyproject`/`prek.toml`
  edits. Full `uv run pytest` + `ruff` + `ansible-lint` + `validate_compose_templates` + `gitleaks`.
- Smoke: `docker exec autofix-bridge python /app/autofix.py --once` (arr unchanged);
  `/usr/local/bin/autofix-disk-prune.sh` run once → state.json + green "N% < 80%" no-op.

## Rollout & secrets

- New secret `monitor_bridge_disk_prune_push_token` (32-alnum, `auto` tier) via the `/add-secret`
  flow (`sops` set → `secret_rotation.py sync`). The arr token is unchanged.
- Deploy order: `autofix-bridge` first (recreates the container, installs the disk cron + journald
  cap; then `docker rm -f arr-autoblock`), then `monitor-bridge` (adds the check + bind mount).
- **Local master, unpushed** per standing preference (push only when asked). `DRY_RUN=false`
  default; flip via `autofix_disk_dry_run` + redeploy to return to report-only.

## Out of scope (noted, not built)

- A plugin/orchestrator framework inside `autofix.py` (deferred until a 2nd containerized API
  remediation exists).
- rsyslog↔journald log duplication on the server (a separate, larger reclaim; not this work).
- Any second *API* remediation (Prowlarr indexers etc. were evaluated and rejected as auto-fix
  candidates — they self-heal via backoff or need a human; see the conversation analysis).
