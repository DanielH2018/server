---
id: "0020"
title: Flux applies the cluster from a promoted artifact
status: Accepted
date: 2026-10-12
governs: []
---

# ADR-0020: Flux applies the cluster from a promoted artifact

## Status

Accepted. The operator approved starting slice D1 on 2026-10-12, when this record opened as
`Proposed`. The last pull request of slice D6 (D6.21) completed it from what D5 and D6 paid
and set it to `Accepted`.

This record supersedes [ADR-0010](0010-pull-based-gitops-over-argo-and-flux.md), whose status
reads `Superseded by ADR-0020`. The operator ruled on 2026-11-06, in the session that orchestrates slice D6, to approve this
record and to amend ADR-0012 and ADR-0017. Each of those two carries a dated *Amendment*
section that names the statements this record overtakes. ADR-0011 stays unchanged.

## Context

[ADR-0010](0010-pull-based-gitops-over-argo-and-flux.md) kept the homelab's own pull-based
deployer and deferred Flux. It rested on three premises: the deployer's decision logic had no
controller-native equivalent, Ansible had to stay the renderer and the secret plane, and
`daniel-box` had little memory to spare.

The [target architecture](../target-architecture/design.md) re-measured all three, in its
section *Cluster deploy plane*. The deployer's decision logic grew from 1,928 to 9,499 lines.
Five path narrowings exist "because a path moved while the rendered bytes did not"
([#2810](https://github.com/DanielH2018/server/issues/2810)). Flux decrypts age natively.
`daniel-box` read 44% to 54% memory in the D1 plan's readings of 2026-10-11 and 2026-10-12,
where ADR-0010 recorded 65%. These figures are context for the decision at the time, not live
settings.

On 2026-10-12 the operator was asked whether to approve starting D1, which ADR-0010 deferred.
The answer was:

> The Target Architecture docs are the source of truth

That ruling adopts the slices from D1 on, beginning with
[D1](../target-architecture/deploy/d1-flux-installed.md). [#4695](https://github.com/DanielH2018/server/issues/4695)
opened this record.

## Decision

CI renders every k8s manifest and publishes the result as an OCI artifact. A `promote`
workflow moves the tag that Flux follows, and Flux applies the artifact server-side with prune
and wait. Neither the GitOps deployer nor Ansible applies a k8s workload.
[Contracts](../target-architecture/contracts.md) C2 owns the artifact and its promotion, and
C3 owns the Flux objects.

Every k8s `containers_list` entry is Flux's, and a service stays Flux-owned or retires. H1.9
([#7752](https://github.com/DanielH2018/server/issues/7752)) dropped the `owner: flux` key
each entry carried, because an entry in `cluster/services.yml` is Flux's by being there. The operator ruled on 2026-11-06 ([#7464](https://github.com/DanielH2018/server/issues/7464))
that a service does not move back to Ansible. A bad role change reverts through `promote` or
holds through the `DEPLOY_HOLD` repository variable.

The GitOps deployer keeps the host plane. It applies a setup-role change as
`initial_setup.yml --tags <role>` until slice H7 retires it. `daniel-pi` stays on Docker
([ADR-0013](0013-daniel-pi-stays-on-docker.md)).

## Consequences

Each item below comes from the tree, a merged pull request or a plan at the commit that
completes this record.

**The deployer and the landing code shrank, the landing code less than planned.**

- The deployer's Python under `ansible/roles/setup/gitops_deploy/files/` measured 9,620 lines
  at `70591c307` and 7,763 lines at the commit before this record's acceptance. The command is
  `git archive <rev> ansible/roles/setup/gitops_deploy/files | tar -xO | wc -l`.
- `land.sh`, `land.py` and `land_lib/` measured 6,258 lines at `70591c307` and 8,639 lines
  after D6.18b, by the same method with `--exclude='*/tests/*'`. D1 to D5 added the
  promotion waits before D6 removed the k8s arms, so the landing code ends above its baseline.
  D6's exit 6 bounds it at 8,800 lines.
- `scripts/deploy_tools/promote.py` is 492 lines against a bound of 900.

**A k8s tag has one writer, and the tree enforces it.**

- `deploy.sh` exits 5 for a k8s tag, `--check` and `--dry-run` included
  (`scripts/deploy_tools/deploy_lib/owners.py:refuse_k8s_tags`).
- The publish floor (`scripts/deploy_tools/bundle_census.py:PUBLISH_FLOOR`) fails publish
  when the service list drops `coredns`, `longhorn`, `metallb` or `traefik`.
- The deployer routes every path under `ansible/roles/k8s/<role>/` to `cs.flux` and reads no
  `owner`. `ansible/roles/setup/gitops_deploy/tests/test_changeset_has_no_service_fields.py`
  holds that the deleted `ChangeSet` fields stay deleted.
- `k8s_tags` (`scripts/lib/k8s_roles.py`) is the one reader of which tags Flux applies, and
  the render entry points call it directly. The census that followed each entry point down to
  it was deleted by #8165, once its `flux_owned_tags` wrapper was gone.

**Removing a tag from the bundle deletes its objects.** D6.20 ([#7487](https://github.com/DanielH2018/server/pull/7487))
switched 65 `<tag>` Kustomizations to the default deletion policy, which follows `prune`.
Seven of them render `prune: false`, so they still delete nothing. Fifty-two Kustomizations and
the root keep `deletionPolicy: Orphan`: `flux`, every `-deferred`, `-snapshot`, `-prepull`,
`-precheck` and `-verify` kind, and the shared kinds.
[#7485](https://github.com/DanielH2018/server/issues/7485) later moved every `-snapshot` and `-prepull` but `flux`'s to the default policy, because
their objects carry the tag's name and no emptied template reaches them at a retirement.

- The renderer labels every PersistentVolumeClaim and Namespace with a prune exemption
  (`scripts/renderer/renderer_lib/prune_label.py:EXEMPT_KINDS`), so a retired tag leaves its
  data in place. A PersistentVolume has no exemption: `scripts/deploy_tools/tag_rule.py`
  refuses to drop a tag whose render at `prod` holds one.
- A retired tag leaves its `-snapshot` and `-prepull` objects behind, because those kinds keep
  `Orphan`. The operator deletes them by hand
  ([#7485](https://github.com/DanielH2018/server/issues/7485) tracks the follow-up).
- The deletion is not reversible. A revert of D6.20 restores `Orphan` and returns no object
  already deleted.
- The `flux-deletion-drill` job proves the policy in a throwaway kind cluster. The ruleset
  requires that check (D6 Open question 10, ruled 2026-10-25).
- At D6.20's base, `owner: flux` showed 0 moves back to Ansible, 1 retirement (uptime-kuma, 2026-11-04) and
  0 renames since D2.

**`promote` and `DEPLOY_HOLD` replace the deployer's rollback and hold.**

- A promotion that fails its verification makes `promote`'s `settle` job write `DEPLOY_HOLD`,
  and `land.sh` ends `deploy-failed` with cause `promote-held`. Clearing the hold is the
  operator's decision (`docs/landing.md`).
- `promote.py` refuses a bundle that loses an object no `Removes-Object` trailer declares
  (`scripts/deploy_tools/object_loss.py`).
- `promote` defers a promotion that the weekly reboot window would cut short
  (`scripts/deploy_tools/promote_lib/reboot_window.py`).

**The soak Job draws one Grafana deploy marker per soaked promotion.** The soak
(`scripts/renderer/job_files/flux_soak.py`) prints an `event=deploy` line, and Grafana's "Flux deploys" annotation query reads it from
Loki. A soak that passes prints it with `result=ok`. A soak that fails prints it with
`result=fail`, the failed services in `services=` and `duration_s`. A soak that errors
prints it with `result=fail` and `services=flux`. Flux has applied the revision in every
case, so the dashboards mark it, and the annotation's title tells a failure from a pass
([#6164](https://github.com/DanielH2018/server/issues/6164)).
`scripts/renderer/tests/test_flux_soak.py` holds this paragraph to what the soak prints.

**`land.sh` waits on the cluster, never on one `promote` run.** GitHub can evict a queued
run, and a later run can promote a newer commit. After merging M, `promote.py land-wait M`
takes M's changed set from two `digests.json` files: M's own, and the one on the newest
published ancestor of M's first parent. A missing or unreadable file counts as every name
changed. Every landing reaches the wait, and the changed set names the tags its health gate and
its `source=promote` annotation cover. The wait ends in one of three ways:

- `settled`: every Kustomization in the set, and `homelab`, is Ready at a revision R that
  descends from M or equals it, and `flux/soak` reads `success` on R.
- `deploy-failed` with cause `promote-held`: `DEPLOY_HOLD` reads anything but `none`.
- `gave-up` with cause `promote-timeout`: the wait reaches twice the promote timeout.

**A later promotion can supersede one still in `verify`.** `SUPERSEDE` in
`scripts/deploy_tools/promote.py` turns this on. `decide` promotes T over an in-flight P only
when all of the following hold:

- `DEPLOY_HOLD` reads `none`.
- `prod` is not the digest tagged `superseding`, so at most one supersession happens per
  verification.
- P has no failure yet.
- T's own change is disjoint from P's set.
- Neither change touches `longhorn`, `flux` or `cluster-vars`, or was widened.
- No hold without a reset is involved.
- T is outside the reboot window.

T's verification takes its changed set against P's base, so one verification covers both
commits, and `last-good` moves once, to T. P's `verify` prints `superseded` once `prod` no
longer names P. Its `settle` writes nothing: `settle` writes `last-good`, the hold or the reset
only while `prod` still names its own digest. Before `land-wait` reports a `flux/soak` failure on R,
it re-reads `homelab`. A move to a descendant of R may be a supersession, so the wait goes
on.

**Deploys depend on GitHub and GHCR.** The artifact lives in GHCR and `promote` runs in GitHub
Actions. Break-glass promotion covers an outage of either (`docs/break-glass.md`). It needs an
operator-held key pair and keeps the in-cluster registry in service. The `flux-deletion-drill`
workflow runs a break-glass case with a throwaway key, and `docs/break-glass.md` states which steps the
drill never runs.

**Flux's controllers and CRDs are something to upgrade.** Renovate opens a manual pull request
for each `fluxcd/flux2` release, because the vendored install needs regenerating
(`docs/flux-deploy-plane.md`, *Upgrading Flux*). At the time of this decision the Flux release
in use listed Kubernetes up to 1.36 while the cluster ran 1.37. The operator accepted that gap
on 2026-10-12.

**Two codebases remain until later slices.**

- The Ansible k8s mechanics stay in the tree until slice R4: the `manifests` role's tasks and
  the k8s play of `ansible/deploy.yml`. Nothing runs them for a k8s tag.
- The deployer's setup arm, its fast-forward and `deploy_ui` stay until slice H7.
- `hold_sha` covers the host plane only, and `DEPLOY_HOLD` covers the cluster.

**ADR-0012 and ADR-0017 differ from the tree, and each carries an *Amendment* section.** The
operator ruled on 2026-11-06 that ADR-0011 stays unchanged, because its decision, one lock on
every path that writes the git tree, still holds for the host plane.

## Governs

No `# DECIDED:` marker names this record, so `governs:` is empty. The tests named under
*Consequences* enforce the decision: the publish floor, the entry-point census, the `ChangeSet` test
and the deletion drill.
