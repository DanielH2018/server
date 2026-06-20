# Home Assistant engineering agent + skills + `probe.py ha`

**Date:** 2026-06-20
**Status:** approved (design), pre-implementation
**Origin:** operator request — "create an Agent for my HA instance to improve Claude when
working on it, and add any skills that could be useful."

## Problem

The bedroom HA suite is the most intricate part of the homelab: automations/scenes/scripts/
template-sensors deployed **via `copy` (not `template`)**, tunable math factored into
unit-tested `custom_templates/*.jinja` macros, a pre-deploy config validator, plus a thick
layer of hard-won **verification traps** (recorder WAL/immutable/stale-after-restart,
`automation.<alias-slug>` ≠ its `id`, Adaptive Lighting self-on, the Z2M-availability prereq).
Each new session re-derives these from `CLAUDE.md`, and the most expensive failure mode is
declaring HA work "done" when an automation silently didn't recreate or the recorder read was
stale. Nothing today lets Claude cheaply **check live HA state**.

## Goal

Encode the HA conventions + verification discipline into reusable Claude tooling so that
(a) HA work follows the copy-not-template / macro-and-test / verify-it-loaded path by default,
and (b) Claude can query live HA state through one allow-listed, read-only surface.

## Deliverables

Seven, all version-controlled in `.claude/`, `scripts/`, `ansible/`.

### 1. `claude_ha_token` secret — DONE (commit `95e34eb4`)

A dedicated HA long-lived access token (operator-minted in HA → Profile → Long-Lived Access
Tokens), encrypted in `ansible/vars/secrets.yml`, registered for rotation (`assisted` tier).
Separate from `monitor_bridge_ha_token` so either can be revoked without breaking the other.
Rotation = revoke + reissue in the HA UI. **Caveat:** an HA LLAT is admin-scoped (HA has no
token scoping), so the value *can* write — our tooling only ever issues GETs.

### 2. `scripts/probe.py ha` subcommand — the foundation

The single allow-listed surface the agent + `ha-verify-state` skill call. Covered by the
existing allow-list entry `Bash(uv run python scripts/probe.py:*)` — no new permission, no
prompt. **Read-only (GET only).**

Subcommands:
- `probe.py ha state <entity_id>` — `GET /api/states/<entity_id>`; prints `state` + the salient
  attributes (`friendly_name`, `last_changed`, `last_updated`). `--json` prints raw.
- `probe.py ha automation <id-or-alias>` — resolves the **alias-slug-vs-id trap**: if the arg
  starts with `automation.` it's a direct `GET /api/states/<entity_id>`; otherwise it fetches
  `/api/states` and matches an `automation.*` entity whose `attributes.id == arg` OR whose
  `entity_id == "automation." + arg` OR whose slugified `friendly_name == arg`. Prints
  `state` (on/off), `last_triggered`, friendly name.
- `probe.py ha get <api-path>` — escape hatch: raw `GET /api/<api-path>` for anything else
  (e.g. `error_log`, `config`). Output passed through verbatim.

Implementation honoring probe.py's existing **pure/impure split**:
- **Pure + unit-tested** (added to the existing `scripts/test_probe.py`): the URL builders
  (`ha_state_url`, `ha_get_url`), `match_automation(states, query) -> obj|None`, the curl-argv
  builder (token-never-in-argv guard), and the output formatters.
- **Impure** `run_ha(...)` runtime path, parallel to the existing `run_health(...)` (so
  `plan()` stays token-free): resolves the HA IP via the existing `resolve_ip("home-assistant")`,
  decrypts the token via `sops -d --extract '["claude_ha_token"]' <repo>/ansible/vars/secrets.yml`
  (path derived from `__file__`), and curls with the bearer header fed through **stdin
  `curl --config -`** so the token never enters argv/`ps`.
- `--dry-run` prints the command with the token **redacted**.
- Update the module docstring's Subcommands list. Failure modes surface clearly: missing age
  key → sops error; HA container down → the existing `resolve_ip` "no container IP" error.

### 3. Agent `home-assistant-engineer` (read+write)

