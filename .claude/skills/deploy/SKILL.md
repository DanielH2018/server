---
name: deploy
description: Deploy a service using Ansible — k3s workloads (the default) or the Pi's Docker services. Use when the user wants to deploy or redeploy a specific service.
allowed-tools: Bash, Glob
---

Deploy a service using Ansible.

If the user provided a service name as an argument, use it directly. Otherwise ask which service to deploy.

**First, determine the platform** — the verification step differs and the Docker one is dead
on the cluster nodes:

- Role under `ansible/roles/k8s/<service>/`, entry has `platform: k8s` in
  `host_vars/daniel-box.yml` → **k3s workload** (this is nearly everything).
- Role under `ansible/roles/containers/<service>/`, entry in `host_vars/daniel-pi.yml` →
  **Docker on the Pi** (docker-proxy, wg-easy, glances, dozzle, autoheal).

`daniel-server` and `daniel-box` have had **no Docker since 2026-08-14** — never verify a
deploy there with a `docker` command; it doesn't exist on those hosts.

Steps:
1. Confirm the service name matches a role, and note which of the two trees it's in.
2. Ask if they want a dry run first (`--check` mode)
3. If dry run: `./scripts/deploy.sh --tags "<service>" --check`
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
   - **k3s:** `uv run python scripts/probe.py health <service>` is the primary check —
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
   - **Docker (Pi only):** `uv run python scripts/probe.py health <service> --docker` —
     exit 0 = running + healthy; allow-listed. `--docker` inspects the **local** Docker
     daemon, and the Pi's is remote, so run it over ssh (`ssh daniel-pi ...`) or verify via
     the Pi's Uptime Kuma monitor instead.
   - For a config-only run (`--skip-tags deploy`), the workload isn't recreated, so this is
     just a liveness check, not a deploy verification.
6. Report the result, including the verification line. If the gate fails, surface the failing
   probe/event and pull recent logs (`kubectl logs`, or
   `uv run python scripts/probe.py loki-query '{container="<service>"}'`) before declaring
   success.

Run all commands from `/home/ubuntu/server`. Always go through `uv run` — bare
`ansible-playbook` (the uv-tool shim) lacks the module deps and fails. For a service on the
Pi, add `-e target=daniel-pi` (deploy.yml defaults `hosts:` to the local hostname — `--limit`
alone matches nothing).
