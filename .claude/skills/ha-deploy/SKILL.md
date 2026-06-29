---
name: ha-deploy
description: Deploy a Home Assistant config change and verify it actually took effect. Use after editing anything under ansible/roles/containers/home-assistant/ (automations, scenes, scripts, templates, configuration). Deploys via Ansible, gates on container health, then confirms the changed automation/entity actually loaded — not just that the playbook ran.
allowed-tools: Bash, Glob
---

Deploy the `home-assistant` role and **prove the change took**. Run from `/home/ubuntu/server`.
HA is on `daniel-server`; a recreate is ~120s.

## Steps

1. **Validate first.** `uv run python scripts/validate_ha_config.py` (or rely on the
   `validate-ha-config` prek hook). If you touched a `custom_templates/*.jinja` macro, also
   `uv run pytest ansible/roles/containers/home-assistant/tests`. Don't deploy a config that
   fails structural validation.

2. **Confirm `common_config_changed` is wired** for any bind-mounted file you edited — otherwise
   the deploy is idempotent (`recreate: auto`) and your edit won't recreate the container. The
   automations/scenes/scripts/templates/configuration tasks are already wired; a *new*
   bind-mounted file needs its config task `register:`ed and OR'd in (see the role `CLAUDE.md`).

3. **Deploy:**
   ```
   uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"
   ```
   (Always via `uv run` — bare `ansible-playbook` lacks the `community.docker` deps.) Use
   `--check` first for a dry run if the change is risky. For config-only changes that should NOT
   recreate the container, see the `--skip-tags deploy` note in repo-root `CLAUDE.md`.

4. **Gate on health** (allow-listed, no prompt):
   ```
   uv run python scripts/probe.py health home-assistant
   ```
   Exit 0 = running + healthy. The "could not validate that the sqlite3 database was shutdown
   cleanly" line on boot is **benign** (WAL auto-recovers) — not a deploy failure.

5. **Prove it loaded** (this is the step the generic `deploy` skill can't do). Use
   `ha-verify-state`:
   - **Assert ALL automations loaded** (not just one): `uv run python scripts/probe.py ha
     verify-automations` — exit 0 = every automation in `files/automations.yaml` is present in
     the live instance and not `unavailable`. A non-zero exit lists the dropped/errored ids
     (a schema error HA silently skipped at load). File-driven, so live `.storage`/UI cruft is
     ignored.
   - Edited an automation → `uv run python scripts/probe.py ha automation <id-or-alias>` —
     it must exist (resolves the alias-slug-vs-id trap) and, after you trigger it, `last_triggered`
     must advance.
   - Edited an entity/template → `uv run python scripts/probe.py ha state <entity_id>` —
     value present and `last_updated` newer than the container's `StartedAt`.
   - Suspect a render error → `uv run python scripts/probe.py ha get error_log`.

6. **Report** the deploy result, the health line, and the live load/fire evidence. If health
   fails or the automation didn't load, pull logs
   (`uv run python scripts/probe.py loki-query '{container="home-assistant"}'`
   or `docker logs --tail 50 home-assistant`) before declaring success.

## Notes
- A newly added/renamed entity sits `unknown`/`unavailable` until its first report — re-check
  rather than treating it as a failed deploy (see `ha-verify-state`).
- Deploy only from `daniel-server` (where HA + the age key live). `probe.py ha`/`health`
  inspect the local Docker daemon.
