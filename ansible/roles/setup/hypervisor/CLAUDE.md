# hypervisor — KVM/libvirt on daniel-server

Makes a host able to run VMs. Deploys the staging cluster's substrate; see
`docs/staging-cluster.md`, Decisions 1 and 2.

- **Host:** `daniel-server` only (`has_hypervisor: true` in its host_vars). Default is
  `false` in `group_vars/all.yml` — this is opt-in per host, like `has_github_cli`.
- **Run it:** `uv run ansible-playbook ansible/initial_setup.yml --tags hypervisor`,
  **on daniel-server**. That host is `ansible_connection=local` in `hosts.ini`, so running
  this from daniel-box targets daniel-box.
- **Exit criterion:** `virsh --connect qemu:///system version` succeeds as `ubuntu`. The
  role asserts it rather than leaving it to the operator.

## The staging guest

`guest.yml` builds and starts `daniel-stage`: 8 GiB, 4 vCPU, a 100 GB qcow2 converted from
the Ubuntu noble cloud image, seeded with cloud-init and attached to the `staging` network.

- **The cloud image checksum is pinned**, and upstream rotates `current` roughly monthly, so
  this *will* eventually fail on a host that has not downloaded it yet. That is the intended
  failure — re-pin from `cloud-images.ubuntu.com/noble/current/SHA256SUMS`. A staging
  substrate silently built from a different image than the one reviewed defeats the point.
- **The disk is a full `qemu-img convert`, not a backing file.** A backing chain would tie
  the guest to the base image forever, so re-pinning would break an existing guest. qcow2 is
  sparse, so the copy costs what the image holds rather than its virtual size.
- **The seed ISO is built with `xorriso`**, because `cloud-image-utils` is not in this
  host's package set and `xorriso` already is. Its volume id must be `cidata` — that string
  is how cloud-init's NoCloud datasource finds the disk.
- **cloud-init reads the seed on first boot only.** Editing `user-data` afterwards rebuilds
  the ISO and changes nothing. Bump `hypervisor_staging_vm_instance_id` to make cloud-init
  re-run its per-instance modules without rebuilding the disk.
- **The guest's only credential is daniel-server's own ssh key**, slurped at run time. The
  thing that needs to reach the guest is Ansible running on that host; a separate keypair
  would be a second secret to rotate for no additional isolation.
- **No graphics device.** `virsh console` is the recovery path when ssh is what is broken.

ENFORCED by `ansible/tests/test_staging_vm.py`. The load-bearing assertion is that the
domain's interface MAC equals the network's DHCP reservation: if they drift, the guest still
boots and still gets an address, just a dynamic one, and every later slice that names
`staging_vm_ip` points at nothing.

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

The guest's address is a **DHCP reservation** on a fixed QEMU-OUI MAC, declared in
`group_vars/all.yml` so the role and the inventory entry that reaches the guest share one
source. It sits outside the dynamic range, or the lease could be handed out first.

ENFORCED by `ansible/tests/test_staging_network.py`, which renders the template and parses
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

- **A filter applies when the interface is CREATED.** Adding the `<filterref>` to the domain
  template updates the persistent config; a running guest keeps its unfenced interface. `guest.yml`
  reads the live XML and `virsh destroy`s a running guest that lacks it, so the existing start task
  brings it back fenced. That task fires only while the live interface is unfenced.
- **Editing a rule inside the filter needs no restart.** libvirt re-applies a redefined filter to
  every interface already referencing it.

ENFORCED by `ansible/tests/test_staging_egress_fence.py`, which renders the filter and parses it.
That check sees shape and attachment only. Whether the fence *fires* is a property of the host, and
the gate for that half is the probe, run on daniel-server:

```bash
uv run python scripts/diagnostics/staging_egress_probe.py
```

The internet control target must stay reachable. A fence that severed all egress would make every
production target fail too, and that reads as a pass to anyone skimming.

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
teardown. Undefining a guest is a decision; the assert names the guests in the way.

ENFORCED by `ansible/tests/test_has_flag_roles_have_both_directions.py`: a role dispatching
on a `has_*` flag must handle both values. `docker_install` honoured only the true branch
for months, which is how `has_docker: false` came to describe a state nothing converged to.
