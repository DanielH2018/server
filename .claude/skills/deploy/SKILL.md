---
name: deploy
description: Apply a change through the path that owns it — `land.sh`, `promote` and Flux for a k3s workload, a host package for anything under `hosts/` and the Pi, and a hand-run playbook for a bring-up step. No script deploys a workload by hand. Use when the user wants to deploy or redeploy a service, asks for `deploy.sh`, or to check a k8s manifest change before it merges (`prek` vs the renderer vs the render-diff gates).
allowed-tools: Bash, Glob
---

Apply a change through the path that owns it.

**No script deploys a workload by hand.** Every `cluster/services.yml` entry is `platform: k8s`,
which Flux applies. H7.53 deleted `scripts/deploy.sh` with the GitOps deployer.

**First, determine what the change touches:**

- Role under `ansible/roles/k8s/<service>/`, an inventory value or a shared template → **k3s
  workload** (this is nearly everything). It lands through `land.sh`, `promote` and Flux.
- Role under `ansible/roles/setup/<name>/` → **setup role**. Nothing applies it on its own; see
  *Applying a setup role*.
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
| `uv run python scripts/validate/render_diff_gates.py --base origin/master`, which CI's `pr_gates` job runs, and CI's flux round trip job | nothing — the gates compare commits, so **commit first**: an uncommitted edit is invisible to them | a removed object with no `Removes-Object:` line, and a render `flux build` cannot build |
| `takeover-check` on daniel-box (operator's account) | the **live API server**, through a server-side dry run | CRD schemas, admission rejections and field conflicts, for the tags it names |

No check here covers scheduling, PVC binding, probe or rollout behaviour. Those show only after
Flux applies the tag.

## Applying a k3s workload

1. Confirm the service name matches a role and a `containers_list` entry.
2. Run `prek` and the renderer, commit, run the render-diff gates, then open the PR.
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

Nothing applies a setup role on its own: every role a host still runs is emptied or moving into
a host package (H7), and `land.sh` ends a setup-only PR `nothing-to-deploy`. A change that has to
reach a host belongs in the component that replaced the role.

A playbook goes through `uv run`, because bare `ansible-playbook` (the uv-tool shim) lacks the
module deps and fails. A setup play aimed at the Pi takes `-e target=daniel-pi`, because the
playbooks default `hosts:` to the local hostname and `--limit` alone matches nothing.

To apply one setup role by hand, read the playbook and tag from the `Applied by:` line of the
role's `CLAUDE.md`. Neither follows from the role's name: `chezmoi_setup` takes the tag `chezmoi`,
and `k3s` applies through `k3s-bringup.yml`, where the same page names narrower tags than the
maximal `k3s`. Ansible exits 0 on a tag that matches nothing, so a wrong guess reads as a green
recap.

```bash
# Apply one setup role by hand, with the playbook and tag its Applied by: line names
uv run ansible-playbook ansible/<playbook> --tags <tag>

# Initial server setup. The first-host bring-up ORDER (uv -> SOPS onboarding -> this) is in
# ansible/README.md
uv run ansible-playbook ansible/initial_setup.yml

# Edit encrypted secrets
sops ansible/vars/secrets.yml
```
