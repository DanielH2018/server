# hypervisor — KVM/libvirt on daniel-server

Makes a host able to run VMs. It built the staging cluster's substrate until #2941 retired
that cluster on 2026-09-29; what it runs now is the monthly etcd restore drill's throwaway
guest, plus the network and egress fence that guest attaches to. `docs/archive/staging-cluster.md`
is the historical record.

- **Host:** `daniel-server` only (`has_hypervisor: true` in its host_vars). Default is
  `false` in `group_vars/all.yml` — this is opt-in per host, like `has_github_cli`.
- **Run it:** `uv run ansible-playbook ansible/initial_setup.yml --tags hypervisor`,
  **on daniel-server**. That host is `ansible_connection=local` in `hosts.ini`, so running
  this from daniel-box targets daniel-box.
- **Exit criterion:** `virsh --connect qemu:///system version` succeeds as `ubuntu`. The
  role asserts it rather than leaving it to the operator.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "hypervisor"`
- **Crons (1):**
  - `Full etcd restore drill in a throwaway guest` — `20 11 1 * *`
<!-- /generated_from -->

## The retired staging guest, and how the host converges

`daniel-stage` — 8 GiB, 4 vCPU, a 100 GB qcow2 — was removed on 2026-09-29 (#2941). The
operator decided no manual staging sessions continue, and the GitOps tick had already stopped
consulting it (#2859), so the guest sat allocated for work nothing drove.

**Deleting `guest.yml` would not have removed the guest.** `teardown.yml` runs only when
`has_hypervisor` goes false, and that flag stays TRUE on daniel-server for the drill — so the
domain would have kept running as an orphan Ansible no longer manages. That is the
`docker_install` failure `tasks/main.yml` documents, and it is why the reap is on the INSTALL
path: `reap_staging.yml`, included last from `install.yml`, destroys the domain, undefines it
with `--remove-all-storage`, and removes the seed, the seed directory and the rendered XML by
path. It is guarded on the domain existing, so it is a permanent no-op once the host has
converged.

The same file reaps what the staging GATE left: `/home/ubuntu/server-staging`, its lock,
`/usr/local/bin/staging-gate-dispatch` and `/usr/local/bin/staging-gate-run`. It **refuses
while that checkout is dirty**, on the same reasoning that leaves `/var/lib/libvirt` alone — a
tree is reproducible from the remote, an edit that exists only there is not.

The gate's public key moved to `files/staging-gate-retired/`, so the withdrawal task in
`install.yml` deauthorizes it. Deleting `files/staging-gate.pub` on its own would have left the
key working here forever: `authorized_key` runs `state: present` with `exclusive` false, so it
only ever ADDS. The private half (`staging_gate_ssh_key`) is out of SOPS and out of the
rotation registry, and `roles/setup/gitops_deploy` deletes it from daniel-box.

ENFORCED by `ansible/tests/staging/test_staging_tick_arm_retired.py`, which holds a census of
what is gone against a census of what the drill still needs. Each without the other is the
wrong retirement.

## The staging network

`network.yml` declares one NAT network, `staging`, on `virbr-stage`. It renders the XML to
`/etc/libvirt/declared-networks/` and defines from there — libvirt's own
`/etc/libvirt/qemu/networks` is its internal store, written by `net-define` and not meant to
be edited underneath it. The define runs only when that rendered file changes or the network
is undefined, which is what keeps a re-run at `changed=0`.

**The subnet is `192.168.140.0/24`, and the number is not arbitrary.** It was chosen against
a census of what daniel-server actually routes: `10.0.0.0/24` (LAN), `10.42.0.0/16` and
`10.43.0.0/16` (k3s), `10.200.0.0/16` and `172.17.0.0/16` (bridges the retired Docker
install left behind), `192.168.122.0/24` (libvirt's `default`). Being outside `10/8` means a
future k3s or Docker range cannot grow into it.

The drill guest's address is the network's one **DHCP reservation**, on a fixed QEMU-OUI MAC.
It sits outside the dynamic range, or the lease could be handed out first. The load-bearing
assertion is that the domain's interface MAC equals that reservation: if they drift, the guest
still boots and still gets an address, just a dynamic one, and the orchestrator waits on an
address nothing answers.

The network keeps the name `staging` after the cluster went. daniel-server's copy carries that
name under a UUID libvirt refuses to redefine under another one, so renaming it would mean
destroying and rebuilding the bridge for cosmetics.

ENFORCED by `ansible/tests/staging/test_staging_network.py`, which renders the template and parses
it. That matters more here than elsewhere: nothing else in the repo validates libvirt XML,
and two bugs in this role reached a real host in one afternoon because every check only
parsed the file they lived in.

## The egress fence is an nwfilter, and the first attempt was not

`network.yml` defines a libvirt nwfilter, `staging-egress-fence`, that drops anything the guest
sends to `lan_subnet`. The domain's `<interface>` references it by name. Without it the guest
reaches the whole production LAN *masqueraded as daniel-server* — measured 2026-08-27: MetalLB
VIP 301, k3s API 401, daniel-pi's unauthenticated wg-easy admin UI 200.

**The first fix was a UFW `route deny` on this host, and it was inert.** It deployed, `ufw status`
listed it, and the probe still reached all three. `/etc/default/ufw` already carried
`DEFAULT_FORWARD_POLICY="DROP"`, so if UFW's forward chain governed this traffic the guest would
have been fenced before that rule existed — libvirt's own FORWARD accept is reached first. Don't
re-derive this; the rule is now a delete-task in `roles/setup/initial_setup/tasks/network.yml` with
the history at the line.

Two mechanics that decide whether a change lands:

- **A filter applies when the interface is CREATED.** Adding the `<filterref>` to a domain
  template updates the persistent config; a running guest keeps its unfenced interface. The only
  guest left here is transient and gets a fresh tap device on every run, so it always starts
  fenced — the correction task that handled this for the persistent guest went with it (#2941).
- **Editing a rule inside the filter needs no restart.** libvirt re-applies a redefined filter to
  every interface already referencing it. That works only because the template pins the filter's
  UUID: with no `<uuid>` it mints one and then refuses the name collision, so the role would
  deploy once and fail on every re-run.
- **`net-define` collides the same way, and the network template pins its UUID too.** The
  first change to `staging-network.xml.j2` since bring-up (the drill guest's reservation,
  2026-09-11) failed with `network 'staging' already exists with uuid ...`. The network on
  daniel-server predates the pin and carries a random UUID, and libvirt refuses a same-name
  define under any other UUID even after `net-undefine`, so `network.yml` reads the live
  UUID with `net-uuid` and pins that, falling back to `to_uuid` on a fresh host. A re-define
  still only rewrites the persistent config: the running dnsmasq keeps the reservations it
  started with, so the same tasks push a changed reservation in with `net-update --live`
  rather than restart the network under whatever is on the bridge.
- **A referenced filter cannot be undefined.** `nwfilter-undefine` reports "Requested operation is
  not valid: nwfilter is in use" while the guest holds it, so clearing a stray one means
  `virsh destroy` on whatever domain holds it first, then undefine, then re-run the role. The
  refusal is a feature — the fence cannot be removed out from under a running guest.

ENFORCED by `ansible/tests/staging/test_staging_egress_fence.py`, which renders the filter and
parses it. That check sees shape and attachment only.

**Whether the fence FIRES has no gate any more.** That half was
`scripts/diagnostics/staging_egress_probe.py`, and it ran INSIDE the persistent `daniel-stage`
guest — the only place reachability can be measured from. It went with the guest (#2941).
Re-pointing it at the drill's transient guest means running it during a drill, which is a
different design; it is filed rather than done.

## The etcd restore drill's throwaway guest

`etcd_drill.yml` prepares the one guest this host builds, `etcd-drill`, which exists only while
the FULL etcd restore drill runs in it (issue #1175, path 1; the long form is `docs/k3s-etcd-restore.md`).
`scripts/backup/etcd_restore_drill.sh` cannot pass beside a live k3s, so the guest is made to
look like a k3s server node whose k3s is stopped, and the script runs there unmodified.

- **Transient, never defined.** Ansible renders the domain XML, the seed and the pinned host key;
  `etcd-restore-drill-vm` (the monthly root cron) `virsh create`s the guest from that XML on a
  disk it just converted from the base image, and `virsh destroy`s it plus deletes the disk on
  every exit. Nothing here `virsh define`s, so teardown's "no guest defined" refusal never sees
  it, and no restored etcd database, token copy or R2 credential outlives a run. What persists is
  `/var/log/etcd-restore-drill/<run-id>/` (drill stdout, `restore.log`, `server.log`), pruned at
  `hypervisor_etcd_drill_log_retention_days`.
- **The host key is pinned.** A fresh disk every run means a guest-minted host key every run, so
  the orchestrator would have to accept any key — and anything else that ever lands on this bridge
  could then answer for the address. The key is generated once by ansible, seeded through
  cloud-init's `ssh_keys`, and the orchestrator connects with `StrictHostKeyChecking=yes` against
  that one public half.
- **What it ships in, and where it comes from.** daniel-server's own `/usr/local/bin/k3s` (the
  same version as the server that wrote the snapshot; the drill prints the snapshot's recorded
  node versions beside it), `K3S_TOKEN` from the k3s-agent unit's env file (identical to the
  server's `token` when no `--agent-token` is set — a wrong one fails the restore stage loudly),
  and the R2 credentials rendered to `/etc/homelab/etcd-drill-s3.env`. That last one is a new
  residency: the R2 write credentials now sit on this host as well as daniel-box.
- **The cadence and the alarm are one number apart.** `etcd_drill_full_cron` and
  `etcd_drill_full_kuma_interval_s` live in `group_vars/all.yml` because k8s/uptime-kuma reads
  them too. The tile `etcd Restore Drill (full)` is the alarm; the stamp on this host is the
  local record and nothing in the cluster reads it (monitor-bridge is pinned to daniel-box's
  list-only stamp and by design never accepts a `full` one).

ENFORCED by `ansible/tests/staging/test_etcd_drill_vm.py`: MAC ↔ reservation, the fence, the
pinned host key, no `define`, and the deadline pinned between one cron period and two.

## Autonomous-role contract (the monthly drill creates and destroys a guest)

One cron here changes state: `Full etcd restore drill in a throwaway guest`
(`cron_file: etcd-restore-drill-vm`), the monthly root cron that runs the FULL etcd restore
drill in the guest above. The staging guest itself is created by the
role, not by a cron, and is out of this contract.

- **Scope / exclusions:** `virsh create` the `etcd-drill` guest from a disk converted from
  the base image, run `scripts/backup/etcd_restore_drill.sh` inside it against the newest
  off-box snapshot in R2, then `virsh destroy` the guest and delete the disk. **Never** `virsh define`
  (so no guest outlives a run), **never** the live cluster, **never** the staging guest —
  its fence and its disk are untouched.
- **Mode (explicit + reversible):** `hypervisor_etcd_drill_armed` (`defaults/main.yml`,
  `true`) is the cron's `state:`; `false` removes it on the next run. The cadence is
  `etcd_drill_full_cron` in `group_vars/all.yml`, shared with `k8s/uptime-kuma`, which sizes
  the tile's deadline from it.
- **Authoritative sources:** the snapshot listed in R2 (the same bucket the daily off-box
  snapshot writes), daniel-server's own `/usr/local/bin/k3s`, and `K3S_TOKEN` from the
  k3s-agent unit's env file. A wrong token fails the restore stage loudly rather than
  restoring something else.
- **Abort valves:** teardown on every exit path; the pinned guest host key
  (`StrictHostKeyChecking=yes` against one public half, so the orchestrator never accepts an
  unknown guest on the shared bridge); the deadline pinned between one cron period and two.
- **Required evidence:** `/var/log/etcd-restore-drill/<run-id>/` (drill stdout,
  `restore.log`, `server.log`), retained `hypervisor_etcd_drill_log_retention_days`; the
  `etcd Restore Drill (full)` Kuma tile, which is the alarm. Nothing in the cluster reads the
  local stamp — monitor-bridge accepts only daniel-box's `--list-only` stamp by design.
- **Next-run review:** before changing the cadence or what ships into the guest, read the
  last run's directory; `ansible/tests/staging/test_etcd_drill_vm.py` pins the fence, the
  host key, no `define`, and the deadline, so a widening that breaks one fails there first.

## The default network is stopped on purpose

Installing `libvirt-daemon-system` brings up a `default` NAT network and `virbr0`, with its
own NAT and forward rules. daniel-server already runs k3s's flannel and kube-router chains.
A third writer of firewall state appearing as a **side effect of a package install** is the
same class of problem as the Docker reinstall that preceded this role — so `install.yml`
stops that network and clears its autostart. The staging network is declared explicitly in
its own slice; this makes sure nothing else is.

Both `virsh` calls are guarded on the network's current state, so re-runs are no-ops.

`community.libvirt` is deliberately not a dependency: adding a pinned collection is a
lockstep change with `ansible-core` (see `ansible/requirements.yml`) and three `command`
calls did not justify it. Revisit if the guest definition needs the module.

## Teardown exists, and refuses when it should

`has_hypervisor: false` runs `teardown.yml`, which stops libvirtd and purges the packages.
`/var/lib/libvirt` is left alone for the same reason `/var/lib/docker` is — disk images are
irreversible in a way a package is not.

**It refuses while any guest is still defined.** Purging libvirt out from under a domain
orphans its disk image with nothing that knows how to start it, which reads as a successful
teardown. Undefining a guest is a decision; the assert names the guests in the way. The drill's
guest is transient and never defined, so it is never in the way.

ENFORCED by `ansible/tests/setup/test_has_flag_roles_have_both_directions.py`: a role dispatching
on a `has_*` flag must handle both values. `docker_install` honoured only the true branch
for months, which is how `has_docker: false` came to describe a state nothing converged to.
