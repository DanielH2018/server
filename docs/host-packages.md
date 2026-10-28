# Host packages — the operator runbook

The host-package pipeline builds `homelab-*` Debian packages from master on daniel-box, signs
them, and serves them as an apt repository at `http://<daniel-box server_ip>:8791`. The
`builder` component of `homelab-daniel-box` installs the builder half on daniel-box; its
rules are in `hosts/components/builder/CLAUDE.md`, and its design is in
[the H3 plan](target-architecture/host/h3-pi-pilot.md). Until H7.41b the Ansible role
`homelab_builder` installed it.

This page holds the procedures an operator runs when the pipeline stops or must be stopped:

- clearing a deployer hold that a failed apply of the role wrote;
- the two host-plane pages, and clearing a host's hold with either lever;
- the three stop levers: a host's timer, the builder hold, and the disarm;
- rolling a host back;
- rotating the archive key, or recovering from a revoked one;
- clearing a deployer park that the role's `initial_setup.yml` entry caused;
- the signer's pages, and the five builder pages.

## How a package reaches a host

1. `homelab-builder.timer` runs `homelab-builder.service` every 2 minutes. The builder fetches
   master, picks the newest green first-parent commit since the last one it built, renders and
   builds every package whose render changed, and writes them to
   `/var/lib/homelab-builder/staged/` with `staged/chosen`, which names that commit.
2. Every builder run that exits 0 starts `homelab-repo-sign.service` through `OnSuccess=`. The
   signer checks every staged entry, copies the packages into
   `/var/lib/homelab-signer/repo/pool/main/`, and regenerates and signs the indexes. The
   signed `Release` carries `Valid-Until` seven days after its `Date`, and the signer re-signs
   the unchanged tree once its `Date` is 24 hours old.
3. `homelab-apt-repo.service` serves `/var/lib/homelab-signer/repo/` on port 8791. Each host's
   `homelab-updater.timer` installs the newest versions and runs reconcile.

The builder's state is `/var/lib/homelab-builder/state.json`. Its `last_built` key names the
commit it last built. `/var/lib/homelab-builder/published/` holds the render of each package as
it was last staged. The signer's state is `/var/lib/homelab-signer/state.json`, and a refused
set lands in `/var/lib/homelab-signer/refused/<UTC time>/`. Both services log to the journal:
`journalctl -u homelab-builder -u homelab-repo-sign`.

## The host packages

Each host installs `homelab-updater` and its own package, which `hosts/<host>/package.yml`
lists.

- `homelab-daniel-pi` ships reconcile, the secrets unit and the Pi's heartbeats.
- `homelab-daniel-server` ships reconcile, the secrets unit, the `kuma-push` component, the
  UPS shutdown chain and the `k3s-gates` component, whose `/usr/lib/homelab/k3s_gates.py` the
  k3s-agent Plan's `prepare` runs (#6413).
- `homelab-daniel-box` ships reconcile (H6.2), and since H6.3b the secrets unit and the
  `k3s-restart` component, with the `k3s-gates` component that ships the script the restart
  imports. Its one credential is `k3s_readonly_token`, which
  `ansible/vars/hosts/daniel-box.sops.yaml` carries. Ansible keeps the builder, the repository and
  every other daniel-box timer until H7, and the deployer keeps k3s's files until H6.4a. To
  remove the package before H6.4a, run `sudo apt-get remove homelab-daniel-box`. After H6.4a the
  package owns files that `k3s.service`'s `ExecStart` names, and removing it deletes them.
  daniel-server's package does not ship `k3s-restart` yet, so a k3s-agent restart there stays a
  hand step.

## The restart contract

`homelab-reconcile` is the only process that starts or restarts a unit outside the boot path,
unless the manifest marks the unit `windowed`. `homelab-secrets` calls no `systemctl`, and
systemd's own `Restart=` policy and an operator's hand command stay outside the rule. The
design is [the H4 plan](target-architecture/host/h4-secrets-unit.md), *The restart contract*.

- **Hand path.** `systemctl start homelab-reconcile` runs one reconcile now. A person who
  restarts `homelab-secrets` by hand restarts no dependent: that run queues the dependents of
  every changed output in `/run/homelab-secrets/pending.json`, and the next reconcile run
  restarts them.
