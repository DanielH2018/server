# registry — in-cluster Docker image cache

A local `registry:3.1.1` that stores images `k8s/image-builder` builds in-cluster, so
n8n, homelab-mcp, ical-proxy, nut, pi-peer-backup and code-server pull without a public
registry round trip. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Deploy tag:** `--tags "registry"`.
- **No route, no Authelia** — an infra role with no IngressRoute.
- **No `REGISTRY_AUTH`.** Network reachability to `k8s_registry_port` IS the access
  control: `templates/networkpolicy.yaml.j2` admits only two ingress rules, keyed off
  the node's own `cni0`/`flannel.1` gateway address (containerd's pulls arrive SNAT'd
  to it, so a podSelector can't admit them — only an `ipBlock` can).
- **Claim:** `registry-data`, `longhorn-nobackup`, 10Gi. Every stored image rebuilds
  from a Dockerfile in this repo, so backing it up would spend B2 transactions on
  bytes a rebuild regenerates.
- **`k8s_autodeploy: false`** — dependency edges (no intra-tick ordering against the
  services it feeds) plus `Recreate` + its own PVC. Reason is in `defaults/main.yml`.

## Notable
- A weekly garbage collection (`gc-job.yaml.j2`, Sunday 04:20) takes the registry
  offline for up to 20 minutes — nothing else reclaims space, and every rebuild pushing
  the same `latest` tag orphans the previous manifest.
- `registry-gc.sh` proves itself with `crane` push/pull round trips
  (`registry_k8s_probe_source`, `registry_k8s_probe_repo`) rather than trusting the GC
  ran clean.
