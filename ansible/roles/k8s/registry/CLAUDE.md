# registry — in-cluster Docker image cache

A local `registry:3.1.1` that stores images `k8s/image-builder` builds in-cluster, so
n8n, homelab-mcp, ical-proxy, nut, pi-peer-backup, code-server, terraria and valheim pull
without a public
registry round trip. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "registry"`
- **Images:** `registry` (`registry_k8s_image`), `gcr.io/go-containerregistry/crane`
  (`registry_k8s_crane_image`), `alpine` (`registry_k8s_netpol_probe_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `registry-data` (no backup (StorageClass longhorn-nobackup))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — dependency edges — image-supply path
  for n8n/homelab-mcp/ical-proxy/nut/pi-peer-backup/code-server; no intra-tick ordering. ALSO
  Recreate + its own PVC (blob store) — two independent reasons. COUPLING NOTE for a future
  promotion: a revert drops recently-pushed digests from the blob store while nodes that
  already pulled them keep running until their next pull 404s
<!-- /generated_from -->

- **No `REGISTRY_AUTH`.** Network reachability to `k8s_registry_port` IS the access
  control: `templates/networkpolicy.yaml.j2` admits only two ingress rules, keyed off
  the node's own `cni0`/`flannel.1` gateway address (containerd's pulls arrive SNAT'd
  to it, so a podSelector can't admit them — only an `ipBlock` can).
- **`registry-data` is `longhorn-nobackup`**, 10Gi. Every stored image rebuilds from a
  Dockerfile in this repo, so backing it up would spend B2 transactions on bytes a rebuild
  regenerates.

## Notable
- A weekly garbage collection (`gc-job.yaml.j2`, Sunday 04:20) takes the registry
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
    built images run `imagePullPolicy: Always`, so a pod re-fetches by tag on each start and
    untagging the manifest it came from turns its next restart into an ImagePullBackOff.
  - An unrankable tag is KEPT. Ordering needs each manifest's config-blob `created`, because
    the tag names are content hashes and carry no chronology.
- The GC run reports twice: the `Registry GC` Kuma push tile (`registry_gc_push_token`,
  static in `k8s/uptime-kuma`, deadline one hour past the weekly period) and the off-site
  healthchecks.io `registry-gc` dead-man. The token existed nowhere but the script until
  2026-09-18 (#1937), so before that only the dead-man saw a run.
- **The four job manifests stage in `/etc/rancher/k3s/manifests/registry-jobs/`, not in this
  role's own manifest directory** (#1669). `k8s/manifests` prunes every file in
  `manifests/registry/` that `manifests_files` does not name, which deleted all four on every
  deploy. `gc-job.yaml` is the one that mattered: `registry-gc.sh` reads it at cron time, so
  the prune left a window in which GC failed on a missing manifest. Nothing sweeps
  `registry-jobs/`, so a host with `registry_k8s_manage_gc: false` gets an explicit removal
  task outside the GC block.
- `registry-gc.sh` proves itself with `crane` push/pull round trips
  (`registry_k8s_probe_source`, `registry_k8s_probe_repo`) rather than trusting the GC
  ran clean.
