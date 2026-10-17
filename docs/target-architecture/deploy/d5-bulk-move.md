# D5 — Bulk move

Status: IN PROGRESS

PR D5.1a1, the publish layout: [#5259](https://github.com/DanielH2018/server/pull/5259). It
merged on 2026-10-16 at `645fe859a` and deployed nothing. The publish census reserves D5's
per-tag kinds and names, and `SHARED_KINDS` gains no member. Its review's fix round made
`<tag>` depend on `<tag>-snapshot`, as *Snapshots as a Job per role revision* requires.
PR D5.1e, restart annotations for self-rolled workloads:
[#5263](https://github.com/DanielH2018/server/pull/5263). It merged at `e9cb97abb` and
deployed nothing, because neither pihole nor observability is flux-owned. D3's interim census
is deleted, and its replacement in `scripts/renderer/tests/test_renderer_self_rollout_restart.py`
names both roles. Grafana's admin-password initContainer moves with observability's conversion
([#5262](https://github.com/DanielH2018/server/issues/5262)).
PR D5.1g, the takeover census: [#5264](https://github.com/DanielH2018/server/pull/5264). It
merged at `9f3bf0bfb`. Against the live cluster, `probe.py takeover speedtest` listed the
Deployment's `kubectl.kubernetes.io/restartedAt` under `kubectl-rollout` and the PVC's
`homelab.daniel-hunter.com/seeded` annotation under `kubectl-annotate`. Cluster-scoped RBAC
objects are not read yet ([#5258](https://github.com/DanielH2018/server/issues/5258)).
PR D5.1a3, the Job census: [#5271](https://github.com/DanielH2018/server/pull/5271). It
merged at `c5d00ead9`. Its connection helper, `flux_job_connect.py`, lives in the shared
`manifests` role, so the landing deployed every tag and read `settled`. The census's helper
row catches a script that connects without the helper by accident. It does not try to stop a
deliberate bypass through runtime indirection, because the helper is a reliability rule
(*A first connection retries*), and the `# DECIDED:` marker at `ALLOWED_IMPORTS` in
`scripts/renderer/tests/_job_census.py` records that
([#5289](https://github.com/DanielH2018/server/pull/5289)).
PR D5.1c, host writes and concurrency: [#5275](https://github.com/DanielH2018/server/pull/5275).
It merged at `e59b5dde9`, and its landing deployed `flux` and settled. The live
kustomize-controller reads `--concurrent=5`, equal to `k8s_rollout_batch_width`. The four host
writes the baseline omitted have their moving PRs named since
[#5290](https://github.com/DanielH2018/server/pull/5290) (`f5f92e02a`).
PR D5.1a2, prune labels, digest fields and collisions:
[#5298](https://github.com/DanielH2018/server/pull/5298). It merged at `74aec39bb` and deployed
nothing. Flux-owned claims and Namespaces carry `kustomize.toolkit.fluxcd.io/prune: disabled`,
each tag's object in `digests.json` gains `claims` and `suspended`, and a census refuses an
object two tags render.
PR D5.1f, the removal gates: [#5296](https://github.com/DanielH2018/server/pull/5296). It merged
at `3aeeb4fd7` and deployed nothing. `cross_plane_order.py` refuses an object drop from a
flux-owned tag that no `Removes-Object:` trailer declares, and a flux-owned service's retirement.
PR D5.1b, the suspend lever: [#5318](https://github.com/DanielH2018/server/pull/5318). It merged at
`46784ff2f` and deployed nothing. `flux_suspend` renders `spec.suspend` only when true, the renderer
drops every edge to a suspended tag into `homelab/suspended-deps`, `promote.py verify` leaves a
suspended tag out, and the shed steps in `docs/break-glass.md` suspend a flux-owned service. Promote
run 38965987807 read `verify` `verdict=good failed=` on it and moved `last-good` to its revision.
PR D5.3a, the Prometheus peer: [#5315](https://github.com/DanielH2018/server/pull/5315). It merged at
`3f7494ef4`, and its landing deployed `netpol-baseline` and settled. The live `prometheus-callers`
policy admits a `flux-system` pod labelled `app: soak` on 9090, beside the unchanged `homelab` peer.
Open question 3's review found no new exposure.
PR D5.1d, a suspended role takes no change: [#5342](https://github.com/DanielH2018/server/pull/5342).
It merged at `9cbe9ae9d` and deployed nothing. `cross_plane_order.py` refuses a render change to
a tag suspended at the PR's base, `decide` holds with `<R> suspended-changed=<tag>`, and
`land-wait` returns `suspended`. Promote run 38972816002 ran its `decide` on its own revision and
read `verify` `verdict=good failed=`.
PR D5.1h, the `k8s_host_files` role: [#5337](https://github.com/DanielH2018/server/pull/5337). It
merged at `f261e792f` and parked the tick, as designed. The operator fast-forwarded the primary
checkout to it and ran `initial_setup.yml --tags k8s_host_files`, which read `ok=12 changed=0`,
and the next tick crossed. From here the tick applies every `k8s_host_files` PR.
PR D5.3b1, the soak script, inert: [#5340](https://github.com/DanielH2018/server/pull/5340). It
merged at `2f1fe908a`. The script lives in the shared `manifests` role, so the landing deployed
every tag and settled. It records the operator's two 2026-10-16 rulings at step 16 and Open
question 19, and the soak and Job-census suites read 105 passed on master.
PR D5.2a, snapshot Jobs: [#5350](https://github.com/DanielH2018/server/pull/5350). It merged at
`ca52038ac`. The renderer emits `<tag>-snapshot` for a role with snapshot claims, and its Job
script lives in the shared `manifests` role, so the landing deployed every tag and settled. No
flux-owned tag has a snapshot claim yet, so no live tag renders one. With D5.3b1's script, three
flat `flux_*.py` files now share `manifests/files/`. The module-family census allows them, because
each Job's ConfigMap carries its script and the connection helper as flat keys.
PR D5.4a1, image contexts as a renderer output:
[#5357](https://github.com/DanielH2018/server/pull/5357). It merged at `cb912b310` and deployed
nothing. Every `cluster()` call now builds all ten contexts, so the bundle test's shard weight
was re-recorded at 11.45s
([#5374](https://github.com/DanielH2018/server/issues/5374) narrows a one-tag render). The
promote run on `b65d0b4e0` read `verify` `verdict=good failed=`.
PR D5.2c, the revert entry point: [#5362](https://github.com/DanielH2018/server/pull/5362). It
merged at `b65d0b4e0` and deployed nothing. Its review left two runbook-test findings, and
[#5377](https://github.com/DanielH2018/server/pull/5377) replaced the ordering-word check with a
pinned sentence at `29fb683fd`.
PR D5.4a2, CI images: [#5378](https://github.com/DanielH2018/server/pull/5378). It merged at
`9d6a40b52` and was reverted in [#5390](https://github.com/DanielH2018/server/pull/5390) at
`3b74100cc`. Its first `images` run, promote run 38992066819, pushed nut, pi-peer-backup,
terraria and valheim to new GHCR packages. It failed on n8n-runners, whose context no longer
builds ([#5391](https://github.com/DanielH2018/server/issues/5391)). `publish` needs `images`, so
every promotion stopped until the revert.
[#5392](https://github.com/DanielH2018/server/pull/5392) fixed n8n-runners at `6ad46042e`, and
D5.4a2 landed again in [#5403](https://github.com/DanielH2018/server/pull/5403) at `2a1ac8573`.
Its first `images` run, promote run 39036029019, pushed n8n-runners `sha-16bb62020cf1` and found
the other nine tags already served. The run read `verdict=good` and moved `last-good`. All ten
packages answered an anonymous manifest request with 200 on 2026-10-17, so Open question 8's
ruling is in effect.
PR D5.3b2, the soak Kustomization, observed:
[#5386](https://github.com/DanielH2018/server/pull/5386). It merged at `95cd242bc` and settled
with `flux` and `observability` healthy. kustomize-controller runs `--concurrent=6`, `last-good`
reads Ready, and the landing revision's soak Job logged `soak: R changes no service; nothing to
judge`. The PR also covers `last-good` in `FluxNotReady`, and the cross-plane snapshot passes R
through `HOMELAB_RENDER_REVISION`. Its live check, two consecutive `flux/soak` `success`
statuses on revisions that change a flux-owned tag, passed on 2026-10-17 with D5.5's and D5.6's
soaks (below).
The run-once cleanup PR: [#5387](https://github.com/DanielH2018/server/pull/5387), together with
H2.7. It merged at `471397804`. It deleted game-stats' and observability's run-once record
cleanups and the render-record census row.
PR D5.2b1, the stateful hold: [#5361](https://github.com/DanielH2018/server/pull/5361). It merged
at `b346b7141` and deployed nothing. In its review the operator confirmed both of Open question
1's co-batched cases and the hold value `<T> stateful=unverified`. The first promote run after
it, 39037279058, read `verdict=good`, moved `last-good` and skipped `post`, as a run with no hold
must.
PR D5.4b, GHCR refs: [#5407](https://github.com/DanielH2018/server/pull/5407). It merged at
`588cdfd0d` and settled with `nut` healthy. No flux-owned role consumes a built image yet, so
the GHCR rewrite has no live consumer, and nut keeps `localhost:5000/nut:sha-cf9e7ebc31ed` with
`Always`. nut's `imagePullPolicy` comment is now a Jinja comment, which cost the one nut restart.
M6's B-1e landed at `f8e28584f`, so littlelink's flux PR no longer waits on M6 (Open question 12).
PR D5.5, Wave A's first PR: [#5417](https://github.com/DanielH2018/server/pull/5417). It moved
gpu-exporter, nut-exporter and pihole-exporter to Flux at `d02b4c299`. The flux CLI was not yet
installed, and the three renders substitute nothing, so the operator's pre-merge diff was a
server-side `kubectl diff` as kustomize-controller. Only gpu-exporter's DaemonSet changed, by
`checksum/restart`. Soak Job `soak-cc85dbec` judged the three tags and `flux/soak` read `success`.
The operator kept pihole-exporter with an exemption: a `pihole_password` rotation does not roll it
under Flux, and the operator rolls it by hand ([#5416](https://github.com/DanielH2018/server/issues/5416)).
PR D5.6: [#5420](https://github.com/DanielH2018/server/pull/5420). It moved littlelink,
longhorn-ui and deploy-ui to Flux at `88a31914c`. The operator's diff showed no change for any of
the three, and `deploy.sh --tags <tag>` exited 5 for each after the merge. Soak Job
`soak-9c66fcc4` judged the three tags and passed, the second consecutive judged success. The
operator accepted that code review is now the only gate on longhorn-ui's and deploy-ui's routes.
PR D5.7: [#5423](https://github.com/DanielH2018/server/pull/5423). It moved homepage and
node-exporter to Flux at `4462b2540`. homepage's Secret substitutes, so the operator ran the
real `flux diff`, with the CLI installed in dotfiles#859. It needed `--ignore-not-found`, because a
first flip has no live Kustomization. Every object gained Flux's two ownership labels, and the
Deployment gained `checksum/restart`. homepage's RBAC census removed only the
`last-applied-configuration` annotation from its three RBAC objects and kept nothing. Soak Job
`soak-5602724c` judged both tags from 17:47:09 to 17:50:35 and passed. blackbox-exporter, Wave A's
late arrival, is the ingress pager M4's soak measures, so it moves in Wave A's last PR after
2026-10-20T06:19Z.
PR D5.3b3, the soak gates: [#5426](https://github.com/DanielH2018/server/pull/5426), with
[#5380](https://github.com/DanielH2018/server/issues/5380) and
[#5419](https://github.com/DanielH2018/server/issues/5419). It merged at `0203086a3`. Its landing
deployed every Ansible-owned tag, because it edits the shared `k8s/manifests` role, and all of them
settled. The first promote run after it printed `verify: expecting flux/homelab, flux/soak on
0203086a within 3690s` and read `verdict=good`. The operator ruled to amend ADR-0012, and
[#5429](https://github.com/DanielH2018/server/pull/5429) did so at `3ca6c0895`. Its review left
[#5427](https://github.com/DanielH2018/server/issues/5427): the lander units' deadlines were shorter
than a gated landing. [#5430](https://github.com/DanielH2018/server/pull/5430) raised them at
`8592dd71a`: `claude-land@` to `TimeoutStartSec=4h` and `renovate-agent-land@` to `190min`, each
with `LAND_DEADLINE=10800`.

Related issues:

- [#4564](https://github.com/DanielH2018/server/issues/4564): karakeep-chrome's rollout wait, superseded by Flux `wait`.
- [#4556](https://github.com/DanielH2018/server/issues/4556): the qbittorrent reach probe's pod name, superseded when the verify tasks move.
- [#4188](https://github.com/DanielH2018/server/issues/4188): the Home Assistant hold and its Recreate downtime, relevant to home-assistant's
  own window (*Waves and windows*).

Depends on: [D3](d3-secrets.md), [D4](d4-ordering.md), [R3](../render/r3-remove-live-reads.md),
[M1](../monitoring/m1-alertmanager.md), [M2](../monitoring/m2-first-bridge-family.md). Wave D
also depends on H2's family PRs for six roles, as *Host-side rows* lists, and never on H1.9.

- At PR level, D5.1a1 to D5.1d need only D3.3 and D4.
- R3 gates the n8n, pi-peer-backup and authelia moves.
- M1 and M2 gate D5.3b1, and through the soak every wave.
- H1.1a gates traefik's flip and the flux PR, because H1.1a defines the `flux_prune` lever
  that both set (*How each kind of task moves*). H1 depends on D2 and D4 only, so the edge
  adds no loop.
- D3's `cluster_vars.py write` refusals gate two flips: the `{{` and argon2 refusals gate
  authelia's, and the `mqtt_password_hash` colon refusal gates mosquitto's (*How each kind of
  task moves*).
- R2 PR 7b and a green run of D3.3d's `sops-manifest-type-proof` job gate qbittorrent's flip,
  because R2 PR 7b moves `mullvad_account` into a dedicated SOPS Secret whose all-digit value
  only that job measures (*How each kind of task moves*). Neither R2 nor D3 waits on D5, so
  the edge adds no loop.
- M6's B-1e gates littlelink's flip, because M6's live health-gate drill needs littlelink
  Ansible-owned. M6 depends on M2 to M5 only, so the edge adds no loop.
- H1.7b gates the flux PR. Until H1.7b, H1's bring-up runs step 1 with
  `--skip-tags kubeconfig,longhorn,longhorn_backup` and needs the **operator-only** step 7a,
  and *Bootstrapping Flux on a rebuilt cluster* carries neither (H1, *The Longhorn window,
  H1.5a until H1.7b*). H1.7b needs at most D5.1b and D5.1d, under H1's Open question 10
  fallback, and both come long before the flux PR, so the edge adds no loop.

Unblocks: [D6](d6-delete-old-path.md). H1 sets no `flux_suspend`, because H1.7b retires
`k3s_manage_backup_targets`. Only under H1's Open question 10 fallback does H1.7b wait on D5.1b
and D5.1d (Open question 13).

## Goal and exit criterion

After D5, Flux owns every k8s service, itself, and the `homelab` namespace. Ansible's k8s play
applies nothing. Four duties that Ansible tasks performed get a Flux-era owner:

- **Pre-apply Longhorn snapshots** become a Job per role revision.
- **The restart soak** becomes a Job that queries Prometheus through M2's workload-service
  recording rules and reports a commit status.
- **Images built in the cluster** are built by CI instead.
- **Each role's imperative tasks** move to a Job, a rule, a setup role, or the renderer.
- **The restarts that three roles issue by hand** become pod-template annotations that the
  renderer computes.

The 59 Deployments and 5 DaemonSets that carry `restartedAt` roll once at their takeover. The
40 `Recreate` Deployments among them roll at their flips: at any hour, except the seven
own-window roles' (*Waves and windows*).

**Exit criterion.** Seven checks, each under *Verification*:

1. **Every entry is flux-owned.** Every k8s entry carries `owner: flux`: the 56 at
   `70591c307`, plus `flux`, plus any entry added after the baseline, such as M4's
   blackbox-exporter. The check compares two counts rather than a written number.
   `./scripts/deploy.sh --list-services` marks every k8s tag as owned by Flux, and
   `render_targets.py` lists no tag, because it lists Ansible-owned tags only. The
   render-records producer is disarmed through `render_records_state: absent` in its role
   defaults, which the tick applies through the setup arm, and its Kuma tile is gone.
2. **No client-side apply remains on what the k8s tags render.** Every Deployment, StatefulSet
   and DaemonSet in `homelab` and `observability` carries the label
   `kustomize.toolkit.fluxcd.io/name`. So do the named members `homelab/authelia`,
   `homelab/nut`, `homelab/pihole-2` and `kube-system/dri-device-plugin`. No workload with
   that label carries `kubectl.kubernetes.io/last-applied-configuration`. The add-ons in
   `longhorn-system`, `metallb-system` and the rest of `kube-system` belong to H1, so this
   check leaves them out.
3. **Every promotion is verified, soak included.** `promote` waits on the statuses of each
   revision's changed set (C2), and `flux/soak` joins that set whenever a flux-owned tag
   changes. The soak Job runs on every promoted commit, so the last ten promoted master
   commits show `flux/soak` as `success`.
4. **Snapshots and images have their new owners.** Each of the 14 roles that declare
   `k8s_autodeploy_snapshot_pvcs` has a Longhorn snapshot whose name starts
   `autodeploy-<tag>-<digest8>-`, for its digest at `prod`'s revision. Each of the ten built
   images runs from `ghcr.io/danielh2018/<name>:<content tag>`, equal to the rendered ref.
5. **Ansible applies no namespace.** The `homelab` Namespace carries the Flux label and
   `kustomize.toolkit.fluxcd.io/prune: disabled`. Its LimitRange carries the Flux label.
   `ansible/deploy.yml` holds no task that applies the Namespace or its LimitRange, so a role
   handed back to Ansible cannot re-arm one.
6. **The revert runbook works under self-management.** After the flux PR, the speedtest revert
   drill runs again end to end. Each hand suspend survives at least one reconcile of the
   Kustomization that applies the suspended object, and lists the field manager
   `homelab-operator`.
7. **kustomize-controller's memory is re-measured at full load.** After the namespace PR,
   kustomize-controller's peak working set during a reconcile of every Kustomization stays
   at or under its 64Mi request, the figure D1's exit 4 records beside ADR-0010's premise
   of about 30 MiB per controller. The flux role's `CLAUDE.md` records the peak beside
   D1's figure.

## Baseline at `70591c307`

- **The task census.** Counting top-level tasks per role under `ansible/roles/k8s/*/tasks/`
  gives three groups:
    - **21 roles have one task**, an include of `k8s/manifests`: bazarr, bento-pdf, deploy-ui,
      freshrss, gpu-exporter, home-assistant, homepage, littlelink, livesync, loki-homelab,
      longhorn-ui, navidrome, node-exporter, nut-exporter, peanut, pihole-exporter,
      scrutiny, speedtest, texbrain, wg-easy and zigbee2mqtt. D2 to D4 move three of them.
    - **10 roles have two**: cloudflare-ddns, code-server, docs, homelab-mcp, ical-proxy,
      karakeep, nut, radarr, terraria and valheim.
    - **The rest have more**, from mosquitto's 3 to observability's 30.

  R1's census row in `ansible/tests/repo/test_census_rows_roles.py` lists every task chunk that
  writes a cluster object outside `k8s/manifests`, each with its owning slice. D5 is that
  census's consumer. R1 records host writes separately, as a channel its render does not
  cover.
- **Task-file sizes.** `wc -l` over each role's `tasks/*.yml` gives more than 200 lines for
  media-volume (247), janitorr (266), qbittorrent (280), game-stats (289), jellyfin (296),
  registry (298), authelia (317), netpol-baseline (347), crowdsec (433), pihole (509) and
  observability (592). tdarr has 183.
- **Rollout strategies.** 32 roles render a `Recreate` workload, from authelia to
  zigbee2mqtt. Seven render a DaemonSet: crowdsec, dri-device-plugin, gpu-exporter,
  loki-homelab, node-exporter, observability and scrutiny. Live, 59 of 71 Deployments carry
  `restartedAt`, 40 of them `Recreate`, as do 5 DaemonSets (D2 baseline). navidrome is
  `Recreate` and parked at `navidrome_k8s_replicas: 0`.
- **Snapshots.** `grep -l '^k8s_autodeploy_snapshot_pvcs' ansible/roles/k8s/*/defaults/main.yml
  | wc -l` prints 14: bazarr, code-server, freshrss, home-assistant, jellyfin, karakeep,
  livesync, prowlarr, qbittorrent, radarr, sonarr, speedtest, tdarr and zigbee2mqtt.
  navidrome's and observability's defaults name the key only in comments that say they declare
  none. `ansible/roles/k8s/volume-snapshot/CLAUDE.md` says "Fourteen roles are opted in".
    - That file names each snapshot `autodeploy-<service>-<sha8>-<claim>-<token>`. The token
      is a UTC timestamp (`volume_snapshot_run_token`, format `%Y%m%d%H%M%S`) that "keeps a
      rollback deploy from being refused by its own protection step".
    - A pruned Snapshot CR keeps a `longhorn.io` finalizer and sets `markRemoved: true`, so
      "the name stays taken". This is failure class 6 (Longhorn's `markRemoved` CR).
    - A name must stay under 63 bytes (#2734), and the retention floor is 2.
    - The claim part drops a leading `<service>-`. `volume-revert`'s task "Name the snapshot
      prefix for …" in `ansible/roles/k8s/volume-revert/tasks/claim.yml` builds the same
      prefix and checks `volume_revert_sha` against `^[0-9a-f]{8,}$`. The longest names are
      code-server's workspace claim and home-assistant's config claim, at 56 bytes each.
- **The revert.** `ansible/roles/k8s/volume-revert/CLAUDE.md` says the role "scales the
  Deployment to zero, reverts, and leaves it there", and that "a caller that reverts without
  applying afterwards leaves the service down". It has no standalone deploy tag. Its only
  caller is the manifests role, gated on `k8s_restore_snapshot_sha`, which only the deployer's
  rollback sets.
- **Built images.** `ansible/roles/k8s/image-builder/CLAUDE.md` lists nine callers:
  ical-proxy, terraria, pi-peer-backup, n8n, code-server, nut, homelab-mcp, valheim and
  karakeep. n8n builds two images, `n8n` and `n8n-runners`, so there are ten. Ten
  `*_k8s_image` pins read `k8s_built_image_tags`, valheim's being its initContainer image
  `valheim_k8s_mods_image`.
    - Each build pushes a content tag, `sha-<12 hex>`, hashed over its context ConfigMap's
      `data`, to the in-cluster registry.
    - Every context pins its base image by digest in its `FROM` line, for example
      `ansible/roles/k8s/nut/templates/Dockerfile.j2`, and Renovate bumps those digests.
    - `image_builder_force=true` rebuilds for a base-image CVE that the hash cannot see.
    - A GitHub-hosted runner cannot push to the in-cluster registry. The cluster pulls the
      public package `ghcr.io/danielh2018/texbrain` without a pull secret (D1 baseline).
    - Every consumer sets `imagePullPolicy: Always`. `grep -rn imagePullPolicy` over the nine
      roles' templates finds it in n8n (two Deployments), homelab-mcp, nut, ical-proxy,
      terraria, valheim, code-server (twice), karakeep (three times) and pi-peer-backup's
      CronJob. `ansible/roles/k8s/registry/CLAUDE.md` says a built-image pod "re-fetches by
      tag on each start". With `Always`, kubelet resolves the tag at the registry on every
      container start, so an unreachable registry fails a restart even when the image is
      cached.
- **Master's branch rules.** `gh api repos/DanielH2018/server/rules/branches/master` returns
  the rule types `creation`, `deletion`, `non_fast_forward`, `pull_request`,
  `required_signatures`, `required_status_checks` and `update`. GitHub starts no workflow run
  for a push or a pull request that `GITHUB_TOKEN` creates, so a bot commit gets neither the
  required checks nor `promote`. The status-check rule's
  `strict_required_status_checks_policy` reads `false`, so a PR's checks may have run against
  an older master than the one it merges into.
- **Rendered Jobs that tasks apply.** These templates set a fixed name and
  `ttlSecondsAfterFinished`, and a task applies each one outside `k8s/manifests`:
    - `netpol-baseline-probe` (`ansible/roles/k8s/netpol-baseline/templates/netpol-probe-job.yaml.j2`);
    - the netpol probe macro `ansible/templates/netpol-probe-job.yml.j2`, called by n8n
      (`n8n-netpol-probe`), headlamp (`headlamp-netpol-probe`), prowlarr
      (`flaresolverr-netpol-probe`) and registry (`registry-netpol-probe`);
    - `media-volume-hardlink-probe` (media-volume);
    - registry's `registry-selftest-push`, `registry-selftest-pull` and
      `registry-selftest-pull-agent`, and `registry-gc`, whose manifest the host cron
      `registry-gc.sh` reads at run time.
- **Imperative ConfigMaps.** autofix-bridge, game-stats, monitor-bridge and observability run
  `kubectl create configmap --from-file` in their tasks.
- **Host writes inside k8s roles.** Twelve roles write outside `/etc/rancher/k3s/manifests/`.
  R1's baseline names nine and docs is the tenth. D5.1c's census found media-volume and
  game-stats, and a second write in each of observability and traefik (#5266). The staging
  under `/etc/rancher/k3s/<role>/` of the imperative ConfigMaps above is a host write too.
  Their row moves it, so it is not repeated here.
    - artifacts: peer directories, the peer sync script and its cron.
    - configarr: the health reader scripts, `host_lib`, and "Schedule the configarr sync
      heartbeat".
    - crowdsec: two decision modules and four crons, from "Schedule the home-allowlist updater"
      to "Schedule the AppSec verifier".
    - docs: "Ensure the built docs site directory exists".
    - game-stats: "Remove the staged manifests, release and render records the pre-#2813
      roles left behind", an absent arm over three staging directories and six records. It
      is a run-once cleanup. daniel-box applied game-stats at `c5d00ead`, which carries the
      cleanup's last edit (`7fceba186`), and none of the six records is left. The three
      directories sit under a root-only path, so their removal rests on that apply.
    - janitorr: the health reader and "Schedule the janitorr error heartbeat".
    - media-volume: "Create the media volume directories on the node", three
      `state: directory` tasks under `media_volume_host_path` (`/srv/media`) on daniel-box,
      the node the PV pins. The hardlink probe needs `media/` and `torrents/` to exist on a
      volume that has never been synced.
    - observability: "Schedule the telemetry-stack heartbeat", and "Remove the release and
      render records the claude-otel name left behind", a run-once absent arm over three
      records. daniel-box applied observability at `c5d00ead` too, and none of the three is
      left.
    - pi-peer-backup: "Install the forced-command wrapper on daniel-pi" and "Authorize the pull
      key on daniel-pi", both `delegate_to: daniel-pi`.
    - qbittorrent: the prefs-check script and its cron.
    - registry: the job manifest directory and the GC cron. The cron's script,
      `ansible/roles/k8s/registry/templates/registry-gc.sh.j2`, runs `kubectl scale
      deploy/registry --replicas=0` for the GC ("STOP-THE-WORLD, NOT OPTIONAL") and scales
      back in `scale_up_and_report`. The rendered Deployment sets `replicas: 1`. The GC is
      monitored by the Kuma `Registry GC` tile, the healthchecks.io `registry-gc` check in
      monitor-bridge's defaults, `registry_gc_push_token` in `ansible/secret_rotation.yml`,
      and `scripts/validate/validate_lib/cron_targets.py`. The self-test Jobs copy
      `registry_k8s_probe_source` (`alpine:3.22`) with `crane copy`, which re-pushes only
      layers the registry lacks.
    - traefik: "Remove the Cloudflare IP drift cron", an absent arm, and "Install the
      Traefik RBAC (cluster-scoped role + per-namespace roles)". That task templates
      `rbac.yaml.j2` to `/etc/rancher/k3s/traefik-clusterrole.yaml`, and the next task
      applies the file. The file holds Roles in other namespaces, and the role's namespaced
      `-n` apply of the manifests directory never reads it.

  The other host scripts that D5 moves run `kubectl exec` into an app at most. None scales,
  patches or applies a cluster object.
- **Utility roles.** `ansible/roles/k8s/` holds 63 role directories against 56 k8s entries.
  The seven with no entry are utility roles that R4 deletes: arr-notification, cronjob-gate,
  image-builder, longhorn-api, manifests, volume-revert and volume-snapshot. Three task files
  among them use `delegate_to` or write the host: `ansible/roles/k8s/manifests/tasks/release_digest.yml`
  (`delegate_to: localhost`), `ansible/roles/k8s/volume-snapshot/tasks/main.yml` ("Resolve
  the deploy tag for …", a git read on localhost) and
  `ansible/roles/k8s/manifests/tasks/release_stamp.yml` (a copy into `manifests_release_dir`,
  `/var/lib/homelab/k8s-releases.d`).
- **Verify tasks.** Six roles have a `tasks/verify.yml`: crowdsec, janitorr, jellyfin,
  qbittorrent, sonarr and tdarr.
    - sonarr sets `k8s_autodeploy: true`, and its `CLAUDE.md` says `verify.yml` "opens with
      its own `rollout status` gate".
    - Renovate automerges a digest or minor bump of an eligible role through its own merge
      call, and `renovate.json`'s descriptions say "the deployer's health gate and rollback
      are its only check". D2 runs `probe.py health` only from `land.sh`, which such a merge
      never calls.
    - crowdsec's verify, "Ban daniel-pi and prove the edge enforces it", bans the Pi at LAPI
      and probes the edge from the Pi.
    - R4's table *Where each half of a task file goes* names D5 as the owner of every verify
      successor, and R4 checks them.
    - **Today's verifies reach their targets as the node, or through root `kubectl exec`.**
      Counting `uri:` and `curl ` in each `tasks/verify.yml` gives sonarr 2, qbittorrent 3,
      crowdsec 2, janitorr 1, jellyfin 0 and tdarr 0. Counting ` exec ` gives janitorr 5,
      crowdsec 5, jellyfin 4, qbittorrent 3, tdarr 3 and sonarr 0. sonarr's reads dial the
      Service ClusterIP from daniel-box, which `netpol_baseline_node_cidrs` admits. sonarr's
      `containers_list` entry sets `netpol_role_owned: true`, and its `netpol_from` admits
      traefik, janitorr, configarr, monitor-bridge and autofix-bridge only.
      `networkpolicy.yaml.j2`'s `baseline-ingress` admits `app: traefik`, observability's
      `app: prometheus` and the node CIDRs. `networkpolicy-prometheus.yaml.j2` admits from
      `homelab` only monitor-bridge, homelab-mcp and homepage.
- **Three roles restart their own workloads on a config or Secret change.** Each sets
  `manifests_rollout: ''`, which skips the shared restart as well as the shared wait.
    - cloudflare-ddns names both Deployments in `manifests_extra_rollouts`. Its `CLAUDE.md`
      records the failure without it: a rotated push token left both monitors DOWN behind a
      green deploy. Its templates carry no `checksum/` annotation.
    - observability hashes each ConfigMap into its own pod template as `checksum/config`. A
      Secret edit goes through "Restart the telemetry workloads whose secret changed", for
      each `observability_stabilise_workloads` entry whose `restart_on` holds `secret`
      (prometheus and grafana). M1 PR 2 adds alertmanager to that list, and M1 PR 5 narrows
      each restart to the Secret files the workload reads (`restart_secrets`).
      "Sync the live Grafana admin password with SOPS" runs `grafana cli admin
      reset-admin-password` through `kubectl exec` when a Secret changed.
    - pihole restarts both instances through `tasks/roll_one.yml` (`rollout restart
      deploy/{{ pihole_instance }}`) when its render, its Secret or its image changes.
      `grep -rl 'checksum/' ansible/roles/k8s/pihole/templates/` prints nothing.
    - `grep -rn 'rollout restart' ansible/roles/k8s/*/tasks/*.yml`, outside `k8s/manifests`
      and comments, finds only observability's and pihole's tasks. prowlarr, which the
      manifests `CLAUDE.md` names beside them, rolls flaresolverr through
      `manifests_extra_rollouts` with a non-empty `manifests_rollout`, which D3's rule
      covers. A restart written as a raw task in a later role is invisible to D5.1e's
      census, which reads include vars, so R1's census row and each role PR's class quote
      are its backstop.
    - **The three are the whole set.** `grep -rln "manifests_rollout: ''\|manifests_rollout:
      \"\"" ansible/roles/k8s/*/tasks/` names eleven role directories. One is `manifests`,
      whose match is a comment. Of the other ten, seven run no long-lived pod that reads a
      ConfigMap or Secret:
        - deploy-ui and longhorn-ui render a route only, as their task comments say;
        - dri-device-plugin's DaemonSet names no ConfigMap or Secret;
        - configarr and pi-peer-backup render a CronJob, whose next run reads the new
          content;
        - media-volume and netpol-baseline render no Deployment, only a probe Job.

      `grep -ln 'configMap\|secretRef\|secretKeyRef\|configMapKeyRef\|secretName'` over
      dri-device-plugin's and media-volume's templates prints nothing. D5 moves all ten, and
      no other slice flips an existing k8s role.
- **The gate Job.** `ansible/roles/k8s/cronjob-gate/tasks/main.yml` deletes, then creates, a
  Job `<cronjob>-deploy-gate` from the CronJob, and waits for it. Its callers are configarr
  and pi-peer-backup. `ansible/roles/k8s/pi-peer-backup/files/pull-pi-peers.sh` sets `GATE_RUN=1` when
  `HOSTNAME` matches `pi-peer-backup-deploy-gate-*`, and then pushes neither Kuma nor
  Healthchecks.io. pi-peer-backup's `CLAUDE.md` says a configarr gate run "genuinely performs
  the reconcile".
- **Other in-cluster writes that R4 assigns to D5.** crowdsec's "Register the remote agent
  machines on the LAPI", "Create the home-ips allowlist on the LAPI" and "Create the
  remote-ips allowlist on the LAPI" each `kubectl exec` into `deploy/crowdsec`.
  `ansible/roles/k8s/arr-notification/` runs `files/seed_arr_notification.py` against the
  Service ClusterIP of sonarr and radarr, which include it. The script imports only the
  standard library. `ansible/roles/k8s/manifests/tasks/verify_secret_keys.yml` patches out a
  live Secret key that the render no longer declares, because `kubectl apply` prunes only
  keys it holds a last-applied baseline for.
- **Host-file sources are role-relative.** crowdsec's `tasks/main.yml` copies `src:
  crowdsec-update-home-allowlist.sh.j2`, `remote_allowlist.py`, `bouncer_prune.py` and
  `crowdsec-appsec-verify.sh.j2`, and those templates read crowdsec's role defaults.
  netpol-baseline's `CLAUDE.md` states that "Role defaults are role-scoped, so neither
  resolves in another role".
- **The soak.** `ansible/post_tasks/k8s_stabilise_gate.yml` reads restart counts after
  `rollout status` returns, waits `k8s_rollout_stabilise_seconds` (60, in
  `ansible/inventory/group_vars/all.yml`), and reads them again. ADR-0012 states a 180-second
  soak.
    - Prometheus scrapes kube-state-metrics every 1m (M2 baseline).
      `ansible/roles/k8s/observability/templates/prometheus.yaml.j2` sets no
      `evaluation_interval`, so rules evaluate at Prometheus's default of 1m.
    - `probe.py metric 'count(kube_pod_status_ready_time)'` returned 101 on 2026-10-11, so
      kube-state-metrics exports each pod's Ready time.
    - Prometheus is reachable only on the LAN. ADR-0018 keeps CI hosted, so the `promote` job
      cannot query Prometheus.
- **M2's recording rules.** M2 PR 1 adds `homelab:pod_service:info{namespace, pod, service}`.
  Its literal override table maps `authelia` and `authelia-redis` to `authelia`, `nut` to
  `nut` and `pihole-2` to `pihole`, which carry no `homelab/role` label. M2 states that "D5
  can join its own soak rule to the same workload-service recording rules".
- **The weekly reboot.** The "Weekly system restart" cron runs at `weekly_reboot_hour` 7 and
  `weekly_reboot_minute` 30, Sunday, in UTC. M2's `weekly-reboot` mute covers Sunday 07:25 to
  08:15 UTC.
- **M1's rule layout.** M1 puts rule files in
  `ansible/roles/k8s/observability/files/rules/<group>.yaml` and their promtool tests in
  `ansible/roles/k8s/observability/tests/rules/`. C7 requires `service` and `severity` labels
  on every rule.
- **The namespace comes from the play, not from a role.** `ansible/deploy.yml`'s k8s pre_tasks
  "Build the workload namespace manifest" and "Apply the workload namespace" apply the
  `homelab` Namespace, with `k8s_psa_labels`, and its LimitRange. "Tear down retired k8s
  services" tears down `k8s_retired_services`. Both writes run while `containers_list` is
  non-empty. D1 adds a `flux` entry with no owner key, so the list keeps an Ansible-owned
  entry until the flux role flips.
    - Three test files read those two tasks by name or by text:
        - `ansible/tests/k8s/test_namespace_resource_defaults.py:_homelab_ns_items`, which
          feeds the LimitRange tests, and that file's
          `test_the_namespace_is_applied_before_its_limitrange` and
          `test_the_namespace_apply_reports_changed_per_line`;
        - `ansible/tests/deploy/test_k8s_dry_run.py:test_namespace_apply_is_guarded`;
        - `ansible/tests/setup/test_k3s_control_plane_hardening.py`'s
          `test_both_namespaces_carry_the_psa_labels` and
          `test_namespace_apply_reports_a_label_change_as_changed`.
- **Pre-apply live reads.** R3 removes all three: pi-peer-backup's host key, n8n's digest and
  authelia's argon2 digests. R3 PR 3 adds the SOPS keys `authelia_password_argon2`,
  `authelia_claude_password_argon2` and `authelia_agent_password_argon2`, and deletes the read,
  reuse and hash tasks. authelia's TOTP seeding task stays for D5.
- **Renovate.** A packageRule disables automerge for an image bump in every role declaring
  `k8s_autodeploy: false` (issue #1886). A separate rule automerges digest bumps on the k8s
  plane.
    - The rule with `groupName` `k8s image {{depName}}` makes one PR per image, not per role.
      The exportarr sidecar is one image pinned in sonarr, radarr and prowlarr, held equal by
      `ansible/tests/services/test_exportarr_sidecars.py::test_the_image_pins_stay_in_lockstep`.
      All three are snapshot roles, so one exportarr bump changes three snapshot roles in one
      commit.
- **`k8s_autodeploy: false` stops auto-deploys of image bumps.**
  `ansible/roles/setup/gitops_deploy/CLAUDE.md` says "`k8s_autodeploy: false` is how you stop
  a role". A hand `land.sh` still deploys such a role.
- **Batch width.** The play rolls `ansible/inventory/group_vars/all.yml:k8s_rollout_batch_width`
  (5) roles at once.
- **Flux strips client-side state, and takes over hand writes.** kustomize-controller v1.9.6's
  `internal/controller/kustomization_controller.go`, read through `gh`, sets
  `applyOpts.Cleanup` to remove the annotation `corev1.LastAppliedConfigAnnotation` and lists
  four field managers: `kubectl` (Apply), `kubectl` (Update), `before-first-apply` (Update) and
  the controller's own name (Update).
    - In `fluxcd/pkg`, `ssa/patch.go:matchFieldManager` matches a manager by prefix unless
      `ExactMatch` is set, and skips an entry with a subresource. So `kubectl-patch`,
      `kubectl-rollout` and `kubectl-client-side-apply` all match `kubectl`.
    - `ssa/manager_apply.go:cleanupMetadata` calls `PatchReplaceFieldsManagers`, which hands
      the matched fields to kustomize-controller's own Apply entry. The next apply that omits
      such a field removes it. This runs on every existing object at every apply.
    - Live, read-only: `kubectl -n homelab get deploy speedtest -o
      jsonpath='{.metadata.managedFields[*].manager}'` printed `kubectl-rollout
      kubectl-client-side-apply k3s` on 2026-10-11.
    - In `internal/controller/kustomization_controller.go`, `Reconcile` returns on
      `checkDependencies` with `DependencyNotReady` before `reconcile` sets
      `obj.Status.LastAttemptedRevision`. A Kustomization that an upstream failure blocked
      therefore never reads the blocked revision in that field.
    - `reconcile` sets that field from `revision := src.GetArtifact().Revision`, so for the
      OCI source it holds `prod@sha256:<digest>` and never a commit (C6). `getOriginRevision`
      in the same file reads the commit from the artifact's metadata key
      `org.opencontainers.image.revision`, which C2's push sets to `master@sha1:<sha>`. D4's
      Risks name a reading of the wrong field as failure class 2.
    - A `kubectl scale` writes through the `scale` subresource, so cleanup leaves its entry
      alone. Flux still restores a rendered `replicas` value, as ordinary drift correction.
- **Who applies the Flux objects after the flux PR.** D1 makes `sync.yaml.j2` a
  `manifests_deferred_files` entry, holding the OCIRepository `homelab`, the root Kustomization
  `homelab`, the Providers and the Alerts. D4 gives a tag with deferred files a `<tag>-deferred`
  Kustomization. Once the flux role is flux-owned, `flux-deferred` applies the root, and the
  root applies every Kustomization object in `./flux`, `flux-deferred` included. Every object a
  runbook suspends therefore has a Flux applier. A hand `kubectl patch` under its default
  manager, `kubectl-patch`, is reverted at that applier's next reconcile.
- **The Pi's log path crosses the edge.** The Pi's Alloy pushes to
  `https://loki-homelab.local.{{ domain }}/loki/api/v1/push`
  (`ansible/roles/containers/alloy/templates/config.alloy.j2`), through Traefik. crowdsec's
  "Probe the VIP from the banned Pi — expect 403" proves the edge refuses the Pi's requests
  during a ban. Prometheus scrapes the Pi's Alloy directly at the Pi's LAN address, as job
  `alloy-pi` in `ansible/roles/k8s/observability/templates/prometheus.yaml.j2`. The Pi's
  node_exporter runs with `--no-collector.textfile`
  (`ansible/roles/setup/optimize_pi/templates/node_exporter.service.j2`).
- **render_records.** `ansible/roles/setup/render_records/defaults/main.yml:render_records_state`
  derives from `render_records_enabled`. R4's Open question 5 asks D5 to disarm the producer at
  or before the flip that empties `render_targets.py`'s list. `files/render_records.py:main`
  pushes `down` and returns 1 on an empty list (R4 baseline).
    - The two levers route to different planes. Measured with
      `deploy_changes.py:services_from_changed_paths`:
      `['ansible/inventory/group_vars/all.yml']` gives `{'broad': True, 'broad_deploy':
      True}`. `['ansible/roles/setup/render_records/defaults/main.yml']` gives `{'broad':
      True, 'broad_setup': True, 'setup_roles': {'render_records'}}`.
    - `ansible/initial_setup.yml` applies the role only through `--tags render_records`, so
      only the second route reaches its absent arm from a tick. A `group_vars` flip runs
      `deploy.yml`, which never includes the role.
    - The `render-records.json` tile in uptime-kuma's `static-monitors.yaml.j2` has no gate on
      `render_records_enabled`, as its comment says, so it turns red once the producer stops.
- **A new setup role's playbook entry parks the tick.**
  `scripts/deploy_tools/setup_routing.py:PLAYBOOKS` routes a setup role only through
  `initial_setup.yml`, `k3s-bringup.yml` or `bootstrap.yml`, so the PR that creates a setup
  role edits one of them. Measured with `services_from_changed_paths` at `70591c307`:
    - `['ansible/initial_setup.yml']` gives `broad_manual=True` and
      `manual_playbooks={'ansible/initial_setup.yml'}`.
      `deploy_defer.py:parks_the_tick` returns True on `cs.broad_manual`, so the tick parks
      with no fast-forward and writes `behind_since`. Every later merge waits behind it until
      the primary checkout is fast-forwarded by hand (`docs/gitops-pipeline.md`, *When a tick
      parks*). A hand fast-forward empties the range, so it also skips the deploys of any
      other merge inside it.
    - A path under `ansible/roles/setup/k8s_host_files/` gives `broad_setup=True` and
      `setup_roles={'k8s_host_files'}`, which the tick applies itself once the playbook
      entry exists.
    - `initial_setup.yml` loads its become password from SOPS, and the agent user never holds
      the age key (`docs/claude-agent-user.md`), so a hand run of it decrypts.
- **The reset input shares `promote`'s concurrency group.** C2 gives `promote` the group
  `promote`. GitHub keeps one running and one pending run per group, and a newly queued run
  cancels the pending one. So a `workflow_dispatch` of the same workflow waits behind a
  running run, and the next merge's run can cancel it.

## Design

### Mechanisms first, then waves

```text
D5.1a1 per-tag kinds in D2.6b's allowlist census, reserved names, deletionPolicy
D5.1a2 prune-disabled labels, claims and suspend fields in digests.json, collision census
D5.1a3 Job census: TTL, names, backoffLimit, rerun-safe, connection helper
D5.1b the suspend lever: render, no `suspend: false`, promote exclusion, hand-write field manager
D5.1c host-write census, kustomize-controller concurrency
D5.1d suspended-role gate, land-wait `suspended`, promote `suspended-changed`
D5.1e D3's checksum/restart widened to self-rolled workloads
D5.1f removal gates: a dropped workload, a retirement of a flux-owned service
D5.1g takeover census: fields a cleaned manager owns that the render omits
D5.1h the k8s_host_files role and its initial_setup.yml entry, alone (parks the tick once)
D5.2a snapshot Jobs ─┐
D5.2b1 stateful hold and its post ─▶ D5.2b2 reset input and reboot window
D5.2c revert entry point and runbook
D5.3a Prometheus peer ─▶ D5.3b1 soak script, inert ─▶ D5.3b2 soak Kustomization ─▶ D5.3b3 verify and land-wait wait on flux/soak
D5.4a1 image contexts ─▶ D5.4a2 CI images ─▶ (operator: packages public) ─▶ D5.4b GHCR refs
        ─▶ Waves A, A', B (speedtest revert drill), C, D (conversion PRs, then flip)
        ─▶ render-records disarm ─▶ bootstrap drill ─▶ flux itself ─▶ namespace
```

### Prune exemptions, digests and the collision census (D5.1a1, D5.1a2)

- **Prune exemptions.** The renderer adds the label `kustomize.toolkit.fluxcd.io/prune:
  disabled` to every PersistentVolumeClaim and Namespace it emits, as C3 requires.
- **`deletionPolicy: Orphan` on every non-tag Kustomization.** D2 sets it on each per-tag
  Kustomization, and D4 copies it to `-deferred`. The renderer sets it on `<tag>-snapshot`,
  `<tag>-verify`, `soak` and `homelab-namespace` too. Removing any of them from the bundle
  then deletes nothing, which matters most for `homelab-namespace`'s LimitRange. A renderer
  test fails on a Kustomization in the fixture bundle without it.
- **Per-tag digests.** D2's first publishing PR adds `out/digests.json`, which maps each
  name to an object with two digests, `own` and `footprint` (C1). D5 reads `own`, the
  SHA-256 of `cluster/<tag>/` alone, wherever this plan says a tag's digest: `digest8`,
  snapshot names, the D5.1d gate, `suspended-changed` and the stateful hold's trigger.
  `digest8` is the first eight hex characters of `own`. C2's changed set reads `footprint`.
    - **What D5.1a2 adds.** Each tag's object gains two fields: `claims`, the snapshot
      claims the tag declares, and `suspended`, the rendered suspend flag. C1 does not list
      them, and Open question 10 requests that amendment.
    - **Why the stateful rules read `own`.** The snapshot Job and the revert both key on
      `own`'s `digest8`, so a rule keyed on `footprint` would name a revision that took no
      snapshot. A change that moves only `footprint`, such as a `flux/<tag>.yaml` timeout or
      a `cluster-vars` file, changes no image the role runs.
- **A collision census.** A test in `scripts/renderer/tests/` fails when two tags render the
  same apiVersion, kind, namespace and name. A pair that must share an object is listed with
  its reason and moves in one PR. Otherwise one object would have two writers, one per plane.
- **Where each Kustomization is written.** D5 builds on the layout that C1 and C3 already
  state, and changes one timing in it (Open question 16):
    - **What D2 to D4 leave.** D2.6b adds the publish census with an empty `SHARED_KINDS`.
      The census is an allowlist computed from the renderer's inputs, never from its output,
      and `SHARED_KINDS` ships empty. A shared kind has its own file,
      `out/flux/<name>.yaml`, holding exactly the Kustomization `<name>`, and its own
      directory, `out/cluster/<name>/`. A member with no file fails the census (D4, *A shared
      kind has its own file*). D3.3a adds `cluster-vars`, the first member. The expected
      statuses need no change, because C2's changed set covers each Kustomization in
      `out/flux/` whose footprint moved, with its dependents. A kind that D5 adds needs only its
      footprint rule. The one exception is temporary: from D5.3b2 until D5.3b3, `verify`'s
      observe-only set holds `soak` (*The soak as a Job*).
    - **What D5.1a1 adds.** A per-tag kind (`<tag>-snapshot`, `<tag>-verify`) is a document
      in `out/flux/<tag>.yaml`, with its manifests in `out/cluster/<tag>-snapshot/` or
      `out/cluster/<tag>-verify/`. Each tag's file must hold exactly `<tag>` and the suffixed
      kinds its role's inputs call for: `-deferred` when the role passes
      `manifests_deferred_files`, `-snapshot` when it declares `k8s_autodeploy_snapshot_pvcs`,
      and `-verify` when it has a verify Job or a post-apply mutation Job.
    - **How a role declares a verify Job.** The role ships it, with any ConfigMap or RBAC
      beside it, as `templates/verify/*.yaml.j2`. The renderer renders those files into
      `out/cluster/<tag>-verify/`, and the census grants `-verify` to the published tags
      whose role ships one. Both read `verify_files` in `scripts/renderer/renderer_lib/ordering.py` (#5261).
      The manifests role derives its file lists from the top-level `templates/*.yaml.j2`
      alone, so Ansible never renders or applies them. A role therefore adds the directory
      in the PR that hands its tag to Flux.
    - **`soak` and `homelab-namespace` join `SHARED_KINDS` later.** Each joins in the PR that
      renders it: `soak` in D5.3b2, `homelab-namespace` in the namespace PR. D4's census fails a
      member with no file, so adding either in D5.1a1, before any file renders it, would fail
      D5.1a1 itself. C3 says D5.1a1 adds both, and Open question 16 asks to amend that row.
    - **Reserved names.** D5.1a1 adds a literal list of reserved names, `soak` and
      `homelab-namespace`, beside the members of `SHARED_KINDS`, so no deploy tag can take
      either name before its kind exists. It also reserves the suffixes `-snapshot` and
      `-verify`, beside D4.2's `-deferred`.
    - D2's `out/digests.json` sits at the bundle root (C1). No Kustomization's path reaches
      it.
    - Red fixtures: an unknown extra document in a tag's file, an unknown extra file, a
      fixture tag named `soak`, which the literal reserved list fails, and a renderer patched
      to emit a `-snapshot` document for a role that declares no snapshot claims. Each later
      PR that adds a kind adds it to `SHARED_KINDS` or to the input rule, with a fixture
      showing that the kind passes.

### The stop lever (D5.1b)

A role's `defaults/main.yml` may set `flux_suspend: true`. Its meaning differs from
`k8s_autodeploy: false`. `k8s_autodeploy` stops Renovate automerging the role and, while
Ansible owns it, the deployer auto-deploying its image bumps. `flux_suspend` stops all
reconciliation of a flux-owned role. D5.1b rewrites the sentence in
`ansible/roles/setup/gitops_deploy/CLAUDE.md` to name which lever applies to which plane.

- **What it renders.** `spec.suspend: true` on `<tag>` and on each of its non-tag
  Kustomizations (`<tag>-snapshot`, `<tag>-verify`, `<tag>-deferred`).
- **It never renders `spec.suspend: false`.** The renderer omits the field unless it is true,
  for every Flux object, the sync objects included. kustomize-controller applies with forced
  ownership, so a rendered `false` would take the field back from an operator's hand suspend.
  A test in `scripts/renderer/tests/` renders the whole fixture bundle and fails on any
  `suspend: false`. D1's `sync.yaml.j2` follows the same rule (a cross-slice note to D1).
- **Hand suspends use their own field manager.** Every hand suspend and resume, in the
  revert runbook and in the flux PR's recovery runbook, is `sudo k3s kubectl -n flux-system
  patch <kind> <name> --type merge --field-manager=homelab-operator -p
  '{"spec":{"suspend":true}}'`, and its resume is the same command with `false`. The manager
  name matches none of the four that kustomize-controller cleans (Baseline), so the applier of
  the suspended object leaves the field alone. The default manager, `kubectl-patch`, matches the
  `kubectl` prefix, and the applier would revert it at its next reconcile. D5 uses this one form
  from D5.2c on, so the runbook does not change shape at the flux PR. The manager is for
  `spec.suspend` only. A hand apply of whole objects keeps kubectl's default manager, so that
  Flux takes its fields back (the broken-sync-object runbook in the flux PR).
- **Not an expected status.** C2's changed set leaves suspended Kustomizations out, and
  `promote.py verify` reads the flag from the `suspended` field of `digests.json`. A
  suspended Kustomization never posts and never advances its revision, so without this every
  verify whose changed set reached it would time out. Any other reader that
  compares a Kustomization's `status.lastAppliedRevision` with its source's revision must drop
  a suspended Kustomization the same way. H6's host-mode gate is one such reader.
- **No edge to it.** The renderer drops every `dependsOn` edge that names a suspended
  Kustomization, including `soak`'s edges and netpol-baseline's `K8S_APPLIES_LAST` edges. It
  lists each dropped edge in the annotation `homelab/suspended-deps`, as D4 does for an
  Ansible-owned dependency. Without this, every dependent would wait forever on the suspended
  tag's revision.
- **A suspended role takes no change (D5.1d).** A merged change to a suspended role applies
  nothing until the resume. Ordering the merges, as D4's `cross_plane_order.py` does for an
  Ansible-owned role, is not enough, because nothing applies the suspended role's half. So the
  suspend is a temporary stop, and three checks hold it there:
    - **The gate.** `cross_plane_order.py` refuses a PR that changes the render under
      `cluster/<tag>/` of a tag suspended on its base, unless the tag is not suspended at the
      PR's head. It compares `own` and reads the `suspended` field of `digests.json` at both
  ends, never the
      `flux_suspend` key itself, so a role that derives the key from another variable is
      covered too. A shared-template change whose render touches the suspended tag waits for
      the resume. That is the cost.
    - **The race.** Master's status-check rule is not strict (Baseline), so a PR whose checks
      ran before the suspend merged can still merge after it. `promote.py decide` therefore
      treats a revision R that changes the `own` digest of a tag suspended at both
      `last-good` and R as a hold, with the cause `suspended-changed`, naming the tag. The
      hold path writes `DEPLOY_HOLD` as `<R> suspended-changed=<tag>` and posts it. A step of
      the `promote` job after Decide sends the post, with the webhook and no token (#5359), as
      D5.2b1's `post` job does for the stateful hold. The value never takes C2's `failed=` form, so C2's proposed revert rule never
      clears it. `probe.py landing` prints the
      cause beside `DEPLOY_HOLD`. The way out is a commit that reverts the racing change or
      resumes the tag. Clearing `DEPLOY_HOLD` without one only holds again at the next
      `decide`, and the post says so.
    - **`land-wait`.** D2's `promote.py land-wait` gains the verdict `suspended`. When one of
      the PR's tags is suspended at the merge SHA, it exits at once and names the tag, rather
      than polling to `gave-up` at twice the promote timeout, about 5 hours on the full map
      (D4, *The size*). The suspend PR itself ends this way.
- **D5.1b is the only definer of `flux_suspend`.** The key, its verify exclusion, its dropped
  edges and its soak exclusion land in D5.1b, and the gate, the `suspended-changed` hold and
  the `land-wait` verdict land in D5.1d. A copy defined anywhere else would lack the last three
  and leave a suspended tag timing out every verify. H1 defines and renders none of it: H1.7b
  retires `k3s_manage_backup_targets` rather than map it to a lasting suspend, and H1.1a adds
  nothing of the key (H1, *The backup plane and its levers*).
    - An operator who must stop `longhorn-backup` uses this key as a temporary stop. D5.1d
      then refuses every render change to that tag until the resume, unless the same PR
      removes the key.
    - **H1's fallback.** If the operator keeps the mode instead (H1 Open question 10), H1.7b
      renders the flag through `flux_suspend` and waits on D5.1b and D5.1d. A PR that changes
      the render and sets the variable back to true then passes, because D5.1d reads the
      suspend flag from `digests.json` and the tag is not suspended at the PR's head. Open
      question 13 records both cases.
- **It takes hold only once `last-good` carries it.** A hold-and-reset applies `last-good`'s
  bundle. If `last-good` predates the suspend, the reset un-suspends the role. So the suspend
  PR lands alone, and the session checks that `probe.py landing` shows `last-good` at or past
  it.
- **The way back and the way to see it.** A PR that removes the key resumes the role.
  `probe.py landing` lists every suspended Kustomization and OCIRepository, the root
  included, from `.spec.suspend` read with the read-only `kubectl`. It names the field manager
  of each, so a hand suspend reads differently from one in git.

### Host writes and concurrency (D5.1c)

- **A host-write census.** A `Census` row in `ansible/tests/repo/test_census_rows_roles.py`
  reads every task file under `ansible/roles/k8s/*/tasks/`. Its offence is a task that writes
  a host, wherever it is delegated:
    - `ansible.builtin.cron` or `ansible.posix.authorized_key`;
    - a `copy`, `template`, `file`, `lineinfile`, `blockinfile`, `get_url`, `unarchive`,
      `systemd` or `service` module whose destination lies outside
      `/etc/rancher/k3s/manifests/`;
    - a `command` or `shell` task whose command writes a file: `install`, `tee`, `cp`, `mv`,
      `mkdir`, `ln`, or an output redirect.

  A `delegate_to` alone is no offence, so the read-only tasks in `release_digest.yml` and in
  volume-snapshot's "Resolve the deploy tag for …" pass.
    - The census exempts every role directory that has no `containers_list` entry. It derives
      that set by comparing the two, rather than from a hand list. At the baseline it is the
      seven utility roles, so `release_stamp.yml`'s copy is exempt, and R4 deletes it with its
      role.
    - Its `allow` map holds each task file that still writes a host, with the PR that moves
      it as the reason. That covers the twelve roles of the baseline's host-write list and
      the imperative-ConfigMap staging of autofix-bridge and monitor-bridge. The offence has no exception for `state:
      absent` or `state: directory`, so an absent arm and a directory task are host writes
      too.
        - observability: H2.7 moves the heartbeat and its cron, and D5's run-once cleanup PR
          deletes the claude-otel record cleanup (*Host-side rows*). Whichever lands second
          drops the entry.
        - game-stats' `tasks/main.yml`: D5's run-once cleanup PR, which deletes the pre-#2813
          record cleanup and drops the entry.
        - crowdsec, configarr, janitorr, qbittorrent and artifacts: D5's `k8s_host_files`
          conversion PR for the role (*Host-side rows*). H2's family PR moves the live crons
          and scripts and leaves absent arms, and artifacts keeps its three directory tasks, so
          the entry survives H2 and only D5's PR drops it.
        - registry: D5's GC retirement, whose absent arms then move with the rest.
        - docs, pi-peer-backup and media-volume: D5's `k8s_host_files` conversion PR.
        - traefik: D5's `k8s_host_files` conversion PR moves the Cloudflare drift absent arm
          and narrows the reason to the RBAC file. traefik's flip PR renders `rbac.yaml.j2`
          into the bundle, deletes the staging and apply tasks, and drops the entry.
        - autofix-bridge, monitor-bridge, game-stats' three staging files and observability's
          `tasks/dashboards.yml`: the role's Wave D PR that renders the imperative ConfigMap.
    - `must_find` names pi-peer-backup's and docs's task files, and artifacts'
      `tasks/main.yml` for its three directory tasks, which no H2 PR moves. Only D5's PRs move
      these three, so the row fails when one of them stops matching rather than when the
      selector drifts (failure class 4). Each D5 conversion PR that moves a member drops it
      from `must_find` in the same commit.
    - Red fixtures: an unlisted role with `ansible.builtin.cron`, an unlisted role with an
      `ansible.builtin.cron` at `state: absent`, and an unlisted role whose `shell` task runs
      `install -m 0755` into `/usr/local/bin`.
    - A flip PR cannot pass while its role keeps an `allow` entry.
- **No host script writes a flux-owned object.** A second `Census` row reads the templates and
  files that `k8s_host_files` installs. It fails on a `kubectl` call with a write verb
  (`scale`, `patch`, `apply`, `create`, `delete`, `annotate`, `label`, `replace`, `set` or
  `rollout restart`). A host script that writes an object Flux renders is a second writer
  under C4. `kubectl exec` into an app writes no object and passes. The red fixture is a copy
  of `registry-gc.sh.j2`'s `scale` call, held in the test, because the template itself goes
  with the GC. That call is why D5 retires the GC rather than moving it, to the host or into
  the cluster (Wave D's registry bullet, Open question 15).
- **Concurrency.** kustomize-controller's `--concurrent` equals `k8s_rollout_batch_width`,
  patched over the vendored components in the flux role. This keeps the rollout concurrency
  the play has today.
    - A Kustomization with `wait: true` holds a worker until its health check settles. A
      snapshot Job holds one while it waits for `readyToUse`. `soak` holds one for its whole
      wait, but it starts only after every tag is Ready, so it competes with no tag of its own
      revision.
    - D4 derives the verify timeout from the longest `dependsOn` path. D5 needs that sum to
      include the `<tag>-snapshot` and `soak` timeouts on the path, plus a queueing term:
      the number of changed Kustomizations divided by `--concurrent`, rounded up, times the
      largest changed timeout. This is a cross-slice change to D4.

### Restart annotations for self-rolled workloads (D5.1e)

D3 stamps `checksum/restart` on the pod template of each restart target the shared role
names: the `manifests_rollout` workload when it is non-empty, and every
`manifests_extra_rollouts` entry, whatever `manifests_rollout` holds. That covers
cloudflare-ddns's two Deployments (D3, *Restart triggers*). D3 leaves every
`manifests_self_rollouts` entry bare, and its interim census refuses `owner: flux` for a role
that passes one. pihole and observability are those roles. Under Flux, nothing would restart
their workloads on a config or Secret change without D5.1e: Alertmanager would keep its old
routes, receivers and mutes after every M2 to M6 edit, Grafana and Prometheus would keep a
rotated credential, and pihole would keep its old dnsmasq and unbound config. The soak would
pass each time, because no pod is created after T0.

- **D5.1e widens D3's target set, and adds no annotation of its own.** For a flux-owned tag,
  the renderer also stamps `checksum/restart` on the pod template of every
  `manifests_self_rollouts` entry. The named members are observability's six workloads plus
  M1's Alertmanager, and pihole's two instances. Both roles move in Wave D, so the annotation
  first appears in their own flip and adds no roll of its own.
- **The value is D3's, unchanged.** It is the SHA-256 of the ConfigMaps and Secrets that the
  workload's own pod template names, each in canonical form, plus the `.source` hash of every
  key those Secrets reference. Secrets are hashed in their placeholder form, so the value
  exposes no plaintext, which removes observability's reason for hashing no Secret. An
  Alertmanager edit therefore moves Alertmanager's annotation and no other, which is M1
  PR 5's "one Secret, one restart".
- **pihole keeps one instance at a time.** Instance 1's annotation renders in `pihole`, and
  instance 2's in `pihole-deferred`, which D4 makes depend on `pihole` with `wait: true`. A
  shared ConfigMap edit moves both annotations, and instance 2 rolls only after instance 1 is
  Ready, as `roll_one.yml` ensures today.
- **Grafana's admin password** rides on the same annotation. A rotated `grafana_admin_password`
  rolls grafana, and an initContainer on the grafana pod runs `grafana cli admin
  reset-admin-password` from the substituted Secret before Grafana starts. That replaces both
  the restart task and "Sync the live Grafana admin password with SOPS". grafana is
  `Recreate` (`spec_shell('Recreate')` in `grafana.yaml.j2`), so no server runs beside the
  initContainer. observability's conversion PR checks on the pinned image that the CLI works
  against the database with no server running.
- **Tests in `scripts/renderer/tests/`.** An Alertmanager Secret edit moves alertmanager's
  annotation and no other workload's. A pihole ConfigMap edit moves both instance templates,
  instance 2's inside `pihole-deferred`. D3's tests already cover cloudflare-ddns and a YAML
  comment edit.
- **D5.1e replaces D3's interim census.** The same PR deletes the census that refuses
  `owner: flux` for a role with `manifests_self_rollouts`, and adds one that fails on a
  flux-owned tag whose include names a `manifests_self_rollouts` target without
  `checksum/restart`. Its red fixture is pihole-2's entry with the annotation removed, and
  its named members are pihole and observability.
- **No bare restart target stays.** Of the ten roles with `manifests_rollout: ''`, only
  cloudflare-ddns, pihole and observability run a long-lived pod that reads a ConfigMap or
  Secret (Baseline). D3's rule covers the first, and D5.1e the other two. D5.1e lands before
  the first Wave A PR, and D3's interim census refuses the other two until then.

### Removal gates (D5.1f)

Two removals look like ordinary edits and delete live objects. Both checks run in D4's
`cross_plane_order.py --base <sha>`, which renders the cluster at the PR's base and head. A
cross-slice note tells D4 that D5.1d and D5.1f extend it.

- **A dropped object.** D2's *Risks* expected D5's soak to notice a Deployment that prune
  deleted. The soak cannot: a dropped template is absent from the render, so no rendered list
  names it, and a pruned workload creates no pod. Prune deletes a NetworkPolicy, an
  IngressRoute, a CronJob, a Service or an RBAC object just as silently. The check compares
  revisions instead. It fails a PR that removes an object of any kind from a flux-owned
  tag's render, unless one of the PR's commits carries a trailer for each removed object.
  The trailer reads `Removes-Object: <kind>/<namespace>/<name>`. A cluster-scoped object
  leaves the namespace empty, as in `Removes-Object: ClusterRole//<name>`, the grammar D2
  shares.
    - **What it exempts.** A rename is not a removal. An object whose kind and name, with
      the trailing digest that the renderer appends removed, still render in the head
      passes, such as a Job named by its spec digest or a ConfigMap named by its script
      digest. An object labelled `kustomize.toolkit.fluxcd.io/prune: disabled` passes too,
      because prune never deletes it.
    - **Fixtures.** Red: a base with a Deployment and a NetworkPolicy in a tag and a head
      without the NetworkPolicy, with no trailer; and a head that drops a ClusterRole with
      no trailer. Green: the same pairs with their trailers, and a head that renames a
      snapshot Job because the role's digest moved.
    - **D2 checks the same trailer at promote time.** D2.6c's `decide` reads it over the
      commits between `prod` and T, because only `decide` knows which `prod` T replaces. An
      automerged bump that drops an object is then covered too. A squash merge need not
      keep a PR commit's trailer, so a cross-slice note asks D2 where `decide` reads it. A
      second note asks D2 to point its *Risks* sentence at this check.
    - **A handback is exempt.** A PR that removes a tag's `owner: flux`, such as the revert
      of a flip in *Rollback*, removes the tag's whole Flux render, and `deletionPolicy:
      Orphan` deletes nothing. The arm skips such a tag, so the emergency path never needs
      trailers. A green fixture reverts a flip and passes.
    - **The first of D2's two retirement PRs** empties the templates, so it carries a trailer
      for each object it removes.
    - **It runs on pull requests only.** It reads the trailers from the PR's own commits. A
      squash merge need not copy them into the master commit, so on a push to master the
      arm is skipped, and the PR has already passed it.
- **A retirement of a flux-owned service.** `ansible/tasks/k8s_retire.yml` runs `kubectl
  delete` on the staged directory. For a flux-owned role that directory is the last render
  Ansible staged before the flip, and Flux at the old `prod` keeps applying the tag's
  Kustomization until promotion, so it re-creates what Ansible deleted. The check fails a PR
  that adds a name to `k8s_retired_services` whose entry carries `owner: flux` on the PR's
  base. Such a service retires by D2's two PRs: the first empties its templates, so Flux
  prunes its objects, and the second removes its entry. The red fixture is a flux-owned entry
  moved into `k8s_retired_services` in one PR.
    - Prune never deletes the claim, which carries `prune: disabled`. Deleting a retired
      service's claim, and with it the Longhorn volume, is an **operator-only** hand step
      after the second PR, named in `docs/flux-deploy-plane.md`. A cross-slice note asks D2 to
      state it.

### What a takeover strips (D5.1g)

Flux's first apply of a role runs kustomize-controller's cleanup on every live object it
renders (Baseline). The fields owned by a cleaned manager move to kustomize-controller, and
the same apply then drops each one the render omits. A server-side dry run never runs that
cleanup, and it leaves a field that another manager owns in place when the applied config
omits it. So no dry-run diff shows the strip. speedtest is the case: its `kubectl-rollout`
entry owns `restartedAt`, the takeover drops it, and the pod rolls (Goal), while a dry-run
diff shows nothing.

- **The census.** A read-only subcommand, `probe.py takeover <tag>`, renders the tag's
  directory and reads each live object it names with `kubectl get -o json
  --show-managed-fields`, under the agent's read-only ServiceAccount. For each object it
  prints the field paths that a cleaned manager owns and the rendered object does not
  declare. It prints paths only, because `fieldsV1` names fields and never values.
    - The cleaned managers are the ones the Baseline quotes from kustomize-controller
      v1.9.6: any name with the prefix `kubectl`, `before-first-apply`, and the
      controller's own Update entry. An entry with a subresource is skipped, as
      `matchFieldManager` skips it. A test pins the list to those names.
    - A placeholder sits at the same path as the value it stands for, so the placeholder
      render answers whether the render declares a path.
    - Secrets are left out. The read-only ServiceAccount cannot read them, and D3.5 hands
      their fields to kustomize-controller first.
    - The annotation `kubectl.kubernetes.io/last-applied-configuration` is left out too.
      `kubectl-client-side-apply` owns it on every object and no render carries it, so the
      cleanup always removes it, and exit 2 checks that it is gone.
- **Tests in `scripts/diagnostics/tests/`.** A fixture Deployment, copied from the Baseline's
  live read of speedtest, whose `kubectl-rollout` entry owns
  `spec.template.metadata.annotations.kubectl.kubernetes.io/restartedAt` prints that path.
  That is the red half, and speedtest is its named member. A field the render declares
  prints nothing. A field owned by `k3s`, and one owned by a `kubectl` entry with
  `subresource: scale`, print nothing.
- **What the census cannot see is the operator's.** An apply can also fail on an immutable
  field, such as a PVC's spec or a selector, or change a value. For those the operator
  renders the flip PR's head, where the entry carries `owner: flux`, because no bundle holds
  `out/flux/<tag>.yaml` for an Ansible-owned tag. Then the operator runs
  `flux diff kustomization <tag> --path out/cluster/<tag> --kustomization-file
  out/flux/<tag>.yaml` with the flux CLI at D1's `flux_version`, on a machine with
  cluster-admin access, because daniel-box has no flux CLI (D2 baseline). Without
  `--dry-run`, the CLI substitutes from the Secrets the Kustomization's `substituteFrom`
  names, so the diff compares real values rather than `${…}` placeholders. It reads
  `homelab-cluster-vars`, so it is **operator-only**.

### The `k8s_host_files` role enters the setup plane once (D5.1h)

The PR that creates the role must add its entry to `ansible/initial_setup.yml`, and that path
parks the tick (Baseline). So D5.1h creates the role alone, before any row moves into it.

- **What it holds.** `ansible/roles/setup/k8s_host_files/` with a `tasks/main.yml` that
  includes no task file yet, its `CLAUDE.md` with the Autonomous-role contract, the
  `StrictUndefined` render test in `ansible/tests/setup/` with its red fixture (*Host-side
  rows*), and the `initial_setup.yml` entry, gated to daniel-box. Nothing else rides in the
  PR.
- **Before the merge.** `probe.py gitops-state` shows `behind_since` and `hold_sha` empty, and
  `probe.py landing` shows no other landing in flight, so the range the park holds is this PR
  alone.
- **While it is parked.** The tick logs a park line naming `initial_setup.yml` on every tick,
  writes `behind_since` and pages once. `land.sh` prints the bring-up playbook's remediation
  rather than `settled`, and the PR body quotes its `VERDICT:` line. Another session's
  landing meanwhile reads the tick as not yet across origin and exits 75, as `land.sh --help`
  lists, and root `CLAUDE.md`'s *When to wait* tells that session to name D5.1h and stop.
- **The clear, operator-only.** The playbook decrypts its become password from SOPS
  (Baseline). The operator works in the primary checkout on daniel-box, in the order
  `docs/gitops-pipeline.md`'s *When a tick parks* gives:
    1. `git log --oneline <local HEAD>..origin/master` lists D5.1h's merge commit. When a
       later commit follows it, the next step fast-forwards to D5.1h's merge commit only,
       so the tick still applies the later ones. When an earlier commit precedes it, the
       fast-forward skips that commit's tick-owned half. The operator applies that half by
       hand afterwards, its setup roles through `initial_setup.yml --tags <role>` and
       anything else through `./scripts/deploy.sh --tags <service>`, and names each apply
       in the PR thread.
    1. `git merge --ff-only <D5.1h's merge commit>`.
    1. `uv run ansible-playbook ansible/initial_setup.yml --tags k8s_host_files >
       /tmp/k8s-host-files.log 2>&1 < /dev/null`. The redirect is what a shell with
       non-blocking stdio needs (the MEMORY note on non-blocking stdio). The role has no task
       yet, so the run changes nothing on the host.

  The next tick finds no bring-up change in its range, so the park clears and
  `behind_since` empties once the tick reaches origin.
- **From then on the tick applies the role.** Every later PR under the role routes as
  `setup_roles={'k8s_host_files'}` (Baseline), so the tick runs `initial_setup.yml --tags
  k8s_host_files` and `land.sh` follows it. D5.1h lands before the first PR that moves a row
  into the role, which is Wave C's pi-peer-backup PR.

### Rules for every Job Flux applies (D5.1a3)

D5 puts Jobs into the bundle: the snapshot Jobs, the soak Job, the verify Jobs and Wave D's
conversion Jobs. Two Kubernetes behaviours make a careless Job re-run or wedge:

- **A TTL re-runs it.** A Job with `ttlSecondsAfterFinished` is deleted after the TTL, and
  Flux then re-creates it, so the snapshot or the migration runs again. Every rendered Job in
  the baseline sets a TTL, most of them 86400 seconds, so that is the pattern a Job would
  copy. No Job
  that Flux applies sets a TTL. Prune deletes a Job when its name leaves the bundle.
- **A changed spec under one name fails the apply.** A Job's pod template is immutable. Every
  Job's name therefore ends in a digest of its own spec, so any spec change yields another
  name. A Job that must run once per role revision embeds the role's `digest8` in its spec, so
  a role change renames it too.

- **A reset or a revert re-runs it.** Prune deletes a Job when the role moves on, and a reset to
  `last-good` brings the older Job back, so Flux runs it again. Every Job must therefore be
  safe to run twice. The snapshot Job takes a fresh token. A mutation Job must leave the same
  state on its second run.

**What every Job carries.** The scripts use only the Python standard library. They reach the
API server with the pod's ServiceAccount token, and Prometheus or an app over HTTP.

- **Image.** One pin, `flux_job_image`, set to the `python:3.14-alpine` digest that
  `monitor_bridge_k8s_image` already pins. Renovate's existing digest rule tracks it.
- **Script delivery.** A ConfigMap in the same Kustomization, whose name ends in the digest of
  the script. The Job's spec names the ConfigMap, so the Job's spec digest covers the script,
  and a script fix renames the Job.
- **Namespace.** Snapshot and soak Jobs run in `flux-system`. Verify and mutation Jobs run in
  their tag's namespace, because they probe as callers from that namespace.
- **A fence admits every Job that dials through it.** Today's verifies run as the node or
  through root `kubectl exec` (Baseline), and a Job pod is neither. Each Job's conversion PR
  therefore carries a caller census: every Service the Job dials, with the fence that guards
  it, and every pod it would exec into.
    - Each peer lands before the flip, in netpol-baseline or in the role-owned fence (for
      sonarr, `sonarr-verify` joins `netpol_from`). Each new peer gets a row in
      `netpol_baseline_probe_targets`, so a probe leg refused from an unlisted pod shows the
      fence can go red.
    - A Job that copies an existing pod template keeps that template's `app:` label, and
      with it the fence's existing admission. configarr's gate Job is the case.
    - An exec check becomes a network read wherever the app exposes one, or moves into the
      target's own pod as an initContainer, as authelia's TOTP seed does.
    - Where an exec remains, the Job's Role grants `create` on `pods/exec` in its namespace.
      RBAC cannot narrow that grant to one pod, because a Deployment's pod names are
      generated, so it covers the whole namespace. The conversion PR states it as a trade,
      and the operator reviews it.
- **Post-apply mutation Jobs share `<tag>-verify`.** A mutation that needs the app serving,
  such as arr-notification's seed or configarr's gate run, runs as a Job in `<tag>-verify`,
  beside the verify Jobs, because that Kustomization starts only after the tag is applied and
  Ready. It carries `homelab/rerun-safe`. A `-verify` Kustomization whose directory holds a
  placeholder gets `substituteFrom` and the `cluster-vars` edge, by D3's per-tag rule applied
  to the kind.
- **Pod spec.** `restartPolicy: Never`, `activeDeadlineSeconds` below its Kustomization's
  `timeout`, and a non-root securityContext that PSA `restricted` admits. No priority class:
  `ansible/tests/k8s/test_pod_template_hygiene.py` leaves Jobs out of its scope on purpose.
- **`backoffLimit`.** 1 for a snapshot Job, so its one retry takes a new token. 0 for soak and
  verify Jobs, so a flaky check fails rather than passing on a retry. A mutation Job states its
  own value, with its reason.
- **Names.** Every Job's name ends in its spec digest, the soak Job included: it is
  `soak-<spec digest8>`, and its spec embeds R's SHA.
- **A first connection retries.** A brand-new pod admitted by a selector peer can be refused
  until kube-router learns its address (the MEMORY note on kube-router's source-pod lag,
  enforced only for the netpol probe's control leg by
  `ansible/tests/k8s/test_netpol_probe_race.py`). Every Job that reads a fenced Service opens
  its first connection through one shared helper, which retries a refused or timed-out
  connection for 60 seconds. The helper tells a refusal apart from an empty answer, and never
  retries the second.
- **A mutation Job is safe to re-run.** Its template carries the annotation
  `homelab/rerun-safe`, naming the test that runs it twice against a fixture and finds the
  second run changes nothing.

A `Census` row in `scripts/renderer/tests/` fails on a rendered Job that sets
`ttlSecondsAfterFinished`, whose name does not end in its spec digest, that lacks
`backoffLimit`, or that is a mutation Job without `homelab/rerun-safe`. A second row fails on
a Job script that opens a connection without the shared helper. Their red fixtures are one Job
of each kind, and a script that calls `urllib.request.urlopen` directly. **Its scope** is the
Jobs in the render of a flux-owned tag and in the non-tag Kustomizations. An Ansible-owned
role's Jobs are conversion rows, listed in the baseline, and each moves in its role's PR to the
`<tag>-verify` class below. registry-gc's manifest leaves with its cron (Wave D's registry
bullet).

### Snapshots as a Job per role revision (D5.2a)

- **A Kustomization per snapshot role.** For each flux-owned tag with
  `k8s_autodeploy_snapshot_pvcs`, the renderer emits a non-tag Kustomization,
  `<tag>-snapshot`. It holds one Job, `snapshot-<tag>-<spec digest8>`. The spec carries the
  role's `digest8` and its claims, so the Job is renamed whenever the role's content changes.
  The tag's Kustomization depends on `<tag>-snapshot`, which has `wait: true`. The role
  therefore applies only after every claim's snapshot is `readyToUse`.
- **The snapshot waits for the role's own upstreams.** `<tag>-snapshot` inherits `<tag>`'s
  `dependsOn`, less itself. Without the edges, `sonarr-snapshot` would run as soon as the
  revision arrived, while sonarr applies only after authelia and the rest of D4's chain, and
  a revert would discard everything sonarr wrote in between. The edges keep the snapshot next
  to the apply, as `volume-snapshot` takes it "immediately before the deploy". They also keep
  a snapshot Job from holding a worker ahead of the chain. A test in
  `scripts/renderer/tests/` renders a fixture snapshot tag with one upstream edge and finds
  that edge on both `<tag>-snapshot` and `<tag>`. A suspended upstream's edge is dropped from
  both, as D5.1b drops it everywhere.
- **The snapshot name keeps `volume-snapshot`'s shape.** Per claim, the Job names the snapshot
  `autodeploy-<tag>-<digest8>-<claim>-<token>`. `<claim>` drops a leading `<tag>-`, by the
  rule `claim.yml` and `volume-revert` use. `<token>` is the UTC time at which the Job's pod
  starts, in `volume-snapshot`'s `%Y%m%d%H%M%S` format.
    - The shape is no longer than the Ansible-era name, whose longest form is 56 bytes. The Job
      still asserts the 63-byte bound before it creates anything.
    - `volume-revert` reads these snapshots unchanged, with `volume_revert_sha=<digest8>`,
      because its prefix and its `^[0-9a-f]{8,}$` check already accept them.
    - A role handed back to Ansible keeps one retention window, because the prune selects on
      the same `autodeploy-<tag>-` prefix.
- **A returning digest gets a fresh snapshot.** A reset or a revert can bring a role back to a
  digest it had before. Prune deleted that digest's Job when the role moved on, so Flux
  creates the Job again, and its pod takes a fresh token. Without the token the name would
  collide with a retained snapshot or with a `markRemoved` CR.
- **What the Job does, per claim.** It keeps every check `claim.yml` makes today:
    - resolve the claim's Longhorn volume from the PVC. A missing PVC fails the Job when
      `<tag>`'s `status.inventory` lists that PVC, because then a claim that held data is
      gone. A PVC that is absent both live and from the inventory is new in R: `<tag>` has
      never applied it, so it holds no data to protect. The Job skips that claim and logs
      the skip with the claim's name. The tag's first apply, when `<tag>` has no
      `status.inventory` at all, is the case where every claim is new.
        - **Why the new claim needs the skip.** The manifests role's contract has a role
          that snapshots apply its claims before the snapshot. Under Flux the claim comes
          only from `<tag>`, which depends on `<tag>-snapshot`. A PR that adds a claim to
          both `k8s_claims` and `k8s_autodeploy_snapshot_pvcs` of a flux-owned role would
          otherwise produce a snapshot Job that never passes. The role changed, so D5.2b1's
          stateful hold would fire without a reset, and every later revision would hold
          until a revert;
    - read the volume's `spec.snapshotMaxSize` and the snapshot space in use, and fail, naming
      the cap, the space in use and the headroom, only when the numbers say the cap is full.
      The read fails open, as `claim.yml`'s `DECIDED` marker on this gate explains: an empty
      or `"0"` cap reads as uncapped. The Job script carries that marker with its reasoning.
      A reached cap latches, because Longhorn refuses new snapshots rather than pruning, so
      without this check every later revision of that role would hold on a bare timeout;
    - create the snapshot. On `AlreadyExists` it exits 1 naming the CR and never adopts it,
      because an existing CR is either another run's recovery point or a `markRemoved` one.
      The pod's retry starts one second later or more, so it takes another token;
    - wait for `readyToUse`;
    - list the claim's snapshots and fail when the listing lacks the one it just created, as
      "Check that the snapshot listing found this run's snapshot for …" does, because that
      listing is the input to a delete;
    - prune the `autodeploy-<tag>-` snapshots of that claim down to `volume_snapshot_retain`,
      floor 2, never counting a `markRemoved` CR, and deleting with `--wait=false`.

  Its ServiceAccount gets a Role in `longhorn-system` that creates, lists and deletes
  Snapshots and reads Volumes, and read access to PVCs, PVs and the Kustomizations in
  `flux-system`.

### The stateful hold, its post, and the reset input (D5.2b1, D5.2b2)

- **The rollback rule changes.** Today the deployer reverts the volumes and then redeploys the
  prior commit. In CI, `promote` cannot reach Longhorn. Resetting `prod` would apply the old
  image onto data the failed image may have migrated, the very state `volume-snapshot` exists
  to prevent. So when a failed revision's changed tags, read from the `own` digests in
  `digests.json` against `last-good`'s, include a snapshot role, `promote` writes
  `DEPLOY_HOLD` and does **not** reset `prod`. For every other failure, C2's hold-then-reset
  stands.
- **The hold's value is D5's own.** The stateful hold writes `<T> stateful=<names>`, where
  `<names>` are the expected names that failed or timed out. It never takes C2's `failed=`
  form, so C2's proposed revert rule cannot clear it while a volume still holds data R
  wrote. Only the operator clears it, at the end of the revert runbook's branch.
- **The trigger is a changed snapshot role, not a failed one.** `promote`'s concurrency group
  cancels pending runs, so one revision often spans several merges, such as a Renovate bump of
  an eligible snapshot role and a hand change to traefik. If traefik fails, the revision still
  holds without a reset, and the broken edge stays live until the operator acts. The breadth is
  deliberate: a reset would roll the snapshot role, which applied R successfully, back onto data
  R may have migrated. The runbook's second branch covers this case, and Open question 1 asks
  the operator to accept it.
- **The hold posts itself.** After it writes `DEPLOY_HOLD`, `promote.py hold` posts to the
  deployer's Discord channel, the channel the deployer's own holds post to. The message names:
    - the held SHA;
    - every changed tag, and every Kustomization that failed or timed out, each marked as a
      snapshot role or not, so the operator can pick the runbook's branch;
    - when `soak` failed, the soak Job's name, `soak-<spec digest8>`, which the render fixes,
      and the read-only command that prints the services it named (D5.3);
    - for each changed snapshot role, its `digest8`, the snapshot prefix of each claim, and
      a command block in Branch 1's order: the read-only classify command, its two suspends,
      its `volume-revert.yml` run with its own `digest8` and claims, and its two resumes.
      Beside the classify command the post prints the exact string a role that attempted R
      reads there, `prod@sha256:<digest>`. The digest is the manifest digest of the
      `sha-<R>` tag that `publish` pushed. `promote` reads it from GHCR, and `prod` carries
      the same digest, because `promote` moves `prod` with `flux tag artifact` (D1).
      `promote` runs in CI and cannot read the cluster, so the post prints a block for every
      changed snapshot role and leaves the choice of which to revert to the classify step;
    - whether `prod` was reset, and the runbook in `docs/volume-snapshot-drills.md`.

  A failed post fails the `promote` run, so GitHub shows a red run. The webhook is a repository
  secret, `DEPLOY_DISCORD_WEBHOOK`, holding the value of `gitops_deploy_discord_webhook`.
  Adding it is **operator-only**, because it decrypts a SOPS value and changes GitHub
  settings. The rotation registry entry for `gitops_deploy_discord_webhook` notes the copy.
- **A reset input.** `promote.yml` gains a `workflow_dispatch` input, `reset_to_last_good`. It
  moves `prod` to `last-good` and does nothing else. Like D1's inputs, it runs only from
  `refs/heads/master`. The failure path already performs this reset. The input exposes it to
  the revert runbook and to the flux PR's broken-sync-object runbook.
    - **Decide, then write.** The input follows C2's split. A read-only job checks the hold,
      runs the in-flight check below, and names `prod`'s and `last-good`'s digests. A second
      job of the same shape as D2's `settle` job, with no checkout and `packages: write`,
      moves the tag. Both run in `promote-reset`, so the write never waits in the group
      `promote`. The `promote` and `verify` jobs stay read-only, and any other tag move D5
      adds follows the same split.
    - **It runs in its own concurrency group, `promote-reset`.** In the group `promote`, a
      reset dispatched while a later run's `decide` waits on a verification in flight would
      wait up to that wait's end, as much as 6 hours (D4 *The cap*), and the next merge's run
      would cancel it while it was pending (Baseline). D2.5's `verify` and `settle` join no
      group, so `promote-reset` serialises the reset against nothing of D2's. The in-flight
      check below replaces the serialisation that a shared group would have given.
    - **The hazard C2's two checks leave open.** C2 *A verification in flight blocks the
      next promotion* guards a verification with two checks rather than a group: `decide`
      waits while `prod` differs from `last-good`, and `tag` moves `prod` only while `prod`
      still names the digest `decide` read. A reset dispatched while another run verifies
      would move `prod` to `last-good`. That releases `decide`'s wait, and the in-flight
      run's `settle` still acts on its own verdict. On a failure it writes its own hold over
      the operator's reason. On a `good` verdict from statuses Flux posted before the reset,
      it moves `last-good` to a T that `prod` no longer names (#5079).
    - **It refuses while a verification is in flight.** The read job reads `prod`'s digest
      and `last-good`'s, by the same two HEADs `decide` polls. When they are equal, the reset
      prints that `prod` already names `last-good`, moves nothing and succeeds. When they
      differ, the reset proceeds only if `DEPLOY_HOLD`'s first word is a 40-hex SHA X and the
      manifest digest of `sha-<X>` in GHCR equals `prod`'s digest. The read job compares the
      two digests rather than trusting the SHA string. A hold of that shape means the run
      that moved `prod` to X has settled and left `prod` in place: the stateful hold
      `<T> stateful=<names>`, H1's forward-only hold `<T>`, or C2's `<T> failed=<names>` and
      `<T> unverified` after `settle`'s own reset failed. Any other difference means a run
      is between its `tag` and its `settle`. The reset then exits non-zero and names that
      run's commit and the in-progress `promote` run, read with `gh run list --workflow
      promote.yml --status in_progress`. The message tells the operator to wait for that
      run's `settle` or to cancel the run (below), to read `last-good` again, and then to
      dispatch again. A `good` verdict moves `last-good` to that commit, and a failure
      resets `prod` itself. Either way the second dispatch finds `prod` equal to `last-good`
      and moves nothing.
    - **Why it refuses rather than waits.** A refusal leaves the next step to the operator
      once `last-good` is final. A reset that waited in its job would reset to whatever
      `last-good` became, which may be the very revision the operator meant to leave. It
      would also need a timeout sized to `decide`'s in-flight wait. The cost is that the
      in-flight revision stays live until its run settles.
    - **Cancelling the run bounds that cost.** A broken Flux posts no `flux/<name>`
      statuses, so in the runbooks for a broken controller upgrade and a broken sync object
      the in-flight `verify` runs to its derived timeout. That is 13 minutes at D2.5 and up
      to hours under D4 *The cap*, with the broken revision live throughout. The operator
      need not wait it out. `gh run cancel <run-id> -R DanielH2018/server` cancels the run
      the refusal names (**operator-only**). `settle`'s condition is `always()` and the
      `tag` job's `moved` output, so it still runs after a cancelled `verify`. It writes
      `<T> unverified`, a form the revert rule never passes, and resets `prod` to
      `last-good` (C2 *On failure*). The second dispatch then finds `prod` on `last-good`
      and moves nothing.
    - **Why not have `settle` re-read `prod` before it writes.** That changes D2's `settle`,
      and the re-read and the write still race the reset, because no group serialises the
      two without exposing a pending `settle` to eviction. A `settle` that skipped its hold
      on a moved `prod` would also lose the record of T's failure. The refusal keeps D2
      unchanged and holds both checks true.
    - **Which holds the refusal passes is not a policy on what may be reset.** The check
      answers one question: has `prod`'s run settled? Whether a reset across a snapshot role
      or a forward-only tag is allowed is each runbook's rule. Branch 1 allows it for the
      roles it reverts. The cold-start step 2 below allows it only with a recorded
      compatibility judgment.
    - **The write moves `prod` only while it still names the digest the read job read**,
      the same compare-and-move that `tag` performs. It moves `prod` to the `last-good`
      digest that the read job read, then records it in `PROD_DIGEST`. A mismatch moves
      nothing. When the live digest is already `last-good`'s, as after `settle`'s own reset
      lands first, the write succeeds and says so. Any other mismatch exits non-zero naming
      both digests. The write runs only in the refusal's
      pass case, where `prod` differs from `last-good`. In that state every `decide` either
      returns `held` or sits in its in-flight wait, so no `tag` runs, and the two
      compare-and-moves cannot interleave in different groups. One case escapes that
      argument. Under C2's revert rule, a `<T> failed=<names>` hold lets `decide` wait
      rather than return `held`. If `settle`'s own reset of `prod` lands between the
      write's HEAD and its move, then a `decide` poll and a `tag` must also fit inside that
      gap of a second or two. The write then puts `prod` back on `last-good`, and the
      revert candidate U's `verify` reaches `promote.py`'s derived timeout, because Flux
      posts no statuses for U. That prints `failed`, so U's `settle` writes
      `<U> failed=<names>` over T's hold. `prod` stays on `last-good` and a hold stays set,
      but the hold now names U, in the form the revert rule passes. T's failure survives
      only in its run's log. A candidate the rule passes against U's hold descends from U,
      which already reverted T's failed names. The cost of this residual is the lost
      record of T, not a moved `prod`.
    - **It ignores the reboot window.** A reset moves `prod` back to a revision the cluster
      already ran, and the operator dispatches it on purpose.
    - **It refuses unless a hold is set**, and exits non-zero naming the reason. It reads
      `DEPLOY_HOLD` by D1's rule: `none` is no hold, an empty value or a missing variable is
      an error, and any other value is a hold. So it refuses on `none`, on an empty value, on
      a missing variable and on a failed read. Without a hold the next promote run would move
      `prod` forward again. Every runbook that dispatches it sets the hold first: the failure
      path writes it in Branch 1, and the broken-sync-object runbook sets it by hand.
    - **Setting and clearing the hold by hand.** Both are **operator-only**. To set it, run
      `gh variable set DEPLOY_HOLD --body <reason> -R DanielH2018/server` with a reason that
      is neither `none` nor empty. To clear it, run
      `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`, D1's no-hold value (D1
      Open question 9). Each set and clear step in this plan's runbooks names one of the two.
    - **Why a separate group is safe.** A promote run reads `DEPLOY_HOLD` on every poll of
      `decide`'s in-flight wait, and no other wait separates that read from the move of
      `prod` (below). So no promote run moves `prod` after a reset that ran under a hold,
      except under the revert rule's own pass. No run is verifying when the reset moves
      `prod`, because the reset refuses while one is. The reset therefore never leaves
      `prod` behind `last-good`.
    - **A hand-set hold does not stop a run that already decided.** A `decide` that read
      `none` just before the operator set the hold can still reach `tag`. The reset then
      finds `prod` ahead of `last-good` with a hold that does not name `prod`'s commit, and
      it refuses. That run's `settle` writes `<T> failed=<names>` or `<T> unverified` over
      the operator's reason when T fails. The value is still a hold, and the failure path
      has reset `prod`. This is D2's behaviour for any hand-set hold, not the reset's.
- **No promotion runs into the weekly reboot.** The reboot kills the soak Job's pod, recreates
  pods after T0, and fires critical rules whose `activeAt` follows T0. M2's `weekly-reboot`
  mute is an Alertmanager route, so it silences nothing in `ALERTS`. A promotion verifying
  across the reboot would therefore hold, and clearing a hold is operator-only.
    - **`promote.py decide` derives a no-promotion window for each target T.** The window
      ends when M2's mute ends. It starts one T's derived verify timeout before the reboot,
      so a promotion that starts just before the window still finishes verifying before the
      reboot. The inputs are the reboot cron's `weekly_reboot_minute`,
      `weekly_reboot_hour`, `weekly_reboot_weekday`,
      `weekly_reboot_shutdown_delay_minutes` and `weekly_reboot_timezone`, M2's
      `weekly_reboot_mute_lead_minutes` and `weekly_reboot_mute_recovery_minutes`, and T's
      timeout.
    - **Which timeout `decide` uses.** It is T's own derived verify timeout, computed by the
      same function that `verify` calls: D4's chained timeout with D5's `soak` and
      `<tag>-snapshot` timeouts and the queueing term, capped at 5.5 hours. One function
      means `decide` and `verify` cannot disagree about it.
    - **Inside the window the run defers. It never waits in its job.** D4's 5.5-hour cap
      counts from the start of the `promote` job, so a wait before `verify` would come out of
      `verify`'s budget. A broad revision merged early in the window would wait two to three
      hours and leave `verify` less than its own derived timeout. `verify` would then time
      out and write a false hold, and clearing a hold is operator-only. So `decide` ends the
      run with the result `deferred` and moves nothing. `promote.yml` gains a `schedule:`
      trigger at the window's end, and that run promotes master's tip, as `promote` already
      picks its target from master. A commit that lands during the window is not lost, and
      no new commit is needed to promote afterwards. Every run that promotes reaches
      `verify` with no wait before it, so `verify` keeps its full derived budget.
    - **The schedule is derived, not typed.** GitHub reads a `schedule:` cron as a literal in
      the workflow file. A test derives the window's end from the variables above and fails
      when the workflow's cron differs, so moving the reboot without the cron fails CI.
    - **GitHub can start a scheduled run late, or skip it under load.** A late run promotes
      late. A skipped one leaves the window's commits for the next merge's run or for a
      `promote` dispatch by D2's procedure.
    - **One `DEPLOY_HOLD` read is enough.** `decide` reads the hold before it moves `prod`,
      and nothing waits between the two. A deferred run moves nothing, and the scheduled run
      reads the hold afresh.
    - `land-wait` reads the same window. For a merge inside it, its give-up deadline counts
      from the window's end, not from the merge, and it prints when the window ends. Otherwise
      a healthy merge at 06:30 would read `gave-up` before its promotion had started.
    - **The window's size.** The reboot runs at about 07:35 UTC on Sunday:
      `weekly_reboot_hour` 7 and `weekly_reboot_minute` 30 in
      `ansible/inventory/group_vars/all.yml`, plus the 5-minute
      `weekly_reboot_shutdown_delay_minutes`. D4's *The size* puts the full-map timeout at
      about 103 minutes of chain, and up to about 2.5 hours for a revision within
      `--concurrent`. For such a revision the window opens near 05:05, and D5's soak,
      snapshot and queueing terms open it earlier. A revision at the 5.5-hour cap defers
      from about 02:05. The window closes at about 08:15 in every case. That is the cost: a
      weekly pause in promotion of about three to six hours on Sunday morning, depending on
      the revision.
    - **Tests** in `scripts/deploy_tools/tests/`:
        - the window derives from those variables, and the test fails if the code reads a
          literal hour instead;
        - the workflow's `schedule:` cron equals the derived end. The red half is a fixture
          with the reboot hour moved and the cron unchanged;
        - a fixture run that enters early in T's window ends `deferred`, moves nothing, and
          never calls the fixture clock's sleep. The fixture scheduled run at the window's
          end then hands `verify` T's full derived timeout. The red half is a `decide` that
          waits in its job, whose `verify` budget is the 5.5-hour cap minus the wait;
        - the reset input moves `prod` inside the window under a hold such as `drill`.
          It refuses with `DEPLOY_HOLD` reading `none`, with an empty value, with the
          variable missing and with a failed read. The red half is a guard that refuses only
          on an empty value, which the `none` fixture passes;
        - the reset refuses while a verification is in flight. A fixture with `prod` on T,
          `last-good` behind it and a hand-set hold `drill` refuses and names T. The red
          half is a guard that checks only that a hold exists, which that fixture passes;
        - the reset proceeds on a settled hold. A fixture with `prod` on T and
          `DEPLOY_HOLD` reading `<T> stateful=<names>` moves `prod` to `last-good`, and the
          same fixture with a hold that names T while `sha-<T>`'s digest differs from
          `prod`'s refuses. The red half is a check that matches the SHA string without
          comparing digests, which the second fixture passes;
        - the reset moves nothing when `prod` already names `last-good`, and the write moves
          nothing when `prod` changed after the read job read it. The red half is a write
          that skips the compare, which moves the fixture's `prod` off the revision a
          fixture `tag` just moved it to.

### The revert runbook (D5.2c)

The runbook lives in `docs/volume-snapshot-drills.md`. Every step that writes to the cluster
is **operator-only**, because the agent's permissions deny `sudo` and every `kubectl` write.
The hold post lists every changed and failed tag, and the operator picks one of three
branches from it.

**Choosing the branch.** Two reads come first, both with the read-only `kubectl`, and both
before any reset, because a reset prunes R's soak Job with its pod:

- **Which services the soak named**, when `soak` failed. The soak Job writes them to its
  termination message, and the post prints the command: `kubectl -n flux-system get pods -l
  job-name=<soak Job> -o
  jsonpath='{.items[*].status.containerStatuses[0].state.terminated.message}'`.
- **Which changed snapshot roles attempted R.** For each one, the post's classify command
  reads `kubectl -n flux-system get kustomization <s> -o
  jsonpath='{.status.lastAttemptedRevision}'`. That field holds a tag and a digest, never a
  commit (Baseline), so the operator compares it with the string the post prints beside the
  command, `prod@sha256:<digest of sha-<R>>`. Equal means the role attempted R. Any other
  value means it did not. Comparing that field with R's SHA would never match, so every
  role would read as blocked, and a failed snapshot role would be sent to Branch 2. D4's
  Risks name that trap, a reading of the wrong revision field, as failure class 2.
    - The same step cross-checks the string. While the hold keeps `prod` at R,
      `kubectl -n flux-system get ocirepository homelab -o
      jsonpath='{.status.artifact.revision}'` prints that same value. No reset has run yet,
      because both reads come before any reset.
    - A role that an upstream failure blocked never reads R's string there (Baseline), so its
      data is unchanged. It took no R snapshot, so `volume-revert` would fail on it ("Fail
      when no snapshot matches this deploy").

Then the rule:

- **Branch 1** when a changed snapshot role that attempted R failed: its `<s>` or
  `<s>-verify` failed or timed out, or the soak named its service.
- **Branch 3** when a `<s>-snapshot` Job failed on a full cap.
- **Branch 2** otherwise.

**Branch 1: a snapshot role failed.** A reset moves `prod` for the whole bundle, so it rolls
back every changed snapshot role, not only the failed one. Each changed snapshot role that
attempted R is therefore reverted, from its own block in the post. Roles that did not attempt
R need no step, because the reset leaves them on the spec they already run. The steps run in
this order, and resuming comes last, because a resumed `<tag>` re-applies whatever `prod`
names.

1. **Suspend every role that will be reverted, before any revert.** For each, suspend
   `<s>-snapshot`, then `<s>`. Each is `sudo k3s kubectl -n flux-system patch kustomization
   <name> --type merge --field-manager=homelab-operator -p '{"spec":{"suspend":true}}'`. The
   field manager keeps each suspend in place through every reconcile of the Kustomization
   that applies the suspended object: the root, and from the flux PR on, `flux-deferred`
   (D5.1b). The root therefore needs no suspend of its own.
2. **Revert the volumes, one run per role.** Run each block's `uv run ansible-playbook
   ansible/volume-revert.yml -e volume_revert_service=<s> -e '{"volume_revert_claims":
   [<claims>]}' -e volume_revert_sha=<digest8>`, with that role's own `digest8` from the post.
   The playbook takes one `volume_revert_service`. The role leaves each Deployment at zero
   replicas.
3. **Reset `prod`.** Run `gh workflow run promote.yml -R DanielH2018/server --ref master -f
   reset_to_last_good=true`. The stateful hold names `prod`'s commit, so the reset's
   in-flight check passes. Then wait until the OCIRepository `homelab` reports
   `last-good`'s commit. The read-only `kubectl -n flux-system get ocirepository homelab -o
   jsonpath='{.status.artifact.metadata.org\.opencontainers\.image\.revision}'` must print
   `master@sha1:<last-good SHA>`. `.status.artifact.revision` holds a digest, not a commit
   (Baseline).
4. **Resume each reverted role, in the post's order.** Resume `<s>-snapshot`, then `<s>`,
   each with the same patch and `false`. The snapshot Job for `last-good`'s digest runs again
   with a fresh token. Then `<s>` applies `last-good`'s spec, which restores the replica
   count.
5. **Clear the hold.** Land the revert of the failed commit. Then clear `DEPLOY_HOLD` with
   `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server` and dispatch `promote` for
   it, by D2's procedure.

A co-changed role that attempted R and applied it successfully may skip steps 1, 2 and 4
only when the operator records in the hold's thread that its R-written data suits
`last-good`'s image. The reset then rolls it back without a revert. Without that record, it
is reverted.

**Branch 2: no snapshot role that attempted R failed.** The failures are in non-snapshot
roles, or in a `<s>-snapshot` Job that failed before `<s>` attempted R for a reason other
than a full cap. No volume needs a revert, so the operator skips steps 1 to 4. The default
is a forward revert: land the revert of the commit that broke the failing tag, then clear
`DEPLOY_HOLD` with `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server` and
dispatch `promote`. The changed snapshot roles stay at R, which they
applied successfully. A reset of `prod` is the alternative only after the
operator confirms that each changed snapshot role's data still suits `last-good`'s image.

**Branch 3: a snapshot Job failed on a full cap.** The role never applied R, so its data is
unchanged. The recovery is a hand delete of that volume's older snapshots, which `claim.yml`'s
comment on the cap gate names, and then a delete of the failed Job so that Flux re-creates it.
Both are `kubectl` writes. Then the operator clears `DEPLOY_HOLD` with
`gh variable set DEPLOY_HOLD --body none -R DanielH2018/server` and dispatches `promote` for R.

**The entry point.** `ansible/volume-revert.yml` is a playbook on daniel-box that includes
`k8s/volume-revert` alone and never runs `deploy.yml`. `k8s_no_mutate` comes from
`group_vars/all.yml`, so the role's guards resolve as they do in the play. The PR records the
route that `deploy_changes.py` gives the path. A test asserts that a change to the playbook
applies nothing from a tick, because a runbook must never run unattended. R4's exit 1 keeps
`volume-revert` and `longhorn-api` in its runbook allowlist, so R4's PR 7 is not needed. H7
replaces the playbook with a task-free tool before it deletes Ansible.

**The drill.** In Wave B, once speedtest is flux-owned, a one-line PR points speedtest's
`readinessProbe` at a missing path, as D2.8 does for bento-pdf. `verify` fails, and `promote`
holds without resetting and posts a message that names speedtest's snapshot. The operator runs
the runbook end to end. The revert then lands, as in D2.9. This is the live red proof of the
hold rule, the post, the entry point and the resume order.

**The drill runs again after the flux PR.** In Wave B, Ansible applies the root, so the drill
cannot show that a hand suspend survives Flux's own reconcile of the root. After the flux PR,
`flux-deferred` applies the root, and the root applies `<tag>`. The second drill waits through
one reconcile of each, longer than their `interval`, before step 2. It reads `.spec.suspend`
and the `homelab-operator` manager on `speedtest` and `speedtest-snapshot` with the read-only
`kubectl`, and quotes both. That drill is exit criterion 6.

### The soak as a Job (D5.3)

D5 adds no alert rule for the soak. The Job reads M2's `homelab:pod_service:info`, the
critical alerts that C7 defines, and any alert labelled `verify="true"`, whatever its
severity. A verify successor written as a rule carries that label, so it is read on every
promotion, an automerged one included, without having to page.

- **The Kustomization.** A non-tag Kustomization, `soak`, carries no `dependsOn` edges (Open
  question 19, ruled 2026-10-16). It holds one Job, `soak-<spec digest8>`, whose spec embeds
  R's SHA. The renderer takes the commit SHA as an input, so two renders of one commit stay
  identical. C2's changed set holds `soak` whenever it holds a tag, by a C2 rule that Open
  question 19 requests, so an unchanged tag that is not Ready blocks nothing.
- **Its run-time wait.** Before it reads a pod or an alert, the Job waits until every
  Kustomization in its scope reads Ready with `status.lastAppliedOriginRevision` naming
  `master@sha1:<R>`, by the origin-key rule of *Its anchor* below. A Kustomization Ready at
  an older revision keeps it waiting. The wait has its own deadline, below the Job's
  `activeDeadlineSeconds`, and at that deadline the Job exits 1 naming every Kustomization
  still not Ready at R. A suspended tag is outside the scope (C3, D5.1b), so it never
  blocks the wait. kustomize-controller's `--concurrent` gains one worker, because the
  soak holds a worker from the start of R's rollout.
- **Its footprint ignores R's SHA.** The renderer computes `soak`'s `own` and `footprint`
  digests (C1) over its render with R's SHA and the Job name derived from it replaced by
  fixed values. Otherwise `cluster/soak/` would change on every commit, and C2's changed set
  would never be empty. A docs-only commit then expects `flux/homelab` alone, as C2 says.
  The renderer test is two fixture commits that differ only in a docs file and give an
  empty changed set. Its red half is the same pair with the digest taken over the raw
  render, which puts `soak` in the set. The Job's ServiceAccount may read
  OCIRepositories in `flux-system`, and `get` and `list` Kustomizations there. In each namespace in its scope it may list pods, and
  `get` and `list` ReplicaSets in the `apps` group.
- **Its scope.** The Job reads only pods whose owner chain ends in a Deployment, DaemonSet or
  StatefulSet, in a namespace that some flux-owned tag renders into. M2's join covers exactly
  those owners. Job pods fall outside the scope: the soak's own pod, the `<tag>-snapshot` and
  `<tag>-verify` pods, and any CronJob pod that fires during the soak.
    - **A Deployment's pod is one hop further.** Its ownerReference names a ReplicaSet, and
      the ReplicaSet's own ownerReference names the Deployment. So the Job reads each owning
      ReplicaSet, which is why its Role grants ReplicaSets. M2 needs the same hop on the
      Prometheus side, through `kube_replicaset_owner{owner_kind="Deployment"}`. A pod whose
      owner is a ReplicaSet with no Deployment owner is out of scope, as M2's join leaves it.
    - **A refused ReplicaSet read fails the soak.** Without the grant, the Job would either
      fail with a 403 on every run, or drop every Deployment pod from the restart leg. The
      second outcome passes the restart check on nothing, for most of the 71 Deployments
      (failure class 3). So a 403 or any other error on that read exits 1 and names the
      ReplicaSet, and never shrinks the scope.
- **Its anchor, T0.** T0 is the time source-controller fetched revision R, read from the
  OCIRepository `homelab`'s `.status.artifact.lastUpdateTime`. The Job exits 1 if that
  artifact's commit is not R. That cannot happen while R's `promote` job holds the
  `promote` concurrency group, unless an operator reset intervened.
    - **The field it reads.** The commit is
      `.status.artifact.metadata["org.opencontainers.image.revision"]`, compared with
      `master@sha1:<R>`. That is the key that `kustomization_controller.go:getOriginRevision`
      reads, and D4 reads the same key. `.status.artifact.revision` holds
      `prod@sha256:<digest>` and names no commit (C6). A check against it would never match
      R, so every soak would exit 1 and every promotion would hold (failure class 2, D4's
      Risks).
- **Its scope is R's changed set.** The Job reads two `digests.json` files, R's from the
  artifact of the OCIRepository `homelab` and the base's from the artifact of a second
  OCIRepository, `last-good`, which follows the `last-good` tag. It computes C2's changed
  set between them through `scripts/deploy_tools/changed_set.py:changed_set`, which its
  ConfigMap carries as a byte-identical copy that the Job census checks. Once *Its anchor*
  has checked that `homelab`'s artifact is R, that artifact is `sha-<R>`'s content. R's
  file is not embedded in the Job's ConfigMap, because it would then move `soak`'s own
  digest on every change.
    - **The verifier is source-controller.** Both OCIRepositories carry the `verify` block
      that `homelab` carries, so source-controller checks each artifact's cosign signature
      against the `promote` workflow's identity before it serves the artifact. The Job runs
      no verifier of its own. It reads a file only from an OCIRepository that reads Ready
      and `SourceVerified`, with its spec observed, and only when the fetched tarball's
      SHA-256 matches `.status.artifact.digest`. `flux_job_image` cannot carry a cosign
      check: the standard library has no ECDSA, and the census admits no container that
      does not run a Python script. Reading through source-controller keeps the Job on
      that one image, and its one HTTPS endpoint, the API server, trusts the
      ServiceAccount's CA through `SSL_CERT_FILE`. Its services are the set's names with any reserved suffix stripped,
  minus the members of `SHARED_KINDS`. The restart and alert legs below read only those
  services.
    - **The base.** C2 takes the changed set against `prod`'s commit before the move, and
      the Job runs after the move. `last-good` names that commit in every case but two.
      After a stateful hold, `prod` stays at the held revision, so the Job's set is a
      superset of `verify`'s and checks more services, not fewer. In the reset race of *The
      stateful hold*, `prod` can trail `last-good`, and the Job's set leaves out names
      already verified at `last-good`.
    - **It fails toward the fleet.** The Job narrows only from a `digests.json` whose
      artifact signature verifies against the `promote` workflow's identity (C2). A failed
      read, a failed verification or a malformed file widens the scope to every flux-owned
      service and W to the fleet value, and the Job logs why. A base whose origin revision
      is already R widens too: under D5.3b2's observe-only `verify`, `settle` can move
      `last-good` to R while the soak runs, and R against itself would read as no change.
    - **An empty scope passes at once.** When the set holds no service, the Job runs no
      leg, waits for nothing and exits 0.
- **Its wait.** W is the larger of `k8s_rollout_stabilise_seconds` and the longest `for:`
  among critical or `verify="true"` rules labelled with a service in the scope, plus 60
  seconds for the kube-state-metrics scrape and 60 seconds for rule evaluation. The renderer
  writes each flux-owned service's longest such `for:` into the Job's ConfigMap, and the Job
  computes W at run time from the services in its scope. A future 1-hour critical rule on
  one service therefore lengthens only the soaks of revisions that change that service. The
  PR quotes the map's largest entry, the fleet value. The Job waits until W has passed since
  the latest Ready `lastTransitionTime` that the API reports among in-scope pods created
  after T0 and mapped to a service in the scope. The
  anchor comes from the API, not from Prometheus, so a pod not yet scraped cannot drop out of
  it. W includes a scrape interval, so every such pod has a Ready sample by the time the
  checks run. When no such pod was created after T0, as for a change that rolls no pod, it
  waits W from T0 and then runs only the alert check.
- **Restarts.** The Job reads restart counts and termination times from the API, from each
  pod's `containerStatuses`, never from a scrape. Prometheus supplies only the service mapping
  and the first Ready time. The check covers every in-scope pod created after T0 and mapped to
  a service in the scope through `homelab:pod_service:info`, in two legs:
    - **The gate leg** is today's gate, exactly. The Job reads `restartCount` when its run-time
      wait ends, which is after every Kustomization in its scope is Ready at R, and again
      when its W wait ends. The wait is never
      shorter than `k8s_rollout_stabilise_seconds`. Any increase fails.
    - **The early leg** covers the time between a pod's first Ready and the Job's start. The
      first Ready is the minimum of `kube_pod_status_ready_time` since T0, because that series
      moves to the latest Ready after a restart. A container whose
      `lastState.terminated.finishedAt` is at or after that time fails.
    - **An absent sample fails closed.** A pod that the API shows Ready but that has no
      `kube_pod_status_ready_time` sample since T0 fails the soak, naming the pod.
    - **A restart inside a scrape gap fails closed.** When kube-state-metrics has no sample
      within two scrape intervals of a container's termination, the leg cannot order the
      restart against Ready. It fails, naming the pod, the container and the gap.
    - **The bounded miss.** A Ready that lasted less than one scrape interval leaves no
      sample, so the early leg reads a restart right after it as a restart before Ready. The
      gate leg still catches any restart after the Job starts. Today's gate misses every
      restart before `rollout status` returns, so neither leg is weaker than it.
    - A restart before the first Ready is not counted, as today's gate does not count one.
    - A pod created before T0 belongs to a workload that R did not roll, which today's gate
      ignores too. The weekly reboot's restarts would fall here, and D5.2b2 keeps promotions
      out of the reboot window anyway.
    - An in-scope pod created after T0 whose `homelab:pod_service:info` series is missing or
      reads `service="unmapped"` fails the soak rather than passing, because an empty join
      must not read as clean (failure class 3).
- **Alerts.** Any firing alert for a service in the scope, with `severity="critical"` or the
  label `verify="true"`, whose `ALERTS_FOR_STATE` value is at or after T0 fails the soak and is
  named. Both series are read with `shadow!="true",alertname!="Watchdog"`, the filter M1's
  Open question 5 sets for C7's gate. A shadow alert pages nobody, and M4's
  `IngressProbeFailedCritical` keeps `shadow: "true"` until M4 PR 5, which can overlap D5.
  The Watchdog always fires, carries `service: observability` and has no `for`.
    - From the flux PR on, the soak reads D1.6b's `FluxControllerDown`, a critical alert
      with `service="flux"`, whenever `flux` is in the scope. The same group's
      `FluxNotReady` is a warning without `verify="true"`, so the soak ignores it.
- **Why the scope is read at run time.** Today's gate reads only the roles in its batch, and
  the soak matches it. The render cannot hold the scope, because the base it would diff
  against is not in its render. The renderer reads one commit, and `decide` picks T against
  `prod` only at run time. The GHCR read adds no outage mode, because a promotion already
  needs GitHub and GHCR, and so does the `flux/soak` post.
    - **The residual.** An unrelated critical alert no longer fails the soak. It pages
      through Alertmanager like any critical alert. A failed soak still resets `prod` for a
      revision that changes no snapshot role. Open question 17 states what C2's proposed
      revert rule leaves open (**operator-only**).
- **A Prometheus roll in its own revision.** When R changes observability, Prometheus rolls
  (`Recreate`). When `observability` is in the scope, the run-time wait holds the Job until it reads
  Ready at R, so the Job's legs start after Prometheus is back.
  Prometheus's pod is an in-scope pod created after T0, so the wait runs W past its Ready, as
  the API reports it, and every rule has evaluated for W before the alert check. The scrape
  gap that the roll leaves is the gap case above. It fails the soak only when a container
  restarted inside it.
    - A rule with no `for` re-activates on a Prometheus restart with a fresh `activeAt`.
      M1's `AlertmanagerNotificationsFailing` is `critical` and has none. So when an in-scope
      Prometheus pod was created after T0, an alert whose label set
      `last_over_time(ALERTS{alertstate="firing"}[5m])`, evaluated at T0, already shows
      firing is pre-existing, whatever its new `activeAt`. Prometheus's TSDB keeps that
      history on `prometheus-data` through the roll.
- **The failing services reach the operator.** On failure, the Job writes one line,
  `failed-services: <service>,…`, to its termination message, and exits 1. notification-
  controller's status carries only the Kustomization's own message, so the post prints the
  read-only command that reads this line (D5.2c, *Choosing the branch*).
- **The status.** The Job's completion makes `soak` Ready, and notification-controller posts
  `flux/soak`. That is how a GitHub-hosted job learns a LAN-only verdict.
- **Observe first, then gate.** `promote.py verify` expects every name in C2's changed set,
  and `soak` joins it whenever a tag changes. So `out/flux/soak.yaml` would make
  `flux/soak` gate in the same PR that first publishes it. Gating would then start on a soak
  nobody had watched pass. The first live soak could fail on a wrong field, a fence refusal
  or a missing Prometheus peer. It would then hold, and for a revision with no snapshot role
  reset `prod`, on every promotion until a revert.
    - D5.3b2 therefore adds an observe-only set to `promote.py verify`, a literal
      `OBSERVE_ONLY = frozenset({"soak"})`. `verify` subtracts it from the expected set, so
      it never waits on `flux/soak`. It also ignores a `flux/soak` `failure`, so that status
      neither holds nor resets. The name differs from D5.1b's soak exclusion, which keeps a
      suspended tag outside the soak's scope.
    - D5.3b3 deletes the set, and from then on `verify` expects `flux/soak` like any other
      name in the changed set.
- **`land-wait` waits on the soak from D5.3b3 on.** C6 and D2's *How `land.sh` waits* make a
  `flux/soak` `success` on R part of `settled`. The PR's tags never include `soak`, and
  the soak Job's run-time wait holds it until every changed tag is Ready at R, so the tags are
  always Ready before the soak reaches a verdict. A `land-wait` that read only the tags would therefore return `settled` before any
  soak verdict existed. If the soak then failed, `verify` would hold, and might reset
  `prod`, after the session had already reported a verified deploy. So D5.3b3, the PR that
  makes the soak gate `verify`, also extends `promote.py land-wait`:
    - **What it reads.** Once every one of the PR's tags is Ready at an origin revision R
      that descends from M, `land-wait` reads R's combined status with `gh`, the read that
      D2's *How `land.sh` waits* already plans "from D5 on". D2 reads it once. The soak
      finishes after the tags, so `land-wait` repeats the read on each 30-second poll until
      `flux/soak` has a verdict. That adds at most 120 authenticated calls an hour, inside
      the token's 5,000, and none against the anonymous quota.
    - **`settled`** now also needs a `flux/soak` `success` on R.
    - **A `flux/soak` `failure` on R ends the wait as `deploy-failed`**, with a new `Cause`,
      `soak-failed`. The message prints the read-only command that prints the soak's
      `failed-services:` line (D5.2c, *Choosing the branch*). It does not wait for
      `verify` to write `DEPLOY_HOLD`, which ends the wait as `promote-held` a poll later
      anyway.
    - **No `flux/soak` status, or a `pending` one, keeps the wait going.** The give-up
      deadline is unchanged, because the derived promote timeout already includes `soak`'s
      timeout (*Its timeout*, below).
    - **Before D5.3b3 the soak gates nothing**, so a `settled` that ignores it is accurate.
      That is why the read lands with the observe-only set's deletion, not with D5.3b2.
- **Its timeout.** The renderer sets `soak`'s `timeout` to the fleet W plus 10 minutes, a
  static ceiling, so the Job's wait never outlives its Kustomization. D4's chained verify
  timeout keeps the same ceiling, because the Job's W can exceed T's own. After a stateful
  hold the Job's base gives a superset of `verify`'s set, and a failed base read falls back
  to the fleet W. A `verify` budget taken from T's own W would then time out on a healthy
  soak and write a false hold. The per-scope W shortens only the Job's actual wait. The
  cost is that the reboot window and `land-wait`'s give-up deadline still scale with the
  fleet W. With no `dependsOn` edges, `soak`'s timeout leaves D4's chained path, so D5.3b2
  changes D4's timeout function to keep `verify`'s budget above it (Open question 19).
- **Reaching Prometheus.** Prometheus's fence is the bespoke
  `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-prometheus.yaml.j2`, which
  netpol-baseline's tasks apply. The Job is a cross-namespace caller from `flux-system`, so
  that template gains a peer for it in D5.3a, which lands and is checked before D5.3b2 wires
  the Job. The peer is one `from` item that ANDs the `flux-system` namespace with the pod
  label `app: soak`, on TCP 9090. `job-name` cannot be the selector, because the Job's name
  moves with R's SHA, and the renderer fixes no other pod-label convention. So D5.3b2's Job
  template sets `app: soak` on the Job's pod template (`spec.template.metadata.labels`). On
  the Job's own `metadata` alone the label matches nothing, because a NetworkPolicy reads pod
  labels only, and the soak's every query is refused.
- **The deploy annotation (D5.3b3).** After D6.12, `deploy_lib/playbook.py:annotate` is the
  only writer of Grafana's `deploy-annotation` line, and from the flux PR on `deploy.sh`
  deploys nothing in the cluster. `promote` cannot take over, because it runs on GitHub's
  runners and cannot write daniel-box's syslog. So the soak Job writes the cluster's
  annotation. When it posts `success`, and only then, it prints one logfmt line to stdout:
  `event=deploy services=flux sha=<R's first eight hex characters> result=ok source=flux`.
  Alloy ships the line to loki-homelab under `loki_streams.cluster_pods`' job, `k8s`.
    - **A second annotation query.** observability's `observability_deploy_annotation_expr`
      selects `{job="syslog"}`, and a LogQL log query has one stream selector. So D5.3b3 adds
      a second default, `observability_flux_deploy_annotation_expr`, which reads
      `{job="k8s", namespace="flux-system", container="soak"} |= "event=deploy" | logfmt`.
      `scripts/grafana/inject_dashboard_annotations.py` injects both. The renderer names the
      soak Job's one container `soak`, so the selector reads a fixed value. The stream selector
      scopes the query, so a dashboard load reads only the soak's lines, never every pod's.
    - **Tests.** `test_deploy_annotations.py` pins the soak's line shape and its
      success-only print, with a failing soak fixture that prints nothing.
      `test_loki_stream_labels.py` reads the new expression's `job` against `loki_streams`.

### Images built by CI (D5.4)

- **Build before publish.** `promote.yml` gains an `images` job, which `publish` needs. For
  each of the ten images, it renders the context with `homelab-render`, computes the same
  content tag as `image_builder_content_tag`, and pushes
  `ghcr.io/danielh2018/<name>:<content tag>` unless that tag already exists.
  `ansible/tests/deploy/test_image_builder_content_tag.py` is the oracle for the hash, and a
  test beside the `images` job's code asserts that it computes the same tag.
- **The context is a renderer output.** C1 defines only the `cluster` and `host` modes, and
  the context comes from `ansible/roles/k8s/image-builder/templates/context-configmap.yaml.j2`,
  a utility role that no tag renders. D5.4a1 adds, under `homelab-render cluster`,
  `DIR/images/<name>/`, which holds the rendered context files for each of the ten images,
  and `DIR/images/tags.json`, which maps each name to its content tag. The renderer renders
  each context with the `image_builder_*` variables that the calling role's include passes.
  It computes the tag from the context's `data`, as `image_builder_content_tag` does, and a
  flux-owned consumer's image ref reads the tag from that same computation in the same
  render. `scripts/lib/render_guard.py:BUILT_IMAGE_TAG_STUBS` seeds a stub for guards only,
  so a consumer rendered from it would name the wrong image. A renderer test fails when a
  consumer's rendered ref differs from `tags.json`. Open question 10 carries the C1
  amendment, which lands before D5.4a1 merges. No Kustomization's path reaches `DIR/images/`.
- **The packages are public.** **Operator-only, after the first `images` run:** set each of
  the ten packages public, as the `homelab-manifests` and `texbrain` packages are (ADR-0018).
  Open question 8 holds the pull-secret alternative.
- **Which registry a role reads.** The renderer points a flux-owned consumer's image at GHCR.
  An Ansible-owned consumer keeps the in-cluster registry until it moves.
- **A GHCR ref is pulled once per node.** D5.4b sets `imagePullPolicy: IfNotPresent` on every
  GHCR consumer, in the same PR that changes its ref. The content tag is immutable under the
  push-once rule above, so a cached copy is always the right image. With the baseline's
  `Always`, kubelet would ask GHCR on every container start, so a GHCR or WAN outage would
  fail a restart, a reboot or a crash recovery of all ten consumers.
    - nut is the pod half of the UPS shutdown chain, and a power event can take the WAN down
      with it. Under `IfNotPresent`, a restart on daniel-server uses the image its last
      rollout pulled. GHCR is needed only for a new content tag, or after kubelet's image
      garbage collection removes an image no container uses. H4 keeps the `nut` pod in the
      cluster, so this holds until an H-slice changes it.
    - A cold pull still needs GHCR. A node that never pulled nut's tag, or whose kubelet
      removed it, cannot start nut during a WAN outage, and WAN outages tend to come with
      the power events nut exists for. So nut's image keeps a LAN source, and Open question
      2 carries the recommendation: the in-cluster registry as a pull-through mirror of
      GHCR. nut's Wave C PR waits on that ruling.
    - A renderer census in `scripts/renderer/tests/` fails on a `ghcr.io/danielh2018/*:sha-*`
      ref with `imagePullPolicy: Always`, or with no policy. Its red fixture is nut's
      Deployment with `Always`.
- **Rebuilds stay in git.** Every context pins its base image by digest, so a base-image
  refresh arrives as a Renovate digest bump. That bump changes the context, so the content tag
  changes and Flux rolls the pods. A package refresh inside a build that no pin moves is a hand
  PR that bumps a `rebuild_stamp` data key in that context. This replaces `image_builder_force`
  and keeps C1's "no live reads". No scheduled workflow commits to master, because the branch
  rules require a signed, reviewed pull request with status checks.
- **A context that holds a secret** cannot be built in CI. The D5.4b census fails on any
  context ConfigMap that renders a placeholder, and that role's move waits on its own fix.

### How each kind of task moves (waves C and D)

Each role PR quotes its rows from R1's census and from the host-write census, and assigns
every row one class:

| Class | Example at `70591c307` | Becomes |
|---|---|---|
| wait or rollout | cloudflare-ddns "Wait for the cloudflare-ddns rollouts" | Flux's `wait`; deleted |
| verify on a metric | mosquitto "Fail if mosquitto-lan did not get its pinned LAN address"; dri-device-plugin "Read back the advertised device resource" | a rule labelled `service="<tag>"` and `verify="true"`, in M1's layout, read by the soak Job; M2's rule where one exists, such as `K8sExtendedResourceMissing` for dri-device-plugin |
| verify through the pod network or an API | sonarr "Fail if the cluster library is not intact"; the netpol probe Jobs; registry's self-tests; media-volume's hardlink probe | a Job in `<tag>-verify`, with its caller census and its fence peers landed before the flip (*What every Job carries*) |
| verify by running a CronJob once | `cronjob-gate`'s gate Job for pi-peer-backup | a Job in `<tag>-verify` copied from the CronJob's `jobTemplate`; design below |
| verify from outside the cluster | crowdsec "Ban daniel-pi and prove the edge enforces it" | crowdsec's plan, below |
| in-cluster mutation | pihole's gravity reconcile, qbittorrent's `files/apply_prefs.py`, jellyfin's Longhorn `snapshotMaxSize` patch, authelia's TOTP seed, crowdsec's machine registration and allowlists, Grafana's admin password, arr-notification's seed, configarr's gate run | a Job named by spec digest and safe to re-run, or an initContainer when the app needs it first; each row's design is below |
| restart on config or Secret change | cloudflare-ddns's `manifests_extra_rollouts`; observability's secret restart; pihole's `roll_one.yml` | `checksum/restart` per workload: D3's rule for cloudflare-ddns, widened by D5.1e to the self-rolled workloads of observability and pihole |
| stale Secret keys | `k8s/manifests`' `verify_secret_keys.yml` | Flux's server-side apply removes a field it owns and no longer declares; D3.5 hands each Secret's fields to kustomize-controller first |
| host-side, moved by H2 | the live crons, scripts and modules of crowdsec, configarr, janitorr, qbittorrent, artifacts and observability | H2's family PR for the role (H2.4b, H2.4c, H2.5b, H2.7), landed before the flip, which waits on it; the absent arms it leaves are the next row's |
| host-side, moved by D5 | docs's site directory, pi-peer-backup's two Pi writes, traefik's absent arm, registry's GC absent arms; the absent arms H2 leaves in crowdsec, configarr, janitorr and qbittorrent; artifacts' three directory tasks; media-volume's three media directories | the setup role `k8s_host_files`, in a conversion PR before the flip, until H5 or H7 takes each row; sources and variables move with the task (below) |
| host-side cleanup that has already run | game-stats' pre-#2813 record cleanup; observability's claude-otel record cleanup | deleted by D5's run-once cleanup PR before either role flips (below) |
| manifest staged outside the manifests directory | traefik's RBAC, `/etc/rancher/k3s/traefik-clusterrole.yaml` | rendered into the bundle by traefik's flip PR, which deletes the staging and apply tasks (Wave D's traefik bullet) |
| host-side that writes a flux-owned object | registry's GC cron and manifest | retired before the flip (Wave D's registry bullet) |
| imperative ConfigMap | monitor-bridge, autofix-bridge, game-stats, observability | a rendered ConfigMap, covered by its readers' `checksum/restart` (D3, and D5.1e for self-rolled readers); observability's ConfigMaps keep their own `checksum/config` too |
| pre-apply live read | pi-peer-backup, n8n, authelia's argon2 digests | R3's fix; D5 only checks the keys are in D3's cluster file |
| image build | the ten images of the nine `image-builder` callers | D5.4 |

**Each class has a red fixture.** A converted rule gets a promtool case that fires it. A
verify Job gets a fixture where its check fails, and the Job exits 1.

**A converted metric verify changes paging, so the operator reviews it.** A deploy-time
assertion becomes a standing rule.

- Each converted rule defaults to `severity="warning"`, which M2 routes to Discord without
  email, and carries `verify="true"`, which the soak reads. Raising one to `critical`, which
  M1 routes to email too, is **operator-only**, and the conversion PR asks for it by name.
- Where M2 already has a rule for the same fault, the conversion adds `verify="true"` to it
  rather than writing a second one. dri-device-plugin's successor is M2's
  `K8sExtendedResourceMissing`, a warning.
- The rule lands in a conversion PR while observability is still Ansible-owned, so `land.sh`
  applies it and rolls Prometheus then. The flip PR never touches observability, so a flip's
  revision does not roll Prometheus under its own soak.

**Each mutation row's design:**

- **authelia's TOTP seed** becomes an initContainer on the authelia pod, which runs the same
  `authelia storage user totp generate … --force` against the pod's own database and
  configuration. The secret arrives through substitution into the pod's Secret. The seed
  writes a fixed secret, so a re-run leaves the same row, as the task's own comment says.
  `Recreate` means no server runs beside it. On a new volume the initContainer meets an empty
  database. The conversion PR checks on the pinned image whether `totp generate` needs
  `authelia storage migrate up` first, and runs it first if so.
- **jellyfin's cap** becomes a Job in jellyfin's Kustomization. It resolves the volume from the
  PVC's `spec.volumeName` at run time, and patches `spec.snapshotMaxSize` to the rendered
  value, so a re-run changes nothing. RBAC cannot name a volume whose name the renderer cannot
  know, so its Role in `longhorn-system` grants `get` and `patch` on every
  `volumes.longhorn.io`. That is wider than the snapshot Jobs need, since they only read
  Volumes, and the conversion PR states it as the trade.
- **pihole's gravity reconcile and qbittorrent's prefs.** Each one's conversion PR names its
  mechanism, a Job or an initContainer, and the RBAC it needs, stating any grant wider than
  one object as a trade. Both meet the re-run rule: pihole's reconcile is already an
  idempotent INSERT/UPDATE that rebuilds gravity only on a change, and qbittorrent's runs
  `files/apply_prefs.py`. Each gets a double-run test.
- **crowdsec's machine registration and its two allowlists** become an initContainer on the
  LAPI pod. It runs the same `cscli machines add` for each absent machine and the same
  `cscli allowlists create home-ips` and `remote-ips`, tolerating "already exist", against
  the pod's own database before LAPI serves. The agent password arrives through substitution
  into the pod's Secret, so no RBAC and no exec are needed. A re-run adds nothing, as the
  tasks' own idempotence shows. crowdsec's Deployment is `Recreate` (`spec_shell('Recreate')`
  in its `deployment.yaml.j2`), so no LAPI serves beside the initContainer, as for authelia.
  The conversion PR checks on the pinned image that each `cscli` command works against the
  database with no LAPI running. If one does not, the PR
  falls back to a Job with the namespace-wide `pods/exec` grant, stated as the trade.
- **Grafana's admin password** becomes an initContainer on the grafana pod (D5.1e).
- **arr-notification's seed** becomes a post-apply Job in `sonarr-verify` and
  `radarr-verify`, which runs `files/seed_arr_notification.py` against the app's Service. Its
  webhook arrives through substitution into a Secret in the same Kustomization. Its API key
  comes from `<app>-exportarr`, the Secret the app's own render applies, as sonarr's verify
  Job reads it (#5471).
  Its first connection goes through the shared helper. `sonarr-verify` and `radarr-verify`
  join those roles' `netpol_from`. The script adopts and never deletes, so a re-run changes
  nothing, and its existing tests in `ansible/roles/k8s/arr-notification/tests/` gain a
  double-run case.
- **`cronjob-gate`'s gate Job** becomes a Job in `<tag>-verify` that the renderer copies from
  the CronJob's `jobTemplate`. The renderer strips `ttlSecondsAfterFinished`, which the Job
  census forbids, and keeps the pod template's labels, so the fences that admit the CronJob
  admit the gate. It names the Job `<cronjob>-deploy-gate-<spec digest8>` and embeds the
  role's `digest8`, so it runs once per role revision. Its `-verify` Kustomization's
  `timeout` exceeds the CronJob's `activeDeadlineSeconds`, as `cronjob_gate_timeout` does
  today, so a hung run fails on its deadline with its own message. Its pods are then named
  `pi-peer-backup-deploy-gate-<spec digest8>-<suffix>`, which `pull-pi-peers.sh` still reads
  as a gate run. A test beside the renderer reads the `HOSTNAME` pattern out of
  `pull-pi-peers.sh` and matches the rendered Job name plus a pod suffix against it, with a
  red half for a Job named `pi-peer-backup-gate-<digest8>`.
    - **The gate loses `Forbid`'s protection.** `kubectl create job --from=cronjob` sets a
      controller ownerReference to the CronJob, and `concurrencyPolicy: Forbid` keys on it to
      suppress a scheduled firing while a gate runs. `cronjob-gate`'s `CLAUDE.md` calls that
      the protected direction. A rendered Job cannot carry the reference, because the
      CronJob's UID is unknown at render time. So a scheduled firing can now overlap a gate
      run in both directions. Both callers meet that file's second property, idempotence
      under one extra run, so the overlap is the accepted cost, and each gate PR states it.
    - **The gate becomes stricter.** Today's gate fails the deploy only when the container
      never starts, and reports a non-zero exit without failing
      (`ansible/roles/k8s/cronjob-gate/CLAUDE.md`, a decision recorded in
      `docs/cronjob-gate-design.md`). A Job fails its Kustomization on any non-zero exit, so
      under Flux a transient Pi or *arr outage during a promotion holds it. The default is
      the stricter stance, because a hold is the safe direction. Each gate's PR states the
      change, and the operator accepts it or asks for today's split in that PR's review.
    - **H2 adds callers.** H2.4b gates crowdsec's allowlist and AppSec CronJobs, and H2.5b
      gates the three *arr checks, each through `k8s/cronjob-gate`. Each converts by this rule
      at its role's flip. The role PR classes each gate as a verify, `backoffLimit: 0`, or as
      a post-apply mutation with `homelab/rerun-safe`, as R1's census row lists it then.
    - pi-peer-backup's gate is a verify: `backoffLimit: 0`, and Wave C's pi-peer-backup PR
      wires it.
    - configarr's gate run performs the reconcile, so it is a post-apply mutation Job with
      `homelab/rerun-safe`, and Wave D's configarr PR wires it. configarr's sync converges
      on its config, which is why `cronjob-gate`'s `CLAUDE.md` admits it as a caller, so the
      annotation names the PR's live double run, quoted in its body, whose second run reports
      no change. The PR also checks that the daniel-box health reader still finds the gate's
      Job under its new name.

**`<tag>-verify`.** R4's Open question 1 proposes it, and D5 adopts it:

- It depends on the tag's last Kustomization, with `wait: true`: `<tag>-deferred` for a tag
  with deferred files (netpol-baseline, headlamp, pihole), `<tag>` otherwise. The renderer
  writes that edge, and a test in `scripts/renderer/tests/` checks it on a fixture tag with a
  deferred file. The Job therefore starts only after every object the tag renders is applied
  and Ready, which means the new ReplicaSet is available and every deferred policy is in
  place. A Job in `<tag>`'s own Kustomization would start beside the rollout and could read
  the old pod, the trap that the header of `ansible/roles/k8s/janitorr/tasks/verify.yml`
  describes.
- D4 places netpol-baseline's probe Job in `netpol-baseline-deferred` instead, under its rule
  for verifying Jobs. D5's edge gives the same order, after every policy the probe tests. The
  Job moves to `netpol-baseline-verify`, and D4's named member moves with it (Open question
  11).
- Its Job embeds the role's `digest8`, so it runs once per role revision, an automerged bump
  included.
- It is an expected status of `promote` whenever C2's changed set holds it, and `soak`
  depends on it.

**`probe.py health` is never the only successor of an eligible role's verify.** It runs only
from `land.sh`, so it misses every automerged bump. It may be the only successor for a role
with `k8s_autodeploy: false`, whose merges all go through `land.sh`.

**What counts as a verify successor.** Every converted verify row ends as one of three
shapes. R4's exit 5 checks the mapping against this list, so a cross-slice note asks R4 to
quote it:

- a rule labelled `service="<tag>"` and either `severity="critical"` or `verify="true"`,
  which the soak Job reads on every promotion (D5.3);
- a Job in `<tag>-verify`, which depends on the tag's last Kustomization: `<tag>-deferred`
  when the tag has deferred files, `<tag>` otherwise;
- a check in `probe.py health <tag>`, for a role with `k8s_autodeploy: false` only.

A converted rule defaults to `warning` with `verify="true"`, so a check that accepted only
`critical` would reject every rule D5 ships. C7 gains the `verify` label and the `flux/soak`
status before D5.3b1 merges (Open question 6), which is before any conversion PR adds such a
rule and long before R4's PR 1 runs its mapping.

**Host-side rows** split between H2 and D5. H2's family PRs move the live host crons of six
roles into the cluster or into `claude_code`: crowdsec (H2.4b, H2.4c), configarr, janitorr and
qbittorrent (H2.5b), and artifacts and observability (H2.7). H2 lands those PRs while the roles
are Ansible-owned, which is the order H2's *Who writes each change at merge time* assumes. Each
leaves rows behind (H2, *What H2 leaves in `k8s_host_files`*):

- crowdsec, configarr, janitorr and qbittorrent keep absent arms: the cron files, the script
  paths and, for configarr and janitorr, the host-lib copies;
- artifacts keeps its three directory tasks, "Ensure the local artifacts directory exists",
  "Ensure a peer directory exists for every synced host" and "Ensure the star list directory
  exists", each `ansible.builtin.file` with `state: directory`. The star list task, added on
  2026-10-11 after H2 counted the role's rows, also carries `become: true`;
- observability keeps no host row of H2's. Its claude-otel record cleanup was never an H2 row,
  and D5's run-once cleanup PR deletes it (below).

Every such row is a host write under D5.1c, which has no exception for an absent arm or a
directory. So D5 moves these rows, and the host rows of docs, pi-peer-backup, traefik,
registry and media-volume, to one setup role, `ansible/roles/setup/k8s_host_files/`, with
one task file per source role, in a conversion PR before that role flips:

- **One `k8s_host_files` conversion PR per role.** crowdsec, configarr, janitorr, qbittorrent
  and artifacts each get one. It lands after the role's H2 family PR, so it moves only what
  that PR leaves, and it drops the role's `allow` entry. docs, traefik, media-volume and
  registry's GC arms get one each in Wave D, and pi-peer-backup's rides in its Wave C PR.
  traefik's PR narrows the entry's reason to the RBAC file rather than dropping it, and the
  flip PR drops it (Wave D's traefik bullet). A role flips only once its host-write `allow`
  entry is gone. So crowdsec, configarr, janitorr, qbittorrent and artifacts each flip after
  both their H2 PR and their conversion PR. observability flips after H2.7 and the run-once
  cleanup PR, and game-stats after the run-once cleanup PR and its imperative ConfigMap PR.
- **The run-once cleanup PR deletes two cleanups that have already run.** game-stats' "Remove
  the staged manifests, release and render records the pre-#2813 roles left behind" and
  observability's "Remove the release and render records the claude-otel name left behind"
  remove host files nothing recreates. daniel-box applied both roles at `c5d00ead`, which
  carries both tasks' last edit (`7fceba186`). None of their nine release and render records
  is left, and the PR body quotes an `ls` of them. Moving a task with nothing left to remove
  into `k8s_host_files` would only hand H7.18c another legacy entry, so the PR deletes both.
  It lands any time before either role flips, and it is a tasks-only change, which the
  deployer skips. The same PR:
    - drops game-stats' `tasks/main.yml` entry and the claude-otel clause of observability's
      entry, or the whole entry if H2.7 has landed;
    - retires the `release-record-cleanup-removes-the-render-record` census row and its
      module (#5162). Its `must_find` names only
      these two task files and its floor is their three records, so with both tasks gone the
      row has no subject. `ansible/tasks/k8s_retire.yml` already removes both record kinds
      for a whole-service retirement.
- **What `k8s_host_files` holds, and who takes each row.** No row stays there past H7:
    - docs's "Ensure the built docs site directory exists" on daniel-box, which H7.10a
      replaces with a `tmpfiles.d` line in daniel-box's host tree;
    - artifacts' three directory tasks on daniel-box, which H7.12c's daniel-box PR replaces
      with `tmpfiles.d` lines in daniel-box's host tree: one for the local directory, one
      per peer and one for the star list directory;
    - media-volume's three directories under `/srv/media` on daniel-box, owned by
      `puid`:`pgid` with no mode, which H7.9b replaces the same way. A `tmpfiles.d` line can
      set the owner and leave the mode alone, which the task's comment requires because
      the seeding rsync imposes daniel-server's modes;
    - pi-peer-backup's forced-command wrapper and authorized key on daniel-pi, which H7.4's
      `pi-peer-backup-target` component ships;
    - crowdsec-verify's probe timer on daniel-pi, which H5.14 moves into the Pi package;
    - the absent arms: traefik's for the Cloudflare IP drift cron, registry's three for the
      GC, and the ones H2 leaves in crowdsec, configarr, janitorr and qbittorrent. They have
      nothing to remove after their first apply. H7.18c turns them into legacy entries and
      deletes the role.

  H5 (*Rows H5 takes from D5's `k8s_host_files`*) and H7 (its setup-role table, H7.4, H7.9b,
  H7.10a, H7.12c and H7.18c) list these rows.

- **The task moves with its sources and variables.** Role defaults and role-relative `src:`
  paths do not resolve in another role (Baseline). So each conversion PR moves the role's
  host templates and files into `k8s_host_files`' own `templates/` and `files/`. A variable
  that only the host files read moves to `k8s_host_files`' `defaults/main.yml`. No task
  `include_vars` another role's defaults.
    - **A variable both sides read stays in the k8s role, with a test-held copy.** The k8s
      role's defaults keep it, `k8s_host_files`' defaults hold a copy under that role's
      prefix, and a test in `ansible/tests/setup/` holds each pair equal. A change then edits
      both, and each half routes to its own plane: the copy to the setup plane, which the tick
      applies. A shared key in `group_vars/all.yml` would route to the deploy plane only
      (Baseline), which never runs `initial_setup.yml`, so the host half would never
      re-apply. H2.7 uses the same pair for `claude_code`. artifacts' `artifacts_host_dir`,
      `artifacts_peer_dir`, `artifacts_peer_sources` and `artifacts_stars_dir` are the pair
      test's named members: `ansible/roles/k8s/artifacts/templates/deployment.yaml.j2` reads
      all four, and so do the three directory tasks. media-volume's conversion PR adds `media_volume_host_path`,
      which `ansible/roles/k8s/media-volume/templates/pv.yaml.j2` and the directory task both
      read. The task's owner reads `puid` and `pgid` from `group_vars/all.yml`, the shared-key
      case above, so the same PR copies them into `k8s_host_files`' defaults as pairs too.
    - A test in `ansible/tests/setup/` renders every template in `k8s_host_files` with that
      role's defaults and `group_vars`, under `StrictUndefined`, through
      `scripts/lib/ansible_jinja_env.py`. Its red half is a fixture template that reads a
      variable defined only in a `roles/k8s/` defaults file. D5.1h lands it while the role
      has no template, so until then it asserts that the set it renders is empty. Wave C's
      pi-peer-backup PR adds the first template, and with it the test's named member.
    - The pair test lands in D5.1h too, asserting an empty set of pairs. artifacts'
      conversion PR adds its first pairs and names them as its members.
- **D5.1h enters the role into the setup plane, alone.** Its `initial_setup.yml` entry parks
  the tick once, and the operator clears it (*The `k8s_host_files` role enters the setup
  plane once*). From then on the tick applies every `k8s_host_files` PR and `land.sh` follows
  it.
- Its `CLAUDE.md` carries the Autonomous-role contract, because it installs a timer and
  removes crons. `ansible/tests/repo/test_role_claude_md.py` and
  `ansible/tests/setup/test_setup_cron_roles_have_a_contract.py` hold that.
- The role's playbook entry runs on daniel-box. `setup_routing.py` places a role whose gate
  admits only another host as a hand apply, so a Pi-gated role would never run from the tick.
  pi-peer-backup's two writes on daniel-pi therefore keep `delegate_to: daniel-pi` inside
  `k8s_host_files`, which is today's mechanism, until H7.4's `pi-peer-backup-target`
  component ships them. The crowdsec probe timer does the same until H5.14. A rotated
  `pi_peer_backup_ssh_key` reaches the Pi through that role's apply, and the rotation registry
  entry names it as the redeploy until H7.4 repoints it.
- M6's batch B6 rewrites several of the crons H2 moves. H2 and M6 order those PRs between
  themselves. D5 flips each role as it finds it once its H2 PR and its `k8s_host_files`
  conversion PR have landed.

**crowdsec's verify from outside the cluster.** A Job inside the cluster cannot prove that the
edge enforces a ban for an outside client. crowdsec does not flip without this successor:

- A `crowdsec-verify` Job bans daniel-pi's address at LAPI for a bounded window, then waits
  for the edge bouncer's pull, as hops 1 and 2 of today's gate do.
- A timer on daniel-pi, which `k8s_host_files` installs through `delegate_to` until H5,
  probes the VIP every minute and appends the HTTP status to a file under
  `/var/log/pi-health/`, which the Pi's Alloy already tails.
- **The evidence must not cross the edge it tests.** The Pi's Alloy pushes logs through
  Traefik, which refuses the Pi for the whole ban (Baseline). Alloy's `loki.write` retries only
  429 and 5xx, so a log line would be dropped. Instead, a metrics stage in the Pi's Alloy
  pipeline counts probe results by status, and Prometheus scrapes it directly as `alloy-pi`.
  That is a pull to the Pi's LAN address that never passes Traefik. The stage counts each line
  when Alloy reads it, whatever happens to the push.
- The Job polls Prometheus for an increase in the 403 count inside the window, and lifts the
  ban in every exit path. The window covers one timer period, one 1-minute scrape and the
  30-second scrape timeout, with slack. The Pi's own log pushes are refused during it, as
  they are during today's gate.
- **Red fixtures.** An edge that answers 200 while banned fails, naming the edge. A blocked
  evidence channel, where `up{job="alloy-pi"}` is 0 or the counter has no sample in the
  window, fails with a message that names the channel, not the edge.
- **What each PR lands.** crowdsec's first conversion PR lands the Pi timer, the Alloy stage
  (a hand apply on the Pi, `-e target=daniel-pi`) and the Job's script and tests as inert
  code, which no render emits yet. The flip PR wires `crowdsec-verify` and deletes
  `tasks/verify.yml`. Open question 9 records the channel.
- **H5.8 moves the same Alloy config.** The stage goes into
  `ansible/roles/containers/alloy/templates/config.alloy.j2`, which H5.8 turns into the
  package file `/usr/lib/homelab/compose/alloy/config.alloy`. Whichever of crowdsec's first
  conversion PR and H5.8 lands second carries the other's change: the stage into the packaged
  file, or the packaged path into the PR. The PR that lands second quotes the stage's counter
  from a read-only `probe.py metric` query on `alloy-pi` after its apply. A cross-slice note
  asks H5 to state the same edge, and to list the probe timer beside `/var/log/pi-health`.

A role whose rows are not all classified and converted does not move. Ansible stops running a
role's tasks the moment it is flux-owned, so D5 converts each verify at that role's move.

### Waves and windows

**Wave size and the status burst.** Each promotion posts one commit status per Kustomization
that reconciles it. D2's Open question 13 projects that burst at D5's scale, and each wave PR
sizes its batch of roles against that projection.

| Wave | Roles | Shape | Window |
|---|---|---|---|
| A | deploy-ui, gpu-exporter, homepage, littlelink, longhorn-ui, node-exporter, nut-exporter, pihole-exporter | one task, `RollingUpdate` or DaemonSet, no snapshot | any time; littlelink only after M6's B-1e |
| A' | loki-homelab, navidrome, scrutiny; then wg-easy alone | one task, `Recreate`, no snapshot | any time; wg-easy in its own window, with the operator on the LAN |
| B | bazarr, freshrss, livesync, speedtest; then zigbee2mqtt; then home-assistant | one task, snapshot claims | any time; zigbee2mqtt and home-assistant each in its own window |
| C | ical-proxy, homelab-mcp, terraria, valheim, code-server, karakeep, nut, n8n, pi-peer-backup | image build; code-server and karakeep with snapshots; n8n with a probe Job; pi-peer-backup with Pi writes and a gate Job | any time |
| D | cloudflare-ddns, docs, radarr, mosquitto, dri-device-plugin, artifacts, autofix-bridge, game-stats, prowlarr, sonarr, configarr, tdarr, qbittorrent, janitorr, jellyfin, media-volume, registry, monitor-bridge, uptime-kuma, observability, headlamp, crowdsec, traefik, authelia, pihole, netpol-baseline | imperative tasks | any time, three to five roles per PR; the home-edge roles (crowdsec, traefik, authelia, pihole) each alone in its own window |

- **Windows, as the operator ruled on 2026-10-17.** A flip needs no weekday or hour. A
  `Recreate` role's takeover roll takes that one service down briefly, and the operator accepts
  that at any hour. Seven roles each take a window of their own: wg-easy, zigbee2mqtt,
  home-assistant, crowdsec, traefik, authelia and pihole. An own window is an hour, agreed with
  the operator and announced in the deploy channel, in which no other flip lands. wg-easy's
  also needs the operator on the LAN. The plan first held every `Recreate` flip to the hour
  after M2's `weekly-reboot` mute ends, Sunday 08:15 to 09:15 UTC, and the ruling drops that:
  the hour was a maintenance slot, and no flip depends on it.
- **Flip PR size, as the operator ruled on 2026-10-17.** A flip PR in waves B to D carries
  three to five roles, where the plan first gave Wave C about two and Wave D one. The seven
  own-window roles still flip alone. A role joins a flip PR only once every conversion PR,
  H2 PR and M6 step it waits on has landed, so a batch takes the next ready roles in wave-table
  order rather than a fixed slice of the table. The cost is that one role's failed soak holds
  every role in its revision, and the stateful hold (D5.2b1) applies to the whole batch.
- **Which PRs the fan-out review reads, as the operator ruled on 2026-10-17.** A flip PR and a
  status PR skip the fan-out pipeline's separate model review. CI, the operator's pre-merge
  diff and the soak still gate each one. A mechanism, conversion or deletion PR keeps the
  review.
- **A flip still keeps clear of the weekly reboot.** A flip whose verification would still be
  running at the 07:35 UTC Sunday reboot does not land. Once D5.2b2 lands, `promote` enforces
  this itself: inside its derived no-promotion window it defers, and the flip promotes when the
  window ends at about 08:15 UTC.
- **Order within wave D** goes from low blast radius to high. The home-edge roles come near
  the end, then netpol-baseline, because it depends on everything (D4).
- **observability moves before headlamp.** headlamp renders a deferred document into
  `observability`, so D4 gives `headlamp-deferred` an edge to `observability`. A `dependsOn`
  naming a Kustomization that does not exist fails on every reconcile. D5 also asks D4 to drop a namespace edge to an Ansible-owned role, as it
  drops a toposort edge.
- **uptime-kuma moves only if M6 has not retired it.** If M6 lands first, its entry leaves
  the list, and it never moves.
- **wg-easy moves alone.** Its At-a-glance block calls it the "sole remote-access path; a
  failed deploy can cut off remote recovery", and it is `Recreate` with an RWO claim. Its PR
  lands in its own window, and its precondition is that the operator is on the LAN, not
  connected through wg-easy. If the takeover roll fails, the recovery starts from the PR's
  takeover census and the operator's substituted diff, read against the live Deployment from
  the LAN. Then a revert of the flip hands the role back to Ansible for `land.sh` to redeploy.
- **Late arrivals.** An entry created after `70591c307` and before the first Wave A PR is
  Ansible-owned, because the scaffolder writes `owner: flux` only from that PR on. M4's
  blackbox-exporter is the known case: M4 PR 1 says "D5 moves it with the rest". Before each
  wave's last PR, the session splits `render_targets.py daniel-box`'s output on commas, and
  assigns every tag the wave tables do not name to the earliest remaining wave whose shape it
  matches. blackbox-exporter, a one-task `RollingUpdate` role, joins Wave A if it exists by
  then.
- **The last three PRs** disarm render-records, move the flux role, then move the namespace.
  Each has the precondition that `render_targets.py daniel-box` prints `flux` alone (before
  the disarm PR and the flux PR) or nothing (before the namespace PR). The `owner: flux` count
  in `ansible/inventory/host_vars/daniel-box.yml` must be one less than its `platform: k8s`
  count before the disarm PR and the flux PR, and equal to it before the namespace PR. Each PR
  body quotes both outputs.

## Steps, as PRs

**Every role PR** meets these preconditions, and its body quotes the evidence:

- D2.7's preconditions and two-writer check, adapted to its roles. That check covers
  `deploy.sh` exiting 5, `--list-services`, the three-reader census and `render_targets.py`.
- R1's census rows and the host-write census rows, each with its class and its successor.
- For every Job the role's `-verify` or tag Kustomization adds, its caller census and the
  merged PR that landed its fence peers.
- **What the takeover strips:** the output of `probe.py takeover <tag>` for each of the PR's
  tags (D5.1g), which the session runs with the read-only `kubectl`. It lists every field
  path that a manager kustomize-controller cleans owns and the render omits, such as a
  `kubectl-rollout` `restartedAt` or a `kubectl-patch` hand patch. Each listed path is either
  expected, like `restartedAt`, whose loss rolls the pod once, or moved into the render before
  the flip.
- **Operator-only: what the takeover strips from RBAC objects.** The read-only ServiceAccount
  cannot read Roles, RoleBindings, ClusterRoles or ClusterRoleBindings, so `probe.py takeover`
  names each as not checked on its last line. The operator runs H1.1j's
  `scripts/dev/takeover_census.py` with the admin kubeconfig over those objects
  (`docs/flux-deploy-plane.md`, *What a takeover strips*), and the PR quotes its output.
- **Operator-only: what the takeover fails on or changes.** The output of `flux diff
  kustomization` over the role's substituted render (D5.1g), for immutable fields such as a
  PVC's spec and for changed values. It leaves Secrets out, because D3.5 covered their
  managers. The agent's read-only ServiceAccount cannot run a dry-run write, and the
  substitution reads `homelab-cluster-vars`.
- **The session runs both checks through the takeover check wrapper (#5494).** `sudo -n
  /usr/local/sbin/homelab-takeover-check --label <pr> --render <dir> --src <dir> --tags <a,b>`
  does the `flux diff`, or the server-side `kubectl diff` stand-in, and the census for each
  tag, and prints object names, field paths and exit codes only. The operator no longer runs
  a script per flip batch. The wrapper's limits are in the `setup/claude_code` role's
  `CLAUDE.md`, *The takeover check*.
- Both quote object names and field paths only, never values or diff hunks, because R2's
  baseline finds credentials in ConfigMaps too, and the repository is public.
- For an own-window role (*Waves and windows*), the window it lands in.

1. **D5.1a1 — The publish layout** (about 150 lines). Extends D2.6b's allowlist census,
   which holds `cluster-vars` from D3.3a: the per-tag kinds and where they are written, the
   input rule for each tag's file, the reserved names `soak` and `homelab-namespace` and the
   suffixes `-snapshot` and `-verify`, and `deletionPolicy: Orphan` on every non-tag kind,
   with the red fixtures listed in the design. It adds no member to `SHARED_KINDS`. Inert for
   every role. It lands after D3.3 and before any PR that adds a D5 kind.
2. **D5.1a2 — Prune labels, digest fields and collisions** (about 90 lines). The
   prune-disabled labels, the `claims` and `suspended` fields that each tag's object in D2's
   `digests.json` gains, and the collision census with its red fixture of two tags rendering
   one Middleware. D2's first publishing PR creates the file, so D5.1a2 lands after it and
   after the C1 amendment in Open question 10.
3. **D5.1a3 — The Job census** (about 130 lines). The census rows for TTL, name, `backoffLimit`
   and `homelab/rerun-safe`, the shared connection helper with its retry, and the row against
   a script that connects without it. Red fixtures: one Job of each kind with a TTL, a
   mutation Job without the annotation, and a script that calls `urllib.request.urlopen`
   directly.
4. **D5.1b — The suspend lever** (about 150 lines). The renderer's suspend and dropped edges,
   the rule against `suspend: false`, `promote.py verify`'s exclusion, the `probe.py landing`
   line with its field managers, and the `gitops_deploy` `CLAUDE.md` sentence. Tests:
    - in `scripts/renderer/tests/`: a suspended fixture tag is in no `dependsOn`, and is in
      `soak`'s and netpol-baseline's `homelab/suspended-deps`; no document in the fixture
      bundle renders `suspend: false`, and a fixture template that writes one fails;
    - in `scripts/deploy_tools/tests/`: a suspended tag that never posts neither times out
      `verify` nor triggers a reset.
    - **The shed scenario moves to the lever in the same PR.** D2's first leaf and D4's
      texbrain are flux-owned before D5 starts, and every wave adds more. Flux reverts a
      hand scale through drift correction of the rendered `replicas`, and
      `./scripts/deploy.sh --tags <svc>` exits 5
      for a flux-owned tag. So the shed steps in `docs/break-glass.md` (*one node is down
      and the survivor runs short of memory*) no longer free memory, or scale back up, for
      such a service. D5.1b rewrites them, all **operator-only** because they need `sudo
      k3s kubectl` and a `DEPLOY_HOLD` change:
        1. Set `DEPLOY_HOLD` with `gh variable set DEPLOY_HOLD --body <reason> -R
           DanielH2018/server`. A hand suspend is not in `digests.json`. So when a
           suspended tag is in a later revision's changed set, `verify` would wait on it
           until it timed out, as in the revert runbook.
            - **Offline branch.** When GitHub cannot be reached, the operator skips this
              step and records the reason and the time in a local file on daniel-box.
              Step 2 runs as written, because it needs only the LAN. Once the WAN returns,
              the first action is this step's `gh variable set`, before any resume and
              before step 3, because a queued `promote` run can start as soon as GitHub
              answers.
        2. For each flux-owned service in the shed set, suspend `<tag>` with
           `--field-manager=homelab-operator` (*The stop lever*), then scale it to zero. An
           Ansible-owned one scales to zero as before. A suspend frees the reconcile worker
           that the tag holds only when that worker's current wait ends, which can take up
           to the tag's `timeout`.
        3. Once the node is back, and before any resume, read the hold with `gh variable
           get DEPLOY_HOLD -R DanielH2018/server` and compare it with step 1's reason.
           Step 1 does not stop a promote run that was already past `decide`, because
           `decide` reads the hold only before it moves `prod`. If that run's `verify`
           failed, it wrote its revision's SHA over the reason and may have reset `prod`
           to `last-good` (C2). A reset moves every unsuspended tag to `last-good` at once,
           and each suspended `<tag>` applies `last-good` when it resumes.
            - **It names a SHA.** The hold records a failed revision, so the operator does
              not clear it here. The operator picks a branch by the revert runbook's
              *Choosing the branch* (D5.2c), and resumes the shed tags after that branch's
              own resume step. The branch's last step clears the hold. Before D5.2c lands,
              the operator works the hold by D2's procedure instead.
            - **It still reads step 1's reason.** Resume each suspended `<tag>` with the
              same patch and `false`, rather than running `deploy.sh`. Flux's next
              reconcile restores the rendered replica count. Wait until `<tag>` is Ready,
              then clear `DEPLOY_HOLD` with
              `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`.

           An Ansible-owned one is redeployed with `./scripts/deploy.sh --tags <svc>` as
           before.
      The daniel-server-dead scenario's step that redeploys the shed set points at that
      last step. `probe.py shed-set` (`scripts/diagnostics/probe_lib/shed_set.py`) prints
      each service's owner, read through `tag_owners`, and the suspend and resume commands
      for a flux-owned one. A test beside shed-set's existing tests, with one flux-owned and
      one Ansible-owned fixture service, finds the suspend command on the first only.
      D5.2c's text test also reads `docs/break-glass.md`, so a shed step that patches
      without `--field-manager=homelab-operator` fails it from then on, and so does a shed
      clear of `DEPLOY_HOLD` that no `gh variable get DEPLOY_HOLD` read precedes.
5. **D5.1c — Host-write census and concurrency** (about 140 lines). The two census rows, and
   the concurrency patch. The patch reaches the flux role, which Ansible still owns, so
   `land.sh` deploys it.
6. **D5.1d — A suspended role takes no change** (about 140 lines). Lands before any role sets
   `flux_suspend`. Tests:
    - beside `cross_plane_order.py`: a PR that changes a suspended tag's render fails; the
      same change together with the removal of `flux_suspend` passes;
    - in `scripts/deploy_tools/tests/`: `decide` holds with `suspended-changed` on a revision
      that changes a tag suspended at both ends, and passes the resume revision; `land-wait`
      returns `suspended` at once for a suspended tag, where the red half is the same fixture
      polling to `gave-up`.
7. **D5.1e — Restart annotations for self-rolled workloads** (about 120 lines). D3's
   `checksum/restart` stamped on every `manifests_self_rollouts` entry, the census that
   replaces D3's interim one with pihole-2's red fixture, and the two renderer tests in the
   design. Inert until observability or pihole flips, because it stamps only flux-owned
   tags. It lands before the first Wave A PR, so no role with `manifests_rollout: ''` flips
   without a restart trigger (Baseline).
8. **D5.1f — Removal gates** (about 150 lines). Both checks in `cross_plane_order.py`, with
   their red and green fixtures beside it, the `Removes-Object:` grammar and its two
   exemptions, and the retirement order with its
   **operator-only** claim delete in `docs/flux-deploy-plane.md`.
9. **D5.1g — The takeover census** (about 120 lines). `probe.py takeover <tag>`, its tests
   in `scripts/diagnostics/tests/` with speedtest's `restartedAt` as the red half, and the
   operator's `flux diff kustomization` command in `docs/flux-deploy-plane.md`. Read-only,
   so it changes nothing live. It lands before the first Wave A PR, whose body quotes its
   output.
10. **D5.1h — The `k8s_host_files` role, alone** (about 80 lines). The role with no task file
    yet, its `CLAUDE.md`, the `StrictUndefined` render test with its red fixture, and its
    `ansible/initial_setup.yml` entry, gated to daniel-box. Nothing else rides in it,
    because the entry parks the tick. The merge waits for a converged deployer, and the
    **operator-only** clear follows *The `k8s_host_files` role enters the setup plane once*.
    The PR body quotes the park line, `land.sh`'s `VERDICT:` line and the operator's
    playbook recap. It lands before Wave C's pi-peer-backup PR, the first that moves a row
    into the role.
11. **D5.2a — Snapshot Jobs** (about 200 lines, split in two if the script's checks push it
    past that). The `<tag>-snapshot` renderer kind with its inherited edges, the Job script
    and its RBAC. Tests:
    - in `scripts/renderer/tests/`: a fixture role gets `<tag>-snapshot`, its Job name moves
      with the digest and with the script, its longest snapshot name stays under 63 bytes,
      and a fixture tag's upstream edge appears on both `<tag>-snapshot` and `<tag>`;
    - a Job-script test with five red halves: a snapshot that never reports `readyToUse`; an
      existing CR with the same name, which must fail rather than be adopted; a `markRemoved`
      CR, which must not count toward retention; a reached cap, which must fail naming the
      cap before creating anything; and a listing that lacks the new snapshot, which must
      fail before any delete. A green half reads an empty cap as uncapped;
    - a Job-script test for a new claim. A claim absent live and absent from a fixture
      `<tag>` inventory that lists the role's other claims is skipped, logged, and the Job
      passes. The red half is the same claim listed in the inventory but absent live, which
      fails naming the PVC.
12. **D5.2b1 — The stateful hold and its post** (about 160 lines). The no-reset rule and the
    hold post with its per-role command blocks. **Operator-only before merge:** add
    `DEPLOY_DISCORD_WEBHOOK`. Tests in `scripts/deploy_tools/tests/`:
    - a failed revision that changes a snapshot role holds without resetting, and one that
      does not resets;
    - the stateful hold writes `<T> stateful=<names>`. The red half is a hold written in
      C2's `failed=` form, which C2's proposed revert rule could clear;
    - a revision that changes a snapshot role and a failing non-snapshot role holds without
      resetting, and its message marks which tag failed;
    - a revision that changes two snapshot roles, one of which failed, posts a full command
      block for each, each with its own `digest8` and claims, and marks the failed one. The
      red half is a post that carries a block for the failed role only;
    - the hold message names each changed snapshot role's prefix per claim, and a fixture
      with two claims finds both;
    - each classify command carries the comparison string `prod@sha256:<digest>`, with the
      digest that a fixture registry returns for the `sha-<R>` tag. The red halves are a post
      whose classify command prints R's SHA as the comparison value, and one that prints none;
    - a `soak` failure's post names the soak Job and prints its read command;
    - a failed post fails the run.
13. **D5.2b2 — The reset input and the reboot window** (about 130 lines). The
    `reset_to_last_good` input in its own concurrency group `promote-reset`, which ignores
    the window and refuses when `DEPLOY_HOLD` reads `none`, is empty or is missing; the derived no-promotion window in
    `decide` and `land-wait`, read with T's own derived timeout; the `deferred` result and
    `promote.yml`'s derived `schedule:` trigger at the window's end, so no run waits in the
    `promote` job, whose 6 hours also hold `decide`'s in-flight wait (D4 *The cap*); the
    reset's refusal while a verification is in flight, passed only by a hold whose SHA's
    `sha-<X>` digest equals `prod`'s, and its write's compare-and-move (#5079); and the
    seven tests, as *The stateful hold* describes.
14. **D5.2c — The revert entry point** (about 150 lines). `ansible/volume-revert.yml`, the
    runbook with its two branch-choosing reads and its three branches in
    `docs/volume-snapshot-drills.md`, and the edits to
    `ansible/roles/k8s/volume-revert/CLAUDE.md` and
    `ansible/roles/k8s/volume-snapshot/CLAUDE.md`. A test in `ansible/tests/deploy/` asserts
    that a change to the playbook applies nothing from a tick. A text test over the runbook,
    and over D5.1b's shed steps in `docs/break-glass.md`, fails on a `kubectl patch` without
    `--field-manager=homelab-operator`. In the shed scenario it also fails when the step
    that clears `DEPLOY_HOLD` with `--body none` comes before, or without, a `gh variable
    get DEPLOY_HOLD` read. Its red fixture for that clause is the shed scenario with that
    read removed, which leaves the clear unconditional. In the shed scenario's offline
    branch it fails when a resume or a clear comes before the branch's `gh variable set
    DEPLOY_HOLD`. Its red fixture is the offline branch with a resume moved first, and the
    branch as written passes. The test also fails on a
    `volume-revert.yml` command written into Branch 1 itself, so that Branch 1 always runs
    the post's per-role blocks rather than a one-role copy. It also fails on a classify step
    that reads `lastAttemptedRevision` without naming the `prod@sha256:` value it compares
    with, and on a wait for `last-good` that reads `.status.artifact.revision` instead of the
    `org.opencontainers.image.revision` key.
    The playbook's play gathers facts, because `ansible/roles/k8s/longhorn-api/tasks/resolve.yml`
    reads `ansible_hostname` four times. It leaves `gather_facts` unset or true, or it runs
    `ansible.builtin.setup` as `ansible/pre_tasks/load_secrets.yml` does. Every playbook but
    `bootstrap.yml` sets `gather_facts: false` and gets its facts from that pre_task, so a
    copied header without the pre_task would run the revert with `ansible_hostname`
    undefined. R4's PR 8 runbook-resolution test asserts this.
15. **D5.3a — The Prometheus peer** (about 30 lines). The `flux-system` peer in
    `networkpolicy-prometheus.yaml.j2`. netpol-baseline is Ansible-owned, so `land.sh` deploys
    it. The session checks the peer with the read-only `kubectl -n observability get
    networkpolicy`.
16. **D5.3b1 — The soak script, inert** (about 230 lines, split in two if review asks).
    Requires M1 and the M2 PR whose rule the soak reads, M2's PR 1 (#4777, the
    workload-service mapping), not M2 done. The operator ruled so on 2026-10-16, making M2 a
    PR-level sub-edge from D5.3b1 on. The script includes the run-time scope: the two
    signed `digests.json` reads, the changed set, the fallback to the fleet and W from the
    per-service map. It names the signature verifier the Job runs: source-controller's
    cosign check on the OCIRepositories `homelab` and `last-good` (*Its scope is R's
    changed set*), so no second image joins the soak Job. The script also holds Open
    question 19's run-time wait. It is `ansible/roles/k8s/manifests/files/flux_soak.py`,
    beside `flux_job_connect.py`, with its tests in that role's `tests/`.
    The Job script and its tests, from fixture API and query results, which no render emits
    yet:
    - an authelia pod that restarts after its first Ready fails the soak and names
      `authelia`; a restart before Ready passes; a restart in a pod older than T0 passes;
    - a restarting pod owned by a ReplicaSet that a Deployment owns fails the soak and names
      its service. This is the red half against a scope that drops Deployment pods. A 403 on
      the ReplicaSet read exits 1 naming the ReplicaSet, and a pod owned by a ReplicaSet
      with no Deployment owner is not read;
    - an in-scope pod with no service mapping fails; a Job-owned pod with no mapping passes;
      the soak's own pod is never read;
    - a critical alert active before T0 passes, and one active after T0 fails; a warning
      alert labelled `verify="true"` active after T0 fails; a critical alert labelled
      `shadow="true"` active after T0 passes; the Watchdog passes;
    - a critical alert with no `for`, firing before T0 and re-activated after a Prometheus
      pod created after T0, passes;
    - a Ready pod with no Ready sample fails, naming it; a restart inside a fixture scrape
      gap fails, naming the gap; a restart during the gate leg's wait fails;
    - a failure writes `failed-services:` to the termination message, naming each failing
      service, a snapshot role's service among them;
    - a critical alert after T0 on a service outside the changed set passes, and the same
      fixture fails when the base file does not verify, because the scope widens to the
      fleet. The red half is a Job that ignores the scope;
    - a 1-hour critical rule on an unchanged service leaves W at the changed services'
      value, and an empty changed set exits 0 at once without waiting;
    - a first connection that is refused twice and then answers passes;
    - the revision check reads the origin key. A fixture OCIRepository whose
      `artifact.revision` is `prod@sha256:<digest>` and whose
      `artifact.metadata["org.opencontainers.image.revision"]` is `master@sha1:<R>` passes.
      The same fixture with that key naming another commit exits 1. The first fixture is the
      red half for a check that reads `artifact.revision`.
17. **D5.3b2 — The soak Kustomization, observed** (about 140 lines). The `soak` renderer
    kind, the per-service map that W reads, the Job's RBAC, ReplicaSets included, and its
    ConfigMap. Open question 19's ruling puts these here too:
    - the renderer renders `soak` with no `dependsOn` edges;
    - the Job's Role grants `get` and `list` on Kustomizations in `flux-system`;
    - kustomize-controller's `--concurrent` gains one worker;
    - D4's timeout function changes, because `soak`'s timeout leaves the chain (*Its
      timeout*);
    - C2's rule that `soak` is in the changed set whenever the set holds a tag is in
      `contracts.md` before this PR merges;
    - `d4-ordering.md`'s *`K8S_APPLIES_LAST` in the renderer* bullet stops naming `soak` as
      a Kustomization that depends on every tag, because the renderer no longer gives
      `soak` edges.

    D5.3b1's script also fixes these inputs:
    - the flux role renders the OCIRepository `last-good`, with `homelab`'s `verify` block;
    - the ConfigMap carries `flux_soak.py`, `flux_job_connect.py` and `changed_set.py`;
    - the per-service map holds each service's longest `for:` and the namespaces its tag
      renders into;
    - the Job sets `SSL_CERT_FILE` to the ServiceAccount's `ca.crt`, and passes a
      `--wait-seconds` below `activeDeadlineSeconds`. `soak`'s footprint rule with R's SHA normalised, and its renderer test with
    the docs-only pair as described in *The soak as a Job*. `soak`
    joins `SHARED_KINDS` here,
    with a fixture showing that it passes the census. The PR quotes W's value. It also adds
    `verify`'s observe-only set holding `soak` (*The soak as a Job*). Tests in
    `scripts/deploy_tools/tests/`, from a fixture `sha-T` bundle that holds
    `flux/soak.yaml`:
    - every other status `success` and no `flux/soak` status moves `last-good`;
    - every other status `success` and a `flux/soak` `failure` moves `last-good` and writes
      no hold;
    - the red half is the same two fixtures with the set empty, which keep waiting and hold.

    Live check: two consecutive promotions post `flux/soak` `success`, each from a fresh Job
    pod, so a refused first connection would have shown. A `failure` in that period holds
    nothing. The session reads the failing services as *Choosing the branch* says, and fixes
    the soak before D5.3b3.
18. **D5.3b3 — `verify` waits on the soak** (about 40 lines). It deletes the observe-only set,
    so `promote.py verify` expects `flux/soak` whenever the changed set holds it. It merges
    only after the rulings in Open questions 17 and 19. The D5.3b2 tests flip: the fixture with no
    `flux/soak` status keeps waiting, and the one with a `flux/soak` `failure` holds. Its
    precondition, quoted in the PR body, is D5.3b2's two consecutive `success` statuses.
    Edit `renovate.json`'s descriptions that say "the deployer's health gate and rollback are
    its only check", keeping their `ENFORCED:` citations. **Operator-only:** decide whether
    ADR-0012 needs an amendment, now that its soak is a Job anchored on each pod's first
    Ready time.
    - **`land-wait` waits on the soak in the same PR** (*The soak as a Job*), which makes
      the PR about 90 lines. The deploy annotation (*The soak as a Job*) adds about 30
      more. Tests in `scripts/deploy_tools/tests/`, from fixture
      Kustomizations and fixture combined statuses:
        - every tag Ready at R and no `flux/soak` status keeps waiting. This is the red half
          against a `land-wait` that reads only the tags, which returns `settled` here;
        - every tag Ready at R and a `flux/soak` `pending` keeps waiting;
        - every tag Ready at R and a `flux/soak` `success` on R reads `settled`;
        - a `flux/soak` `failure` on R reads `deploy-failed` with the `Cause`
          `soak-failed`, before any `DEPLOY_HOLD` is set;
        - a `flux/soak` `success` on a commit other than R keeps waiting.
    - `land.sh --help`, `docs/landing.md` and D2's `land-wait` section in
      `docs/flux-deploy-plane.md` gain the `soak-failed` cause and the soak read in the
      same PR.
19. **D5.4a1 — Image contexts as a renderer output** (about 120 lines). `DIR/images/<name>/`,
    `DIR/images/tags.json`, and the test that a consumer's rendered ref equals `tags.json`.
    Lands after the C1 amendment (Open question 10).
20. **D5.4a2 — CI images** (about 150 lines). The `images` job and its content-tag test. Then
    **operator-only:** set the ten packages public.
21. **D5.4b — GHCR refs** (about 120 lines). The GHCR image refs for flux-owned consumers,
    `imagePullPolicy: IfNotPresent` on each, the census against `Always` on a GHCR ref, and
    the secret-in-context census. The PR edits the sentences that explain `Always` in
    `ansible/roles/k8s/nut/templates/deployment.yaml.j2` and
    `ansible/roles/k8s/registry/CLAUDE.md`, saying which refs still use it and why.
22. **D5.5 to D5.7 — Wave A**, about three roles per PR, about 60 lines each.
23. **D5.8 — Wave A'**, loki-homelab, navidrome and scrutiny together, about 50 lines.
    Then wg-easy in its own window, about 20 lines, with the LAN precondition.
24. **D5.9 to D5.13 — Wave B.**
    - The four plain roles in one PR (about 80 lines).
    - The speedtest revert drill: the breaking PR, then the runbook, then the revert, as in
      D2.8 and D2.9. One line each.
    - zigbee2mqtt (about 40 lines), then home-assistant (about 60 lines). home-assistant also
      needs D3's SOPS manifest for `google_assistant_service_account` and its `decryption`
      setting.
25. **D5.14 to D5.18 — Wave C**, two to five roles per PR (*Flip PR size*). nut is the UPS
    primary and gets its own PR. n8n and pi-peer-backup wait on R3. pi-peer-backup also needs
    D3's SOPS manifest. n8n's `n8n-netpol-probe` becomes `n8n-verify`. pi-peer-backup's Pi
    writes move to `k8s_host_files`, which D5.1h has created, and its `cronjob-gate` Job
    becomes the gate Job in `pi-peer-backup-verify`, a named member of that PR with the
    `pull-pi-peers.sh` pattern test. **Each Wave C PR's precondition:** an anonymous token from
    `https://ghcr.io/token` for `repository:danielh2018/<name>:pull`, then a manifest GET of
    the exact content tag, returns 200 on daniel-box and on daniel-server. nut's PR also waits
    on Open question 2's ruling. If the mirror is kept, a manifest GET of nut's content tag
    through the mirror on daniel-server returns 200 first.
26. **D5.19 onwards — Wave D**, in the order of the wave table, three to five roles per flip PR
    (*Flip PR size*, below).
    - **Two readings on the first Wave D revisions.** The session re-takes D4's
      worker-occupancy reading and tunes the 30-minute window of D4.9's
      `FluxWorkersSaturated` from it. It also measures how long `verify` runs on the first
      broad revision, the number D4 Open question 14 waits on. The next Wave D PR's body
      quotes both.
    - **tdarr's pre-pull moves to Flux.** tdarr's conversion PR replaces its pre-pull task
      with a pre-pull that Flux applies, so tdarr's `timeout` drops from 3000 s to the
      600 s default and a stuck tdarr holds a worker for at most 10 minutes. Open question
      21 asks where that pre-pull lives.
    - **Conversion PRs first**, while Ansible still owns the role. Each replaces one group of
      tasks with rendered objects or `k8s_host_files` tasks that Ansible applies, at about
      100 to 200 lines. Every role over 200 task lines gets at least two, and observability,
      pihole and crowdsec get three or more.
    - **The run-once cleanup PR** (#5284) deletes game-stats' and observability's record
      cleanups and retires #5162's census row, about 140 lines removed (*Host-side rows*).
      It may land before Wave D starts, and it lands before either role flips.
    - **H2's PR precedes the flip of six roles.** crowdsec, configarr, janitorr, qbittorrent,
      artifacts and observability each flip only after H2.1 and that role's H2 family PR have
      landed (*Host-side rows*). H2 lands those PRs while the roles are Ansible-owned, so D5's
      conversion PRs for the same role rebase onto them.
    - **Then the `k8s_host_files` conversion PR, for five of them.** crowdsec, configarr,
      janitorr and qbittorrent move the absent arms their H2 PR left, and artifacts moves all
      three of its directory tasks, the star list directory included, each into the role's task file in `k8s_host_files`, about 40 to 80
      lines each. The PR drops the role's `allow` entry, and artifacts' PR drops its
      `must_find` member. The tick applies it through the setup plane, and the PR body quotes
      that apply. observability needs none, because H2.7 leaves it no host row and the
      run-once cleanup PR deletes its claude-otel record cleanup (*Host-side rows*).
    - **The flip PR's body** links the merged H2 PR and, for those five, the merged
      `k8s_host_files` conversion PR, and quotes the host-write census showing no `allow`
      entry left for the role. observability's and game-stats' flip PRs also link the
      merged run-once cleanup PR.
    - **A verify conversion lands as inert code.** The task files run 103 to 213 lines
      (`wc -l` over the six `tasks/verify.yml`), and netpol-baseline's probe template is 161.
      So a conversion PR lands each verify Job's script, its RBAC template and its red-fixture
      tests, which no render emits while the tag is Ansible-owned. A converted metric rule
      also lands in a conversion PR, as the class table says.
    - **A verify or mutation Job's fence peers land before its flip.** Each conversion PR
      quotes the Job's caller census (*What every Job carries*). Its peers land in
      netpol-baseline, or in the role-owned fence, in a PR that `land.sh` deploys before the
      flip, each with a `netpol_baseline_probe_targets` row whose refused leg shows it can go
      red. An exec that remains carries its `pods/exec` grant as a stated, operator-reviewed
      trade. The named members per PR:
        - sonarr: `sonarr-verify` joins sonarr's `netpol_from`, for the verify reads and the
          arr-notification seed; radarr: `radarr-verify`, for the seed;
        - crowdsec: `crowdsec-verify` joins the LAPI fence in `networkpolicy-crowdsec.yaml.j2`
          and the `homelab` peers of `networkpolicy-prometheus.yaml.j2`; the machine
          registration and allowlists become the LAPI initContainer;
        - observability: the Grafana admin initContainer and the restart annotations
          (D5.1e);
        - janitorr, jellyfin, qbittorrent and tdarr: each exec check, converted or granted;
        - configarr: the gate Job, which keeps `app: configarr` and so sonarr's existing
          admission.
    - **Then the flip PR.** It wires `<tag>-verify` to the inert Job, deletes the converted
      tasks, and flips the entry. Each flip PR states its own estimate. Most are 40 to 80
      lines plus the deleted tasks. A role whose deletions alone pass 200 lines, such as
      qbittorrent, jellyfin or crowdsec, deletes its task files in a follow-up PR after the
      flip, which is safe because Ansible no longer reaches a flux-owned role.
    - authelia: the precondition is that D3's cluster file holds R3's three argon2 keys,
      written by the operator with `cluster_vars.py write` and listed by `cluster_vars.py
      check`. `cluster_vars.py check` (operator-run) also lists D3's SOPS manifest for
      `authelia_oidc_rsa_key_content`, and the role's Kustomization renders `decryption`. A
      test in `scripts/renderer/tests/` names authelia as a member of the tags that render
      `decryption`. The TOTP seed becomes an initContainer, which makes
      `authelia_claude_totp_secret` a substituted key, so `cluster_vars.py check` lists it
      too.
        - **The asserts' successors are merged first.** Authelia's Ansible asserts stop
          running once Flux renders authelia, so the flip also requires two D3 refusals in
          `cluster_vars.py write`. One refuses `{{` in any value, because authelia's
          template filter parses the whole file and would fail to start on it. The other
          refuses a malformed or duplicated digest in the three `_argon2` keys, by R3's
          predicate, because a bad digest locks every user out of SSO. Each refusal names
          the key and never the value, and each has a fixture test in D3. The flip PR body
          quotes the D3 PR that merged them.
        - **The rotation procedure moves to Flux.** R3's PR 4 writes a six-step rotation
          procedure into `docs/secret-rotation.md`. Its step 4 lands a rotation PR with
          `land.sh --tags authelia`, because a secrets-only PR derives no deploy tag: at
          `70591c307`, `land_tags.py:plane_note` appends `_ROTATION_NOTE`, and
          `land_lib/deploy.py:no_tag_outcome` ends the landing with nothing deployed. The
          flip PR rewrites step 4 to the Flux path, where a merged D3 cluster file reaches
          the live Secret, as D3's exit criterion 3 shows for peanut. Step 5 keeps its
          order: the `claude_code` apply runs only after authelia has converged.
    - mosquitto: the precondition is D3's `cluster_vars.py write` refusal of `:` in
      `mqtt_password_hash`, merged before the flip. R2's colon-count assert stops running once
      Flux renders mosquitto. Without the refusal, a value holding `:` would reach the
      password file with its prefix stripped, and every MQTT client would fail to log in.
    - qbittorrent: the flip has three preconditions, because R2 PR 7b moves `mullvad_account`
      out of qbittorrent's Secret into the dedicated SOPS Secret `qbittorrent-mullvad-account`,
      the fourth `render_sops_manifests` entry (R2 *`mullvad_account` becomes a dedicated
      Secret*). The flip PR body quotes all three.
        - **R2 PR 7b is merged**, so no placeholder carries `mullvad_account` and R2's
          `GUARD_DEFERRED` is empty (R2's PR 9 later deleted the set). The flip PR body
          links the merged PR.
        - **D3's SOPS manifest and `decryption`.** `cluster_vars.py check` (operator-run)
          lists D3's SOPS manifest for `mullvad_account`, and qbittorrent's Kustomization
          renders `decryption`, as for home-assistant, pi-peer-backup and authelia. A test in
          `scripts/renderer/tests/` names qbittorrent as a member of the tags that render
          `decryption`.
        - **A green run of D3.3d's `sops-manifest-type-proof` job.** The run shows that an
          all-digit value in a quoted `stringData` field of a SOPS-encrypted Secret applies
          as a string. The flip PR body links the run. If the job is red, D3's `write` emits
          the value under base64 `data:` first, and the flip waits on the D3 PR that does so
          (R2 *`mullvad_account` becomes a dedicated Secret*). The API server refuses an
          int as a `stringData` value, so a flip without either could leave qbittorrent's
          Kustomization unable to apply.
    - registry: the self-test and netpol probe Jobs become `registry-verify`. **The GC is
      retired, not moved,** in a conversion PR while registry is still Ansible-owned. Its
      script scales `deploy/registry`, whose `replicas: 1` Flux renders, so under Flux the
      drift correction would restore the replica during a GC run, the corruption the script's
      "STOP-THE-WORLD" comment exists to prevent. By Wave D every image consumer pulls from
      GHCR, so no build pushes to the registry, and nothing new needs collecting. The
      self-test copies a small `alpine:3.22` image, so the store gains one copy per upstream
      rebuild of that tag. The PR:
        - sets the existing lever `registry_k8s_manage_gc: false`. Its off arm removes only
          `gc-job.yaml` today, so the PR extends it with absent arms for the `registry-gc`
          cron file, `/usr/local/bin/registry-gc.sh` and `/var/lib/registry-gc`. Those arms
          move to `k8s_host_files` with the other host rows, and flipping the lever back is
          the way to re-arm GC while registry is Ansible-owned;
        - removes the Kuma `Registry GC` tile and its group rule in uptime-kuma, the
          `registry-gc` healthchecks.io entry in monitor-bridge's defaults, the
          `registry_gc_push_token` entry in `ansible/secret_rotation.yml`, and the cron in
          `scripts/validate/validate_lib/cron_targets.py`, with the tests that cite them;
        - removes whichever registry-gc monitor exists at its base. M6's B6 also lists
          registry-gc, and a cross-slice note asks M6 to drop it. If B6 merged first anyway,
          this PR also removes the textfile rule and timer B6 built for it. The PR body
          quotes `grep -rn 'registry.gc' ansible/roles/k8s/uptime-kuma
          ansible/roles/k8s/monitor-bridge ansible/roles/k8s/observability
          ansible/secret_rotation.yml` printing nothing;
        - deletes the check at healthchecks.io, **operator-only** if no code manages it;
        - states the precondition, quoted: no `image-builder` caller is Ansible-owned;
        - quotes its live check after `land.sh` deploys it: `ls /usr/local/bin/registry-gc.sh
          /var/lib/registry-gc` on daniel-box reports both missing. The cron file sits in
          `/etc/cron.d`, which only root can list, so the operator confirms it is gone
          (**operator-only**).

      **D5 is the only owner of registry GC** (Open question 15). H2.6a and H2.6b would move
      the GC into an orchestrator CronJob that scales the same Deployment. Under Flux that is
      a second writer on `spec.replicas`, with the same drift-correction hazard as the host
      cron. So H2 drops both PRs and the `registry-gc` row from its exit list, and the host
      cron runs as today until this PR retires it. registry's flip therefore waits on no H2
      PR. Open question 2 rules on the registry's own fate before nut's Wave C PR.
    - traefik: the CRDs come from a URL in the role's `tasks/main.yml`, which no render may
      fetch under C1, so they get an in-tree copy pinned by `traefik_k8s_crd_version`. The
      cluster RBAC carries `kustomize.toolkit.fluxcd.io/prune: disabled` before the move, so
      a later revert of the move cannot prune the ClusterRole (R4's Open question 4).
        - **The RBAC moves into the bundle at the flip.** `rbac.yaml.j2` is not in traefik's
          `manifests_files`. Its task templates it to
          `/etc/rancher/k3s/traefik-clusterrole.yaml`, outside the manifests directory, and
          a second task applies that file. The file holds Roles in other namespaces, so the
          move cannot happen while Ansible's namespaced apply owns the role. The flip PR
          renders `rbac.yaml.j2` into traefik's bundle and deletes both tasks, which drops
          the last clause of traefik's host-write `allow` entry. The staged file stays on
          daniel-box with no reader, which is accepted. It is root-owned, holds no secret,
          and k3s auto-applies only `/var/lib/rancher/k3s/server/manifests/`, so it changes
          nothing.
        - **traefik's tag renders `prune: false`.** The flip PR is the first whose render puts
          the CRDs under a Flux Kustomization. It sets H1.1a's `flux_prune: false` in
          traefik's defaults, which is C3's per-role `prune: false`. Its precondition, quoted
          in the PR body, is that H1.1a has merged. Under `prune: true`, a render that
          dropped a CRD file would delete that CRD, for example a `traefik_k8s_crd_version`
          bump whose upstream definitions drop a kind. The API server then deletes every
          IngressRoute or Middleware of that kind, and the edge goes down.
          `deletionPolicy` does not help: it governs deleting the Kustomization, not pruning
          an object the render dropped.
        - **The CRD census.** The flip PR adds a renderer test that fails on a
          CustomResourceDefinition in any tag whose Kustomization renders `prune: true`.
          traefik is a named member that the test must find carrying CRDs and `prune: false`.
          The red fixture is traefik's render with `flux_prune` removed. This is D6.19's CRD
          guard landing early, without D6's exception for `flux`.
        - **The rejected alternative** is a `kustomize.toolkit.fluxcd.io/prune: disabled`
          label on every rendered CRD. It would keep pruning for traefik's other objects.
          But D6.19's guard keys on the tag's `prune` value, so traefik's tag would fail it.
        - **The cost:** an object dropped from traefik's render, such as a retired
          Middleware, stays live until the operator deletes it (**operator-only**). The
          same cost holds for H1's five plumbing tags.
    - crowdsec: its first conversion PR lands the Pi timer, the Alloy metrics stage and the
      inert `crowdsec-verify` Job. Its flip wires the Job.
    - netpol-baseline's PR repeats D4's drill on itself. A workload change that fails leaves
      the new policies unapplied, which is D4's exit criterion on the real policies-first
      edge. Its probe Job becomes `netpol-baseline-verify`, which depends on
      `netpol-baseline-deferred`.
    - pihole's PR exercises `pihole-deferred` live: instance 2 applies only after instance 1
      is Ready.
    - **Before the first Wave D flip**, the session measures one revision that changes at
      least ten flux-owned tags, from `prod`'s move to the last status, and compares it with
      the derived verify timeout. A measurement above half the timeout raises the timeout
      first.
27. **The render-records disarm PR** (about 30 lines). It lands after the last Wave D flip and
    before the flux PR, while `render_targets.py daniel-box` prints `flux` alone, so the
    producer never runs on an empty list (R4's Open question 5). The PR body quotes that
    output.
    - Replace the derivation in `ansible/roles/setup/render_records/defaults/main.yml` with
      `render_records_state: absent`, the lever R4 names. The role's `CLAUDE.md` says that this
      default pins the role absent until R4 deletes it, whatever `render_records_enabled`
      says. The PR leaves `group_vars/all.yml` alone, because any change there routes to the
      deploy plane.
    - Remove the `render-records.json` tile from uptime-kuma's `static-monitors.yaml.j2`, whose
      comment says the tile stays red until the producer is re-armed, and its
      `^render-records$` group rule from uptime-kuma's defaults. If M6 has retired Kuma, there
      is no tile to remove.
    - **The route, quoted in the PR body.** `services_from_changed_paths` over the role
      defaults, the role `CLAUDE.md` and the two uptime-kuma paths gives `{'broad': True,
      'broad_setup': True, 'setup_roles': {'render_records'}, 'k8s': {'uptime-kuma'}}` at
      `70591c307`. The tick applies `initial_setup.yml --tags render_records`, and the absent
      arm stops the timer and removes the units. uptime-kuma is flux-owned by then, so the
      deployer's k8s arms skip it (C4), and `promote` applies the tile's removal. `land.sh`
      follows both.
    - **Live check.** After the tick, `systemctl list-unit-files 'kuma-check-render-records*'`
      lists no unit, as exit 1 reads it.
28. **The bootstrap drill PR** (about 190 lines). It lands before the flux PR, because the
    flux PR removes the only path that installs Flux on a rebuilt cluster. D1's flux role
    applies `cluster/flux/` from its own task. Once `flux` is `owner: flux`, the Ansible k8s
    play, `deploy.sh`, the deployer's k8s arms and `land.sh` all skip it (C4), and
    `deploy.sh --tags flux` exits 5 (D2). This PR carries the procedure that the flux PR
    writes into `docs/flux-deploy-plane.md` as *Bootstrapping Flux on a rebuilt cluster*.
    **That section is the single owner of the bring-up and DR order from the flux PR on.**
    H1's *Bring-up and DR order after H1* and R4's *Bootstrapping Flux without Ansible* cite
    it rather than keep their own copy. Until the flux PR merges, H1's order, with D1's
    Ansible role, stays in force. The flux PR writes four parts of this step into the
    section: the procedure's steps 1 to 9, the field-manager paragraph below them, the
    interim `./scripts/deploy.sh` step that the namespace PR deletes, and *Why the root is
    withheld, not suspended* in the words given here. The other bullets of this step stay
    in this plan, in the renderer tests and in the drill workflow.
    - **The procedure**, all **operator-only**. It starts once H1's steps 1 to 3 have run:
      k3s is installed, `cluster-base` is applied under kustomize-controller's field manager,
      and the read-only token exists. Then:
        1. Confirm that `prod`'s `longhorn-backup` render arms both backup targets, as H1's
           step 6 says. If an arm PR must merge, it merges and promotes now, and the hold
           its verify leaves is cleared in step 9.
        2. Settle the hold, then choose the revision. The root applies whatever `prod`
           names and adopts what the operator applied by hand, so every document below is
           rendered at `prod`'s commit. A reset moves `prod`, so the hold comes first, read
           with `gh variable get DEPLOY_HOLD -R DanielH2018/server`:
            - **No hold.** When it reads `none`, the operator waits until no run of the
              `promote` workflow is queued or in progress. Then the operator sets the DR
              hold with `gh variable set DEPLOY_HOLD --body dr-restore -R
              DanielH2018/server`. Without it, a merge during the restores would promote.
              Its `verify` would time out, because no Kustomization can post before step 9,
              and the failure path would reset `prod`. The OCIRepository would then serve a
              commit other than the one rendered here.
            - **A hold that step 1 did not leave.** A promotion failed or timed out before
              the rebuild, so `prod` names `last-good` or the held revision R. The hold
              stays until step 9, because it already stops `decide`. The operator may
              dispatch `reset_to_last_good`, which moves only a GHCR tag and makes `prod`
              name `last-good`'s commit, **only when no tag changed between `last-good` and
              `prod` is a snapshot role or a forward-only tag.** A tag has changed when its
              `own` digest in `digests.json` differs between the two bundles, or when only one
              bundle holds it (*The stateful hold*, and H1's *The rule*). A failure hold's
              post lists the changed tags and marks each snapshot role (D5.2b1). H1 names
              the forward-only tags.
            - **The one exception** is the compatibility judgment that Branch 1 already
              requires, recorded in the hold's thread before the dispatch. For a snapshot
              role, the operator records that the data R wrote suits `last-good`'s image.
              For a forward-only tag, the operator records that the downgrade is safe on a
              fresh cluster with the restored backups. Without that record, the render
              stays at the held `prod`.
            - **Why the rule.** Step 8 restores the newest backups, which R's image may
              already have written, and step 9's root applies whatever `prod` names. A
              reset across a changed snapshot role would apply `last-good`'s image onto data
              R may have migrated (*The stateful hold*). contracts.md's *The forward-only
              hold* row forbids a reset across a forward-only tag for the same reason.

           Once the hold is settled, the operator reads `prod`'s commit without the
           cluster, from the `org.opencontainers.image.revision` key of `prod`'s manifest in
           GHCR (C2).
            - **Offline branch.** When GitHub or GHCR cannot be reached, steps 1 and 2
              cannot run. The operator follows C2's break-glass promotion instead, and D6
              owns that runbook and its order against steps 3 to 9. The render is `homelab-render cluster` in placeholder mode at that
           commit, with `flux_verify_drill` false.
        3. Apply `cluster/flux/`, the Flux components (D1).
        4. Create `flux-system/sops-age` from the cluster age key in the password manager,
           with D3.0's command.
        5. From `cluster/flux-deferred/`, apply the OCIRepository `homelab` and nothing else.
           **The root Kustomization is withheld until step 9.** While it does not exist,
           nothing applies a stateful tag, so no PVC is provisioned under a backed-up name.
        6. Apply the child Kustomizations the restores need: the `flux/<tag>.yaml` of each
           plumbing tag and of every Kustomization in their `dependsOn` closure,
           `flux/cluster-vars.yaml`, and, from the namespace PR on,
           `flux/homelab-namespace.yaml`. The closure never holds `flux-deferred` or the
           root, because `flux-deferred` applies the root. A renderer test fails when a
           plumbing tag's `dependsOn` closure reaches either. Wait until each reads `Ready`. Then run the agent
           join and the storage smoke test, which both need Longhorn.
        7. Force the backupstore sync and wait for both targets to read available, as
           `docs/longhorn-disaster-recovery.md`'s step 3 says.
        8. Run `uv run python scripts/deploy_tools/runbook_gates.py longhorn-dr`. Then
           restore each volume and create its PV and PVC, as that document's step 4 says.
        9. Apply the root Kustomization `homelab` from `cluster/flux-deferred/`. It applies
           every Kustomization at `prod` and adopts the ones step 6 applied, and the stateful
           tags bind the restored claims. Once every status on `prod`'s commit reads
           `success`, clear the hold, whether step 1 left it, step 2 set the DR value or
           step 2 kept a failure hold, with
           `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`. If a status
           fails, the hold stays, and the operator picks a branch by the revert runbook's
           *Choosing the branch* (D5.2c).

      Every hand apply in steps 3, 5, 6 and 9 is `sudo k3s kubectl apply --server-side
      --field-manager=kustomize-controller` on the rendered file. That is the root's own
      field manager, so the root and the `flux` tag adopt each object without a conflict.
    - **Why the root is withheld, not suspended.** H1's order applies the root suspended and
      resumes it after the restores. Here the root comes from a hand apply of its render,
      which carries no `spec.suspend`: the renderer refuses a suspended sync render from
      the flux PR on, and it never renders `suspend: false` (D5.1b). A root applied and then
      suspended with the `homelab-operator` patch could reconcile between the two commands
      and provision empty PVCs under the backed-up names. A root that does not exist cannot.
      Step 9's apply is the resume. From the flux PR on, the order needs no D1 role
      variable. Before the flux PR, H1's order runs D1's role, and a cross-slice note to H1
      gives that window a form without a new variable.
    - **The renderer test** that the documents applied by hand hold no `${` and no `$$`,
      with R4's red fixture: an OCIRepository whose URL holds a placeholder.
    - **The drill**, C8's throwaway kind cluster in a CI job, with R4's four substitutions:
      a throwaway age keypair, a fixture bundle, a registry beside the cluster, and an
      OCIRepository patched to that registry without its `verify` block. It runs against
      the pinned Flux components that D1 renders, in the procedure's order. The fixture
      bundle holds a plumbing fixture tag that step 6 applies and a stateful fixture tag
      that only the root applies. The stateful tag renders one claim and one ConfigMap.
      Step 8's stand-in creates that claim from the fixture's own render, with a marker
      label, before step 9.
        - **Where the sync objects come from at this PR's head.** `flux` is still
          Ansible-owned here, so `flux/flux.yaml` holds no `flux-deferred` Kustomization
          (D4, *The `-deferred` rule keys on `manifests_deferred_files`*). The directory
          is a different output. C1's *Cluster output* writes `DIR/cluster/<tag>-deferred/`
          for every role that passes `manifests_deferred_files`, with no owner condition,
          and only the publish job selects tags by owner (D2, *Publication and
          verification*). So the drill renders with `homelab-render cluster --role flux`
          beside the fixture tags, and `cluster/flux-deferred/` holds the OCIRepository
          and the root that steps 5 and 9 apply. No step applies the `flux-deferred`
          Kustomization by hand. A renderer test in this PR renders the Ansible-owned
          `flux` role and fails unless `cluster/flux-deferred/` holds the OCIRepository
          `homelab` and the Kustomization `homelab`, so a renderer that gated the
          directory on the owner would fail CI here. The flux PR's precondition is a green
          run on its own head, where the render also carries the `flux-deferred`
          Kustomization.
        - The green job checks the stateful tag twice. Before step 9, the job requires
          that its Kustomization is NotFound and its ConfigMap is NotFound. It also requires
          that the claim carries the marker label and no `kustomize.toolkit.fluxcd.io/name`
          label, because only the stand-in has written it. After step 9, it requires that
          the Kustomization reads Ready, the ConfigMap exists, and the claim keeps the
          marker label and the stand-in's UID while carrying
          `kustomize.toolkit.fluxcd.io/name` set to the stateful tag. The job also requires
          that the plumbing Kustomization is Ready and carries the root's ownership labels,
          and that `homelab-cluster-vars` holds the fixture key.
        - A second job skips the `sops-age` step and passes only when `cluster-vars` fails
          to decrypt.
        - A third job applies the root before the stand-in, and passes only when the claim
          check fails, because the root created the claim first.

      It triggers on `workflow_dispatch` and on a path-filtered `pull_request`, so it runs
      on this PR before it merges. The operator pushes the workflow file.
    - **Why kind suffices** (C8). Every step acts on the Kubernetes API and the pinned Flux
      controllers. Decryption runs inside kustomize-controller. The stateful fixture's claim
      uses kind's default StorageClass, so the drill proves the order, not a Longhorn
      restore. No fixture needs a LoadBalancer or a node label.
    - The PR changes no runbook, because Ansible still installs Flux until the flux PR.
      Its description quotes the run's three jobs.
29. **The flux PR** (about 130 lines). Its preconditions are the late-arrivals check under
    *Waves and windows* and a green run of the bootstrap drill on the flux PR itself. It also
    waits on H1.7b (*Depends on*).
    - Flip the flux role. Its components and sync objects then come from the artifact, so
      Flux reconciles itself, as `flux bootstrap` would arrange. `flux-deferred` applies the
      sync objects, the root among them, and the root applies `flux-deferred`. `sops-age`
      stays operator-created bootstrap material.
    - The flux role, which D1 creates, sets `flux_prune: false`, so the `flux` tag renders
      `prune: false`. Its components carry Flux's own CRDs, and a controller upgrade whose
      components drop a CRD would otherwise delete that CRD and every object of its kind.
      `deletionPolicy: Orphan` on `flux` covers only a
      deletion of the Kustomization itself. `flux` joins the CRD census's named members.
      The cost is the same as traefik's: an object a later upgrade drops stays until the
      operator deletes it.
    - The prune of `flux-deferred` succeeds D1's task that deletes
      `ocirepository/verify-drill` while `flux_verify_drill` is false, as R4's PR 1 maps that
      row. `flux_prune: false` therefore renders `prune: false` on the `flux` Kustomization
      only, and `flux-deferred` keeps `prune: true`, by H1's rule for a role's prune field. A
      renderer test asserts both fields, and the PR body quotes the `prune` field of the
      rendered `flux/flux-deferred.yaml`. If it reads `false`, the row has no successor and a
      `verify-drill` object outlives its drill, so the PR does not merge.
    - The renderer refuses `flux_sync_suspend: true` from here on. Flux would apply the
      suspend to its own source and could never fetch the commit that clears it. Stopping
      everything becomes a hand suspend in the recovery runbook, with its resume beside it.
    - A renderer test pins the sync objects' URL, `ref` and `verify` block to D1's values, so
      a change to them fails CI rather than cutting Flux off from its source.
    - Edit the comment above `render_records_enabled` in `ansible/inventory/group_vars/all.yml`
      to say that the role default pins the producer absent (the disarm PR). The flip already
      changes `ansible/inventory/host_vars/daniel-box.yml`, which routes to the deploy plane
      (`{'broad': True, 'broad_deploy': True}`), so the comment adds no apply. The PR body
      quotes the route for its changed paths.
    - `docs/flux-deploy-plane.md` gains the recovery runbook, all **operator-only** because it
      needs `sudo k3s kubectl`. Every suspend and resume in it uses
      `--field-manager=homelab-operator` (D5.1b):
        - a broken controller upgrade. The restored controller would re-apply the broken
          components from `prod` at its next reconcile of `flux`. So the branch has the same
          shape as the broken-sync-object branch, in this order:
            1. Set `DEPLOY_HOLD` with `gh variable set DEPLOY_HOLD --body <reason> -R
               DanielH2018/server`, so no promotion moves `prod` past the reset.
            2. Suspend `flux` with `--field-manager=homelab-operator`. The suspend survives
               the root's reconcile (D5.1b). The source can still hold R's artifact after the
               reset, so without the suspend the restored kustomize-controller could apply
               R's components again.
            3. Dispatch `reset_to_last_good`. If it refuses because a verification is in
               flight, cancel the run it names with `gh run cancel <run-id> -R
               DanielH2018/server`. A broken Flux posts no statuses, so waiting would last
               the whole derived verify timeout. That run's `settle` still runs: it writes
               `<T> unverified` over the reason from step 1 and resets `prod` itself. Read
               `last-good` again, then dispatch again (*A reset input*).
            4. Apply the components rendered at `last-good` with `sudo k3s kubectl apply
               --server-side --force-conflicts --field-manager=kubectl -f <file>`, for the
               reasons the broken-sync-object branch below gives.
            5. Wait until the controllers are Ready and the OCIRepository's
               `org.opencontainers.image.revision` key reads `master@sha1:<last-good SHA>`,
               as Branch 1's step 3 reads it. A broken source-controller cannot report
               before step 4, so this wait comes after the apply.
            6. Resume `flux` with the same patch and `false`. Then land the revert of the
               upgrade, clear `DEPLOY_HOLD` with `gh variable set DEPLOY_HOLD --body none
               -R DanielH2018/server` and dispatch `promote`, by D2's procedure;
        - stop everything: suspend the OCIRepository `homelab`. Every Kustomization reads its
          revision from that one source, so none gets a new revision. Each still corrects drift
          at its interval against the revision it holds. Suspending the root alone would not
          stop anything, because each child reconciles its own source revision. The resume is
          the same patch with `false`;
        - a broken sync object, which leaves Flux unable to fetch: set `DEPLOY_HOLD`
          with `gh variable set DEPLOY_HOLD --body <reason> -R DanielH2018/server`, so no
          promotion moves `prod` past the reset, then suspend `flux-deferred`, which applies
          the sync objects, and dispatch `reset_to_last_good`. A refusal that names a
          verification in flight takes step 3's cancel in the branch above. Then apply the
          sync objects rendered at `last-good` with `sudo k3s kubectl apply --server-side
          --force-conflicts --field-manager=kubectl -f <file>`. Wait until the OCIRepository
          is Ready and its `org.opencontainers.image.revision` key reads
          `master@sha1:<last-good SHA>`, then resume `flux-deferred`.
            - **Why `--force-conflicts`.** kustomize-controller's own Apply entry owns every
              field of the sync objects (Baseline). A server-side apply by another manager
              that sets a different value on an owned field is refused as a conflict unless
              it is forced. The broken fields, such as the OCIRepository's URL or `ref`, are
              exactly those fields.
            - **Why the manager `kubectl`.** The apply names that manager explicitly, so the
              Apply entry it creates matches the cleanup's `kubectl` (Apply) entry by
              construction rather than by kubectl's default. Upstream's comment on that entry
              reads "to undo changes made with 'kubectl apply --server-side
              --force-conflicts'". So Flux's cleanup takes those fields back at
              `flux-deferred`'s first reconcile after the resume. Under `homelab-operator`,
              an Apply owner would stay on every field, and a later commit that removed one
              would never take effect.
    - **A text test over the recovery runbook**, in `ansible/tests/repo/`, reads only that
      runbook's section of `docs/flux-deploy-plane.md`, bounded by its heading, because the
      bootstrap section's applies carry no `--force-conflicts`. It fails on a suspend
      or a resume without `--field-manager=homelab-operator`, and on a `kubectl apply
      --server-side` without `--force-conflicts`. In each of the two hand-apply branches it
      also fails when the apply precedes the `DEPLOY_HOLD` step. Its red fixtures are each
      branch with the hold moved after the apply, and the sync-object branch with
      `--force-conflicts` removed.
    - **The cold-start runbooks move to the bootstrap.** `docs/flux-deploy-plane.md` gains
      *Bootstrapping Flux on a rebuilt cluster*, the bootstrap drill PR's procedure, all
      **operator-only**. Three runbooks stop sending the operator to `./scripts/deploy.sh`
      for the cluster, because from this PR on it deploys no workload there:
        - `docs/break-glass.md`, *daniel-box is dead*, step 4, *Deploy*;
        - `docs/longhorn-disaster-recovery.md`, *Procedure (fresh host, total loss)*,
          step 5, *Deploy*;
        - `docs/k3s-etcd-restore.md`, *After the restore*, the step that redeploys from
          Ansible.

      This PR edits all three, and it replaces whatever bring-up order each holds at its
      base, H1's included, with a link to the bootstrap section. Neither H1 nor R4 edits
      them after it. The Longhorn ordering stays: volumes are restored in step 8, before
      step 9 applies the root, which provisions every PVC. Until the namespace PR lands, the `homelab`
      Namespace and its LimitRange still come only from the k8s play's pre_tasks. So until
      then the bootstrap section first runs `./scripts/deploy.sh` and checks, with the
      read-only `kubectl get namespace homelab` and `kubectl -n homelab get limitrange`,
      that both exist. The namespace PR deletes that step.
    - **A text test over the bootstrap section**, in `ansible/tests/repo/`, reads only that
      section, bounded by its heading, and fails when the
      root's apply comes before the restore step, or when a hand apply lacks
      `--field-manager=kustomize-controller`. Its red fixture is the section with step 9
      moved before step 7. It also fails when step 2 names `reset_to_last_good` without the
      condition "only when no tag changed between `last-good` and `prod` is a snapshot role
      or a forward-only tag" and the recorded-judgment exception beside it. Its red fixture
      for that clause is the section with the condition deleted from step 2.
    - **A text test over the three runbooks**, in `ansible/tests/repo/`, fails when a cold
      start step names `./scripts/deploy.sh` without the bootstrap section's link. Its red
      fixture is `docs/break-glass.md`'s *daniel-box is dead* scenario as it stands at
      `70591c307`.
    - **After it lands**, the speedtest revert drill runs again, as D5.2c describes. That
      drill is exit criterion 6.
30. **The namespace PR** (about 170 lines). Its precondition is the late-arrivals check
    under *Waves and windows*.
    - **The bootstrap section loses its `./scripts/deploy.sh` step** in the same PR,
      because `homelab-namespace` now creates the Namespace and its LimitRange once the
      root reconciles.
    - **The bootstrap section's text test gains R4's PR 7b check.** The test carries R4's
      `awk` program verbatim, runs it over `docs/flux-deploy-plane.md`, and fails unless it
      exits 0 with empty output. The clause lands here, not in the flux PR, because the
      flux PR's section still holds the interim `./scripts/deploy.sh` step, which R4's red
      half relies on. Its red fixtures are the section as the flux PR writes it, and a copy
      whose rationale names the refused `flux_sync_suspend` key with the value `true`.
    - **Delete "Build the workload namespace manifest" and "Apply the workload namespace"**
      from `ansible/deploy.yml`, in the commit that adds the non-tag Kustomization
      `homelab-namespace`, which joins `SHARED_KINDS` here. From that commit on, Flux is the
      only writer of the Namespace and its LimitRange. A gate on role ownership, such as
      `containers_list | ansible_owned | length > 0`, would not hold that: every role PR's
      rollback is D2's handback, and one handback after this PR would re-arm Ansible's
      client-side apply of the Namespace and its LimitRange beside `homelab-namespace`, with
      no end to the overlap. Deleting the tasks leaves no gate a handback can open. A
      handed-back role still deploys, because the Namespace exists and Flux keeps it.
    - `homelab-namespace` renders the Namespace with `k8s_psa_labels` and the LimitRange from
      `k8s_default_limitrange`, the same inputs the deleted task read. Every flux-owned tag
      that renders into `homelab` gains a `dependsOn` on it.
    - **The tests that read the deleted tasks move with them** (Baseline):
      `test_namespace_resource_defaults.py:_homelab_ns_items` reads the rendered
      `homelab-namespace` instead, so the LimitRange checks, `type: Container` among them,
      keep their subject; `test_both_namespaces_carry_the_psa_labels` reads the same render;
      `test_namespace_apply_is_guarded`,
      `test_namespace_apply_reports_a_label_change_as_changed`,
      `test_the_namespace_is_applied_before_its_limitrange` and
      `test_the_namespace_apply_reports_changed_per_line` are deleted, because their subject
      is gone.
    - "Tear down retired k8s services" keeps its gate, and it serves only a service that was
      Ansible-owned on the PR's base. From this PR on, every entry is flux-owned, so a
      service retired before D6.20 retires by D2's two PRs: the first empties its templates,
      so Flux prunes its objects, and the second removes its entry. D5.1f's gate refuses a
      flux-owned name added to `k8s_retired_services`, because the play's teardown would
      delete the stale staged render while Flux at the old `prod` still applies it. M6's
      retirement of uptime-kuma, if it follows uptime-kuma's move, takes this path. The claim
      survives prune and is the **operator-only** hand delete D5.1f names.
    - **A test in `ansible/tests/deploy/`** evaluates every pre_task of the k8s play
      against two inventory fixtures, one with every entry flux-owned and one with a single
      entry handed back, and fails when any task that applies a Namespace or a LimitRange
      would run. The tree passes, because no such task remains. Its red half is a fixture
      play that keeps the apply task gated on `containers_list | ansible_owned | length >
      0`, which runs against the handed-back fixture. A second test fails when
      `homelab-namespace` is missing from the render or from `SHARED_KINDS`.
31. **The memory reading** (about 10 lines). After the namespace PR, the flux role's
    `CLAUDE.md` records kustomize-controller's peak working set from exit 7, beside D1's
    figure and its 64Mi request. The PR body quotes the `probe.py metric` output. If the
    peak exceeds the request, the same PR raises the request and says so.

## Verification

```bash
# Render the SHA that `probe.py landing` names as prod into /tmp/out first,
# with `homelab-render cluster --out /tmp/out`.

# Exit 1.
./scripts/deploy.sh --list-services
grep -c 'platform: k8s' ansible/inventory/host_vars/daniel-box.yml      # N
grep -c 'owner: flux' ansible/inventory/host_vars/daniel-box.yml        # the same N
uv run python scripts/deploy_tools/render_targets.py daniel-box | tr ',' '\n' | grep -c .   # 0
systemctl list-unit-files 'kuma-check-render-records*' | grep -c render-records   # 0
grep -rl 'render-records.json' /tmp/out/cluster | grep -c .                        # 0

# Exit 2: what every k8s tag renders is Flux-applied, with no client-side state left.
kubectl get deploy,sts,ds -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{" "}{.metadata.labels.kustomize\.toolkit\.fluxcd\.io/name}{"\n"}{end}' \
  > /tmp/workloads.txt
grep -E '^(homelab|observability)/' /tmp/workloads.txt | awk 'NF < 2' | grep -c .     # 0
grep -c -E '^(homelab/authelia|homelab/nut|homelab/pihole-2|kube-system/dri-device-plugin) [a-z]' \
  /tmp/workloads.txt                                                                 # 4
kubectl get deploy,sts,ds -A -l kustomize.toolkit.fluxcd.io/name \
  -o jsonpath='{range .items[*]}{.metadata.annotations}{"\n"}{end}' \
  | grep -c last-applied-configuration                                               # 0

# Exit 3. A commit carries flux/homelab only if prod pointed at it (C2, C6).
gh api 'repos/DanielH2018/server/commits?sha=master&per_page=100' --jq '.[].sha' \
  | xargs -I{} gh api --paginate 'repos/DanielH2018/server/commits/{}/status?per_page=100' \
      --jq '.sha as $s | .statuses[] | select(.context == "flux/homelab" or .context == "flux/soak") | "\($s) \(.context) \(.state)"' \
  > /tmp/promoted.txt
awk '$2 == "flux/homelab" {print $1}' /tmp/promoted.txt | uniq | head -10 > /tmp/last10.txt
wc -l < /tmp/last10.txt                                                             # 10
grep -F -f /tmp/last10.txt /tmp/promoted.txt \
  | awk '$2 == "flux/soak" && $3 == "success"' | wc -l                               # 10

# Exit 4.
jq -r 'to_entries[] | select(.key != "_unclassified") | select((.value.claims // []) | length > 0) | "autodeploy-\(.key)-\(.value.own[0:8])-"' \
  /tmp/out/digests.json > /tmp/prefixes.txt
wc -l < /tmp/prefixes.txt                                                           # 14
kubectl -n longhorn-system get snapshots.longhorn.io -o name | sed 's#.*/##' \
  | grep -o -F -f /tmp/prefixes.txt | sort -u | wc -l                               # 14
grep -rhoE 'ghcr\.io/danielh2018/(ical-proxy|terraria|pi-peer-backup|n8n|n8n-runners|code-server|nut|homelab-mcp|valheim|karakeep):sha-[0-9a-f]{12}' \
  /tmp/out/cluster | sort -u > /tmp/want.txt
wc -l < /tmp/want.txt                                                               # 10
kubectl -n homelab get deploy,sts,ds,cronjobs -o jsonpath='{..image}' | tr ' ' '\n' \
  | sort -u > /tmp/have.txt
comm -23 /tmp/want.txt /tmp/have.txt | grep -c .                                    # 0
uv run pytest scripts/renderer/tests -k ghcr_pull_policy                             # passes

# Exit 5. The Namespace's labels name the Kustomization and carry prune: disabled;
# the LimitRange's name the Kustomization.
kubectl get ns homelab -o jsonpath='{.metadata.labels}'
kubectl -n homelab get limitrange -o jsonpath='{..metadata.labels}'
grep -c -e 'Build the workload namespace manifest' -e 'Apply the workload namespace' \
  ansible/deploy.yml                                                                  # 0
uv run pytest ansible/tests/deploy -k namespace

# Restart annotations, once each role is flux-owned: cloudflare-ddns's from D3's rule,
# pihole's and observability's from D5.1e. Each named member prints a non-empty value.
kubectl -n homelab get deploy cloudflare-ddns-direct cloudflare-ddns-proxied pihole pihole-2 \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.template.metadata.annotations.checksum/restart}{"\n"}{end}'
kubectl -n observability get deploy prometheus grafana alertmanager \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.template.metadata.annotations.checksum/restart}{"\n"}{end}'

# The two tags that ship CRDs render prune: false. Each line reads `<name> false`.
kubectl -n flux-system get kustomization traefik flux \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.prune}{"\n"}{end}'

# Exit 6, inside the second speedtest drill, after one interval of flux-deferred and the root.
kubectl -n flux-system get kustomization speedtest speedtest-snapshot \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.suspend}{" "}{.metadata.managedFields[*].manager}{"\n"}{end}'
# Each line reads `<name> true` and lists `homelab-operator`.

# Exit 7, after the namespace PR. Operator-only trigger: delete kustomize-controller's
# pod. The fresh pod reconciles every Kustomization at once, and no restartedAt is written. Then read its peak working set, in bytes,
# over the hour that followed.
uv run python scripts/diagnostics/probe.py metric \
  'max_over_time(container_memory_working_set_bytes{namespace="flux-system",container="manager",pod=~"kustomize-controller-.*"}[1h])'
# At or under 67108864, the 64Mi request.
```

Exit 3 counts only commits that `prod` pointed at. notification-controller posts `flux/*`
statuses only on the commit an applied origin revision names (C2, C6), so a commit with a
`flux/homelab` status is one `prod` applied, and a superseded commit has none. The command
takes the ten newest such commits among the last 100 on master, and both counts must read
10: fewer promoted commits fail the first, and a `failure`, a `pending` or a missing
`flux/soak` fails the second. The combined-status endpoint returns the newest status per
context, and `--paginate` covers a commit with more than 100 contexts, which the bundle's
Kustomization count can reach. Exit 4's image check names each
of the ten images, so a texbrain match or a missing image cannot satisfy it, and `{..image}`
includes valheim's initContainer.

**Each new check can go red.**

- **The soak.** A small PR adds a `livenessProbe` that fails after readiness passes to
  bento-pdf, which is flux-owned since D2. CI is green, because no test checks a probe. After
  it lands, the soak Job must exit 1 and name bento-pdf's pod, `promote` holds and resets, and
  the revert lands, as in D2.8 and D2.9. Clearing `DEPLOY_HOLD` afterwards, with
  `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`, is **operator-only**
  because it changes a repository variable. This is the case Flux's `wait` cannot see,
  because the pod passes readiness first.
- **The snapshot Job.** Its script test's five red halves, the stateful rollback rule's
  no-reset test, and the speedtest drill.
- **The suspend lever.** Its tests in D5.1b, and the fixture template that renders
  `suspend: false`.
- **A suspended role takes no change.** D5.1d's gate, `suspended-changed` and `land-wait`
  tests, each with its red half.
- **Hand suspends survive Flux.** The runbook text test fails on a `kubectl patch` without
  `--field-manager=homelab-operator`. The live proof is exit 6, the drill after the flux PR.
- **The publish census.** Fixtures with an unknown document and an unknown file each fail.
  A tag named `soak` fails D5.1a1's literal reserved list, before `soak` joins
  `SHARED_KINDS`.
- **The collision census.** A fixture with two tags rendering one Middleware fails.
- **The Job census.** One Job of each kind with a TTL fails, a mutation Job without
  `homelab/rerun-safe` fails, and a script that calls `urllib.request.urlopen` directly fails.
- **The host-write censuses.** A fixture task with `ansible.builtin.cron` in an unlisted role
  fails, a `shell` task running `install` fails, the row fails when pi-peer-backup's,
  docs's or artifacts' task file stops matching, and the test's copy of
  `registry-gc.sh.j2`'s `scale` call fails the host-script row. An absent arm in an unlisted
  role fails too, so an H2 family PR cannot drop a role's `allow` entry while its arms stay.
- **The image censuses.** A fixture context that renders a placeholder fails, and nut's
  Deployment with `imagePullPolicy: Always` on a GHCR ref fails.
- **`<tag>-verify`'s edge.** A fixture tag with a deferred file whose verify Kustomization
  depends on `<tag>` fails.
- **The reboot window.** A fixture with a promotion inside T's derived window ends
  `deferred` without sleeping, and one outside it promotes. The scheduled run at the
  window's end hands `verify` T's full derived timeout, and the red half is a `decide` that
  waits in its job. The workflow's cron matches the derived end. The reset input refuses
  when `DEPLOY_HOLD` reads `none`, is empty or is missing. It also refuses while `prod`
  differs from `last-good` under a hold that does not name `prod`'s commit, and it moves
  `prod` under a settled `<T> stateful=<names>` hold.
- **crowdsec's channel.** A fixture where the edge answers 200 fails naming the edge, and one
  with no `alloy-pi` sample fails naming the channel.
- **Each converted verify.** Its red fixture, by class, and a refused probe leg for each new
  fence peer.
- **The restart annotations.** An Alertmanager Secret edit that moves another workload's
  annotation fails, a pihole ConfigMap edit that leaves instance 2's annotation unmoved
  fails, and pihole-2's entry with its annotation removed fails D5.1e's census.
- **The removal gates.** A head that drops a NetworkPolicy, or a ClusterRole, with no
  `Removes-Object:` trailer fails, a head that renames a snapshot Job passes, and a flux-owned entry moved into `k8s_retired_services` fails. The revert of a
  flip passes, so the gate never blocks a handback.
- **The co-batched hold.** A post that carries a command block for the failed snapshot role
  alone fails D5.2b1's two-role test.
- **The gate Job's name.** A Job named `pi-peer-backup-gate-<digest8>` fails the
  `pull-pi-peers.sh` pattern test.
- **The moved host files.** A fixture template that reads a variable defined only in a
  `roles/k8s/` defaults file fails the `k8s_host_files` render test, and a copy of
  `artifacts_peer_sources` that differs from the artifacts role's fails the pair test.
- **The takeover census.** A fixture Deployment whose `kubectl-rollout` entry owns
  `restartedAt`, which the render omits, is listed. The same field owned by `k3s`, or by a
  `kubectl` entry with `subresource: scale`, is not.
- **The namespace writer.** A fixture play that keeps the namespace apply gated on
  `containers_list | ansible_owned | length > 0` fails the namespace test against the
  handed-back inventory fixture.
- **The soak's alert filter.** The shadow, Watchdog and re-activation fixtures in D5.3b1
  pass, and the same fixtures without the filter fail.
- **The soak's revision field.** A check that reads `artifact.revision` fails D5.3b1's
  matching fixture, whose origin key names R.
- **The classify comparison.** A post whose classify command names R's SHA, or no value,
  fails D5.2b1's test, and a runbook classify step with no `prod@sha256:` value fails
  D5.2c's text test.
- **Observe first.** With D5.3b2's observe-only set emptied, its two fixtures wait and hold.
  After D5.3b3, the same fixtures prove that `flux/soak` gates.
- **`land-wait` waits on the soak.** D5.3b3's fixture with every tag Ready and no
  `flux/soak` status reads `settled` against a `land-wait` that reads only the tags, so the
  test fails there. Live, after D5.3b3: the next landing of a flux-owned PR prints its
  `settled` only after R carries `flux/soak` `success`, read with `gh api
  repos/{owner}/{repo}/commits/<R>/status`.
- **The cold start.** The bootstrap drill's green and red jobs pass on the flux PR. After it,
  `grep -n 'deploy.sh' docs/break-glass.md docs/longhorn-disaster-recovery.md
  docs/k3s-etcd-restore.md` prints no cluster step without the bootstrap section's link,
  and the three runbooks' text test holds that. The bootstrap section's text test fails on
  its red fixture with step 2's reset condition deleted. After the namespace PR, R4's
  PR 7b `awk` program exits 0 and prints nothing over the section, inside that test. The
  drill's green job prints the stateful fixture's NotFound reads before step 9 and its
  Ready read after it.
- **The shed lever.** `probe.py shed-set` prints the suspend command for a flux-owned
  service, and D5.2c's text test fails on a shed patch without the field manager, and on
  a shed clear of `DEPLOY_HOLD` with no `gh variable get DEPLOY_HOLD` read before it.
- **The shed scenario's offline branch.** D5.2c's text test fails on its red fixture, a
  resume moved before the branch's `gh variable set DEPLOY_HOLD`.
- **The soak's footprint.** Two fixture commits that differ only in a docs file give an
  empty changed set. The same pair hashed over the raw render puts `soak` in the set and
  fails.
- **The soak's scope.** D5.3b1's unrelated-alert fixture passes inside the scope and fails
  when the base file does not verify. A Job that ignores the scope fails it.
- **kustomize-controller's memory.** The exit-7 comparison reads a fixture sample of 80Mi as
  over the request and fails.
- **A new claim.** D5.2a's new-claim fixture passes and its inventory-listed twin fails.
- **The reboot window.** `promote`'s first Sunday run inside the window ends `deferred`, and
  the scheduled run at the window's end promotes master's tip with `verify`'s full derived
  timeout, read from the run's log.
- **The CRD census.** traefik's render with `flux_prune` removed fails it, and so does
  `flux`'s after the flux PR.
- **The recovery runbook.** Each hand-apply branch with its hold moved after the apply fails
  the flux PR's text test, and so does the sync-object branch without `--force-conflicts`.
- **Each wave's PR.** The skip tests from D2 run against the live inventory, so a flipped role
  that an entry point still reaches fails CI.

## Rollback

- **D5.1 to D5.4.** Each reverts on its own while no role depends on it. Revert role PRs
  first.
    - Reverting D5.1b while a role sets `flux_suspend` resumes that role, so remove the key
      first. Reverting D5.1d while a role is suspended reopens the race it closes, so it goes
      only after every suspend is removed.
    - Reverting D5.1a1's publish census restores D2.6b's census with D3.3a's `cluster-vars`,
      which refuses every per-tag D5 kind, so it goes only after the PRs that add those kinds.
    - Reverting D5.1e while observability or pihole is flux-owned leaves that role with no
      restart on a config or Secret change, and restores D3's interim census, which then
      refuses those entries. So hand those roles back first. cloudflare-ddns keeps D3's
      annotation either way.
    - Reverting D5.1f removes both removal gates and changes nothing live.
    - Reverting D5.1g removes a read-only subcommand and changes nothing live.
    - Reverting D5.1h removes the role's playbook entry, so it parks the tick once more and
      needs the same **operator-only** clear. It goes only before any row has moved into the
      role, because the entry is what the tick applies those rows through.
    - Reverting D5.2a while a snapshot role is flux-owned removes that role's snapshot before
      its next change, so hand the role back first.
    - Reverting D5.2b1 restores reset-on-failure for snapshot roles. Reverting D5.2b2
      removes the reset input, which Branch 1 and the broken-sync-object runbook need, so it
      goes only with D5.2c, and only before the flux PR. Reverting
      D5.2c leaves no revert entry point, so it goes only with D5.2a.
    - Reverting D5.3b3 restores D5.3b2's observe-only set, so `verify` stops waiting on
      `flux/soak` and ignores its `failure`, while the Job still runs. The same revert
      returns `land-wait` to reading only the tags, so `settled` and `verify` again agree
      that the soak gates nothing. Reverting D5.3b2 then
      removes the set and the `soak` Kustomization in one commit, and the Job stays under
      `deletionPolicy: Orphan` and is harmless. Revert D5.3b3 first, because D5.3b2's
      revert removes the set that D5.3b3 already deleted and does not apply cleanly alone.
    - Reverting D5.4a1 while a flux-owned consumer reads `tags.json` would leave its ref
      unrendered, so it goes only after D5.4b's revert.
    - Reverting D5.4b points flux-owned consumers back at the in-cluster registry. Every
      content tag CI pushed was also pushed in-cluster by the last Ansible deploy, but a
      later one may be missing there, so hand those roles back first.
- **The registry GC retirement.** Reverting it restores the cron, its script, its manifest and
  its monitors, and sets `registry_k8s_manage_gc` back to true, and Ansible applies them while
  registry is Ansible-owned. After registry's flip, the revert would put the scaling cron back
  beside Flux, so it goes only with a handback of registry. A handback is also the only way to
  re-arm GC in any form, because H2 no longer builds an orchestrator (Open question 15).
- **A role PR.** The handback from D2. The role's converted tasks are restored by the same
  revert, so Ansible runs them again on its next deploy. A Job the role added is orphaned
  under `deletionPolicy: Orphan`, and a completed Job is harmless. A host task file moved to
  `k8s_host_files` stays there until its own conversion PR is reverted.
- **The prune setting on traefik and `flux`.** Never revert `flux_prune: false` on its own
  while the role is flux-owned, and the CRD census fails such a revert in CI. A handback
  reverts the flip PR whole. The Kustomization then leaves the bundle under `deletionPolicy:
  Orphan`, so the CRDs and their objects stay.
- **The render-records disarm PR.** Reverting it restores the derivation from
  `render_records_enabled`, which is still true, together with the Kuma tile and its group
  rule. The revert changes the same role defaults path, so the tick applies the role's present
  arm through the setup arm, and `land.sh` follows it. The producer then renders the list of
  Ansible-owned tags again. After the flux PR that list is empty, and the producer pushes
  `down` every run, so this revert goes only with a revert of the flux PR.
- **The flux PR.** Reverting it hands the flux role back to Ansible. The `flux` and
  `flux-deferred` Kustomizations leave the bundle, their objects stay under `deletionPolicy:
  Orphan`, and `land.sh` redeploys the role. The revert restores the `group_vars` comment and
  leaves the producer disarmed, because the disarm PR holds the lever. While Flux cannot
  reconcile, every service keeps its last applied spec, and the recovery runbook applies.
  The same revert restores the three runbooks' `./scripts/deploy.sh` step, which installs
  Flux again once Ansible owns the role, so the runbooks never point at a path that deploys
  nothing. The bootstrap drill PR reverts on its own, but only after the flux PR's revert,
  because the bootstrap section links its procedure. The revert also turns R4's PR 1
  precondition red on `flux` by design, because its exemption holds only while `flux` is
  flux-owned.
- **The namespace PR.** Reverting it restores the two tasks with their gate
  `containers_list | length > 0`, which holds, and removes `homelab-namespace` and its
  `SHARED_KINDS` member in the same commit. The revert edits `ansible/deploy.yml`, so the
  deployer applies it as a deploy-plane change at its next tick, and the namespace pre_task
  writes the Namespace and its LimitRange then. Flux at the old `prod` keeps applying
  `homelab-namespace` until `promote` moves `prod` past the revert. For that window both
  write the same objects. The overlap is harmless and bounded: neither the namespace PR nor
  its revert changes `k8s_psa_labels` or the LimitRange's values, so both writers declare the
  same fields with the same values, and the window ends when `prod` moves. A handback of a
  single role opens no such window, because the namespace PR leaves no gate for it to open.
  Once `prod` moves, `homelab-namespace` leaves the bundle, and `deletionPolicy: Orphan` keeps
  the Namespace and its LimitRange. The Namespace also carries `prune: disabled`.
- **What a partial failure leaves.** A wave's flip that merges while `promote` fails leaves
  those roles frozen at their last spec with no writer, as in D2. A snapshot role's failure
  leaves the failed revision live until the operator runs the runbook. A Wave D PR that flipped
  without converting a row loses that row's effect. Its verify class is the costly one,
  because nothing reports the loss. The census-quote precondition is what prevents it.

## Risks

- **Downtime for `Recreate` roles at takeover.** Each rolls once. Mitigation: the soak judges
  each roll, and the seven own-window roles each land alone in an announced hour.
- **Verification lost at the move** (failure class 2). A verify task stops running the moment
  its role is flux-owned. Mitigation: the class table, which never lets `probe.py health`
  alone succeed an eligible role's verify, and the census-quote precondition.
- **A verify Job is refused by the fence it tests through.** Today's verifies run as the node
  or through root `kubectl exec`, and a Job pod is neither, so a converted verify would fail
  live and hold every flip. Mitigation: each Job's caller census, its peers landed and
  probed before the flip, exec checks moved to network reads or initContainers, and any
  remaining `pods/exec` grant stated as a trade.
- **A config or Secret change stops reaching its pod** (failure class 2). Three roles restart
  by hand today, and a flux-owned role runs no task. Mitigation: `checksum/restart` per
  workload, D3's rule for cloudflare-ddns and D5.1e's for pihole and observability, D5.1e's
  census, and fixtures that move exactly the readers of a change.
- **The stateful rule leaves a broken role live** until the operator acts. That is the
  deliberate trade. A revert done by hand is safer than an automatic rollback onto migrated
  data. Mitigation: `promote` posts the hold to Discord with each snapshot prefix, a failed post
  fails the run, and the speedtest drill proves the runbook.
- **A co-batched failure stays live too.** The no-reset rule keys on a changed snapshot role,
  so a broken edge role that shares a revision with a Renovate bump of a snapshot role also
  stays live until the operator acts. Mitigation: the hold post marks every changed and failed
  tag, and the runbook's second branch forward-reverts the broken commit without touching the
  snapshot roles. Open question 1 puts this case to the operator.
- **A reset rolls back every co-changed snapshot role** (failure class 5). One exportarr bump
  changes sonarr, radarr and prowlarr together (Baseline), and a reset moves `prod` for the
  whole bundle. A Branch 1 that reverted only the failed role would roll the other two back
  onto data R may have migrated. Mitigation: the post prints a command block per changed
  snapshot role, the classify read picks every role that attempted R, Branch 1 suspends all
  of them before any revert and reverts each with its own `digest8`, and D5.2b1's two-role
  test fails a post that covers the failed role alone. A role the operator judges compatible
  skips its revert only with a recorded judgment.
- **Flux's cleanup reverts a hand write** (failure class 5, a sequence treated as a
  transaction). kustomize-controller hands every field under a `kubectl`-prefixed manager to
  itself on each apply, and the next apply removes it. A runbook suspend made with
  `kubectl patch` would therefore undo itself within one interval of its applier, and a
  failing `<tag>` would re-apply R while `volume-revert` holds the volume. Mitigation: every
  hand write uses `--field-manager=homelab-operator`, the renderer never writes `suspend:
  false`, a text test holds the runbook to the field manager, and exit 6 re-runs the drill
  under self-management.
- **A suspended role's merged change applies nothing.** Mitigation: D5.1d refuses such a
  change in CI, holds on one that races past it, and gives `land-wait` a `suspended` verdict.
- **A promotion across the weekly reboot holds.** Mitigation: the derived no-promotion
  window in D5.2b2, at the cost of a weekly pause of about three to six hours on Sunday
  morning.
- **A wait for the window inside the `promote` job would cause a false hold** (failure
  class 5). D4's 5.5-hour cap counts from the job's start, so a two-to-three-hour wait would
  leave `verify` less than its derived timeout. The wait would also block a reset in the
  same group and read a hold set during it too late. Mitigation: `decide` never waits. It
  ends `deferred`, a derived `schedule:` run promotes at the window's end, the reset runs in
  its own group, and a test fails on a `decide` that sleeps. The residue is GitHub's
  scheduling: a late or skipped scheduled run delays the promotion until the next merge or
  a dispatch, and never holds.
- **An unrelated failure holds a good revision.** The soak reads only R's changed set, so
  an unrelated alert no longer fails it. Two paths remain. A changed Kustomization that never
  reads Ready at R fails the soak at its wait's deadline, and the failure names that
  Kustomization (Open question 19). A failed or unverified base read widens that soak's scope
  to the fleet. Mitigation: the post names the alert or the blocking Kustomization, and the
  Job logs why it widened. Open question 17 states what C2's revert rule leaves open
  (**operator-only**).
- **A snapshot name collides with a retained or `markRemoved` CR** (failure class 6).
  Mitigation: the per-run token, a Job that never adopts an existing CR, and its red test.
- **The revert runbook is a sequence, not a transaction** (failure class 5). A resume before
  the reset re-applies the failed revision. Mitigation: the ordered steps, resume last, and
  the drill.
- **A suspend can undo itself.** A reset to a `last-good` before the suspend un-suspends the
  role. Mitigation: the suspend PR lands alone, and `probe.py landing` shows where `last-good`
  stands.
- **The soak reads an empty join as clean** (failure class 3). Mitigation: a pod with no
  service mapping fails the soak, and the authelia fixture covers the override table's roles.
- **The soak holds on a restart today's gate ignores.** Mitigation: it counts only restarts
  after a pod's first Ready, and only for pods created after T0.
- **The soak cannot order a restart against Ready** (failure class 3). A 1-minute scrape
  leaves gaps, and Prometheus itself rolls when R changes observability. Mitigation: restart
  counts come from the API, an absent Ready sample or a restart inside a scrape gap fails
  closed and names the pod, and the bounded miss is stated rather than hidden.
- **A fresh Job pod is refused by kube-router** until it learns the pod's address.
  Mitigation: the shared retry helper, its census row, and two consecutive live soak passes.
- **A grouped Renovate PR rolls many roles at once.** Flux has no per-tick cap like
  `split_k8s_auto_deploy`'s `max_per_tick`. Mitigation: `--concurrent` bounds the parallelism
  to the batch width. The Renovate rule against automerge for `k8s_autodeploy: false` roles
  stays.
- **Queueing makes `verify` time out falsely.** Mitigation: D4's derivation gains the queueing
  term, and the measurement before Wave D checks it.
- **Two writers through a shared object** (failure class 5). The collision census, and moving
  any listed pair in one PR.
- **A retirement deletes what Flux still applies** (failure class 5). The play's teardown reads
  a stale staged render while Flux at the old `prod` re-creates it. Mitigation: D5.1f refuses
  a flux-owned name in `k8s_retired_services`, and a flux-owned service retires by D2's two
  PRs.
- **A dropped template deletes a live object behind a green status.** Prune removes it and
  `wait` reads Ready. Mitigation: D5.1f's gate requires a `Removes-Object:` trailer for each
  object of any kind that a PR removes, and D2's `decide` checks the same trailer at promote
  time.
- **A snapshot taken long before its apply.** Without edges, `<tag>-snapshot` runs when the
  revision arrives. Mitigation: it inherits `<tag>`'s `dependsOn`, with a renderer test.
- **The takeover strips or fails on live state** (failure class 3: a dry-run diff cannot see
  the cleanup, so it would read clean). Mitigation: D5.1g's takeover census lists every field
  a cleaned manager owns that the render omits, with speedtest's `restartedAt` as its red
  half. The operator's substituted `flux diff` covers immutable fields and changed values.
  Every role PR quotes both.
- **A host-side task moved to a setup role is applied by a different playbook.** Mitigation:
  the role PR quotes the setup-plane apply in its body, and `land.sh` follows it.
- **Creating the setup role parks every landing** (failure class 8: the lever routes to a
  plane the tick never applies). Its `initial_setup.yml` entry is a bring-up change.
  Mitigation: D5.1h carries the entry and nothing else, merges only into a converged
  deployer, and the operator clears the park at once by *When a tick parks*'s order,
  fast-forwarding to D5.1h's merge commit only, so no other merge's tick-owned apply is
  skipped.
- **A moved host template loses its sources or variables.** Role-relative paths and role
  defaults do not cross roles. Mitigation: each conversion PR moves the sources and the
  variables with the task, and the `StrictUndefined` render test fails on an unresolved
  variable.
- **A shared variable's host half never re-applies** (failure class 8). A key in
  `group_vars/all.yml` routes to the deploy plane, which never runs `k8s_host_files`.
  Mitigation: a variable both sides read stays in the k8s role with a test-held copy in
  `k8s_host_files`, so a change edits both and the copy routes to the setup plane, as H2.7
  does for `claude_code`.
- **A private package freezes a role.** A pull failure after the flip leaves the role at its
  last spec with no writer. Mitigation: the packages are made public before Wave C, and each
  Wave C PR checks an anonymous fetch from both nodes.
- **Self-management closes its own door.** A suspend or a broken sync object applied by Flux to
  itself cannot be cleared from git. Mitigation: the flux PR removes the git lever, pins the
  sync objects in a test, and writes the hand recovery. Both hand-apply branches set the hold
  and stop the re-apply before the apply, and force the conflicts the apply must win. A text
  test holds both.
- **A reading of the wrong revision field** (failure class 2, as D4's Risks name it).
  `lastAttemptedRevision` and the OCIRepository's `artifact.revision` hold
  `prod@sha256:<digest>`. A soak that compared `artifact.revision` with R would fail every
  promotion. A classify step that compared `lastAttemptedRevision` with R's SHA would read
  every role as blocked and send a failed snapshot role to Branch 2, which applies
  `last-good`'s image with no volume revert. Mitigation: the soak reads the origin key, the
  post prints the `prod@sha256:` value to compare, every wait for `last-good` reads the
  origin key, and D5.3b1, D5.2b1 and D5.2c each carry a red fixture for the wrong field.
- **A first live soak gates before anyone has watched it pass** (failure class 5, a
  sequence treated as a transaction). Publishing `out/flux/soak.yaml` is enough to make
  `verify` expect `flux/soak`. Mitigation: D5.3b2's observe-only set, which D5.3b3 removes
  only after two consecutive `success` statuses. A contracts note records the temporary
  carve-out from C2's expected set.
- **`land.sh` reports a verified deploy before the soak's verdict** (failure class 5). The
  tags are Ready before the soak Job starts, so a `land-wait` that reads only the tags
  always settles first. Mitigation: D5.3b3 makes `settled` need `flux/soak` `success` on
  R, ends the wait on a `failure`, and tests the no-status fixture as the red half.
- **A rebuilt cluster gets no Flux** (failure class 5). From the flux PR on, every path that
  `docs/break-glass.md` and `docs/longhorn-disaster-recovery.md` name for the cluster
  skips the `flux` tag, so their `./scripts/deploy.sh` step deploys no workload. A hand
  scale-to-zero of a flux-owned service is reverted by drift correction, so the shed
  scenario frees no memory. Mitigation: the bootstrap drill PR proves the cold bootstrap on
  a kind cluster, and the flux PR rewrites the three runbooks to use it. D5.1b rewrites
  the shed scenario around a hand suspend under `DEPLOY_HOLD`.
- **A new claim on a snapshot role could hold every later revision.** The claim exists only
  after `<tag>` applies, and `<tag>` waits on `<tag>-snapshot`. Mitigation: the snapshot Job
  skips a claim that is absent both live and from `<tag>`'s `status.inventory`, and fails
  one that the inventory lists but the cluster lacks. D5.2a tests both.
- **The soak's restart leg drops Deployment pods** (failure class 3). A Deployment's pod is
  owned by a ReplicaSet, so a Job that cannot read ReplicaSets would see an empty scope and
  pass. Mitigation: the Job's Role grants ReplicaSets, a refused read fails the soak, and
  D5.3b1 tests a restarting Deployment pod.
- **A dropped CRD deletes every object of its kind** (failure class 5). Prune and the
  render are two steps that the plan must not treat as one: a render that drops a CRD file
  makes the next apply delete the CRD, and the API server then deletes every custom object
  of that kind. traefik's tag holds IngressRoute and Middleware, and `flux` holds Flux's own
  kinds. Mitigation: both render `prune: false` through H1.1a's lever, and the CRD census
  fails a CRD in any tag that renders `prune: true`. The cost is that an object dropped from
  either render stays live until the operator deletes it.
- **GHCR holds every built image.** A GHCR or WAN outage blocks a build, and a pull of a new
  content tag. With the baseline's `imagePullPolicy: Always`, it would also fail every restart,
  reboot and crash recovery of the ten consumers, nut's UPS pod among them. Mitigation:
  `IfNotPresent` on every GHCR ref, safe because the content tag is immutable, and the census
  that refuses `Always` on one. The residual is a node that has never pulled the tag, or whose
  kubelet garbage-collected it. Open question 2 recommends keeping the in-cluster registry
  as a LAN pull-through mirror of GHCR, which gives nut a LAN source for that cold pull.
- **The crowdsec verify's evidence crosses the edge it tests.** Mitigation: the evidence
  reaches Prometheus by a direct scrape of the Pi's Alloy, and a red fixture names a blocked
  channel apart from a failing edge.
- **A host script writes a flux-owned object** (failure class 5). Mitigation: the host-script
  census row, and the retirement of the registry GC, its only member at the baseline. H2 drops
  its GC orchestrator, which would have scaled the same Deployment from inside the cluster
  (Open question 15).
- **A disarm lever routes to a plane that never applies it** (failure class 8). The role's
  `CLAUDE.md` names `render_records_enabled` in `group_vars/all.yml`, but the deployer routes
  that path to `deploy.yml`, which never includes the role. Mitigation: the disarm PR uses the
  role-defaults lever, quotes the measured route, and checks that the units are gone.
- **A Wave D role flips before H2 has moved its host crons** (failure class 5). The crons
  would keep running on the host beside a flux-owned role, and nothing would remove them.
  Mitigation: the flip cannot pass while the role keeps a host-write `allow` entry, and the
  flip PR links the merged H2 PR.
- **H2's leftover rows block a flip, or vanish with it.** H2 leaves absent arms in four roles
  and three directory tasks in artifacts, and D5.1c counts each as a host write. Mitigation:
  a `k8s_host_files` conversion PR per role moves them before the flip, the census names
  artifacts' task file as a member it must find, and the flip PR links the merged
  conversion PR.
- **A handback re-arms a second namespace writer** (failure class 5). A namespace task gated
  on role ownership would run again beside `homelab-namespace` as soon as any role is handed
  back, and would rewrite the Namespace's `last-applied-configuration` that Flux's cleanup
  strips, on every deploy, for as long as the handback lasts. Mitigation: the namespace PR
  deletes the two tasks, and a test with a handed-back inventory fixture fails on any task
  that would apply a Namespace.
- **wg-easy's takeover cuts remote access.** Mitigation: its own window, with the operator on
  the LAN.
- **The soak Job reports through Flux.** A Prometheus outage fails the soak and holds. That
  is the safe direction, and M1's Watchdog pages for the outage itself.
- **The soak holds a good revision on an alert that pages nobody.** A shadow alert, the
  Watchdog, or a pre-existing alert with no `for` that a Prometheus roll re-activates would
  each fail it, and clearing a hold is operator-only. Mitigation: the `shadow!="true"` and
  `alertname!="Watchdog"` filter, the pre-T0 firing check after a Prometheus roll, and their
  fixtures.
- **An exit check passes vacuously** (failure classes 3 and 4). Mitigation: exits 2 and 4 name
  their members and their expected counts.
- **A hard-coded count goes stale** (failure class 8). An entry added after the baseline, such
  as M4's blackbox-exporter, would sit outside every wave and keep the namespace task live.
  Mitigation: exit 1 compares two counts, and the late-arrivals rule and the flux and namespace
  PRs' preconditions read `render_targets.py`.
- **A cold start applies the root before the restores.** The root would then provision empty
  PVCs under the backed-up names. Mitigation: the bootstrap section withholds the root until
  step 9, its text test fails on a root apply before the restore step, and the drill's third
  job proves that the claim check goes red when the root comes first.
- **A cold start resets `prod` across a migrated snapshot.** Step 8 restores backups that
  R's image may have written, and a reset would then run `last-good`'s image on them.
  Mitigation: step 2 allows `reset_to_last_good` only when no changed tag is a snapshot
  role or a forward-only tag, or when the operator records Branch 1's compatibility
  judgment first. The bootstrap section's text test fails when step 2 drops that
  condition.
- **A shed clear erases a failure hold.** A promote run already in `verify` when the shed
  sets `DEPLOY_HOLD` can fail on a hand-suspended tag. It then writes its SHA over the
  operator's reason and may reset `prod`. Mitigation: the shed's step 3 reads the hold
  before any resume, clears only step 1's own reason, and sends a SHA to the revert
  runbook's branch choice. D5.2c's text test fails on a clear with no read before it.
- **M6's drill holds littlelink back.** littlelink's flip waits on M6's B-1e, so a late M6
  Phase B delays the flux PR, whose precondition is that every other role is flux-owned.
  Mitigation: only littlelink waits. If M6 moves its drill to a fixture tag, the edge drops.

## Tests and docs that change in the same PR

- D5.1 to D5.4: tests in `scripts/renderer/tests/`, `scripts/deploy_tools/tests/`,
  `ansible/tests/deploy/` and `ansible/tests/repo/test_census_rows_roles.py`.
  `docs/flux-deploy-plane.md` gains snapshots, the soak, images and the stop lever.
    - D5.1b: `ansible/roles/setup/gitops_deploy/CLAUDE.md`'s sentence on stopping a role.
      `docs/break-glass.md`'s shed scenario and the daniel-server-dead step that points at
      it, with `probe.py shed-set`'s owner column and its test. The shed scenario's step 3
      reads `DEPLOY_HOLD` before any resume and clears only step 1's own reason.
    - D5.2b1 and D5.2c: `ansible/roles/k8s/volume-snapshot/CLAUDE.md`'s "A rollback is not a
      manual operation", `ansible/roles/k8s/volume-revert/CLAUDE.md`'s caller section, and
      the runbook in `docs/volume-snapshot-drills.md`.
    - D5.1d: D2's `land-wait` section in `docs/flux-deploy-plane.md` gains the `suspended`
      verdict, and `land.sh --help` lists it.
    - D5.1e: `ansible/roles/k8s/manifests/CLAUDE.md`'s rule that a pod mounting content
      outside the cycle needs its own trigger says that `checksum/restart` covers
      `manifests_self_rollouts` entries for flux-owned roles. D3's interim census and its
      test go in the same PR.
    - D5.1g: `docs/flux-deploy-plane.md` states what a takeover strips, the
      `probe.py takeover` command and the operator's `flux diff kustomization` command, and
      `docs/claude-tooling.md`'s probe list gains the subcommand.
    - D5.1h: the role's `CLAUDE.md` with the Autonomous-role contract, the `StrictUndefined`
      render test and the pair test in `ansible/tests/setup/`. The **operator-only** clear
      stays in this plan's *The `k8s_host_files` role enters the setup plane once* section
      and in [#5337](https://github.com/DanielH2018/server/pull/5337)'s body.
      `docs/gitops-pipeline.md`'s *When a tick parks* holds the general procedure for a
      parked tick, and the park recurs only on a revert of D5.1h.
    - D5.1f: `docs/flux-deploy-plane.md` states the `Removes-Object:` trailer, its
      cluster-scoped form, its two exemptions and the
      retirement order with its operator-only claim delete.
    - D5.2b2: `docs/flux-deploy-plane.md` states the reset input's concurrency group, its
      refusal without a hold, its refusal while a verification is in flight, the `deferred`
      result, the derived `schedule:` run at the
      window's end, and which timeout `decide` reads.
    - D5.2a: the snapshot section of `docs/flux-deploy-plane.md` states the new-claim skip.
      `ansible/roles/k8s/manifests/CLAUDE.md`'s claims rule says that under Flux a new claim
      on a snapshot role is skipped by the snapshot Job and created by `<tag>`.
    - D5.2c: the runbook's branch-choosing reads and its per-role Branch 1. Its text test
      gains the shed scenario's read-before-clear clause, with its red fixture.
    - D5.3b2: `docs/flux-deploy-plane.md` states the observe-only set and that D5.3b3
      removes it.
    - D5.3b3: `renovate.json`'s two descriptions, the observe-only sentence in
      `docs/flux-deploy-plane.md`, and the operator's ADR-0012 decision. `land.sh --help`,
      `docs/landing.md` and D2's `land-wait` section in `docs/flux-deploy-plane.md` gain the
      `flux/soak` read and the `soak-failed` cause, with the land-wait tests in
      `scripts/deploy_tools/tests/`.
    - D5.4a1 and D5.4a2: `ansible/roles/k8s/image-builder/CLAUDE.md` says what replaced it for
      flux-owned roles, that `image_builder_force` has no Flux-era use, and that the content
      tag comes from `DIR/images/tags.json`.
    - D5.4b: the comment above `imagePullPolicy` in
      `ansible/roles/k8s/nut/templates/deployment.yaml.j2`, and the `Always` sentence in
      `ansible/roles/k8s/registry/CLAUDE.md`.
- Each role PR: the role's `CLAUDE.md` says Flux owns it and where each converted task went.
  Its tests move from `ansible/tests/services/` or `ansible/roles/k8s/<role>/tests/` to
  wherever the converted code lives. A test whose subject is gone is deleted, and any
  `ENFORCED` citation of it goes in the same PR (`.claude/rules/python-layout.md`). A deleted
  test that `docs/failure-classes.md` cites forces an edit to that table.
- Each `k8s_host_files` conversion PR: its sources and variables, a pair-test row and a
  comment at both copies for any shared variable, the role's row in `k8s_host_files`'
  `CLAUDE.md`, and the host-write census's `allow` and `must_find` entries for the role.
- Each fence-peer PR: the role's `containers_list` entry or the bespoke policy, its
  `netpol_baseline_probe_targets` row, and `ansible/roles/k8s/netpol-baseline/CLAUDE.md` when a
  bespoke policy gains a cross-namespace peer.
- observability's conversion PR: the role `CLAUDE.md`'s *One config change, one restart*
  section names `checksum/restart` and the Grafana initContainer, and drops the restart
  task's reasoning about exposing a Secret.
- pihole's flip: the role `CLAUDE.md`'s notes on `roll_one.yml` say the annotation and
  `pihole-deferred` replace it.
- pi-peer-backup's flip and configarr's flip: each role's `CLAUDE.md` says the gate Job's new
  name, and `ansible/roles/k8s/cronjob-gate/CLAUDE.md` says which callers remain.
- sonarr's and radarr's flips: `ansible/roles/k8s/arr-notification/CLAUDE.md` says the seed
  runs as a post-apply Job, and why the ClusterIP rule no longer applies.
- registry's GC retirement: `ansible/roles/k8s/registry/CLAUDE.md`'s GC notes, the uptime-kuma
  tile and group rule, monitor-bridge's healthchecks.io entry, the rotation registry entry,
  `cron_targets.py`, and the tests that cite each, such as
  `ansible/tests/services/test_healthchecks_pings.py` and
  `ansible/tests/services/test_monitor_bridge_healthchecks_expected.py`.
- crowdsec's first conversion PR: `ansible/roles/containers/alloy/CLAUDE.md`'s source list,
  and the label-contract test it names.
- The render-records disarm PR: `ansible/roles/setup/render_records/CLAUDE.md`'s *Mode* line,
  the uptime-kuma tile with its group rule, and every test that names either. `grep -rln
  'render-records' ansible/tests ansible/roles/k8s/uptime-kuma/tests` finds them, for example
  `ansible/tests/setup/test_kuma_check_timer.py`, whose `KNOWN_CHECKS` names the check. The
  role's own tests in `ansible/roles/setup/render_records/tests/` gain the absent default.
- traefik's flip: the role `CLAUDE.md` says the tag renders `prune: false` and why, and
  the CRD census lands with traefik as its named member.
- The flux PR: the flux role's `CLAUDE.md`, which D1 creates, gains the `prune: false`
  reason. The recovery runbook gains its two hold-first hand-apply branches and the text
  test over them. The PR also edits the comment above `render_records_enabled` in
  `ansible/inventory/group_vars/all.yml`, and adds `flux` to the CRD census's members.
  `docs/flux-deploy-plane.md` gains *Bootstrapping Flux on a rebuilt cluster*.
  `docs/break-glass.md`, `docs/longhorn-disaster-recovery.md` and `docs/k3s-etcd-restore.md`
  link it in place of their cluster `./scripts/deploy.sh` step, with the text test over
  the three and the text test over the bootstrap section's order and step 2's reset
  condition.
- The bootstrap drill PR: the drill workflow with its three jobs and its fixture bundle,
  which holds a plumbing and a stateful fixture tag, the renderer test over the
  hand-applied documents, and the renderer test that the Ansible-owned `flux` role still
  writes `cluster/flux-deferred/`. No runbook changes, because Ansible still installs Flux.
- D5.3b3: `test_deploy_annotations.py` and `test_loki_stream_labels.py` gain the soak's
  annotation line and the second annotation query.
- The namespace PR: the tests that read the two deleted tasks (Baseline) move to the
  rendered `homelab-namespace` or are deleted, and `docs/flux-deploy-plane.md` names
  `homelab-namespace` as the namespace's only writer. The bootstrap section drops its
  `./scripts/deploy.sh` step, and its text test gains R4's PR 7b `awk` program.
- `scripts/dev/new_k8s_service.py` writes `owner: flux` on a new entry from the first Wave A
  PR on, so a service added during D5 is born owned by Flux. The `new-k8s-service` skill says
  so.

## Complexity delta

- **Added:** four generated non-tag Kustomization kinds (`<tag>-snapshot`, `<tag>-verify`,
  `soak`, `homelab-namespace`), with an allowlist publish census and reserved names; the
  snapshot and soak Job scripts, one Job image pin, or two if the soak's verifier needs its own, and one retry helper, and per Wave D role a
  Job, an initContainer or a rule for each converted task; the `flux_suspend` lever, its
  dropped edges, its change gate and one `land-wait` verdict; one field-manager name for hand
  writes; a revert playbook and a `reset_to_last_good` input with its own concurrency
  group; a hold post with per-role command blocks, a reboot window and one repository
  secret; an `images` job and the renderer's image-context output; one setup role for host
  files, with a pair test for shared variables; one read-only `probe.py takeover`
  subcommand; one Pi timer and one Alloy metrics stage; the self-rolled targets of D3's
  `checksum/restart`; two removal gates and one `Removes-Object:` commit trailer over every kind, which D2's
  `decide` shares; a fence peer per verify or
  mutation Job; seven census rows (host writes, host scripts, Job rules, Job connections,
  GHCR pull policy, publish layout, restart annotations, the last replacing D3's interim
  one); `prune: false` on traefik and `flux` through H1.1a's existing lever, and one
  renderer CRD census, which D6.19's guard would otherwise add later; one text test over
  the flux recovery runbook. D5.3b2's observe-only set in `verify` is added and then
  removed by D5.3b3, so it nets to zero. One `land-wait` read of `flux/soak` and one
  `Cause`, `soak-failed`; one `deferred` promote result and one derived `schedule:`
  trigger; one ReplicaSet grant on the soak's Role; one skip rule for a new claim in the
  snapshot Job; one CI drill workflow for the cold bootstrap, which R4 would otherwise add;
  one owner column in `probe.py shed-set`; two fields in each tag's object of D2's
  `digests.json`, which D2 creates; a normalised footprint for `soak`; the soak's run-time
  scope, with two signed GHCR reads and a per-service W map; an offline branch each in the
  shed scenario and the cold bootstrap; one exit check on kustomize-controller's memory, recorded by a 10-line PR;
  one pre-pull for tdarr that Flux applies, which replaces a task. Net PR sizes: D5.1a2
  shrinks by about 30 lines, D5.1f grows by about 30, D5.3b1 by about 40 and D5.3b2 by
  about 30.
- **Removed:** every Ansible k8s task that is only render, apply, wait or restart, including
  observability's secret restart, pihole's `roll_one.yml` and the Grafana password sync. The
  converted imperative tasks. `cronjob-gate`'s delete-and-create. `image_builder_force`. The
  registry GC cron, its script, its Job manifest and its three monitors. The play's per-role
  snapshot and revert includes, and the stabilisation gate's role. The render-records producer's timer and its Kuma tile. The
  play's two namespace pre_tasks, which the namespace PR deletes outright. R4 deletes the
  rest of the code. D5 makes it unreachable.
- **Not added:** a soak alert rule group, which M2's recording rules make unnecessary, and a
  scheduled rebuild workflow, which Renovate's digest bumps make unnecessary. H2's registry GC
  orchestrator, its RBAC and its fence peer are not added either, because D5 retires the GC
  (Open question 15).
- **Kept small:** `k8s_host_files` holds rows from nine roles and one Pi timer, and most of
  the rows are absent arms. H2 moves the live crons of six roles first, so what reaches
  `k8s_host_files` from five of them is absent arms and artifacts' three directories. Only
  docs's and artifacts' directories and pi-peer-backup's two Pi writes stay live until H7.

## Open questions

1. **Does the operator accept "hold without reset" for stateful roles?** It trades today's
   automatic revert for a hand one. It also covers two co-batched cases. A revision that
   changes a snapshot role and breaks a non-snapshot role, such as traefik, holds without a
   reset, and the broken role stays live until the operator runs the runbook's second branch.
   A revision that changes several snapshot roles, such as an exportarr bump, and breaks one
   of them takes Branch 1 for every role that attempted R, so a reset reverts the healthy
   ones' volumes too, unless the operator records that a role's data suits `last-good`'s
   image. Resolution: the operator decides all three in D5.2b1's review (**operator-only**).
   **Ruled 2026-10-16, in principle: accepted.** A failed revision that changes a snapshot
   role holds without a reset. The operator confirms the two co-batched cases in D5.2b1's
   review, with the code in front of them.
   **Confirmed 2026-10-17 in D5.2b1's review:** both co-batched cases hold as built, and the
   hold value `<T> stateful=unverified` is accepted.
2. **Does the in-cluster registry stay as a LAN mirror of GHCR?** After D5.4 no role pushes
   to it. A GHCR-only image path leaves the cluster no GitHub-independent pull, and nut, the
   pod half of the UPS shutdown chain, cannot start cold during a WAN outage.
    - **Recommendation:** keep the registry as a pull-through mirror for `ghcr.io`, named as
      a mirror endpoint in k3s's `registries.yaml`. containerd then tries the LAN registry
      first and GHCR second. A mirror holds only what was pulled through it, so nut's Wave C
      PR gains a precondition: a manifest GET of nut's content tag through the mirror on
      daniel-server returns 200.
    - **Why D5 and not D6.** D6 waits on D5, and nut flips in Wave C, so the ruling cannot
      wait for D6. The conversion takes two PRs: the registry's proxy configuration, moved
      ahead of nut's PR, and the `registries.yaml` entry, which `roles/setup/k3s` writes and
      H6 packages.
    - Resolution: the operator rules before nut's Wave C PR merges (**operator-only**). If
      the operator declines, nut's PR states the residual, a failed cold pull during a WAN
      outage, and D6 keeps the retirement question.
    - **Ruled 2026-10-16: yes.** The registry stays as a pull-through mirror for `ghcr.io`,
      in the two PRs the recommendation names, and nut's Wave C PR carries the mirror
      precondition.
3. **Is the soak's Prometheus reach a new exposure?** It is one more caller on an existing
   fence. Resolution: reviewed in D5.3a against ADR-0009.
    - **Reviewed 2026-10-16 in #5315: no new exposure.** The soak is one more read-only caller
      on 9090, because Prometheus runs without its admin, lifecycle and remote-write-receiver
      flags. Only kustomize-controller, or an identity that can create pods in `flux-system`,
      could carry `app: soak`, and either can already apply anything cluster-wide.
4. **C2 changes in seven ways.** Snapshot roles hold without a reset. `promote`'s expected
   statuses become C2's changed set, minus suspended ones, which can hold `<tag>-snapshot`,
   `<tag>-verify`, `soak` and `homelab-namespace`. The pillar review fold applied this part
   to C2. D5's own hold values, `<T> stateful=<names>` and `<R> suspended-changed=<tag>`,
   never take C2's `failed=` form. From
   D5.3b2 until D5.3b3, `verify` also leaves out `soak`, through its observe-only set, and
   ignores a `flux/soak` `failure`. A
   `reset_to_last_good` dispatch input exists, in its own concurrency group
   `promote-reset`, ignoring the reboot window and refusing without a hold. The hold posts
   to Discord, with a command block per changed snapshot role. A revision that changes a
   suspended tag holds with `suspended-changed`. No promotion moves `prod` inside the derived
   reboot window: a run inside it ends `deferred` without waiting, and a derived `schedule:`
   run promotes at the window's end. Resolution: contracts.md's C2 row is amended before
   D5.1b merges. Its register row names all seven, the observe-only `soak` carve-out from
   D5.3b2 to D5.3b3 among them.
   **Applied on 2026-10-16.** C2 states all seven, and the gate before D5.1b merges is clear.
5. **C3 changes.** It gains the non-tag Kustomizations `<tag>-snapshot`, `<tag>-verify` (R4's
   Open question 1), `soak` and `homelab-namespace`, and names `homelab-namespace` as the
   namespace's owner. `<tag>-verify` depends on the tag's last Kustomization. It gains
   `spec.suspend` through `flux_suspend`, rendered only when true, and edges to a suspended
   Kustomization are dropped. A hand write to a Flux object uses the field manager
   `homelab-operator`. Every non-tag Kustomization sets `deletionPolicy: Orphan`, and
   `<tag>-snapshot` inherits `<tag>`'s `dependsOn`. C3 already names shared kinds,
   `SHARED_KINDS`, the reserved names and the allowlist census, from the integration round.
   Resolution: contracts.md's C3 row gains the rest before D5.1a1 merges, as its
   *Requested amendments not yet applied* lists. Open question 16 adds one change to the
   same row. C3's per-role `prune: false` also widens. C3 allows it "for H1's vendored
   CRDs", and D5 sets it on traefik and `flux`, whose renders carry their own CRDs. The
   register row gains the widening, gated before traefik's flip PR merges.
   **Applied on 2026-10-16.** C3 states the rest, and the gate before D5.1a1 merges is clear.
   Its first per-role field reads "`prune: false`, for a plumbing tag (H1) or a tag whose
   render holds a CustomResourceDefinition (D5)", one row under H1.1a's gate (H1 Open
   question 2). traefik's flip waits on H1.1a, so its gate is clear too.
6. **C7 changes.** The soak's verdict reaches GitHub as the commit status `flux/soak`, a channel
   C7 does not list. A rule labelled `verify="true"` is a verify successor that the soak reads,
   whatever its severity, so C7 gains that label. The soak reads alerts with M1's
   `shadow!="true",alertname!="Watchdog"` filter. Resolution: contracts.md's C7 row is amended
   before D5.3b1 merges. That precedes every conversion PR that adds a `verify="true"` rule,
   and R4's PR 1, which maps successors against the three shapes in *What counts as a verify
   successor*.
   **Applied on 2026-10-16.** Round 1 put all three parts in C7, for X-07: `flux/soak` and
   M1's filter in *The soak*, and `verify="true"` in *Rule labels* and *Verify successors*.
   The gate before D5.3b1 merges is clear.
7. **D1 says D5's last PR moves the flux role.** D5 moves it second to last, before the
   namespace, so that the namespace's gate closes in the commit that hands it to Flux.
   Resolution: D1 edits its sentence.
8. **Public packages or a pull secret?** Default: public, as `homelab-manifests` and
   `texbrain` are, because a context that renders a placeholder never builds. Resolution: the
   operator decides before D5.4a merges (**operator-only**). **Ruled 2026-10-16: public.**
9. **Which vantage point and channel does crowdsec's off-cluster verify use?** This plan's
   choice is the Pi timer, counted by a metrics stage in the Pi's Alloy and read through
   Prometheus's direct `alloy-pi` scrape. The log path through loki-homelab is ruled out,
   because it crosses the edge that holds the ban. The alternative is the textfile collector on
   the Pi's node_exporter, which `optimize_pi` runs with `--no-collector.textfile` today, read
   through the `node-pi` scrape. M4's blackbox_exporter fits only if it gains a probe outside
   the cluster. Resolution: crowdsec's first conversion PR confirms the channel, and crowdsec
   does not flip without it.
10. **C1's output layout grows by two digest fields and the image contexts.** C1 already
    lists the shared-kind files and directories, `DIR/cluster/<tag>-snapshot/` and
    `DIR/cluster/<tag>-verify/`. It dates `DIR/digests.json` from D2's first publishing PR,
    with an `own` and a `footprint` digest per name. D5.1a2 adds two fields to each tag's
    object, `claims` and `suspended`, which C1 does not list. D5.4a1 adds the image
    contexts: `DIR/images/<name>/`, holding each of the ten rendered contexts, and
    `DIR/images/tags.json`, mapping each name to its content tag, `sha-<12 hex>` over the
    context's `data` as `image_builder_content_tag` computes it. A flux-owned consumer's ref
    reads that tag from the same render, never from
    `render_guard.py:BUILT_IMAGE_TAG_STUBS`, and no Kustomization's path reaches
    `DIR/images/`. Resolution: contracts.md's C1 row is amended, for the two fields before
    D5.1a2 merges and for the contexts before D5.4a1 merges, as its
    *Requested amendments not yet applied* lists.
    **Applied on 2026-10-16** for the two fields, and the gate before D5.1a2 merges is
    clear.
    **Applied on 2026-10-16** for the image contexts too. C1 *Cluster output* states
    `DIR/images/`, `tags.json` and the content-tag rule, and the gate before D5.4a1 merges is
    clear.
11. **Where a verifying Job lives.** D4 places netpol-baseline's probe Job in
    `netpol-baseline-deferred` and makes it a named member there (D4's Open question 4). D5
    puts every verify Job in `<tag>-verify`, which depends on the tag's last Kustomization, so
    the order is the same. Resolution: D4 replaces its placement rule with that edge, and its
    named-member test moves to `netpol-baseline-verify`, before D5's netpol-baseline PR.
12. **Which converted verify rules page?** A converted rule is `warning` by default, which
    pages Discord only, and the soak reads it through `verify="true"`. Resolution: each
    conversion PR names its rules, and the operator decides per rule whether it becomes
    `critical` (**operator-only**).
13. **Is `flux_suspend` a temporary stop or a lasting mode?** D5.1d treats it as temporary: a
    suspended tag takes no change until it resumes. H1 has resolved this on its side: H1.7b
    retires `k3s_manage_backup_targets`, H1 sets no `flux_suspend`, and H1.1a defines none of
    it (H1, *The backup plane and its levers*). D5.1b stays the only definer of the key.
    - **H1's fallback.** If the operator keeps the mode in H1.7b's review (H1 Open question
      10), H1.7b renders the flag through `flux_suspend`, waits on D5.1b and D5.1d, and
      accepts D5.1d's freeze, under which a disarm PR must also resume the tag. D5.1d's gate
      reads the rendered suspend flag, so a PR that sets the variable back to true may
      change the render.
    - Resolution: the operator rules in H1.7b's review (**operator-only**). D5 needs no
      change either way. **Ruled 2026-10-16: temporary.** The operator retired
      `k3s_manage_backup_targets` (H1 Open question 10), so H1's fallback is not taken.
14. **The gate's stance and each `pods/exec` grant.** Under Flux the `cronjob-gate`
    successors fail on any non-zero exit, where today's gate fails only when the container
    never starts. Any exec that a verify or mutation Job keeps needs a namespace-wide
    `pods/exec` grant. Resolution: the gate PRs and the conversion PRs each state theirs, and
    the operator decides in that PR's review (**operator-only**).
15. **Who owns registry GC?** H2.6b moves it into an orchestrator CronJob that scales
    `deploy/registry`, and D5 retires it before registry flips. Both cannot hold, and the
    orchestrator would be a second writer on a Flux-owned `spec.replicas`. Decided here: D5
    retires the GC in Wave D, and H2 drops H2.6a, H2.6b and the `registry-gc` row of its exit
    criterion. D5's Wave D flips of six other roles wait on their H2 family PRs, and
    contracts.md's index narrows H2's row to H1.1a to H1.8, so the chain from D5 Wave D
    through H2 never reaches H1.9, R4 or D6.
    - H1.7b needs no part of D5, so the index has no loop through H1. Only under H1's Open
      question 10 fallback does H1.7b need D5.1b and D5.1d. The index read at slice
      granularity would then loop: D5 needs H2, H2 needs H1, and H1.7b needs D5.1b. The
      PR-level graph would still have none. D5.1b and D5.1d need only D3.3 and D4, H2 needs
      H1.1a to H1.8, and only D5's Wave D flips need H2. The index would then name an H1.7b
      sub-edge rather than a whole slice.
    - Resolution: H2 records the decision, and the contracts index gains the PR-level edges,
      before H2.6a would have merged. If the registry stays as a pull-through mirror
      (Open question 2), the PR that converts it states how the mirror expires blobs.
16. **When do `soak` and `homelab-namespace` join `SHARED_KINDS`?** C3 says D5.1a1 adds
    both. D4's census fails a member with no file, and no file renders either kind until
    D5.3b2 and the namespace PR, so D5.1a1 would fail its own census. D5 therefore adds each
    in the PR that renders it, and D5.1a1 instead reserves both names in a literal list, so
    no deploy tag can take one first (*Where each Kustomization is written*). D4's text on
    D5.1a1 has the same error. Resolution: contracts.md's C3 row reads "D5.3b2 adds `soak`
    and the namespace PR adds `homelab-namespace`; D5.1a1 reserves both names", before
    D5.1a1 merges, and the change joins *Requested amendments not yet applied* with that
    gate. D4 corrects its sentence in the same round.
    **Applied.** contracts.md's round 2 made the change (X2-11). C3 *Shared kinds* names
    D5.3b2 and the namespace PR, and *Reserved names* names D5.1a1. The gate before D5.1a1
    merges is clear.
17. **What does C2's revert rule leave open for the soak?** C2's proposed revert rule
    replaces the trade this question asked about, a fleet-wide soak whose failure reset
    `prod` and rolled back other PRs' healthy changes. The soak reads R's changed set (*The
    soak as a Job*), and the ruling on the revert rule is the operator's under C2
    (**operator-only**). Two residual trades stay:
    - **A failed soak still resets `prod`** for a revision that changes no snapshot role,
      until a revert lands or the operator clears the hold. A critical alert on an
      unchanged service no longer stops promoting, which M1 gave as a reason for the
      fleet-wide scope. It pages through Alertmanager instead.
    - **A `failed=` hold that names `soak` should not clear by the revert rule.** A later
      commit U leaves `soak`'s footprint equal to `last-good`'s, because the footprint
      ignores R's SHA. U's soak judges only pods created after U's T0 and alerts activated
      after it, so a service still broken from T passes, and the hold would clear with no
      revert. Recommendation: C2's rule treats a `failed=` list that holds `soak` as
      ineligible, and a cross-slice note asks C2 for it.
    - Resolution: the operator rules on C2's revert rule, with this exclusion, before
      D5.3b3 makes the soak gate (**operator-only**).
    - **Ruled 2026-10-16: C2's revert rule, with the exclusion.** A `failed=` list that
      names `soak` is ineligible for the revert rule, and the cross-slice note asks C2 for
      it.
18. **C6 changes.** C6's *`land.sh`* row says that from D5 on `flux/soak` also reads
    `success` on R. D5 makes that true from D5.3b3, not from D5's first PR, because before
    D5.3b3 the soak gates nothing. It also adds a case C6 does not state: a `flux/soak`
    `failure` on R ends the wait as `deploy-failed` with the `Cause` `soak-failed`, before
    `verify` writes `DEPLOY_HOLD`. `land-wait` repeats the combined-status read on each poll
    until the soak has a verdict, where D2 reads it once. Resolution: contracts.md's C6 row
    is amended before D5.3b3 merges.
    **Applied on 2026-10-16.** C6 *`land.sh`* states the soak's wait from D5.3b3, with
    `settle`, C2's writer, as the job that writes `DEPLOY_HOLD`. The gate before D5.3b3
    merges is clear.
19. **Do `soak`'s `dependsOn` edges stay?** They make `soak` start after every tag is Ready
    and put it in every changed set that holds a tag. They also block it behind any
    unchanged tag that is not Ready. `flux/soak` then never posts on R, and `verify` times
    out and holds, which is the whole-fleet gate that C2's changed set removes.
    - **Recommendation:** the renderer drops the edges. The Job waits at run time until
      each Kustomization in its scope reads Ready at R by `status.lastAppliedOriginRevision`
      (C6), with `get` and `list` on Kustomizations in `flux-system`. C2 gains a rule that
      `soak` is in the changed set whenever the set holds a tag. kustomize-controller's
      `--concurrent` gains one worker, because the soak would hold its worker from the
      start of R's rollout.
    - **Why it is not design yet.** The rule is a C2 amendment, and the soak's timeout
      would leave D4's chained path, so D4's timeout function changes too.
    - Resolution: decided in D5.3b2's review, before D5.3b3 makes the soak gate. Until
      D5.3b3 the soak is observe-only, so the edges hold nothing.
    - **Ruled 2026-10-16: accepted.** The recommendation stands as written. The run-time wait
      is in D5.3b1, in the Job script. The edge drop in the renderer, the Kustomization
      `get` and `list` grant in the Job's RBAC, the extra kustomize-controller worker and
      the change to D4's timeout function are in D5.3b2 (step 17). The C2 rule is a
      contract amendment that the orchestrator requests once #5320's rewrite of
      `contracts.md`'s *Requested amendments* lands, and it gates D5.3b2's merge.
    - **Applied on 2026-10-16.** C2 *Verification* states that the changed set holds `soak`
      whenever it holds a tag, and the gate before D5.3b2 merges is clear.
20. **Which inbound edges could narrow, so that the D2-to-D6 interim is one short window?**
    D5's index row names about ten inbound edges from four areas: D3 and D4; R3; M1, M2
    and M6's B-1e; and H1.1a, H1.7b, H2.1 and H2's family PRs. Both planes run until D6, so
    every edge that holds D5's first PR back lengthens the interim. Candidates for the
    pre-start review round:
    - **R3 as a PR-level sub-edge** on the n8n, pi-peer-backup and authelia PRs only. The
      mechanism PRs D5.1a1 to D5.2c and D5.4 need no part of R3.
    - **M1 and M2 as a PR-level sub-edge from D5.3b1 on.** D5.3b1 is the first PR that
      reads their rules, and D5.3a's fence peer needs only the Prometheus that runs today.
    - **D3 and D4 as PR-level edges.** D5.1a1 needs D3.3 and D2.6b, and each role PR needs
      D3's per-role pieces.
    - H1, H2 and M6 are sub-edges already.
    - Resolution: the pre-start review round decides, and the index row stays as written
      until it does.
21. **Where does tdarr's pre-pull live?** A Job that must finish before `tdarr` applies
    needs a Kustomization that `tdarr` depends on, and D5 renders no such kind for a role
    without snapshot claims. Recommendation: a pre-pull Job pinned to tdarr's node, with
    tdarr's image and a no-op command, in a predecessor Kustomization that the renderer
    adds for tdarr alone. A predecessor with `wait: true` holds a worker through the cold
    pull, so it moves the 25-minute wait rather than removing it. The pre-pull's own
    `timeout` bounds that wait, and a failed pre-pull delays only tdarr. Resolution:
    tdarr's conversion PR names the kind, and the publish census admits it in the same PR.
    The suffix needs a C3 per-tag-kind amendment and a reserved name in D5.1a1's list.

## Review log

### Round 1 — per-slice adversarial review

Findings raised:

- D5-R1-01 (high): the namespace PR leaves Ansible and Flux both writing the homelab Namespace
  until the flux PR.
- D5-R1-02 (high): the `flux_suspend` stop lever either reverts itself or freezes every later
  promotion.
- D5-R1-03 (high): snapshot names carry no per-run token, so a revert or reset collides with an
  existing snapshot.
- D5-R1-04 (high): the operator volume revert races Flux's drift correction, and the plan
  defines no command to run it.
- D5-R1-05 (high): exit criterion 2 cannot be met without H1, and its command contradicts its
  own namespace list.
- D5-R1-06 (high): D5 re-owns authelia's argon2 digests, which R3 already removes.
- D5-R1-07 (high): the soak rule rebuilds M2's workload-to-service mapping and cannot see
  authelia, nut or pihole-2.
- D5-R1-08 (high): `image-rebuild.yml` cannot get a commit onto master, and a `GITHUB_TOKEN`
  change triggers no CI or promote.
- D5-R1-09 (high): D5.4 publishes ten GHCR packages but has no step to make them pullable.
- D5-R1-10 (high): host-side writes inside k8s roles are under-counted, and the census D5
  relies on does not list them.
- D5-R1-11 (high): converted verify tasks lose coverage on automerged bumps and race the
  rollout they verify; ownership conflicts with R4.
- D5-R1-12 (high): the claim that the stateful hold pages and names its snapshots has no
  mechanism behind it.
- D5-R1-13 (medium): baseline counts are wrong: 14 snapshot roles, not 16, and ten built
  images, not nine.
- D5-R1-14 (medium): the soak window misses restarts it should catch, and the soak rule's
  severity is unstated.
- D5-R1-15 (medium): D5.3 races the Prometheus network-policy peer (Ansible) against the first
  soak Job (Flux).
- D5-R1-16 (medium): moving headlamp before observability can leave `headlamp-deferred`
  depending on a Kustomization that does not exist.
- D5-R1-17 (medium): rendered Jobs that already exist break D5's Job rules, and no census
  lists them.
- D5-R1-18 (medium): crowdsec's verify needs a vantage point outside the cluster, and no class
  in the table fits it.
- D5-R1-19 (medium): PR sizes break the 100–200 line rule, or are not estimated at all.
- D5-R1-20 (medium): contract changes are mentioned inline but missing from Open questions.
- D5-R1-21 (medium): docs and an ADR that D5 changes are missing from its same-PR list.
- D5-R1-22 (medium): exit 4's checks name no members, so a partial move passes.
- D5-R1-23 (medium): the soak red-proof drill cannot run as specified.
- D5-R1-24 (medium): no per-role evidence of what the server-side takeover will strip.
- D5-R1-25 (medium): controller concurrency is shared by every Kustomization, but the verify
  timeout ignores queueing.
- D5-R1-26 (medium): the rollout windows overlap the weekly-reboot mute and its recovery
  period.
- D5-R1-27 (medium): once Flux manages itself, D1's suspend switch cannot be turned off from
  git.

Fixed:

- D5-R1-01: the flux PR now precedes the namespace PR, so `containers_list | ansible_owned` is
  empty when the namespace task's gate closes in the commit that adds `homelab-namespace`. A
  test pair covers both fixtures. The retire task keeps its gate, and the rollback text matches
  what the revert restores. Open question 7 records the change to D1's sentence.
- D5-R1-02: the lever is specified end to end in D5.1b: suspended Kustomizations leave the
  expected statuses and every `dependsOn`, `cross_plane_order.py` keeps the ordering, the
  reset hazard is stated, and the way back and the way to see it exist. Three tests cover it.
- D5-R1-03: snapshots keep `volume-snapshot`'s name shape with its per-run token, the Job never
  adopts an existing CR, and failure class 6 is named. The Job-script test has the same-name
  red half.
- D5-R1-04: D5.2c writes the ordered runbook, defines `ansible/volume-revert.yml` outside
  `deploy.yml`, adds the `reset_to_last_good` input, cites R4's exit 1 and H7 as the tool's
  keepers, and adds the speedtest drill.
- D5-R1-05: exit 2 is scoped to `homelab`, `observability` and named members selected by the
  Flux name label. kustomize-controller v1.9.6's `applyOpts.Cleanup` removes the annotation,
  which the baseline quotes.
- D5-R1-06: the argon2 row and question are gone. D5 cites R3 PR 3, keeps the TOTP seed, and
  adds the cluster-file precondition for authelia.
- D5-R1-07: D5 adds no soak rule or allowlist edit, and the Job joins through M2's
  `homelab:pod_service:info`. Its fixture restarts an authelia pod.
- D5-R1-08: `image-rebuild.yml` is dropped. Base-image refreshes arrive as Renovate digest
  bumps, and a package refresh is a hand PR. The branch rules are quoted.
- D5-R1-09: an operator-only step sets the ten packages public, Open question 8 holds the
  alternative, and each Wave C PR checks an anonymous fetch from both nodes.
- D5-R1-10: the baseline lists all ten host-writing roles, D5.1c adds a host-write census with
  crowdsec and pi-peer-backup as members it must find, and every row moves to
  `k8s_host_files` before its role flips.
- D5-R1-11, the fix half: verify Jobs live in `<tag>-verify`, which depends on `<tag>` and is
  an expected status and a soak dependency. Metric verifies become critical rules read by the
  soak, `probe.py health` alone is limited to denylisted roles, R4's Open question 1 is
  adopted, and each class has a red fixture.
- D5-R1-12: `promote.py hold` posts to Discord, naming each snapshot prefix. A test checks the
  message, and a failed post fails the run.
- D5-R1-13: the counts are 14 and ten, with the command quoted. navidrome moves to Wave A'.
- D5-R1-14: the soak anchors on each pod's first Ready time after T0, adds the scrape and
  evaluation intervals, and states its timeout. It has no rule, so it has no severity.
- D5-R1-15: the peer lands in D5.3a and is checked before D5.3b.
- D5-R1-16: observability moves before headlamp, and D4 is asked to drop namespace edges to
  Ansible-owned roles.
- D5-R1-17: the Job census's scope is stated, and every existing Job is a conversion row,
  n8n's and headlamp's included.
- D5-R1-18: an off-cluster verify class and crowdsec's plan are added, with Open question 9.
- D5-R1-19: every PR is estimated, D5.1 and D5.2 are split three ways, and large Wave D roles
  get conversion PRs before their flip.
- D5-R1-20: Open questions 4 to 7 name C2, C3, C7 and D1.
- D5-R1-21: the gitops_deploy, volume-snapshot and volume-revert `CLAUDE.md` files,
  `renovate.json` and an operator-only ADR-0012 decision are in their PRs.
- D5-R1-22: exit 4 names the ten images and checks the 14 snapshot prefixes against
  `digests.json`.
- D5-R1-23: the drill follows D2.8 and D2.9 on flux-owned bento-pdf. It needs no dispatch
  input and no throwaway Kustomization, and only clearing `DEPLOY_HOLD` is operator-only.
- D5-R1-24: every role PR quotes an operator-run server-side dry-run diff, leaving Secrets out.
- D5-R1-25: D5 states the worker use and asks D4 for a queueing term, and a measurement runs
  before Wave D.
- D5-R1-26: the window starts when M2's mute ends, at 08:15 UTC.
- D5-R1-27: the flux PR removes the git suspend lever, pins the sync objects in a test, and
  writes the hand recovery for a suspend and a broken sync object.

Refuted in part:

- D5-R1-11, the ownership half. The finding says R4's table assigns read-only verifies to R4
  PR 4. At `70591c307`, `grep -n -i 'blackbox\|read-only)'
  docs/target-architecture/render/r4-delete-k8s-ansible-path.md` prints nothing. R4's table
  *Where each half of a task file goes* names D5 as the owner of the Verify half and gives R4
  the check in its PR 1. No ownership conflict exists.
- D5-R1-17, the census half. The finding says R1's census rows never list these Jobs because
  they are templates. R1's baseline names "probe Jobs in `n8n` and `prowlarr`", because its
  rule reads the tasks that apply them, such as registry's "Prove a pod can PUSH to the
  registry over its Service". The fix to D5's own census scope stands.

### Round 2 — per-slice adversarial review

Findings raised:

- D5-R2-01 (critical): after the flux PR, kustomize-controller reverts the hand suspends in the
  revert and stop-everything runbooks, so the fixes for D5-R1-04 and D5-R1-27 fail.
- D5-R2-02 (high): GHCR refs keep `imagePullPolicy: Always`, so a GHCR or WAN outage blocks
  restarts of all ten built-image consumers, the UPS chain included.
- D5-R2-03 (high): crowdsec's off-cluster verify ships its evidence through the edge that holds
  the ban.
- D5-R2-04 (high): `<tag>-verify` depends only on `<tag>`, which contradicts D4's placement rule
  and races the deferred apply.
- D5-R2-05 (high): four non-tag Kustomization kinds and `digests.json` break D2's and D4's
  publish census and C1's layout, and no amendment is planned.
- D5-R2-06 (high): entries added after the baseline are in no wave, and the namespace PR can
  leave two writers on the `homelab` Namespace.
- D5-R2-07 (high): the registry GC cron, moved as it is, scales `deploy/registry` while Flux
  owns its replicas.
- D5-R2-08 (high): a suspended role's merged change never applies, so "Ordering stays safe" is
  false and `land-wait` never settles.
- D5-R2-09 (medium): the stateful hold covers every failure in a revision that changes any
  snapshot role, and the runbook has no branch for a failing non-snapshot tag.
- D5-R2-10 (medium): the soak and verify Jobs dial fenced backends as fresh pods, which
  kube-router's source-pod lag refuses.
- D5-R2-11 (medium): the soak's restart reading at first-Ready time is ambiguous under a
  1-minute scrape, and an absent sample is unspecified.
- D5-R2-12 (medium): a promotion that overlaps the Sunday reboot fails its soak and sets
  `DEPLOY_HOLD`.
- D5-R2-13 (medium): disarming render-records at the flux PR leaves its Kuma tile red, and D5
  uses the wrong lever.
- D5-R2-14 (medium): verify conversions cannot fit 40-to-80-line flip PRs, and crowdsec's
  conversion PR cannot render `crowdsec-verify`.
- D5-R2-15 (medium): wg-easy, the sole remote-access path, shares a Wave A' window.
- D5-R2-16 (medium): the host-write census catches read-only `delegate_to` tasks and utility
  roles, and misses `command` and `shell` writes.
- D5-R2-17 (medium): the snapshot and soak Jobs have no stated image, namespace, script
  delivery or `backoffLimit`, and `soak-<sha12>` breaks D5's own Job census.
- D5-R2-18 (medium): mutation Jobs cannot narrow RBAC to one object for authelia's TOTP seed or
  jellyfin's cap, and re-run safety under a reset is not required.
- D5-R2-19 (medium): the snapshot Job drops volume-snapshot's cap check and its own-snapshot
  listing check.
- D5-R2-20 (medium): converted metric verifies become standing critical pages and couple each
  flip PR to an Ansible-owned observability roll.
- D5-R2-21 (medium): authelia's flip precondition omits D3's SOPS manifest for its OIDC key.

Fixed:

- D5-R2-01: verified through `gh`. `fluxcd/pkg` `ssa/patch.go:matchFieldManager` matches by
  prefix, `ssa/manager_apply.go:cleanupMetadata` calls `PatchReplaceFieldsManagers`, and the
  live `speedtest` Deployment lists `kubectl-rollout`. The Baseline states the cleanup and the
  ownership chain after the flux PR. Every hand suspend uses `--field-manager=homelab-operator`
  from D5.2c on, the renderer never renders `suspend: false`, and a text test holds the
  runbook to the manager. The revert runbook no longer suspends the root. Stop-everything
  suspends the OCIRepository, because suspending the root does not stop its children. The
  broken-sync-object runbook suspends `flux-deferred`, and its hand apply keeps kubectl's
  default manager so that Flux takes those fields back. The drill runs again after the flux PR
  as exit criterion 6, and Risks maps the hazard to failure class 5.
- D5-R2-02: D5.4b sets `IfNotPresent` on every GHCR ref, a renderer census refuses `Always`
  on one, and the Risks entry states the restart dependency. nut's residual is named. H4
  keeps the `nut` pod in the cluster. The finding cites nut's `CLAUDE.md`, but the sentence
  that explains `Always` is in `ansible/roles/k8s/nut/templates/deployment.yaml.j2`, so D5.4b
  edits that comment and the registry `CLAUDE.md` line.
- D5-R2-03: the evidence moves to a metrics stage in the Pi's Alloy, scraped directly as
  `alloy-pi`. A red fixture names a blocked channel apart from a failing edge, and Open
  question 9 records the choice and the textfile alternative. The Pi's node_exporter runs with
  `--no-collector.textfile`, which is why the textfile route is the alternative.
- D5-R2-04: `<tag>-verify` depends on the tag's last Kustomization, with a renderer test on a
  deferred fixture tag. Open question 11 and a cross-slice note carry the change to D4.
- D5-R2-05: D5.1a fixes where each kind is written, and replaces the census with an allowlist
  computed from the renderer's inputs: a closed `SHARED_KINDS` set and per-tag suffixes from
  each role's inputs, so a renderer bug cannot widen it. A reserved-name check and red
  fixtures, one of them a renderer that emits an extra document, come with it. Open question
  10 amends C1, and cross-slice notes go to D3 for `cluster-vars` and to D2 and D4 for the
  census.
- D5-R2-06: a late-arrivals rule names blackbox-exporter, the flux and namespace PRs quote
  `render_targets.py` and two counts as preconditions, and exit 1 compares the `owner: flux`
  count with the `platform: k8s` count instead of writing 57. `render_targets.py:main` prints
  one comma-joined line, so the commands split on commas before counting.
- D5-R2-07: the GC is retired in a conversion PR through the existing lever
  `registry_k8s_manage_gc: false`, whose off arm gains absent arms for the cron, the script
  and the state directory. Its three monitors, its rotation entry and its cron-target row go
  in the same PR, and a host-script census row refuses a `kubectl` write verb.
  Mechanism corrected: `kubectl scale` writes through the `scale` subresource, which
  `matchFieldManager` skips, so cleanup does not touch it. The two writers are real, but
  through ordinary drift correction of the rendered `replicas: 1`. A field-manager trick
  would therefore not help, which is why D5 retires the cron.
- D5-R2-08: D5.1d replaces "Ordering stays safe". The gate refuses any change to a suspended
  tag's render except in the resume PR, `promote.py decide` holds with `suspended-changed`
  when a PR races past the gate, which master's non-strict status-check rule allows, and
  `land-wait` gains a `suspended` verdict. The hold names its way out, a revert of the racing
  change or a resume. Each has a red test. Open question 13 and a
  cross-slice note raise H1's lasting use of `flux_suspend`.
- D5-R2-09: the hold post marks every changed and failed tag, the runbook has three branches,
  and Risks and Open question 1 state the co-batched case.
- D5-R2-10: a shared retry helper for a first connection, a census row, a soak fixture, and two
  consecutive live soak passes.
- D5-R2-11: restart counts and termination times come from the API, the gate leg reproduces
  today's gate, an absent Ready sample and a restart inside a scrape gap fail closed, and the
  bounded miss is stated. The fixtures are listed in D5.3b.
- D5-R2-12: `promote.py decide` derives a no-promotion window from the reboot and mute
  variables and D4's chained timeout, and waits in its job until the window ends. A test
  derives the window. `land-wait` counts its deadline from the window's end for a merge inside
  it, so a deferred merge does not read `gave-up`.
- D5-R2-13: the flux PR sets `render_records_enabled: false` in group_vars and removes the
  tile and its group rule, and exit 1 checks the tile is absent. Corrected in the integration
  round (X-05): that lever routes to the deploy plane and never reaches the role. The disarm is
  `render_records_state: absent` in the role's defaults, in its own PR before the flux PR.
- D5-R2-14: verify Jobs, their RBAC and tests land as inert code in conversion PRs, a flip PR
  only wires them, a large deletion follows the flip in its own PR, and crowdsec's two PRs are
  split explicitly.
- D5-R2-15: wg-easy moves alone, in its own window, with the operator on the LAN and the
  recovery named.
- D5-R2-16: the offence is a host write, with the missing modules and a `command`/`shell`
  pattern. Utility roles are exempt by derivation rather than by a hand list. A `shell` red
  fixture is added.
- D5-R2-17: *What every Job carries* states image, script delivery, namespace, pod spec and
  `backoffLimit`, and the soak Job is `soak-<spec digest8>`. No kubectl image is pinned in the
  tree, so the Jobs use the Python standard library on the `python:3.14-alpine` digest that
  monitor-bridge already pins. `ansible/tests/k8s/test_pod_template_hygiene.py` leaves Jobs
  out of scope, so no priority class is required.
- D5-R2-18: authelia's seed becomes an initContainer, with a check of whether the CLI needs a
  migration first. jellyfin's cap becomes a Job whose Role covers every Longhorn volume,
  stated as the trade. A re-run rule and the `homelab/rerun-safe` census apply to every
  mutation Job.
- D5-R2-19: the snapshot Job keeps the fail-open cap read with its `DECIDED` reasoning, the
  headroom check and the listing check. Two red halves cover them, and the runbook's third
  branch is the hand recovery.
- D5-R2-20: converted rules default to `warning` with `verify="true"`, which the soak reads.
  Raising one to `critical` is operator-only (Open question 12). The rules land in conversion
  PRs, M2's rule is reused where one exists, and the soak's behaviour on a Prometheus roll is
  stated.
- D5-R2-21: authelia's precondition lists the OIDC SOPS manifest and the `decryption` setting,
  with a renderer test that names authelia.

Refuted: none. D5-R2-02 and D5-R2-07 are fixed with the corrections stated above.

### Round 3 — per-slice adversarial review

Findings raised:

- D5-R3-01 (high): Branch 1 of the revert runbook resets every changed snapshot role but
  reverts the volumes of only the failed one.
- D5-R3-02 (high): roles with `manifests_rollout: ''` lose their restart-on-change, so
  Alertmanager, Grafana, Prometheus and pihole would no longer pick up config or Secret
  changes.
- D5-R3-03 (high): verify Jobs run as pods in `homelab`, but no NetworkPolicy admits them and
  no RBAC replaces the `kubectl exec` most verifies use.
- D5-R3-04 (high): the namespace PR's retirement rule makes Ansible delete objects that Flux is
  still applying, using stale staged manifests.
- D5-R3-05 (medium): the soak's alert check does not exclude `shadow="true"` alerts or the
  Watchdog.
- D5-R3-06 (medium): a revision that rolls Prometheus re-activates alerts without `for:` that
  were already firing, and the soak fails.
- D5-R3-07 (medium): D5 has no row for several imperative halves that R4 assigns to it,
  including `cronjob-gate`'s gate Job.
- D5-R3-08 (medium): `<tag>-snapshot` has no `dependsOn`, so its snapshot can predate the
  role's apply by tens of minutes.
- D5-R3-09 (medium): D2 expects D5's soak to catch a workload that prune deleted, and the soak
  cannot see one.
- D5-R3-10 (medium): moving host-side task files "verbatim" into `setup/k8s_host_files` breaks
  their sources and variables.
- D5-R3-11 (medium): when the soak fails, the hold post does not say which branch of the
  runbook to take.
- D5-R3-12 (medium): the estimates for D5.1a, D5.2b and D5.3b are far below what each PR
  contains.
- D5-R3-13 (medium): rendering image build contexts is a C1 output that Open question 10 does
  not list.
- D5-R3-14 (medium): exit 5 contradicts D5.1a's prune label, the non-tag Kustomizations set no
  `deletionPolicy`, and the namespace rollback has an overlap.

Fixed:

- D5-R3-01: confirmed. `renovate.json`'s `k8s image {{depName}}` group makes one PR per image,
  and the exportarr pin is shared by sonarr, radarr and prowlarr
  (`ansible/tests/services/test_exportarr_sidecars.py::test_the_image_pins_stay_in_lockstep`).
  The hold post now
  prints a command block per changed snapshot role. The runbook opens with a read-only
  classify step on `.status.lastAttemptedRevision`, which kustomize-controller v1.9.6 sets
  only after `checkDependencies` passes (read through `gh`), so a role blocked upstream is not
  reverted, and `volume-revert`'s "Fail when no snapshot matches this deploy" cannot fire on
  it. Branch 1 suspends every role that attempted R before any revert, reverts each with its
  own `digest8`, resets, and resumes each. A recorded compatibility judgment is the only way
  to skip a revert. D5.2b1's two-role test fails a post that covers the failed role alone, and
  the runbook text test fails on a one-role `volume-revert.yml` command written into Branch 1.
  Risks and Open question 1 state the case.
- D5-R3-02: confirmed, and wider than raised. cloudflare-ddns also sets `manifests_rollout:
  ''` and restarts through `manifests_extra_rollouts`, and its templates carry no `checksum/`.
  D5.1e adds `checksum/restart` for every restart target D3's rule leaves bare, valued per
  workload over the ConfigMaps and placeholder-form Secrets its pod template names, plus their
  `.source` hashes. That keeps M1 PR 5's one-Secret, one-restart rule and exposes no
  plaintext. pihole's instance 2 annotation renders in `pihole-deferred`. Grafana's password
  sync becomes an initContainer. The class table gains the row and corrects the
  imperative-ConfigMap row. A cross-slice note asks D3 to drop its premise. A grep for
  `rollout restart` over every k8s role's tasks, quoted in the Baseline, finds no fourth
  member.
- D5-R3-03: confirmed. The counts match, and sonarr's `netpol_from` admits five callers. *What
  every Job carries* adds the caller census, the peers landed and probed before the flip,
  exec checks moved to network reads or initContainers, and any remaining `pods/exec` grant
  stated as a namespace-wide trade. The verify row, the role-PR preconditions, Wave D's named
  members and Open question 14 carry it.
- D5-R3-04: confirmed against D2's *Risks* and `ansible/tasks/k8s_retire.yml`. The namespace
  PR now uses D2's two-PR retirement. D5.1f's gate in `cross_plane_order.py` refuses a name
  added to `k8s_retired_services` whose entry is flux-owned on the PR's base, because Ansible
  cannot see the base commit. The claim that survives prune is an operator-only hand delete,
  and a cross-slice note asks D2 to state it.
- D5-R3-05: the soak reads both alert series with `shadow!="true",alertname!="Watchdog"`, with
  a shadow fixture and a Watchdog fixture. Open question 6 and a cross-slice note to M1 carry
  it.
- D5-R3-06: after a Prometheus roll, an alert already firing at T0 by
  `last_over_time(ALERTS{alertstate="firing"}[5m])` is pre-existing. A fixture covers a
  critical alert with no `for`.
- D5-R3-07: the Baseline lists the gate Job, crowdsec's registration and allowlists,
  arr-notification and `verify_secret_keys.yml`. Each has a class and a design: the gate Job
  is copied from the CronJob's `jobTemplate` and named `<cronjob>-deploy-gate-<spec digest8>`,
  with a test against `pull-pi-peers.sh`'s pattern; configarr's gate is a post-apply mutation
  Job; crowdsec's writes and Grafana's password become initContainers; arr-notification's
  seed is a post-apply Job; stale Secret keys fall to Flux's server-side apply. Wave C names
  pi-peer-backup's gate. The gate's stricter stance and its loss of `concurrencyPolicy:
  Forbid`'s protection, which needs the CronJob's UID, are stated, with Open question 14.
  crowdsec and grafana are both `Recreate`, so each initContainer runs with no server beside
  it.
- D5-R3-08: `<tag>-snapshot` inherits `<tag>`'s `dependsOn`, with a renderer test.
- D5-R3-09: fixed with a different mechanism. A soak comparison of the rendered list with the
  live API cannot see the failure: a dropped template is absent from the render, so no
  rendered list names it, and anything rendered but missing already fails Flux's `wait`. D5.1f
  instead compares the PR's base and head renders in `cross_plane_order.py`, and fails a
  removed Deployment, StatefulSet or DaemonSet without a `Removes-Workload:` trailer. A
  handback is exempt, so reverting a flip never needs trailers, and the arm runs on pull
  requests only, because a squash merge need not keep the trailers. A cross-slice note asks
  D2 to point its sentence at this gate.
- D5-R3-10: each conversion PR moves the sources into `k8s_host_files` and the variables into
  its defaults or `group_vars/all.yml`, with no cross-role `include_vars`. A `StrictUndefined`
  render test in `ansible/tests/setup/` has a red half for an unresolved variable.
- D5-R3-11: the soak writes `failed-services:` to its termination message, the post names the
  Job and prints the read command, and the runbook reads it before the reset prunes the Job.
  The branch rule names a soak-named snapshot role as Branch 1.
- D5-R3-12: D5.1a splits into D5.1a1 to D5.1a3, D5.2b into D5.2b1 and D5.2b2, D5.3b into
  D5.3b1 (the script, inert), D5.3b2 (the Kustomization) and D5.3b3 (`verify` waits on it),
  and D5.4a into D5.4a1 and D5.4a2, each re-estimated from its parts.
- D5-R3-13: D5.4a1 adds `DIR/images/<name>/` and `DIR/images/tags.json`, and consumers read
  the tag from the same render, never from `BUILT_IMAGE_TAG_STUBS`. Open question 10 carries
  the C1 amendment.
- D5-R3-14: every non-tag Kustomization sets `deletionPolicy: Orphan`, with a renderer test.
  Exit 5 no longer claims `prune: disabled` on the LimitRange, which `deletionPolicy: Orphan`
  protects instead. The namespace rollback states the deploy-plane apply at the next tick,
  the overlap with Flux at the old `prod`, and why identical values make it harmless.

Refuted: none.

### Round 4 — integration review

Findings raised:

- X-04 (high): D3.3's non-tag `cluster-vars` Kustomization collides with D2's and D4's publish
  census and verify set.
- X-05 (high): D5 disarms render_records through `group_vars/all.yml`, which the deployer routes
  to the deploy plane, so nothing removes the producer.
- X-06 (high): H2 keeps registry GC as a scaling CronJob, D5 retires it, and no dependency edge
  orders them.
- X-07 (high): R4 exit 5's accepted verify-successor shapes contradict the successors D5
  builds, so R4's exit cannot be met.
- X-13 (medium): `flux_suspend` is a lasting disarm mode in H1 and a temporary stop in D5, and
  H1.1a may define it first.
- X-14 (medium): no slice retires D5's `k8s_host_files` role, and D5's Pi-side crowdsec changes
  race H5's compose move.
- X-16 (medium): D5 refutes D3's restart-trigger premise, and D3 is not updated.

Fixed:

- X-04: confirmed. D3 *Design* publishes `out/cluster/cluster-vars/`, and D3.3 makes `promote`
  wait on `flux/cluster-vars`, while D2's census admits only `out/flux/<tag>.yaml` for
  flux-owned tags. *Where each Kustomization is written* now has D3.3 introduce the shared-kind
  rule: `out/flux/cluster-vars.yaml` holding exactly `cluster-vars`, a closed `SHARED_KINDS`
  set starting as `{cluster-vars}`, and the reserved name. D2's per-file status rule and D4.3's
  per-name rule both expect `flux/cluster-vars` from that file, so the verify set needs no
  change. D5.1a1 extends the rule with the per-tag kinds, `soak` and `homelab-namespace`.
  Open questions 5 and 10 move the C1 and C3 amendments for the shared kind to before D3.3,
  and the D5.1a1 rollback now restores D3.3's census. Corrected in round 10 (D5-R10-03): the
  integration round's C3, D2.6b and D4.3a introduce `SHARED_KINDS` empty and D3.3a adds
  `cluster-vars`. `soak` and `homelab-namespace` join in D5.3b2 and the namespace PR, and
  D5.1a1 reserves both names (Open question 16).
- X-05: confirmed by measurement at `70591c307` with
  `deploy_changes.py:services_from_changed_paths`: `group_vars/all.yml` gives
  `{'broad': True, 'broad_deploy': True}`, and the role defaults path gives `{'broad': True,
  'broad_setup': True, 'setup_roles': {'render_records'}}`. A new render-records disarm PR,
  after the last Wave D flip and before the flux PR, sets `render_records_state: absent` in
  the role's defaults and removes the Kuma tile and group rule. It quotes the measured route of
  its changed paths, `{'broad': True, 'broad_setup': True, 'setup_roles': {'render_records'},
  'k8s': {'uptime-kuma'}}`, and checks that the units are gone. It lands while the list still
  holds `flux`, so the producer never runs on an empty list. Exit 1, the Baseline, the
  rollback for both PRs, the docs list, a Risks entry and D5-R2-13 now match.
- X-06: confirmed against H2's *Registry GC is an orchestrator pod* and H2.6b. D5 is now the
  only owner: Open question 15 records the decision that H2 drops H2.6a, H2.6b and its
  `registry-gc` exit row. D5's header, the class table and *Host-side rows* add the edge from
  Wave D's flips of crowdsec, configarr, janitorr, qbittorrent, artifacts and observability to
  H2.1 and their H2 family PRs. registry's flip waits on no H2 PR. The host-write census's
  `allow` reasons and `must_find` follow the split. Open question 15 states that the PR-level
  graph is acyclic, and that the index must name PRs on the D5, H1 and H2 rows.
- X-07: confirmed against R4 exit 5's first two shapes. *What counts as a verify successor*
  lists the three shapes R4 must accept: a rule with `service="<tag>"` and either
  `severity="critical"` or `verify="true"`; a Job in `<tag>-verify` depending on the tag's last
  Kustomization; and `probe.py health <tag>` for a `k8s_autodeploy: false` role only. Open
  question 6 moves the C7 amendment to before D5.3b1 merges, ahead of every conversion PR and
  of R4's PR 1.
- X-13: D5.1b is the only definer of `flux_suspend`. H1.1a drops its fallback, and H1.7b
  waits on D5.1b and D5.1d. D5.1d's gate reads the suspend flag from `digests.json` at base and
  head, never the key, so H1's derived suspend is covered, and a PR that re-arms
  `k3s_manage_backup_targets` may change the render. Open question 13 holds the proposal for
  H1 to state. Corrected in round 10 (D5-R10-05): H1 retired the flag instead and sets no
  `flux_suspend`, so H1.7b waits on D5.1b and D5.1d only under H1's Open question 10
  fallback.
- X-14: *Host-side rows* lists every `k8s_host_files` row and its successor. docs's directory
  goes to H7, pi-peer-backup's two Pi writes and the crowdsec probe timer go to H5, and the
  absent arms are deleted with the role by H7. crowdsec's verify section states the ordering
  with H5.8 for the Alloy stage, and the cross-slice notes ask H5 and H7 to list the rows.
  Corrected in round 10: the list missed the absent arms H2 leaves in four roles and
  artifacts' two directory tasks (D5-R10-01), and pi-peer-backup's two Pi writes go to
  H7.4, not H5 (D5-R10-06).
- X-16: the Baseline now carries the census. `grep -rln` for an empty `manifests_rollout`
  over `ansible/roles/k8s/*/tasks/` names eleven directories, `manifests` by a comment.
  Of the other ten, only cloudflare-ddns, observability and pihole run a long-lived pod that
  reads a ConfigMap or Secret. D5 moves all ten, and D5.1e lands before Wave A. A cross-slice
  note asks D3 to drop its premise or quote this census. Corrected in round 10 (D5-R10-04):
  D3 adopted D5's per-workload value under the name `checksum/restart` and covers
  cloudflare-ddns, so D5.1e only widens it to the self-rolled workloads of pihole and
  observability, and replaces D3's interim census.

Refuted: none.

### Round 10 — per-slice adversarial review

Findings raised:

- D5-R10-01 (high): the X-14 fix is incomplete: no D5 PR moves the absent arms H2 leaves in
  crowdsec, configarr, janitorr and qbittorrent, or artifacts' two directory tasks, so D5.1c's
  census blocks those flips.
- D5-R10-02 (high): after the namespace PR, any role handback re-arms Ansible's namespace
  apply beside `homelab-namespace`, an open-ended two-writer window.
- D5-R10-03 (high): the X-04 fix contradicts C3, D2.6b and D4.3a on who creates
  `SHARED_KINDS` and when `soak` and `homelab-namespace` join it, without saying so.
- D5-R10-04 (medium): D5.1e is stale against D3's X-16 fix: D3 stamps `checksum/restart` and
  covers cloudflare-ddns, but D5 still describes `checksum/role-config`.
- D5-R10-05 (medium): the X-13 text is stale: H1 retired `k3s_manage_backup_targets` and never
  renders `flux_suspend`, so H1.7b does not wait on D5.1b or D5.1d.
- D5-R10-06 (medium): D5 hands pi-peer-backup's two Pi writes to H5, but H5, H2 and H7 assign
  them to H7.
- D5-R10-07 (medium): the PR that creates `k8s_host_files` must edit
  `ansible/initial_setup.yml`, which parks the GitOps tick, but the plan says the tick applies
  it.
- D5-R10-08 (medium): the role-PR server-side dry-run diff cannot show what the takeover
  strips, and it diffs a placeholder render.
- D5-R10-09 (medium): the soak fails on any critical or `verify="true"` alert, or any
  restart, in any flux-owned service, and a reset then rolls back a good revision; the trade
  is unstated, and M1's refutation rests on stale text.
- D5-R10-10 (medium): the reboot-window wait holds the `promote` concurrency group for up to
  about 2h15m, which can delay or cancel a `reset_to_last_good` dispatch, and the hold
  re-check is unspecified.
- D5-R10-11 (medium): exit 3's command reads the last ten master commits, not the last ten
  promoted ones, so it can pass with no promoted commit checked.

Fixed:

- D5-R10-01: confirmed. H2's *What H2 leaves in `k8s_host_files`* leaves absent arms in four
  roles and artifacts' two directory tasks, and `ansible/roles/k8s/artifacts/tasks/main.yml`
  holds "Ensure the local artifacts directory exists" and "Ensure a peer directory exists for
  every synced host", both `ansible.builtin.file` with `state: directory`. *Host-side rows*
  and Wave D gain one `k8s_host_files` conversion PR per role for crowdsec, configarr,
  janitorr, qbittorrent and artifacts, after the role's H2 family PR. That PR is the `allow`
  reason for those roles, and observability's stays H2.7. Each row names its successor: H7.18c
  for the absent arms, and daniel-box's host tree in H7 for artifacts' directories, which a
  cross-slice note asks H7 to list. artifacts' task file is a `must_find` member, and an
  absent arm in an unlisted role is a red fixture. Shared variables such as
  `artifacts_peer_sources` no longer hoist to `group_vars/all.yml`, which routes to the
  deploy plane only (Baseline). They keep a test-held copy, as H2.7 does. *Kept small*, the
  D5.1c bullet and the X-14 entry are corrected.
- D5-R10-02: confirmed against `ansible/deploy.yml`'s "Build the workload namespace manifest"
  and "Apply the workload namespace". The namespace PR now deletes both tasks in the commit
  that adds `homelab-namespace`, so no handback can re-arm them. Option 1, a
  `k8s_namespace_owner` flag, was rejected because it adds a second lever that must stay in
  step with `SHARED_KINDS`. The tests that read the tasks move to the rendered
  `homelab-namespace` or are deleted, as the Baseline lists. The new test's red half is a
  fixture play gated on `containers_list | ansible_owned | length > 0`, run against a
  handed-back inventory fixture. Exit 5, the rollback and Risks follow.
- D5-R10-03: confirmed against C3's *Shared kinds*, D2.6b and D4's *A shared kind has its own
  file*. *Where each Kustomization is written* now follows them: D2.6b and D4.3a ship
  `SHARED_KINDS` empty, D3.3a adds `cluster-vars`, and D5.1a1 adds the per-tag kinds and the
  reserved names. `soak` and `homelab-namespace` join in D5.3b2 and the namespace PR, because
  D4's census fails a member with no file. D5.1a1 reserves both names in a literal list, so
  its `soak` red fixture still fails. Open question 16 names the C3 change, and the
  cross-slice notes carry it to contracts.md and D4. The X-04 entry and the D5.1a1 rollback
  bullet are corrected.
- D5-R10-04: confirmed against D3's *Restart triggers*. D5.1e now widens D3's
  `checksum/restart` to the `manifests_self_rollouts` entries of pihole and observability
  only, replaces D3's interim census, and uses pihole-2's entry as its red fixture.
  cloudflare-ddns leaves D5.1e's targets, tests, fixture and rollback bullet, and
  `checksum/role-config` is gone from the plan. The X-16 entry records that D3 adopted the
  name.
- D5-R10-05: confirmed against H1's *The backup plane and its levers* and H1's header. The
  header, D5.1b's bullet and Open questions 13 and 15 now say H1 retired the flag. D5.1b
  stays the only definer of `flux_suspend`, and the loop argument is conditional on H1's
  Open question 10 fallback. The X-13 entry is corrected.
- D5-R10-06: confirmed against H5's *Rows H5 takes from D5's `k8s_host_files`* and H7.4.
  pi-peer-backup's two Pi writes now go to H7.4's `pi-peer-backup-target` component, and only
  the crowdsec probe timer goes to H5.14.
- D5-R10-07: confirmed by measurement at `70591c307`:
  `services_from_changed_paths(['ansible/initial_setup.yml'])` gives `broad_manual=True` and
  `manual_playbooks={'ansible/initial_setup.yml'}`, and a path under
  `ansible/roles/setup/k8s_host_files/` gives `setup_roles={'k8s_host_files'}`. A new PR,
  D5.1h, creates the role and its playbook entry alone, merges into a converged deployer, and
  states the park, `land.sh`'s output and the clear. The hand apply is **operator-only**,
  because `initial_setup.yml` decrypts its become password and the agent user holds no age
  key. The operator fast-forwards only to D5.1h's merge commit, so no other merge's tick-owned
  apply is skipped.
- D5-R10-08: confirmed against the Baseline's `cleanupMetadata` reading. A new read-only
  `probe.py takeover <tag>` (D5.1g) lists each field path that a cleaned manager owns and the
  render omits, with speedtest's `restartedAt` as its red half. The operator's diff becomes
  `flux diff kustomization` over the substituted render. `flux2`'s
  `cmd/flux/diff_kustomization.go` declares `--path` and `--kustomization-file`, and its
  `build_kustomization.go` says substitution from Secrets is skipped only under `--dry-run`.
  Both quote field paths only.
- D5-R10-09: the fleet-wide scope is kept as a stated trade. The soak cannot narrow to R's
  tags from its render, because `decide` picks T against `prod` only at run time. The cost, a
  reset of a healthy revision on an unrelated alert, is in *The soak as a Job*, in Risks and
  in Open question 17 (**operator-only**). A cross-slice note asks M1 to correct its
  description of the soak. M1's Watchdog refutation still holds, through the soak's
  `alertname!="Watchdog"` filter.
- D5-R10-10: confirmed. `decide` reads `DEPLOY_HOLD` before the window wait and again after
  it, with a test where a hold set during the wait stops the promotion. The reset input runs
  in its own concurrency group, `promote-reset`, ignores the window, and refuses without a
  hold. The broken-sync-object runbook therefore sets the hold first. Risks and Open
  question 4 carry the case.
- D5-R10-11: confirmed. Exit 3 now keeps only the master commits that carry a `flux/homelab`
  status, which C2 and C6 post only on an applied revision's commit. It reads each one through
  the combined-status endpoint with `--paginate`, and requires ten such commits, each with a
  `success` `flux/soak`. The call shape was run against two master commits through `gh`.

Refuted: none.

### Round c1 — per-slice adversarial review

Findings raised:

- D5-C1-01 (high): the soak's T0 check compares a digest-form revision with a commit SHA, so
  every soak can fail.
- D5-C1-02 (high): the runbook's classify step reads `lastAttemptedRevision`, which never
  holds R, so a role that applied R can be sent to Branch 2.
- D5-C1-03 (high): D5.3b2 makes `flux/soak` gate promotion at once, so the observe-first step
  D5.3b3 assumes does not exist.
- D5-C1-04 (high): traefik's CRDs and Flux's own CRDs move into tags that render
  `prune: true`, against D6.19's CRD guard.
- D5-C1-05 (medium): the broken-sync-object recovery applies server-side without
  `--force-conflicts`, so it fails on exactly the fields it must fix.
- D5-C1-06 (medium): the broken-controller-upgrade runbook sets no hold, reset or suspend,
  so the restored controller re-applies the broken upgrade.

Fixed:

- D5-C1-01: confirmed through `gh` against kustomize-controller v1.9.6.
  `internal/controller/kustomization_controller.go:getOriginRevision` returns
  `a.Metadata[OCIArtifactOriginRevisionAnnotation]`, whose value is
  `"org.opencontainers.image.revision"`. C6 and D4's *Only the origin revision names a
  commit* state that `artifact.revision` holds `prod@sha256:<digest>`. *The soak as a Job*
  now names `.status.artifact.metadata["org.opencontainers.image.revision"]`, compared with
  `master@sha1:<R>`. D5.3b1 gains the passing digest-form fixture and the failing mismatched
  one. Risks gains the failure-class-2 entry. Branch 1's step 3 and both flux recovery
  branches now name the same key when they wait for `last-good`.
- D5-C1-02: confirmed. In the same file, `reconcile` sets `revision :=
  src.GetArtifact().Revision` and then `obj.Status.LastAttemptedRevision = revision`. The
  post now prints `prod@sha256:<digest>` beside each classify command, with the digest of
  the `sha-<R>` tag. `prod` carries the same digest because D1's promote moves it with
  `flux tag artifact :sha-<target> --tag prod`. *Choosing the branch* compares with that
  value and cross-checks it against the OCIRepository's `.status.artifact.revision` before
  any reset. It cites D4's failure-class-2 risk. The Baseline names the field's form.
  D5.2b1's post test and D5.2c's text test each gain a red half.
- D5-C1-03: confirmed against C2's *Verification* and D2.5's `verify`, which derive the
  expected set from every Kustomization under `flux/`. Option 1 was taken. D5.3b2 adds an
  observe-only set, `OBSERVE_ONLY = frozenset({"soak"})`. `verify` neither waits on
  `flux/soak` nor reads its `failure` as a hold, and D5.3b2 tests both, with an empty-set
  red half. D5.3b3 deletes the set, and its precondition is D5.3b2's two consecutive
  `success` statuses. D2 never says whether `verify` reads a `failure` outside the expected
  set, so the set covers both reads explicitly. `land-wait` reads only the PR's own tags.
  The set is named apart from D5.1b's soak exclusion. *Where each Kustomization is
  written*, the Rollback bullets, Open question 4, Risks and the Tests list follow. Option
  2 was rejected because an attended drill still gates the first live soak.
- D5-C1-04: confirmed. `ansible/roles/k8s/traefik/tasks/main.yml` fetches the CRDs from a
  URL built on `traefik_k8s_crd_version`. D6's *No CRD sits in a tag the default policy can
  delete* needs C3's per-role `prune: false`. traefik's flip and the flux PR now set H1.1a's
  `flux_prune: false`. traefik's flip adds the renderer CRD census with traefik as a named
  member, and `flux` joins the census in the flux PR. The header gains the PR-level edge to
  H1.1a. H1's header depends on D2 and D4 only, so the edge adds no loop. The per-CRD
  `prune: disabled` label was rejected, because D6.19's guard keys on the tag's `prune`
  value. Risks gains a failure-class-5 entry. Rollback forbids reverting the setting alone.
  Verification gains a live read of both tags' `.spec.prune`.
- D5-C1-05: confirmed. The cleanup list in `kustomization_controller.go` opens with
  `kubectl` (Apply), and upstream's comment on that entry reads "to undo changes made with
  'kubectl apply --server-side --force-conflicts'". The sync-object apply is now
  `sudo k3s kubectl apply --server-side --force-conflicts --field-manager=kubectl`. The
  explicit manager makes the match to that entry hold by construction. A text test over the
  flux recovery runbook requires the flag.
- D5-C1-06: confirmed by reading the runbook line. The controller branch now runs: hold,
  suspend `flux` with `homelab-operator`, `reset_to_last_good`, the forced hand apply of
  `last-good`'s components, a wait for the controllers and the origin key, then the resume.
  The wait follows the apply, because a broken source-controller cannot report before it.
  The text test fails when either hand-apply branch applies before its hold.

Refuted: none.

### Round c2 — per-slice adversarial review

Findings raised:

- D5-C2-01 (high): no D5 PR makes `land-wait` wait on `flux/soak`, so C6 is violated
  without saying so and `land.sh` reports `settled` before the soak verdict.
- D5-C2-02 (high): the flux PR removes the only path that installs Flux on a rebuilt
  cluster, and the break-glass runbooks D5 invalidates are never updated.
- D5-C2-03 (medium): the reboot-window figures contradict D4, and the window wait eats the
  5.5-hour job cap D4 reserves for `verify`, unstated.
- D5-C2-04 (medium): adding a snapshot claim to a flux-owned snapshot role deadlocks, because
  the snapshot Job fails on a PVC that only the blocked `<tag>` would create.
- D5-C2-05 (medium): the soak's scope needs the pod owner chain through ReplicaSets, but its
  RBAC grants only pods and OCIRepositories.

Fixed:

- D5-C2-01: confirmed. contracts.md C6's *`land.sh`* row and D2's *How `land.sh` waits*
  both make `flux/soak` `success` on R part of `settled`, and *The soak as a Job* said the
  opposite. D5.3b3 now extends `promote.py land-wait`. Once the tags are Ready at R, it
  reads R's combined status on each poll. `settled` needs `flux/soak` `success` on R, and a
  `failure` ends the wait as `deploy-failed` with the new `Cause` `soak-failed`. Tests in
  `scripts/deploy_tools/tests/` take the no-status fixture as the red half, and cover
  `pending`, `success`, `failure` and a status on another commit. `land.sh --help`,
  `docs/landing.md` and `docs/flux-deploy-plane.md` change in D5.3b3. The read lands with
  the observe-only set's deletion, because before then the soak gates nothing. Open
  question 18 records the C6 change, and Verification, Rollback and Risks follow.
- D5-C2-02: confirmed. *Scenario: daniel-box is dead* in `docs/break-glass.md` ends at
  `./scripts/deploy.sh`, as does step 5 of `docs/longhorn-disaster-recovery.md`. R4's PR 7b
  writes the bootstrap section "unless D5 wrote it", so D5 now writes it. A new bootstrap
  drill PR, before the flux PR, carries R4's procedure, its renderer test and its kind
  drill (C8's first substitute). The flux PR adds *Bootstrapping Flux on a rebuilt
  cluster* to `docs/flux-deploy-plane.md` and points `docs/break-glass.md`,
  `docs/longhorn-disaster-recovery.md` and `docs/k3s-etcd-restore.md` at it, with a text
  test. Until the namespace PR, the section keeps a `./scripts/deploy.sh` step for the
  Namespace, and the namespace PR removes it. The procedure renders at `last-good`, as the
  finding asks. D5.1b rewrites the shed scenario: set `DEPLOY_HOLD`, suspend each
  flux-owned `<tag>` with `homelab-operator`, scale to zero, and later resume rather than
  run `deploy.sh`. `probe.py shed-set` gains an owner column. D5.1b is the right PR,
  because D2's and D4's leaves are flux-owned before D5 starts. The hold step is new beyond
  the finding: a hand suspend is not in `digests.json`, so `verify` would wait on it until
  it timed out.
- D5-C2-03: confirmed. D4's *The size* gives about 103 minutes of chain and up to about
  2.5 hours within `--concurrent`, and D4's cap paragraph names D5's window wait as eating
  the 5.5 hours. `decide` no longer waits in its job. Inside the window it ends `deferred`,
  and a `schedule:` trigger derived from the reboot variables promotes at the window's
  end. `decide` uses T's own derived verify timeout, from the function `verify` calls. The
  window is recomputed: reboot at about 07:35 UTC, opening near 05:05 for a revision
  within `--concurrent` and from about 02:05 at the cap, closing at about 08:15. The tests
  gain a cron-equals-derived-end check and a no-sleep check whose red half is a `decide`
  that waits. That test also checks that the scheduled run hands `verify` its full
  budget. D5.1d's `land-wait` figure now reads about 5 hours. The second hold read and its
  test go, because no wait separates the read from the move. The reset group's rationale,
  Risks and Open question 4 follow.
- D5-C2-04: confirmed against `ansible/roles/k8s/manifests/CLAUDE.md`, *The caller's
  contract*. The snapshot Job skips a claim absent both live and from `<tag>`'s
  `status.inventory`, and logs the skip. The first-apply rule is now that rule's case where
  every claim is new. A PVC that the inventory lists but the cluster lacks still fails.
  D5.2a's Job-script test gains the passing new-claim fixture and its failing
  inventory-listed twin.
- D5-C2-05: confirmed. A Deployment's pod is owned by a ReplicaSet, and M2 adds
  `replicasets` to kube-state-metrics' `--resources` for the same hop. The soak's Role
  grants `get` and `list` on `apps/replicasets` in each in-scope namespace, and a refused
  read fails the soak rather than shrinking the scope. D5.3b1 gains a restarting
  Deployment pod that fails and names its service, as the red half against a scope that
  drops it.

Refuted: none.

### Round integration-2 — integration review

Findings raised:

- X2-01 (high): `DEPLOY_HOLD`'s no-hold value is `none` in D1 and D2 but "empty" in C2, C6,
  H1, D4 and D5's reset guard, so the guard never fires.
- X2-03 (high): D3's `cluster_vars.py write` lacks the successor refusals that R2 and R3 rely
  on once Flux renders authelia and mosquitto.
- X2-07 (high): three incompatible post-H1 bring-up and DR procedures, and D5's bootstrap has
  no way to install Longhorn before the restores.
- X2-08 (high): M6's live health-gate drill needs an Ansible-owned leaf that D5 removes, and
  no edge orders the two.
- X2-11 (medium): the register and the slice index lack about 20 requested rows and
  sub-edges.
- X2-13 (medium): registry-gc's cron, Kuma tile and push token are retired by both D5 Wave D
  and M6's B6.
- X2-14 (medium): H6's host-mode gate refuses whenever any Kustomization lags its source,
  which a D5 suspend causes.
- X2-19 (medium): after D6, Grafana deploy annotations come only from `deploy.sh`, which
  deploys nothing in the cluster.

Fixed:

- X2-01: confirmed against D1's *Each read has three outcomes* and Open question 9. The
  reset input now refuses on `none`, an empty value, a missing variable and a failed read.
  Its test has the three refusal fixtures and a red half that refuses only on empty. The
  D5.2b2 summary and the Verification line follow. A new bullet names the set and clear
  commands, and every set and clear step in Branches 1 to 3, the shed steps, the controller
  upgrade, the broken sync object, the bootstrap and the soak Verification names
  `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server` or its `<reason>` form.
  The *Risks* entry D1 names was already gone with the in-job window wait.
- X2-03: confirmed. A grep of D3 for `argon2`, `{{` and `mqtt_password_hash` finds no
  refusal. authelia's precondition now requires D3's `{{` and argon2 refusals merged, and a
  new mosquitto precondition requires the `mqtt_password_hash` colon refusal. The header
  names both gates. D3, C3 and R4 changes are cross-slice notes.
- X2-07: confirmed against H1's *Bring-up and DR order after H1* and R4's *Bootstrapping
  Flux without Ansible*. The bootstrap drill PR's procedure carries H1's order: the
  OCIRepository first, the plumbing tags with their `dependsOn` closure, `cluster-vars` and
  `homelab-namespace`, the backupstore sync, the DR gate and the restores, then the root.
  It renders at `prod`, after an optional reset, which settles `prod` against `last-good`.
  The root is withheld until step 9, not applied suspended. Its render carries no
  `suspend`, so an apply followed by a `homelab-operator` patch could reconcile between the
  two commands. The finding's root-only suspend is therefore met by never applying the root
  early, so from the flux PR on no D1 role variable is needed. The window before the flux PR
  is a cross-slice note to H1. Step 6's closure excludes `flux-deferred` and the root, with a
  renderer test, and the two text tests over `docs/flux-deploy-plane.md` each read only their
  own section. The drill gains a stateful fixture tag and a
  third job that applies the root first and must fail the claim check. A text test over the
  section fails on a root apply before the restores. The flux PR edits all three runbooks,
  and H1 and R4 cite the section (cross-slice notes).
- X2-08: confirmed. M6's precondition names littlelink. littlelink's flip now waits on M6's
  B-1e, in the header and in Wave A's row. M6 depends on M2 to M5 only, so the edge adds no
  loop. A *Risks* entry names the delay. The index sub-edge is a cross-slice note.
- X2-11: D5's part fixed. Open question 4 counted six C2 changes and listed seven; it now
  says seven and names the observe-only carve-out. Open question 5 gains the widening of
  C3's per-role `prune: false` to traefik and `flux`, gated before traefik's flip. The
  register rows and the index sub-edges are cross-slice notes to contracts.
- X2-13: confirmed. The GC retirement PR removes whichever registry-gc monitor exists at its
  base, and its body quotes a grep that prints nothing. M6 dropping registry-gc from B6 is a
  cross-slice note.
- X2-14: the stop lever's *Not an expected status* says that any reader comparing
  `lastAppliedRevision` with its source drops suspended Kustomizations, H6's gate among them.
  The gate's fix is a cross-slice note to H6.
- X2-19: `promote` runs on GitHub's runners and cannot write daniel-box's syslog
  (`deploy_lib/playbook.py:annotate` writes with `logger`). So D5 makes the soak Job the
  cluster's annotation writer. It prints one logfmt line only on `success`. D5.3b3 adds a
  second annotation query, scoped in its stream selector to the soak's container, with tests.

Refuted: none.

### Round i1 — per-slice adversarial review

Findings raised:

- D5-I1-01 (high): the X2-07 fix withholds the root, but R4's PR 8 gate still requires a
  suspended-then-resumed root and a two-job drill, so R4 cannot merge.
- D5-I1-02 (high): the bootstrap reset decision has no rule, though H1 and R4 defer to D5
  for when a reset is allowed.
- D5-I1-03 (medium): R4 PR 7b's mechanical check matches the literal key-and-value form
  that D5's withheld-root rationale contained.
- D5-I1-04 (medium): the bootstrap drill runs on PR 28, before the flux PR, but applies
  sync objects from `cluster/flux-deferred/`, which may not render while the flux role is
  Ansible-owned.
- D5-I1-05 (medium): the shed steps clear `DEPLOY_HOLD` unconditionally, but a promote
  run already in `verify` can overwrite the operator's hold and reset `prod`.

Fixed:

- D5-I1-01: the R4 half was already gone, and the D5 half is fixed.
    - **The R4 half.** R4's working tree carries the withheld-root form. Its property 1
      says the root "does not exist yet" and that "No step patches the root's
      `spec.suspend`". Property 4 and PR 8 name all three jobs. R4's own Round i1 entry,
      R4-i1-H1, made that change. `grep -n -E 'both jobs|suspended with D5|while the root
      is suspended' docs/target-architecture/render/r4-delete-k8s-ansible-path.md`
      printed nothing and exited 1. No conflict remains, so no Open question records one.
    - **The D5 half.** The green job now asserts the withheld root directly. The stateful
      fixture tag renders one claim and one ConfigMap. Before step 9, the job requires
      that the tag's Kustomization and ConfigMap are NotFound. It also requires that the
      claim carries the marker label and no `kustomize.toolkit.fluxcd.io/name` label.
      After step 9, it requires the Kustomization Ready, the ConfigMap present, and the
      claim with the stand-in's UID and the Flux label. The stand-in's claim is live
      before step 9 by design, so the check excludes it from "no object is live".
      *Verification* names both reads. R4's property 4 restates the green job, so the
      added checks are a cross-slice note.
- D5-I1-02: step 2 now states the rule. `reset_to_last_good` is allowed only when no tag
  changed between `last-good` and `prod` is a snapshot role or a forward-only tag. The
  changed set uses *The stateful hold*'s and H1's predicate, read from both bundles'
  `digests.json` or the hold's post. The one exception is the compatibility judgment
  that Branch 1 already requires, recorded in the hold's thread. Without it, the render
  stays at the held `prod`. Step 2 now settles the hold before it reads `prod`'s commit
  from GHCR, because a reset moves `prod`. The bootstrap section's text test fails when step 2 drops the
  condition, and its red fixture is step 2 with the condition deleted. *Risks* gained *A
  cold start resets `prod` across a migrated snapshot*, and *Verification* names the red
  fixture.
    - While checking step 2 against H1's *Bring-up and DR order after H1*, step 7, I found
      that D5's section lacked H1's DR hold. Without it, a merge during the restores would
      time out in `verify`, because nothing can post before step 9, and the failure path
      would reset `prod` under the hand-applied render. Step 2 now sets `dr-restore` when
      `DEPLOY_HOLD` reads `none`, after no `promote` run is queued or in progress. Step 9
      clears whichever hold is set once every status reads `success`, and sends a failing
      status to the revert runbook's branch choice.
- D5-I1-03: the rationale now says "the renderer refuses a suspended sync render". Step 28
  states that the flux PR writes steps 1 to 9, the field-manager paragraph, the interim
  `./scripts/deploy.sh` step and the reworded rationale into the section. I ran R4's PR 7b
  `awk` program on a copy of steps 1 to 9 and the rationale, under the section heading and
  outside the tree. Before the edit it printed the rationale's line and exited 1. After
  the edit it printed nothing and exited 0. The namespace PR's text test now carries R4's
  program verbatim and fails unless it exits 0 with empty output. That clause lands in the
  namespace PR, because the flux PR's section still holds the `./scripts/deploy.sh` step
  that R4's red half relies on.
- D5-I1-04: no renderer symbol exists to cite, because `homelab-render` is future code at
  `70591c307`. The drill bullet now names where the sync objects come from at PR 28's
  head. C1's *Cluster output* writes `DIR/cluster/<tag>-deferred/` for every role that
  passes `manifests_deferred_files`, with no owner condition. Only the publish job selects
  tags by owner (D2, *Publication and verification*). D4's "no `flux-deferred` is
  rendered" concerns the Kustomization document in `flux/flux.yaml`, which no step applies
  by hand. The drill renders with `--role flux`. A renderer test in the bootstrap drill PR
  fails unless the Ansible-owned role's render holds the OCIRepository and the root in
  `cluster/flux-deferred/`. The flux PR's precondition already required a green drill run
  on its own head, and the drill bullet now says so.
- D5-I1-05: confirmed against *One `DEPLOY_HOLD` read is enough*, where `decide` reads the
  hold only before it moves `prod`, and C2's failure path. The shed's step 3 now reads the
  hold with `gh variable get DEPLOY_HOLD -R DanielH2018/server` before any resume. A value
  other than step 1's reason sends the operator to the revert runbook's *Choosing the
  branch*, or to D2's procedure before D5.2c lands. The step also says that a reset moves
  every unsuspended tag to `last-good`, and that a resumed tag applies `last-good`. D5.2c's
  text test fails on a shed clear that no read precedes, and its red fixture is the
  scenario with the read removed. *Risks* gained *A shed clear erases a failure hold*.

Refuted: none.

### Cross-slice follow-up (2026-10-12)

- Cross-slice note from R3: the authelia flip rewrites step 4 of R3's rotation procedure in
  `docs/secret-rotation.md` to the Flux path and keeps step 5's order.
- Cross-slice note from H1: the flux PR waits on H1.7b, in the header and in step 29, rather
  than carrying H1's step 1 variant and step 7a.
- Cross-slice note from R4 on the flux PR: step 29 names the prune of `flux-deferred` as the
  successor of D1's `verify-drill` delete and pins `prune: true` on it, and *Rollback* says
  that a revert turns R4's precondition red on `flux`.
- Cross-slice note from R4 on D5.2c: the step says that `ansible/volume-revert.yml` gathers
  facts. Confirmed at `70591c307`: `longhorn-api/tasks/resolve.yml` reads `ansible_hostname`
  four times, and `load_secrets.yml` runs `ansible.builtin.setup`.

### Cross-slice follow-up (2026-10-14)

- Cross-slice note from R2 (Open question 13,
  [#4895](https://github.com/DanielH2018/server/issues/4895)): qbittorrent's flip in step 26
  waits on R2 PR 7b, D3's SOPS manifest for `mullvad_account` with `decryption`, and a green
  run of D3.3d's `sops-manifest-type-proof` job. The header names the edge. The issue cited
  the job as D3.3c's. D3 places it at D3.3d, after D3.3c's manifest copy, which it exercises.

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review
round covers it.

- **F-01.** C2's changed set replaces "every Kustomization in `flux/`, minus suspended" in
  the exit criterion, *Where each Kustomization is written*, the stop lever and `-verify`'s
  status, D5.3b2 and D5.3b3, and Open question 4. The soak reads R's changed set at run
  time from two signed `digests.json` reads, fails toward the fleet, exits at once on an
  empty scope, and takes W from a per-service map (*The soak as a Job*, D5.3b1, D5.3b2).
  `soak`'s footprint ignores R's SHA, so a docs-only commit's set is empty. Open question
  17 points at C2's revert rule and keeps two residual trades. The stateful hold
  writes `<T> stateful=<names>` and `decide` writes `<R> suspended-changed=<tag>`. The
  fleet gate through `soak`'s `dependsOn` went to Open question 19. *Risks* was rewritten.
- **`digests.json`.** D2 creates it. D5 reads `own` for `digest8`, the gate,
  `suspended-changed`, the stateful hold and bootstrap step 2. D5.1a2 shrinks to the
  `claims` and `suspended` fields, which C1 lacks, so Open question 10 requests them. Exit
  4's `jq` reads `.own`.
- **F-03.** Open question 2 recommends the registry as a LAN pull-through mirror, ruled
  before nut's Wave C PR. nut keeps a LAN source through it (D5.4b, Wave C, *Risks*). The
  shed scenario and bootstrap step 2 gained offline branches, and D5.2c's text test
  accepts the shed branch.
- **F-06.** D5.1f's check became `Removes-Object:` over every kind, with D2's grammar, two
  exemptions, added fixtures and D2's `decide` at promote time.
- **F-17.** Exit criterion 7 and its `probe.py metric` read. D1 states no premise range,
  so the bound is the 64Mi request.
- **F-08.** Open question 20 lists edge candidates. The index is unchanged.
- **Coordinator notes.** `FluxControllerDown` in the soak's scope, the reset input's
  decide-then-write split, the shed's worker timing, two Wave D readings for D4, tdarr's
  pre-pull (Open question 21) and D2's burst projection in *Waves and windows*.

### D2.5 verification fold (2026-10-15)

- **The reset's group was justified by a `verify` inside the group `promote`.** Confirmed
  against `.github/workflows/promote.yml`: D2.5 runs `verify` and `settle` in no group.
  Fixed: *It runs in its own concurrency group* now names the wait that a reset in the group
  `promote` would sit behind, `decide`'s in-flight wait. The new bullet *It must hold C2's
  two in-flight checks* states the hazard a reset during a verification creates, and D5.2b2
  carries it. The design that answers it is open in #5079.

### Reset design fold (2026-10-15)

- **The reset's answer to C2's two checks (#5079).** *A reset input* now refuses while
  `prod` differs from `last-good`, unless `DEPLOY_HOLD` names a SHA whose `sha-<X>` digest
  equals `prod`'s. That form of hold means `prod`'s run has settled and left `prod` in
  place. The write moves `prod` only while `prod` still names the digest the read job read.
  Rejected: a plain refusal while the two digests differ, because the stateful hold leaves
  them differing by design and Branch 1 would always refuse. Also rejected: `settle`
  re-reading `prod` before it writes, because it changes D2 and still races the reset
  across groups. The runbooks for a broken controller upgrade and a broken sync object
  gain a cancel of the in-flight run, whose `settle` still resets `prod`, and C2's D5 row
  records the answer.
- **The residual race's hold form was wrong.** Confirmed against C2 *On failure*: a
  derived timeout prints `failed`, so the revert candidate's `settle` writes
  `<U> failed=<names>`, not `<U> unverified`. Fixed: *A reset input* names that hold, the
  lost record of T, and why a candidate the rule then passes still carries T's revert.
- **The wait for an in-flight `settle` had no bound when Flux is broken.** Confirmed: a
  broken Flux posts no statuses, so `verify` runs to its derived timeout. Fixed: the
  refusal names the in-progress run, and the two runbooks cancel it. `settle` runs under
  `always()`, so it still writes `<T> unverified` and resets `prod`.

### Host-write census fold (2026-10-16)

- **The baseline omitted four host writes that D5.1c's census finds (#5266).** Confirmed
  against the census in `ansible/tests/repo/_host_write_rows.py`. Fixed, one mover per
  write:
    - media-volume's three media directories move to `k8s_host_files` in media-volume's
      own conversion PR. H7 replaces them with `tmpfiles.d` lines, which the cross-slice
      note on artifacts' directories now covers too.
    - traefik's RBAC, staged at `/etc/rancher/k3s/traefik-clusterrole.yaml`, moves into
      traefik's bundle in its flip PR. It cannot move earlier, because it holds Roles in
      other namespaces. The staged file is left behind and accepted as inert.
    - game-stats' pre-#2813 record cleanup and observability's claude-otel record cleanup
      have already run on daniel-box, so the new run-once cleanup PR deletes them rather
      than moving them. That PR also retires the render-record census row, whose only
      members they are. observability therefore flips after H2.7 and that PR, not after
      H2.7 alone.
- Rejected: deleting the two cleanups in the PR that records this fold. It would retire
  #5162's row on the day that row landed, which is a decision for a PR of its own.
