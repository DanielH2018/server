# hypervisor — KVM/libvirt on daniel-server

Makes a host able to run VMs. What it runs is the monthly etcd restore drill's throwaway guest,
plus the network and egress fence that guest attaches to; it carried the staging cluster until
#2941 retired that cluster on 2026-09-29.

This file holds the rules. `docs/hypervisor-libvirt-internals.md` holds the working-out behind
them — the staging reap, the subnet census, the inert UFW attempt, the two UUID collisions and
what ships into the drill guest.

- **Host:** `daniel-server` only (`has_hypervisor: true` in its host_vars). Default is
  `false` in `group_vars/all.yml` — opt-in per host.
- **Run it:** `uv run ansible-playbook ansible/initial_setup.yml --tags hypervisor`,
  **on daniel-server**. That host is `ansible_connection=local` in `hosts.ini`, so running
  this from daniel-box targets daniel-box. The role's own exit criterion is that
  `virsh --connect qemu:///system version` succeeds as `ubuntu`.
- **`install.yml` stops libvirt's `default` network and clears its autostart**, so the package
  install cannot add a third writer of firewall state beside flannel and kube-router. Leave that
  pair of guarded `virsh` calls in place.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "hypervisor"`
- **Crons (1):**
  - `Full etcd restore drill in a throwaway guest` — `20 11 1 * *`
<!-- /generated_from -->

## The staging network

`network.yml` declares one NAT network, `staging`, on `virbr-stage`, subnet
`192.168.140.0/24`. It renders the XML to `/etc/libvirt/declared-networks/` and defines from
there, never into libvirt's own store.

The drill guest's address is the network's one **DHCP reservation**, on a fixed QEMU-OUI MAC
outside the dynamic range. **The domain's interface MAC must equal that reservation**: drift
leaves the guest booting on a dynamic address while the orchestrator waits on one nothing
answers.

ENFORCED by `ansible/tests/staging/test_staging_network.py` — nothing else in the repo
validates libvirt XML.

## The egress fence is an nwfilter, and the first attempt was not

`network.yml` defines a libvirt nwfilter, `staging-egress-fence`, that drops anything the guest
sends to `lan_subnet`, and the domain's `<interface>` references it by name. Without it the guest
reaches the whole production LAN masqueraded as daniel-server, measured 2026-08-27.

**The first fix was a UFW `route deny` on this host, and it was inert** — libvirt's own FORWARD
accept is reached first. Don't re-derive it: the rule is a delete-task in
`roles/setup/initial_setup/tasks/network.yml`, and the docs page has the derivation.

Three mechanics decide whether a change lands:

- **A filter applies when the interface is CREATED**, so a `<filterref>` added to a domain
  template leaves a running guest unfenced. The one guest here is transient, with a fresh tap
  device per run, so it always starts fenced.
- **Editing a rule inside the filter needs no restart**, because the template pins the filter's
  UUID. `net-define` collides the same way, so the network template pins its UUID too, and a
  changed DHCP reservation goes in with `net-update --live`.
- **A referenced filter cannot be undefined** while a guest holds it. `virsh destroy` that
  domain first, then undefine, then re-run the role.

ENFORCED by `ansible/tests/staging/test_staging_egress_fence.py`, which sees shape and attachment
only. **Whether the fence FIRES has no gate** — the probe lived inside the retired guest.

## Autonomous-role contract (the monthly drill creates and destroys a guest)

One cron here changes state: `Full etcd restore drill in a throwaway guest`
(`cron_file: etcd-restore-drill-vm`), the monthly root cron that runs the FULL etcd restore
drill (issue #1175, path 1; long form `docs/k3s-etcd-restore.md`) in the one guest this host
builds. `etcd_drill.yml` prepares that guest, `etcd-drill`, and it is **transient, never
defined** — so teardown's "no guest defined" refusal never sees it, and no restored etcd
database, token copy or R2 credential outlives a run. Its host key is pinned, because a fresh
disk per run would otherwise force the orchestrator to accept any key.

- **Scope / exclusions:** `virsh create` the guest from a disk converted from the base image,
  run `scripts/backup/etcd_restore_drill.sh` in it against the newest off-box snapshot in R2,
  then `virsh destroy` the guest and delete the disk. **Never** `virsh define`, never the live
  cluster.
- **Mode (explicit + reversible):** `hypervisor_etcd_drill_armed` (`defaults/main.yml`, `true`)
  is the cron's `state:`; `false` removes it on the next run. The cadence is
  `etcd_drill_full_cron` in `group_vars/all.yml`, shared with `k8s/uptime-kuma`.
- **Authoritative sources:** the snapshot listed in R2, daniel-server's own
  `/usr/local/bin/k3s`, and `K3S_TOKEN` from the k3s-agent unit's env file. A wrong token fails
  the restore stage loudly.
- **Abort valves:** teardown on every exit path; the pinned guest host key; the deadline pinned
  between one cron period and two.
- **Required evidence:** `/var/log/etcd-restore-drill/<run-id>/`, retained
  `hypervisor_etcd_drill_log_retention_days`, and the `etcd Restore Drill (full)` Kuma tile,
  which is the alarm and sizes its deadline from `etcd_drill_full_kuma_interval_s`. Nothing in
  the cluster reads the local stamp — monitor-bridge takes daniel-box's list-only stamp and by
  design never a `full` one.
- **Next-run review:** read the last run's directory before changing the cadence or what ships
  into the guest. `ansible/tests/staging/test_etcd_drill_vm.py` pins the fence, the host key, no
  `define` and the deadline, so a widening that breaks one fails there first.

## Teardown exists, and refuses when it should

`has_hypervisor: false` runs `teardown.yml`, which stops libvirtd and purges the packages.
`/var/lib/libvirt` is left alone for the same reason `/var/lib/docker` is — disk images are
irreversible in a way a package is not.

**It refuses while any guest is still defined.** Purging libvirt out from under a domain orphans
its disk image with nothing that knows how to start it, which reads as a successful teardown.
The assert names the guests in the way; the drill's guest is transient and never one of them.

ENFORCED by `ansible/tests/setup/test_has_flag_roles_have_both_directions.py`: a role dispatching
on a `has_*` flag must handle both values.
