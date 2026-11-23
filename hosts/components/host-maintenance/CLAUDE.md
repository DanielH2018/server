# `hosts/components/host-maintenance`

## Autonomous-role contract (each job runs with no human in the loop)

- **Scope / exclusions:** reboot this host, stage firmware updates, and remove orphaned packages and
  the config remnants of removed ones. None edits the repository, the cluster or another host.
  The systemd-units check only reads unit state, and the ucf-dist check only lists `/etc`.
- **Mode (explicit and reversible):** each timer's enable link arms it; dropping the component, or
  `has_fwupd` for the firmware update, stops the timer on the install that drops it.
- **Abort valves:** the reboot refuses on reconcile's validation flag.
- **Required evidence:** a `weekly-restart`, `apt-autoremove`, `dpkg-purge` or `fwupd-auto` line in
  the journal, the Healthchecks.io check for the reboot, and the `apt-autoremove`, `dpkg-purge`,
  `firmware-update`, `systemd-units` and `ucf-dist` textfiles.
