# autofix-bridge — generic auto-remediation (the writer twin of monitor-bridge)

The homelab's **auto-remediation home** — where a read-only monitor-bridge signal earns a
sanctioned automatic *fix*. Renamed from `arr-autoblock` (2026-07-06) to stop proliferating a
sidecar per fix, and moved in-cluster at the Docker uninstall (2026-08-14) with its behaviour
and contract unchanged.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=autofix-bridge` with exit 5
- **Image:** `python` (`autofix_bridge_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Stdlib only**, no web UI, no Authelia
- **Host:** daniel-box — pinned by `nodeSelector`, so a daniel-server drain or cold boot
  cannot take the remediation loop down with it
- **Reaches:** `sonarr:8989` and `radarr:7878` (queue read, blocklist and search writes),
  uptime-kuma at its entry's `port` (push), and the *arr Discord webhook
- **Depends on:** sonarr, radarr and uptime-kuma, at runtime
- **Flux applies it, not Ansible** (D5.19, the first Wave D PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. To
  give it back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain
  revert of the flag leaves two writers on the objects. The Deployment is `Recreate` with one
  replica, so a roll stops the loop until the new pod starts. It has no readinessProbe, so the
  Kustomization's `wait` reads the pod Ready as soon as its container starts, as the Ansible
  rollout wait did.
- **The env Secret substitutes from `homelab-cluster-vars`.** The Kuma push token, the
  Discord webhook and the two *arr API keys are `${<key>}` placeholders in the published
  render. Flux substitutes the values from the `homelab-cluster-vars` Secret, which it
  decrypts from `ansible/vars/cluster-vars.sops.env`. The Kustomization depends on
  `cluster-vars`, so a rotated value is live before autofix-bridge reconciles, and the restart
  checksum rolls the pod. A rotation reaches the cluster only through `cluster_vars.py write`.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`
  with `manifests_rollout: autofix-bridge`, and no Flux path runs it. It stays for the
  handback. The Kustomization's `wait` replaces the rollout wait, and the restart checksum
  replaces the shared restart.

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
  plane to report-only is one env flip, merged (the sidecar's reaches the pod through
  `promote`) or deployed (fake-remux's through its setup role) — preserve that.
- **Authoritative sources:** the *arr `/api/v3/queue` and ffprobe truth, never a cached
  guess.
- **Abort valves:** `GRACE_CYCLES`, plus `MAX_ACTIONS_PER_CYCLE` / `MAX_PER_SCAN` — a mass
  match reads as a systemic cause, so it acts on **none** and alerts.
- **Required evidence:** every cycle writes a `{ts,ok,msg}` state file or heartbeat that
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
   (host ffprobe on the media files), each writing a state file `fake-remux-health.sh` pushes
   to Kuma. Both run as `sys_user`, never root.
   - **The scan never deletes or re-searches**: it flags a file whose quality claims a Remux
     but whose video stream is a re-encode, and seeds the ledger. It ffprobes the file on the
     host, under `fake_remux_host_data_root`.
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
  a push monitor as the per-cycle heartbeat on a 600s backstop.
- **RENAME GOTCHA — don't "fix" it:** the **push monitor id, token and env are deliberately
  kept** `arr-autoblock` / `arr_autoblock_push_token` / `KUMA_PUSH_ARR_AUTOBLOCK`, because a
  monitor names the *check* rather than the container and renaming loses its history. A grep
  hitting `arr-autoblock` here is CORRECT, not a missed rename.
- **journald cap is NOT owned here.** It lives in initial_setup's `50-homelab.conf`. A `60-`
  drop-in this role once shipped silently won, because systemd merges drop-ins
  last-wins-by-filename, so the role now REMOVES any stale one.
- **Don't re-propose the rejected auto-fix candidates** — prowlarr indexers, b2, recyclarr
  and targets were surveyed and refused ([[autofix-bridge-auto-remediation]]).

## Editing & testing
- Sidecar: `files/autofix.py`, mounted from a ConfigMap along with monitor-bridge's
  `bridge/common.py` and setup/common's `kuma_push.py`, the Kuma push every host pusher shares
  (`defaults/main.yml`'s `autofix_bridge_modules` names all three). **Never fork a second copy
  of either here** — edit the owning role's. `templates/script-configmap.yaml.j2` embeds
  every module into the rendered ConfigMap `autofix-bridge-script`, so an edit to any of the
  three changes this role's render. Under Ansible the shared role's rollout restart then rolls
  the pod. Under Flux the renderer's restart annotation (`RESTART_ANNOTATION` in
  `scripts/renderer/renderer_lib/restart.py`) hashes the ConfigMap into the pod template. `scripts/renderer/tests/test_renderer_autofix_bridge_script.py` holds both the
  embed and the checksum.
- Manifests: `templates/deployment.yaml.j2`, which carries every setting as an `env` value,
  `templates/env-secret.yaml.j2`, which carries only the credentials, and
  `templates/script-configmap.yaml.j2`. A setting never goes in the Secret: Flux's typed patch
  reads a Secret's quoted `"300"` back as a number and refuses it (#5574).
- The two fake-remux crons live in `ansible/roles/setup/fake_remux/files/`;
  `docs/autofix-bridge-actuators.md` carries the command that runs either by hand safely.
- Tests: `uv run pytest ansible/roles/k8s/autofix-bridge/tests` and `uv run pytest
  ansible/roles/setup/fake_remux/tests`.
