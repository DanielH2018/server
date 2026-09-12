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
  interleave with the GitOps timer or the secret-rotation cron on the tree, nor with another
  deploy of the same service on the cluster
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

## Exit codes are resume points

Each of these means **nothing was deployed**. None is a playbook failure.

| Code | Means | Do |
|---|---|---|
| 77 | The snapshot worktree could not be created | Check `/tmp/homelab-deploy-snapshots/` is writable and `git worktree add --detach` works |
| 75 | A lock stayed busy — the tree lock, or one of this run's services' | Retry |
| 4 | The commit being deployed — `HEAD`, or `--at <sha>` — is behind `origin/master` on a path the deploy reaches | Pull, then retry. Never `--skip-staleness-check` |
| 3 | The change is broad and maps to no single service | Run the playbook the change's plane needs |
| 2 | The tag matched no service | `--list-services` prints the valid values |
| 64 | The flags contradict each other, or `--at` named no commit this checkout has (or none at all) | Fix the command line |

Being *ahead* of master is normal branch work and is never refused.

## Checking a change without deploying it

Three modes, and they see genuinely different things. Reaching for the wrong one is how a
manifest bug reaches production.

| Mode | What sees the manifests | Catches |
|---|---|---|
| `prek run --all-files` | Nothing — renders locally, then parses and schema-checks | Jinja indent bugs, invalid YAML, duplicate keys, undefined fields, wrong types |
| `--check` | Nothing — the apply is **skipped** | Task-level wiring. Not the manifests themselves |
| `--dry-run` | The **live API server**, via `kubectl apply --dry-run=server` | Everything prek catches, plus CRD schemas, CRD ordering and admission rejections |

`--dry-run` renders to a temp directory, applies with `--dry-run=server`, and discards it.
Nothing is staged, applied, patched or rolled.

### What a green dry run does not prove

**It refuses some roles outright.** The ones in `k8s_dry_run_unsupported` mutate outside the
shared manifests path — sidecar ConfigMaps built with `kubectl create`, probe Jobs, `exec -i`
into a live pod — so they would half-apply. The playbook fails fast and names them.

**A brand-new service is only half-checked.** `volume-claim` is skipped because it is a
dependency of many roles and mutates, and nothing at admission verifies that a referenced PVC
exists. So the Deployment validates while the volume is never proven provisionable.

**It says nothing about runtime.** Scheduling, PVC binding, probe behaviour and rollout
behaviour all need a real deploy.

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

- The lock serialises; it does not queue fairly. Exit 75 means retry.
- Scope your deploy to your own services. A shared SHA range covers other sessions' work too,
  and deploying another session's half-finished landing is not yours to do.
- **`--detach` returning is not a verified deploy.** It backgrounds the rollout wait, which is
  most of the deploy.

## The deploy queue page

`deploy.local.<domain>` (Authelia two-factor) shows the four things "the queue" means here:
landings in flight and who holds the tree lock, services whose release is behind master,
the deployer's markers, and open PRs. Each button runs the command you would type: land
runs `land.sh --pr <n> --since <sha>`, deploy runs `deploy.sh --tags <svc>`, cancel
SIGTERMs a listed landing, clear hold removes `hold_sha` and `hold_plane` together against a
SHA you type, and the staging override sets or clears its marker. Output goes to
`~/.local/state/deploy-ui/` on daniel-box and one audit line per action reaches Loki under
`deploy-ui`. The daemon is `roles/setup/deploy_ui`; the route is `roles/k8s/deploy-ui`.
