---
id: "0029"
title: Each host is configured by its own package, and Ansible is gone
status: Proposed
date: 2026-11-12
governs:
  - "hosts/lib/tests/test_reconcile_size_census.py#raising CEILING is an operator ruling"
---

# ADR-0029: Each host is configured by its own package, and Ansible is gone

## Status

Proposed. Approving this record is **operator-only**
([H7](../target-architecture/host/h7-delete-ansible.md), exit observation 6). Step H7.57 is
not landed until the operator approves it.

On approval, this record amends ADR-0003 and ADR-0013 (*Amendments* below), and it
supersedes the bring-up sections of the setup roles' `CLAUDE.md` files. Whether ADR-0001,
ADR-0011 and ADR-0017 need an amendment is a separate **operator-only** ruling, and
*Questions for the operator* below states the facts for each.

## Context

Until the host slices, Ansible configured every host. Three bring-up playbooks
(`initial_setup.yml`, `bootstrap.yml` and `k3s-bringup.yml`) applied the setup roles under
`ansible/roles/setup/` and `ansible/roles/containers/`. The GitOps deployer re-applied
daniel-box's and daniel-server's setup plane from master on every tick, and daniel-pi took
its setup plays over SSH with `-e target=daniel-pi`.

That model had four costs that the
[target-architecture design](../target-architecture/design.md) names under its host plane:

- A role declared only what should exist. Removing a file or a unit needed an absent arm
  that someone remembered to write, and a missing one left the old state in place.
- No host recorded which commit it had applied.
- A play could run on the wrong machine. Both cluster nodes used `connection=local`, so
  `-e target=<host>` selected that host's variables and ran on the controller.
- A killed controller left a held `dpkg` lock on the Pi.

The design weighed these alternatives and rejected them:

- agent-based tools (Salt, Puppet, Chef), which do not fit a 456 MB Pi;
- `pyinfra`'s push mode, which keeps the push model and its wrong-machine failure;
- `sysext`, because a read-only `/usr` and `/etc` break apt and k3s;
- `bootc`, which is a demo on Ubuntu, and Ubuntu Core, which takes only snaps;
- chezmoi for the host plane, and `doco-cd` or `Komodo` for the Pi's Compose project.

ansible-pull was the runner-up. It would have kept the roles and changed only who triggers
them. The plan made it the fallback if H3's `Depends` test failed, or if reconcile crossed
its line ceiling. The `Depends` test passed on the Pi on 2026-10-16
([H3](../target-architecture/host/h3-pi-pilot.md)), and the reconcile-size census holds
reconcile under its ceiling (exit observation 7).

## Decision

Each host is configured by three Debian packages: `homelab-<hostname>`, `homelab-updater`
and `homelab-tools`. homelab-render renders each host's package tree from the repository,
and `nfpm` builds it. The LAN builder on daniel-box builds a commit on `origin/master`'s
first-parent chain with a green CI verdict
([ADR-0021](0021-the-host-package-builder-trusts-master-plus-green-ci.md)), signs the apt
repository and publishes it. Each host's `homelab-updater` timer installs its own package,
so a host pulls only what is addressed to it. `dpkg` removes a file the package stops
shipping. Four reconcilers in `homelab-reconcile` own the state `dpkg` cannot: `ufw`, libvirt,
users, and the line arm for a file that a maintainer script generates or that another
package owns as a conffile.

`hosts/bootstrap-host.sh` replaces the three bring-up playbooks, so a first install and
every later update share one code path. system-upgrade-controller upgrades k3s
([ADR-0025](0025-suc-upgrades-k3s-and-homelab-k3s-restart-restarts-it.md)). The operator's
user-level tooling is configured by chezmoi. ansible-core, its collections, every playbook
and every setup role are deleted, and the GitOps deployer is retired.

## Decisions recorded with it

H7's plan assigns these narrower decisions to this record.

### The design counts four reconcilers, not three

The design named `ufw`, libvirt and users. H7 added the line arm, because it is the only
mechanism that fits a file a maintainer script generates, or a conffile another package
owns. A divert was the alternative. It does not stop a maintainer script from rewriting an
unowned file, and on a conffile it is the approach that H4's review finding H4-R1-04
rejected. H7 ships no divert.

### Reconcile carries a line ceiling, and crossing it is design

The host plane added a second mechanism beside `dpkg`. The design's *Open questions* asks
whether that mechanism stays small enough to justify itself, and the ceiling is the
trigger. The ceiling is (B + 720) × 1.3, rounded up to the next 50:

- B is the source line count of `hosts/lib/homelab-reconcile` at H3.2b's merge commit,
  because H3.2b stated no size;
- 720 is the growth the slices stated, H4.1b and H4.3b1's `udev` arm in H4 and H7.2a to
  H7.2d in H7;
- the 30% is headroom for fixes inside that stated scope, not room for another arm.

`hosts/lib/tests/test_reconcile_size_census.py` holds B, the stated growth and the
constant, and checks that the constant equals the formula. A PR that crosses the ceiling
fails the census. Raising the constant is an operator ruling. In the crossing PR, the
operator rules whether to cut reconcile, to raise the ceiling here with a reason, or to
switch the host plan to ansible-pull.

### `homelab-checkout-sync` keeps the CI gate and does not read `DEPLOY_HOLD`

The deployer's timer fast-forwarded the primary checkout on daniel-box. After it retired,
`homelab-checkout-sync` in `homelab-daniel-box` does that job. It keeps the deployer's CI
ancestor walk and merges only a commit whose CI verdict is green. It refuses an empty set
of required CI contexts, a dirty tree, the wrong branch, a missing token and a `HEAD` that
is not an ancestor of the fetched tip.

