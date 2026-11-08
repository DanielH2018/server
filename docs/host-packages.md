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
- rebuilding a cluster node, adding a host, and upgrading a host's Ubuntu release;
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
- `homelab-daniel-server` ships reconcile, the secrets unit, the
  UPS shutdown chain and the `k3s-gates` component, whose `/usr/lib/homelab/k3s_gates.py` the
  k3s-agent Plan's `prepare` runs (#6413). Since H7.34 it also ships the k3s agent's whole host
  half (`hosts/components/k3s-agent/`) and the hypervisor with its monthly etcd restore drill
  (`hosts/components/hypervisor/`).
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
- **A legacy entry whose replacement failed to start.** Reconcile keeps the entry while a unit
  the same component or package spec ships failed to start in that run, and logs
  `keeping <entry>: <unit> failed to start`. The failed start already fails the run, so the host
  holds, and the entry retires on the first run after the unit starts. A held host runs no
  reconcile until `--clear-hold`. A unit reconcile did not try to start blocks nothing: a
  `manual`, `windowed` or masked one. An `/etc` copy that shadows a packaged unit is the exception.
  Reconcile removes it whatever the start did, because that copy may be why the unit failed, then
  restarts the unit on the packaged file before it retires the spec's other entries.
- **The overlap while a host holds.** The keep is per spec, so old and new schedulers can both run
  until `--clear-hold`. A failed timer keeps every cron of its spec, including one whose own timer
  started: a failed `homelab-apt-autoremove.timer` keeps `Weekly system restart` beside a running
  `homelab-reboot.timer`. A kept cron also overlaps its own timer once that timer starts outside
  reconcile, by hand or at boot. The trade favours a job that runs twice over one that does not
  run, and the hold pages the operator.
- **A legacy directory.** A `path` entry naming a directory retires only once the directory is
  empty: reconcile runs `rmdir`, never a recursive delete. A non-empty one stays and fails the
  run. A package therefore lists the directory after the files in it, as the Pi's does for
  `/etc/homelab/interim`.
- **A legacy template unit.** A `unit` entry such as `claude-land@.service` is disabled and its
  file removed, but never stopped, because `systemctl` refuses a template's own name. While any
  instance is active, activating or reloading, the entry stays and counts as drift, as a running
  legacy service does. A run therefore never ends an instance mid-run.
