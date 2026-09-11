# k3s control-plane upgrade runbook

A k3s bump reinstalls the control plane on a single-server cluster, so it runs by hand. Renovate
raises the PR and the `k3s control plane (manual — plan the upgrade, mind Longhorn's version skips)`
rule blocks its automerge, which means the daily unattended `/renovate-prs` run can never land it.
This page is what an operator follows instead.

The pin is `k3s_version` in `ansible/roles/setup/k3s/defaults/main.yml`. It covers both nodes:
`server.yml` installs daniel-box, `agent.yml` installs daniel-server, and both pass it to the same
`get.k3s.io` installer as `INSTALL_K3S_VERSION`.

## Sequence at a glance

Upgrade the cluster first, merge the pin second. That order is forced — see *Merging first parks the
GitOps deployer* below.

1. Pass the gates.
2. Record pre-upgrade state.
3. Upgrade the server node, verify.
4. Upgrade the agent node, verify.
5. Merge the pin, then fast-forward the primary checkout so the deployer unparks.

## The gates — before any install

Each one is a stop condition, not a checklist item.

```bash
# 1. No degraded Longhorn volume. A degraded volume plus a node restart is how the last
#    good replica goes. `detached`/`unknown` is normal for an idle volume; `degraded` is not.
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns='NAME:.metadata.name,STATE:.status.state,ROBUST:.status.robustness'

# 2. No Longhorn backup mid-flight — a restart aborts it and the retry storm follows.
kubectl -n longhorn-system get backups.longhorn.io \
  -o jsonpath='{range .items[*]}{.status.state}{"\n"}{end}' | sort | uniq -c

# 3. The GitOps deployer holds no SHA. A non-empty hold means a previous deploy already
#    failed its health gate, and this upgrade would land on top of that.
ls /var/lib/gitops-deploy/hold_sha

# 4. Both nodes Ready.
kubectl get nodes -o wide
```

## Read the release notes for what this cluster actually runs

k3s release notes lead with warnings about bundled components. Three of them are disabled here
(`--disable=traefik --disable=servicelb --disable=local-storage`, `k3s_server_args`), so a warning
about the bundled Traefik chart, klipper-lb or local-path does not apply. Traefik is a repo-owned
role, and its chart version has nothing to do with the k3s pin.

What does apply: the Kubernetes minor itself, containerd, runc, flannel, CoreDNS and etcd. A patch
bump moves those within a minor. A **minor** bump additionally needs Longhorn's support matrix
checked, and Longhorn upgraded on its own ladder first — that is `docs/longhorn-upgrade.md`, a
separate procedure with a separate pin (`k3s_longhorn_version`).

## Record pre-upgrade state

The upgrade is verified by diff, so capture the "before" while it still exists.

```bash
kubectl get nodes -o wide
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns='NAME:.metadata.name,STATE:.status.state,ROBUST:.status.robustness'
kubectl get svc -A --field-selector spec.type=LoadBalancer -o wide
uv run python scripts/diagnostics/probe.py monitors
```

## Upgrade the server node

Bump `k3s_version`, then run on daniel-box (the play refuses to run anywhere else):

```bash
uv run ansible-playbook ansible/k3s-bringup.yml --tags k3s_server
```

The installer rewrites the unit, daemon-reloads and restarts k3s itself; no handler is involved.
The apiserver, Traefik's ingress path and Pi-hole DNS all go away for the length of that restart,
because they all sit behind this node.

The install task is guarded on two clauses ORed together: the unit's arguments, and the installed
version. The version clause exists because a bump leaves every argument identical — without it the
guard reads false, the installer never runs, and the play reports `ok` while the node stays put.

Verify before touching the agent:

```bash
kubectl get nodes -o wide                    # daniel-box on the new version, Ready
kubectl -n kube-system get pods              # CoreDNS and metrics-server Running
uv run python scripts/diagnostics/probe.py targets
dig +short @10.0.0.243 <a-known-name>        # Pi-hole answering on its MetalLB VIP
```

## Upgrade the agent node

```bash
uv run ansible-playbook ansible/k3s-bringup.yml -e join_agent=daniel-server --tags k3s_agent
```

**Then confirm the version moved on daniel-server specifically.** `hosts.ini` pins both nodes
`connection=local`, so a play aimed at the wrong host installs on the one you ran from behind a
green recap. `kubectl get nodes -o wide` is the only evidence that settles it.

Restarting the agent is ungraceful from Longhorn's point of view: `kubectl drain` and `cordon` are
write verbs, and the ServiceAccount available here is read-only, so there is no drain step to run.
Every replica on daniel-server detaches and reattaches. That is the single riskiest moment in this
procedure, and the reason gate 1 exists.

## Verify the change, not just the workload

A Ready node proves the kubelet came back. It does not prove the storage under it survived.

```bash
# Every volume back to attached/healthy, with the count matching the pre-upgrade capture.
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns='NAME:.metadata.name,STATE:.status.state,ROBUST:.status.robustness'

# No filesystem went read-only. An iSCSI keepalive stall during the restart surfaces as a
# read-only ext4 mount inside a pod that still reads Running, and a read-only remount does
# not clear on a rollout restart — it needs a scale-to-0 and a detach.
uv run python scripts/diagnostics/probe.py monitors

# The MetalLB VIPs, tested from a host that is NOT the announcer. ETP-Local VIPs have
# blacked out twice on a node rejoin with every host-local probe green.
```

## Merging first parks the GitOps deployer

`ansible/roles/setup/` is in `_BROAD_SETUP_PREFIXES` (`deploy_changes.py`), so the deployer treats a
pin change as a broad setup-plane change and looks for an `initial_setup.yml` tag to apply it with.
The `k3s` role is not in `initial_setup.yml` — it lives in `k3s-bringup.yml` — so `setup_tags_for`
resolves no tag, `handle_broad` parks, and the tick does **not** fast-forward. Every other session's
deploy then fails `deploy.sh` exit 4 until an operator clears it. This happened on 2026-09-09
(issue #1467) for this exact path.

So merge last, and clear the park immediately afterwards by fast-forwarding the primary checkout:

```bash
git -C /home/ubuntu/server pull --ff-only
```

With the cluster already on the new version, the hand-apply the alert asks for is a verification
run rather than a change.

## Rollback

Re-pin to the previous version and re-run the same two commands. The installer's version guard makes
a downgrade work exactly like an upgrade.

This holds **within a minor version**. Across a minor it does not: Kubernetes does not support
downgrading the control plane, and a stored-object schema written by the newer apiserver may not be
readable by the older one. For a minor bump the recovery path is `docs/k3s-etcd-restore.md`, not a
re-pin.

k3s takes automatic etcd snapshots (the cluster runs with `--cluster-init`). Verify one exists before
a minor bump rather than assuming it: `sudo k3s etcd-snapshot ls`, which needs an operator shell —
`sudo` is denied to Claude sessions.

## What this procedure does not serialize

`k3s-bringup.yml` does not take `/var/lock/server-git-tree.lock`; only `deploy.sh` does. Another
session's deploy can therefore run straight through the control-plane restart. Check the SessionStart
banner for live sessions before starting, and prefer a quiet window.