`.claude/agents/home-assistant-engineer.md`. Frontmatter: `model: inherit` (does real
engineering on subtle logic — not downgraded), `tools: Read, Write, Edit, Grep, Glob, Bash`.
System prompt mirrors `homelab-network-diagnostician`'s shape (mental model → tools → method →
rules) but is read+write. It encodes:
- **Where to edit:** `templates/*.j2` (Ansible-rendered: `configuration.yaml`, compose, lovelace,
  customize) vs `files/` (deployed by `copy`, verbatim — automations, scenes, scripts, templates,
  `custom_templates/*.jinja`). The **copy-not-template** rule and *why* (HA `{{ }}` would break
  Ansible's templater). Never edit `containers/`.
- **The macro-and-test rule:** tunable math goes in a `custom_templates/*.jinja` macro (numbers
  in → number/bool out) with a test in `tests/`, imported from the YAML caller — never inlined.
- **`common_config_changed` wiring** so an edited file actually recreates HA.
- **The deploy→verify loop** and the **verification traps** (recorder WAL/immutable/stale-
  after-restart via `last_updated_ts` vs container `StartedAt`; alias-slug ≠ id; AL self-on at
  startup; Z2M-availability prereq for offline detection; banker's rounding in the Jinja harness).
- **Its tools:** the four skills below + `probe.py ha` for live state.
- **Rules:** validate before deploy; confirm an automation actually *loaded* (by alias-slug)
  before claiming success; don't re-flag intentional designs (Authelia-off, AL self-on, etc.).

### 4–7. Four skills (`.claude/skills/<name>/SKILL.md`)

Designed to **compose**, not duplicate. Format matches existing skills (`name`, `description`,
`allowed-tools` frontmatter).

- **`ha-edit-automation`** *(workflow)* — pick the right file → math into a tested
  `custom_templates/*.jinja` macro → wire `common_config_changed` if a new bind-mounted file →
  validate (`validate_ha_config.py` / the prek hook) → invoke `ha-deploy` → invoke
  `ha-verify-state`. The "do HA work correctly" entry point. `allowed-tools: Read, Edit, Write, Bash, Glob`.
- **`ha-verify-state`** *(reference + checklist)* — query live state via `probe.py ha`; the
  recorder WAL/immutable read trap; alias-slug vs id; live-vs-stale discrimination. Reused by
  the other two. `allowed-tools: Bash, Read`.
- **`z2m-device-setting`** — persist an Aqara/Hue device setting via
  `docker exec -i mosquitto mosquitto_pub -t 'zigbee2mqtt/<dev>/set' -m '{...}'`; **not
  templated**; must re-apply after a re-pair; verify via the device's reported state.
  `allowed-tools: Bash`.
- **`ha-deploy`** *(thin)* — `uv run ansible-playbook ansible/deploy.yml --tags home-assistant`
  → `probe.py health home-assistant` → hand to `ha-verify-state` to confirm the changed
  automation actually loaded. Composes the others rather than duplicating the generic `deploy`
  skill. `allowed-tools: Bash, Glob`.

### Wiring

Reference the agent + skills + `probe.py ha` in:
- `ansible/roles/containers/home-assistant/CLAUDE.md` (Testing / Editing sections).
- root `CLAUDE.md` `.claude/` tooling section (alongside the existing agent/skill list).

## Out of scope (YAGNI)

- No **write** capability in `probe.py` (GET only). Mutations go through Ansible deploy or the
  documented `mosquitto_pub` / HA-UI paths.
- No HA **schema / entity-existence** validation beyond what `validate_ha_config.py` already
  does (that needs `hass --script check_config` in a Docker HA image — separate effort).
- No changes to the running HA config itself.

## Testing

- `scripts/test_probe.py` — the new pure functions (URL builders, `match_automation`),
  matching probe.py's existing testable-pure design. Auto-collected by `uv run pytest`
  (`scripts/` is already in `pyproject.toml` `testpaths`).
- The skills/agent are markdown; "tested" by using them. `ha-verify-state` + `probe.py ha state`
  against a known live entity is the end-to-end smoke check once the token is in place.
