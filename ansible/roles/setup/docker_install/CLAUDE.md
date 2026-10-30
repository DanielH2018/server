# docker_install — the Pi's package owns Docker; the teardown half stays

The role keeps only its teardown half, which reaps a retired Docker install on the hosts with
`has_docker: false`. H7.26 moved the install half into `homelab-daniel-pi`. The role stays until
H7.20a, because `ansible/initial_setup.yml` names it and an edit to that playbook parks the
deployer. Repo-root `CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "docker_install"`
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

## Where the work went
`hosts/components/pi-docker/` ships every install step, and its `component.yml` header carries
the rules a session must not break. `docs/docker-engine-pins-and-runtime.md` has the 2026-09-18
incident, the pin derivations and the Go runtime measurements.

- The engine pins, rendered into `/usr/lib/homelab/docker-engine.pins`, and
  `homelab-docker-engine.service`, the one path that installs or moves the held engine. An
  operator starts it from a LAN session: `sudo systemctl start homelab-docker-engine.service`.
- `daemon.json`, the two Go-runtime drop-ins and containerd's config block, each declared
  `manual:` so no package install restarts a daemon.
- The external `proxy` and `lifecycle` networks. `homelab-compose.service` creates each one the
  project names, through an `ExecStartPre=` the renderer derives from the compose files
  (`hosts/components/pi-compose/units/homelab-compose.service.j2`).
- The Docker apt source and key, in `hosts/components/apt-docker/` since H7.24a.

## What stayed behind
- The LAN's ufw rules for the dockerd and containerd metrics listeners stay Ansible-era rules
  until H7.30 declares the Pi's ufw rules.
- No task adds the connecting user to the `docker` group. The one Pi heartbeat that runs
  `docker`, `homelab-pi-recovery.service`, grants the group itself with
  `SupplementaryGroups=docker`.
- `initial_setup`'s `host-basics.yml` still holds the engine packages before its dist-upgrade.

## Teardown (`tasks/teardown.yml`, `has_docker: false`)
Reaps what an imperative Docker uninstall leaves behind: any `docker-compose-*.service` unit
and the crons in `docker_install_stale_crons`, all `state: absent`, so it is a no-op on a host
that never had Docker. It unholds the engine packages before it purges them, because apt with
`-y` refuses to change a held package. **Not covered, deliberately:** `/var/lib/docker` and the
rendered `containers/` tree, because those hold data.

## Rollback
A revert of H7.26 restores the install half, and the operator then re-applies the role from a
node over ssh after `homelab_host_applied_info` shows the reverted version: `cd
/home/ubuntu/server && uv run ansible-playbook ansible/initial_setup.yml --tags docker_install
-e target=daniel-pi` (operator-only). The engine never moved, so the revert leaves it held at
the version it runs.