- **`windowed` units.** Reconcile neither starts nor restarts a unit the manifest marks
  `windowed` (`k3s.service` and `k3s-agent.service`, once H6 marks them). Its queue entry is
  dropped. Two other processes restart such a unit, each under its own condition
  ([the H4 plan](target-architecture/host/h4-secrets-unit.md), *The restart contract*):
    - **SUC's upgrade container** restarts it after replacing the binary. It runs when `promote`
      applies a changed Plan, behind the Plan's `prepare` gates, outside any window.
    - **`homelab-k3s-restart.service`** (the `k3s-restart` component, H6.3b) restarts it inside
      the host's `k3s_restart_window`, once per changed hash of the files the package ships for
      the unit. It runs behind `k3s_gates.py host` and makes one attempt per hash. A package
      that lists the component declares the credential `k3s_readonly_token`.
    - **The two exclude each other on a node.** `prepare` refuses while
      `/run/homelab-k3s-restart/` exists, which the unit's `RuntimeDirectory=` creates. The unit
      refuses while a Plan is applying or a SUC Job in `system-upgrade` has no `Complete` or
      `Failed` condition.
    - **To pause the window**, run `sudo systemctl mask --now homelab-k3s-restart.timer`. To
      resume, `unmask` it and `start` it. **To retry a failed attempt**, remove
      `/var/lib/homelab-k3s-restart/failed.sha256`. A k3s that a failed restart left down is
      restarted by hand (H6, *Rollback*), because the unit's gates need the API.
- **The restart set.** Before step 2, reconcile builds one set from the units whose file or
  drop-in changed and the units in `pending.json`. A unit enters it only when it is enabled or
  active. Step 2 starts every other enabled unit that is not active. Step 3 restarts the set.
  Each unit gets at most one call per run, preceded by `systemctl reset-failed`, and no call
  uses `--no-block`.
- **A unit that cannot start yet.** A unit whose rendered outputs do not exist, or whose call
  fails, is added to `pending.json` and keeps its old hash in `applied.json`. The run exits 1,
  so the host holds. The next run tries it again.
- **A package that ships `secrets-unit`.** The manifest then carries `secrets_inputs_sha256`.
  When it differs from `applied.json`, or `homelab-secrets.service` is not active, reconcile runs
  a blocking `systemctl restart homelab-secrets.service` before step 2. Legacy entries retire only
  once that unit is active and every credential and output in `secrets.json` exists. A package
  without the field makes no call that names `homelab-secrets`, as the Pi's did until H4.5.
- **A legacy directory.** A `path` entry naming a directory retires only once the directory is
  empty: reconcile runs `rmdir`, never a recursive delete. A non-empty one stays and fails the
  run. A package therefore lists the directory after the files in it, as the Pi's does for
  `/etc/homelab/interim`.

## Activation, pending state and drift

`dpkg` installs a packaged file and applies nothing. A file entry in a `component.yml` therefore
carries `activate:`, and reconcile runs that action in step 3b when the installed file's hash
changes. The grammar and the validations are the docstring of
`hosts/lib/reconcile_lib/activate.py`. The design is H7's *How moved config becomes live*.

- **A validation failure is a fault.** `sshd -t` or `fail2ban-client -t` fails, or reconcile
  cannot parse a sysctl or journald file. The running service stays as it was, the run exits 1,
  the host holds, and reconcile writes `/run/homelab/validate-failed`. The next run that passes
  every validation removes the flag. `homelab-reboot.timer`, which H7.7 adds, refuses to reboot
  while the flag exists.
- **Reboot-pending and activation-pending.** A `reboot` action creates
  `/run/homelab/reboot-pending`, which the next boot clears. A `manual:<unit>` action, or any
  action on a unit the manifest marks `manual` or `windowed`, records the unit's `InvocationID`
  in `applied.json`. The record clears when the unit restarts. Reconcile writes both states to
  `homelab_host_reboot_pending{host}` and `homelab_host_activation_pending{host,unit}` in
  `homelab_host_reconcile.prom`, beside the updater's `homelab_host.prom`. While the flag
  exists, `homelab_host_reboot_pending_since_seconds{host}` carries its mtime, the time the
  host first owed the reboot; a later reboot-class change leaves it alone.
- **Drift is not a failure.** `homelab_host_reconcile_drift{host,arm}` counts items an arm
  left alone on purpose: UFW rules the floor kept, libvirt objects a running guest still
  references, users the floor kept, and legacy services that are still running.
  The run succeeds, so a revert still updates the host. The `host_packages` check names a
  non-zero count in its message and does not go down. To retry the items before the host's next
  package version, run `sudo systemctl start homelab-reconcile.service` on the host
  (**operator-only**).

## The UFW arm

A host package declares firewall rules in a `ufw:` key, and the UFW arm of reconcile
(`hosts/lib/reconcile_lib/ufw.py`) keeps them. The arm labels every rule it writes with
`comment 'homelab:<id>'`, or `homelab:keep:<id>` for the SSH and join rules it must never
delete. A manifest that declares no UFW rule, and has never declared one, makes the arm run no
`ufw` command at all.

UFW 0.36.2-6, measured on Ubuntu 24.04 by extracting the package and driving its Python front
end with `--rootdir` on a copy of the stock `/etc/ufw`:

| Command | Result |
|---|---|
| `ufw allow 80/tcp comment 'x'`, then `ufw allow 80/tcp comment 'y'` | one rule, comment `y`: appending an existing spec relabels it in place |
| `ufw insert 1 allow 8/tcp comment 'b2'` for a spec that exists | `Skipping inserting existing rule`; the old comment stays |
| `ufw delete allow 80/tcp` | deletes the rule whatever its comment |
| `ufw delete allow 22/tcp comment 'zzz'` | `Could not delete non-existent rule`; a different comment matches nothing |
| `ufw show added` | `ufw <spec> comment '<text>'` per rule, in order, and `(None)` for an empty list |

