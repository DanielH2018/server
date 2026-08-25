---
name: ha-verify-state
description: Verify LIVE Home Assistant state correctly — whether an entity is live/stale, whether an automation loaded and fired, and the values it sees. Use after deploying an HA change, when debugging why an automation didn't fire, or any time you need ground-truth HA state. Encodes the recorder-DB and alias-slug traps so "looks done" matches "is done".
allowed-tools: Bash, Read
---

Ground-truth Home Assistant state, without falling into the traps that make a change *look*
applied when it isn't. **Prefer the live REST API over the recorder DB.** Run from `/home/ubuntu/server`.

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
