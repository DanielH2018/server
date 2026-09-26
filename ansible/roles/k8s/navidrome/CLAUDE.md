# navidrome — Subsonic-compatible music server (parked)

Navidrome, stood up 2026-09-02 for issue #803, then scaled to zero on operator request.
There is nothing for it to serve yet: the media volume holds books, movies and TV, no music
directory, so the library mount is an `emptyDir`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "navidrome"`
- **Image:** `ghcr.io/navidrome/navidrome` (`navidrome_k8s_image`)
- **Route:** `navidrome.<domain>` · `navidrome.local.<domain>`, Authelia one_factor
- **Claim:** `navidrome-data` (weekly -> B2 (default target))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — parked at replicas 0 — an
  auto-applied image bump would never reach a pod
<!-- /generated_from -->

- **Pinned `tag@sha256`.**
- **Host:** daniel-server preferred (`node_affinity_preference`), not pinned — schedules
  elsewhere if daniel-server is cordoned or full.
- **The route is not rendered while parked**, see below.
- **`navidrome-data`** (`navidrome_k8s_claim`), Longhorn, 2Gi — SQLite index and transcoding
  cache, sized as headroom rather than a measurement.
- **Parked at `navidrome_k8s_replicas: 0`**, which is what the denylist reason above means.

## Notable
- **Parked, not scaled live.** `defaults/main.yml` sets `navidrome_k8s_replicas: 0`
  declaratively rather than via `kubectl scale`, because the read-only ServiceAccount cannot
  scale anything and a redeploy would reset a live scale back to 1 anyway.
- **`ND_ENABLEEXTERNALSERVICES` is `"false"`**, which is load-bearing for the network posture:
  it disables both the GitHub release check and the metadata agents, so the pod opens no
  outbound connection at all — that's what puts it in `netpol-baseline`'s `BORN_FENCED_ROLES`.
- **The route is gated on the replica count.** `templates/ingressroute.yaml.j2` renders
  nothing while `navidrome_k8s_replicas` is 0, and `tasks/main.yml` deletes the live
  IngressRoute in the same state. Traefik re-reads every IngressRoute on each config refresh
  and logs `no servers found for homelab/navidrome` whenever the EndpointSlice behind one is
  empty — every ~20s, forever, burying every other router error (issue #1323). Both halves
  are needed: `kubectl apply` only adds and updates, so rendering nothing leaves the live
  route serving. `k8s/manifests`' opt-in `manifests_prune` cannot do the deleting here — the
  live object predates any `homelab/role` label, so its selector structurally cannot match it.
- **`Recreate` strategy**, not rolling: the data PVC is RWO on a single Longhorn replica, and
  SQLite wants one writer.

## Bringing it back
Set `navidrome_k8s_replicas: 1`, point `navidrome_k8s_music_dir` at a real library on the
media volume, flip `k8s_autodeploy: true`, and add a Kuma monitor (one added while parked
would be permanently red). The route comes back with the replica count — no separate step. Deploy with `./scripts/deploy.sh --tags
"navidrome"`.
