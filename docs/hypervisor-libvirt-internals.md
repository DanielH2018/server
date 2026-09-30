# hypervisor internals — the retired staging guest, the network and the fence

Working-out moved off `ansible/roles/setup/hypervisor/CLAUDE.md` (#2991), which a session reads
on every touch of the role. The role doc keeps the rules; this page keeps how each one was
arrived at. `docs/archive/staging-cluster.md` is the record of the cluster this role once
carried, and `docs/k3s-etcd-restore.md` is the drill the surviving guest runs.

## The retired staging guest, and how the host converges

`daniel-stage` — 8 GiB, 4 vCPU, a 100 GB qcow2 — was removed on 2026-09-29 (#2941). The
operator decided no manual staging sessions continue, and the GitOps tick had already stopped
consulting it (#2859), so the guest sat allocated for work nothing drove.

**Deleting `guest.yml` would not have removed the guest.** `teardown.yml` runs only when
`has_hypervisor` goes false, and that flag stays true on daniel-server for the drill — so the
domain would have kept running as an orphan Ansible no longer manages. That is the
`docker_install` failure `tasks/main.yml` documents, and it is why the reap is on the install
path: `reap_staging.yml`, included last from `install.yml`, destroys the domain, runs
`virsh undefine --remove-all-storage` on it, and removes the seed, the seed directory and
the rendered XML by path. It is guarded on the domain existing, so it is a permanent no-op once the host has
converged.

The same file reaps what the staging gate left: `/home/ubuntu/server-staging`, its lock,
`/usr/local/bin/staging-gate-dispatch` and `/usr/local/bin/staging-gate-run`. It **refuses
while that checkout is dirty**, on the same reasoning that leaves `/var/lib/libvirt` alone. A
tree is reproducible from the remote; an edit that exists only there is not.

The gate's public key moved to `files/staging-gate-retired/`, so the withdrawal task in
`install.yml` takes it out of `authorized_keys`. Deleting `files/staging-gate.pub` on its own would have left the
key working here forever: `authorized_key` runs `state: present` with `exclusive` false, so it
only ever adds. The private half (`staging_gate_ssh_key`) is out of SOPS and out of the
rotation registry, and `ansible/roles/setup/gitops_deploy` deletes it from daniel-box.

`ansible/tests/staging/test_staging_tick_arm_retired.py` holds a census of what is gone against
a census of what the drill still needs. Each without the other is the wrong retirement.

## Why the staging subnet is `192.168.140.0/24`

The number was chosen against a census of what daniel-server actually routes: `10.0.0.0/24`
(LAN), `10.42.0.0/16` and `10.43.0.0/16` (k3s), `10.200.0.0/16` and `172.17.0.0/16` (bridges
the retired Docker install left behind), and `192.168.122.0/24` (libvirt's `default`). Being
outside `10/8` means a future k3s or Docker range cannot grow into it.

`network.yml` renders the network XML to `/etc/libvirt/declared-networks/` and defines from
there. libvirt's own `/etc/libvirt/qemu/networks` is its internal store, written by
`net-define` and not meant to be edited underneath it. The define runs only when that rendered
file changes or the network is undefined, which is what keeps a re-run at `changed=0`.

The network keeps the name `staging` after the cluster went. daniel-server's copy carries that
name under a UUID libvirt refuses to redefine under another one, so renaming it would mean
destroying and rebuilding the bridge for cosmetics.

`ansible/tests/staging/test_staging_network.py` renders the template and parses it. That matters
more here than elsewhere: nothing else in the repo validates libvirt XML, and two bugs in this
role reached a real host in one afternoon because every check only parsed the file it lived in.

## The UFW route deny was inert, and why

The first fix for the guest reaching the production LAN was a UFW `route deny` on this host. It
deployed, `ufw status` listed it, and the probe still reached all three targets.
`/etc/default/ufw` already carried `DEFAULT_FORWARD_POLICY="DROP"`, so if UFW's forward chain
governed this traffic the guest would have been fenced before that rule existed — libvirt's own
FORWARD accept is reached first. The rule is now a delete-task in
`ansible/roles/setup/initial_setup/tasks/network.yml`, with the history at the line.

The reachability that forced the fence was measured on 2026-08-27 from inside the guest, which
reached the whole production LAN masqueraded as daniel-server: MetalLB VIP 301, k3s API 401,
and daniel-pi's unauthenticated wg-easy admin UI 200.

**Whether the fence fires is measured during a drill.** That half used to be
`scripts/diagnostics/staging_egress_probe.py`, an on-demand probe inside the persistent
`daniel-stage` guest — the only place reachability can be measured from. It went with the guest
(#2941) and came back as a leg of the drill orchestrator (#2943), because the drill's guest is
transient: it exists only while a drill runs, so the measurement has to run during one.

`fence_check` in `etcd-restore-drill-vm.sh.j2` dials the three fenced ranges plus an internet
control target from the guest, after ssh comes up and before the guest is handed the cluster
token or the R2 credentials — so a leak aborts the run with nothing staged. The evidence is
`egress-fence.log` in the run's directory, and the verdict rides the run's Kuma message
(`fence=hold`, or `fence=hold,unproven=<labels>` when a target answered from neither the guest
nor daniel-server, which means the target moved rather than that the fence held).

Two addresses are allocated rather than pinned in the inventory — Longhorn's frontend ClusterIP
and a pod IP discovered from `ip neigh show dev cni0` — which is why each carries the host-side
control leg. Without it a probe against an address production had moved off would pass forever.

## Two libvirt UUID collisions the templates pin around

- **A redefined nwfilter re-applies to every interface already referencing it**, so editing a
  rule inside the filter needs no guest restart. That works only because the template pins the
  filter's UUID: with no `<uuid>` libvirt mints one and then refuses the name collision, so the
  role would deploy once and fail on every re-run.
- **`net-define` collides the same way.** The first change to `staging-network.xml.j2` since
  bring-up — the drill guest's reservation, 2026-09-11 — failed with `network 'staging' already
  exists with uuid ...`. The network on daniel-server predates the pin and carries a random
  UUID, and libvirt refuses a same-name define under any other UUID even after `net-undefine`.
  So `network.yml` reads the live UUID with `net-uuid` and pins that, falling back to `to_uuid`
  on a fresh host. A re-define still only rewrites the persistent config: the running `dnsmasq`
  keeps the reservations it started with, which is why the same tasks push a changed reservation
  in with `net-update --live` rather than restart the network under whatever is on the bridge.
- **A referenced filter cannot be undefined.** `nwfilter-undefine` reports "Requested operation
  is not valid: nwfilter is in use" while a guest holds it, so clearing a stray one means
  `virsh destroy` on whatever domain holds it, then `virsh undefine`, then re-run the role.
  The refusal is a feature: the fence cannot be removed out from under a running guest.

## How the drill guest is built and torn down

`etcd_drill.yml` renders the domain XML, the cloud-init seed and the pinned host key.
`etcd-restore-drill-vm`, the monthly root cron, converts a disk from the base image, `virsh
create`s the guest from that XML, runs the drill, then `virsh destroy`s the guest and deletes
the disk on every exit path. Nothing in the role `virsh define`s, which is what makes the guest
invisible to teardown's "no guest defined" refusal and keeps any restored etcd database, token
copy or R2 credential from outliving a run.

The guest's host key is pinned rather than accepted on first connect. A fresh disk every run
means a guest-minted key every run, so the orchestrator would otherwise have to accept any key
— and anything else that ever lands on this bridge could then answer for the address. Ansible
generates the key once, seeds it through cloud-init's `ssh_keys`, and the orchestrator connects
with `StrictHostKeyChecking=yes` against that one public half.

## What ships into the drill guest, and where each piece comes from

- daniel-server's own `/usr/local/bin/k3s` — the same version as the server that wrote the
  snapshot. The drill prints the snapshot's recorded node versions beside it.
- `K3S_TOKEN` from the k3s-agent unit's environment file. It is identical to the server's
  `token` when no `--agent-token` is set, and a wrong one fails the restore stage loudly.
- The R2 write credentials, rendered to `/etc/homelab/etcd-drill-s3.env`. That is a residency
  the drill added: those credentials sit on this host as well as on daniel-box.

`scripts/backup/etcd_restore_drill.sh` cannot pass beside a live k3s, so the guest is made to
look like a k3s server node whose k3s is stopped and the script runs there unmodified. What
persists after a run is `/var/log/etcd-restore-drill/<run-id>/` — drill stdout, `restore.log`
and `server.log` — pruned at `hypervisor_etcd_drill_log_retention_days`.

## Why libvirt's `default` network is stopped

Installing `libvirt-daemon-system` brings up a `default` NAT network and `virbr0` with its own
NAT and forward rules, and daniel-server already runs k3s's flannel and kube-router chains. A
third writer of firewall state arriving as a side effect of a package install is the same class
of problem as the Docker reinstall that preceded this role, so `install.yml` stops that network
and clears its `autostart` flag. The staging network is declared explicitly in its own slice; this makes
sure nothing else is. Both `virsh` calls are guarded on the network's current state, so re-runs
are no-ops.

`community.libvirt` is deliberately not a dependency. Adding a pinned collection is a lockstep
change with `ansible-core` (`ansible/requirements.yml`), and three `command` calls did not
justify it. Revisit if the guest definition needs the module.
