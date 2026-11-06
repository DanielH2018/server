---
name: deploy
description: Apply a change through the path that owns it — `land.sh`, `promote` and Flux for a k3s workload, the GitOps deployer or `initial_setup.yml` for a setup role, the host package for the Pi. `scripts/deploy.sh` deploys none of these. Use when the user wants to deploy or redeploy a service, when `deploy.sh` exits 5, or to check a k8s manifest change before it merges (`prek` vs the renderer vs the render-diff gates).
allowed-tools: Bash, Glob
---

Apply a change through the path that owns it.

**`scripts/deploy.sh` deploys no workload.** Every `containers_list` entry is `platform: k8s`
with `owner: flux`, and daniel-pi's and daniel-server's lists are empty. *What `deploy.sh` still
does* below has the rest.

**First, determine what the change touches:**

- Role under `ansible/roles/k8s/<service>/`, an inventory value or a shared template → **k3s
  workload** (this is nearly everything). It lands through `land.sh`, `promote` and Flux.
- Role under `ansible/roles/setup/<name>/` → **setup role**. The GitOps deployer applies it.
- Entry in `compose_services` in `host_vars/daniel-pi.yml`, or anything under `hosts/` → **host
  package**. For the Pi's docker-proxy, wg-easy, alloy and autoheal, the `homelab-daniel-pi`
  package renders them from `hosts/components/pi-compose/services/<name>/`, and the Pi's updater
  installs a merged change (`docs/host-packages.md`).

`daniel-server` and `daniel-box` have had **no Docker since 2026-08-14** — never verify a
deploy there with a `docker` command; it doesn't exist on those hosts.

## Checking a k8s change before it merges

These check genuinely different things, and reaching for the wrong one is how a manifest bug
reaches production.

