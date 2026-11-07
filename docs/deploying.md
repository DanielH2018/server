# Deploying by hand

`scripts/deploy.sh` deploys no workload. `promote` and Flux apply every k8s change, the
[GitOps pipeline](gitops-pipeline.md) applies setup roles, and the Pi's services ship in its host
package. This page says which path applies a change, and what the wrapper still does.

## Which path applies a change

- **A k8s role, an inventory value or a shared template:** merge through `land.sh`. `promote`
  publishes the merge commit, Flux applies it, and `land.sh` gates the tags the PR changed. The
  `land-after-merge` skill has the commands, and [Landing](landing.md) has the verdicts. To apply
  a green master commit that no promotion followed, dispatch `promote` with its full 40-hex SHA:
  `gh workflow run promote.yml -R DanielH2018/server --ref master -f sha=<sha>`.
- **A setup role:** the GitOps deployer applies it on the tick after the merge. A role the tick
  cannot apply becomes a `manual_plane` line in `owed.jsonl`, and
  `uv run python scripts/deploy_tools/owed_run.py <role>` clears it. By hand, run the playbook
  and tag that the `Applied by:` line of the role's `CLAUDE.md` names. Neither follows from the
  role's name: `chezmoi_setup` takes the tag `chezmoi`, and `k3s` applies through
  `k3s-bringup.yml`, where the same page names narrower tags than the maximal `k3s`. Ansible
  exits 0 on a tag that matches nothing.
- **A bring-up playbook:** the deployer never applies these, so run the playbook by hand.
- **A Pi Compose service or anything under `hosts/`:** the builder publishes the package and the
  host's updater installs it ([Host packages](host-packages.md)).

## What the wrapper still does

The wrapper runs `ansible/deploy.yml` and no other playbook. That playbook holds one play, which
runs no role on any host, because no `containers_list` entry is `platform: docker`. What each form
does follows from that:

- **A k8s tag in `--tags`:** exit 5, under `--check` and `--dry-run` too
  (`scripts/deploy_tools/deploy_lib/owners.py:refuse_k8s_tags`).
- **`--changed [<ref>]`:** a read of what a branch owes by hand. A broad range exits 3 and prints
  the setup or bring-up command. Any other range derives no tag and ends with the verdict
  `nothing-to-deploy`.
- **No `--tags`:** takes the locks, cuts the snapshot and runs `deploy.yml`, which runs no role.
- **`-e target=daniel-pi`:** runs the secrets preamble against the Pi and no role, so it deploys
  nothing. `wg-easy` passes the tag check because daniel-box's list carries it too, and any other
  Pi service name is a tag miss (exit 2).
- **`--check` and `--dry-run`:** unlocked, from the working tree, and with no role to render, so
  neither shows a manifest to anything. *Checking a k8s change before it merges* below has what
  does.
- **`--list-services`:** prints every valid tag, and names each k8s tag on stderr.

## The gates and locks of a run

A run of the wrapper does four things a bare `ansible-playbook` does not:

