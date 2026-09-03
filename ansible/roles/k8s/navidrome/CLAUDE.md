# navidrome — Subsonic-compatible music server (parked)

Navidrome, stood up 2026-09-02 for issue #803, then scaled to zero on operator request.
There is nothing for it to serve yet: the media volume holds books, movies and TV, no music
directory, so the library mount is an `emptyDir`.

## At a glance
- **Image:** `navidrome_k8s_image`, pinned `tag@sha256`.
- **Host:** daniel-server preferred (`node_affinity_preference`), not pinned — schedules
  elsewhere if daniel-server is cordoned or full.
- **Route:** `navidrome.<domain>` · Authelia
- **Persists:** `navidrome-data` PVC (`navidrome_k8s_claim`), Longhorn, 2Gi — SQLite index and
  transcoding cache, sized as headroom rather than a measurement.
- **Deploy tag:** `--tags "navidrome"`. Denylisted from auto-deploy — parked at
  `navidrome_k8s_replicas: 0`, so an auto-applied image bump would never reach a pod.

## Notable
- **Parked, not scaled live.** `defaults/main.yml` sets `navidrome_k8s_replicas: 0`
  declaratively rather than via `kubectl scale`, because the read-only ServiceAccount cannot
  scale anything and a redeploy would reset a live scale back to 1 anyway.
- **`ND_ENABLEEXTERNALSERVICES` is `"false"`**, which is load-bearing for the network posture:
  it disables both the GitHub release check and the metadata agents, so the pod opens no
  outbound connection at all — that's what puts it in `netpol-baseline`'s `BORN_FENCED_ROLES`.
- **`Recreate` strategy**, not rolling: the data PVC is RWO on a single Longhorn replica, and
  SQLite wants one writer.

## Bringing it back
Set `navidrome_k8s_replicas: 1`, point `navidrome_k8s_music_dir` at a real library on the
media volume, flip `k8s_autodeploy: true`, and add a Kuma monitor (one added while parked
would be permanently red). Deploy with `uv run ansible-playbook ansible/deploy.yml --tags
"navidrome"`.
