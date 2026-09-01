---
name: deploy
description: Deploy a service using Ansible — k3s workloads (the default) or the Pi's Docker services. Use when the user wants to deploy or redeploy a specific service, or to check a k8s manifest change without deploying it (`prek` vs `--check` vs `--dry-run`).
allowed-tools: Bash, Glob
---

Deploy a service using Ansible.

If the user provided a service name as an argument, use it directly. Otherwise ask which service to deploy.

**In the post-merge path, do not ask anything.** When this deploy is the follow-through on a PR
that just merged (`CLAUDE.md` → *After a PR Merges — Pull, Deploy, Verify*), the service is
already determined by the merged diff and the user has already asked. Skip step 2's dry-run
question and go straight to the deploy, from `/home/ubuntu/server` on master rather than from a
worktree. Everything else below — the platform split, the lock, the verification gate — is
unchanged.

**First, determine the platform** — the verification step differs and the Docker one is dead
on the cluster nodes:

- Role under `ansible/roles/k8s/<service>/`, entry has `platform: k8s` in
  `host_vars/daniel-box.yml` → **k3s workload** (this is nearly everything).
- Role under `ansible/roles/containers/<service>/`, entry in `host_vars/daniel-pi.yml` →
  **Docker on the Pi** (docker-proxy, wg-easy, glances, autoheal).

`daniel-server` and `daniel-box` have had **no Docker since 2026-08-14** — never verify a
deploy there with a `docker` command; it doesn't exist on those hosts.

## Checking a k8s change without deploying it

Three modes, and they check genuinely different things — reaching for the wrong one is how a
manifest bug reaches production.

| Mode | What sees the manifests | Catches |
|---|---|---|
| `prek run --all-files` | nothing (renders locally, then parses and schema-checks) | Jinja indent bugs, invalid YAML, duplicate keys, **undefined fields and wrong types** — everything but CRDs, which have no upstream schema |
| `--check` | nothing — the apply is **skipped**, so no API server is involved | task-level wiring; not the manifests themselves |
| `--dry-run` | the **live API server**, via `kubectl apply --dry-run=server` | what prek catches, plus **CRD** schemas, CRD-ordering mistakes and admission rejections |

`--dry-run` renders to a temp dir, applies with `--dry-run=server`, and discards the temp dir.
Nothing is staged, applied, patched or rolled. It runs unlocked, because it mutates nothing. It
does **not** catch scheduling, PVC binding, probe or rollout behaviour — those need a real deploy.

Two limits worth knowing before you trust a green dry run:
- **It refuses the roles named in `k8s_dry_run_unsupported`** — count them with
  `grep -A20 "^k8s_dry_run_unsupported:" ansible/inventory/group_vars/all.yml`; don't
  hand-maintain the number, it read "~17" against a real 15 for two commits. Roles that mutate
  outside `roles/k8s/manifests` (sidecar ConfigMaps built with `kubectl create`, netpol-probe
  Jobs, `exec -i` into a live pod) would half-apply, so `deploy.yml` fails fast and names them.
  `ansible/tests/deploy/test_k8s_dry_run.py` re-derives the list from the role sources so it cannot drift.
- **A brand-new service is only half-checked.** `volume-claim` is skipped (it is a dependency of
  25 roles and mutates), and nothing at admission verifies that a referenced PVC exists — so
  the Deployment validates while the volume is never proven provisionable.

Steps:
1. Confirm the service name matches a role, and note which of the two trees it's in.
2. Ask if they want a dry run first. For a k8s workload that means `--dry-run`, which is the
   only mode that shows the manifests to an API server; `--check` answers a different question
   and is the right choice only when the doubt is about task wiring.
3. If dry run: `./scripts/deploy.sh --tags "<service>" --dry-run` (or `--check`, per step 2)
4. If dry run passes or they skip it: `./scripts/deploy.sh --tags "<service>"`
   (add `-e target=daniel-pi` for a Pi service)

   Deploy through `scripts/deploy.sh`, not `ansible-playbook` directly — it takes
   `/var/lock/server-git-tree.lock`, the same lock gitops-deploy.service and the
   secret-rotate cron use. What that lock guards is the local git tree every deploy reads
   its templates from, which gitops-deploy rewrites with a `git pull` mid-run — so a Pi
   deploy takes it too, even though the writes land on the Pi. `--check` runs unlocked.
   Exit 75 means the lock stayed busy for 25 minutes and **nothing was deployed** — that is
   not a playbook failure.
