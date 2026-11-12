# The GitOps pipeline

How a merged change reaches the hosts and the cluster.

H7.52 retired `gitops-deploy.timer` and its service. The deployer's record, its rules and the
incidents behind them are archived in [The GitOps deployer](archive/gitops-deployer.md) and
[the archived role page](archive/gitops-deploy-role.md). H7.53 deleted its code.

!!! warning "A merge is not a deploy"
    `promote` and Flux apply every k8s change after master CI, an image-pin bump and a
    deploy-plane path included, and `land.sh` waits on that promotion. The host packages apply
    the host plane once the builder publishes them. No setup role applies on its own:
    `docs/deploying.md` says which path applies a change. `k8s_autodeploy` says only whether
    Renovate may automerge an image bump: a role on the denylist, which holds the roles
    declaring `false`, gets a `manual —` PR.

Flux follows the promoted artifact and applies every service's render, so no Ansible play
deploys a workload.
[The Flux deploy plane](flux-deploy-plane.md) has what it runs and what it reports.

`homelab-checkout-sync.timer` fast-forwards daniel-box's primary checkout to the newest green
master commit. It applies nothing. `hosts/components/checkout-sync/component.yml` holds its
contract.

Each k8s role declares its own `k8s_autodeploy` stance
(`ansible/roles/k8s/<role>/defaults/main.yml`), which says whether Renovate may automerge an
image bump. `scripts/lib/k8s_roles.py` collects the ones declaring `false` into the denylist that
`renovate.json`'s no-automerge rule and the Renovate lander read.

<!-- Generated from k8s_autodeploy_counts.py, which reads every role's own declaration; edit
     the role's defaults/main.yml, not this table. -->
--8<-- "assets/generated/fragments/autodeploy-coverage.md"

## A change under `hosts/` is the builder's

The host-package builder on daniel-box applies a change under `hosts/`:
`homelab-builder.timer` builds each changed package from the newest green master commit,
`homelab-repo-sign.service` signs and publishes it, and each host's `homelab-updater.timer`
installs it. [Host packages](host-packages.md) is the runbook. One change needs two paths: a
key rotation commits the new keyring under `hosts/keys/`, and only a `homelab_builder` apply
rewrites the `archive-key.fpr` the builder checks it against. The runbook names that apply.

## Applying a commit no promotion followed

To apply a green master commit that no promotion followed, dispatch `promote` with the commit's
full 40-hex SHA. `promote` refuses a commit without green CI:

```bash
gh workflow run promote.yml -R DanielH2018/server --ref master -f sha=<sha>
```

## Gating on CI correctly

Do not hand-poll CI. `land.sh` owns the merge, the CI wait, the promotion wait and the verify step, and
`scripts/deploy_tools/await_ci.py` is the CI waiter it uses; the `land-after-merge` skill has the
invocation. Both read the check runs of the merge commit, the same endpoint checkout-sync reads,
so your verdict and its verdict agree. An empty or incomplete check-run list is pending, never
green.

A bare `gh run list --branch master --limit 1` is the wrong gate. GitHub creates the run for a
freshly pushed merge commit a moment after the push, so that query returns the *previous* master
run, green, and the wait returns instantly having watched the wrong commit.