The measurement did not cover exit codes, and it did not cover the `Status: inactive` output of
`ufw status numbered`, which needs root and iptables. The container case that records both is an
open follow-up. The stub in `hosts/lib/tests/_reconcile_stub.py` replays the table above.

So the arm adopts a rule that predates it, such as the live hosts' `limit 22/tcp` with no comment, by
appending it again, and it uses `insert 1` only for a spec that is missing. A legacy entry that
names a spec a declaration shares never deletes it, because that rule is the adopted one.

Reconcile enables UFW only when `ufw status` reads `Status: inactive`, and only once the keep
rules `ssh-limit` and `lan-ssh` are declared and present. A rebuilt host is the case it exists
for. The live hosts are active, so there the step does nothing.

### A default policy is set only when it differs

On an active firewall, `ufw default <policy> <direction>` stops and starts the whole ruleset,
even when the policy is unchanged. `frontend.py` `set_default_policy` calls `stop_firewall()`
and then `start_firewall()`, and `ufw enable` on an active firewall calls `start_firewall()`
too. The start flushes every UFW chain before it reloads them from `/etc/ufw`. On a k3s node,
kube-router's network-policy sync saves the filter table and restores all of it. A restore of
a snapshot taken inside that window writes the flushed chains back.

That happened on daniel-box on 2026-10-25. The first install of a package whose `builder`
component declares `ufw:` ran the arm, which then issued two `ufw default` calls. kube-router
logged a sync at 14:14:43.722, and the first `[UFW BLOCK] IN=lo` came at 14:14:43.8.
`ufw-before-input` was left holding only `-j ufw-user-input`, which is the last line
`ufw_start` appends. Loopback traffic was dropped, so host DNS on 127.0.0.1 and k3s's own API
failed. k3s lost its lease and exited at 14:14:54. `/etc/ufw/before.rules` was intact
throughout, and a manual `ufw reload` at about 14:28 restored the chain.

So the arm reads `ufw status verbose` and sets only a policy that differs. A steady-state run
issues no command that reloads the firewall. An inactive UFW prints no policies, so there the
arm sets both, which reloads nothing. Last, on an active UFW, the arm reads
`iptables -S ufw-before-input`. When the chain lacks `-A ufw-before-input -i lo -j ACCEPT`,
the run fails, and the failure holds the host. The check only reads and restarts nothing. The
repair is an operator's `ufw reload`, which opens the same window again. The check reads once,
straight after the arm's commands, so it misses a restore that lands later. initial_setup's
`Enable UFW firewall` task has the same gate and the same check
(`ansible/roles/setup/initial_setup/tasks/network.yml`).

A rule add, relabel or delete on an active firewall still reloads the user chains. Those
chains sit below the before chains, so loopback and established traffic survive that window.

## The users, libvirt and line arms

Three more arms of reconcile keep state that `dpkg` does not hold. Each has the same early exit
as the UFW arm: a manifest that declares nothing for the arm, and has never declared anything,
makes the arm run no command at all. The keys a component writes are the docstring of
`scripts/renderer/renderer_lib/host_arms.py`, and each arm's manifest schema is the docstring of
its module in `hosts/lib/reconcile_lib/`.

| Arm | A component declares | The arm keeps | A fault holds the host | Drift never holds it |
|---|---|---|---|---|
| `users` | `users:` with a fixed uid per user | the users, created by `systemd-sysusers` from `/usr/lib/sysusers.d/homelab.conf` | a declared uid that differs from the live uid, a user still missing after `systemd-sysusers` | a user the floor kept |
| `libvirt` | `libvirt:` with networks and network filters, each XML pinning its name and UUID | the objects, defined when their XML hash changes or libvirt lost them | a failed define | a removal that libvirt refuses |
| `lines` | `lines:` with line or block edits and an optional `activate:` | one anchored line or one marker-delimited block per edit | an edit of an owned file that is not a conffile | none |

Reconcile runs the users arm and then the line arm before it starts any unit, so a unit that
runs as a new user starts, and the activation step reads the file the line arm edited. It runs
the libvirt arm and then the UFW arm last. The line arm therefore edits `/etc/ufw/sysctl.conf`
before an enable of UFW applies it.

- **The arms delete nothing they did not record.** The users arm expires a user that leaves the
  list with `usermod --expiredate 1` and never deletes one. The libvirt arm removes only a name
  its own record holds, so libvirt's `default` network is never touched. The line arm never
  removes a line. Removing a user's home, or a line, is an operator step.
- **An empty declaration is a floor.** A revert to a version that declares no users, or no
  libvirt object, expires and removes nothing, and counts each recorded item as drift. Without
  that floor a revert would remove the agent users or the staging network, and a host held
  after a revert could never clear its hold.
