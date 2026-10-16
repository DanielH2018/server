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
  `revision`. The controllers do not export this series themselves. A Kustomization's
  `revision` reads `status.lastAppliedOriginRevision`, so it carries `master@sha1:<sha>`, the
  field [contracts.md C6](target-architecture/contracts.md) names. An `OCIRepository`'s `revision` reads
  `status.artifact.revision`, the digest form. The config is a defaults variable,
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
state. monitor-bridge reads that state each 300-second cycle and serves it as four gauges, one
series each, which the `deploy-pipeline` scrape job collects:

- `homelab_deploy_hold_active` reads 1 while `DEPLOY_HOLD` reads anything but `none`.
- `homelab_deploy_hold_age_seconds` is the time since `DEPLOY_HOLD` was last written while a
  hold stands, and 0 otherwise.
- `homelab_deploy_unpromoted_age_seconds` is the time since the oldest green master commit that
  `prod`'s commit lacks went green, and 0 when there is none.
- `homelab_deploy_prod_digest_match` reads 1 when `prod`'s digest in GHCR equals
  `PROD_DIGEST`, and also while `PROD_DIGEST` reads `none` and no `prod` tag exists.

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
  `DeployHoldActive` already pages that state.
- **`ProdMovedOutsidePromote`, critical.** `prod`'s digest has differed from `PROD_DIGEST` for
  15 minutes. `promote` writes `PROD_DIGEST` each time it moves `prod`, so either something
  else moved the tag, or `promote` moved it and the write failed. Flux follows `prod`, so
  production runs whatever the tag names now. The 15 minutes cover the gap between a move and
  its write, plus one exporter cycle.
- **`DeployPipelineExporterAbsent`, warning.** For 15 minutes, either the scrape job is gone
  from `prometheus.yml`, or monitor-bridge is scraped without one of the four gauges. A read
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
  The read-only ServiceAccount cannot read ClusterRoles or ClusterRoleBindings, so the census
  names each one as not checked.
- **An empty census is not a clean one.** A tag with no rendered object live exits 75.

The census cannot see an apply that fails on an immutable field, such as a PVC's spec or a
selector, or one that changes a value. For those the operator renders the flip PR's head, where
the entry carries `owner: flux`, and runs the flux CLI at `flux_version` on a machine with
cluster-admin access (**operator-only**):

```bash
uv run python scripts/renderer/homelab_render.py cluster --out out --role <tag>
flux diff kustomization <tag> --path out/cluster/<tag> \
    --kustomization-file out/flux/<tag>.yaml
```

Without `--dry-run`, the CLI substitutes from the Secrets the Kustomization's `substituteFrom`
names, so the diff compares real values rather than `${…}` placeholders. It reads
`homelab-cluster-vars`, which is why it is operator-only. A role PR quotes object names and
field paths from both commands, never values or diff hunks.

H1's flips use a different census, `scripts/dev/takeover_census.py` (H1.1j), which is also
operator-only. It compares the live object with a server-side dry-run apply as
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
return of a service to Ansible, and every retirement, follows the same order.

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
  Kustomization whose `dependsOn` chain reaches one, such as a tag's `-deferred`. A suspended
  Kustomization is left out. A change under `_unclassified`, or a bundle with no
  `digests.json`, expects every name, and an empty set expects `flux/homelab` alone.
- **What it never expects.** A Kustomization outside the set, so a failure that T did not
  cause never holds; the Flux alerts page it instead. A dependent whose status on `prod`'s
  commit already read `failure` is reported in the log and not expected.
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

### The timeout follows the chain

`scripts/deploy_tools/chained_timeout.py:promote_timeout` derives `verify`'s wait from T's bundle and its changed set, the same set
`verify` expects, so the two cannot disagree. With `dependsOn` edges, a Kustomization starts
only after its dependencies finish, so the wait sums four terms:

- **The longest path.** The longest `dependsOn` path in T's bundle that ends at a changed
  name. A changed name on the path counts its own `timeout`. An unchanged ancestor counts no
  `timeout`: Flux still waits for it to reconcile T, but its apply changes nothing, and a
  healthy one passes its health check at once. Each edge on the path counts one
  `--requeue-dependency` interval, 30 seconds by default. A Kustomization whose dependency is
  not Ready requeues after that interval and no sooner. The walk covers every Kustomization in
  the bundle, so `cluster-vars` counts whenever it sits on the path.
- **The queueing term.** The number of changed names, dependents included, divided by
  `--concurrent` and rounded up, times the largest `timeout` among them. A Kustomization that
  waits on health holds one of the Kustomize controller's workers, so a revision that changes more
  names than `--concurrent` queues behind the workers. The flux role sets `--concurrent` to
  `k8s_rollout_batch_width`, 5, the width the deploy play rolls workloads at. Without the term
  `verify` would time out on such a revision and write a false hold.
- **The `OCIRepository` interval**, 1 minute.
- **The largest `retryInterval`**, 2 minutes on every Kustomization the renderer writes.

The root `homelab`'s 5-minute `timeout` is the floor of the first two terms. The flux role
leaves `--requeue-dependency` at its default, and
`scripts/deploy_tools/tests/test_chained_timeout.py` fails a vendored install or role default
that sets it. `ansible/tests/k8s/test_flux_role.py` holds `chained_timeout.CONCURRENT` equal to
the `--concurrent` the role renders.
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
  33.5 minutes. `land-wait` restates that figure as `wait.py:PROMOTE_TIMEOUT_S` and gives up
  at twice it. While a verification of an earlier commit is in flight, M's `decide` waits on
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

D5's per-tag kinds, `<tag>-snapshot` and `<tag>-verify`, follow in the same file in that
order, each applying its own `./cluster/<tag><suffix>`. `<tag>-snapshot` inherits `<tag>`'s
edges, and `<tag>` then depends on `<tag>-snapshot`, so the role applies only after its
snapshot is taken. `<tag>-verify` depends on the tag's last Kustomization: `<tag>-deferred` when the
tag has one, `<tag>` otherwise. A `K8S_APPLIES_LAST` tag waits on no other tag's
`-snapshot` or `-verify`. No role renders either kind yet.

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
also refuses a tag ending in `-deferred`, `-snapshot` or `-verify`. `RESERVED_NAMES` holds
`soak` and `homelab-namespace`, D5's shared kinds that no PR renders yet. Each joins
`SHARED_KINDS` only in the PR that renders it, because the census fails a member with no
file. The suffixes keep a tag's Kustomization from colliding with another tag's per-tag kind.
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
- `-verify` for no tag yet, because no role input names a verify Job. The first PR that
  renders one adds its reader.

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
  `scripts/renderer/renderer_lib/bundle.py`). The spec holds no `${...}` placeholder,
  because Flux substitutes it after the digest is taken. A substituted value reaches a Job
  through a Secret.
- **Script delivery.** Every container runs `python3 <mount>/<script>.py`, where `<mount>`
  mounts a ConfigMap whole and `<script>.py` is one of its keys. No `-c`, no `-m` and no
  shell. Each ConfigMap a Job mounts ends its name in `-<digest8>` of its content
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

The SOPS key `flux_github_status_token` is a fine-grained token with Commit statuses read and
write on two repositories, `DanielH2018/server` and `DanielH2018/dotfiles`, which
notification-controller posts statuses with. It has no expiration date. It lives only in
SOPS, on the registry's `external` tier, and the flux role's `CLAUDE.md` has its rotation
steps.
