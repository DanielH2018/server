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

## What it does not do

No guest is defined here — that belongs to a later slice, so re-running this role on a host
that already has a staging VM leaves it alone.

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
