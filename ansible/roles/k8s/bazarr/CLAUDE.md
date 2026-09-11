# bazarr — subtitle manager for the *arr stack

Bazarr pulls subtitles for the media Sonarr/Radarr manage. See repo-root `CLAUDE.md` for
shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/bazarr` (`bazarr_k8s_image`)
- **Deploy tag:** `--tags "bazarr"`. Route: `bazarr.<domain>` (Authelia), port 6767.
- **Storage:** `bazarr-config` PVC (`longhorn`, 1Gi) holds the database and subtitle paths.
  Also mounts the shared `media-data` claim (read/write, via subPath) for the files
  themselves.
- **Auto-deploy:** eligible. `k8s_autodeploy: true` since slice 7b (2026-08-21) — the
  `bazarr-config` claim is snapshotted pre-apply by `k8s/volume-snapshot` and reverted on a
  failed deploy, which is what makes the Recreate + RWO migrating-state risk safe to
  auto-promote. `media-data` itself is **not** reverted (a shared claim other roles also
  write); a revert can forget a subtitle file bazarr already wrote there, which is
  self-healing because bazarr checks existence before re-downloading.

## Notable
- `-lsNN` linuxserver tagging hides a breaking bump as a routine patch bump — the reason the
  auto-deploy reasoning in `defaults/main.yml` calls this out explicitly even after
  promotion.
- `templates/networkpolicy-bazarr.yaml.j2` admits monitor-bridge to bazarr's API port on top
  of the netpol-baseline default set (traefik, prometheus, the two cni0 gateways) — without
  it monitor-bridge's health check gets refused, which happened on the check's first three
  cycles (2026-08-29) before the policy existed.

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
the cost is real and the benefit is currently zero. **Revisit when a wanted count stays
non-zero** — that is the measurement that would change the answer, not a judgement about the
library.

## Editing
- Manifests: `templates/deployment.yaml.j2`, `templates/ingressroute.yaml.j2`,
  `templates/networkpolicy-bazarr.yaml.j2`, `templates/service.yaml.j2`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "bazarr"`.
