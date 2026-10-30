# k3s control-plane upgrade runbook

A k3s bump reinstalls the control plane on a single-server cluster, so it runs by hand. Renovate
raises the PR and the `k3s control plane (manual — hand-run upgrade: docs/k3s-upgrade.md; k8s_autodeploy: false)`
rule blocks its automerge, which means the daily unattended `/renovate-prs` run can never land it.
This page is what an operator follows instead.

Renovate raises k3s patch releases only. An `allowedVersions` cap on `k3s-io/k3s` holds k3s at
the minor whose Kubernetes schemas are vendored under `scripts/validate/schemas/kubernetes.io/`,
because `test_vendored_core_schemas_match_the_cluster` fails a k3s pin that names a different
Kubernetes tag from the one the schemas came from (#2367). The schemas are Kubernetes' own
OpenAPI files, so they exist the day the release does. A minor upgrade is one PR that moves
`k3s_version`, `k3s_install_script_sha256`, `K8S_SCHEMA_VERSION` and the cap together, then runs
`uv run python scripts/validate/refresh_vendored_schemas.py` to vendor the new tag's schemas,
then follows this page before merging. A patch bump needs the refresh too, because the test
compares the full tag. `test_renovate_k3s_cap_follows_the_schema` holds the cap one minor above
the schema.

The pin is `k3s_version` in `ansible/roles/k8s/system-upgrade/defaults/main.yml`, moved there from
`ansible/roles/setup/k3s/defaults/main.yml` by H6.1b. The `setup/k3s` tasks read it with `include_vars`.
It covers both nodes.
The system-upgrade-controller (SUC) writes both nodes' versions. Its `k3s-server` Plan selects
daniel-box since H6.3c, and its `k3s-agent` Plan selects daniel-server since H6.3d. Each Plan
replaces its node's binary. `server.yml` and `agent.yml` install k3s on a fresh host only. The installer is `install.sh` from that same release tag,
fetched with `checksum:` against `k3s_install_script_sha256` in the same file — so a bump has a
second literal to finish, and a wrong one fails the download rather than the install:

```bash
curl -fsSL "https://raw.githubusercontent.com/k3s-io/k3s/<new tag>/install.sh" | sha256sum
```

## Sequence at a glance

Merging the pin upgrades both nodes through SUC, so the merge comes before either verification.
An agent newer than its server is unsupported. The `k3s-agent` Plan's `prepare` therefore runs in
`agent` mode, which waits for the `k3s-server` Plan before it lets daniel-server upgrade.
Merging does not park the deployer: see *After merging the pin* below.

1. Pass the gates.
2. Record pre-upgrade state.
3. Merge the pin. SUC upgrades daniel-box, then daniel-server. The pin writes no `manual_plane`
   line (H6.1b), so only a change elsewhere under `ansible/roles/setup/k3s/` leaves one to clear.
4. Verify the server by its node label and version.
5. Verify the agent by its node label and version.

## The gates — before any install

Each one is a stop condition, not a checklist item. They run as one script, in order, and the
exit code names the first gate that refused — so a skipped gate is impossible and a failed one
is a number rather than a paragraph (#2162):

```bash
uv run python scripts/deploy_tools/runbook_gates.py k3s-upgrade
```

Exit 0 means all four passed. Exit 1–4 is the gate that failed, and the script prints what it
found. Exit 69 means the cluster could not be asked: no readable kubeconfig, or a kubectl that
serves the staging cluster (`scripts/lib/kubectl.py` refuses that rather than grading it). It
also means a deployer marker exists and could not be read, which is not a hold.

1. **No degraded Longhorn volume.** A degraded volume plus a node restart is how the last good
   replica goes. `detached`/`unknown` is normal for an idle volume; `degraded` (or `faulted`)
   is not.
2. **No Longhorn backup mid-flight.** A restart aborts it and the retry storm follows.
3. **The GitOps deployer holds no SHA.** A non-empty `hold_sha` means a previous deploy already
   failed its health gate, and this upgrade would land on top of that. The marker lives on
   daniel-box, so this gate fails on any other host rather than reading an absent directory as
   a cleared hold.
4. **Both nodes Ready.**

## Read the release notes for what this cluster actually runs

k3s release notes lead with warnings about bundled components. Three of them are disabled here
(the `disable` list in `k3s_server_config`, `hosts/components/k3s-server/component.yml`), so a warning
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
systemctl show k3s -p ExecMainStartTimestamp
ssh daniel-server 'systemctl show k3s-agent -p ExecMainStartTimestamp; sha256sum /usr/local/bin/k3s'
```

## Before the Plan first selects a node (**operator-only**)

SUC's `prepare` container reads the API as the `k3s-plan` ServiceAccount. Impersonating it needs
more than the read-only account holds, so no agent runs these. Each must print `yes`.
`hosts/lib/tests/test_k3s_gates_operator_reads.py` derives the list from `k3s_gates.py`'s API
paths and fails when this page omits one.

```bash
cd /home/ubuntu/server && sudo k3s kubectl auth can-i list volumes.longhorn.io --as=system:serviceaccount:system-upgrade:k3s-plan
cd /home/ubuntu/server && sudo k3s kubectl auth can-i list backups.longhorn.io --as=system:serviceaccount:system-upgrade:k3s-plan
cd /home/ubuntu/server && sudo k3s kubectl auth can-i list nodes --as=system:serviceaccount:system-upgrade:k3s-plan
cd /home/ubuntu/server && sudo k3s kubectl auth can-i get deployments.apps/coredns --as=system:serviceaccount:system-upgrade:k3s-plan -n kube-system
cd /home/ubuntu/server && sudo k3s kubectl auth can-i get leases.coordination.k8s.io/kube-controller-manager --as=system:serviceaccount:system-upgrade:k3s-plan -n kube-system
cd /home/ubuntu/server && sudo k3s kubectl auth can-i get plans.upgrade.cattle.io/k3s-server --as=system:serviceaccount:system-upgrade:k3s-plan
```

## Upgrade the server node

SUC upgrades daniel-box. Merging a `k3s_version` bump makes Flux apply the `system-upgrade` tag,
the `k3s-server` Plan resolves the new version, its `prepare` container waits for the gates, and
the upgrade container replaces the binary. No Ansible command is part of the upgrade.
The apiserver, Traefik's ingress path and Pi-hole DNS go away for the length of the restart,
because they all sit behind this node.

Do not run `k3s-bringup.yml --tags k3s_server` to upgrade. Its install guard has no version
clause since H6.3c. A missing binary runs the full install. A missing argument re-runs the
installer with `INSTALL_K3S_SKIP_DOWNLOAD=true`, which rewrites the unit and never the binary.
A flag change therefore still reaches daniel-box through that play until H6.4b.

Widening the Plan is a selector change only, so it keeps the Plan's version and hash. The
Kustomization can read Current before any Job exists. The evidence is the node: its
`plan.upgrade.cattle.io/k3s-server` label equals the Plan's `status.latestHash`, the Job shows a
`completionTime`, and `ExecMainStartTimestamp` for `k3s` did not move.

```bash
kubectl -n system-upgrade get plans -o custom-columns='NAME:.metadata.name,HASH:.status.latestHash,VERSION:.status.latestVersion'
kubectl get nodes -L plan.upgrade.cattle.io/k3s-server
kubectl -n system-upgrade get jobs -o custom-columns=NAME:.metadata.name,DONE:.status.completionTime
systemctl show k3s -p ExecMainStartTimestamp
```

A **minor** bump first passes the guest rehearsal of
`docs/target-architecture/host/h6-k3s-suc.md` (*Rehearsal in a drill guest*, H6.1b2), and its PR
body quotes the result. A patch bump needs no rehearsal.

Verify before touching the agent:

```bash
kubectl get nodes -o wide                    # daniel-box on the new version, Ready
kubectl -n kube-system get pods              # CoreDNS and metrics-server Running
uv run python scripts/diagnostics/probe.py targets
dig +short @10.0.0.243 <a-known-name>        # Pi-hole answering on its MetalLB VIP
```

### Move CoreDNS to the image the new k3s bundles

`k3s_server` does not move the CoreDNS image. The `coredns.yaml.skip` marker stops k3s applying
its bundled CoreDNS add-on, so the Deployment keeps the image it had before the upgrade (#3257).
Flux applies the k8s `coredns` role, and the role's `coredns_image` pin is the only thing that
moves the image. No task on the host reads the staged manifest any more. A k3s bump therefore
carries the CoreDNS port in the same PR as the `k3s_version` move:

1. Read k3s's manifest at the new tag, and diff it against the old tag. The tag's `+` is
   `%2B` in the URL:
   `gh api -H 'Accept: application/vnd.github.raw' 'repos/k3s-io/k3s/contents/manifests/coredns.yaml?ref=<tag>'`.
2. Port each change into `ansible/roles/k8s/coredns/`. The image goes into `coredns_image`,
   and the `Corefile` into `templates/config/coredns-corefile.j2`. Everything else goes into the
   Deployment, Service and RBAC templates, with each `%{...}%` placeholder written as the value
   k3s substitutes on this cluster.
3. Move the excerpt and its label in
   `ansible/tests/setup/test_coredns_image_follows_bundled_manifest.py` to the new tag. The test
   fails until the label equals `k3s_version` and the pin equals the excerpt's image.
4. Read the image the new pod runs for a uid change. The Deployment pins
   `runAsUser: 65532` beside `runAsNonRoot`, so an image whose `USER` moved refuses to start.
5. Merge through `land.sh`, which is the apply. A pod-template change rolls the single replica
   and leaves a cluster-DNS gap, so land it in a window.

```bash
kubectl -n kube-system get deploy coredns -o jsonpath='{.spec.template.spec.containers[0].image}'
uv run python scripts/diagnostics/probe.py health coredns
```

#### Break-glass: a bad CoreDNS revision (**operator-only**)

Flux resolves GHCR through the cluster DNS it owns, so a revision that breaks CoreDNS can block
its own fix. The `coredns` tag is forward-only, so a failed revision holds and `prod` does not
reset into a fetch that needs the DNS that just failed.

1. Annotate the CoreDNS objects in the cluster, so `kustomize-controller` skips them:
   `sudo k3s kubectl -n kube-system annotate deployment/coredns configmap/coredns service/kube-dns kustomize.toolkit.fluxcd.io/reconcile=disabled`.
2. Render the `coredns` tag at the `last-good` SHA, from a checkout of that SHA, with
   `uv run python scripts/renderer/homelab_render.py cluster --out <out> --role coredns`.
   Then apply it:
   `sudo k3s kubectl apply -k <out>/cluster/coredns`.
   `probe.py landing` prints `last-good`.
3. Fix forward through a PR. Wait until the `coredns` Kustomization's
   `status.lastAppliedOriginRevision` names the fix or a descendant of it, then remove the
   annotation from each object with
   `sudo k3s kubectl -n kube-system annotate deployment/coredns configmap/coredns service/kube-dns kustomize.toolkit.fluxcd.io/reconcile-`.

Until step 3, `probe.py landing` lists each object that still carries the annotation.

## Upgrade the agent node

SUC upgrades daniel-server. The `k3s-agent` Plan resolves the same version as the server Plan.
Its `prepare` container runs the gates in `agent` mode, which first waits for the `k3s-server`
Plan to finish, and then the upgrade container replaces the binary. No Ansible command is part
of the upgrade.

Do not run `k3s-bringup.yml -e join_agent=daniel-server --tags k3s_agent` to upgrade. Its
install guard has no version clause since H6.3d. A missing binary runs the full install. A
missing argument re-runs the installer with `INSTALL_K3S_SKIP_DOWNLOAD=true`, which rewrites the
unit and never the binary. A flag change therefore still reaches daniel-server through that play.

The evidence is the node, as for the server: its `plan.upgrade.cattle.io/k3s-agent` label equals
the agent Plan's `status.latestHash`, the Job shows a `completionTime`, and
`kubectl get nodes -o wide` shows daniel-server on the new version. A selector-only change, such
as H6.3d itself, also leaves the agent's `ExecMainStartTimestamp` where it was.

```bash
kubectl -n system-upgrade get plans -o custom-columns='NAME:.metadata.name,HASH:.status.latestHash,VERSION:.status.latestVersion'
kubectl get nodes -L plan.upgrade.cattle.io/k3s-agent
kubectl -n system-upgrade get jobs -o custom-columns=NAME:.metadata.name,DONE:.status.completionTime
kubectl get nodes -o wide
ssh daniel-server 'systemctl show k3s-agent -p ExecMainStartTimestamp'
```

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

## After merging the pin

A pin change no longer reaches the deployer's setup plane. The pin lives in the Flux-owned
`system-upgrade` role (H6.1b), so a bump routes to the `system-upgrade` tag, which `land.sh`
waits on through promotion, and the deployer writes no `manual_plane` line for `k3s`. The
`k3s-server` Plan selects daniel-box since H6.3c and the `k3s-agent` Plan selects daniel-server
since H6.3d, so merging the pin upgrades both nodes through SUC and no hand apply is owed for
either. Another change under
`ansible/roles/setup/k3s/` still takes the setup plane's path. `k3s` is routed to
`k3s-bringup.yml`, not to `initial_setup.yml` (`deploy_setup_roles.py:roles_outside_initial_setup_in`),
so `tick_applies_setup_role` returns false for it. `handle_broad` (`deploy_handlers.py`) then
fast-forwards the primary checkout and writes a `manual_plane` line for `k3s` to
`/var/lib/gitops-deploy/owed.jsonl`. Only the bring-up playbooks in `_BROAD_MANUAL_PREFIXES`
(`deploy_changes.py`) still park the tick. Before 2026-09-11 a `k3s` change did park the tick
(issue #1467).

A recorded line pages **GitOps Deploy — Status** six hours later unless something clears it.
When the cluster is already upgraded by step 4, the hand-apply the line asks for is a
verification run, not a change. After the merge, run the apply the deployer printed if you want
that verification, then clear the line:

```bash
uv run python scripts/deploy_tools/gitops_state.py clear-owed manual_plane k3s
```

[A role only a hand can apply is recorded, not parked](gitops-pipeline.md#a-role-only-a-hand-can-apply-is-recorded-not-parked)
owns the ledger, the narrowed `--applied` form of the clear, and the page. Do not run
`git pull --ff-only` on the primary checkout to release the deployer. The deployer has no park
to clear here, and a hand fast-forward empties the `local..origin` range, which cancels the deploys
those commits were due ([gitops-pipeline.md](gitops-pipeline.md), "The tree is dirty").

## Rollback

SUC's `upgrade.sh` refuses an older binary, so a lowered Plan fails its Job and a re-pin alone
does not downgrade either node. Pause SUC on both nodes (`homelab/k3s-upgrade-paused` on each
node, or an empty `system_upgrade_nodes`). Downgrade daniel-server by hand first, then daniel-box,
so the agent is never newer than its server. Then land a PR that lowers `k3s_version` and sets
`k3s_downgrade_acknowledged` to the same value in
`ansible/roles/k8s/system-upgrade/defaults/main.yml`. Without the key,
`scripts/validate/cluster_pins.py` refuses the decrease.

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
