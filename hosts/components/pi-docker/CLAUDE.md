# `hosts/components/pi-docker`

## Autonomous-role contract (the timer moves the Pi's Docker engine with no human in the loop)

- **Scope:** the engine packages docker-engine.pins names, the hold on them, and the `homelab`
  Compose project's containers, which an upgrade stops and recreates. Nothing else.
- **Mode:** the timer's enable link in this package arms it. Dropping the link from a package
  version, or reverting this component, stops the timer on the install that drops it.
- **Authoritative sources:** docker-engine.pins, rendered from `defaults:` in `component.yml`, and dpkg's
  installed versions.
- **Abort valves:** every package at its pin exits 0 and changes nothing. A running updater or
  reconcile that outlasts the wait stops the run before the project stops, with the updater
  timer left masked.
- **Required evidence:** the unit's journal, and the DockerEnginePinUnapplied alert.
- **Next-run review:** after a failed run, read `journalctl -u homelab-docker-engine` on daniel-pi
  and unmask homelab-updater.timer once the cause is fixed.

## Autonomous-role contract (homelab-docker-prune removes Docker objects with no human in the loop)

- **Scope:** images, containers, networks and build cache that nothing uses and that were created
  more than a week ago. Nothing else.
- **Mode:** the timer's enable link in this package arms it, at the `docker_prune_cron_*` slot in
  defaults/main.yml. Dropping the link stops the timer on the install that drops it.
- **Abort valves:** a failed prune leaves its Healthchecks.io ping unsent.
- **Required evidence:** a `docker-prune` line in the journal and the `<host>-docker-prune`
  Healthchecks.io check.
