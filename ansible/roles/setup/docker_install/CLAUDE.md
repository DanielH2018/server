# docker_install — an emptied role; the host packages own Docker and its teardown

`tasks/main.yml` is an `end_role` stub, like `claude_code`'s. H7.26 moved the install half into
`homelab-daniel-pi`, and H7.48 moved the teardown half into the cluster nodes' legacy lists. The
role stays until H7.20a, because `ansible/initial_setup.yml` names it and an edit to that
playbook parks the deployer. Repo-root `CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "docker_install"`; ends on hosts where `True`
  (leading `meta: end_role`)
- **Crons / timers:** none (the role ends on every host before its first task)
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
- The LAN's ufw rules for the dockerd and containerd metrics listeners, declared in
  `hosts/daniel-pi/package.yml` since H7.30. Reconcile's ufw arm relabelled the Ansible-era rules
  in place.

## What stayed behind
- No task adds the connecting user to the `docker` group. The one Pi heartbeat that runs
  `docker`, `homelab-pi-recovery.service`, grants the group itself with
  `SupplementaryGroups=docker`.
- No setup task holds the engine: H7.60 deleted `initial_setup`'s hold with the dist-upgrade it
  guarded. `homelab-docker-engine.service` holds the packages when it installs them.

## Teardown, as legacy entries (H7.48)
`hosts/daniel-box/package.yml` and `hosts/daniel-server/package.yml` list what the teardown
half reaped and reconcile can still retire: the two crons of retired Compose services
(`Redeploy ical-proxy`, `Clean stale tdarr transcode cache`) and the Go-runtime drop-ins with
their `*.service.d` directories. The glob over `docker-compose-*.service`, the unhold and the
apt purge have no legacy kind. They went because both nodes measured clean on 2026-11-09: no
dockerd binary, no `docker-compose-*.service`, and `dpkg -l 'docker*' containerd.io` found
no package. A node that gains Docker again is rebuilt, not torn down. **Never touched:**
`/var/lib/docker` and the rendered `containers/` tree, because those hold data.

## Rollback
A revert of H7.26 restores the install half, and the operator then re-applies the role from a
node over ssh after `homelab_host_applied_info` shows the reverted version: `cd
/home/ubuntu/server && uv run ansible-playbook ansible/initial_setup.yml --tags docker_install
-e target=daniel-pi` (operator-only). The engine never moved, so the revert leaves it held at
the version it runs.
