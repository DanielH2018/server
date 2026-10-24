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
It covers both nodes:
`server.yml` installs daniel-box, `agent.yml` installs daniel-server, and both pass it to the same
installer as `INSTALL_K3S_VERSION`. The installer is `install.sh` from that same release tag,
fetched with `checksum:` against `k3s_install_script_sha256` in the same file — so a bump has a
second literal to finish, and a wrong one fails the download rather than the install:

```bash
curl -fsSL "https://raw.githubusercontent.com/k3s-io/k3s/<new tag>/install.sh" | sha256sum
```

## Sequence at a glance

Upgrade the cluster first, merge the pin second. A failed upgrade then leaves master unpinned.
Merging does not park the deployer either way — see *After merging the pin* below.

1. Pass the gates.
2. Record pre-upgrade state.
3. Upgrade the server node, verify.
4. Upgrade the agent node, verify.
5. Merge the pin. The pin writes no `manual_plane` line (H6.1b), so only a change elsewhere under
   `ansible/roles/setup/k3s/` leaves one to clear.

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

## After merging the pin

A pin change no longer reaches the deployer's setup plane. The pin lives in the Flux-owned
`system-upgrade` role (H6.1b), so a bump routes to the `system-upgrade` tag, which `land.sh`
waits on through promotion, and the deployer writes no `manual_plane` line for `k3s`. The two
Plans select no node until H6.3c, so merging the pin upgrades nothing: this runbook's hand apply
is still the upgrade, and no line reminds you that it is owed. Another change under
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
