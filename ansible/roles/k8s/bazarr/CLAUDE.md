# bazarr — subtitle manager for the *arr stack

Bazarr pulls subtitles for the media Sonarr/Radarr manage. See repo-root `CLAUDE.md` for
shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses `--tags=bazarr`
  with exit 5
- **Image:** `lscr.io/linuxserver/bazarr` (`bazarr_k8s_image`)
- **Route:** `bazarr.<domain>` · `bazarr.local.<domain>`, Authelia one_factor
- **Claims:** `bazarr-config` (weekly -> B2 (default target)), `media-data` (not Longhorn
  (media-local))
<!-- /generated_from -->

- **Port:** 6767.
- **Storage:** `bazarr-config` PVC (`longhorn`, 1Gi) holds the database and subtitle paths.
  Also mounts the shared `media-data` claim (read/write, via subPath) for the files
  themselves.
- **Flux applies it, not Ansible** (D5.9, the first Wave B PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. Its
  image-bump PRs therefore draw no `land-wait` and no `probe.py health` run. To give it back to
  Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects. authelia, media-volume and traefik stay
  Ansible-owned, so the Kustomization records them in its `homelab/ansible-owned-deps`
  annotation, not in `dependsOn`. The Deployment is `Recreate`, so a change that rolls it
  takes the service down briefly.
- **The `bazarr-snapshot` Job snapshots `bazarr-config` before each revision applies**
  (D5.2a). The bazarr Kustomization depends on `bazarr-snapshot`, so a revision applies only
  after the snapshot is ready; `docs/flux-deploy-plane.md` lists the Job's checks. A failed
  revision that changes bazarr writes the stateful hold, and `promote` does not reset `prod`,
  because the failed image may already have migrated the database. Nothing reverts the claim
  on its own: the operator follows the revert runbook in `docs/volume-snapshot-drills.md`.
  `k8s_autodeploy: true` still lets Renovate automerge the image bump, which is safe for the
  same reason it was under Ansible: a bad swap can be reverted to the snapshot.
- **`media-data` is neither snapshotted nor reverted** (`k8s_autodeploy_unreverted_claims`). It
  is a shared claim other roles also write; a revert can forget a subtitle file bazarr already
  wrote there, which is self-healing because bazarr checks existence before re-downloading.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`,
  and no Flux path runs it. It stays for the handback. Its pre-apply snapshot through
  `k8s/volume-snapshot` became the `bazarr-snapshot` Job, and the Kustomization's `wait`
  replaces the rollout wait.

## Notable
- `-lsNN` linuxserver tagging hides a breaking bump as a routine patch bump — the reason the
  auto-deploy reasoning in `defaults/main.yml` calls this out explicitly even after
  promotion.
- `templates/networkpolicy-bazarr.yaml.j2` admits monitor-bridge to bazarr's API port on top
  of the netpol-baseline default set (traefik, prometheus, the two cni0 gateways) — without
  it monitor-bridge's health check gets refused, which happened on the check's first three
  cycles (2026-08-29) before the policy existed. The caller list is `netpol_from` on bazarr's
  `containers_list` entry, rendered through the shared `networkpolicy` macro.

## Subtitle providers — PVC state, recorded here because the repo cannot hold it

**Nothing in this role configures providers.** Bazarr keeps the selection in its own database
on the `bazarr-config` PVC, so a grep of the tree proves nothing either way — the one place the
"the repo describes the deployment" property does not hold here. This section is the record
instead. It is a reading, not a declaration: change a provider in the UI and this goes stale.

Read from the live API on **2026-09-10** (`GET /api/system/settings` -> `general.enabled_providers`,
cross-checked against `GET /api/providers`, authed with the SOPS `bazarr_api_key`):

| Provider | Status |
|---|---|
| `opensubtitlescom` | Good (account `danielh2018`) |
| `gestdown` | Good |
| `supersubtitles` | Good |

One language profile, `English`, with no cutoff and no forced/HI variants.

To re-read it, query the pod directly rather than through Traefik — the route is behind
Authelia and an API key does not satisfy that middleware:

```bash
curl -sS -H "X-API-KEY: <bazarr_api_key>" http://<pod-ip>:6767/api/providers
```

### Whisper: not added, and the number that decides it

**Not adopted.** The Whisper provider transcribes the audio track when no published subtitle
exists, which is the anime and foreign-audio case the other three miss — but there is no such
case outstanding here. `GET /api/episodes/wanted` and `GET /api/movies/wanted` both returned
`"total": 0` on 2026-09-10: the three providers satisfy the only language profile with nothing
missing. Against that, Whisper needs an ASR sidecar (`whisperai.endpoint` is already stubbed at
`http://whisper:9001` in Bazarr's own defaults, pointing at nothing) and a CPU budget on
`daniel-box`, which is also the node running the GPU transcoder and every other workload. So
the cost is real and the benefit was zero on 2026-09-10. **Revisit when a wanted count stays
non-zero** — that is the measurement that would change the answer, not a judgement about the
library.

## Editing
- Manifests: `templates/deployment.yaml.j2`, `templates/networkpolicy-bazarr.yaml.j2`. The
  Service and the IngressRoute come from the shared
  `ansible/templates/service-default.yaml.j2` and
  `ansible/templates/ingressroute-default.yaml.j2`, which this role ships no template for —
  the route's hostname, port and Authelia gate are the `containers_list` entry's.
