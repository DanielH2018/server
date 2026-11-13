# Deploying a change

No script here deploys a workload by hand. `promote` and Flux apply every k8s change, and the
host packages apply the host plane, the Pi's services included. This page says which path
applies a change and what runs in place of the GitOps deployer.

H7.52 retired `gitops-deploy.timer`, and H7.53
([#7962](https://github.com/DanielH2018/server/issues/7962)) deleted its code with
`scripts/deploy.sh`. [The GitOps deployer](archive/gitops-deployer.md) and
[the archived role page](archive/gitops-deploy-role.md) hold its record, its rules and the
incidents behind them.

!!! warning "A merge is not a deploy"
    `promote` and Flux apply every k8s change after master CI, an image-pin bump and a
    deploy-plane path included, and `land.sh` waits on that promotion. The host packages apply
    the host plane once the builder publishes them. No setup role applies on its own.

## Which path applies a change

- **A k8s role, an inventory value or a shared template:** merge through `land.sh`. `promote`
  publishes the merge commit, Flux applies it, and `land.sh` gates the tags the PR changed. The
  `land-after-merge` skill has the commands, and [Landing](landing.md) has the verdicts. To apply
  a green master commit that no promotion followed, dispatch `promote` with its full 40-hex SHA:
  `gh workflow run promote.yml -R DanielH2018/server --ref master -f sha=<sha>`.
- **A Pi Compose service or anything under `hosts/`:** the builder publishes the package and the
  host's updater installs it ([Host packages](host-packages.md)). `land.sh` waits for that host.
- **A host's setup or a new host's bring-up:** H7.55 deleted the setup roles and the bring-up
  playbooks. A host's setup ships in its host package, as the bullet above says, and
  `hosts/bootstrap-host.sh` brings up a new host.

Flux follows the promoted artifact and applies every service's render, so no Ansible play
deploys a workload. [The Flux deploy plane](flux-deploy-plane.md) has what it runs and what it
reports.

`homelab-checkout-sync.timer` fast-forwards daniel-box's primary checkout to the newest green
master commit. It applies nothing. `hosts/components/checkout-sync/component.yml` holds its
contract.

### A change under `hosts/` is the builder's

The host-package builder on daniel-box applies a change under `hosts/`.
`homelab-builder.timer` builds each changed package from the newest green master commit,
`homelab-repo-sign.service` signs and publishes it, and each host's `homelab-updater.timer`
installs it. [Host packages](host-packages.md) is the runbook. One change needs two paths: a
key rotation commits the new keyring under `hosts/keys/`, and only a `homelab_builder` apply
rewrites the `archive-key.fpr` the builder checks it against. The runbook names that apply.

### Which image bumps Renovate may automerge

Each k8s role declares its own `k8s_autodeploy` stance
(`k8s/roles/<role>/defaults/main.yml`), which says only whether Renovate may automerge
an image bump. `scripts/lib/k8s_roles.py` collects the roles declaring `false` into the
denylist that `renovate.json`'s no-automerge rule and the Renovate lander read. A role on the
denylist gets a `manual —` PR.

<!-- Generated from k8s_autodeploy_counts.py, which reads every role's own declaration; edit
     the role's defaults/main.yml, not this table. -->
--8<-- "assets/generated/fragments/autodeploy-coverage.md"

### Who holds the tree lock

This is the one list of the jobs that take `/var/lock/server-git-tree.lock`. Other docs link
here rather than naming the holders themselves. The table is generated from the templates that
take the lock, so a new holder appears in it without an edit. Cadences are in
[Scheduled jobs](reference/crons.md).

--8<-- "assets/generated/fragments/tree-lock-holders.md"

The crons take the lock through one Jinja macro, `k8s/templates/git-tree-lock.j2`, which
owns the wait and the alert a skip raises.

## Checking a k8s change before it merges

The `deploy` skill (`.claude/skills/deploy/SKILL.md`) owns the table of what `prek run --all-files`,
the renderer and CI's render-diff gates each catch.

## Verify twice

A landed change has two questions and one command only answers the first.

```bash
uv run python scripts/diagnostics/probe.py health <service>
```

That gates the rollout and a 180-second restart window — see
[ADR-0012](adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md). It exits 0 only
when the workload is fully rolled out and nothing has restarted recently, and it fails closed
on an unreadable restart time. On a k8s tag it then reads the tag's firing alerts and
ingress probes (`docs/claude-tooling.md` has the selector).

**It cannot see whether your change took effect.** Two standing examples:

- An Authelia 302 fires in the middleware before the backend is reached, so a redirect proves
  the edge is up and nothing about the workload.
- Nineteen dead Grafana panels rendered nothing for 55 minutes behind a 1/1 pod, with clean
  migrations and zero errors.

So exercise the thing you actually changed as well.

## Working alongside other sessions

- Scope a landing to your own PR. A shared SHA range covers other sessions' work too, and
  landing another session's half-finished work is not yours to do.

## Retiring a k8s service

To retire a k8s service, follow
[*Retiring a flux-owned service*](flux-deploy-plane.md#retiring-a-flux-owned-service).

Retiring a service does not delete the volume's existing Longhorn backups. Once the volume is gone,
no RecurringJob selects it, so no `retain` ever prunes them. To finish the retirement, read
which target holds them from the BackupVolume's `spec.backupTargetName`:

```bash
kubectl -n longhorn-system get backupvolumes.longhorn.io | grep <pvc-name>
```

- On `default` (B2), drain the whole prefix through the B2 API with
  `uv run python scripts/backup/prune_backups.py --volumes <pvc-name>`. A dry run
  comes first, then the same command with `--apply`. The dry run lists the store
  once and saves the listing to `/tmp/longhorn-b2-listing.json` on daniel-box. The apply reuses
  that listing for up to 60 minutes and prints `listing: reused` with its age. After 60 minutes,
  or for a different bucket, it prints `listing: fresh, because <reason>` and lists again. Add
  `--fresh-listing` to force a fresh listing. The apply re-reads each volume's own
  prefix after deleting it, so a version created since the dry run stops the drain with an
  error, and a version already deleted is skipped as `already gone`. The apply removes the
  saved listing once it has deleted anything, so a second apply needs a new dry run. Its
  target sync drops the BackupVolume and Backup CRs.
- On `r2`, `b2-drain` cannot reach the backups. Use
  `scripts/backup/longhorn_reap.py backups --apply-deleted-volumes`, which deletes a
  deleted volume's backups through Longhorn.

Either way, check `probe.py b2-spend` for the day's Class C headroom before the apply. The
healthchecks-config retirement (#3499) left its B2 backups behind this way (#3519).

## The deploy queue page

`deploy.local.<domain>` (Authelia two-factor) shows landings in flight with the locks each
holds, recently finished landings, and open PRs. Each button runs the command you would type:
land runs `land.sh --pr <n> --since <sha>`, and cancel SIGTERMs a listed landing. Output goes to
`~/.local/state/deploy-ui/` on daniel-box and one audit line per action reaches Loki under
`deploy-ui`. The daemon is `hosts/components/deploy-ui/`; the route is `k8s/roles/deploy-ui/`.
