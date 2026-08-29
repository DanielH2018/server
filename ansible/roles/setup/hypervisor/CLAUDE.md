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
  every interface already referencing it. That works only because the template pins the filter's
  UUID: `nwfilter-define` is not `net-define`, and with no `<uuid>` it mints one and then refuses
  the name collision, so the role would deploy once and fail on every re-run.
- **A referenced filter cannot be undefined.** `nwfilter-undefine` reports "Requested operation is
  not valid: nwfilter is in use" while the guest holds it, so clearing a stray one means
  `virsh destroy daniel-stage` first, then undefine, then re-run the role. The refusal is a
  feature — the fence cannot be removed out from under a running guest.

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

## The staging gate's checkout

`install.yml` clones `/home/ubuntu/server-staging`, and it is the tree the staging gate deploys
FROM. `scripts/deploy_tools/staging_gate_remote.sh` cds there, fast-forwards it to the SHA under
test, and runs `./scripts/deploy.sh --tags <svc> -e target=daniel-stage`.

That script has **two callers**, and the file is written to suit both. `staging_gate.py` still
pipes it over ssh (`bash -s`), which is the live path. The restricted key described below execs
it from the checkout instead. Because the second caller runs it from disk in a tree it
fast-forwards, **its body is wrapped in a `main` function** — bash reads a script by byte offset
as it executes, so a `git merge --ff-only` rewriting the file mid-run would resume at a
meaningless offset. Wrapping makes bash parse the whole body before any of it runs. Don't unwrap
it.

**It exists because the gate used to do all of that to `/home/ubuntu/server`, this host's own
checkout** (2026-08-29 review M-2). Three things were wrong with that and only the first is
obvious:

- An operator's tree jumped to arbitrary commits behind their back, since the gate never restored
  what it merged. daniel-server's checkout was found sitting on whatever SHA the gate last tested.
- Two gate runs — the 30-minute tick's and an operator driving `staging_gate.py` by hand — could
  interleave a fetch, a merge and a deploy on one tree, each believing it had pinned the commit it
  was measuring.
- A dirty tree there made the gate answer `PREP_FAILED` for every commit. That maps to NO_VERDICT,
  which the deployer reports as "staging could not be asked, which is not a rejection" and then
  deploys prod anyway — so the gate could be dead for days and read as staging being down.

The clone fixes all three by giving the gate a tree it owns. **The gate moving its own tree
forward is the point, not a residual defect** — what M-2 objected to was it moving someone else's.

Three things are load-bearing:

- **`update: false` on the git task.** The gate fast-forwards this tree every tick; an updating
  clone would yank it back to master's tip mid-run, and `deploy.sh` renders from the working
  directory, so the verdict would describe a tree nobody asked about. Ansible creates it once and
  never touches the branch again.
- **Cloned from the remote, not from `/home/ubuntu/server`.** A local clone would share an object
  store and re-couple the two trees' fates, which is the thing being undone.
- **`/var/lock/staging-gate.lock` is NOT `/var/lock/server-git-tree.lock`.** `deploy.sh` takes the
  latter *inside* the gate, and `flock` re-opens the file it is given while POSIX locks are not
  reentrant across a fresh open — sharing one would deadlock the gate against itself. Contention
  on the staging lock is a PREP failure, because a run that never started learned nothing about
  the SHA.

Teardown removes the clone and the lock, but **refuses while the tree is dirty**, on the same
reasoning that leaves `/var/lib/libvirt` alone: a clean tree costs a re-clone to rebuild, and an
edit that exists only here is not reproducible from anywhere.

The path and the lock are duplicated between this role's `defaults/main.yml` and that shell
script, which cannot read a Jinja var. `ansible/tests/test_staging_gate_paths_agree.py` pins them
equal — the drift is silent in the worst direction, since a stale path in the script makes every
tick answer NO_VERDICT rather than fail.

## The staging gate's restricted ssh key

The gate runs on daniel-box and has to reach the staging guest, which only daniel-server routes
to — so it hops here over ssh (`docs/staging-phase-c.md`, Decision 1). Until 2026-08-29 that hop
authenticated with **the operator's own unrestricted key**, so anything able to invoke the gate
had a full shell on this host (review M-3).

`install.yml` now also deploys a dedicated ed25519 identity:

- The **public** half is `files/staging-gate.pub`, authorized for `sys_user` with
  `restrict,command="/usr/local/bin/staging-gate-dispatch"`.
- The **private** half is `staging_gate_ssh_key` in SOPS, written to
  `/etc/gitops-deploy/staging_gate_ed25519` (0600) on daniel-box by `roles/setup/gitops_deploy`.
