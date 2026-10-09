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
  **Docker on the Pi** (docker-proxy, wg-easy, alloy, autoheal).

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
- **A role's own cluster writes are skipped, not exercised.** Sidecar ConfigMaps built with
  `kubectl create`, netpol-probe Jobs and `exec -i` into a live pod all sit outside
  `roles/k8s/manifests` and are guarded on `k8s_no_mutate`, so a dry run proves the manifests
  and not those. `ansible/tests/deploy/test_k8s_dry_run.py` refuses a role that grows such a
  write without the guard.
- **A brand-new service is only half-checked.** Its `k8s_claims` are applied with
  `--dry-run=server`, which validates the claim object and provisions nothing, and nothing at
  admission verifies that a referenced PVC exists. So the Deployment validates while the volume
  is never proven provisionable.

Steps:
1. Confirm the service name matches a role, and note which of the two trees it's in.
2. Ask if they want a dry run first. For a k8s workload that means `--dry-run`, which is the
   only mode that shows the manifests to an API server; `--check` answers a different question
   and is the right choice only when the doubt is about task wiring.
3. If dry run: `./scripts/deploy.sh --tags "<service>" --dry-run` (or `--check`, per step 2)
4. If dry run passes or they skip it: `./scripts/deploy.sh --tags "<service>"`
   (add `-e target=daniel-pi` for a Pi service)

   Deploy through `scripts/deploy.sh`, not `ansible-playbook` directly — it takes
   the git-tree lock (its holders are listed in
   [docs/deploying.md](../../../docs/deploying.md#who-holds-the-tree-lock)) long enough to
   copy `HEAD` into a snapshot worktree, then runs
   the playbook from that snapshot under `/var/lock/server-deploy-<tag>.lock`, one per
   service. So deploys of the same service serialize and deploys of different services do
   not. A Pi deploy takes both too, even though the writes land on the Pi. `--check` runs
   unlocked, from the working tree. **The snapshot is of `HEAD`: an uncommitted edit is not
   deployed.** On a non-zero exit, read the run's last two lines
   ([below](#when-a-run-fails-read-its-last-two-lines)).
5. **Verify it actually came up healthy** — Ansible reporting `ok`/`changed` only means the
   playbook ran, not that the workload is up (it can apply cleanly then crash-loop or fail
   its probes).
   - **k3s:** `uv run python scripts/diagnostics/probe.py health <service>` is the primary check —
     allow-listed, k8s-native. Exit 0 only when the rollout is fully complete (observed
     generation caught up, every replica updated/ready/available) **and** no container
     restarted in the last 180s; an unreadable restart timestamp counts as recent (fails
     closed). That restart window is exactly what `kubectl rollout status` can't see —
     readiness flips a Deployment `Available` before a bad liveness probe starts killing it,
     so a rollout-status check alone can report green on a crashlooping pod. A third half
     fails a workload the release record says this apply queued a restart of when its
     `restartedAt` is not newer than the apply (`NOT ROLLED`) — a deploy that changed the
     manifests and rolled nothing no longer reads green (#1867).
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

The bare `ansible-playbook` forms are what the wrapper runs. They work, but they have none of
the locks, the snapshot, the tag check or the staleness check — use one only when you
deliberately want that.

```bash
# Deploy a specific service
./scripts/deploy.sh --tags "<service-name>"

# Apply a change to a shared k8s role (manifests, volume-snapshot, image-builder, ...).
# It has no containers_list entry, so deploy.sh replaces the name with every service
# that includes it and prints the list. manifests and volume-snapshot reach ~58 services.
./scripts/deploy.sh --tags "<shared-role>"

# Target the Pi. NB `-e target=`, NOT `--limit` — the play's hosts: defaults to the local
# hostname, so --limit daniel-pi matches zero hosts. The Pi is ansible_connection=ssh, so
# this reaches it from either node. `-e target=` a LOCAL-connection host (either cluster
# node) and the tasks run on the machine you typed it on — see ansible/inventory/hosts.ini.
uv run ansible-playbook ansible/deploy.yml --tags "<service-name>" -e target=daniel-pi

# Deploy a commit that is not this checkout's HEAD: the snapshot is cut from <sha>, and the
# staleness gate and the tag check are asked about <sha> too. This is how land.sh deploys a
# PR's merge commit without waiting for the tick to fast-forward the primary checkout.
# Any committish this checkout's object store resolves; --at with --changed is a bad command line.
./scripts/deploy.sh --tags "<service-name>" --at <sha>

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

# Trigger a GitOps tick now instead of waiting for the 10-min timer (daniel-box only).
# Runs the identical code path the timer runs — there is no dry-run mode.
./scripts/deploy_tools/gitops_tick.sh

# Initial server setup. The first-host bring-up ORDER (uv -> SOPS onboarding -> this) is in
# ansible/README.md
uv run ansible-playbook ansible/initial_setup.yml
```

## Why `deploy.sh` rather than the playbook

It takes two kinds of lock ([ADR-0017](../../../docs/adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md)).
The git-tree lock — the lock every job in
[docs/deploying.md's holder list](../../../docs/deploying.md#who-holds-the-tree-lock)
takes — guards the local git tree every deploy renders from,
which gitops-deploy rewrites with a `git pull` mid-run; `deploy.sh` holds it only to copy
`HEAD` into a detached worktree under `/tmp/homelab-deploy-snapshots/`. It then releases that
and holds one `/var/lock/server-deploy-<tag>.lock` per service across the playbook, which is
what stops two deploys of the same service racing. A `-e target=daniel-pi` deploy takes both.

Because the snapshot is of `HEAD`, **an uncommitted edit is not deployed** — commit first.

## When a run fails, read its last two lines

`deploy.sh` prints what its exit code means on every non-zero exit, as its last two lines; act
on those rather than on the number. What the codes mean as a group is
[docs/deploying.md's *Exit codes are resume points*](../../../docs/deploying.md#exit-codes-are-resume-points),
and a fact an operator needs at a failed run belongs in `CONTRACTS` in `scripts/lib/exit_codes.py`,
not in this skill: a copy here drifted once already (#2853).

## What `--tags`, `--at` and `--detach` change

**With `--tags`, only a commit reaching those tags makes the tree stale.** The staleness gate
classifies every path in `HEAD..origin/master` with the deployer's own mapper. A path reaching
one of the tags refuses, and so does any broad path (shared templates, `ansible/inventory/`,
the setup plane). The refusal names the commits and paths responsible. A tail that touches
only other roles prints one line saying how far behind the tree is, and the deploy proceeds.
Without `--tags` the deploy is unscoped, so any commit behind refuses. The narrowing matters
because the GitOps deployer fast-forwards to the newest GREEN commit in its range rather than
to the tip, so the primary checkout is legitimately behind a pending tip while every landing
deploys from it.

**With `--at <sha>`, the question is about `<sha>`, not about this checkout.** The run renders
a snapshot of `<sha>`, so the staleness gate asks what `<sha>..origin/master` carries and the
tag check reads `containers_list` at `<sha>`. A checkout behind master therefore deploys a
current commit, while a `<sha>` that is itself behind on the requested tags still refuses as
stale. An `--at` with no value is a bad command line rather than a fall-back to `HEAD`, so an
`--at "$sha"` whose variable came back empty deploys nothing.

With `--detach`, the completion notifier's health gate renders that same snapshot: it is kept
until the notifier has run. The gate enumerates the workloads to check from the manifests of
the commit that was deployed, never from the working tree, which under `--at` is a different
commit and even without it can carry uncommitted edits.

The `--detach` playbook run is a grandchild in its own session, outside the caller's process
tree, so a Bash call killed at its time limit does not kill it. Started from a systemd user
unit, it also moves into its own `deploy-<pid>.scope`, so stopping that unit does not kill it.
The deploy log names the scope (`lib/detach_fork.py`).
