# `hosts/components/hypervisor` — KVM/libvirt on daniel-server

Makes daniel-server able to run VMs. What it runs is the monthly etcd restore drill's throwaway
guest, plus the network and egress fence that guest attaches to.

This file holds the rules. `docs/hypervisor-libvirt-internals.md` holds the working-out behind
them — the staging reap, the subnet census, the inert UFW attempt, the fence's tiles, the UUID
collisions, what ships into the drill guest, and why monitor-bridge never reads this drill's
local stamp.

- **Host:** `homelab-daniel-server` alone lists this component (H7.34), and no inventory flag gates
  it.
- **What a package cannot ship, `etcd-drill-prepare` makes before each run:** the checksum-pinned
  base image, the guest's pinned host key, the seed ISO that embeds its private half, and the
  stamp and log directories. It keeps the operator's `libvirt` and `kvm` groups too. Its failure
  fails the run, and the rule on the drill's textfile pages when the next verdict is overdue.
- **libvirt's `default` network stays off:** a third firewall-state writer beside flannel and
  kube-router otherwise. A `legacy:` entry retires its autostart link, and `etcd-drill-prepare`
  stops it. The libvirt arm never touches an object it did not declare.
- **To take it away:** drop the component from `hosts/daniel-server/package.yml`. The arm undefines
  the network and the filter it recorded, and the timer stops. The packages and `/var/lib/libvirt`
  stay, because a disk image is not reversible the way a package is.

## The staging network

`component.yml` declares one NAT network, `staging`, on `virbr-stage`, subnet
`192.168.140.0/24`, to reconcile's libvirt arm. The arm defines it from the packaged XML under
`/usr/lib/homelab/libvirt/`, never into libvirt's own store, and keeps it started and autostarted.

The drill guest's address is the network's one **DHCP reservation**, on a fixed MAC outside the
dynamic range. **The domain's interface MAC must equal that reservation**: drift leaves the
guest booting on a dynamic address the orchestrator never gets an answer from.

ENFORCED by `tests/staging/test_staging_network.py` — nothing else in the repo
validates libvirt XML.

## The egress fence is an nwfilter, and the first attempt was not

`component.yml` declares a libvirt nwfilter, `staging-egress-fence`, dropping guest traffic to
`lan_subnet`, `k3s_pod_cidr` or `k3s_service_cidr`; the domain's `<interface>` references it by
name. Without it the guest reaches production masqueraded as daniel-server.

**A UFW `route deny` was tried first here, and it was inert**: libvirt's own FORWARD accept is
reached first. Don't re-derive it; the docs page has why.

Three mechanics decide whether a change lands:

- **A filter applies when the interface is CREATED**, so a `<filterref>` added to a domain
  template leaves a running guest unfenced. The one guest here is transient and always starts
  fenced.
- **Editing a rule inside the filter needs no restart**, because the template pins the filter's
  UUID. `net-define` collides the same way, so `hypervisor_staging_net_uuid` pins the live
  network's. The arm's define reaches only the persistent config, so a changed DHCP
  reservation also needs a live update by hand; `docs/hypervisor-libvirt-internals.md` has why.
- **A referenced filter cannot be undefined** while a guest holds it. `virsh destroy` that
  domain first, then undefine, and the next reconcile defines it again.

### Whether it fires is measured during a drill

`fence_check` in the orchestrator dials every fenced range plus an internet control target from
inside the guest, **before the guest is handed the cluster token or the R2 credentials**. A leak
aborts the run with nothing staged.

- **The control target is load-bearing**: the two allocated targets (Longhorn's ClusterIP, a pod
  IP) carry a host-side control leg, and a refusal from BOTH sides means the target moved.
- **An unproven target does not fail the drill**, per the `# DECIDED: a leak aborts` marker. It
  alarms on the fence's OWN check, `etcd-restore-drill-egress-fence`: the drill's verdict reads
  `up` through an unproven range.

ENFORCED by `tests/staging/test_staging_egress_fence.py`, which holds the filter's
shape, and by `tests/staging/test_staging_egress_fence_fires.py`, which runs the leg
against stub dials — one test per verdict.

## Autonomous-role contract (the monthly drill creates and destroys a guest)

One timer here changes state: `homelab-etcd-restore-drill.timer`, the monthly root job that runs
the FULL etcd restore drill (issue #1175, path 1; long form `docs/k3s-etcd-restore.md`) in the one
guest this host builds. `etcd-drill-prepare` prepares that guest, `etcd-drill`, and it is
**transient, never defined**, so no restored etcd database, token copy or R2 credential outlives
a run. Its host key is pinned, not accepted on first connect.

- **Scope / exclusions:** `virsh create` the guest from a disk converted from the base image,
  run `scripts/backup/etcd_restore_drill.sh` in it against the newest off-box snapshot in R2,
  then `virsh destroy` the guest and delete the disk. **Never** `virsh define`, never the live
  cluster.
- **Mode (explicit + reversible):** `hypervisor_etcd_drill_armed` (`defaults/main.yml`, `true`)
  gates the timer's enable link; `false` stops it at the next package version. The cadence, `etcd_drill_full_cron` in
  `group_vars/all.yml`, also sizes the textfile verdicts' deadline.
- **Authoritative sources:** the snapshot listed in R2, daniel-server's own
  `/usr/local/bin/k3s`, and `K3S_TOKEN` from the k3s-agent unit's env file — a wrong token fails
  the restore loudly. Nothing rewrites that file on a joined agent, so after a deliberate
  server-token rotation the drill fails until the operator updates it.
- **Abort valves:** teardown on every exit path; the pinned guest host key; the deadline pinned
  between one timer period and two; the egress-fence leg, which aborts before any credential is
  staged.
- **Required evidence:** `/var/log/etcd-restore-drill/<run-id>/` — including
  `egress-fence.log`, the fence leg's per-target verdicts — retained
  `hypervisor_etcd_drill_log_retention_days`, and the two textfile verdicts that alarm,
  `etcd-restore-drill-vm.prom` (the drill) and `etcd-restore-drill-egress-fence.prom` (the fence
  leg) in node_exporter's textfile directory, with a deadline from
  `etcd_drill_full_kuma_interval_s`. The rules in
  `observability/files/rules/other-setup-checks.yaml` read them (M6 batch B4), and the run's
  `status=` and `fence=` syslog lines carry the messages. B4's Cutover-2 removed the two Kuma
  pushes.
- **Next-run review:** read the last run's directory before changing the cadence or what ships
  into the guest. `tests/staging/test_etcd_drill_vm.py` pins the fence, the host key, no
  `define` and the deadline.