- **The line arm refuses an owned file unless it is a conffile.** It runs `dpkg -S` and then
  `dpkg-query -W -f='${Conffiles}'` on each owner. An unowned file, and a conffile, take the
  edit. Any other owned file is a fault: the package shipped something wrong. The arm
  rewrites a file in place and keeps its owner and mode, because sshd rejects an
  `authorized_keys` file with the wrong ones. Its regexp and markers equal the Ansible
  task's, so the first run adopts the line or block Ansible wrote.
- **The checks at render time run before the arm's.** A `lines:` entry whose line its own regexp
  misses would be appended again on every run, and a libvirt XML without a `<uuid>` would make
  `net-define` collide with the live object. Both fail the render.

The line arm's two owning-package upgrade cases run on both sides. The tests in
`hosts/lib/tests/test_reconcile_lines_arm.py` cover the arm's side with stub `dpkg`. The
`host_packages` container job covers the `dpkg` side (`scripts/validate/validate_lib/conffile_case.py`).
It installs a fixture package that owns a conffile, then a homelab fixture whose `lines:` edit lands
in it, and upgrades the owner under `--force-confold`: once with the conffile unchanged and once
changed. Each upgrade records whether `dpkg` kept the local file or held the package back. The
case fails when the line was gone before the next reconcile, and checks it again after that
reconcile.

### Getting a host out of one of these holds

A fault in these arms holds the host like any failed reconcile. The updater stops installing
until a version reconciles. `hosts/lib/tests/test_reconcile_hold_way_out.py` drives the real
updater against the real reconcile through a uid mismatch. It proves that `--clear-hold` alone
keeps the hold while the fault stands, and that installing a version which declares the live uid
clears it. The commands are the ones in *Clearing a host's hold* (**operator-only**).

## The `udev` arm

Step 5 of reconcile reloads `udev` when a package's rule changes. A package ships a rule as
`/usr/lib/udev/rules.d/NN-homelab-*.rules`. Reconcile hashes each such file and compares it with
`udev` in `applied.json`. A rule that is new or whose hash differs runs
`udevadm control --reload-rules` and then `udevadm trigger --subsystem-match=usb`. A host with no
such rule, an unchanged rule and a dropped rule make no `udevadm` call. After a drop, a device
keeps the mode of its last `udev` event until the next event, such as a re-plug, a trigger or a
reboot. A failed call exits 1 and leaves the old hash, so the next run retries it.

## Clearing a deployer hold that a failed `homelab_builder` or `gitops_deploy` apply wrote

The GitOps tick applies `gitops_deploy` on daniel-box through the setup plane. Until H7.41b it
applied `homelab_builder` the same way, and that role now ends without a task, so a hold that
names it was written before H7.41b. Every broad arm of the deployer is
forward-only, so a failed apply leaves the tree merged and writes two records:

- `hold_sha` in `/var/lib/gitops-deploy/`, which names the SHA that failed;
- one `hold_plane` line in `/var/lib/gitops-deploy/owed.jsonl` for each playbook and tag set
  that failed.

While the hold stands, `land.sh` reports `deploy-failed` for every session's landing, and
**GitOps Deploy — Status** pages. Each apply that can fail this way merges in an attended
window for that reason.

To clear the hold:

1. **Merge a fix, or the disarm, as its own PR.** The tick applies the role. An apply that covers every held plane clears `hold_sha` with no
   further step.
2. **If that apply fails too, list what the hold names.** On daniel-box, read the held SHA
   and every `hold_plane` line:

    ```bash
    cat /var/lib/gitops-deploy/hold_sha
    grep '"hold_plane"' /var/lib/gitops-deploy/owed.jsonl
    ```

    The list can name `homelab_builder`, `gitops_deploy` or both.
3. **Operator-only: apply each named plane by hand.** The play's `load_secrets` `pre_task`
   decrypts, so an agent session cannot run this step. From the primary checkout on
   daniel-box:

    ```bash
    uv run ansible-playbook ansible/initial_setup.yml --tags homelab_builder,gitops_deploy
    ```

    Name only the tags the hold lists.
4. **Clear the hold.** A hand `ansible-playbook` run is not the deployer, so it clears
   nothing on its own. Once every held plane is applied, run:

    ```bash
    uv run python scripts/deploy_tools/gitops_state.py clear-hold <full hold_sha>
    ```

    The command refuses unless the SHA is the one held. It removes `hold_sha` and every
    `hold_plane` line together. [The GitOps pipeline](gitops-pipeline.md)'s *Which apply
    clears a hold* states the coverage rule.

Never `rm` `hold_sha`. That leaves its `hold_plane` lines in the ledger with nothing that
names them, and `clear-hold --orphaned` is then the only way to drop them.

## Host-plane pages

Two Alertmanager rules in observability's `host-checks` group page on a host's updater. Each
carries `severity="critical"` and the host's name as `service`, and the alert's description
names what to read first.