| Check | What sees the manifests | Catches |
|---|---|---|
| `prek run --all-files` | nothing (renders locally, then parses and schema-checks) | Jinja indent bugs, invalid YAML, duplicate keys, **undefined fields and wrong types** — everything but CRDs, which have no upstream schema |
| `uv run python scripts/renderer/homelab_render.py cluster --out <dir> --role <tag>` | nothing — it reads the git tree alone | what Flux would receive for the tag, the secret rules and the renderer's refusals; read the files under `<dir>/cluster/<tag>/` |
| `uv run python scripts/validate/render_diff_gates.py --base origin/master`, which CI's `pr_gates` job runs, and CI's flux round trip job | nothing | a removed object with no `Removes-Object:` line, and a render `flux build` cannot build |
| `takeover-check` on daniel-box (operator's account) | the **live API server**, through a server-side dry run | CRD schemas, admission rejections and field conflicts, for the tags it names |

`deploy.sh --check` and `--dry-run` are not on this list. Neither runs a role, so neither shows a
manifest to anything, and a k8s tag in `--tags` exits 5 under both.

No check here covers scheduling, PVC binding, probe or rollout behaviour. Those show only after
Flux applies the tag.

## Applying a k3s workload

1. Confirm the service name matches a role and a `containers_list` entry.
2. Run the checks above, then commit and open the PR.
3. Land it with the `land-after-merge` skill: `land.sh` merges, waits for master CI, waits on the
   `promote` run that applies the merge commit, and gates the health.
4. **Verify it actually came up healthy.** Flux reporting `Ready` only means the apply finished,
   not that the workload is up (it can apply cleanly then crash-loop or fail its probes).
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
       rolled. For **stale Secret keys**, see `.claude/rules/k8s-templates.md`, *Removing a
       key from a Secret template*.
   - **Docker (Pi only), after the package installs:** `uv run python
     scripts/diagnostics/probe.py health <service> --docker` — exit 0 = running + healthy;
     allow-listed. Run it from daniel-box as written: `--docker` reaches the Pi's Docker daemon
     over ssh itself.
5. Report the result, including the verification line. If the gate fails, surface the failing
   probe/event and pull recent logs (`kubectl logs`, or
   `uv run python scripts/diagnostics/probe.py loki-query '{container="<service>"}'`) before declaring
   success.

To apply a green master commit that no promotion followed, dispatch `promote` with the commit's
full 40-hex SHA. `promote` refuses a commit without green CI:

```bash
gh workflow run promote.yml -R DanielH2018/server --ref master -f sha=<sha>
```

## Applying a setup role

The GitOps deployer applies a setup role on the tick after the merge, and the `gitops-tick` skill
triggers a tick by hand. A role the tick cannot apply becomes a `manual_plane` line in `owed.jsonl`.

A playbook goes through `uv run`, because bare `ansible-playbook` (the uv-tool shim) lacks the
module deps and fails. A setup play aimed at the Pi takes `-e target=daniel-pi`, because the
playbooks default `hosts:` to the local hostname and `--limit` alone matches nothing.

```bash
# Clear the owed line your own merged PR created
uv run python scripts/deploy_tools/owed_run.py <role>

# Apply one setup role by hand
uv run ansible-playbook ansible/initial_setup.yml --tags <role>

# Trigger a GitOps tick now instead of waiting for the 10-min timer (daniel-box only).
# Runs the identical code path the timer runs — there is no dry-run mode.
./scripts/deploy_tools/gitops_tick.sh

# Initial server setup. The first-host bring-up ORDER (uv -> SOPS onboarding -> this) is in
# ansible/README.md
uv run ansible-playbook ansible/initial_setup.yml

# Edit encrypted secrets
sops ansible/vars/secrets.yml
```

## What `deploy.sh` still does

The wrapper runs `ansible/deploy.yml` and no other playbook, and that playbook runs no role.
Run it from the root of the checkout you mean to act on, because it reads the checkout containing
your working directory. Its arguments other than its own flags pass through to `ansible-playbook`.
A bare `uv run ansible-playbook ansible/deploy.yml` has none of the locks, the snapshot, the tag
check or the staleness check.

```bash
# What a branch owes by hand. A broad range exits 3 and prints the setup or bring-up command.
# Any other range derives no tag and ends with the verdict nothing-to-deploy.
./scripts/deploy.sh --changed

# Every valid tag, with each k8s tag named on stderr
./scripts/deploy.sh --list-services

# The locks, the snapshot and the k8s_retired_services teardown, and no role.
# It refuses any commit behind origin/master.
./scripts/deploy.sh
```

- **The other flags still parse, and none of them reaches a role.** `--at <sha>` cuts the snapshot
  from that commit, and `--at` with `--changed` is a bad command line. `--detach` backgrounds the
  playbook and prints a `wait:` line that waits for its health gate; exit 75 from that line means
  the run is still going, so re-run only the wait. `--check` and `--dry-run` run unlocked, from
  the working tree. `-e target=daniel-pi` runs the secrets preamble against the Pi and deploys
  nothing.
- **A k8s tag is never yours to deploy.** When its `containers_list` entry is `platform: k8s`, a
  `--tags` list naming it exits 5 as a whole, and no flag bypasses that. Its change lands through
  `promote` once it merges. `--changed` leaves such a tag out and names it, and a shared role's
  name is a tag miss.
- **A commit behind origin/master refuses as stale.** `--tags` narrows the question to those tags,
  so a tail that touches only other roles prints one line and proceeds, while a broad path
  (shared templates, `ansible/inventory/`, the setup plane) still refuses. Under `--at <sha>` the
  question is what `<sha>..origin/master` carries.

[docs/deploying.md](../../../docs/deploying.md#the-gates-and-locks-of-a-run) has the locks and the
snapshot, including
[the tree lock's holders](../../../docs/deploying.md#who-holds-the-tree-lock).

## When a run fails, read its last two lines

`deploy.sh` prints what its exit code means on every non-zero exit, as its last two lines; act
on those rather than on the number. What the codes mean as a group is
[docs/deploying.md's *Exit codes are resume points*](../../../docs/deploying.md#exit-codes-are-resume-points),
and a fact an operator needs at a failed run belongs in `CONTRACTS` in `scripts/lib/exit_codes.py`,
not in this skill: a copy here drifted once already (#2853).