- Takes `/var/lock/server-git-tree.lock` to snapshot `HEAD`, then one
  `/var/lock/server-deploy-<tag>.lock` per service for the playbook, so the run cannot
  interleave with any [tree-lock holder](#who-holds-the-tree-lock) on the tree, nor with
  another run on the same service
  ([ADR-0011](adr/0011-one-lock-serialises-every-deploy-path.md),
  [ADR-0017](adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md)).
- Runs from a detached worktree of `HEAD` under `/tmp/homelab-deploy-snapshots/`, not from
  the working tree. **An uncommitted edit is not read.** `--check` and `--dry-run` are the
  exceptions and still read the working tree. `--at <sha>` snapshots that commit instead of
  `HEAD`, and asks the tag check and the staleness gate about it too.
- Checks the tags against `containers_list` first, because Ansible itself exits 0 on a tag
  that matches nothing.
- Refuses a tree that is behind `origin/master` on something the run reads. With `--tags` the
  question is narrowed to those tags: a commit behind that touches only other roles prints a
  note and proceeds, while one reaching a named tag or a broad path (shared templates,
  `ansible/inventory/`, the setup plane) refuses and names the commits. An unscoped run refuses
  on any commit behind.

Once its gates pass, the wrapper also clears the temp directories that dead Ansible runs left
under `~/.ansible/tmp`. A controller killed mid-task leaves an `ansible-tmp-*` directory. Any
exit that skips Python's `atexit` handlers leaves an `ansible-local-*` directory. The wrapper
removes the first kind after a day. It removes the second kind after a day only when the
directory is empty and its process is gone. `journalctl -t deploy-ansible-tmp-sweep` lists
each removal, so a run that keeps leaking stays visible after the sweep
(`scripts/deploy_tools/deploy_lib/ansible_tmp.py`).

### Who holds the tree lock

This is the one list of the jobs that take `/var/lock/server-git-tree.lock`. Other docs link
here rather than naming the holders themselves. The table is generated from the templates that
take the lock, so a new holder appears in it without an edit. Cadences are in
[Scheduled jobs](reference/crons.md).

--8<-- "assets/generated/fragments/tree-lock-holders.md"

`ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_timeout_budgets.py` pins the wait of
each holder against the deployer's worst-case hold. Its census names the waiters by hand, so a
new waiter has to join it. `deploy_lib/under_locks.py:TREE_LOCK_HOLDERS` is the message a deploy
prints when it could not take the lock, and
`scripts/docs/tests/test_fragments_deploy.py` fails when that message omits a holder in the table.

The three crons take the lock through one Jinja macro,
`ansible/templates/git-tree-lock.j2`, which owns the wait and the
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

`DEPLOY_FLUX_OWNED` (5) means the tag is not Ansible's to deploy at all. Its `containers_list`
entry is `platform: k8s`, so Flux applies it through `promote` and no Ansible path may. The
wrapper refuses the whole `--tags` list, before any lock, under `--check` and `--dry-run` too,
and `--skip-tag-check` does not bypass it. Merge the change and let `promote` publish it.
`--changed` leaves such a tag out and names it instead of refusing. A list that leaves empty
deploys nothing and exits 0, and its verdict line reads `nothing-to-deploy` rather than
`deployed`. A shared k8s role typed into `--tags` is a tag miss. `--list-services` names each
k8s tag on stderr.

Being *ahead* of master is normal branch work and is never refused.

## Checking a k8s change before it merges

The `deploy` skill (`.claude/skills/deploy/SKILL.md`) owns the table of what `prek run --all-files`,
the renderer and CI's render-diff gates each catch. `deploy.sh --check` and `--dry-run` catch no
manifest fault, because neither runs a role.

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

- Runs on the same service serialize; the tree lock is held for the snapshot only (ADR-0017).
  Neither lock queues fairly. Exit 75 means a lock was busy and nothing ran, so retry. The
  queued path reports it only after the full `LOCK_WAIT`; `--detach` probes the tree lock with
  `flock -n` and reports it at once, so 75 from a `--detach` run says nothing about how long the
  holder has held it.
- Scope a landing to your own PR. A shared SHA range covers other sessions' work too, and
  landing another session's half-finished work is not yours to do.
- **`--detach` returning is not a finished run.** It backgrounds the playbook and the notifier's
  health gate. To wait for the gate, run the `wait:` line `--detach` prints, which exits 0 only
  when the run settled (#3934).
- A `--detach` run holds its service locks until its notifier's health gate has posted (#3817).
- A `--detach` verdict the host could not post to Discord is not lost. The notifier queues it
  in `/var/lib/gitops-deploy/detach_spool`, and the next GitOps deployer tick sends it with a
  `(delayed: first attempt <UTC time>)` line (#3987). The detach log holds the verdict either way.

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

`deploy.local.<domain>` (Authelia two-factor) shows the three things "the queue" means here:
landings and deploys in flight with the locks each holds, the deployer's markers, and open PRs.
Each button runs the command you would type: land runs `land.sh --pr <n> --since <sha>`, cancel
SIGTERMs a listed landing, clear hold removes `hold_sha` and its `hold_plane` ledger lines together against a
SHA you type. Output goes to
`~/.local/state/deploy-ui/` on daniel-box and one audit line per action reaches Loki under
`deploy-ui`. The daemon is `roles/setup/deploy_ui`; the route is `roles/k8s/deploy-ui`.
