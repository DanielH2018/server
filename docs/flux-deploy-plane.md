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

notification-controller's Discord alerts stay as a second channel beside these.
`ansible/roles/k8s/observability/tests/rules/flux.test.yaml` fires each rule and holds a
case one evaluation short of each hold. A lost `gotk_resource_info` leaves the last two rules
quiet ([#4809](https://github.com/DanielH2018/server/issues/4809)).

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
move changes nothing in the cluster but the commit Flux reports. The workflow has four jobs:

1. `render` checks out the triggering commit and refuses one that is not on master. It builds
   the bundle (*The bundle*), runs `promote.py check-bundle` over it, and scans it with
   gitleaks. It holds read access only, and a refusal at any step pushes nothing.
1. `publish` checks out no repository code. It pushes the bundle as `sha-<commit>` and signs
   it with cosign, under a certificate that names the workflow's own identity.
1. `promote` runs `promote.py decide`, read-only. `decide` walks master from its tip back to
   `prod`'s commit and takes the first commit that is published, signed and green in CI. It
   prints one line, `trigger=<sha> verdict=<verdict> target=<sha>`, where the verdict is
   `promote`, `held`, `current` or `error`.
1. `tag` checks out no repository code. On the verdict `promote` it moves `prod` to
   `sha-<target>` and writes that digest to the repository variable `PROD_DIGEST`.

The repository variable `DEPLOY_HOLD` stops promotion. The value `none` means no hold, and any
other value holds, so a mistyped hold still holds. An empty or missing variable makes `decide`
print `error`, and no tag moves. Setting or clearing a hold is **operator-only**:

```bash
gh variable set DEPLOY_HOLD --body none -R DanielH2018/server
```

An agent session cannot run that command, nor write `PROD_DIGEST` or move a tag: the
`block-footguns` hook denies them (`.claude/hooks/_deploy_plane_rule.py`). Reading either
variable with `gh variable get` stays allowed.

A dispatch of the workflow from master with the input `verify_drill` pushes the bundle
unsigned to the tag `verify-drill`, for the flux role's signature drill. It skips `promote`
and `tag`, so it never moves `prod`. The drill push carries the annotation
`io.homelab.verify-drill=true`, so its digest differs from the signed `sha-<commit>` digest of
the same commit. Without it, the drill tag pointed at a signed digest and verified. The
dispatch then runs `cosign verify` on the drill digest under `promote`'s identity, and the
run fails if the verification succeeds.

## The bundle

The render job publishes the deploy tags whose `containers_list` entry carries `owner: flux`.
`promote.py published-tags` prints them. A tag marked `owner: released` is left out. That
marking is the first PR of handing a service back (*Handing a service back*).

The build step passes each published tag to the renderer with `--role` and no
`--secret-stubs`. No `--secret-stubs` is what selects placeholder mode, the only mode that
publishes (`docs/homelab-render.md`, *Placeholder mode*). With no published tag the renderer
never runs, because `cluster` without `--role` renders every tag. The step then writes D1's
empty root and an empty bundle's `digests.json` itself.
`ansible/tests/repo/test_promote_workflow_bundle.py` fails if either literal drifts from what
the renderer writes for no tag.

The bundle holds:

- `cluster/<tag>/`, each published tag's manifests and their `kustomization.yaml`.
- `flux/<tag>.yaml`, the Kustomization `<tag>` in `flux-system`. It applies
  `./cluster/<tag>` from the `OCIRepository` `homelab` with `prune: true`, `wait: true`,
  `deletionPolicy: Orphan` and the manifests role's `manifests_rollout_timeout_default`.
  It carries `postBuild.substituteFrom` naming the Secret `homelab-cluster-vars` exactly
  when the tag's render holds a placeholder.
- `flux/kustomization.yaml`, which lists every other file in `flux/`. The root Kustomization
  `homelab` applies it.
- `digests.json`, described below.

A published tag that defers files is refused, because no Kustomization applies
`<tag>-deferred` until D4.2 adds it.

### Shared kinds and reserved names

A Kustomization that belongs to no deploy tag is a shared kind. `SHARED_KINDS` in
`scripts/renderer/renderer_lib/bundle.py` names every one, and it is empty until D3.3a adds
`cluster-vars`. A shared kind publishes `flux/<name>.yaml` and `cluster/<name>/`. Each member
needs a footprint rule in `FOOTPRINT_RULES` there, which names the tags its files count for,
and `test_every_shared_kind_has_a_footprint_rule` fails a member without one.

Every `cluster` render refuses a deploy tag named `homelab`, the root's name, or after a
`SHARED_KINDS` member. D4.2 adds the `-deferred` suffix to that check.

### `digests.json`

The renderer computes `digests.json` in its own process. It maps each published tag and each
shared kind to two SHA-256 digests:

- `own` covers `cluster/<name>/` alone.
- `footprint` covers every path that counts for the name: the tag's own directories, with
  `-deferred` stripped, its `flux/<tag>.yaml`, and the files of each shared kind whose rule
  lists the tag.

`flux/kustomization.yaml`, `digests.json` itself and `images/` count for no name. Any other
path that no rule claims lands under `_unclassified`, with the same two digests and a `paths`
list. `land.sh`'s wait reads `footprint` through `scripts/deploy_tools/changed_set.py`, and a
change under `_unclassified` counts as every name changed. D6's *What a footprint counts* in
`docs/target-architecture/deploy/d6-delete-old-path.md` defines the classification.

### The publish census

`promote.py check-bundle out` fails the render job before gitleaks and before the push. It
computes its allowlist from the inventory and `SHARED_KINDS`, never from the render, so a
renderer bug cannot widen it. It fails when:

- `flux/` lacks a file for a published tag or a shared kind, or holds any other file;
- `flux/kustomization.yaml` omits a file in `flux/`, or lists one that is not there;
- `flux/<name>.yaml` holds anything but the Kustomization `<name>` applying
  `./cluster/<name>`;
- a published tag has no `cluster/<tag>/`;
- `digests.json` is missing or names another set.

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
`promote` reads `DEPLOY_HOLD` with it, and `tag` writes `PROD_DIGEST` with it. The flux role's `CLAUDE.md` has its rotation steps, and the
rotation registry's `external-90d` tier is the one its SOPS copy belongs on.

The SOPS key `flux_github_status_token` is a fine-grained token with Commit statuses read and
write on two repositories, `DanielH2018/server` and `DanielH2018/dotfiles`, which
notification-controller posts statuses with. It has no expiration date. It lives only in
SOPS, on the registry's `external` tier, and the flux role's `CLAUDE.md` has its rotation
steps.
