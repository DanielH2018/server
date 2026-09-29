# autofix-bridge — generic auto-remediation (the writer twin of monitor-bridge)

The homelab's **auto-remediation home** — where a read-only monitor-bridge signal earns a
sanctioned automatic *fix*. Renamed from `arr-autoblock` (2026-07-06) to stop proliferating a
sidecar per fix, and moved in-cluster at the Docker uninstall (2026-08-14) with its behaviour
and contract unchanged.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "autofix-bridge"`
- **Image:** `python` (`autofix_bridge_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — observability — auto-remediation
  loop; a broken deploy silently stops fixing arr issues. ALSO no readinessProbe (probe-less) —
  two independent reasons
<!-- /generated_from -->

- **Stdlib only** (no build, no extra deps) · **No web UI**, no Authelia
- **Host:** daniel-box — pinned by `nodeSelector`, so a daniel-server drain or cold boot
  cannot take the remediation loop down with it
- **Reaches:** `sonarr:8989` and `radarr:7878` (queue read, blocklist and search writes),
  `uptime-kuma:3001` (push), and the *arr Discord webhook
- **Depends on:** sonarr, radarr, uptime-kuma (`meta/deps.yml`)

## Autonomous-role contract (it changes state with no human in the loop)
This is a **change-producing autonomous role**, so its authority is written down and a change
here must satisfy this summary.
- **Scope / exclusions:** *arr queue remediation and fake-remux replacement, and nothing else.
  **Never** a delete before the replacement is ffprobe-verified genuine, **never** a legit
  in-progress download — the sidecar acts on a bare `trackedDownloadStatus=error`, which at
  the pinned *arr versions only `RejectedImportService` writes, and a client or VPN outage
  empties the queue rather than filling it with errors.
- **Mode (per actuator, explicit + reversible):** sidecar `DRY_RUN` (live) and fake-remux
  `FAKE_REMUX_REPLACE_MODE` (off/shadow/live, run `live` by host_vars). Returning either
  plane to report-only is one env flip and a redeploy — preserve that.
- **Authoritative sources:** the *arr `/api/v3/queue` and ffprobe truth, never a cached
  guess.
- **Abort valves:** `GRACE_CYCLES`, plus `MAX_ACTIONS_PER_CYCLE` / `MAX_PER_SCAN` — a mass
  match reads as a systemic cause, so it acts on **none** and alerts.
- **Required evidence:** every cycle writes a `{ts,ok,msg}` state file or push heartbeat that
  monitor-bridge reads, and a live action is Discord-alerted. No silent mutation.
- **Next-run review:** before widening scope or flipping a plane to `live`, read the last
  run's outcomes (`outcomes.jsonl`, the Discord log).

## Two actuator planes (the load-bearing design point — don't merge them)
1. **Containerized HTTP-API plane** — the zero-privilege sidecar (`files/autofix.py`), which
   polls the *arr queues and auto-blocklists stuck or poisoned items. **LIVE
   (`DRY_RUN=false`)** since 2026-07-06, bounded by `GRACE_CYCLES=3`,
   `MAX_ACTIONS_PER_CYCLE=5` and `DANGEROUS_MSG_PATTERNS`. A fourth valve,
   `CLIENT_ERROR_PATTERNS`, was retired 2026-09-18 (#1951) as inert at the pinned *arr
   versions, and **an *arr bump re-opens that**: re-run #1951's verify-by first.
2. **Host plane** — the two fake-remux crons, doing work the locked-down container cannot
   (`docker exec`, ffprobe), each reporting through a state file monitor-bridge reads. Both
   run as `sys_user` in the docker group, never root.
   - **The scan never deletes or re-searches**: it flags a file whose quality claims a Remux
     but whose video stream is a re-encode, and seeds the ledger. It ffprobes through
     jellyfin's read-only media mount, so a probe cannot write and jellyfin being down skips
     files rather than flagging them.
   - **The reconcile searches first and deletes last** — it grabs a candidate, waits for the
     download, ffprobes it, and **only then** deletes the fake and lets Sonarr import.
     `FAKE_REMUX_REPLACE_MODE` gates it; the template default is `shadow`, but **daniel-box
     runs `live`** by inventory override and that is the intended setting. Don't "restore" it.
   - **disk-autoprune retired 2026-08-14, with no successor.** Nothing prunes disk on the
     cluster nodes, so monitor-bridge's Root Disk pager is alerting without remediation —
     deliberately.

`docs/autofix-bridge-actuators.md` has each actuator's mechanics, the evidence that retired
`CLIENT_ERROR_PATTERNS`, the tunables and the policy file's selection knobs.

## Notable
- **Two Kuma monitors, on purpose:** a liveness tile as the fast dead-man for a hard crash,
  and a push monitor as the loop's per-cycle heartbeat on a 600s backstop.
- **RENAME GOTCHA — don't "fix" it:** the **push monitor id, token and env are deliberately
  kept** `arr-autoblock` / `arr_autoblock_push_token` / `KUMA_PUSH_ARR_AUTOBLOCK`, because a
  monitor names the *check* rather than the container and renaming loses its history. A grep
  hitting `arr-autoblock` here is CORRECT, not a missed rename.
- **journald cap is NOT owned here.** It lives solely in initial_setup's `50-homelab.conf`. A
  `60-` drop-in this role once shipped silently won, because systemd merges drop-ins
  last-wins-by-filename, so the role now REMOVES any stale `60-autofix-journald.conf`.
- **Deploy `autofix-bridge` before `monitor-bridge`**, which bind-mounts the fake-remux state
  dir `:ro`. Both state files are seeded on first deploy so its two checks cannot false-DOWN
  on a fresh host.
- **Don't re-propose the rejected auto-fix candidates** — prowlarr indexers, b2, recyclarr
  and targets were surveyed and refused. See [[autofix-bridge-auto-remediation]].

## Editing & testing
- Sidecar: `files/autofix.py`, mounted from a ConfigMap along with monitor-bridge's
  `bridge/common.py` (`defaults/main.yml`'s `autofix_bridge_modules` names both). **Never
  fork a second copy of `bridge/common.py` here** — edit monitor-bridge's. The
  `checksum/autofix-script` annotation hashes every staged module, because a ConfigMap change
  alone does not restart a Deployment.
- Manifests: `templates/deployment.yaml.j2`, `templates/env-secret.yaml.j2`
- The two fake-remux crons live in `ansible/roles/setup/fake_remux/files/`;
  `docs/autofix-bridge-actuators.md` carries the command that runs either by hand safely.
- Unit tests: `uv run pytest ansible/roles/k8s/autofix-bridge/tests` and `uv run pytest
  ansible/roles/setup/fake_remux/files`.
- Deploy: `./scripts/deploy.sh --tags "autofix-bridge"`
