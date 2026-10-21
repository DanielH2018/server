# The Flux deploy plane

Flux's source controller, Kustomize controller and notification controller run in the
`flux-system` namespace. They follow the promoted artifact, whose root Kustomization is
empty, so Flux applies nothing outside `flux-system`, and the GitOps deployer and Ansible
still deploy every service, Flux included. The plan that takes
Flux from here to owning the cluster is [slice D1](target-architecture/deploy/d1-flux-installed.md)
and the slices after it, and the decision it proposes is
[ADR-0020](adr/0020-flux-applies-the-cluster-from-a-promoted-artifact.md).

## What runs

- **Three controllers, at the release `flux_version` names** in
  `ansible/roles/k8s/flux/defaults/main.yml`. helm-controller and the image-automation
  controllers are not installed, because nothing here uses them.
- **One role deploys them**, `ansible/roles/k8s/flux/`, under the tag `flux`. It is an
  ordinary Ansible-owned k8s role, denylisted from auto-deploy (`k8s_autodeploy: false`).
- **The install is vendored.** `files/gotk-components.yaml` is the output of `flux install
  --export`, filtered and pinned by digest. Its first line is the pipeline that produced it,
  so a regeneration reviews as that pipeline plus upstream's change. The role's `CLAUDE.md`
  has the regeneration steps.
- **The pods run at `homelab-critical`**, the class of the `platform` tier, beside the image
  registry and the alert bridge. They do not run at upstream's `system-cluster-critical`.

## Upgrading Flux

Renovate opens a manual PR for a new `fluxcd/flux2` release, because its hosted app can bump
`flux_version` but cannot run `flux install`. The PR is finished by regenerating the vendored
file at the new release. Until then, the header test in `ansible/tests/k8s/test_flux_role.py`
fails, because the file's first line still names the old release. Land the finished PR with
`land.sh --tags flux`; the tick applies nothing for a denylisted role.

A regenerated `ansible/roles/k8s/flux/files/gotk-components.yaml` also runs CI's
`sops-manifest-type-proof` job, which applies the file to a kind cluster. The bump PR shows
whether the new controllers still deliver an all-digit SOPS manifest value as a string
(*SOPS manifests* below).

Flux 2.9 lists Kubernetes 1.34 to 1.36, and the cluster runs 1.37. The operator accepted
that gap on 2026-10-12. Before each bump, check `gh release list -R fluxcd/flux2` for a
release that lists the cluster's Kubernetes version.

## The `flux-system` fence

Upstream's `allow-egress` policy is the namespace's default-deny: it selects every pod in
`flux-system` and admits ingress only from pods in that namespace. The role adds
`flux-allow-prometheus`, which admits only the `app: prometheus` pods in `observability` to
the controllers' metrics port. Upstream's `allow-scraping` and `allow-webhooks` policies admit
every namespace, so the filter that generates the vendored file drops both. No Receiver exists, so nothing needs the
webhook port.

netpol-baseline's probe Job tests the fence live on every netpol-baseline deploy. It dials
source-controller, notification-controller and webhook-receiver from the `homelab` namespace,
and each connection must be refused. Each of those Services maps port 80 to a named pod port,
so the probe dials 80. The table row also names the pod port behind it, 9090 for the two
controllers and 9292 for webhook-receiver. `ansible/roles/k8s/netpol-baseline/CLAUDE.md` has
the table.

## Metrics

Two sources put Flux in the metrics plane, both in `ansible/roles/k8s/observability/`.

- **The `flux` scrape job** reads each controller's `http-prom` port (8080) every minute. It
  finds the pods by discovery, because no Service exposes that port. It drops every series
  whose name ends in `_bucket`, because no rule or board reads a Flux histogram. `_count`
  and `_sum` survive. `up{job="flux"}` is 1 for each of the three controllers while the scrape
  works.
