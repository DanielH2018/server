# janitorr — Automated media library cleanup

Deletes watched/old media and cleans up Sonarr/Radarr based on disk-usage rules.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "janitorr"`
- **Images:** `ghcr.io/schaka/janitorr` (`janitorr_k8s_image`), `python`
  (`janitorr_k8s_health_image`), `alpine/k8s` (`janitorr_k8s_health_kubectl_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `media-data` (not Longhorn (media-local))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — probe-less — no readinessProbe on the
  Deployment; also the only role that deletes real media, so a wedge is not merely inert
<!-- /generated_from -->

- **Digest-pinned to a `jvm-stable` build** (pulled 2026-06-28). `jvm-stable` is a floating non-semver
  alias Renovate can't version-track, and janitorr deletes real media, so updates are deliberate.
  **Manual update:** pull `jvm-stable`, take the new digest, update the k8s role's default, redeploy.
- **Host: daniel-box (k8s), since 2026-08-08 — slice 4, B7b.** The Docker role this config came
  from is gone; this role's Secret renders `templates/config/application.yml.j2`. Edit retention
  rules HERE; deploy with `--tags janitorr` from daniel-box.
- **No web UI** (background service) · targets the cluster sonarr/radarr/jellyfin
- **Its health signal is the `janitorr-health` CronJob** (`templates/health-cronjob.yaml.j2`),
  every 10 minutes. `files/janitorr_health.py` reads the running pod's container start time
  and, past a 10-minute startup grace, counts `Unexpected error occurred in scheduled task`
  lines in its log, through its own ServiceAccount (`templates/health-rbac.yaml.j2`). It pushes
  the "Janitorr Errors" Kuma monitor. Not gated by `k8s/cronjob-gate`: `k8s_autodeploy` is
  false here, so no batch gate is required.
- **The health reader was a daniel-box host cron until H2.5c.** The absent arms for the cron,
  `/opt/janitorr-health`, `/usr/local/bin/janitorr-health.sh` and the `janitorr` drift fragment
  live in `ansible/roles/setup/k8s_host_files/tasks/janitorr.yml`, which the setup plane
  applies through `initial_setup.yml`, so they survive janitorr's Flux flip.
- **The push URL, token included, lives in its own Secret, `janitorr-health`**
  (`templates/health-secret.yaml.j2`), and reaches the pod as `KUMA_PUSH_URL` through
  `secretKeyRef`. It is a whole URL rather than a bare token so that it stays out of
  `ansible/roles/k8s/manifests/files/standalone_secret_keys.json`. The reader's code ships in
  the `janitorr-health` ConfigMap, beside `host_lib.py` and `kuma_push.py` from `setup/common`,
  and no secret goes in it (`ansible/tests/k8s/test_no_secret_in_a_moved_jobs_configmap.py`).
- **The health pod carries `app: janitorr-health`, never `app: janitorr`.** The reader and
  `tasks/verify.yml` both take the first Running pod labelled `app=janitorr`. uptime-kuma's
  `netpol_from` admits `janitorr-health`.

## Notable
- Behaviour (retention rules, leaving-soon thresholds, dry-run flag) lives in
  `templates/config/application.yml.j2`. **It deletes files** — `dry-run` was flipped off
  2026-06-10 (operator decision after the initial trial period), so it now cleans for
  real. Tag media `janitorr-keep` in the *arrs to exempt it (see `exclusion-tags` in
  `application.yml.j2`). **NB:** Radarr/Sonarr reject underscores in tag labels
  (`^[a-z0-9-]+$`) and Janitorr matches by exact label — the upstream-doc `janitorr_keep`
  is uncreatable here, so the tags are hyphenated. Add a tag in Radarr via
  Settings→Tags or the API; Janitorr picks it up on its next run and drops the item
  from the Leaving Soon collection.