- The forced command is rendered from `templates/staging-gate-dispatch.sh.j2`, root-owned 0755 —
  if `sys_user` could rewrite it, that user would choose what the key runs.

**The trap that makes the naive version useless: a `command=` forced command does NOT stop ssh
forwarding stdin.** The gate's own design pipes a script to `bash -s`, so a forced command that
still read stdin would execute whatever the caller sent, and the restriction would be
decorative. The dispatcher therefore closes stdin on its first executable line and takes an
**operation name plus arguments** — `gate <40-hex sha> <tags>` in `$SSH_ORIGINAL_COMMAND` —
never a script body. Every field is checked against a whitelist charset and *rejected* rather
than escaped; nothing is interpolated into a shell string.

`ansible/tests/test_staging_gate_dispatch.py` drives the dispatcher's own `validate_request`
(the file guards `main` on `BASH_SOURCE` so the test can source it) and pins both properties.
Its rejecting half covers `bash -s`, an empty command, a ref name in place of a SHA, and shell
metacharacters in the tags; two tests feed a script body on stdin and assert it does not run.

**The dispatcher does no git work and takes no lock**, on purpose. All of that stays in
`staging_gate_remote.sh` so there is one copy, and because **flock attaches to the open file
description rather than the process** — a second `exec 9>` on the same path conflicts with the
first even inside one process tree, so a dispatcher that took the lock would deadlock the gate
against itself.

**What may lag, and why that is safe.** The dispatcher is pre-deployed, so it can be older than
the SHA being gated. It reads nothing from the tree, so the only thing that can lag is its
validation, and stale validation can only *refuse* — never approve. Everything after the `exec`
comes from the checkout, including `deploy.sh`, whose exit code is the verdict. Refusals exit
71 (`DISPATCH_REFUSED`) and prep failures 70, both of which `staging_gate.py` maps to
NO_VERDICT: the gate could not be *asked*, which must never read as staging rejecting a change.

**Residual risk, accepted:** a holder of this key can gate any commit reachable on `origin`,
because the gate fetches from there. That is inherent to "the gate deploys a SHA" and is not
made worse by this change.

**The gate uses this key.** `staging_gate.py` authenticates with
`/etc/gitops-deploy/staging_gate_ed25519` and sends `gate <sha> <tags>` as its request; it no
longer pipes a script to `bash -s`. Prove the path with:

```bash
# on daniel-box, after `initial_setup.yml --tags hypervisor` has run on daniel-server
./scripts/deploy_tools/verify_staging_gate_key.sh "$(git rev-parse origin/master)"
```

**Do not hand-roll those two ssh commands.** This file prescribed them until 2026-08-29 and they
are unsound. The negative check was `ssh -i <key> -o IdentitiesOnly=yes <host> "bash -s"`
expecting 71 — and it printed **0**, the signal of the restriction failing, when the real cause
was that the key would not load, so ssh silently fell back to a default identity and ran a normal
shell. `IdentitiesOnly=yes` does not prevent that: the DEFAULT identity files still count as
configured, so it bounds which keys are offered without guaranteeing ours is one of them.

A check that reports "your security control is broken" when the truth is "your key file is
unreadable" is worse than no check, because the next person acts on the wrong diagnosis. The
script closes both halves: it refuses to connect at all until `ssh-keygen -y` on the key matches
`files/staging-gate.pub`, and it requires the negative case to produce **both** a 71 and the
dispatcher's own refusal marker on stderr — a fallback to another key cannot print that marker,
so "fell back" and "restriction bypassed" stay distinguishable. Its exit codes name them
separately: 10 key unusable, 11 fell back, 12 restriction open, 13 no verdict.
`ansible/tests/test_verify_staging_gate_key.py` drives that verdict function without a network.

**What stops a silent fallback.** `IdentitiesOnly=yes` does not guarantee this key is the one
used — the default identity files still count as configured — so if the key were missing or
unloadable, ssh would quietly authenticate as the operator and the gate would keep returning
verdicts while running unrestricted. That would hide the very regression the key exists to
prevent. `staging_gate.py:identity_problem()` therefore refuses to connect at all unless
`ssh-keygen -y` on the key matches `files/staging-gate.pub`, and a far side that answers 127 —
the shell not finding a `gate` command, which only happens when no forced command is attached —
is reported as a failed authentication rather than as a verdict. Both map to NO_VERDICT, never
to REJECTED: a security regression must not read as staging rejecting a change.

Removing or restricting the operator's own key is a separate decision and is not part of this
work.
