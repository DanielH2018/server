# The Flux deploy plane

Flux's source controller, Kustomize controller and notification controller run in the
`flux-system` namespace. They follow no source yet, so Flux applies nothing, and
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

## Publishing and promotion

The `promote` workflow, `.github/workflows/promote.yml`, runs after each successful push run
of CI on master. It publishes the commit as the OCI artifact
`ghcr.io/danielh2018/homelab-manifests:sha-<commit>` and moves the tag `prod` to the newest
published master commit. No controller follows `prod` yet, so a move changes nothing in the
cluster. The workflow has four jobs:

1. `render` checks out the triggering commit and refuses one that is not on master. It builds
   the bundle, one empty root Kustomization, and scans it with gitleaks. It holds read access
   only.
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
and `tag`, so it never moves `prod`.

## Tokens

No agent token ever carries the `write:packages` scope. Pushing an artifact, signing it and
moving its `prod` tag belong only to the code-free `publish` and `tag` jobs, under each job's
own `GITHUB_TOKEN`.

The repository secret `PROMOTE_TOKEN` is a fine-grained token with Variables read and write
on this repository and a 90-day expiry. `promote` reads `DEPLOY_HOLD` with it, and `tag`
writes `PROD_DIGEST` with it. The flux role's `CLAUDE.md` has its rotation steps, and the
rotation registry's `external-90d` tier is the one its SOPS copy belongs on.