- Mounts the whole `containers/data` tree at `/data` (same as Sonarr/Radarr since the
  2026-07-02 hardlink-mount unification). Janitorr acts on media via the Sonarr/Radarr
  APIs; its direct filesystem use is `leaving-soon-dir` (where it writes the symlinks) and
  `free-space-check-dir`, both `/data`-relative. **Path-namespace trap:**
  `media-server-leaving-soon-dir` and the symlink targets are `/data/media/...` strings
  that JELLYFIN must resolve — jellyfin's primary mount puts the media tree at `/data`,
  so it carries a second `data/media:/data/media` mount specifically to make janitorr's
  namespace resolve there (2026-07-02 review M4; see the jellyfin role CLAUDE.md). If
  either side's mounts change, re-check both configs together.
- **A restartCount in the low single digits right after a node reboot is EXPECTED, not a
  fault** (diagnosed 2026-07-02 under Docker; the mechanism below is the k8s one, restated
  by the 2026-09-24 review, which read a live pod at restartCount 5 with its last crash
  about two minutes after the node came up). Spring fails fast when sonarr and radarr are
  not up yet —
  `feign.RetryableException: Connection refused … sonarr:8989` during context init, *before*
  any cleanup job runs, so a boot-window crash carries zero deletion risk. The kubelet then
  restarts the container under **CrashLoopBackOff**, which backs off exponentially (10s,
  doubling, capped at 5 minutes) rather than retrying on a fixed interval, so the pod heals
  itself once the *arrs answer. Don't re-flag; only investigate restarts that are NOT
  clustered in a post-boot window.
- **To tell a post-boot crash from a real fault, compare the node's `uptime -s` with the
  pod's `lastState.terminated.finishedAt`**: `kubectl -n homelab get pod -l app=janitorr
  -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.finishedAt}'`.
  A finish time within a few minutes of boot is this, and the log line above confirms it.
  A finish time well after boot is a real fault — read the container log.
- **DECIDED: the self-healing restart is accepted, and this role adds no wait-for-deps init
  container** (#2421). Three reasons, and the third is why it is recorded rather than tried.
  (1) `tasks/verify.yml` already proves `sonarr:8989`, `radarr:7878` and `jellyfin:8096`
  accept TCP from inside the NEW pod on every deploy, which is the same assertion a gate
  would make, made where an operator is watching. (2) An unbounded wait trades a bounded
  post-boot crash loop for an indefinite `Init` stall, with no restart count and no
  CrashLoopBackOff to notice — see the docstring of
  `ansible/tests/services/test_karakeep_backend_policies.py`, which is where that pattern
  DOES apply and what it costs. (3) `k8s_autodeploy` is false here, so a pod that never leaves
  Init waits for a human rather than being caught by a rollout gate, on the one role in the
  fleet that deletes real media. Contradict this at a cited `file:line` with new evidence.
- **The `containers_list` `depends_on: [media-volume, sonarr, radarr, jellyfin]` is not a fix
  for the above.** It orders the Ansible applies within one deploy run (`ansible/deploy.yml`
  toposorts from it); it does not gate pod start after a node reboot, when no Ansible run is
  involved at all.

## The Flux verify Job waits in `templates/verify-inert/`

Nothing renders that directory while Ansible owns janitorr. The flip PR renames it to
`templates/verify/`, and the renderer then emits `janitorr-verify`, which replaces
`tasks/verify.yml` with no `pods/exec` grant. `files/verify_janitorr.py` reads the mounted
`application.yml`, dials sonarr, radarr and jellyfin over HTTP, and writes, reads and removes
the leaving-soon probe link through jellyfin's mount layout; its docstring maps each proof.
Its pod is `app: janitorr-verify`, which sonarr's and radarr's `netpol_from` list ahead of the
flip. `tests/test_verify_janitorr.py` renders the renamed directory through the Job census and
holds the Job's mounts to janitorr's and jellyfin's.

## Editing
- Rules: `templates/config/application.yml.j2` (rendered into the k8s Secret by `roles/k8s/janitorr`)
- Verify the health reader: `kubectl -n homelab logs job/<the newest janitorr-health Job>`. Its
  `janitorr-health: status=up` line is the verdict, and `janitorr-health: pushed` is the push.