- **`HostReconcileFailed`** fires when `homelab_host_reconcile_ok` has read 0 on a host for 15
  minutes. The updater writes 0 on every run while the host holds after a failed reconcile,
  so the host installs nothing newer until the hold clears. Read `journalctl -u
  homelab-updater -u homelab-reconcile` on the host and fix the cause. Then clear the hold
  with one of the updater's two levers, run as root on the host:
    - `/usr/lib/homelab/homelab-updater --clear-hold` reruns reconcile against the installed
      packages and clears the hold if it passes;
    - `/usr/lib/homelab/homelab-updater --clear-hold --install-newest` clears the hold, then
      installs the newest packages and reconciles as a timer run does. Use it when the fix is
      a newer package.
- **`HostUpdateStale`** fires when a host listed in
  `ansible/roles/k8s/observability/defaults/main.yml:observability_expected_hosts` reports no
  successful update within its `limit_seconds`. It also fires when the host's exporter is up
  and serves no success timestamp. A failed fetch writes no metric, so an unreachable
  repository, a stopped `homelab-apt-repo.service`, a firewall rule, a keyring mismatch and a
  stopped timer all end here. Read `systemctl status homelab-updater.timer` and `journalctl -u
  homelab-updater` on the host. A dead exporter does not fire it: that host's `up` at 0 keeps
  the rule quiet.

Each limit exceeds the longest gap between two of the host's updater fires plus the updater's
`TimeoutStartSec`, so one failed fetch never pages.
`scripts/renderer/tests/test_renderer_host_update_limits.py` fails a limit the host's rendered
schedule outgrows. The map lists daniel-pi (job `node-pi`, 11400 seconds), added by H3.9c.

## Clearing a host's hold

A failed reconcile makes a host's updater write `/var/lib/homelab-updater/hold`, and
`HostReconcileFailed` pages 15 minutes later. Each lever runs as root on the held host, from a
LAN session:

1. Read `journalctl -u homelab-updater -u homelab-reconcile` and fix the cause.
1. When the installed package is fine and the failure was transient, rerun reconcile. The
   command clears the hold only if reconcile passes:

    ```bash
    sudo /usr/lib/homelab/homelab-updater --clear-hold
    ```

1. When the installed package itself makes reconcile fail, wait until the builder has
   published the fix or the revert. Then clear the hold and install the newest versions:

    ```bash
    sudo /usr/lib/homelab/homelab-updater --clear-hold --install-newest
    ```

    A failure writes the hold again. With nothing newer published, this acts as
    `--clear-hold`.

## The timers the Pi package ships

`homelab-daniel-pi` links these timers in `timers.target`:

- `homelab-updater.timer` installs the newest packages and runs reconcile;
- `homelab-pi-sd-health.timer`, `homelab-pi-gz-integrity.timer` and `homelab-pi-recovery.timer`
  run the three heartbeats of `hosts/components/pi-heartbeats/`;
- `homelab-crowdsec-probe.timer` (H5.14, `hosts/components/crowdsec-probe/`) runs CrowdSec's edge
  probe once a minute, as the same user as the heartbeats.

To confirm the package owns the probe and its last run succeeded, run this read-only call:

```bash
ssh daniel-pi 'dpkg -S /usr/lib/systemd/system/homelab-crowdsec-probe.timer; systemctl show homelab-crowdsec-probe.service -p ExecMainExitTimestamp -p ExecMainStatus -p Result -p User; stat -c "%U %n" /var/log/pi-health'
```

To stop the probe for a while, run `sudo systemctl stop homelab-crowdsec-probe.timer` on the Pi.
The next package install starts it again. To stop it for good, drop the timer's `enable:` line
from the component in a package version. Reconcile stops the timer on the install that drops it.

## Stopping updates on one host

To stop one host installing anything, mask its updater timer on that host:

```bash
sudo systemctl mask --now homelab-updater.timer
```

