# registry — in-cluster Docker image cache

A local `registry` (`registry_k8s_image`) that stores images `k8s/image-builder` builds in-cluster, so
n8n, homelab-mcp, ical-proxy, nut, pi-peer-backup, code-server, terraria, valheim and
karakeep pull
without a public
registry round trip. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "registry"`
- **Images:** `registry` (`registry_k8s_image`), `gcr.io/go-containerregistry/crane`
  (`registry_k8s_crane_image`), `alpine` (`registry_k8s_netpol_probe_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** `registry-data` (no backup (StorageClass longhorn-nobackup)),
  `registry-ghcr-mirror` (no backup (StorageClass longhorn-nobackup))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — dependency edges — image-supply path
  for n8n/homelab-mcp/ical-proxy/nut/pi-peer-backup/code-server/terraria/valheim/karakeep; no
  intra-tick ordering. ALSO Recreate + its own PVC (blob store) — two independent reasons.
  COUPLING NOTE for a future promotion: a revert drops recently-pushed digests from the blob
  store while nodes that already pulled them keep running until their next pull 404s
<!-- /generated_from -->

- **No `REGISTRY_AUTH`.** Network reachability to `k8s_registry_port` IS the access
  control: `templates/networkpolicy.yaml.j2` admits only two ingress rules, keyed off
  the node's own `cni0`/`flannel.1` gateway address (containerd's pulls arrive SNAT'd
  to it, so a podSelector can't admit them — only an `ipBlock` can).
- **`registry-data` is `longhorn-nobackup`**, 10Gi. Every stored image rebuilds from a
  Dockerfile in this repo, so backing it up would spend B2 transactions on bytes a rebuild
  regenerates.

