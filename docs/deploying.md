# Deploying by hand

The path an operator drives. The automatic pull-based path is the
[GitOps pipeline](gitops-pipeline.md), and reaching for the wrong one is the usual mistake —
GitOps decides what to deploy on its own, this is you deciding.

## Use the wrapper

```bash
./scripts/deploy.sh --tags "<service>"
```

Not a bare `ansible-playbook`. The wrapper does three things the bare form does not:

- Takes `/var/lock/server-git-tree.lock`, so the deploy cannot interleave with the GitOps
  timer, the secret-rotation cron, or another session ([ADR-0011](adr/0011-one-lock-serialises-every-deploy-path.md)).
- Checks the tags against `containers_list` first, because Ansible itself exits 0 on a tag
  that matches nothing.
- Refuses a tree that is behind `origin/master`, because a stale tree renders stale templates
  and reverts live config while every repo-side check reads green.

The bare forms still work and are what the wrapper runs. Use them only when you deliberately
want none of the above.

## Exit codes are resume points

Each of these means **nothing was deployed**. None is a playbook failure.

| Code | Means | Do |
|---|---|---|
| 75 | The lock stayed busy | Retry |
| 4 | The tree is behind `origin/master` | Pull, then retry. Never `--skip-staleness-check` |
| 3 | The change is broad and maps to no single service | Run the playbook the change's plane needs |
| 2 | The tag matched no service | `--list-services` prints the valid values |

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

**A brand-new service is only half-checked.** `seed-volume` is skipped because it is a
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
