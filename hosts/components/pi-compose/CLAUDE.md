# `hosts/components/pi-compose`

## Autonomous-role contract (compose-adopt removes containers unattended)

- **Scope / exclusions:** compose-adopt removes a container only when its name is a service the
  merged project declares and its com.docker.compose.project label is not `homelab`. It touches
  no other container, image, volume or network. compose-run's `up --remove-orphans` removes a
  `homelab` container that the project no longer declares, and nothing from another project.
- **Mode (explicit and reversible):** the enable link in `component.yml` arms the unit. Reconcile's
  step 2 starts an enabled unit that is not active, and the restart set restarts only an
  enabled or active one. Dropping the link from a later package version stops the boot start,
  and `sudo systemctl mask homelab-compose.service` stops every other start until it is
  unmasked. Neither stops a container, because the unit has no ExecStop=.
- **Authoritative sources:** `docker compose config --services` for the names, and `docker ps -a`
  for each container's project label, read before any removal.
- **Required evidence, every run:** one `logger -t compose-adopt` line for each removal, naming the
  container and its old project, and homelab_compose.prom in node_exporter's textfile
  directory with the result, the package's SHA and the run's duration.
- **Next-run review:** a removal line after the first run that moved a service means a container
  keeps returning under another project. Find the starter before widening anything here.

ENFORCED: hosts/lib/tests/test_pi_compose_scripts.py (the stub-docker cases for compose-run and
compose-adopt), hosts/lib/tests/test_pi_compose_unit.py (the unit's timeout and restart contract).
