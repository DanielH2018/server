# registry — in-cluster Docker image cache

A local `registry` (`registry_k8s_image`) that stores images `k8s/image-builder` builds in-cluster, so
n8n, homelab-mcp, ical-proxy, nut, pi-peer-backup, code-server, terraria, valheim and
karakeep pull
without a public
registry round trip. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=registry` with exit 5
- **Images:** `registry` (`registry_k8s_image`), `gcr.io/go-containerregistry/crane`
  (`registry_k8s_crane_image`), `alpine` (`registry_k8s_netpol_probe_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** `registry-data` (no backup (StorageClass longhorn-nobackup)),
  `registry-ghcr-mirror` (no backup (StorageClass longhorn-nobackup))
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
    `registry_k8s_ghcr_mirror_claim` (30Gi, `longhorn-nobackup`).
  - The TTL is zero, so only the weekly GC reclaims the store (#5541). A proxy refuses
    manifest DELETE, so `ansible/roles/setup/k8s_host_files/templates/ghcr-mirror-gc-job.yaml.j2`'s Job, `registry-gc-ghcr-mirror`, edits
    the store offline. `registry-gc.sh` runs it with only `deploy/ghcr-mirror` scaled to zero.
    Its `prune` initContainer runs `ansible/roles/setup/k8s_host_files/files/ghcr-mirror-prune.sh`, and its
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
  - containerd falls back to `ghcr.io` while the GC scales the mirror to zero. No build writes
    the mirror's store, so the GC does not wait for builds.
  - **Nothing but `registry-gc.sh` writes the mirror's `spec.replicas`**, so the operator
    accepted it as the one exception to contract C4 on field ownership (#5732). The script
    stays a host cron after the flip.
  - **No deploy restores the mirror.** Until the flip, a deploy task restored a mirror a
    stopped GC left at zero, under the script's flock (#5736). Flux owns the role now and
    renders no `replicas:` for the mirror, so nothing but `registry-gc.sh` writes the count
    and the deploy-time restore was deleted with the tasks.
  - **A run killed mid-phase is repaired by the next one.** The stamp
    `/var/lib/registry-gc/ghcr-mirror-down` exists from before the scale-down until the mirror
    serves again, and a run that finds it restores the mirror first. Observability's
    `GhcrMirrorScaledToZero` (`files/rules/registry.yaml` there) pages one left at zero for 2h.
- **The push store is not collected** (D5, Wave D's registry bullet). Its GC scaled
  `deploy/registry`, whose `replicas: 1` Flux renders after the flip, so Flux would restore
  the pod mid-GC, the corruption the stop-the-world rule exists to prevent. Every
  `k8s/image-builder` caller is flux-owned and pulls from GHCR, so no build reaches the store.
  It still grows. The verify Job's seed adds each new `flux_job_image` pin, 20.38 MB
  compressed when measured on 2026-10-19, under a new tag. Each upstream rebuild of
  `registry_k8s_probe_source` adds a copy too. Nothing reclaims either, because only
  `garbage-collect` frees blobs.
  - **monitor-bridge's `pvc_fullness` page is the bound** (#5752). It pages when a claim passes
    85%, and `registry-data` is not in its `PVC_EXCLUDE`. On 2026-10-24 the claim was 38% full
    (3.98 GB), so at about 1 GiB a year of pins it pages years before the claim fills.
    `ansible/tests/services/test_storage_rule_literals.py::test_no_uncollected_claim_is_excluded_from_the_fullness_page`
    fails if the claim joins the exclusion. No runbook for a page exists yet. A collection
    must not race Flux on `spec.replicas`, and #6114 tracks the options.
- **A sidecar on every node proves the mirror serves pulls** (#5542). containerd falls back
  to `ghcr.io` past a broken mirror, so nothing else notices one while the WAN is up.
  node-exporter's `ghcr-mirror-pull` sidecar
  (`ansible/roles/k8s/node-exporter/files/ghcr-mirror-pull.sh`) GETs nut's manifest every
  600s through the endpoint the node's `registries.yaml` names. The pod is hostNetwork, so
  the request reaches this role's fence from the same node grant containerd's pulls use. nut's
  role renders the tag to GET, in `ansible/roles/k8s/nut/templates/ghcr-mirror-probe-configmap.yaml.j2`.
  Observability's `GhcrMirrorPullFailed` pages a node whose check has failed for 45 minutes,
  past the longest GC hold, and `GhcrMirrorPullNotReporting` pages a node whose check file
  went stale. Both rules are in `files/rules/registry.yaml` there. The check proves the path
  and the mirror's answer, not the cache: with the WAN up, the proxy fetches a missing tag
  from GHCR and still answers 200.
- The GC run reports twice: the `Registry GC` Kuma push tile (`registry_gc_push_token`,
  static in `k8s/uptime-kuma`, deadline one hour past the weekly period) and the off-site
  healthchecks.io `registry-gc` dead-man. The token existed nowhere but the script until
  2026-09-18 (#1937), so before that only the dead-man saw a run.
- **The job manifests stage in `/etc/rancher/k3s/manifests/registry-jobs/`, not in this
  role's own manifest directory** (#1669). `k8s/manifests` prunes every file in
  `manifests/registry/` outside the role's file list, which deleted them on every deploy.
  Their `-job.yaml` suffix keeps them out of the derived list. `ghcr-mirror-gc-job.yaml` is
  the one that mattered: `registry-gc.sh` reads it at cron time, so the prune left a window in
  which GC failed on a missing manifest. That manifest, the script, `/var/lib/registry-gc` and
  the cron now install from `ansible/roles/setup/k8s_host_files/tasks/registry.yml`, because
  a Flux-owned role runs no tasks. The same file holds the retired push store's `gc-job.yaml`
  absent arm. `registry_k8s_manage_gc: false` skips that whole block and removes nothing.
  The setup role keeps copies of the GC's variables in its `defaults/main.yml`, and
  `ansible/tests/setup/test_k8s_host_files_pairs.py` holds them equal to this role's.
- **Flux owns registry (D5, Wave D).** `promote` applies it, and no Ansible run reaches
  `tasks/main.yml`, which keeps only the `k8s/manifests` include for the handback
  (`docs/flux-deploy-plane.md`). The self-tests and the netpol probe are the `registry-verify`
  Jobs in `templates/verify/`, which the renderer emits once the `registry` Kustomization
  reads Ready. `files/registry_verify.py` runs one check per Job:
  - `push` writes a fixed image through the distribution API, then seeds the pull Jobs'
    image. It copies `flux_job_image`'s linux/amd64 image into the push store under a tag
    named for the pin's digest, reading Docker Hub only when the store lacks that tag.
  - `pull`, and `pull-agent` on a cluster with an agent node, run that copy from
    `k8s_registry_pull_host`, so the pod starting is the proof (ruling on #5680). On a first
    run they wait in ImagePullBackOff until the push has seeded it. The `DECIDED:` comment
    in `templates/verify/job.yaml.j2` has the deadline arithmetic.
  - `isolation` proves both ports refuse `app: registry-verify`.
