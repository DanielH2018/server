# Deploying by hand

The path an operator drives. The automatic pull-based path is the
[GitOps pipeline](gitops-pipeline.md), and reaching for the wrong one is the usual mistake —
GitOps decides what to deploy on its own, this is you deciding.

## Use the wrapper

```bash
./scripts/deploy.sh --tags "<service>"
```

Not a bare `ansible-playbook`. The wrapper does four things the bare form does not:

- Takes `/var/lock/server-git-tree.lock` to snapshot `HEAD`, then one
  `/var/lock/server-deploy-<tag>.lock` per service for the playbook, so the deploy cannot
  interleave with any [tree-lock holder](#who-holds-the-tree-lock) on the tree, nor with
  another deploy of the same service on the cluster
  ([ADR-0011](adr/0011-one-lock-serialises-every-deploy-path.md),
  [ADR-0017](adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md)). Two sessions
  deploying different services run at the same time.
- Renders from a detached worktree of `HEAD` under `/tmp/homelab-deploy-snapshots/`, not from
  the working tree. **An uncommitted edit is not deployed.** `--check` and `--dry-run` are the
  exceptions and still read the working tree. `--at <sha>` snapshots that commit instead of
  `HEAD`, and asks the tag check and the staleness gate about it too. `land.sh` passes the
  merge commit of the pull request it is landing, so a landing deploys without waiting for the
  GitOps tick to fast-forward the primary checkout onto that commit.
- Checks the tags against `containers_list` first, because Ansible itself exits 0 on a tag
  that matches nothing.
- Refuses a tree that is behind `origin/master` on something the deploy renders, because a
  stale tree renders stale templates and reverts live config while every repo-side check reads
  green. With `--tags` the question is narrowed to those tags: a commit behind that touches
  only other roles prints a note and deploys, while one reaching a deployed tag or a broad path
  (shared templates, `ansible/inventory/`, the setup plane) refuses and names the commits. An
  unscoped run refuses on any commit behind.

The bare forms still work and are what the wrapper runs. Use them only when you deliberately
want none of the above.

### Who holds the tree lock

This is the one list of the jobs that take `/var/lock/server-git-tree.lock`. Other docs link
here rather than naming the holders themselves. The table is generated from the templates that
take the lock, so a new holder appears in it without an edit. Cadences are in
[Scheduled jobs](reference/crons.md).

--8<-- "assets/generated/fragments/tree-lock-holders.md"

`ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_timeout_budgets.py` pins the wait of
each holder against the deployer's worst-case hold. Its census names the waiters by hand, so a
new waiter has to join it. `deploy_under_locks.py:TREE_LOCK_HOLDERS` is the message a deploy
prints when it could not take the lock, and
`scripts/docs/tests/test_fragments_deploy.py` fails when that message omits a holder in the table.

The three crons take the lock through one Jinja macro,
`ansible/roles/setup/initial_setup/templates/git-tree-lock.j2`, which owns the wait and the
alert a skip raises.

## Exit codes are resume points

Each non-zero exit in `DEPLOY_SH_NO_VERDICT` means **nothing was deployed**. None is a
playbook failure, and each is a resume point.

**The wrapper says which, on its own last two lines.** A failing run ends with
`deploy.sh: <NAME> (<code>): <meaning> <what to do>` and then
`DEPLOY-VERDICT: <verdict> (<the arguments>)`, both read from
`scripts/lib/exit_codes.py`. The per-code table is rendered from that module into
[Scripts](reference/scripts.md#scriptsdeploysh), so there is one copy and it is the one the
script exits with. This page carried a hand-kept copy until 2026-09-30 and had lost 76 by the
time a test was written for it (#2853).

`DEPLOY_PLAYBOOK_FAILED` is the exception the set excludes: the playbook ran and a task
failed, so whatever applied before it is live.

`DEPLOY_BAD_FLAGS` (64) is outside the set for a different reason: nothing was deployed, and
re-running the same command line changes nothing. It covers a flag this wrapper refuses and a
pass-through argument ansible-playbook's own parser refuses, which the wrapper asks about
before it takes the lock. `land.sh` exits 64 on a bad argument too, so one number means "you
typed it wrong" for both entry points (#3024).

Being *ahead* of master is normal branch work and is never refused.

## Checking a change without deploying it

The `deploy` skill (`.claude/skills/deploy/SKILL.md`) owns the three-mode table (`prek run --all-files`, `--check`, `--dry-run`) and the two limits of a green dry run. Only `--dry-run` shows the manifests to an API server.

A dry run writes nothing on the node either. Every host-plane task in a role carries `when: not k8s_dry_run | bool` (#2614), and `ansible/tests/deploy/test_k8s_dry_run_host_writes.py::test_every_host_write_outside_manifests_is_guarded` fails on one that does not.

## Verify twice

A deploy has two questions and one command only answers the first.

```bash
uv run python scripts/diagnostics/probe.py health <service>
```

That gates the rollout and a 180-second restart window — see
[ADR-0012](adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md). It exits 0 only
when the workload is fully rolled out and nothing has restarted recently, and it fails closed
on an unreadable restart time.

**It cannot see whether your change took effect.** Two standing examples:

- An Authelia 302 fires in the middleware before the backend is reached, so a redirect proves
  the edge is up and nothing about the workload.
- Nineteen dead Grafana panels rendered nothing for 55 minutes behind a 1/1 pod, with clean
  migrations and zero errors.

So exercise the thing you actually changed as well.

## Working alongside other sessions

- Deploys of the same service serialize; the tree lock is held for the snapshot only, so two
  deploys of different services run at once (ADR-0017). Neither lock queues fairly. Exit 75
  means a lock was busy and nothing was deployed — retry. The queued path reports it only
  after the full `LOCK_WAIT`; `--detach` probes the tree lock with `flock -n` and reports it at
  once, so 75 from a `--detach` run says nothing about how long the holder has held it.
- Scope your deploy to your own services. A shared SHA range covers other sessions' work too,
  and deploying another session's half-finished landing is not yours to do.
- **`--detach` returning is not a verified deploy.** It backgrounds the rollout wait, which is
  most of the deploy. `deploy.sh --tags <svc> --detach && cc-wait deploy <svc>` waits for the
  notifier's health gate and exits 0 only when it settled (#3934). For `--changed` or a shared
  role's tag, use the `wait:` line `--detach` prints, because those deploys run other tags.
- A `--detach` run holds its service locks until its notifier's health gate has posted. A
  second deploy of the same service queues behind the gate rather than rolling the workload
  under its sample (#3817).
- A `--detach` verdict the host could not post to Discord is not lost. The notifier queues it
  in `/var/lib/gitops-deploy/detach_spool`, and the next GitOps deployer tick sends it with a
  `(delayed: first attempt <UTC time>)` line (#3987). The detach log holds the verdict either way.

## Retiring a k8s service

Deleting a role from the repo removes nothing live. `kubectl apply` never deletes, and the
role's own prune cannot run once the role is gone. To retire a k8s service, delete its role and
its `containers_list` entry, then add its name to `k8s_retired_services` in
`ansible/inventory/group_vars/all.yml`, all in one PR.

The next k8s deploy runs `ansible/tasks/k8s_retire.yml` for each entry, whatever its `--tags`.
The task file deletes every object the service's staged directory under
`/etc/rancher/k3s/manifests/` declares, its Secret and its `claim-<name>.yaml` included. It
then removes that directory and the service's release and render records. A run that finds
nothing reports `ok`.

Deleting the claim deletes its Longhorn volume, because the `longhorn` StorageClass reclaims
with `Delete`. Land the entry before the next `k3s-bringup.yml --tags longhorn` apply: that
apply returns a volume in no routing list to the `default` group, and a volume outside
`k3s_longhorn_r2_volumes` then backs up daily to B2.

The teardown does not delete the volume's existing Longhorn backups. Once the volume is gone,
no RecurringJob selects it, so no `retain` ever prunes them. To finish the retirement, read
which target holds them from the BackupVolume's `spec.backupTargetName`:

```bash
kubectl -n longhorn-system get backupvolumes.longhorn.io | grep <pvc-name>
```

- On `default` (B2), drain the whole prefix through the B2 API with
  `ansible/prune_backups.yml -e prune_mode=b2-drain -e prune_volumes=<pvc-name>`. A dry run
  comes first, then the same command with `-e prune_apply=true`. The run costs one store
  listing, and its target sync drops the BackupVolume and Backup CRs.
- On `r2`, `b2-drain` cannot reach the backups. Use
  `scripts/backup/longhorn_reap.py backups --apply-deleted-volumes`, which deletes a
  deleted volume's backups through Longhorn.

Either way, check `probe.py b2-spend` for the day's Class C headroom before the apply. The
healthchecks-config retirement (#3499) left its B2 backups behind this way (#3519).

## The deploy queue page

`deploy.local.<domain>` (Authelia two-factor) shows the four things "the queue" means here:
landings and deploys in flight with the locks each holds, services whose release is behind master,
the deployer's markers, and open PRs. Each button runs the command you would type: land
runs `land.sh --pr <n> --since <sha>`, deploy runs `deploy.sh --tags <svc>`, cancel
SIGTERMs a listed landing, clear hold removes `hold_sha` and its `hold_plane` ledger lines together against a
SHA you type. Output goes to
`~/.local/state/deploy-ui/` on daniel-box and one audit line per action reaches Loki under
`deploy-ui`. The daemon is `roles/setup/deploy_ui`; the route is `roles/k8s/deploy-ui`.