It does not read `DEPLOY_HOLD`. That hold stops cluster promotion, and `promote` already
resets `prod`. The checkout's readers publish docs and rotate secrets, and a cluster hold
makes neither unsafe. The deployer read a hold because it applied what it merged.
checkout-sync applies nothing. The contract is in
`hosts/components/checkout-sync/component.yml`.

### The k3s agent keeps the server token

This resolves H6's *Open questions* 4. H7 adds no agent-scoped token, for three reasons:

- No k3s document describes an already-joined agent switching to an agent-scoped token, so
  the switch would need an operator-attended window and an untested rollback.
- The server token would still join agents, so an agent token in SOPS would add a
  credential without removing one.
- The rebuild path already has the operator present to supply the token from
  `docs/break-glass.md` item 2.

The token stays out of band, in the agent installer's env file. A later slice may still add
an agent token, inside an operator-attended window, with the env file as its rollback.

## What remains of the old model

### The primary checkout on daniel-box

`homelab-checkout-sync` keeps a git checkout on daniel-box current on a timer, under the
tree lock, with a dirty-tree refusal and the CI ancestor walk. The checkout-reading crons
run from it. This is a host whose behaviour depends on mutable working-tree state, the
pillar review's failure class E, and H7 keeps it on purpose.

The recommendation is a later slice. It runs the checkout-reading crons from a package or an
image built at the applied commit. A cron that commits, such as docs-refresh, keeps a
private clone under its own state directory instead of the operator's checkout.
checkout-sync, its lock, its refusal and its walk then retire. Whether that slice runs is
an **operator-only** ruling.

### The `ansible/` directory name

The keep-list leaves `ansible/vars/`, `ansible/inventory/`, `ansible/filter_plugins/`,
`ansible/templates/`, `ansible/tests/`, `ansible/secret_rotation.yml`, and each k8s role's
`templates/`, `defaults/` and `files/` under `ansible/`. A reader of that tree looks for
playbooks that no longer exist.

The follow-on is one move PR that renames the tree to a neutral root the operator picks
(**operator-only**). It uses `git mv` only, and changes every path constant through
`scripts/lib/repo_paths.py`. The same PR moves `ansible/.sops.yaml` beside `vars/`, because
SOPS searches upward from the file. It also updates `.gitleaks.toml`, the Renovate rules,
the prek hooks and the CI path filters. It runs when no other slice has a branch open,
because every open branch conflicts with it.

## Consequences

- **A merged host change reaches its host without a hand run.** The updater installs the
  next published version. A change that republishes several hosts reaches a canary host
  first.
- **A failing `postinst` wedges every apt run on that host,** unattended-upgrades included.
  The updater holds the host, and the way out needs root, so it is operator-only.
  `docs/host-packages.md` has the procedure.
- **The signing key is root-equivalent on every host,** so only the LAN builder holds it.
  A compromised merge path reaches every host, the Pi included, as ADR-0021 records.
- **A hand edit to a shipped file is reverted on the next version.** `dpkg --verify`
  reports it in between.
- **The setup roles' bring-up sections are superseded.** `docs/host-packages.md` and
  `ansible/README.md` hold the bring-up procedure. The deployer's role doc is archived as
  `docs/archive/gitops-deploy-role.md`, and the deleted roles' docs stay in git history.
- **Reversing this decision is a rewrite.** The setup roles are deleted, not parked. A
  return to Ansible means recovering them from history before H7.55 and re-deriving every
  move since.

## Amendments

On approval, this record amends two Accepted records. The amended record gains an "Amended
by ADR-0029" note in its *Status*; its decision text is not rewritten.

- **[ADR-0003](0003-sops-with-age-for-secrets-at-rest.md).** Its decision says the
  `community.sops.sops_decrypt` lookup decrypts values at deploy time. ansible-core and the
  collections are gone, so no deploy-time lookup remains. ADR-0022 already records the two
  readers that replaced it: the cluster decrypts its own file with its own key, and a host
  decrypts its own per-host file with its own key. `secrets.yml` stays the encrypted source
  of record.
- **[ADR-0013](0013-daniel-pi-stays-on-docker.md).** Its 2026-10-28 amendment says
  "`-e target=daniel-pi` stays for the setup plays." The setup plays are deleted. The Pi's
  setup plane ships in `homelab-daniel-pi`, and `homelab-docker-engine.service` moves the
  Docker engine. Nothing reaches the Pi over SSH with `-e target=daniel-pi`.

## Questions for the operator

Each of these is an **operator-only** ruling on whether the record needs an amendment.
This record states the facts and decides none of them.

- **[ADR-0001](0001-mkdocs-site-with-generated-reference.md).** Its title says the
  reference pages are generated from the Ansible tree. The generators under `scripts/docs/`
  read the setup roles until H7.18a. Since H7.18a they read `hosts/*/package.yml`, the
  component units and the timers, and no playbook or setup role is left to read.
- **[ADR-0011](0011-one-lock-serialises-every-deploy-path.md) and
  [ADR-0017](0017-the-tree-lock-guards-the-tree-not-the-cluster.md).** `deploy.sh` and
  `gitops-deploy.service` are no longer lock holders: H7.52 retired the deployer's timer,
  and H7.53 deleted `deploy.sh` and the deployer's code. `homelab-checkout-sync` and the crons that write the tree still take
  the tree lock.

## Governs

The census marker in `hosts/lib/tests/test_reconcile_size_census.py` opens "raising CEILING
is an operator ruling" and cites this record. `governs:` names it by that text, so an edit
above it moves nothing.
