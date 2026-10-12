# The Flux deploy plane

Flux's source controller, Kustomize controller and notification controller run in the
`flux-system` namespace on daniel-box. They follow no source yet, so Flux applies nothing, and
the GitOps deployer and Ansible still deploy every service, Flux included. The plan that takes
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

## Reading Flux objects

The read-only identity that plain `kubectl` uses reads no Flux API group yet, so a
`kubectl get` on a Flux kind returns Forbidden. Read the controllers' Deployments and pods
instead, which it can read. Granting the three Flux groups is a later change, applied by the
operator through `k3s-bringup.yml --tags kubeconfig` (**operator-only**).

To check the controllers after a deploy, run `kubectl -n flux-system get deploy` and
`uv run python scripts/diagnostics/probe.py health flux`.

## Tokens

No agent token ever carries the `write:packages` scope. Pushing an artifact, signing it and
moving its `prod` tag belong only to the code-free jobs of the promote workflow that a later
slice adds, under that job's own `GITHUB_TOKEN`.