Reconcile never starts or restarts a masked unit, so no later package undoes the mask.
The Docker engine-upgrade play (`docker_install`'s `engine-upgrade.yml`) uses the same lever. It
masks the timer, waits out a running updater, and unmasks the timer as its last task, unless the
timer was already masked when the play began. A failed play leaves the timer masked.
`HostUpdateStale` fires for the host once its limit passes, which is expected. To resume:

```bash
sudo systemctl unmask homelab-updater.timer
sudo systemctl start homelab-updater.timer
```

## Stopping every publish: the builder hold

To stop the pipeline publishing anything to any host, create the hold on daniel-box:

```bash
sudo touch /etc/homelab-builder/hold
```

`sudo touch` makes the file root's, and neither service user can remove it. While it exists:

- the builder fetches and builds nothing. Its unbuilt-since series reads `reason="hold"`, so
  the metric still shows how far master has moved past the builder;
- the signer leaves `staged/` as it is and publishes nothing new. It still re-signs the
  published tree once its `Date` is 24 hours old, so a long hold never lets `Valid-Until`
  lapse on every host.

To confirm the hold took, read `journalctl -u homelab-repo-sign -n 5`. The newest run logs
`held by /etc/homelab-builder/hold`. To release the hold:

```bash
sudo rm /etc/homelab-builder/hold
```

The next builder run builds the newest green commit and stages again any set the hold kept.
The signer then publishes it. The "Host Packages — Status" tile pages once a hold has left a
commit waiting to be built for 2 hours.

To stop the pipeline for longer than an incident, pause the timer instead. Run
`sudo systemctl mask --now homelab-builder.timer`, and `sudo systemctl unmask homelab-builder.timer
&& sudo systemctl start homelab-builder.timer` to resume. The repository keeps serving the last
published tree while the timer is paused, and `HostUpdateStale` fires once a host's limit passes
with nothing new. To take the repository down too, stop `homelab-apt-repo.service`; each host's
updater then fails its fetch.

## Releasing the other hosts before the canary reports: the rollout gate

A run whose changed set holds more than one host package stages only the canary's package
(daniel-pi, then daniel-server, then daniel-box) and opens a gate at that commit. While the gate
is open, the unbuilt-since series reads `reason="canary"`, and its `sha` names the gate's commit.
The gate closes by itself once Prometheus reports the canary at that commit or a descendant with
`homelab_host_reconcile_ok` at 1. The "Host Packages — Status" tile pages once the gate has stayed
open for 2 hours.

To close the gate before the canary reports, name the gate's commit on daniel-box. The gate's
commit is the `sha` label of the series, and a descendant of it also works:

```bash
echo <sha-from-the-canary-series> | sudo tee /etc/homelab-builder/release
```

`sudo tee` makes the file root's, and the builder user cannot create or change it. The next builder
run closes the gate without asking Prometheus, then builds the deferred host packages at the
commit it chooses. To leave the override behind, remove it once the run has built them:

```bash
sudo rm /etc/homelab-builder/release
```

A file that names an older commit, or no SHA at all, leaves the gate open. The builder logs
`rollout gate at <sha> closed` in `journalctl -u homelab-builder` when the gate closes. Shared
packages (`homelab-updater`, `homelab-tools`) never wait on the gate.

## Rolling a host back

The usual rollback is a revert on master. A version counts first-parent commits, so the
builder builds the reverted tree at a higher version, and each host installs it at its next
updater fire. A held host needs `--clear-hold --install-newest` once the revert is published.

To put one host on an older version at once, use the copy the pool keeps. The signer never
deletes a published `.deb`. On the host, from a LAN session:

1. Stop updates, or the next fire reinstalls the newest version:

    ```bash
    sudo systemctl mask --now homelab-updater.timer
    ```

1. List the versions the repository serves, and install the one wanted. The package's `postinst`
   runs reconcile against the older version.

    ```bash
    apt-cache policy homelab-<host>
    sudo apt-get install --allow-downgrades homelab-<host>=<version>
    ```

1. Once master carries a fix, unmask the timer as *Stopping updates on one host* describes.

## Rotating the archive key, or recovering from a revoked one

`Valid-Until` bounds how long a host accepts an old index signed by a working key. It does not
help once the key itself is revoked, because whoever holds the revoked key can sign a fresh
`Release`. A planned rotation follows the same steps, because every host must re-pin the new
key. Every step is **operator-only**, and each host step runs from a LAN session.

1. On each host, stop updates, so no host installs anything while it still trusts the old key:

    ```bash
    sudo systemctl mask --now homelab-updater.timer
    ```

    `HostUpdateStale` fires for each masked host once its limit passes, which is expected.
1. On daniel-box, set the builder hold (`sudo touch /etc/homelab-builder/hold`). Then generate
   the replacement key in the signer's GnuPG home, as the role's install arm does:

    ```bash
    sudo -u homelab-signer gpg --homedir /var/lib/homelab-signer/gnupg --batch \
      --pinentry-mode loopback --passphrase '' \
      --quick-generate-key "homelab archive signing key <homelab-archive@daniel-box>" \
      rsa4096 sign never
    ```

1. In a worktree, export the new key's public half over the committed keyring. Take its
   fingerprint from `--list-keys`.

    ```bash
    sudo -u homelab-signer gpg --homedir /var/lib/homelab-signer/gnupg \
      --export <new fpr> > hosts/keys/homelab-archive-keyring.gpg
    ```

    In the same commit, update the fingerprint pin, `ARCHIVE_FINGERPRINT` in
    `hosts/bootstrap-host.sh`.
    `ansible/tests/repo/test_lan_facts_have_one_source.py::test_the_bootstrap_pin_is_the_committed_keyrings_key`
    fails until the two match. Copy the new private key into the break-glass kit, as
    [the break-glass doc](break-glass.md) describes.
1. Delete the old key from the signer's home, because the role's apply refuses a home that
   holds more than one key. Then stop the agent the commands started:

    ```bash
    sudo -u homelab-signer gpg --homedir /var/lib/homelab-signer/gnupg --batch --yes \
      --delete-secret-and-public-key <old fpr>
    sudo -u homelab-signer gpgconf --homedir /var/lib/homelab-signer/gnupg --kill gpg-agent
    ```

    Until the apply in the step after next, the signer has no key to sign with. A re-sign
    that falls due before then fails, and pages once with `sign`.
1. Merge the PR. The tick applies nothing for a change under `hosts/`.
1. Let the builder rewrite `/etc/homelab-builder/archive-key.fpr`. Its `ExecStartPre=+` script
   `builder-fingerprint` writes the file from the signer's GnuPG home on every run, so no
   Ansible apply is needed. A held builder still runs it. To write it now, run
   `sudo /usr/lib/homelab/builder-fingerprint`.
1. Release the builder hold (`sudo rm /etc/homelab-builder/hold`). The builder's keyring check
   now accepts the render, so the builder publishes a `homelab-updater` that ships the new
   keyring. The signer's recorded key no longer matches `archive-key.fpr`, so it re-signs the
   whole tree with the new key.
1. On each host, run `sudo ./bootstrap-host.sh <hostname>` from the commit that carries the
   new pin, with that commit's `hosts/keys/homelab-archive-keyring.gpg` copied beside it. Its key check verifies the keyring against that pin. It then installs the newest
   packages, whose `homelab-updater` ships the same keyring. Then unmask and start the timer:

    ```bash
    sudo systemctl unmask homelab-updater.timer
    sudo systemctl start homelab-updater.timer
    ```

A host whose `HostUpdateStale` still fires after the last step has not recovered.

## Clearing a park caused by the role's `initial_setup.yml` entry

This applies to the `gitops_deploy` entry, and to a `homelab_builder` entry restored by a revert
of H7.41b. `initial_setup.yml` is a bring-up playbook, so adding or removing its `homelab_builder` entry
parks the GitOps deployer. The clear is **operator-only**, because the play's `load_secrets`
`pre_task` decrypts. On daniel-box, in the primary checkout:

1. Check the range the park holds:

    ```bash
    git log --oneline HEAD..origin/master
    git diff --name-only HEAD..origin/master
    ```

    Go on only when the range holds the entry's PR alone. Otherwise list each other change
    with the apply it owes, and run each after the last step.
1. `git merge --ff-only origin/master`.
1. `uv run ansible-playbook ansible/initial_setup.yml --tags homelab_builder`.

## The signer's page

`homelab-repo-sign.service` pages once per new failure through
`homelab-alert@homelab-repo-sign.service`. A repeat of the same failure exits 0, so the page does not
repeat every 2 minutes. Read `journalctl -u homelab-repo-sign` for the reason:

- **`refused staged/`.** A staged entry broke the refusal rule. It was a package not named
  `homelab-*`, a version that names a commit other than `staged/chosen`'s, a stray file, a
  symlink, or a malformed `chosen`. The signer published nothing, emptied `staged/`, and kept
  the set under `/var/lib/homelab-signer/refused/<UTC time>/`. Treat it as a builder defect,
  or as a render that tried to get something signed. Read the refused set and the builder's
  journal for that run before anything else. Once master carries the fix, reset the builder's
  published record, below, so the builder rebuilds the refused packages.
- **`key`.** `/etc/homelab-builder/archive-key.fpr` is missing or malformed. The next builder
  run rewrites it, or run `sudo /usr/lib/homelab/builder-fingerprint`.
- **`sign`.** `gpg` could not sign with the key `archive-key.fpr` names, usually because the
  signer's home no longer holds it. Compare `sudo -u homelab-signer gpg --homedir
  /var/lib/homelab-signer/gnupg --list-keys` with the file. The break-glass doc restores the
  key.
- **`index`.** `apt-ftparchive` failed. The journal line carries its stderr.
- **`io`.** A filesystem error, or a tool that would not start. The journal line names the
  path or the tool.
- **`staged`.** `/var/lib/homelab-builder/staged` is not a plain directory, for example a
  symlink. The signer touched nothing. Read `ls -la /var/lib/homelab-builder` and the
  builder's journal, then restore the directory with the role's apply.

A signer failure other than a refusal keeps `staged/`, and the next successful builder run
starts the signer again. A publish that fails before its index goes live removes the pool
files it had added, so no later re-sign indexes them. A failure after the index goes live,
such as a full disk while writing the metric, keeps them, because the live index lists them.

## The builder's pages

The builder pages once per new failure through `homelab-alert@homelab-builder.service`. The Uptime
Kuma tile "Host Packages — Status" (monitor-bridge's `host_packages` row, in "Automation &
Drift") pages on a condition that persists, in two cases:

- the builder's last good fetch of master is older than 30 minutes, or its series is absent.
  That covers a dead `homelab-builder.timer`, a missing token and a fetch that keeps failing;
- a commit of master has waited more than 2 hours to be built. The builder runs and exits 0 in each of these cases,
  so only the tile reports them.

The tile's message names the `reason` and `sha` labels of
`homelab_builder_unbuilt_since_timestamp_seconds`, which name the case. The tile stays silent
while daniel-box's node exporter is down, because Scrape Targets pages that instead. Every procedure below that
edits the builder's state stops its timer first, so no run overwrites the edit:

```bash
sudo systemctl stop homelab-builder.timer
systemctl is-active homelab-builder.service   # repeat until it prints inactive
```

Start the timer again with `sudo systemctl start homelab-builder.timer`.

### `reason="ancestry"`: master was rewritten

The fetched tip does not descend by first parent from `last_built`, so the builder builds
nothing. Once the rewrite is understood and accepted, drop `last_built`, so the next run walks
from the new tip. The edit keeps the file's owner:

```bash
sudo python3 -c 'import json, pathlib; p = pathlib.Path("/var/lib/homelab-builder/state.json"); s = json.loads(p.read_text()); s.pop("last_built", None); s.pop("failure", None); p.write_text(json.dumps(s, indent=2, sort_keys=True))'
```

The next run builds the newest green commit within the walk bound. A version counts the
chosen commit's first-parent commits. A rewrite that shortened master therefore publishes
versions lower than the ones hosts hold, and hosts keep the newer ones until master's count
passes them. Compare `git rev-list --count --first-parent origin/master` with the published
versions before you start the timer.

### Resetting the published record after a signer refusal

The builder records each package's render under `/var/lib/homelab-builder/published/` when it
stages the package, before the signer accepts it. After a refusal those renders read as
published, so the builder does not rebuild them until their render changes again. With the
timer stopped, remove the record:

```bash
sudo rm -rf /var/lib/homelab-builder/published
```

Then drop `last_built` with the command in the `ancestry` section, so the next run rebuilds
every package. A package whose render did not change is published with the same content at the
new version, which costs each host one install.

### `reason="no-green"` that persists: a renamed CI job

The builder grades each commit over `CI_CONTEXTS`, which the role renders from
`ci_required_contexts`. When a CI job is renamed, no commit carries the old context, so no
commit is ever green. Change `ci_required_contexts` in `ansible/inventory/group_vars/all.yml` to
the new name, spelled exactly as `ci.yml`'s `name:`, and merge that. The key is a deploy-plane
change, so the tick re-renders neither settings file. Re-apply the deployer by hand on
daniel-box (**operator-only**), and let the host package carry the builder's copy, which the
updater installs once the builder builds `homelab-daniel-box` with the new list:

```bash
cd /home/ubuntu/server && uv run ansible-playbook ansible/initial_setup.yml --tags gitops_deploy
```

### `reason="keyring"`

The keyring in `homelab-updater`'s render is absent, or its fingerprint differs from
`/etc/homelab-builder/archive-key.fpr`. The builder stages nothing, so no wrong keyring reaches
a host. Compare the fingerprint of the committed keyring with the file on daniel-box:

```bash
gpg --show-keys --with-colons hosts/keys/homelab-archive-keyring.gpg | grep '^fpr' | head -1
cat /etc/homelab-builder/archive-key.fpr
```

Restore a reverted keyring on master. Finish a key rotated on one side only with the rotation
procedure above. The first builder run after the fix builds.

### `reason="ci"`

The builder refuses to grade anything, because `CI_CONTEXTS` is empty or
`CI_ANCESTOR_WALK_MAX` is not a positive integer. Restore `ci_required_contexts` or
`ci_ancestor_walk_max` in `ansible/inventory/group_vars/all.yml`, merge it, and re-apply the
role by hand, because the tick re-renders no settings file for that file
(**operator-only**):

```bash
cd /home/ubuntu/server && uv run ansible-playbook ansible/initial_setup.yml --tags homelab_builder
```

### `reason="token"` and `reason="build"`

`token` means the settings carry no `GITHUB_TOKEN`, or the check-runs API refused the token with
401 or 403. Replace `homelab_builder_github_token` with the `add-secret` skill and re-apply the
role. `build` is a render or an `nfpm` run that failed at the chosen commit. Its journal line
names the package, and the fix lands on master.

### `reason="tools"`: a `homelab-tools` pin did not verify

The builder could not fetch a binary that `hosts/packages/homelab-tools/package.yml` pins, or the
download's sha256 differs from the pin. It builds and stages nothing at that commit, and the
journal line names the URL and both digests. Read the line, then check the pin against the
release's checksum file:

```bash
journalctl -u homelab-builder -n 20 --no-pager | grep 'tools:'
```

A digest that differs from a freshly published release means the pin is wrong or the upstream
asset changed. Fix the pin on master: Renovate's manager moves a version and its digest together,
so a hand edit that moved only one is the usual cause. A fetch error alone (a 404 or a timeout)
clears on the next run once the URL answers. The builder keeps a verified download under
`/var/lib/homelab-builder/tools-cache/<sha256>` and checks it again on every use, so deleting
that directory only costs a download.
