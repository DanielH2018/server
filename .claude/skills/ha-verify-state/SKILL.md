---
name: ha-verify-state
description: Deploy a Home Assistant config change and verify it actually took effect. Use after editing anything under ansible/roles/k8s/home-assistant/ (automations, scenes, scripts, templates, configuration), when debugging why an automation didn't fire, or any time you need ground-truth live HA state. Deploys via Ansible, gates on pod health, then confirms the changed automation/entity really loaded — and encodes the recorder-DB and alias-slug traps so "looks done" matches "is done".
allowed-tools: Bash, Read, Glob
---

Deploy an HA config change and **prove it took**, then ground-truth live state without falling
into the traps that make a change *look* applied when it isn't. Deploying without verifying is
not a supported path here, so the two halves are one skill. **Prefer the live REST API over the
recorder DB.** Run from `/home/ubuntu/server`.

## Deploy a config change

**Since slice-5 B3 (2026-08-09) HA runs in the k3s cluster on `daniel-box`** — deploy from
daniel-box; the config-authoring files stay in `roles/k8s/home-assistant/`, and the
`--tags home-assistant` deploy ships them via the k8s role (ConfigMaps + Secret, then a
rollout restart). A restart is ~60-120s.

1. **Validate first.** `uv run python scripts/home_assistant/validate_ha_config.py` (or rely on the
   `validate-ha-config` prek hook). If you touched a `custom_templates/*.jinja` macro, also
   `uv run pytest ansible/roles/k8s/home-assistant/tests`. Don't deploy a config that
   fails structural validation.

2. **A new config file must be added to its `home_assistant_*_files` list in the role's
   `defaults/main.yml`** — the ConfigMap loops over those lists and the init container installs
   whatever the ConfigMap holds. The manifests role rolls the deployment when the rendered
   ConfigMap/Secret changes, so already-listed files redeploy automatically; a file no list
   names never reaches `/config`, and `validate_ha_config.py` fails on the disagreement.

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

5. **Prove it loaded**, using the live-API commands below:
   - **Assert ALL automations loaded** (not just one): `uv run python scripts/diagnostics/probe.py ha
     verify-automations` — exit 0 = every automation in `files/automations/*.yaml` is present in
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
   - Edited an automation → `probe.py ha automation <id-or-alias>` must find it, and
     `last_triggered` must advance once you trigger it.
   - Edited an entity/template → `probe.py ha state <entity_id>` — value present and
     `last_updated` newer than the pod's start time.
   - Suspect a render error → `probe.py ha get error_log`.

6. **Report** the deploy result, the health line, and the live load/fire evidence. If health
   fails or the automation didn't load, pull logs
   (`kubectl -n homelab logs deploy/home-assistant --tail=50` from daniel-box) before
   declaring success.

## The live API (use this first)

`scripts/diagnostics/probe.py ha …` is read-only and allow-listed (no prompt); it queries HA's REST API
with the `claude_ha_token`:

- **Entity state:** `uv run python scripts/diagnostics/probe.py ha state <entity_id>`
  → current `state` + attributes + `last_changed`/`last_updated`. `--json` for raw.
- **Did an automation load / fire?** `uv run python scripts/diagnostics/probe.py ha automation <id-or-alias>`
  → on/off + `last_triggered`. **Pass the automation's `id` OR its alias-slug OR full
  `automation.<slug>` — the matcher resolves all three.** A non-zero exit + "not found" means it
  did NOT load (wrong file, validation skipped it, or it never deployed).
- **Why did it run but no-op?** `uv run python scripts/diagnostics/probe.py ha why <id-or-alias>` (alias `ha
  trace`) pulls the live per-condition trace — which condition blocked the last run. Caveat: traces
  are in-memory and wiped on every HA restart/deploy, and an automation whose trigger NEVER matched
  leaves no trace — for the "nothing happened" case use `ha get logbook/<entity>` + `last_triggered`.
- **Live error log:** `uv run python scripts/diagnostics/probe.py ha get error_log` — catches a template that
  parsed structurally but throws at render time, or an integration that failed to set up.

To confirm an automation *fired*: note `last_triggered`, cause the trigger, re-query, and check
it advanced. A loaded-but-never-fired automation has an old/`None` `last_triggered`.

## The traps (why this skill exists)

1. **alias-slug ≠ id.** An automation's `entity_id` is derived from its **alias** (slugified) at
   first creation, NOT its `id`. So `id: bedroom_fan_temperature` lives at
   `automation.bedroom_fan_temperature_control`. `probe.py ha automation` handles this; if you
   ever read `/api/states` or the recorder directly, match by the alias-slug or `attributes.id`,
   never assume `automation.<id>`.
2. **The recorder DB goes stale after a restart.** Verifying HA state by reading
   `config/home-assistant_v2.db` right after a deploy is **misleading** — entries can predate the
   restart. Discriminate **live vs removed/stale** by comparing the row's `last_updated_ts`
   against the pod's start time. HA runs in k3s on daniel-box since slice-5 B3, so that is
   `kubectl -n homelab get pod -l app=home-assistant -o jsonpath='{.items[0].status.startTime}'`
   — an entity whose newest row is older than that hasn't reported since the restart. (The DB
   itself lives on the Longhorn PVC now, not a host bind mount, so reaching it means going
   through the pod rather than reading a path on daniel-server.)
3. **The recorder needs its WAL.** If you must read the SQLite recorder, copy `*.db` **plus**
   `*.db-wal` and `*.db-shm` together — opening the `.db` alone (or `immutable=1`) gives a stale
   snapshot missing the most recent writes. A `null` context column does NOT mean "external" —
   custom integrations stamp their own context.
4. **New / Zigbee entities sit `unknown`/`unavailable` until their first report.** Right after a
   deploy or a fresh pairing, that's expected, not a fault. Re-check after the device reports
   (battery Zigbee can be tens of minutes via the Z2M passive timeout).

## When the API path is unavailable

`probe.py ha` needs a host age key to decrypt the token, so run it on daniel-server or
daniel-box — it reaches HA at the stable bridge URL `https://home-assistant.local.<domain>`,
which is the same endpoint before and after the cluster move. If HA is down, check first with
`uv run python scripts/diagnostics/probe.py health home-assistant` (k8s-native: gates on rollout completion
AND no container restart in the last 180s — catches a crashlooping pod that a bare rollout
check would miss). On failure, drill down with `kubectl -n homelab get pods -l
app=home-assistant` and `kubectl -n homelab logs deploy/home-assistant --tail=50`. The
recorder-DB rules above are the fallback for historical questions, with the WAL/start-time
caveats in mind.