5. **Verify it actually came up healthy** — Ansible reporting `ok`/`changed` only means the
   playbook ran, not that the workload is up (it can apply cleanly then crash-loop or fail
   its probes).
   - **k3s:** `uv run python scripts/diagnostics/probe.py health <service>` is the primary check —
     allow-listed, k8s-native. Exit 0 only when the rollout is fully complete (observed
     generation caught up, every replica updated/ready/available) **and** no container
     restarted in the last 180s; an unreadable restart timestamp counts as recent (fails
     closed). That restart window is exactly what `kubectl rollout status` can't see —
     readiness flips a Deployment `Available` before a bad liveness probe starts killing it,
     so a rollout-status check alone can report green on a crashlooping pod.
     - On failure, drill down: `kubectl -n <namespace> rollout status
       deployment/<service> --timeout=120s`, `kubectl -n <namespace> get pods -l
       app=<service>`, `kubectl -n <namespace> describe pod <pod>`, and
       `kubectl -n <namespace> logs <pod> --tail=50`. These verbs are allow-listed and
       read-only, so they run without a prompt.
     - A pod that stays `Running` but never becomes ready is a **probe** failure, not a
       deploy failure — read `describe`'s Events.
     - If a ConfigMap/Secret change appears not to have taken effect, check whether the
       Deployment carries a `checksum/config` pod annotation; without it the pod isn't
       rolled. Also note `kubectl apply` leaves **stale Secret keys** behind — a key removed
       from the manifest persists live until patched out.
   - **Docker (Pi only):** `uv run python scripts/diagnostics/probe.py health <service> --docker` —
     exit 0 = running + healthy; allow-listed. `--docker` inspects the **local** Docker
     daemon, and the Pi's is remote, so run it over ssh (`ssh daniel-pi ...`) or verify via
     the Pi's Uptime Kuma monitor instead.
   - For a config-only run (`--skip-tags deploy`), the workload isn't recreated, so this is
     just a liveness check, not a deploy verification.
6. Report the result, including the verification line. If the gate fails, surface the failing
   probe/event and pull recent logs (`kubectl logs`, or
   `uv run python scripts/diagnostics/probe.py loki-query '{container="<service>"}'`) before declaring
   success.

Run all commands from `/home/ubuntu/server`. Always go through `uv run` — bare
`ansible-playbook` (the uv-tool shim) lacks the module deps and fails. For a service on the
Pi, add `-e target=daniel-pi` (deploy.yml defaults `hosts:` to the local hostname — `--limit`
alone matches nothing).

## The command reference

The bare `ansible-playbook` forms are what the wrapper runs. They work, but they have neither
the lock, the tag check, nor the staleness check — use one only when you deliberately want
that.

```bash
# Deploy a specific service
./scripts/deploy.sh --tags "<service-name>"

# Target the Pi. NB `-e target=`, NOT `--limit` — the play's hosts: defaults to the local
# hostname, so --limit daniel-pi matches zero hosts. The Pi is ansible_connection=ssh, so
# this reaches it from either node. `-e target=` a LOCAL-connection host (either cluster
# node) and the tasks run on the machine you typed it on — see ansible/inventory/hosts.ini.
uv run ansible-playbook ansible/deploy.yml --tags "<service-name>" -e target=daniel-pi

# Deploy everything
uv run ansible-playbook ansible/deploy.yml

# Check mode (task wiring only — the apply is skipped, no API server is involved)
uv run ansible-playbook ansible/deploy.yml --tags "<service-name>" --check

# Validate the k8s manifests against the live API server without applying them
./scripts/deploy.sh --tags "<service-name>" --dry-run

# Config-only: render dirs/templates/host config WITHOUT touching the container.
# Every container-role task is block-tagged config/deploy/cron, and tags UNION in Ansible,
# so scope with --skip-tags. `--skip-tags config` is NOT supported — the registered
# config-change facts feed docker_deploy's recreate decision.
uv run ansible-playbook ansible/deploy.yml --tags "<service-name>" --skip-tags deploy

# Edit encrypted secrets
sops ansible/vars/secrets.yml

# List the services --dry-run refuses to cover
grep -A20 "^k8s_dry_run_unsupported:" ansible/inventory/group_vars/all.yml

# Trigger a GitOps tick now instead of waiting for the 30-min timer (daniel-box only).
# Runs the identical code path the timer runs — there is no dry-run mode.
./scripts/deploy_tools/gitops_tick.sh

# Initial server setup. The first-host bring-up ORDER (uv -> SOPS onboarding -> this) is in
# ansible/README.md
uv run ansible-playbook ansible/initial_setup.yml
```

## Why `deploy.sh` rather than the playbook

It takes `/var/lock/server-git-tree.lock` — the same lock `gitops-deploy.service` (30-min
timer) and the weekly secret-rotate cron hold — so a deploy cannot interleave with the
automated pipeline or with another Claude session. The lock guards the local git tree every
deploy reads its templates from, which gitops-deploy rewrites with a `git pull` mid-run, so a
`-e target=daniel-pi` deploy takes it too.

Its four non-zero exits all mean **nothing was deployed**, and each is a resume point rather
than a failure:

| Exit | Meaning | What to do |
|---|---|---|
| 75 | the lock stayed busy (the timer, or another session) | retry |
| 4 | the tree is behind `origin/master` | `git pull`, never `--skip-staleness-check` |
| 3 | the change is broad and maps to no single service | deploy by hand, or see *When to wait* |
| 2 | a `--tags` value matched no service | `--list-services` prints every valid value |

Exit 4 exists because a stale tree renders stale templates and reverts live config while every
repo-side check still reads green (`scripts/deploy_tools/deploy_staleness.py`). It runs ahead
of `--check` and `--dry-run` too, since a green dry run against a stale tree is itself the
misleading signal. Being *ahead* of master is normal branch work and is never refused.

Exit 2 exists because Ansible itself exits 0 on an unmatched tag, so the wrapper checks tags
against `containers_list` first (`scripts/deploy_tools/deploy_tags.py`).
`--skip-tag-check` bypasses it.