- **kube-state-metrics exports `gotk_resource_info`**, one series per Kustomization and
  `OCIRepository`, with the labels `name`, `exported_namespace`, `ready`, `suspended` and
  `revision`, and on a Kustomization `content_digest` too. The controllers do not export this series themselves. A Kustomization's
  `revision` reads `status.lastAppliedOriginRevision`, so it carries `master@sha1:<sha>`, the
  field [contracts.md C6](target-architecture/contracts.md) names. An `OCIRepository`'s `revision` reads
  `status.artifact.revision`, the digest form. A Kustomization's `content_digest` reads the
  `homelab/content-digest` annotation, which the renderer stamps on each Kustomization `<tag>`
  with the tag's `own` digest from `digests.json`. A master commit that leaves the tag's
  directory unchanged keeps it, while `revision` moves on every commit (#5338). The config is a defaults variable,
  `observability_ksm_flux_resource_state`, passed to kube-state-metrics as one argument, so a
  change to it rolls kube-state-metrics.

**`suspended` is absent, not `"false"`, on an object Flux is reconciling.** kube-state-metrics
drops a label whose field is absent, and the flux role writes `spec.suspend` only while
`flux_sync_suspend` is true. A rule about suspension therefore matches `suspended="true"`.

**Series.** These counts were measured on 2026-10-13 at about 03:00Z, after D1.6 deployed
`observability` at 02:50:58Z:

- `count({job="flux"})` read 718 for the three controllers. Go runtime families (`go_*`) make
  up 459 of them, 153 per controller.
- `count({job="flux",__name__=~".+_bucket"})` returned no series, so the bucket drop holds.
- `count(gotk_resource_info)` read 2: the root Kustomization `homelab` and the
  `OCIRepository` `homelab`. Each promotion adds one short-lived series per object through
  the `revision` label, until the old series leaves the head block.

Both count against M1's fleet budget of 140,000 head series, which reads
`avg_over_time(prometheus_tsdb_head_series[7d])`. That average read 139,817.7 at the same
time. It held Flux for only those first minutes. With the 720 series in the whole window, it
projects to about 140,540, over the budget. The offset is the operator's decision
([#4808](https://github.com/DanielH2018/server/issues/4808)).

**The deploy's gaps.** D1.6 rolled Prometheus, through its config checksum, and
kube-state-metrics, through its new argument. Rule evaluation stopped for 4 minutes: the
Watchdog, which every 1-minute evaluation writes to `ALERTS`, has a sample at 02:49:45Z and
the next at 02:53:45Z, so three evaluations were missed. Scraping resumed sooner, because the
new pod scrapes before its rule manager starts. Prometheus's own `up{job="prometheus"}` has a
sample at 02:50:43Z and the next at 02:51:01Z. kube-state-metrics' `up` has a sample at
02:49:52Z and the next at 02:51:17Z, one missed 1-minute scrape. The M2 soak
([#4786](https://github.com/DanielH2018/server/issues/4786)) therefore has no alert
evaluations from 02:49:45Z to 02:53:45Z.

## Alerts

The `flux` rule group, `ansible/roles/k8s/observability/files/rules/flux.yaml`, pages through
M1's Alertmanager. Every rule carries `service="flux"`.

- **`FluxControllerDown`, critical.** Two rules share the name.
    - One fires when a controller's scrape has failed for 5 minutes. It carries
      `shadow="true"`, because Kuma's `Cluster Scrape Targets` tile already pages that
      condition. M6's batch A3 drops the label when it retires the tile.
    - The other fires when a controller has had no scrape target for 5 minutes, or has been
      scraped with no `controller_runtime_reconcile_total` series. Pod discovery finds no
      target for a controller with no pod, so no Kuma tile sees that loss. This rule
      carries no shadow label.
- **`FluxNotReady`, warning.** A Kustomization, or the `OCIRepository` `homelab`, has had a
  Ready condition other than `True` for 15 minutes. The rule names `homelab`, so the verify
  drill's `verify-drill`, which is meant to read Ready=False, never fires it. The rule also
  fires when kube-state-metrics has been scraped for 15 minutes with no `gotk_resource_info`
  series for the root Kustomization. That loss would otherwise leave this rule and
  `FluxSuspended` quiet. Its alert carries no `exported_namespace`, and its description
  points at the kube-state-metrics log.
- **`FluxSuspended`, warning.** A Kustomization has been suspended for more than 24 hours.
  `flux_sync_suspend` and a hand suspend both count.
- **`FluxWorkersSaturated`, warning.** For 30 minutes, every `kustomize-controller` reconcile
  worker has been busy and its work queue has not been empty, so applies are queueing.
    - The rule compares `controller_runtime_active_workers` with
      `controller_runtime_max_concurrent_reconciles` for `controller="kustomization"`, and
      reads `workqueue_depth` for the queue. It reads the maximum from its gauge, never a
      literal, because the flux role sets `--concurrent` from `k8s_rollout_batch_width`.
      The three series come from the `flux` scrape job, so the rule adds no series.
    - A broad revision fills the workers for hours while it progresses. The rule therefore
      reports queueing, not a failed deploy. It is a warning without `verify="true"`, so
      the promotion soak, which reads only alerts carrying `severity="critical"` or
      `verify="true"` ([contracts.md C7](target-architecture/contracts.md)), never reads it.
    - **The gap `verify`'s timeout cannot see.** A revision queues a reconcile of every
      Kustomization on the source, changed or not. An unchanged Kustomization that is
      unhealthy holds a worker for its whole `timeout`, retries after its `retryInterval`,
      and holds one again. The bundle does not say which Kustomizations are unhealthy, so
      the timeout leaves them out. When as many of them fail at once as there are workers,
      every apply queues, the fix included, and `verify` times out on a changed name that
      never applied. This rule is what reports that state.
    - **The shed bound is one `timeout`.** Suspending an unhealthy Kustomization needs no
      worker, because a suspend is an API write. Nothing shows that a suspend ends a
      health-check wait already in progress, so each held worker frees when its current wait
      ends. The suspended Kustomization then does not retry.

notification-controller's Discord alerts stay as a second channel beside these.
`ansible/roles/k8s/observability/tests/rules/flux.test.yaml` fires each rule and holds a
case one evaluation short of each hold. A lost `gotk_resource_info` leaves `FluxNotReady`'s
readiness arms and `FluxSuspended` quiet ([#4809](https://github.com/DanielH2018/server/issues/4809)).

### The deploy-pipeline alerts

The `deploy-pipeline` rule group,
`ansible/roles/k8s/observability/files/rules/deploy-pipeline.yaml`, pages on `promote`'s own
state. monitor-bridge reads that state each 300-second cycle and serves it as five gauges, one
series each, which the `deploy-pipeline` scrape job collects:

- `homelab_deploy_hold_active` reads 1 while `DEPLOY_HOLD` reads anything but `none`.
- `homelab_deploy_hold_age_seconds` is the time since `DEPLOY_HOLD` was last written while a
  hold stands, and 0 otherwise.
- `homelab_deploy_unpromoted_age_seconds` is the time since the oldest green master commit that
  `prod`'s commit lacks went green, and 0 when there is none.
- `homelab_deploy_prod_digest_match` reads 1 when `prod`'s digest in GHCR equals
  `PROD_DIGEST`, and also while `PROD_DIGEST` reads `none` and no `prod` tag exists.
- `homelab_deploy_reboot_window_active` reads 1 inside the no-promotion window of the
  weekly reboot, where `promote.py decide` defers every promotion. monitor-bridge derives it from the
  `weekly_reboot_*` variables, using the widest window any target has: `VERIFY_CAP_S` before
  the reboot, until M2's mute ends.

Every rule carries `service="deploy-pipeline"`. That value names no deploy tag, so no
`probe.py health` gate reads these alerts, and a standing hold cannot fail the `flux` role's
gate (D2 Open question 12).

- **`DeployHoldActive`, warning.** A hold has stood for 15 minutes, so `promote` moves `prod`
  nowhere. Alertmanager repeats it every 24 hours while the hold stands, not every 6. Read
  the hold with `probe.py landing`, and clear it as *Clearing a hold* below says.
- **`PromotionStalled`, warning.** A green master commit that `prod` lacks went green more
  than 2 hours ago, and no hold stands. Either `promote` is not running, or its runs end
  without moving `prod`, such as a run that never settles and leaves every later run
  reporting `current`. Read the workflow's recent runs. A hold silences this alert, because
  `DeployHoldActive` already pages that state. The reboot window silences it too, because
  `promote` defers inside it on purpose and the scheduled run at its end promotes. The alert
  stays quiet for 30 minutes after the window ends, which covers a late start of that run.
- **`ProdMovedOutsidePromote`, critical.** `prod`'s digest has differed from `PROD_DIGEST` for
  15 minutes. `promote` writes `PROD_DIGEST` each time it moves `prod`, so either something
  else moved the tag, or `promote` moved it and the write failed. Flux follows `prod`, so
  production runs whatever the tag names now. The 15 minutes cover the gap between a move and
  its write, plus one exporter cycle.
- **`DeployPipelineExporterAbsent`, warning.** For 15 minutes, either the scrape job is gone
  from `prometheus.yml`, or monitor-bridge is scraped without one of the five gauges. A read
  that fails exports no series for what it feeds, never a 0, and so does a deleted variable
  or an empty token. monitor-bridge logs one `deploy-pipeline:` line each cycle naming the
  read that failed. A failed scrape of the job pages through Kuma's `Cluster Scrape Targets`
  tile until M6's batch A3, so this rule leaves that case alone.

`ansible/roles/k8s/observability/tests/rules/deploy-pipeline.test.yaml` fires each alert and
keeps each one silent in the case D2's plan names.

## A `containers_list` change reaches Flux

The flux role reads `containers_list`, because each controller's priority class derives from
the `flux` entry's tier. So an added, removed or reordered entry reaches the `flux` tag
through the deployer's broad plane, beside every other role that reads the list, and
`k8s_autodeploy: false` does not stop that plane. The apply is forward-only: a failure in any
of those roles writes `hold_sha` for the whole list.

A PR that adds or removes a k8s entry therefore runs two checks before it merges, and quotes
both in its body:

1. `uv run python scripts/deploy_tools/deploy_tags.py narrow origin/master HEAD` names the
   tags the landing applies. For an added or removed entry, `flux` is among them.
2. `uv run python scripts/diagnostics/probe.py releases --stale-only` and
   `uv run python scripts/diagnostics/probe.py releases` must name none of those tags as
   stale, `unmerged` or `dirty`. Read the service names, not the exit code. A named service
   carries a change the tick would apply or revert as a side effect, so wait until its owner
   lands it.

The operator accepted that unattended apply on 2026-10-12, for this role's first landing and
for later landings that reach `flux` as a reader.

## What a takeover strips

Flux's first apply of a tag takes over every live object the tag renders. Before each apply,
`kustomize-controller` hands the fields of four managers to its own Apply entry: `kubectl` as
Apply, and `kubectl`, `before-first-apply` and `kustomize-controller` as Update. It matches each
name as a prefix, so `kubectl-client-side-apply`, `kubectl-rollout`, `kubectl-patch` and
`kubectl-annotate` all match. It skips an entry that sets `subresource`, such as `kubectl scale` on
`scale`. The apply that follows removes each handed-over field the render does not declare,
unless another manager also owns it. A server-side dry run never runs that cleanup, so no
dry-run diff shows the removal. speedtest's `kubectl-rollout` entry owns `restartedAt`, so its
takeover drops the annotation and the pod rolls once.

To list what a tag's takeover strips, run the read-only census with plain `kubectl`:

```bash
uv run python scripts/diagnostics/probe.py takeover <tag>
```

It renders the tag in placeholder mode and reads each live object with
`--show-managed-fields`. For each object it prints the field paths that a cleaned manager owns,
that no other manager owns, and that the render omits. It prints paths and manager names, never
values. It flags a Deployment, DaemonSet or StatefulSet that rolls, because a stripped path sits
under its pod template. `probe_lib/takeover.py` holds the rules.

- **Listed apart: presumed server defaults.** A create attributes every field the API server
  defaults, such as `dnsPolicy`, to `kubectl-client-side-apply`. A path only that manager owns
  and the object's last-applied config omits is presumed to be such a default. The takeover
  removes it and the server is expected to set it again to the same value. The census lists
  these under each object as *presumed server defaults*, apart from the main list, because the
  rule is inferred rather than measured. A field set at create by admission or by a create
  strategy also matches it and is not restored. A Job's generated `spec.selector` is one, and
  a field a mutating admission hook injects is another. So read that list before calling an
  object clean. `imagePullPolicy`
  goes in the main list when the render's image reference implies a value other than the live
  one, because its default follows that reference.
- **Not listed: `last-applied-configuration`.** The cleanup removes it from every object by
  design.
- **Not read: Secrets and RBAC objects.** The census skips Secrets, whose managers D3.5 handles.
  The read-only ServiceAccount cannot read Roles, RoleBindings, ClusterRoles or
  ClusterRoleBindings, so the census names each one as not checked. Its last line lists them,
  and the operator runs `takeover_census.py` (below) over them. The ServiceAccount stays
  unable to read them: its ClusterRole leaves `rbac.*` out on purpose, because the bindings map
  the escalation paths.
- **An empty census is not a clean one.** A tag with no rendered object live exits 75.

The census cannot see an apply that fails on an immutable field, such as a PVC's spec or a
selector, or one that changes a value. For those the operator renders the flip PR's head, where
the entry carries `owner: flux`, and runs the flux CLI at `flux_version` on a machine with
cluster-admin access (**operator-only**):

```bash
uv run python scripts/renderer/homelab_render.py cluster --out out --role <tag>
sudo /home/ubuntu/.local/bin/flux --kubeconfig /etc/rancher/k3s/k3s.yaml \
    diff kustomization <tag> --path out/cluster/<tag> \
    --kustomization-file out/flux/<tag>.yaml --ignore-not-found --strict-substitute
```

On daniel-box the CLI sits at `~/.local/bin/flux`, installed by a pinned chezmoi external
(dotfiles#859), and the admin kubeconfig is readable only by root. A flip has no live
Kustomization yet, so without `--ignore-not-found` the diff stops with `failed to get
kustomization object`. `--strict-substitute` fails the diff when the render names a variable
that `homelab-cluster-vars` lacks.

Without `--dry-run`, the CLI substitutes from the Secrets the Kustomization's `substituteFrom`
names, so the diff compares real values rather than `${…}` placeholders. It reads
`homelab-cluster-vars`, which is why it is operator-only. A role PR quotes object names and
field paths from both commands, never values or diff hunks.

H1's flips use a different census, `scripts/dev/takeover_census.py` (H1.1j), which is also
operator-only. A D5 role PR runs it too, over each object `probe.py takeover` names as not
checked. It compares the live object with a server-side dry-run apply as
`kustomize-controller`, which needs the admin kubeconfig, and it also prints the fields that a
manager outside the cleanup list keeps. It makes no API call itself. The operator renders the
tag, writes both inputs as JSON and passes them in. Both inputs hold whole live objects, a
Secret's `data` included, so they go in a directory only the operator can read, and the
directory goes once the census has printed:

```bash
uv run python scripts/renderer/homelab_render.py cluster --out out --role <tag>
census=$(mktemp -d)
sudo k3s kubectl get -k out/cluster/<dir> -o json --show-managed-fields > "$census/live.json"
sudo k3s kubectl apply --server-side --dry-run=server --force-conflicts \
    --field-manager=kustomize-controller -k out/cluster/<dir> \
    -o json --show-managed-fields > "$census/dryrun.json"
uv run python scripts/dev/takeover_census.py "$census/live.json" "$census/dryrun.json"
rm -r "$census"
```

`<dir>` is `<tag>`, and then `<tag>-deferred` when the render wrote one. Flux applies a
tag's deferred files as their own `<tag>-deferred` Kustomization, and `kubectl -k` reads one
directory, so the census of a tag that defers files is two runs. `probe.py takeover` reads both
directories in one run.

It differs from `probe.py takeover` in where "the render declares this path" comes from. The
probe looks each path up in the placeholder render. The census reads the dry run's
`kustomize-controller` Apply entry, which is the API server's own record of the fields the
render declares. The dry-run body is no use for this, because the dry run skips the cleanup and
its body still holds every live field. The census prints `removed`, `kept` and `rolls` per
object, lists `last-applied-configuration` under `removed` as the expected entry, and imports
the cleanup list, the flattening and the server-default rule from `probe_lib/takeover.py`. It
exits 1 when the two inputs share no object, or when a dry-run object has no
`kustomize-controller` Apply entry. Both inputs can hold Secret data, so delete the two files
after the run.

## Handing a service back

A service whose `containers_list` entry sets `owner: flux` is applied by Flux, and every
Ansible path skips it. Handing it back to Ansible takes two PRs, in this order. Every later
return of a service to Ansible follows the same order. A retirement takes two PRs as well, in
the order *Retiring a flux-owned service* states.

1. **PR H1 sets `owner: released`.** Every Ansible path still skips the service. The next
   bundle omits `flux/<tag>.yaml`, so the root Kustomization prunes the `<tag>` Kustomization,
   and `deletionPolicy: Orphan` leaves its objects serving. That bundle reaches the cluster only
   when `promote` moves `prod`, which needs `DEPLOY_HOLD` to read `none`. A hold is the likeliest
   reason to hand back. In that case the operator clears it once H1 is on master
   (**operator-only**).
1. **Wait for `NotFound`.**
   `kubectl get kustomizations.kustomize.toolkit.fluxcd.io -n flux-system <tag>` must return
   `NotFound`.
1. **PR H2 removes `owner`.** Its body quotes the `NotFound`. Once it merges, the deployer
   narrows the edit to the service's tag and applies it, and `land.sh` deploys it client-side.

Between H1 and H2 the service has no writer and keeps serving its last spec, so the two
writers never overlap. `land.sh` refuses to merge H2 while the Kustomization exists
([docs/landing.md](landing.md#the-merge-wait)). That refusal is the only guard on the merge:
a merge made outside `land.sh` bypasses it, and the `NotFound` in H2's body is then the only
check. The Flux labels and `kustomize-controller`'s `managedFields` entry stay on the objects.
No skip reads them alone, so they need no removal.

## Reading Flux objects

The read-only identity that plain `kubectl` uses reads every Flux kind. The vendored
ClusterRole `flux-view-flux-system` carries the label
`rbac.authorization.k8s.io/aggregate-to-view`, so Kubernetes adds its `get`, `list` and
`watch` on six Flux API groups to `view`, which that identity binds. No change to
`k3s_readonly_crd_api_groups` is needed. The operator accepted the aggregation on 2026-10-12.

Two guards in `ansible/tests/k8s/test_aggregated_clusterroles.py` keep the grant from
widening unseen. Every ClusterRole that aggregates into `view`, `edit` or `admin` must be on
that module's allowlist with a reason. `flux-view-flux-system` must stay read-only on exactly
its six groups, so a Flux upgrade that widens it fails CI.

To check the controllers after a deploy, run `kubectl -n flux-system get deploy` and
`uv run python scripts/diagnostics/probe.py health flux`.

## What Flux follows

The flux role applies these objects in `flux-system` from `templates/sync.yaml.j2`, after the
Flux CRDs are Established:

- **The `OCIRepository` `homelab`** polls `oci://ghcr.io/danielh2018/homelab-manifests:prod`
  every minute, with no pull secret, so the package stays public. It verifies the cosign
  signature against the identity of the `promote` workflow on `refs/heads/master`, so an artifact
  anything else pushed is never applied.
- **The Kustomization `homelab`** is the root. It applies `./flux` from that artifact, with
  `prune: true`, `wait: false` and `deletionPolicy: Orphan`. The bundle's root holds no
  objects, so its inventory is empty.
- **The Provider `github-status` and the Alert `commit-status`** post a commit status with the
  context `flux/<kustomization>` on the commit the artifact carries. A Ready reconcile posts
  `success`, and an error event posts `failure`.
- **The Provider `discord` and the Alert `deploy-errors`** post error events from every
  Kustomization and from the `OCIRepository` `homelab` to the channel the deployer posts to.

To stop all reconciliation, land a PR that sets `flux_sync_suspend: true` in the role's
defaults. It renders `suspend: true` on the `OCIRepository` and the root, and deletes nothing.
Setting it back to `false` resumes both at the next role run. With the lever off the render
omits the field, so a hand suspend survives the role's runs; a suspend the lever wrote does
not.

The signature drill uses a second lever. `flux_verify_drill: true` renders an `OCIRepository`
`verify-drill`, which follows the unsigned tag that a `verify_drill` dispatch of `promote`
pushes, and must read Ready=False with a verification error. It is not a source of
`deploy-errors`, so the drill posts nothing to Discord. Setting the lever back to `false`
deletes the object at the role's next run.

## Stopping one service: `flux_suspend`

`flux_suspend: true` in a flux-owned role's `defaults/main.yml` stops all reconciliation of
that role. `k8s_autodeploy: false` is a different lever: it stops Renovate merging the
role and, while Ansible owns it, the deployer auto-deploying its image bumps. The plan is
`docs/target-architecture/deploy/d5-bulk-move.md`, *The stop lever (D5.1b)*. A merged change
to a suspended role applies nothing until the resume, so the suspend is a temporary stop, and
three checks hold it there (D5.1d):

- **The gate.** `scripts/validate/cross_plane_order.py`'s arm 5 refuses a PR that changes the
  `own` digest of a tag `digests.json` marks `suspended` at both its base and its head. The
  same change passes in the PR that removes `flux_suspend`. The arm reads the `suspended`
  field, never the key, so a role that derives the key from another variable is covered too.
  A shared-template change whose render reaches a suspended tag waits for its resume.
- **The race.** Master's status-check rule is not strict, so a PR whose checks ran before the
  suspend merged can still merge after it. `promote.py decide` holds a revision R whose `own`
  digest differs from `last-good`'s for a tag suspended in both, and writes `DEPLOY_HOLD` as
  `<R> suspended-changed=<tag>`. The revert rule never passes that form. The way out is a
  commit that reverts the change or resumes the tag, then the operator's clear. A clear
  without one holds again at the next `decide`. `probe.py landing` prints the cause beside
  `DEPLOY_HOLD`. The `promote` job posts the hold to the deployer's Discord channel from a
  step after Decide, which holds the webhook and no token.
- **`land-wait`.** `promote.py land-wait` leaves a tag suspended at the merge commit out of
  the wait, and `land.sh` leaves it out of the health gate. When every tag it waits on is
  suspended, it ends with the verdict `suspended` rather than polling to `gave-up`, and
  `land.sh` prints `nothing-to-deploy` for it (`docs/landing.md`).

- **What it renders.** `spec.suspend: true` on `<tag>` and on each of its `-deferred`,
  `-snapshot` and `-verify` Kustomizations, and `suspended: true` on the tag's object in
  `digests.json`. The value must be a literal `true` or `false`; a Jinja expression fails the render.
- **No `suspend: false`, anywhere.** The renderer writes `spec.suspend` only when it is true.
  `kustomize-controller` applies with forced ownership, so a rendered `false` would take the
  field back from an operator's hand suspend. `suspension.suspend_false` fails the render on any
  Flux object in the bundle that sets it, a role template's included. The flux role's
  `sync.yaml.j2` follows the same rule.
- **No edge to it.** Every `dependsOn` edge to a suspended Kustomization is dropped, and the
  annotation `homelab/suspended-deps` lists it (*Ordering: `dependsOn`*). Otherwise each
  dependent would wait on the suspended tag's revision until the resume.
- **Not expected by `verify`.** A suspended Kustomization never posts. `verify` leaves out
  every Kustomization of a tag that `digests.json` marks `suspended`, so a revision that
  changes it neither times out nor resets (*Verification*).
- **It takes hold only once `last-good` carries it.** A hold-and-reset applies `last-good`'s
  bundle, which un-suspends a role whose suspend landed after it. Land the suspend PR alone,
  then check that `probe.py landing` shows `last-good` at or past it.
- **The way back and the way to see it.** A PR that removes the key resumes the role.
  `probe.py landing` prints a `suspended (Flux):` line naming every suspended Kustomization
  and `OCIRepository`, the root included, with the field manager of each `spec.suspend`. A
  suspend from git reads as `kustomize-controller`'s, and a hand suspend as
  `homelab-operator`'s.

**A hand suspend uses its own field manager.** It is operator-only, because it needs the k3s
admin kubeconfig:

```bash
sudo k3s kubectl -n flux-system patch kustomization <tag> --type merge \
  --field-manager=homelab-operator -p '{"spec":{"suspend":true}}'
```

The resume is the same command with `false`. `homelab-operator` matches none of the managers
`kustomize-controller` cleans, so the applier of the suspended object leaves the field alone.
kubectl's default manager, `kubectl-patch`, matches the `kubectl` prefix, and the next
reconcile would revert it. A hand suspend is not in `digests.json`, so a revision that
changes a hand-suspended tag times out `verify`. Set `DEPLOY_HOLD` first, as
`docs/break-glass.md`'s shed scenario does.

## Publishing and promotion

The `promote` workflow, `.github/workflows/promote.yml`, runs after each successful push run
of CI on master. It publishes the commit as the OCI artifact
`ghcr.io/danielh2018/homelab-manifests:sha-<commit>` and moves the tag `prod` to the newest
published master commit. The `OCIRepository` `homelab` follows `prod`. While no
`containers_list` entry carries `owner: flux`, the bundle's root Kustomization is empty, so a
move changes nothing in the cluster but the commit Flux reports. The workflow has six jobs:

1. `render` checks out the triggering commit and refuses one that is not on master. It builds
   the bundle (*The bundle*), runs `promote.py check-bundle` over it, and scans it with
   gitleaks in its step `Scan the bundle for secrets`. It holds read access only, and a refusal at any step pushes nothing.
1. `publish` checks out no repository code. It pushes the bundle as `sha-<commit>` and signs
   it with cosign, under a certificate that names the workflow's own identity.
1. `promote` runs `promote.py decide`, read-only. `decide` walks master from its tip back to
   `prod`'s commit and takes the first commit that is published, signed and green in CI, and
   that loses no object without a declaration (*Removing an object on purpose*). It prints
   one line, `trigger=<sha> verdict=<verdict> target=<sha>`, where the verdict is
   `promote`, `held`, `current` or `error`. On `promote` it pulls `prod`'s bundle and the
   target's before anything moves, and hands both to `verify`. Its one write is
   `DEPLOY_HOLD`, set to the target's SHA when the target fails the forward-only gate
   (*Forward-only tags*).
1. `tag` checks out no repository code. On the verdict `promote` it moves `prod` to
   `sha-<target>` and writes that digest to the repository variable `PROD_DIGEST`. When
   `last-good` does not exist yet, it first tags `prod`'s old digest `last-good`.
1. `verify` runs `promote.py verify <target>` with `contents: read` and `statuses: read`, and
   prints `good` or `failed` (*Verification*).
1. `settle` checks out no repository code. It moves `last-good` on `good`, and otherwise
   writes the hold and resets `prod`, unless the target changes a forward-only tag. Then it reads CI's green push runs on master, newest
   first. When the newest is for a commit newer than the target, `settle`
   dispatches `promote` again with `-f sha=<that commit>`. The job token makes the dispatch,
   under `actions: write`, which `settle` alone holds. A run whose `tag` found `prod`
   already moved promotes nothing, and this dispatch is what promotes its commit.

The repository variable `DEPLOY_HOLD` stops promotion. The value `none` means no hold, and any
other value holds, so a mistyped hold still holds. An empty or missing variable makes `decide`
print `error`, and no tag moves. Setting or clearing a hold is **operator-only**:

```bash
gh variable set DEPLOY_HOLD --body none -R DanielH2018/server
```

An agent session cannot run that command, nor write `PROD_DIGEST` or move a tag: the
`block-footguns` hook denies them (`.claude/hooks/_deploy_plane_rule.py`). Reading either
variable with `gh variable get` stays allowed.

A dispatch from master with the input `sha` publishes and promotes from that commit rather
than master's tip. `render` refuses it unless it descends from `origin/master`, then re-reads
its CI through the check-runs API with `promote.py ci-verdict`, and refuses anything but
green. This is how a run resumes after a hold is cleared:

```bash
gh workflow run promote.yml -R DanielH2018/server --ref master -f sha=<master tip>
```

A dispatch of the workflow from master with the input `verify_drill` pushes the bundle
unsigned to the tag `verify-drill`, for the flux role's signature drill. It skips `promote`
and `tag`, so it never moves `prod`. The drill push carries the annotation
`io.homelab.verify-drill=true`, so its digest differs from the signed `sha-<commit>` digest of
the same commit. Without it, the drill tag pointed at a signed digest and verified. The
dispatch then runs `cosign verify` on the drill digest under `promote`'s identity, and the
run fails if the verification succeeds.

## Verification

Contract C2 *Verification* is the rule, and `scripts/deploy_tools/promote_lib/verify_promotion.py`
holds it. After `tag` moves `prod` to T, `verify` waits on the commit statuses that
notification-controller posts on T:

- **What it expects.** `flux/homelab`, plus `flux/<name>` for every name in T's changed set.
  `changed_set.changed_set` computes the set from T's `digests.json` and `prod`'s, as it does
  for `land.sh`'s wait. The set holds each name whose `footprint` moved, and every
  Kustomization whose `dependsOn` chain reaches one, such as a tag's `-deferred`. It also holds
  each per-tag kind a moved tag depends on, such as its `-prepull` or `-precheck`: those sit
  above the tag in the chain, so only the tag's footprint brings them in. A suspended
  Kustomization is left out: one whose document sets `spec.suspend: true`, and every
  Kustomization of a tag that T's `digests.json` marks `suspended`. A change under `_unclassified`, or a bundle with no
  `digests.json`, expects every name, and an empty set expects `flux/homelab` alone. The set
  also holds `soak` whenever it holds a tag, because `soak` has no `dependsOn` chain to join
  through (C2).
- **The soak gates.** `verify` expects `flux/soak` like any other name in the set, so a
  revision whose soak never posts times out and holds, and a `flux/soak` `failure` holds. D5.3b2
  had left `soak` out of the set until two live soaks had passed; D5.3b3 removed that
  carve-out on 2026-10-17. `land-wait` never waits on `soak` Ready. It reads the `flux/soak`
  status on R instead (`docs/landing.md`).
- **What it never expects.** A Kustomization outside the set, so a failure that T did not
  cause never holds; the Flux alerts page it instead. A dependent whose status on `prod`'s
  commit already read `failure` is logged as `excused <name>` and not expected. `decide`
  records those statuses before `prod` moves, and `verify` reads that record rather than
  `prod`'s commit (H1.1i). A dependent is a member of the set only through its `dependsOn`
  chain, so a name whose footprint T changed is never excused.
- **T, not the trigger.** `decide` may pick a commit newer than the one whose CI started the
  run, and Flux posts statuses only on the commit `prod` names. `verify` reads T's own bundle,
  which `decide` pulled, never the render job's `out/`.
- **Only this promotion's statuses count.** `tag` stamps the time just before it moves
  `prod`, and `verify` ignores any status on T updated more than a minute before that. A
  commit promoted a second time, after a hold on it was cleared, still carries the first
  promotion's statuses, and an old `failure` would otherwise hold it again at the first read.
- **The verdict.** Every expected status `success` is `good`. Any `failure` or `error` is
  `failed`. A missing or `pending` status keeps the wait going and never reads as success, so
  the timeout ends it as `failed`, naming each name that had no `success`. *The timeout
  follows the chain* below derives it.

`settle` applies the verdict:

- **`good`** tags `sha-<T>` as `last-good`.
- **Anything else** writes `<T> failed=<names>` to `DEPLOY_HOLD`, then resets `prod` to the
  digest `decide` read for `last-good`, then writes that digest to `PROD_DIGEST`. The hold
  comes first, the order `deploy_handlers.py:_rollback_k8s` uses for `hold_sha`, so a reset
  that fails still stops the next promotion. A hold write that fails still attempts the
  reset, and the step names both results. A `verify` that ended with no verdict, because it
  crashed, timed out as a job or was cancelled, holds as `<T> unverified`.
- **Anything else, when T changes a forward-only tag**, writes T's SHA alone to
  `DEPLOY_HOLD`, tags `sha-<T>` as `held` and leaves `prod` on T (*Forward-only tags*).
- **Anything else, when T changes a snapshot role**, writes `<T> stateful=<names>` to
  `DEPLOY_HOLD`, tags `sha-<T>` as `held`, leaves `prod` on T, and the `post` job posts the
  hold (*Snapshot roles hold without a reset*). This branch comes before the forward-only one.

**Every move of `prod` writes `PROD_DIGEST`**: `tag` on a promotion, and `settle` on a reset.
A failed write fails the job after the move. Slice D2.6d's deploy-pipeline exporter compares
the variable with `prod`'s live digest and pages on a mismatch.

**The first verified run seeds `last-good`.** When the tag is absent, `tag` points it at
`prod`'s digest before it moves `prod`, so a failed verification has somewhere to reset to.

**A verification in flight blocks the next one.** Two checks enforce it:

- `prod` and `last-good` name the same digest except between a run's `tag` and its `settle`,
  or after a hold without a reset. While they differ and `prod` is not the `held` digest,
  `decide` waits, re-reading `DEPLOY_HOLD` each 30 seconds, and gives up as `current` after
  350 minutes (*The cap*).
- `tag` moves `prod` only while it still names the digest `decide` read. Two runs can decide on
  the same `prod` when the second run's `promote` job runs while the first run's `tag` waits
  on the group. The first `tag` moves `prod`, and the second finds it moved, logs a warning,
  and moves and verifies nothing. The first run's `settle` then dispatches one more run, so
  the second run's commit does not wait for the next merge (*Publishing and promotion*).

`verify` and `settle` join no concurrency group: GitHub keeps one pending job per group and a
newer one cancels it, so a pending `settle` could be dropped with its hold unwritten. Without
the two checks, a second run could move `prod` past a T that `verify` still waits on, Flux
would never post T's statuses, and the timeout would write a false hold. A run that never
settles leaves `prod` and `last-good` apart, and every later run reports `current`. Slice
D2.6d's `PromotionStalled` alert pages that stall.

**Clearing a hold** is **operator-only**: set `DEPLOY_HOLD` back to `none` (*Publishing and
promotion*), then dispatch `promote` with `-f sha=<master tip>`. `decide` promotes the newest
eligible commit.

**The revert rule is off.** `promote.REVERT_RULE` is `False`. Turning it on is an
**operator-only** ruling (D2 Open question 11), made in a PR of its own. With it on, a
`failed=` hold would let `decide` promote a descendant of the held commit whose `footprint`
for every failed name equals `last-good`'s, and `settle` would set `DEPLOY_HOLD` to `none` once
it verifies. A hold in any other form, and a `failed=` hold that names `soak`, never qualify.

**`probe.py landing` shows both holds** while the deployer and `promote` coexist:

```text
hold_sha (GitOps deployer, Ansible-owned tags): none
DEPLOY_HOLD (promote workflow, Flux-owned tags): none
prod: 3cf44936 sha256:4e55da43005e   last-good: the same digest as prod
```

It reads `DEPLOY_HOLD` with one `gh variable get`, `prod` from the `OCIRepository`
`homelab`'s `status.artifact` with the read-only `kubectl`, and `last-good` with one GHCR
manifest fetch. Each read has a 5-second timeout. A read that fails prints `unknown` on its own
line, and `prod` is reported equal to `last-good` only when both digests were read.

### Forward-only tags

Longhorn supports no downgrade, and MetalLB's CRDs carry a conversion webhook, so a reset of
`prod` after a failed bump would re-apply the older version over upgraded objects. A k8s role
whose `defaults/main.yml` sets `flux_forward_only: true` is a forward-only tag. Three rules in
`scripts/deploy_tools/promote_lib/forward_only.py` follow from it. The plan is
`docs/target-architecture/host/h1-cluster-config-to-flux.md`, *Forward-only tags and the
promote rule*.

- **A hold without a reset.** `decide` names, in its `no_reset` output, each forward-only tag
  whose `footprint` differs between T's `digests.json` and `last-good`'s, or that one of
  the two lacks. On anything but `good`, `settle` then writes T's SHA alone to `DEPLOY_HOLD`,
  never the `failed=` form, so the revert rule never clears it. It leaves `prod` on T and
  tags `sha-<T>` as `held`. `decide` reads `held` once the hold is cleared: a `prod` that
  names the `held` digest is settled, so the in-flight wait does not stall the next run.
- **The gate.** Every commit in (`last-good`, T] that changes `k3s_longhorn_version` or
  `k3s_metallb_version` must map to the merged PR whose merge commit it is, and that PR's
  head must carry a successful `homelab/upgrade-gates` status. `land.sh` posts the status at
  the merge, and `land.sh --regate <pr>` re-posts it. For a commit the move newly applies,
  in (`prod`, T], the status must be at most six hours old.
- **The backstop.** (`prod`, T] may change each pin in one commit only, and
  `k3s_longhorn_version` may move one minor release at most between `prod` and T. It catches
  a merge that bypassed `land.sh`.

A commit that fails the gate or the backstop makes `decide` write T's SHA to `DEPLOY_HOLD`
and print `held`, so nothing moves. A failed PR lookup or status read prints `error`. The way
out is **operator-only**: fix forward through a PR, run `land.sh --regate <pr>` for each bump
the hold names, then clear the hold. For a failed Longhorn bump, the recovery is
`docs/longhorn-disaster-recovery.md`, never a revert.

### Snapshot roles hold without a reset

`promote` runs in CI and cannot reach Longhorn. A reset of `prod` after a failed revision would
apply the old image onto data the failed image may have migrated, the state `volume-snapshot`
exists to prevent. So a failed revision that changes a snapshot role holds without a reset.
`scripts/deploy_tools/promote_lib/stateful.py` holds the rules, and the plan is
`docs/target-architecture/deploy/d5-bulk-move.md`, *The stateful hold, its post, and the reset
input*.

- **Which revisions.** `decide` names, in its `stateful` output, each snapshot role whose
  `own` digest differs between T's `digests.json` and `last-good`'s, or that one of the two
  lacks. A snapshot role is a tag whose `cluster/<tag>-snapshot/job.yaml` T's bundle holds
  (*The snapshot Job*). `own` is the digest that renames the snapshot Job, so a role whose
  `own` is unchanged took no snapshot for T and does not count.
- **The trigger is a changed snapshot role, not a failed one.** A revision that changes sonarr
  and breaks traefik holds without a reset too, and traefik stays broken until the operator
  acts. A reset would roll sonarr, which applied T, back onto data T may have migrated.
- **The hold.** On anything but `good`, `settle` writes `<T> stateful=<names>`, where
  `<names>` are the names `verify` printed as failed or timed out, or `unverified` when it
  printed none. The value never takes the `failed=` form, so the revert rule never clears it.
  `settle` tags `sha-<T>` as `held` and leaves `prod` on T. When T also changes a
  forward-only tag, the stateful form wins; neither resets.
- **The post.** Once the hold is written, the `post` job runs `promote.py hold`, which posts
  to the deployer's Discord channel through the `DEPLOY_DISCORD_WEBHOOK` repository secret,
  a copy of `gitops_deploy_discord_webhook`. It names the held SHA, every changed tag and
  every failed name, each marked as a snapshot role or not, and, when `soak` failed, the soak
  Job and the command that reads the services it named. Then, for each changed snapshot
  role, it prints the `digest8`, each claim's snapshot prefix, and a command block in the
  revert runbook's Branch 1 order: classify, two suspends, the `volume-revert.yml` run, two
  resumes. Beside the classify command it prints `prod@sha256:<digest of sha-<T>>`, the
  string a role that attempted T reads in `.status.lastAttemptedRevision`. CI cannot read the
  cluster, so every changed snapshot role gets a block, and the classify step picks which to
  revert. A message never passes Discord's 2,000 characters: a header that lists every tag
  splits across messages, and no line or block is cut. Each block runs the revert once per
  claim namespace, passing `k8s_namespace`, and the header says so when the checkout lacks
  `ansible/volume-revert.yml`. A 429
  gets one retry after its `retry_after`, up to 10 seconds. A failed post fails the run red;
  the hold is already written by then.
- **The way out** is **operator-only**: the runbook in `docs/volume-snapshot-drills.md`, then
  a clear of `DEPLOY_HOLD`.

### The reset input

A `promote` dispatch with `reset_to_last_good` set moves `prod` to `last-good` and does nothing
else. It runs only from master's copy of the workflow, and dispatching it is **operator-only**.
It skips render, publish and promotion, and refuses a dispatch that also sets `sha` or
`verify_drill`. `scripts/deploy_tools/promote_lib/reset.py` holds the rules.

- **Its concurrency group is `promote-reset`, not `promote`.** In `promote`, the reset would
  wait behind a `decide` that waits on a verification in flight, for up to 6 hours, and a
  run that the next merge starts would evict it while it was pending.
- **It decides, then writes.** The `reset-check` job reads with read-only tokens. The
  `reset-move` job checks out nothing and holds `packages: write`. It moves `prod` only while
  `prod` still names the digest `reset-check` read, and then records PROD_DIGEST. A live
  `prod` already on `last-good` succeeds and moves nothing. Any other digest moves nothing
  and fails, naming both.
- **It refuses without a hold.** `DEPLOY_HOLD` reading `none`, an empty value, a missing
  variable and a failed read all refuse. Without a hold, the next promote run would move
  `prod` forward again. To set a hold, run `gh variable set DEPLOY_HOLD --body <reason> -R
  DanielH2018/server` (**operator-only**).
- **It refuses while a verification is in flight.** When `prod` and `last-good` name the same
  digest, the reset moves nothing and succeeds. When they differ, it proceeds only if
  `DEPLOY_HOLD`'s first word is a commit X and the digest of `sha-<X>` equals `prod`'s. That
  hold means the run that moved `prod` has settled. Any other difference means a run is
  between its `tag` and its `settle`. The refusal names `prod`'s commit and the in-progress
  `promote` runs. To proceed, wait for that run's `settle`, or cancel it with `gh run cancel
  <run-id> -R DanielH2018/server` (**operator-only**), then dispatch again.
- **It ignores the reboot window below.** It moves `prod` back to a revision the cluster
  already ran.

### No promotion runs into the weekly reboot

The reboot kills the soak Job's pod, recreates pods and fires critical rules after the move.
M2's `weekly-reboot` mute is an Alertmanager route, so it silences nothing `verify` reads. A
promotion verifying across the reboot would therefore hold.
`scripts/deploy_tools/promote_lib/reboot_window.py` holds the rules.

- **The window.** It opens T's own derived verify timeout before the reboot, and it closes when
  M2's mute ends. The reboot is the cron's fire plus `weekly_reboot_shutdown_delay_minutes`,
  and the mute ends `weekly_reboot_mute_recovery_minutes` after the reboot. `decide` reads the
  `weekly_reboot_*` variables from `ansible/inventory/group_vars/all.yml` in its checkout.
  At the committed schedule, the window closes at 08:15 UTC on Sunday and opens between about
  02:05 and 07:30, depending on T.
- **Which timeout `decide` reads.** It is T's own, from `verify_promotion.expected_set`, the
  function `verify` calls, so the two cannot disagree.
- **`deferred`.** Inside the window, `decide` ends the run with the verdict `deferred` and
  moves nothing. It never waits in its job, because a wait before `verify` would come out of
  `verify`'s budget and time out into a false hold.
- **The scheduled run.** `promote.yml`'s `schedule:` cron fires at the window's end and
  promotes master's tip. It skips render, images and publish, because every commit merged
  in the window was published by its own run. GitHub reads the cron as a literal, so
  `scripts/deploy_tools/tests/test_promote_reboot_window.py` derives it from the same
  variables and fails when the two differ. GitHub can start a scheduled run late or skip it.
  A skipped run leaves the window's commits for the run the next merge starts.
- **`land-wait`.** Each poll inside the window moves the give-up deadline to the window's end
  plus the base timeout, and prints when the window ends. `land-wait` holds no T's bundle,
  so it reads the widest window any T has, `chained_timeout.VERIFY_CAP_S`. A `land.sh`
  deadline still caps the wait.

### The timeout follows the chain

`scripts/deploy_tools/chained_timeout.py:promote_timeout` derives `verify`'s wait from T's bundle and its changed set, the same set
`verify` expects, so the two cannot disagree. With `dependsOn` edges, a Kustomization starts
only after its dependencies finish, so the wait sums four terms, with the soak beside them:

- **The longest path.** The longest `dependsOn` path in T's bundle that ends at a changed
  name. A changed name on the path counts its own `timeout`. An unchanged ancestor counts no
  `timeout`: Flux still waits for it to reconcile T, but its apply changes nothing, and a
  healthy one passes its health check at once. Each edge on the path counts one
  `--requeue-dependency` interval, 30 seconds by default. A Kustomization whose dependency is
  not Ready requeues after that interval and no sooner. The walk covers every Kustomization in
  the bundle, so `cluster-vars` counts whenever it sits on the path.
- **The queueing term.** The smaller of two bounds on when the last changed name, dependents
  included, finishes. The first is the number of changed names divided by `--concurrent` and
  rounded up, times the largest `timeout` among them. The second is Graham's bound for
  greedy list scheduling: the other names' `timeout`s summed, divided by `--concurrent` and
  rounded up, plus the largest. The first is the smaller for equal `timeout`s. The second is
  the smaller when one long `timeout` sits among short ones, which the first would charge in
  every round (#5482). `docs/target-architecture/deploy/d4-ordering.md` has the proof. A Kustomization that
  waits on health holds one of the Kustomize controller's workers, so a revision that changes more
  names than `--concurrent` queues behind the workers. The flux role sets `--concurrent` to
  `k8s_rollout_batch_width`, 5, the width the deploy play rolls workloads at, plus one worker
  for the soak. Without the term `verify` would time out on such a revision and write a false
  hold.
- **The soak, beside the chain.** `soak` has no edges and holds its own worker from the start
  of the rollout, so it is in neither term above. When it is in the set, the first two terms
  together never fall below its `timeout`, which bounds the soak Job's whole run.
- **The `OCIRepository` interval**, 1 minute.
- **The largest `retryInterval`**, 2 minutes on every Kustomization the renderer writes.

The root `homelab`'s 5-minute `timeout` is the floor of the first two terms. The flux role
leaves `--requeue-dependency` at its default, and
`scripts/deploy_tools/tests/test_chained_timeout.py` fails a vendored install or role default
that sets it. `ansible/tests/k8s/test_flux_role.py` holds `chained_timeout.CONCURRENT` equal to
the `--concurrent` the role renders, less the soak's worker.
A `dependsOn` naming a Kustomization the bundle does not hold, or a cycle, fails `verify`
with no verdict, and `settle` holds it as `<T> unverified`.

**The cap.** The derived timeout is capped at 5.5 hours, `chained_timeout.VERIFY_CAP_S`. A
`verify` job that its own `timeout-minutes` stops leaves no verdict, and `settle` writes
`<T> unverified`, which names no Kustomization. At the cap `verify` prints `failed` with the
names that never passed, and `settle` writes that hold like any other timeout. Three limits
are sized from the cap, and `ansible/tests/repo/test_promote_workflow_verify.py` holds them
against each other:

| Limit | Value | Why |
|---|---|---|
| `verify`'s `timeout-minutes` | 335 | above the 330-minute cap plus one poll |
| `decide`'s in-flight wait, `IN_FLIGHT_WAIT_S` | 350 minutes | `verify`'s 335 and `settle`'s 5, plus 10 for runner queueing |
| the `promote` job's `timeout-minutes` | 360 | above the wait, and GitHub's limit for a hosted job |

A wait shorter than a verification is safe and costs a promotion. `decide` gives up as
`current` and moves nothing, and the commit waits for the next run.

**The size.** At the defaults, each Kustomization's `timeout` is the manifests role's
`manifests_rollout_timeout_default`, 10 minutes:

- a change to bento-pdf alone expects bento-pdf: one timeout and one queueing round, 23
  minutes with the interval and the retry;
- a change to a tag and its `-deferred` dependent: two timeouts, one hop and one round, 33.5
  minutes;
- a rotation that changes `cluster-vars` and the one tag that substitutes from it: the same
  33.5 minutes for the tags;
- the soak, which every set holding a tag also holds: its `timeout` is the tags' chain with
  every Kustomization in the bundle counted as changed, plus the fleet W and two minutes. It
  is longer than the tags' terms, so it sets the wait. For a bundle holding that rotation's
  two Kustomizations alone, with a 3-minute W, the wait is 41.5 minutes. `land-wait` restates
  that figure as `wait.py:PROMOTE_TIMEOUT_S` and gives up at twice it. The soak's chain counts
  every Kustomization in the bundle, so it grows by a 10-minute queueing round for each five
  more 10-minute ones. Once Graham's bound is the smaller, each added Kustomization no longer
  than the longest adds a fifth of its `timeout`. With the 16 tags published at `0758aa3f5`
  the soak's `timeout` is 4350s, and `verify` waits 75.5 minutes. So `land-wait` also reads the listed `soak` Kustomization's
  `timeout` each poll, and gives up no sooner than twice `verify`'s wait on it
  (`wait.py:soak_deadline_s`). A lander unit's `LAND_DEADLINE` caps the wait below that,
  and `docs/landing.md` says how the units size it (#5427). While a verification of an earlier commit is in flight, M's `decide` waits on
  it, so `land-wait` counts its timeout again from each poll that reads one, for up to
  `IN_FLIGHT_WAIT_S`;
- a revision that changes every importer of `workload-shell.yml.j2` queues in rounds of up
  to each role's `timeout`, so it can reach the cap.

## The bundle

The render job publishes the deploy tags whose `containers_list` entry carries `owner: flux`.
`promote.py published-tags` prints them. A tag marked `owner: released` is left out. That
marking is the first PR of handing a service back (*Handing a service back*).

The build step passes each published tag to the renderer with `--role` and no
`--secret-stubs`. No `--secret-stubs` is what selects placeholder mode, the only mode that
publishes (`docs/homelab-render.md`, *Placeholder mode*). With no published tag the step runs
`homelab_render.py cluster --out out --shared-only` instead, because `cluster` without
`--role` renders every tag. That bundle holds the shared kinds and a root listing them, and no
tag, so the first decryption of the cluster file gets a revision of its own.
`ansible/tests/repo/test_promote_workflow_bundle.py` fails a no-tag branch without
`--shared-only`.

After the census, the step `ciphertext` runs `cluster_vars.py check --ciphertext-only` on
every `*.sops.*` file under `out/cluster/`, and fails when it finds none. `.gitleaks.toml`
skips those copies, so this check stands in for the scan there. The same test fails the
workflow when the step is missing or runs after gitleaks.

The bundle holds:

- `cluster/<tag>/`, each published tag's manifests and their `kustomization.yaml`, with a
  byte copy of each SOPS manifest its role lists (*SOPS manifests* below).
- `flux/<tag>.yaml`, the Kustomization `<tag>` in `flux-system`. It applies
  `./cluster/<tag>` from the `OCIRepository` `homelab` with `prune: true`, `wait: true`,
  `deletionPolicy: Orphan` and the manifests role's `manifests_rollout_timeout_default`.
  A role's `defaults/main.yml` changes two of those fields. `flux_prune: false` renders
  `prune: false`, and `manifests_rollout_timeout` replaces the default `timeout`. Each must
  be a literal, and `scripts/lib/k8s_roles.py:role_fields` refuses a Jinja expression.
  It substitutes exactly when the render of its own directory, `cluster/<tag>/`, holds a
  placeholder, as *Which Kustomizations substitute* below says.
- `flux/cluster-vars.yaml` and `cluster/cluster-vars/`, the `cluster-vars` shared kind,
  described below.
- `flux/kustomization.yaml`, which lists every other file in `flux/`. The root Kustomization
  `homelab` applies it.
- `digests.json`, described below.

A published tag that defers files also gets the Kustomization `<tag>-deferred`, the second
document of `flux/<tag>.yaml`. It applies `./cluster/<tag>-deferred` with the tag's fields
and depends on `<tag>`. The publish census admits that second document only for a tag that
defers files (*The publish census*, the per-file rule).

D5's per-tag kinds, `<tag>-snapshot`, `<tag>-prepull` and `<tag>-verify`, follow in the same
file in that order, each applying its own `./cluster/<tag><suffix>`. `<tag>-snapshot` inherits
`<tag>`'s edges, and `<tag>` then depends on `<tag>-snapshot`, so the role applies only after its
snapshot is taken. `<tag>-prepull` does the same beside it, never after it (*The pre-pull Job*
below). `<tag>-verify` depends on the tag's last Kustomization: `<tag>-deferred` when the
tag has one, `<tag>` otherwise. A `K8S_APPLIES_LAST` tag waits on no other tag's
`-snapshot` or `-verify`. A published tag whose role declares `k8s_autodeploy_snapshot_pvcs`
renders `<tag>-snapshot` (*The snapshot Job* below). A published tag whose role ships
`templates/verify/*.yaml.j2` renders `<tag>-verify`; sonarr and radarr were the first, in D5.20.
`<tag>-verify` keeps the bundle's default `timeout` unless a
Job in it sets an `activeDeadlineSeconds` within 60 seconds of it, and then takes that deadline
plus 60 seconds, so a hung Job fails on its own deadline (`ordering.verify_timeout`).

### Which Kustomizations substitute

The renderer decides substitution per Kustomization directory, not per tag. A directory
substitutes when its placeholder key set is not empty: the secret keys whose R2 sentinel
survives the first placeholder pass in that directory's render. `placeholders.py` computes the
set in process, and every consumer reads that one set (contract C1, *Substitution and
escaping*).

- **A substituting directory's Kustomization gets two fields.**
  `postBuild.substituteFrom` names the Secret `homelab-cluster-vars`, and `dependsOn` names
  `cluster-vars`. The edge makes Flux wait until `cluster-vars` is Ready,
  so the Secret exists before the build substitutes from it. The renderer also writes a
  literal `$` there as `$$` when `$`, `{` or a secret reference follows it, and
  substitution turns it back into `$`.
- **Any other directory gets neither field.** Flux never substitutes it, so a literal `${`
  reaches the cluster as written, with no escape. A route alone does not make a directory substitute,
  because `domain` is published and renders as a literal.
- **`cluster-vars` never substitutes.** Its directory holds no rendered template.
- **`<tag>` and `<tag>-deferred` decide separately.** A placeholder in `cluster/<tag>/`
  alone gives `<tag>` both fields and `<tag>-deferred` neither, and the reverse split the
  reverse. The footprint rule still counts per tag, so a cluster-file edit moves the
  footprint of a tag whose either directory substitutes.
- **The projection is `spec.postBuild`.** A reader that sees only the written bundle reads
  that field: the publish gate *source* below, and `promote.py verify`'s timeout.

`scripts/renderer/tests/test_renderer_bundle.py` holds the predicate: a fixture with a
placeholder gets both fields, one without gets neither and keeps `${HOME}` as written.

### Ordering: `dependsOn`

The renderer generates every `dependsOn` from the play's own dependency map, and no template
writes one. The `no-template-hand-writes-depends-on` row of
`ansible/tests/repo/test_census_rows_text.py` fails a template that does.
`scripts/renderer/renderer_lib/cluster.py:bundle_ordering` builds the map with `build_k8s_dep_map` over the full k8s
`containers_list` and reads ownership from the inventory, so a `--role` render gives a tag
the same toposort edges as a full one. The `<tag>-deferred` edges come from the tags the
render publishes, and the publish job renders every published tag. The edges are:

- **A tag's toposort edges, kept when flux-owned.** `<tag>` depends on each member of its
  `build_k8s_dep_map` edges that the inventory marks `owner: flux`. Any other edge is
  dropped, because a `dependsOn` naming a missing Kustomization fails every reconcile.
- **The dropped edges stay visible.** The annotation `homelab/ansible-owned-deps` lists them,
  comma-separated and sorted. Each becomes a real `dependsOn` in the first render after its
  role moves to Flux. bento-pdf carries `authelia,traefik`.
- **A `K8S_APPLIES_LAST` tag waits on the deferred applies.** It also depends on every other
  tag's `<tag>-deferred`, never its own, which depends on it.
- **`<tag>-deferred` depends on `<tag>`.** It also depends on the role that creates a
  namespace one of its documents names, by the same flux-owned rule.
  `NAMESPACE_ROLE_VARS` in the same file maps `k8s_observability_namespace` to
  `observability`, so `headlamp-deferred` and `netpol-baseline-deferred` wait on
  `observability` once it is flux-owned and annotate it until then.
- **`cluster-vars`**, for a substituting directory, as above.
- **An edge to a suspended tag is dropped.** An edge to a Kustomization of a tag whose role
  sets `flux_suspend: true` is left out, and the annotation `homelab/suspended-deps` lists it. netpol-baseline's edges to every `<tag>-deferred` drop
  the same way, and so do the edges among the suspended tag's own Kustomizations. The PR that
  removes the key restores every edge in the same render. The suspended set is read over
  every flux-owned role, so a `--role` render drops the same edges.

`dependsOn` lists the names in sorted order. A cycle among the edges refuses the render, since
Flux would hold every Kustomization on it at `DependencyNotReady`. An edge governs from the
revision after the one that adds it, so a PR that adds an edge does not also change the
dependent's render. The plan is `docs/target-architecture/deploy/d4-ordering.md`, and
`scripts/renderer/renderer_lib/ordering.py` derives the edges, and
`scripts/renderer/tests/test_renderer_bundle_ordering.py` holds the rules.

### Restart triggers: `checksum/restart`

Ansible's shared `rollout restart` does not run for a flux-owned tag. The renderer therefore
stamps the annotation `checksum/restart` on the pod template of each restart target of a
flux-owned tag, and Kubernetes rolls the pod when the value moves.
`scripts/renderer/renderer_lib/restart.py` holds the rule.

- **The targets** are the workloads the shared `k8s/manifests` role restarts on a render
  change, read from the role's include of `k8s/manifests`. They are the `manifests_rollout`
  workload when it is non-empty, and each `manifests_extra_rollouts` entry whose
  `restart_on` names `config` or `secret`. An entry with `restart_on: []`, such as Scrutiny's
  InfluxDB, is one Ansible waits on and never restarts, so it gets no annotation.
- **Every `manifests_self_rollouts` entry is a target too** (D5.1e). pihole and observability
  restart those workloads in their own tasks, and a flux-owned tag runs no task. Such an
  entry admits both ConfigMaps and Secrets, whatever its `restart_on` names, because there
  `restart_on` shapes only the Ansible release record. pihole's instance 2 gets its
  annotation in `pihole-deferred`, which waits on `pihole`, so a shared ConfigMap edit rolls
  instance 2 only after instance 1 is Ready.
- **`restart_on` also narrows the inputs.** `config` admits the workload's ConfigMaps into its
  value and `secret` its Secrets, as the same list narrows Ansible's restart.
- **The value is per workload.** It is one SHA-256 over the admitted ConfigMaps and Secrets
  that the workload's own pod template names through `envFrom`, `env[].valueFrom` or
  `volumes`, each re-serialised in canonical form. It also covers the `.source` digest of
  every secret key those objects reference, a ConfigMap's included: traefik's static
  ConfigMap holds `${email}`, whose text does not move when the key rotates. A YAML comment
  edit moves nothing, and a rotated key moves the value of each workload that reads it, in
  the revision that carries its new ciphertext.
- **Secrets are hashed in placeholder form,** so the annotation exposes no plaintext. A
  Secret shipped as a SOPS manifest contributes its key's `.source` digest, never its
  ciphertext.
- **A stub render stamps the same value.** It hashes the placeholder-form documents too,
  never its own stub output.
- **A target that names no ConfigMap or Secret gets no annotation.** bento-pdf is one, so its
  render does not change.
- **The render refuses** a target that names no rendered Deployment, DaemonSet or
  StatefulSet, and a Secret key that `.source` does not record. Either would leave a workload
  that a rotation never rolls.

The stamp inserts one block of lines at the place the YAML parser marks, and keeps every other
byte of the file. A re-serialised document would drop the quotes around a `"${key}"` and change
the type Flux substitutes into it.

Two tests keep the annotation honest:

- **The outside-role reference test**, in `scripts/renderer/tests/test_renderer_restart.py`,
  fails when a flux-owned pod template names a Secret or ConfigMap that its own role does not
  render. The annotation sees only the role's own render, so such a reference would roll
  nothing. peanut is its named member.
- **The self-rollouts census**, in
  `scripts/renderer/tests/test_renderer_self_rollout_restart.py`, renders every role that
  passes `manifests_self_rollouts` as flux-owned. It fails on an entry whose pod template
  names a ConfigMap or Secret and carries no `checksum/restart`. pihole and observability
  are its named members, and pihole-2 with its annotation removed is its red fixture. flux's
  controllers read neither kind, so they pass unstamped.

### SOPS manifests

R2's multi-line keys, `SOPS_MANIFEST_KEYS`, reach the cluster as SOPS-encrypted Secret
manifests rather than as placeholders. `cluster_vars.py write` (operator-only) keeps one per
`render_sops_manifests` entry under `ansible/vars/cluster-manifests/`, named
`<tag>.<entry stem>.sops.yaml`.

- **The copy is byte for byte.** For a published tag whose role lists
  `render_sops_manifests`, the renderer copies each manifest into `cluster/<tag>/` under its
  own name and lists it in that directory's `kustomization.yaml`. SOPS's MAC covers the
  plaintext fields too, so nothing may edit the file after encryption.
- **The tag's Kustomization gets `decryption`**, provider `sops` with the Secret `sops-age`,
  exactly when its role lists `render_sops_manifests`. `kustomize-controller` decrypts
  nothing without it (contract C3, *Per-role fields*).
- **Each manifest carries `kustomize.toolkit.fluxcd.io/substitute: disabled`** in its
  plaintext metadata, written before encryption. `kustomize-controller` decrypts before it
  substitutes, so without the annotation a `${` inside a decrypted value would be read as a
  reference. The render refuses a manifest without it and writes nothing. A missing manifest
  is refused too.
- **The publish job's `ciphertext` step covers the copies**, because each ends in
  `.sops.yaml` under `out/cluster/`.

`scripts/renderer/tests/test_renderer_bundle.py` holds the pair: pi-peer-backup with a fixture
manifest gets the copy and `decryption`, bento-pdf beside it gets neither, and a manifest
without the annotation fails the render.

**An all-digit value stays a string on this path.** CI's `sops-manifest-type-proof` job
measured it in a kind cluster running the vendored controller images (PR #5171, run
38825866313). The job applies a manifest that `cluster_vars.py write` built, holding
`0123` and `9007199254740993` (2**53 + 1) in quoted `stringData` fields. Both reached the
live Secret as exactly their digits. The same Secret encrypted from integers, so that SOPS
records `type:int`, failed the server-side dry run:

```text
Secret/proof/proof-red dry-run failed: failed to create typed patch object
  .stringData.long_digits: expected string, got &value.valueUnstructured{Value:9007199254740992}
  .stringData.zero_lead: expected string, got &value.valueUnstructured{Value:123}
```

An int-typed value is therefore refused rather than silently changed. It also passes through a
float64 on the way, which turns `...993` into `...992`. `write` writes every manifest value as
a string, and `cluster_vars.py check` requires `type:str` on each one, so neither failure
reaches the cluster.

The job runs on a PR that touches the flux pin, the SOPS pin, the registry image pin,
`cluster_vars.py`, its fixture `scripts/secrets_mgmt/tests/_sops_type_proof.py` or
`.github/workflows/ci.yml`. If its green
half ever shows a changed value, the remedy is D3's fallback. `write` would emit the value
under `data:`, base64-encoded, and `check`'s structure assertion would compare data-key names
across `data` and `stringData`. `docs/target-architecture/deploy/d3-secrets.md` (D3.3d) gives
the reason.

### Shared kinds and reserved names

A Kustomization that belongs to no deploy tag is a shared kind. `SHARED_KINDS` in
`scripts/renderer/renderer_lib/bundle.py` names every one. D3.3a added the first,
`cluster-vars`. A shared kind publishes `flux/<name>.yaml` and `cluster/<name>/`, and the
census fails a bundle that lacks either. Each member needs a footprint rule in
`FOOTPRINT_RULES` there, which names the tags its files count for, and
`test_every_shared_kind_has_a_footprint_rule` fails a member without one.

`cluster-vars` delivers the Secret `homelab-cluster-vars` that a substituting tag's
`postBuild` names:

- `cluster/cluster-vars/` holds `cluster-vars.sops.env`, a byte copy of the cluster file
  `ansible/vars/cluster-vars.sops.env`, and a `kustomization.yaml` whose `secretGenerator`
  makes `homelab-cluster-vars` in `flux-system` from it, with no name suffix.
- `flux/cluster-vars.yaml` sets the same fields as a tag's Kustomization, no `postBuild`, and
  `decryption` with provider `sops` and the Secret `sops-age`, the cluster's age key.
  Flux decrypts the copy with that key in the cluster. CI holds no key, so the bundle stays
  ciphertext until it reaches the cluster.
- Membership does not depend on any tag. A render that cannot read the cluster file refuses
  and writes nothing, so the census never narrows. No PR-time render reads the file, so
  deleting it fails CI through `cluster_vars.py check` ("does not exist"), which
  `scripts/secrets_mgmt/tests/test_cluster_vars_tree.py` runs in the `pytest` job (D3.2c).
- Its footprint rule counts its files for every substituting tag, so an edit to the cluster
  file moves each such tag's `footprint` and no other tag's.
- `promote.py verify` expects `flux/cluster-vars` like a tag's status whenever the kind's
  footprint moves, its first promotion included.

Every `cluster` render refuses a deploy tag named `homelab`, the root's name, after a
`SHARED_KINDS` member, or after a member of `RESERVED_NAMES`, the literal list beside it. It
also refuses a tag ending in `-deferred`, `-snapshot`, `-prepull`, `-precheck` or `-verify`. `RESERVED_NAMES` holds
`soak` and `homelab-namespace`, D5's shared kinds. `soak` is rendered and in `SHARED_KINDS`
(*The soak Job*). `homelab-namespace` joins only in the PR that renders it, because the census
fails a member with no file. The suffixes keep a tag's Kustomization from colliding with another tag's per-tag kind.
The publish census refuses a published tag that takes any of these names as well.

### `digests.json`

The renderer computes `digests.json` in its own process. It maps each published tag and each
shared kind to two SHA-256 digests:

- `own` covers `cluster/<name>/` alone.
- `footprint` covers every path that counts for the name: the tag's own directories, with
  each per-tag suffix stripped, its `flux/<tag>.yaml`, and the files of each shared kind
  whose rule lists the tag.

A published tag's object also holds two fields (D5.1a2):

- `claims` lists the snapshot claims the tag's role declares in
  `k8s_autodeploy_snapshot_pvcs`, sorted. The publish census's `-snapshot` rule reads the
  same function, `scripts/lib/k8s_roles.py:snapshot_claims`.
- `suspended` is true when the tag's rendered Kustomization sets `spec.suspend: true`.

`flux/kustomization.yaml`, `digests.json` itself and `images/` count for no name. Any other
path that no rule claims lands under `_unclassified`, with the same two digests and a `paths`
list. `land.sh`'s wait reads `footprint` through `scripts/deploy_tools/changed_set.py`, and a
change under `_unclassified` counts as every name changed. D6's *What a footprint counts* in
`docs/target-architecture/deploy/d6-delete-old-path.md` defines the classification.

### The publish census

`promote.py check-bundle out` fails the render job before gitleaks and before the push. It
computes its allowlist from the inventory, `SHARED_KINDS` and the published tags' roles,
never from the render, so a renderer bug cannot widen it. Each per-tag kind has its input:

- `-deferred` when the role passes a non-empty `manifests_deferred_files` to its
  `k8s/manifests` include;
- `-snapshot` when the role's defaults declare a non-empty `k8s_autodeploy_snapshot_pvcs`;
- `-verify` when the role ships `templates/verify/*.yaml.j2` (`verifying_tags`, which reads the
  same `verify_files` the renderer reads), or opts a CronJob into a gate Job with
  `flux_cronjob_gates` (`cronjob_gates`). sonarr, radarr, n8n, prowlarr and media-volume do
  the first; karakeep and pi-peer-backup do the second (#5652). configarr sets the second
  ahead of its flip, which stays inert while it is Ansible-owned.

It fails when:

- `flux/` lacks a file for a published tag or a shared kind, or holds any other file;
- `flux/kustomization.yaml` omits a file in `flux/`, or lists one that is not there;
- `flux/<name>.yaml` breaks the per-file rule. The file must hold exactly the Kustomization
  `<name>` applying `./cluster/<name>`, followed by each per-tag kind its inputs call for, in
  the order `-deferred`, `-snapshot`, `-verify`, each applying `./cluster/<name><suffix>`.
  An extra document, or a kind the inputs do not call for, fails. A shared kind's file holds
  exactly its own Kustomization;
- a published tag or a shared kind has no `cluster/<name>/`, or a per-tag kind its inputs
  call for has no `cluster/<name><suffix>/`;
- a published tag takes a reserved name;
- a published tag's role cannot be read, or its `manifests_deferred_files` or
  `k8s_autodeploy_snapshot_pvcs` is not a literal list;
- `digests.json` is missing or names another set.

### The publish gates

`check-bundle` runs three gates beside the census, in
`scripts/deploy_tools/bundle_gates.py`. Each refusal prints one line that opens with the
gate's name.

- **source**: the gate judges each Kustomization document in `flux/<tag>.yaml` against its
  own `spec.path` directory, so `<tag>-deferred` is judged against
  `cluster/<tag>-deferred/` (#5172). A document with no `postBuild` applies a directory that
  holds no `${K}` whose `K` is a key name of `ansible/vars/secrets.yml`. Flux would not substitute such a reference.
  The gate pairs each `$$` first, as Flux does, so `$${K}` and a shell `${HOME}` pass. A
  document with `postBuild` may name in `substituteFrom` only a Secret that a `secretGenerator` in
  the bundle makes. The `cluster-vars` kind generates `homelab-cluster-vars`, the one Secret
  a tag's `postBuild` names. The gate also fails a published tag or a shared kind whose file
  holds no Kustomization, and a document whose `spec.path` is not a directory under
  `cluster/`.
- **host**: every `Host(...)` in an IngressRoute ends in `.<domain>`, the literal that
  `ansible/inventory/group_vars/all.yml` defines. A `${domain}`, a `stub-domain` and a
  hand-written host each fail. While bento-pdf is published, its `.local.` route is the
  gate's named member, and the gate fails when that route holds no matching Host rule.
- **no-stub**: no file in the bundle holds an R1 stub value, `stub-<key>` for a key name of
  `secrets.yml`. The gate decodes each Secret's `data` before it searches.

### The Job census

A Job Flux applies runs again whenever its object comes back: after a TTL deletes it, and
after a reset or a revert restores an older bundle. A changed spec under the same name fails
the apply, because a Job's pod template is immutable. The census in
`scripts/renderer/tests/_job_census.py` holds every Job in the bundle to these rules, and
`test_renderer_job_census.py` runs it over the live bundle in CI. D5's plan has the reasons,
under *Rules for every Job Flux applies (D5.1a3)*.

- **Kind.** Each Job's `homelab/job-kind` annotation names its kind, and its directory must
  admit that kind. `<tag>-snapshot/` admits `snapshot`, `soak/` admits `soak`, and
  `<tag>-verify/` admits `verify` and `mutation`. No other directory holds a Job.
- **No TTL.** No Job sets `ttlSecondsAfterFinished`. Prune deletes a Job once its name leaves
  the bundle.
- **Name.** Each name ends in `-<digest8>`, the first 8 hex digits of the SHA-256 of the
  Job's `spec` as sorted-key JSON (`job_spec_digest8` in
  `scripts/renderer/renderer_lib/digest8.py`). The spec holds no `${...}` placeholder,
  because Flux substitutes it after the digest is taken. A substituted value reaches a Job
  through a Secret.
- **Script delivery.** Every container runs `python3 <mount>/<script>.py`, where `<mount>`
  mounts a ConfigMap whole and `<script>.py` is one of its keys. No `-c`, no `-m` and no
  shell. The one exception is the pre-pull Job's fixed no-op (*The pre-pull Job*). Each ConfigMap a Job mounts ends its name in `-<digest8>` of its content
  (`configmap_digest8`). The Job's spec names the ConfigMap, so a script fix renames the Job.
- **`backoffLimit`.** Every Job sets one: 1 for a snapshot Job, 0 for a soak or verify Job,
  and a mutation Job's own value, with its reason beside it.
- **Rerun safety.** A mutation Job carries `homelab/rerun-safe`, naming as `<path>::<test>`
  the test that runs it twice and finds the second run changes nothing.
- **Connections.** A Python script in a ConfigMap the Job mounts opens every connection
  through `open_url` in `ansible/roles/k8s/manifests/files/flux_job_connect.py`, shipped
  byte-identical beside it. A script imports only the modules in the census's
  `ALLOWED_IMPORTS`, none of which can connect, and builds its request with the helper's
  `Request`. It reads from the helper only the names in its `__all__`, reaches `urllib` only
  through `urllib.error` or `urllib.parse`, and never touches `sys.modules`, so it cannot walk
  through an allowed module to `urllib.request` or `socket`. It uses neither the helper module
  nor `urllib` as a value, and reads no double-underscore name such as `__globals__`. The helper retries a refused or timed-out connect for 60
  seconds, because kube-router can refuse a new pod until it learns its address. It never
  retries an answer, so a flaky check fails rather than passing on its second try.

### The pre-pull Job

A `Recreate` Deployment stops its old pod before the new pod's pull starts, so a large image
from a slow registry is an outage as long as the pull. GHCR serves both nodes at 0.21-0.51
MB/s (measured 2026-10-17), so code-server's 851 MB re-pull after a base bump is about 50
minutes down (#5525).

A role that sets `flux_prepull_timeout` in its `defaults/main.yml` gets
`cluster/<tag>-prepull/` and the Kustomization `<tag>-prepull`
(`scripts/renderer/renderer_lib/prepull.py`). The directory holds one Job per distinct
placement among the tag's Deployments and StatefulSets. A placement is the pod's
`nodeSelector`, node affinity and `tolerations`, and the Job copies it, so the scheduler puts
the Job where it would put the workload. A workload that names no node by
`kubernetes.io/hostname` fails the render. Each Job runs one container per distinct image those
workloads run, with `imagePullPolicy: IfNotPresent` and a script that exits at once, so the
Job completes when the kubelet holds every image. `<tag>` depends on `<tag>-prepull`, so the
old pod keeps serving through the pull and the rollout pays only the container start.

- **The wait moves, it does not go away.** `<tag>-prepull`'s `timeout` is the role's
  `flux_prepull_timeout`, and it holds a
  `kustomize-controller` worker for the pull. It sits on `verify`'s chained timeout like any
  other Kustomization, so the pull's budget stays on the critical path. What changes is
  that the service keeps serving through it.
- **The Job has no deadline.** A slow pull never fails a Pod, so a deadline is the only way
  the Job could fail. Flux never re-runs a failed Job under the same name, so every later
  revision that kept the image would hold `<tag>` at `DependencyNotReady`. Without one, a
  pull that outlasts the `timeout` costs that revision its verdict, the Job keeps pulling,
  and a later retry applies `<tag>`.
- **A revision that keeps the images keeps the Job.** The Job's name carries a digest of its
  spec, image refs and placement included, so only a content-tag change runs a new pull.
- **The Job and the pod are scheduled separately.** A node cordoned between the two sends the
  pod elsewhere, and there the rollout pays the cold pull. The rollout's own budget stays the
  backstop for that case.
- **Every image must carry `sh`.** Each container runs `sh /opt/prepull/prepull.sh`, a
  mounted script that echoes one line. It is a shell script because n8n's image and the
  `alpine` init container of its runners carry no `python3` (#5546), and a container that
  cannot start fails the Job. The census's script-delivery rule admits the shell form only in
  a pre-pull Job, and only when the script is byte-identical to `SCRIPT` in `prepull.py`.
- **Kubelet image garbage collection can still remove the image** between the pull and the
  rollout if the node passes its disk high-water mark, which the rollout budget also covers.

code-server and n8n set it, and both are flux-owned, so the bundle holds `code-server-prepull`
and `n8n-prepull`.

### The snapshot Job

A flux-owned role that declares `k8s_autodeploy_snapshot_pvcs` gets the Kustomization
`<tag>-snapshot`, which `<tag>` depends on. It holds one Job, `snapshot-<tag>-<spec digest8>`,
in `flux-system`, which runs `ansible/roles/k8s/manifests/files/flux_snapshot.py`
(`scripts/renderer/renderer_lib/snapshot.py` renders it). The Job's spec carries the role's
`digest8`, the first 8 hex digits of the tag's `own` digest in `digests.json`, so every change
to `cluster/<tag>/` renames the Job and Flux runs it again before the change applies.

- **The snapshot name** is `autodeploy-<tag>-<digest8>-<claim>-<token>`, the shape
  `volume-snapshot` writes. `<claim>` drops a leading `<tag>-`, and `<token>` is the UTC time
  the Job's pod starts. `volume-revert` reads these snapshots with
  `volume_revert_sha=<digest8>`. The render fails when the longest name could pass 63 bytes,
  and the Job checks again before it creates anything.
- **The checks.** The Job resolves every claim and reads its snapshot-space cap before it
  creates any snapshot. A cap fails the Job only when the volume's numbers say it is full; an
  empty or `"0"` cap, or a failed read, counts as uncapped. Then, per claim, it creates the
  snapshot, waits up to `volume_snapshot_timeout` for `readyToUse`, checks the listing holds
  the snapshot it created, and prunes the claim's `autodeploy-<tag>-` snapshots to
  `volume_snapshot_retain`, floor 2. A `markRemoved` CR never counts toward the window.
- **An existing CR of the same name fails the Job.** The Job never adopts it, because it is
  another run's recovery point or a `markRemoved` one. The one retry (`backoffLimit: 1`)
  starts at least a second later, so it takes another token.
- **A new claim is skipped.** A PVC absent live and absent from `<tag>`'s `status.inventory`
  is new in this revision: `<tag>` has never applied it, so it holds no data. The Job logs
  the skip with the claim's name and passes, and `<tag>` then creates the claim. On a tag's
  first apply `<tag>` has no inventory, so every absent claim is new. A PVC the inventory
  lists and the cluster lacks fails the Job, naming the PVC, because a claim that held data
  is gone.
- **A failed Job stays failed.** Flux never re-runs a Job under the same name, so after both
  tries fail, `<tag>-snapshot` stays not Ready and `<tag>` waits. Two things clear it: the
  role's next revision, which renames the Job, or deleting the failed Job once its cause is
  fixed, after which Flux creates it again (operator-only, because the agent's `kubectl` is
  read-only).
- **RBAC.** The ServiceAccount `snapshot-<tag>` may create, list and delete Snapshots and read
  Volumes in `longhorn-system`, and read the `<tag>` Kustomization. A ClusterRole lets it read
  the claims' PVCs by name and every PV. The PVC grant is cluster-wide because a Role in a
  namespace `<tag>` creates would fail the snapshot's apply on the tag's first revision.

### The soak Job

Every bundle carries the shared kind `soak` (`scripts/renderer/renderer_lib/soak.py`). Its
Kustomization holds one Job, `soak-<spec digest8>`, in `flux-system`, which runs
`ansible/roles/k8s/manifests/files/flux_soak.py` against R, the commit the bundle was rendered
from. The Job's exit is the `flux/soak` status. The design is D5's *The soak as a Job*.

- **No `dependsOn`.** The Job waits at run time until every Kustomization R changed reads
  Ready with `lastAppliedOriginRevision` at R. A suspended tag is outside its scope, and the
  Kustomization's `homelab/suspended-deps` annotation names the suspended tag's
  Kustomizations. Each poll of that wait re-reads `homelab`, and the Job exits 1 once it
  no longer holds R, as after a reset (#5704). `kustomize-controller` runs one worker more than the rollout width, because
  the soak holds one from the start of the rollout.
- **R's SHA moves the Job, not the footprint.** The Job's arguments carry R's SHA, so its name
  changes on every commit. `digests.json` records `soak` over a render at an all-zero SHA, so a
  docs-only commit still has an empty changed set. `homelab_render.py cluster --revision`
  names R, and defaults to the checkout's `HEAD`, which the publish job pins.
- **W.** The ConfigMap's `services.json` maps each published tag to the longest `for:` among
  the critical or `verify="true"` rules under `ansible/roles/k8s/observability/files/rules/`
  that can fire for it, and to the namespaces its tag renders into. A rule with no static
  `service` label counts for every tag. The fleet W is the map's largest entry plus the
  scrape and evaluation allowance.
- **Its timeout.** The Job starts beside the tags, so its wait for them to read Ready at R,
  `--wait-seconds`, is `chained_timeout.promote_timeout` over the bundle's other
  Kustomizations with every one counted as changed. The render cannot know which ones R
  changes. `activeDeadlineSeconds` adds the fleet W and a minute, and the Kustomization's
  `timeout` adds another minute. With the eleven tags published on 2026-10-17, that is 3210s,
  3450s and 3510s.
- **RBAC.** The ServiceAccount `soak` may get and list `OCIRepository` and Kustomization objects in
  `flux-system`. A ClusterRole lets it list pods and get and list ReplicaSets in every
  namespace. The grant is cluster-wide because `soak` has no `dependsOn` edges, so Flux may
  apply it before a tag whose first revision creates a namespace. A Role in that namespace
  would fail `soak`'s apply, and `flux/soak` would read `failure` (#5380).
- **Its base.** The flux role renders the `OCIRepository` named `last-good`, which follows the
  `last-good` tag with `homelab`'s `verify` block. The Job diffs R's `digests.json` against
  it, and widens to the fleet when it cannot.
- **It gates promotion.** A `failure` holds, and for a revision with no snapshot role resets
  `prod` (*Verification*). To read why a soak failed, read the failed Job's termination
  message, its `failed-services:` line. A pass prints the revision, the services it judged
  and the window, then the cluster's `event=deploy` annotation line, which Grafana reads
  through `observability_flux_deploy_annotation_expr`.

## Removing an object on purpose

A render that drops a NetworkPolicy, an IngressRoute, a CronJob, a Service or an RBAC object
gets it pruned, and its Kustomization stays Ready, so `verify` passes. `promote.py decide`
therefore compares the objects in a candidate's `cluster/` with those in `prod`'s bundle. A
candidate that loses an object is eligible only when a commit in (`prod`, candidate] declares
it with a line of this form:

```text
Removes-Object: <kind>/<namespace>/<name>
```

- `<kind>` is the object's `kind`, such as `IngressRoute`.
- A cluster-scoped object leaves the namespace empty: `Removes-Object: ClusterRole//<name>`.
- The key is case-insensitive, and the line may sit anywhere in the commit message.
- Put the line on any commit of the PR. When one does, `land.sh` sends the squash commit's
  body itself: every commit's message, then each line again as a trailer. The line therefore
  reaches master whatever the repository's squash default says. A merge outside `land.sh`
  that replaces the body, such as `gh pr merge --body`, drops it.
- A commit outside the range does not count, including `prod`'s own commit.
- A rename is not a removal. An object whose name, with the trailing `-<digest8>`
  removed, still renders in the candidate passes, as the soak Job and its ConfigMap do on
  every revision. `scripts/deploy_tools/object_loss.py` strips the suffix as
  `cross_plane_order.py` does at PR time.

`decide` skips an ineligible candidate with one stderr line per candidate, naming each
undeclared object, and weighs older ones. When every candidate loses an object, it prints
`current`, and D2.6d's `PromotionStalled` alert pages after 2 hours. A later commit that carries the line
releases the walk.

An object whose whole Kustomization leaves the bundle is exempt. That happens when a service
is handed back and its entry reads `owner: released` (*Handing a service back*).
`deletionPolicy: Orphan` prunes none of its objects. The exemption holds only while that
policy does, and D6.7's switch away from Orphan must remove it.

The promote job checks out master with `fetch-depth: 0`, because `decide` reads these lines
with `git log` back to `prod`'s commit.

### The same check on the pull request

CI's cross-plane order gate, `scripts/validate/cross_plane_order.py`, refuses the PR before it
merges, so a removal never reaches `decide` undeclared. Its arm 3 renders the cluster at the
PR's base and head. It fails when a tag that is `owner: flux` at both ends loses an object of
any kind, and no commit of the PR carries the `Removes-Object:` line for it. The failure line
names the exact line to add. Two removals pass without a line:

- **A rename by digest.** The renderer ends a Job's name in its spec digest and a script
  ConfigMap's name in its content digest, `-<8 hex digits>`. An object whose kind, namespace
  and name, with that suffix removed, still render in the head is renamed, not removed.
- **An object Flux never prunes.** An object labelled or annotated
  `kustomize.toolkit.fluxcd.io/prune: disabled` passes. D5.1a2 stamps that label on claims;
  a claim rendered without it is pruned like any other object and needs the line.

A tag handed back passes too. A tag that is not `owner: flux` at the head, because it reads
`owner: released` or lost its entry, is not judged, so the revert of a flip needs no lines.
The arm reads the PR's own commits, which a squash merge need not copy, so it runs on pull
requests only. On master `decide` reads the same lines and applies both exemptions. The two
checks share one implementation of them, `stem` and `prune_disabled` in
`scripts/deploy_tools/object_loss.py`, so they cannot judge the same drop differently (#5291).

## Retiring a flux-owned service

`ansible/tasks/k8s_retire.yml` deletes the last render Ansible staged for a service. For a
flux-owned service that render predates the flip, and Flux at the old `prod` keeps applying
the tag until promotion, so it re-creates what the teardown deleted. Arm 4 of the
cross-plane order gate therefore refuses a PR that adds a name to `k8s_retired_services`
whose entry reads `owner: flux` on the PR's base. A flux-owned service retires in this order
instead:

1. **The first PR empties the service's render.** Flux prunes every object the render drops.
   The PR carries a `Removes-Object:` line for each one (*Removing an object on purpose*).
   Deleting the role's templates is not enough on its own. The PR also edits the role's
   `k8s/manifests` include, and leaves the `containers_list` entry as it is:

   ```yaml
     vars:
       manifests_rollout: ''
       manifests_exclude_files: [service.yaml, ingressroute.yaml]
   ```

   The renderer refuses a restart target that names no workload in the render, and
   `manifests_rollout` defaults to the tag. The empty value clears that target. Any
   `manifests_extra_rollouts` or `manifests_self_rollouts` entry goes too, and so do the
   role's `k8s_claims`. The exclude list drops the Service and IngressRoute that the shared
   defaults render from the entry's `port` and `hostname`. Editing the entry instead would
   move the render of every Ansible-owned role that reads `containers_list`, such as
   authelia, and arm 1 would flag the PR. The tag's `cluster/<tag>/` directory then holds
   only a `kustomization.yaml` with `resources: []`.
   `scripts/validate/tests/test_validate_cross_plane_retirement.py::test_step_1_of_a_flux_owned_retirement_renders_no_object`
   renders this step on bento-pdf.
1. **Wait for the prune.** `promote` must move `prod` to a commit that holds the first PR,
   and the objects must read `NotFound`.
1. **The second PR removes the `containers_list` entry and the role.** The next bundle omits
   `flux/<tag>.yaml`, and `deletionPolicy: Orphan` leaves nothing behind, because the first
   PR's prune already removed the objects. The name never joins `k8s_retired_services`.
1. **Delete the claim by hand (operator-only).** Prune never deletes a claim labelled
   `kustomize.toolkit.fluxcd.io/prune: disabled`, so the claim and its Longhorn volume
   outlive both PRs. Once the second PR is on master, and only when the data is no longer
   wanted, the operator deletes the claim with `kubectl delete pvc -n <namespace> <claim>`.
   The agent's ServiceAccount cannot run that write.

## Retention of old artifact versions

The `prune-artifacts` workflow, `.github/workflows/prune-artifacts.yml`, runs
`promote.py prune` once a day. It deletes a version of the artifact only when the version is
older than 14 days and is one of these:

- a version whose only tag is `sha-<commit>`, where the commit is a strict ancestor of
  `prod`'s commit, so no promotion can choose it again;
- an untagged version whose manifest has no `subject`, such as a `verify-drill` bundle that a
  later drill replaced;
- a signature of a digest that this run deletes or that the listing no longer holds.

Every other version stays, however old. That covers the versions tagged `prod`, `last-good`
and `verify-drill`, and every `sha-<commit>` version whose commit descends from `prod`'s.

The signatures are why the rule reads manifests rather than ages. cosign v3 stores a
signature as a bundle, which is an untagged package version whose manifest `subject` names
the signed digest. On a registry without the OCI referrers API, cosign also
writes an index tagged `sha256-<hex>`, named after the signed digest. A prune that deleted
every untagged version past 14 days would strip the signature from `prod`. Every later
verification of that digest would then fail, and the deletion cannot be undone. So `prune`
deletes a bundle or an index only when the digest it signs is gone.

`prune` deletes nothing when `prod` does not exist, or when any listing, manifest or ancestry
read fails. A listing that does not hold `prod`'s or `last-good`'s version counts as a failed
read. After its deletes, `prune` runs `cosign verify` on `prod`'s digest, and on
`last-good`'s when that tag exists. A failed verify fails the run, so a wrong selection
shows as a red run on the day it happens. `prune` waits one second before each delete,
because GitHub rate-limits bursts of writes with a 403 that reads like a permission error.
The run's last line counts the versions it listed, the untagged ones among them, the untagged
versions whose manifest has a `subject`, and the versions it selected, deleted and verified.

The first run shows whether the workflow's `GITHUB_TOKEN` may list and delete versions of a
package that the user account owns. If the run logs an HTTP 403, the operator grants this
repository the Admin role in the package's Actions access settings (**operator-only**).
Until then the prune deletes nothing, which costs storage only.

## Tokens

No agent token ever carries the `write:packages` scope. Pushing an artifact, signing it and
moving its `prod` tag belong only to the code-free `publish` and `tag` jobs, under each job's
own `GITHUB_TOKEN`. The `prune` job's `GITHUB_TOKEN` also holds `packages: write`, for its
deletes. That job runs only master's copy of `promote.py`, because it refuses any ref but
`refs/heads/master`.

The repository secret `PROMOTE_TOKEN` is a fine-grained token with Variables read and write
on two repositories, `DanielH2018/server` and `DanielH2018/dotfiles`, and a 90-day expiry.
`promote` reads `DEPLOY_HOLD` with it, and `tag` writes `PROD_DIGEST` with it. It never
starts a run: `settle` dispatches the follow-up `promote` run with its own `GITHUB_TOKEN`
under `actions: write`, because a `workflow_dispatch` is the one event that token may start a
run with. The flux role's `CLAUDE.md` has its rotation steps, and the
rotation registry's `external-90d` tier is the one its SOPS copy belongs on.

The repository secret `DEPLOY_DISCORD_WEBHOOK` holds the value of the SOPS key
`gitops_deploy_discord_webhook`, the deployer's Discord channel. Only the `post` job reads it,
for the stateful hold's post, and that job holds `contents: read` alone. `promote.py` never
prints the URL, and a failed post names only the HTTP status. `docs/secret-rotation.md` has
the rotation step for the copy.

The SOPS key `flux_github_status_token` is a fine-grained token with Commit statuses read and
write on two repositories, `DanielH2018/server` and `DanielH2018/dotfiles`, which
notification-controller posts statuses with. It has no expiration date. It lives only in
SOPS, on the registry's `external` tier, and the flux role's `CLAUDE.md` has its rotation
steps.
