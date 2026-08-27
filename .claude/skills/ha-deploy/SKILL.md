---
name: ha-deploy
description: Deploy a Home Assistant config change and verify it actually took effect. Use after editing anything under ansible/roles/k8s/home-assistant/ (automations, scenes, scripts, templates, configuration). Deploys via Ansible, gates on container health, then confirms the changed automation/entity actually loaded — not just that the playbook ran.
allowed-tools: Bash, Glob
---

Deploy the `home-assistant` role and **prove the change took**. Run from `/home/ubuntu/server`.
**Since slice-5 B3 (2026-08-09) HA runs in the k3s cluster on `daniel-box`** — deploy from
daniel-box; the config-authoring files stay in `roles/k8s/home-assistant/`, and the
`--tags home-assistant` deploy ships them via the k8s role (ConfigMaps + Secret, then a
rollout restart). A restart is ~60-120s.

## Steps

1. **Validate first.** `uv run python scripts/home_assistant/validate_ha_config.py` (or rely on the
   `validate-ha-config` prek hook). If you touched a `custom_templates/*.jinja` macro, also
   `uv run pytest ansible/roles/k8s/home-assistant/tests`. Don't deploy a config that
   fails structural validation.

2. **A new config file must be added to the k8s role's ConfigMap + init-container install
   list** (`roles/k8s/home-assistant/templates/configmap.yaml.j2` + `deployment.yaml.j2`) —
   the manifests role rolls the deployment when the rendered ConfigMap/Secret changes, so
   already-listed files redeploy automatically; a file the ConfigMap doesn't carry silently
   never reaches `/config`.

3. **Deploy:**
   ```
   ./scripts/deploy.sh --tags "home-assistant"
   ```
   The wrapper takes `/var/lock/server-git-tree.lock` so the deploy can't interleave with
   gitops-deploy or another Claude session; **exit 75 means the lock stayed busy and nothing
   was deployed** — not a playbook failure. It runs `uv run ansible-playbook` underneath (bare
   `ansible-playbook` lacks the deps). Use `uv run ansible-playbook ansible/deploy.yml --tags
   "home-assistant" --check` for an unlocked dry run if the change is risky. There is no
   config-only mode for the k8s role: the config *is* the ConfigMap, so shipping it is the
   rollout.

4. **Gate on health:**
   ```
   kubectl -n homelab get pods -l app=home-assistant   # 1/1 Running
   ```
   The "could not validate that the sqlite3 database was shutdown
   cleanly" line on boot is **benign** (WAL auto-recovers) — not a deploy failure.

5. **Prove it loaded** (this is the step the generic `deploy` skill can't do). Use
   `ha-verify-state`:
   - **Assert ALL automations loaded** (not just one): `uv run python scripts/diagnostics/probe.py ha
     verify-automations` — exit 0 = every automation in `files/automations.yaml` is present in
     the live instance and not `unavailable`. A non-zero exit lists the dropped/errored ids
     (a schema error HA silently skipped at load). File-driven, so live `.storage`/UI cruft is
     ignored.
   - **Assert every referenced entity still EXISTS**: `uv run python scripts/diagnostics/probe.py ha
     verify-entities` — it diffs `state/external_entities.yml` against live HA and exits non-zero
     on anything that vanished. Run it every deploy, not only when you touched entities. Nothing
     else in this repo can see a disappearance: `validate_ha_config.py` resolves references against
     that snapshot, so a name that *stopped* existing reads exactly like one that resolves, and the
     prek hook goes green. On 2026-08-16 two Pixel sensors disappeared and three bedroom features
     sat inert behind a clean validation — `states()` on a missing entity renders `unknown`, which
     the automation's own exclusion list swallowed. When something does turn up dead, **fix the
     config first and refresh the snapshot second**: `refresh` alone drops the ids and makes the
     validator start failing on the still-present config refs, which is the desired signal, not a fix.
   - Edited an automation → `uv run python scripts/diagnostics/probe.py ha automation <id-or-alias>` —
     it must exist (resolves the alias-slug-vs-id trap) and, after you trigger it, `last_triggered`
     must advance.
   - Edited an entity/template → `uv run python scripts/diagnostics/probe.py ha state <entity_id>` —
     value present and `last_updated` newer than the container's `StartedAt`.
   - Suspect a render error → `uv run python scripts/diagnostics/probe.py ha get error_log`.

6. **Report** the deploy result, the health line, and the live load/fire evidence. If health
   fails or the automation didn't load, pull logs
   (`kubectl -n homelab logs deploy/home-assistant --tail=50` from daniel-box) before
   declaring success.

## Notes
- A newly added/renamed entity sits `unknown`/`unavailable` until its first report — re-check
  rather than treating it as a failed deploy (see `ha-verify-state`).
- Deploy from `daniel-box` (the cluster + age key live there). `probe.py ha` works from
  either host — it reaches HA via the bridge hostname, not a container inspect.