- **A toggle-gated legacy entry.** A `legacy:` entry may carry `when:`, and ships only while it
  holds. The claude-code component uses it to remove the textfile metrics a disarmed timer
  wrote: the toggle that drops the timer adds the entry. Step 1 stops the dropped timer and service before step 4
  removes the file, so a run in flight cannot write it back. Reconcile rebuilds its retired set
  from each manifest, so an entry that drops out when the timer is re-armed retires its path
  again on the next disarm.

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
  host first owed the reboot; a later reboot-class change leaves it alone. The updater runs
  reconcile only when a package changes, so `homelab-reconcile-metrics.service` runs
  `homelab-reconcile --metrics` once per boot. It rewrites the file from `applied.json`, and
  the reboot-pending series reads 0 after the reboot that cleared the flag (#6188).
- **Start times.** Each time reconcile starts or restarts a unit in steps 2, 3 or 4b, it
  records the time of the call and writes it as
  `homelab_host_unit_started_seconds{host,unit}`. A rule compares it with the unit's own
  result: `ComposeRestartUnreported` pages a Pi Compose run that wrote nothing (#6342).
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
straight after the arm's commands, so it misses a restore that lands later. The arm owns
the enable on every host since H7.32, which deleted initial_setup's `Enable UFW firewall` task.

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

Three Alertmanager rules in observability's `host-checks` group page on a host's updater. Each
carries the host's name as `service`, and the alert's description names what to read first.
The first two carry `severity="critical"`. The third carries `severity="warning"`.

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
  the rule quiet. A failed run of daniel-pi's `homelab-docker-engine.service` leaves
  `homelab-updater.timer` masked, and this rule pages that state.
- **`DockerEnginePinUnapplied`** fires when daniel-pi's `homelab_docker_engine_pending` has
  read 1 for 26 hours, so no run of `homelab-docker-engine.service` has applied the engine
  pins. 26 hours is the longest a pin waits for the 00:30 UTC window, plus the unit's 90-minute
  timeout and the updater run that rewrites the metric. It covers a paused engine timer, a Pi
  that was off at 00:30, and a run that failed without masking the updater. Read `systemctl
  status homelab-docker-engine.timer homelab-updater.timer` and `journalctl -u
  homelab-docker-engine` on the Pi, and `docs/docker-engine-pins-and-runtime.md`, *How the
  engine moves*. Once the cause is fixed, unmask and start `homelab-updater.timer` if the run
  left it masked, then start the unit from a LAN session or wait for the next window.

Each limit exceeds the longest gap between two of the host's updater fires plus the updater's
`TimeoutStartSec`, so one failed fetch never pages. A host that ships
`homelab-docker-engine.timer` also covers the engine run's mask. The run can hold
`homelab-updater.timer` masked for its whole `TimeoutStartSec` (#6613). daniel-pi's daily 03:05
fire comes after the 00:30 engine run's worst case, so its limit is one day plus one run.
`scripts/renderer/tests/test_renderer_host_update_limits.py` fails a limit the host's rendered
schedule outgrows. The map lists daniel-pi (job `node-pi`, 90000 seconds), added by H3.9c and raised by H3.13 (#6911).

## Clearing a host's hold

A failed reconcile makes a host's updater write `/var/lib/homelab-updater/hold`, and
`HostReconcileFailed` pages 15 minutes later. The hold stops the updater only. No setup apply
upgrades a host's packages since H7.60 (#7491), but a hand `apt upgrade` still can. Each lever runs
as root on the held host, from a LAN session:

1. Read `journalctl -u homelab-updater -u homelab-reconcile` and fix the cause. Reconcile logs
   `<action> starts` and `<action> took <N>s` around every command it calls. A run its start
   timeout killed ends on the `starts` line of the action it was in, when the time ran out inside
   a command or the `dpkg` lock wait. Python work between commands has no line, so a long gap after
   a `took` line means that work. The wait for the apt transaction, post-invoke hooks included, runs
   first in `homelab-reconcile-wait.service` under its own 5-minute limit, and logs `wait for apt
   to release the dpkg lock took <N>s` with the same tag.
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
- `homelab-pi-sd-health.timer`, `homelab-pi-gz-integrity.timer`, `homelab-pi-recovery.timer` and
  `homelab-pi-watchdog-health.timer` run the four heartbeats of `hosts/components/pi-heartbeats/`;
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
The Pi's `homelab-docker-engine.service` uses the same lever when it upgrades the engine. It
masks the timer, waits out a running updater, and unmasks the timer as its last step, unless the
timer was already masked when the unit started. A failed run leaves the timer masked.
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
The signer then publishes it. `HostPackageBuilderStalled` pages once a hold has left a
commit waiting to be built for 90060 seconds, the lag grace (*The rollout gate*).

To stop the pipeline for longer than an incident, pause the timer instead. Run
`sudo systemctl mask --now homelab-builder.timer`, and `sudo systemctl unmask homelab-builder.timer
&& sudo systemctl start homelab-builder.timer` to resume. The repository keeps serving the last
published tree while the timer is paused, and `HostUpdateStale` fires once a host's limit passes
with nothing new. To take the repository down too, stop `homelab-apt-repo.service`; each host's
updater then fails its fetch.

## Releasing the other hosts before the canary reports: the rollout gate

A run whose changed set holds more than one host package stages only the canary's package
(daniel-server, then daniel-pi, then daniel-box) and opens a gate at that commit. While the gate
is open, the unbuilt-since series reads `reason="canary"`, and its `sha` names the gate's commit.
The gate closes by itself once Prometheus reports the canary at that commit or a descendant with
`homelab_host_reconcile_ok` at 1. daniel-server leads because the Pi's updater fires once a day,
at 03:05 (H3.13, #6911). A run that changes only the Pi's and daniel-box's packages still makes
the Pi the canary. The Pi's `homelab-updater-poll.timer` starts its updater within about 15
minutes of a newer `homelab-daniel-pi` publish, once per version (#7065), so daniel-box's package
waits for that run. When the poll skips, because the updater timer is masked or apt is busy,
or the started run fails, the wait runs to the Pi's next daily fire. The
series then carries `host="daniel-pi"`, and `HostPackageBuilderStalled` pages once that
gate has stayed open for 90060 seconds, a grace derived from that gap, which
`ansible/filter_plugins/updater_lag_grace.py` reads from the Pi's timer drop-in. A gate whose
canary is any other host pages after 7200 seconds, like every other reason (#6919). To release daniel-box's package sooner, start the Pi's updater by hand or write
the release file below.

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

## Rebuilding a cluster node

`hosts/bootstrap-host.sh` puts a stock Ubuntu install back on the package path. Every step in
this section is **operator-only**, and the guest drill below has not run yet. The design is
[H7](target-architecture/host/h7-delete-ansible.md), *The cluster-node bring-up*.

### What the packages do not hold yet

`homelab-daniel-box` holds the server's whole host half since H7.35. `homelab-daniel-server`
holds the agent's join rules, the Longhorn host prerequisites, the `multipathd` blacklist and
the static LAN address since H7.34. The address pin takes effect at the next boot, so
*Rebuilding daniel-server* reboots before it starts the agent. Neither package touches LVM. The
Ubuntu installer creates each node's one volume, `ubuntu-vg/ubuntu-lv`, and step 2 below grows it.
No other LVM volume exists on either node.

### Preparing the installer on either node

1. To set up the installer's inputs, set the hostname to the node's inventory name, because step
   1 refuses any other. Then install what the next step and step 2 read before any package
   exists. Step 3 installs `age` itself:

    ```bash
    sudo apt-get update
    sudo apt-get install -y git gnupg curl
    ```

1. To give the root volume the whole disk, extend it. The Ubuntu installer creates the one LVM
   volume a node uses, `ubuntu-vg/ubuntu-lv`, and leaves it at about half the disk. Neither
   `bootstrap-host.sh` nor a package changes it, but the script's step 1 warns about a volume
   group with more than 10G unallocated. Skip this when `sudo vgs` shows no free space:

    ```bash
    sudo lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
    sudo resize2fs /dev/ubuntu-vg/ubuntu-lv
    ```

1. Clone the repository from GitHub, or from the kit's repo bundle
   ([the break-glass doc](break-glass.md), item 8). Copy the script and the keyring into one
   directory, because the script reads the keyring beside itself:

    ```bash
    git clone https://github.com/DanielH2018/server.git <clone>
    mkdir <dir>
    cp <clone>/hosts/bootstrap-host.sh <clone>/hosts/keys/homelab-archive-keyring.gpg <dir>/
    cd <dir>
    ```

1. To keep the node's existing age recipient, restore its key to `/etc/homelab/age.key` (root,
   mode 0600) before the first run. Otherwise step 3 creates a fresh key, and the host decrypts
   nothing until the recipient steps in the header of `ansible/.sops.yaml` have run.

### Rebuilding daniel-box

daniel-box serves the repository, so it builds its own packages from the clone.

1. Install the k3s binary. The installer neither enables nor starts the unit:

    ```bash
    sudo ./bootstrap-host.sh daniel-box --until 4 --local-build <clone>
    ```

1. Restore the server token, from [the break-glass doc](break-glass.md), item 2. Package files do
   not hold it. [The etcd restore doc](k3s-etcd-restore.md) says why it must survive:

    ```bash
    sudo install -D -m 0600 <token copy> /var/lib/rancher/k3s/server/token
    ```

1. Run the script to the end. `<clone>` must be a clone whose `HEAD` is a green first-parent
   commit of master:

    ```bash
    sudo ./bootstrap-host.sh daniel-box --local-build <clone>
    ```

1. Restore the archive signing key, which needs the `homelab-signer` user the package declares.
   Until the import, each builder run refuses with `reason="keyring"`, which is the intended
   state for a builder with no key. The next builder run writes
   `/etc/homelab-builder/archive-key.fpr`.

    ```bash
    sudo -u homelab-signer gpg --homedir /var/lib/homelab-signer/gnupg --batch --import <key copy>
    sudo -u homelab-signer gpgconf --homedir /var/lib/homelab-signer/gnupg --kill gpg-agent
    ```

1. To restore etcd as well, do it now, while k3s is still stopped. A new disk holds no local
   snapshot, so the restore reads R2. `homelab-secrets` renders
   `/etc/rancher/k3s/etcd-s3.env` from the host's SOPS file, and the package links it there. Follow
   *Restoring* in [the etcd restore doc](k3s-etcd-restore.md), which also reads the config the
   package installed.
1. Start k3s. No package enable link names `k3s.service`, and reconcile never starts a `windowed`
   unit, so this hand command is the only start on the package path. A link would start k3s at any
   reboot before the restores above, and this command also writes the link every later boot reads:

    ```bash
    sudo systemctl enable --now k3s.service
    sudo k3s kubectl get node daniel-box
    sudo systemctl start homelab-k3s-kubeconfig.service
    ```

1. Bring up Flux:
    - **On a new cluster**, follow [Bootstrapping Flux on a rebuilt
      cluster](flux-deploy-plane.md#bootstrapping-flux-on-a-rebuilt-cluster). The steps above stand
      in for that section's first command, which installs k3s. This page restates none of its
      steps.
    - **Then, on a new cluster only, refresh `k3s_readonly_token`.** A new cluster issues a new token
      for the read-only ServiceAccount, so the SOPS value belongs to the old cluster. Until this step
      lands, `kubectl` and the backup-health reader get 401, and `homelab-k3s-restart` cannot read the
      API. Once Flux has applied `cluster-base`, follow [the token's entry](secret-rotation.md) from
      reading the recreated value onward, skipping the deletion (**operator-only**). Reconcile then
      rewrites `~/.kube/config`.
    - **On a cluster restored from etcd**, the snapshot already holds `sops-age` and the Flux
      objects, so skip the bootstrap and read the Kustomization. Nobody creates
      `homelab-cluster-vars` by hand. When the Kustomization does not read Ready, replace `sops-age`
      with the current cluster key ([the break-glass doc](break-glass.md), item 10).

    ```bash
    sudo k3s kubectl -n flux-system get kustomization cluster-vars
    ```

1. As the operator, apply the dotfiles. They install the per-user uv, `prek` and the pinned Python,
   which the host's `uv` units read because they run with `--no-python-downloads`. The chezmoi
   binary comes from `homelab-tools`, and the `homelab-daniel-box` package depends on the distro
   packages the dotfiles scripts want. The role's own record says the prompt flags did not stop
   `init` prompting on chezmoi v2.71.1, so run this from a terminal and answer `work = false`,
   `agent = false` and `profile = server` if it asks:

    ```bash
    /usr/lib/homelab/bin/chezmoi init --apply --promptBool work=false,agent=false \
        --promptString profile=server DanielH2018/dotfiles
    ~/.local/bin/uv python list --only-installed
    ```

1. To let the deployer's CI gate and `renovate_agent` read an authenticated token, log in to GitHub,
   then apply the dotfiles once more. The `.gitconfig` template selects the `gh` credential helper
   only when `gh` is on PATH as chezmoi renders it, and a host whose first apply ran before `gh` was
   there holds the fallback:

    ```bash
    gh auth login
    /usr/lib/homelab/bin/chezmoi apply
    ```

1. To commit from the host, clone the repo and install its git hooks. No package or role runs this:

    ```bash
    git clone https://github.com/DanielH2018/server.git ~/server
    cd ~/server
    ~/.local/bin/prek install
    ```

### Rebuilding daniel-server

daniel-server installs from daniel-box's repository, so daniel-box and the repository must be up.

1. Read the token from [the break-glass doc](break-glass.md), item 2, without echoing it, and run
   the script. The installer writes both values to the agent's env file,
   `/etc/systemd/system/k3s-agent.service.env`, and the script never prints the token. A rerun that
   finds k3s installed skips the installer, so it never rewrites that file.

    ```bash
    export K3S_URL=https://<daniel-box server_ip>:6443
    read -rs K3S_TOKEN
    export K3S_TOKEN
    sudo --preserve-env=K3S_URL,K3S_TOKEN ./bootstrap-host.sh daniel-server
    ```

1. Rebuild the initramfs and reboot, before the agent starts. The reboot does two things:
    - It loads the i915 option the package ships. i915 loads from the initramfs, so
      `/etc/modprobe.d/i915.conf` reaches it only then, and QuickSync needs the GuC firmware the
      option enables.
    - It brings up the static LAN address. The package's netplan pin activates only at boot, and
      the agent's `node-ip` is `server_ip`, so an agent started on a DHCP address it does not
      hold fails to join.

    ```bash
    sudo update-initramfs -u
    sudo reboot
    ```

1. Check that the node holds `server_ip` on `k3s_node_static_link`, which
   `ansible/inventory/host_vars/daniel-server.yml` names. Then start the agent, for the same
   reason as on daniel-box, and read the node from daniel-box:

    ```bash
    ip -4 addr show <k3s_node_static_link>
    sudo systemctl enable --now k3s-agent.service
    ```

    ```bash
    sudo k3s kubectl get node daniel-server
    ```

1. As the operator, apply the dotfiles. They install the per-user uv, `prek` and the pinned Python,
   which the host's `uv` units read because they run with `--no-python-downloads`. The chezmoi
   binary comes from `homelab-tools`:

    ```bash
    /usr/lib/homelab/bin/chezmoi init --apply --promptBool work=false,agent=false \
        --promptString profile=server DanielH2018/dotfiles
    ~/.local/bin/uv python list --only-installed
    ```

1. To commit from the host, clone the repo and install its git hooks. No package or role runs this:

    ```bash
    git clone https://github.com/DanielH2018/server.git ~/server
    cd ~/server
    ~/.local/bin/prek install
    ```

The agent keeps the server token, because k3s documents no switch to an agent-scoped token for an
agent that has already joined.

### Rebuilding with GitHub or GHCR unreachable

To rebuild without GitHub or GHCR, take every fetch from the break-glass kit:

- Clone from the kit's repo bundle: `git clone <bundle> <clone>`.
- Add `--offline <kit-dir>` to each `bootstrap-host.sh` run. The script checks each file against
  the pin the download path checks, and it refuses on a missing file or a mismatch, naming the
  file. [The break-glass doc](break-glass.md), item 8, lists the directory's files. The script
  also skips both GitHub reads, so it prints that the clone is unchecked.
- For the Flux images and the cluster artifact, follow *Scenario: GitHub, GHCR or Sigstore is
  unreachable* in [the break-glass doc](break-glass.md).

### The guest drill

The guest drill proves daniel-box's procedure above on production hardware. It waits for the k3s
upgrade. *The proofs* in [H7](target-architecture/host/h7-delete-ansible.md) holds its fence, its
identity and its three guest-local deviations.

To run it, use a checkout on daniel-server whose `HEAD` is a green first-parent commit of master,
with its environment built by `uv run python --version`. Then run, as root on daniel-server:

```bash
sudo /usr/lib/homelab/etcd-drill-prepare
sudo /usr/lib/homelab/etcd-restore-drill-vm --rehearse-bootstrap --repo <checkout>
```

The orchestrator renders daniel-box's packages from the checkout to learn which units to mask, and
the guest clones GitHub at that checkout's `HEAD`. The run's directory under
`/var/log/etcd-restore-drill/` holds each step's output, `masks.txt`, `bootstrap-verdict.txt` and
`bootstrap-state.txt`. The run writes no stamp and no textfile verdict, and its syslog tag is
`etcd-drill-bootstrap-drill`.

- **What it runs.** A transient guest on the `staging` network plays daniel-box, with the clone
  fetched from GitHub. The drill skips the token and key restores, so the guest holds no production
  credential. The guest is renamed `daniel-box` first, because step 1 refuses any other name.
  It also waits for cloud-init and stops the apt-daily jobs, because step 5's install does not
  wait for the `dpkg` lock. The
  deviations go in before the install, and k3s starts by hand:
    1. `bootstrap-host.sh daniel-box --until 4 --local-build <clone>`;
    1. the three guest-local deviations. The masks live under `/run/systemd/system`, and
       `coredns-host.service` stays unmasked, because step 6 refuses to stop systemd-resolved
       until it answers on 127.0.0.1;
    1. `bootstrap-host.sh daniel-box --local-build <clone>`;
    1. `systemctl enable --now k3s.service`.
- **What passes.** The guest has `homelab-tools`, `homelab-updater` and `homelab-daniel-box`
  installed, `k3s.service` is the only k3s unit, `k3s kubectl get node daniel-box` reads Ready,
  and `ufw` is active. The orchestrator polls that check over fresh ssh sessions.
- **Blast radius.** The drill creates one transient domain and its disk on daniel-server, and it
  takes CPU and memory from the agent node for one run. It uses the existing `staging` network and
  its fence. Every service keeps serving, because the guest reaches no LAN address. The rollback
  is the drill's own teardown.

## Adding a host

Steps 1 and 3 to 5 go in the host's first PR, and step 7 is a second PR. The design is
[H7](target-architecture/host/h7-delete-ansible.md), *Adding a host*. A host on another
architecture first needs `homelab-tools` built for it, and `bootstrap-host.sh` pins amd64 and
arm64 only.

1. Add `hosts/<host>/package.yml`. The renderer writes the host's `nfpm.yaml` from this file, so
   no spec is written by hand. Add a `units:` entry only for a unit that a process other than
   reconcile restarts, such as `{policy: windowed}`.

    ```yaml
    description: <host>'s homelab host package
    components: [reconcile]
    maintainer_scripts: [preinst, postinst, prerm]
    enable:
      homelab-updater.timer: timers.target
    ```

    In the same step, name the host in two more places. The renderer reads the inventory, and
    `bootstrap-host.sh` refuses a host that `HOSTS` does not list:
    - add the host to `ansible/inventory/hosts.ini`, and copy
      `ansible/inventory/host_vars/_example.yml` to `<host>.yml`;
    - add the host to `HOSTS` in `hosts/bootstrap-host.sh`.
1. **Operator-only:** create the age key and read its public key. Run the script from the PR
   branch's copy, with the keyring beside it, as *Preparing the installer on either node* describes:

    ```bash
    sudo ./bootstrap-host.sh <host> --until 3
    ```

1. In `ansible/.sops.yaml`, add the creation rule for `vars/hosts/<host>\.sops\.yaml$` ahead of the
   generic rule. Its keys are the host's printed key, daniel-box's key and the recovery key, in that
   order. Adding a recipient is **operator-only**.
1. **Operator-only**, when the tree names a credential for the host: write its SOPS file, and commit
   the file. A host whose tree names no key needs no file.

    ```bash
    cd /home/ubuntu/server && uv run python scripts/secrets_mgmt/host_secrets.py sync <host>
    ```

1. Add the host to every other set that names hosts:
    - `NAMED` in `scripts/validate/validate_lib/package_roots.py`, as `homelab-<host>`, which both
      `host_packages` scans read;
    - `CANARY_ORDER` in `hosts/lib/builder_rollout_gate.py`, after daniel-box, for this PR only.
      This PR changes both `hosts/<host>` and `hosts/daniel-box`, and the rollout gate stages only
      the first of them in `CANARY_ORDER`. With daniel-box first, daniel-box installs the rule
      below before the builder publishes `homelab-<host>`. With the new host first, its package
      publishes while daniel-box's firewall still refuses it, and the gate waits on a host that
      cannot install. Step 7 moves the host ahead of daniel-box;
    - a third `apt-repo-<host>` rule under `ufw:` in `hosts/components/builder/component.yml`, or
      daniel-box's firewall refuses the host's `apt-get update` in step 6;
    - one `HostReconcileFailed` rule in
      `ansible/roles/k8s/observability/files/rules/host-checks.yaml`, because
      `ansible/tests/services/test_host_plane_rule_literals.py` fails a `hosts.ini` host without one.

    `host_secrets.py check` finds the host from `hosts/<host>/package.yml`, so it takes no edit.

    A host outside the cluster also needs its metrics to reach Prometheus, because the updater's
    series are textfile metrics:
    - ship a node exporter, as `hosts/components/pi-node-exporter/` does for daniel-pi;
    - add a scrape job like `node-pi` to
      `ansible/roles/k8s/observability/templates/prometheus.yaml.j2`, with `origin: <host>`;
    - add `<host>: {job: <scrape job>, limit_seconds: <seconds>}` to `observability_expected_hosts` in
      `ansible/roles/k8s/observability/defaults/main.yml`, with the limit above the host's longest
      updater gap;
    - add the host's `HostUpdateStale` rule to `host-checks.yaml`. The test above prints the rule.

    A new cluster node instead needs a k3s mode in `hosts/bootstrap-host.sh` and an entry in `NODES`
    in `hosts/lib/k3s_gates.py`.
1. **Operator-only**, after the merge: once the builder has published `homelab-<host>`, run the
   script to the end. The gate publishes it only after daniel-box reports the merge commit, so
   daniel-box's firewall already holds the rule. Do not write the release file to publish it
   sooner. To confirm the rule, check on daniel-box that the host's `server_ip` appears on port
   8791:

    ```bash
    sudo ufw status | grep 8791
    ```

    ```bash
    sudo ./bootstrap-host.sh <host>
    ```

1. Once Prometheus reports `homelab_host_applied_info` for the host, open a second PR that moves
   the host ahead of daniel-box in `CANARY_ORDER`. daniel-box runs the builder, the signer and the repository, so it always
   comes last otherwise: a package that breaks daniel-box can stop the publish of its own fix
   (`docs/target-architecture/host/h3-pi-pilot.md`, *Staged rollout (C5)*). Until this PR merges,
   any run that changes daniel-box's and the new host's packages makes daniel-box the canary.

`hosts/lib/tests/test_host_census.py` fails when a directory under `hosts/` is missing from `NAMED`,
`CANARY_ORDER` or `HOSTS`, or has no creation rule of its own. It names the set. The `host_packages`
job builds a fixture host from step 1's file list, so the YAML block above stays one that renders.

## Upgrading a host's Ubuntu release

No package depends on one Ubuntu release, and the `host_packages` container suite runs on every
release the fleet runs or is moving to. One host moves at a time, in the canary order: daniel-server,
then daniel-pi, then daniel-box. Each step is **operator-only**, apart from the matrix PR.

1. To make the host's package one the suite has tested on the target, merge a PR that adds the
   release to the `host_packages` matrix in `.github/workflows/ci.yml`, with the suite green. The
   PR makes the same edit to `.github/workflows/host-test-image.yml`, which
   `ansible/tests/repo/test_host_test_image.py` holds equal to `ci.yml`, and adds a
   `<release>-<arch> -` line per new leg to `hosts/lib/tests/container/image.pins`, which the same
   test requires. Until that workflow has published the new release's image and a later PR copies
   its digest into `image.pins` and adds the leg to that test's `PUBLISHED` set, the new leg
   pulls the published tag or builds from the Dockerfile. Skip this step when the matrix already lists the
   release.
1. Stop updates on the host, as *Stopping updates on one host* describes, so no package installs
   during the upgrade. `HostUpdateStale` can fire while the timer stays masked, which is expected.
1. On a cluster node, pause system-upgrade-controller and check the cluster, both from daniel-box.
   The label stops a promoted k3s bump from starting a Job on the node. Go on only when the gate
   exits 0. daniel-box's restart runs at `k3s_restart_window` in
   `ansible/inventory/host_vars/daniel-box.yml`, so pick a quiet slot outside that window.

    ```bash
    sudo k3s kubectl label node <node> homelab/k3s-upgrade-paused=true
    sudo python3 /usr/lib/homelab/k3s_gates.py host --server https://127.0.0.1:6443 \
      --token-file /run/credstore/k3s_readonly_token \
      --ca-file /var/lib/rancher/k3s/agent/server-ca.crt
    ```

1. Open a root shell with `sudo -i`, keep it open, and upgrade the host from it. Do not reboot
   yet.

    ```bash
    do-release-upgrade
    ```

1. Check that the upgraded sudo still reads the host-hardening drop-in, from the held root shell.
   From 26.04 on, `/usr/bin/sudo` is an alternative. `ubuntu-minimal` and
   `ubuntu-server-minimal` depend on `sudo-rs`, which registers at priority 50 against classic
   sudo's `/usr/bin/sudo.ws` at 40, so the upgrade selects sudo-rs. sudo-rs's `visudo` rejects
   `timestamp_type` in `/etc/sudoers.d/10-timestamp` (`unknown setting: 'timestamp_type'`), and
   sudo-rs keeps one credential record per terminal, so it cannot honour `global`.

    ```bash
    update-alternatives --query sudo
    visudo -c
    ```

    Then, from a second SSH session as the operator, run `sudo -k && sudo true`. Record in
    the PR or issue that tracks the upgrade whether sudo-rs refused the call or only warned
    about the line. If `visudo -c` fails or the second session's sudo refuses, select classic
    sudo from the held root shell and check again:

    ```bash
    update-alternatives --set sudo /usr/bin/sudo.ws
    visudo -c
    ```

    Do not edit `/etc/sudoers.d/10-timestamp` on the host. The `host-hardening` package owns
    it, so the verify step's `dpkg --verify` reports the edit and the next update restores the
    file. To run sudo-rs on a host instead, first merge a change to
    `hosts/components/host-hardening/templates/10-timestamp` that drops `timestamp_type` for
    that host. Reboot only once the second session's sudo works:

    ```bash
    reboot
    ```

1. Verify the host against the release. `lsb_release -rs` prints the target release, `dpkg --verify`
   prints nothing, and the reconcile start exits 0.

    ```bash
    lsb_release -rs
    dpkg --verify homelab-tools homelab-updater homelab-<host>
    sudo systemctl start homelab-reconcile.service
    ```

1. Resume updates as *Stopping updates on one host* describes, then run the updater once. Only the
   updater writes the two metrics the next step reads, so until this run they hold their
   pre-upgrade values. The run also installs whatever master published meanwhile.

    ```bash
    sudo systemctl start homelab-updater.service
    dpkg-query -W homelab-<host>
    ```

1. Read Prometheus. `homelab_host_reconcile_ok{host="<host>"}` reads 1, and
   `homelab_host_applied_info{host="<host>"}` carries the version `dpkg-query` printed.
1. On a cluster node, remove the label from daniel-box. system-upgrade-controller resumes on the
   node.

    ```bash
    sudo k3s kubectl label node <node> homelab/k3s-upgrade-paused-
    ```

A host that fails the verify step stays stopped, with its updater timer masked. A host that fails
the Prometheus read holds, so mask its timer again. Either host keeps its pause label. Its rollback
is a
rebuild on the previous release with `bootstrap-host.sh`. A cluster node follows *Rebuilding a
cluster node*, and the Pi takes `sudo ./bootstrap-host.sh daniel-pi`.

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

The builder pages once per new failure through `homelab-alert@homelab-builder.service`.
`HostPackageBuilderStalled` (monitor-bridge's `host_packages` row) pages on a condition that
persists, in two cases:

- the builder's last good fetch of master is older than 30 minutes, or its series is absent.
  That covers a dead `homelab-builder.timer`, a missing token and a fetch that keeps failing;
- a commit of master has waited more than 7200 seconds to be built, or 90060 seconds behind a gate
  whose canary is daniel-pi, which covers the Pi's longest gap between updater fires, a day on its
  daily schedule (#6919). The builder runs and exits 0 in each of these cases,
  so only the alert reports them.

The check's message names the `reason` and `sha` labels of
`homelab_builder_unbuilt_since_timestamp_seconds`, which name the case. The alert stays silent
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