## Notable
- **The role runs a second registry, `ghcr-mirror`, as a pull-through mirror of `ghcr.io`**
  (D5 Open question 2, ruled 2026-10-16). Every node's `registries.yaml` names it as the
  `ghcr.io` mirror (`ansible/roles/setup/k3s/templates/registries.yaml.j2`), so a node keeps a
  LAN copy of each flux-owned GHCR image it pulled and nut can cold-start during a WAN outage.
  - It is its own Deployment, `templates/ghcr-mirror.yaml.j2`, not `REGISTRY_PROXY_*` on the
    push store and not a container in the push store's pod (operator ruling on #5680,
    2026-10-18). A registry in proxy mode refuses every manifest Put and Delete, and a sidecar
    could only be collected by scaling `deploy/registry`.
    `ansible/roles/k8s/registry/templates/ghcr-mirror.yaml.j2:DECIDED: its own Deployment` has
    the trade-off.
  - It keeps the address every consumer names: `k8s_registry_ghcr_mirror_port` (5002) on the
    loopback hostPort of the registry node, and on the pinned ClusterIP. One Service fronts
    both Deployments and routes each port by a named targetPort that only one pod declares.
    `ansible/roles/k8s/registry/templates/service.yaml.j2:DECIDED: one Service fronting two`
    has why.
  - Its own NetworkPolicy, beside its Deployment, admits that port only from the two node
    grants, and the netpol probe Job checks a pod is refused on it. The pod is in
    `netpol_baseline_exempt_workloads`, so deploy this role before netpol-baseline.
  - It renders no `replicas:`. The GC scales it to zero, and a rendered field would give the
    apply, and later Flux, a claim on it that restores the pod mid-GC.
  - It caches every `ghcr.io` image in the cluster, not only `danielh2018`'s, because a
    `registries.yaml` mirror covers the whole host. Its store is therefore its own claim,
    `registry_k8s_ghcr_mirror_claim` (30Gi, `longhorn-nobackup`), which the push store's GC
    Job never mounts.
  - The TTL is zero, so only the weekly GC reclaims the store (#5541). A proxy refuses
    manifest DELETE, so `ghcr-mirror-gc-job.yaml.j2`'s Job, `registry-gc-ghcr-mirror`, edits
    the store offline. `registry-gc.sh` runs it in its own phase, with only
    `deploy/ghcr-mirror` scaled to zero, after the push store's phase has brought
    `deploy/registry` back. Its `prune` initContainer runs `files/ghcr-mirror-prune.sh`, and its
    `gc` container then runs plain `garbage-collect`.
  - The prune's unit is a manifest revision, not a tag. A third-party ghcr.io image is pinned
    by digest, and the proxy caches a digest pull with no tag, so `--delete-untagged` on this
    store would delete every such image. A flux-owned built image runs by tag alone
    (`scripts/renderer/renderer_lib/images.py:ghcr_text`), so it is cached tagged. Per
    repository the prune keeps the `registry_k8s_ghcr_mirror_keep_revisions` (3) newest
    top-level revisions, the children of every kept index, every digest in the keep set, and
    the revision each `<repo>:<tag>` in the keep set points at.
  - `registry-gc.sh` writes the keep set to the `registry-gc-ghcr-mirror-keep` ConfigMap
    before the mirror's scale-down. It holds every sha256 digest and every `ghcr.io` tag ref in pods,
    Deployments, StatefulSets, DaemonSets, CronJobs and Jobs across all namespaces. The tag
    refs protect a tag-pulled workload scaled to zero, which has no pod to report a digest.
    The script deletes the previous set first and the Job mounts it as optional, so a run that
    cannot read the cluster prunes nothing. Such a skip reports the run down, so a cause that
    persists cannot hide behind a green tile while the claim fills. Each pruned revision is a
    `pruned <repo>@<digest>` line in the Job log and in the `registry-gc` journal.
  - A cold blob that takes over about 8 minutes to fetch never completes through the
    mirror. ghcr.io's signed blob URL expires mid-transfer on HTTP/1.1 and HTTP/2 alike,
    and the proxy never resumes (#5654). The measurement is in the comment above
    `REGISTRY_PROXY_REMOTEURL` in `templates/ghcr-mirror.yaml.j2`.
  - containerd falls back to `ghcr.io` while the GC scales the mirror to zero. A build in
    flight skips only the push store's phase, since no build writes the mirror's store.
- A weekly garbage collection (`gc-job.yaml.j2`, Sunday 04:20) takes the push store
  offline for up to 20 minutes — nothing else reclaims space, and every rebuild pushing
  the same `latest` tag orphans the previous manifest.
- **`registry-gc.sh` untags superseded content tags BEFORE it scales the registry down**, and
  that order is load-bearing. Every build also pushes an immutable `sha-<12 hex>` tag
  (`k8s/image-builder`), which is a real manifest reference `garbage-collect
  --delete-untagged` can never reclaim — so without the prune the store gains one permanent
  manifest per rebuild. Untagging is an HTTP `DELETE` against the SERVING registry, while the
  GC Job runs the binary against a store with nothing serving it; reversed, every prune fails
  on a refused connection and only the blob sweep runs.
  - It keeps `registry_k8s_keep_content_tags` (3) newest per repository, the digest `latest`
    resolves to, and any digest a running pod resolved. That last one is not belt-and-braces:
    every Ansible-owned consumer pulls from here with `imagePullPolicy: Always`, so a pod
    re-fetches by tag on each start and untagging the manifest it came from turns its next
    restart into an ImagePullBackOff. `Always` stays while Ansible owns the consumer, because this
    store's tags are mutable: `latest` moves on every build, and a forced rebuild re-pushes an
    unchanged content tag with new bytes, so a node's cached copy can be stale. A flux-owned
    consumer pulls its GHCR content tag with `IfNotPresent` instead
    (`scripts/renderer/renderer_lib/images.py:ghcr_refs`). GHCR never re-pushes a tag it serves,
    so a cached copy is always right. That pull goes through the `ghcr-mirror` Deployment above,
    never through this store.
  - An unrankable tag is KEPT. Ordering needs each manifest's config-blob `created`, because
    the tag names are content hashes and carry no chronology.
- The GC run reports twice: the `Registry GC` Kuma push tile (`registry_gc_push_token`,
  static in `k8s/uptime-kuma`, deadline one hour past the weekly period) and the off-site
  healthchecks.io `registry-gc` dead-man. The token existed nowhere but the script until
  2026-09-18 (#1937), so before that only the dead-man saw a run.
- **The five job manifests stage in `/etc/rancher/k3s/manifests/registry-jobs/`, not in this
  role's own manifest directory** (#1669). `k8s/manifests` prunes every file in
  `manifests/registry/` outside the role's file list, which deleted all four on every deploy.
  Their `-job.yaml` suffix keeps them out of the derived list. `gc-job.yaml` and
  `ghcr-mirror-gc-job.yaml` are the ones that matter: `registry-gc.sh` reads them at cron time, so
  the prune left a window in which GC failed on a missing manifest. Nothing sweeps
  `registry-jobs/`, so a host with `registry_k8s_manage_gc: false` gets an explicit removal
  task outside the GC block.
- **`templates/verify-inert/` holds the Flux form of the push self-test and the netpol probe**,
  and nothing renders it yet. The flip PR renames it to `templates/verify/` and deletes the
  tasks. `files/registry_verify.py` pushes a fixed image through the distribution API, or
  proves both ports refuse `app: registry-verify`. The pull self-tests have no Job form yet,
  and the push store's GC retirement follows in its own PR (#5680).
- `registry-gc.sh` proves itself with `crane` push/pull round trips
  (`registry_k8s_probe_source`, `registry_k8s_probe_repo`) rather than trusting the GC
  ran clean.
