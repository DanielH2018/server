# navidrome — Subsonic-compatible music server (parked)

Navidrome, stood up 2026-09-02 for issue #803, then scaled to zero on operator request.
There is nothing for it to serve yet: the media volume holds books, movies and TV, no music
directory, so the library mount is an `emptyDir`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=navidrome` with exit 5
- **Image:** `ghcr.io/navidrome/navidrome` (`navidrome_k8s_image`)
- **Route:** `navidrome.<domain>` · `navidrome.local.<domain>`, Authelia one_factor
- **Claim:** `navidrome-data` (weekly -> B2 (default target))
<!-- /generated_from -->

- **Pinned `tag@sha256`.**
- **Host:** daniel-server preferred (`node_affinity_preference`), not pinned — schedules
  elsewhere if daniel-server is cordoned or full.
- **The route is not deployed while parked**, see below.
- **`navidrome-data`** (`navidrome_k8s_claim`), Longhorn, 2Gi — SQLite index and transcoding
  cache, sized as headroom rather than a measurement.
- **Parked at `navidrome_k8s_replicas: 0`.** `k8s_autodeploy: false` stops Renovate
  automerging the image bump, because a bump would never reach a pod.
- **Flux applies it, not Ansible** (D5.8, Wave A'). Its `containers_list` entry carries
  `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a merged
  change reaches the cluster when `promote` publishes and verifies the next bundle. Its
  image-bump PRs therefore draw no `land-wait` and no `probe.py health` run. To give it back to
  Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects. Both dependencies, authelia and traefik, stay
  Ansible-owned, so the Kustomization records them in its `homelab/ansible-owned-deps`
  annotation, not in `dependsOn`.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`,
  and no Flux path runs it. It stays for the handback. The renderer reads its
  `manifests_files` list, so the replica gate on the route below holds under Flux too. The
  Kustomization's `wait` replaces the rollout wait.

## Notable
- **Parked, not scaled live.** `defaults/main.yml` sets `navidrome_k8s_replicas: 0`
  declaratively rather than via `kubectl scale`, because the read-only ServiceAccount cannot
  scale anything, and Flux's drift correction would reset a live scale to the rendered
  `replicas` anyway.
- **`ND_ENABLEEXTERNALSERVICES` is `"false"`**, which is load-bearing for the network posture:
  it disables both the GitHub release check and the metadata agents, so the pod opens no
  outbound connection at all — that's what puts it in `netpol-baseline`'s `BORN_FENCED_ROLES`.
- **The route is gated on the replica count.** `tasks/main.yml` lists `ingressroute.yaml` in
  `manifests_files` only while `navidrome_k8s_replicas` is above 0. Traefik re-reads every
  IngressRoute on each config refresh and logs `no servers found for homelab/navidrome`
  whenever the EndpointSlice behind one is empty — every ~20s, forever, burying every other
  router error (issue #1323). Flux prunes by its inventory (`prune: true`), so a render that
  drops the route deletes the live IngressRoute at that promotion, as `manifests_prune`
  (#3388) did under Ansible. The gate is in the list rather than the template because the
  prune's labelling wrapper refuses a template that renders no document.
- **`Recreate` strategy**, not rolling: the data PVC is RWO on a single Longhorn replica, and
  SQLite wants one writer. Once it has a pod, a change that rolls it takes the service down
  briefly.

## Bringing it back
Set `navidrome_k8s_replicas: 1`, point `navidrome_k8s_music_dir` at a real library on the
media volume, flip `k8s_autodeploy: true`, and delete the entry's `kuma: false` so Uptime Kuma
derives its ingress tile (one added while parked would be permanently red). The route comes back with the replica count — no separate step. Merge the change;
`promote` applies it, because `deploy.sh --tags navidrome` exits 5 for a flux-owned tag.
