# registry — in-cluster Docker image cache

A local `registry` (`registry_k8s_image`), the push store, that holds images
`k8s/image-builder` builds in-cluster for an Ansible-owned consumer. Every consumer is
flux-owned and pulls from GHCR, so the store serves only the verify Job's seed, and break-glass
pushes its artifact there. It still holds the images it served n8n, homelab-mcp, ical-proxy, nut,
pi-peer-backup, code-server, terraria, valheim and karakeep before they moved. See repo-root
`CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=registry` with exit 5
- **Images:** `registry` (`registry_k8s_image`), `python` (`registry_k8s_ghcr_resume_image`),
  `gcr.io/go-containerregistry/crane` (`registry_k8s_crane_image`), `alpine`
  (`registry_k8s_netpol_probe_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** `registry-data` (no backup (StorageClass longhorn-nobackup)),
  `registry-dockerhub-mirror` (no backup (StorageClass longhorn-nobackup)),
  `registry-ghcr-mirror` (no backup (StorageClass longhorn-nobackup))
<!-- /generated_from -->

- **No `REGISTRY_AUTH`.** Network reachability to `k8s_registry_port` IS the access
  control. `templates/networkpolicy.yaml.j2` admits three sources. image-builder's Jobs and
  this role's self-test Jobs push. Each node's `cni0`/`flannel.1` gateway address carries
  containerd's pulls, which arrive SNAT'd to it, so only an `ipBlock` can admit them.
  `flux-system`'s source-controller fetches the break-glass artifact (#6046).
  `ansible/roles/k8s/registry/templates/networkpolicy.yaml.j2:DECIDED: a standing grant for source-controller`
  has why that grant stands rather than being applied by hand at break-glass time.
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
    every Deployment and routes each port by a named targetPort that only one pod declares.
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
  - **The mirror reaches ghcr.io through a sidecar that resumes a cut blob** (#6401).
    ghcr.io's signed blob URL expires mid-transfer, on HTTP/1.1 and HTTP/2 alike, after about
    8 minutes. distribution's proxy refetches from byte 0 and never resumes, and containerd
    does not fall back on a short body (#5654). While GHCR is slow, a cold blob over about
    200 MB therefore never completes through distribution alone. GHCR served 0.15-0.45 MB/s
    from 2026-10-17 to 2026-10-19, and 44-98 MB/s on 2026-10-24. The measurement is in the
    comment above `REGISTRY_PROXY_REMOTEURL` in `templates/ghcr-mirror.yaml.j2`.
    - `REGISTRY_PROXY_REMOTEURL` names the `ghcr-resume` container on the pod's loopback,
      `registry_k8s_ghcr_resume_port`. It runs `files/ghcr_resume_proxy.py` from a ConfigMap
      named for its content, so a script edit rolls the mirror.
    - The sidecar relays every request to ghcr.io unchanged, the 401 challenge included, so
      distribution still fetches its tokens from ghcr.io's realm. It answers a blob GET
      itself. After a cut it asks ghcr.io for a fresh signed URL, with a fresh anonymous
      token if the old one expired, and continues with `Range: bytes=<sent>-`.
    - A resumed answer that is not a 206 starting at that offset aborts the response short,
      so distribution stores nothing rather than a spliced blob.
    - `ansible/roles/k8s/registry/tests/test_ghcr_resume_proxy.py` runs it against a fake
      ghcr.io whose signed URL cuts every transfer.
  - **`registry-gc.sh` warms the store after each weekly GC.** Once the mirror serves again,
    it GETs every linux/amd64 blob of every `ghcr.io` ref it read for the keep set, through
    the mirror. That caches images a node pulled before the mirror existed, and pulls that fell
    back to `ghcr.io` during a GC, while GHCR is at its normal rate rather than when a cold
    start needs them. A cached blob streams from the store and costs no WAN. On 2026-10-24 a
    cold warm fetched 354 blobs (6.2 GB) in 133s, and a second pass took 49s.
    `k8s_host_files_ghcr_mirror_warm_budget_s` (1800s) bounds the phase. A blob that fails to
    warm is named in the `registry-gc` journal and the run's message, and leaves the run up.
    `ansible/roles/setup/k8s_host_files/templates/registry-gc.sh.j2:DECIDED: a blob that fails to warm`
    has why.
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
- **lscr.io pulls go through the ghcr-mirror too** (#6257). lscr.io serves ghcr.io's
  `linuxserver/*` repositories with ghcr.io's token realm and the same digests, so every node's
  `registries.yaml` names the ghcr-mirror's endpoint for `lscr.io` as well. The proxy caches
  them under their ghcr.io path, so `registry-gc.sh` strips both prefixes when it builds the
  keep set and the warm list. Without that, the prune would delete a digest-pinned lscr.io
  image once its repository held more than the kept revisions.
- **A third registry, `dockerhub-mirror`, is the pull-through mirror of `docker.io`** (#6257).
  It is `templates/dockerhub-mirror.yaml.j2`: its own Deployment, NetworkPolicy and claim
  (`registry_k8s_dockerhub_mirror_claim`, 40Gi, `longhorn-nobackup`), on
  `k8s_registry_dockerhub_mirror_port` (5005) behind the same Service. `registries.yaml` names
  it as the `docker.io` mirror. It also takes repeat pulls off Docker Hub's anonymous rate
  limit.
  - It has no resume sidecar. The cut transfer that sidecar repairs is ghcr.io's signed blob
    URL expiring.
  - Its TTL is `registry_k8s_dockerhub_mirror_ttl` (2160h), not zero, so no GC collects it and
    it renders `replicas: 1`. distribution schedules the expiry only on a fetch from Docker
    Hub, and a pull served from the store never moves it. Every cached image is therefore
    deleted one TTL after its first fetch, however often nodes pulled it since, so a digest pin
    unchanged for longer than the TTL needs the WAN again.
    `ansible/roles/k8s/registry/templates/dockerhub-mirror.yaml.j2:DECIDED: a long TTL` has
    the trade-off.
  - Nothing proves it serves pulls the way node-exporter's `ghcr-mirror-pull` sidecar proves
    the ghcr-mirror does, and `registry-gc.sh` does not warm it.
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
    fails if the claim joins the exclusion. When it pages, `docs/registry-push-store-gc.md` is
    the procedure. The operator untags what nothing runs while the registry serves, then
    suspends the `registry` Kustomization before scaling `deploy/registry` to zero, so Flux
    cannot restore the pod mid-GC. A plain `garbage-collect` Job runs, and the resume scales
    the registry back up. On 2026-10-25 the store held one seed, and the bytes were ten
    repositories of images built before their roles moved to GHCR.
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
  - `isolation` proves all three ports refuse `app: registry-verify`.
