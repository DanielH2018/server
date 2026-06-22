# HA `system_log_event` ⇒ `fire_event` Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fail `validate-ha-config` when an automation triggers on `system_log_event` but `configuration.yaml` does not set `system_log: fire_event: true` (the event never fires by default → trigger silently dead).

**Architecture:** One pure function in `scripts/ha_state_model.py`, wired into `check_errors` next to the existing deterministic checks. Structured-data only (no Jinja/string parsing). Runs in the existing `validate-ha-config` prek hook (local + CI).

**Tech Stack:** Python 3 (stdlib), `uv run pytest`, the `ha_state_model` checks consumed by `validate_ha_config.py`.

## Global Constraints

- **Structured-data check only** — operate on the loaded `config` dict (`config['automation']`, `config['system_log']`); no Jinja/string/regex parsing.
- **Scope = the one pair** `system_log_event` ⇒ `system_log.fire_event` (YAGNI; no general framework).
- **Accept only the canonical `True`** — `fire_event: true` parses to Python `True` via `HAConfigLoader`; `False`/missing/absent all fail.
- **Must stay GREEN on the real role** — the shipped config has `ha_runtime_error_alert` (a `system_log_event` trigger) AND `system_log: fire_event: true`, so `ha_state_model.py check` must remain clean (pure future-tightening).
- **Handle trigger shape variation** — `auto['trigger']` may be a single dict or a list; a trigger's `event_type` may be a `str` or a `list`.

---

### Task 1: `system_log_fire_event_errors` check + wiring + tests

**Files:**
- Modify: `scripts/ha_state_model.py` — add the function; wire it into `check_errors` (`:609-626`)
- Test: `scripts/test_ha_state_model.py`

**Interfaces:**
- Consumes: the `config` dict already loaded by `check_errors` (via `load_role`).
- Produces: `system_log_fire_event_errors(config: dict) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

Add to `scripts/test_ha_state_model.py` (the file imports the module as `hsm`):

```python
def test_system_log_fire_event_required_when_triggered_and_missing():
    config = {"automation": [{"id": "ha_runtime_error_alert",
                              "trigger": [{"platform": "event", "event_type": "system_log_event"}]}]}
    errs = hsm.system_log_fire_event_errors(config)
    assert len(errs) == 1
    assert "ha_runtime_error_alert" in errs[0] and "fire_event" in errs[0]


def test_system_log_fire_event_clean_when_enabled():
    config = {"system_log": {"fire_event": True},
              "automation": [{"id": "a",
                              "trigger": [{"platform": "event", "event_type": "system_log_event"}]}]}
    assert hsm.system_log_fire_event_errors(config) == []


def test_system_log_fire_event_flags_when_false():
    config = {"system_log": {"fire_event": False},
              "automation": [{"id": "a", "trigger": [{"event_type": "system_log_event"}]}]}
    assert len(hsm.system_log_fire_event_errors(config)) == 1


def test_system_log_fire_event_clean_without_trigger():
    config = {"automation": [{"id": "a", "trigger": [{"platform": "state", "entity_id": "x"}]}]}
    assert hsm.system_log_fire_event_errors(config) == []


def test_system_log_fire_event_handles_list_event_type():
    config = {"system_log": {"fire_event": True},
              "automation": [{"id": "a", "trigger": [{"event_type": ["other", "system_log_event"]}]}]}
    assert hsm.system_log_fire_event_errors(config) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest scripts/test_ha_state_model.py -k system_log_fire_event -v`
Expected: FAIL — `AttributeError: module 'ha_state_model' has no attribute 'system_log_fire_event_errors'`.

- [ ] **Step 3: Add the function**

Add to `scripts/ha_state_model.py` (place it near `single_writer_errors`, before `check_errors`):

```python
def system_log_fire_event_errors(config: dict) -> list[str]:
    """HARD: an automation that triggers on `system_log_event` requires `system_log: fire_event:
    true` in configuration.yaml. default_config enables system_log WITHOUT it, so the event never
    fires by default and the trigger never matches (the automation is silently dead). Structured-
    data check — no Jinja/string parsing. (Found the hard way via the ha_runtime_error_alert
    live-fire; this turns it into a pre-deploy gate.)"""
    offenders = []
    for auto in config.get("automation") or []:
        trig = auto.get("trigger") or auto.get("triggers") or []
        if isinstance(trig, dict):
            trig = [trig]
        for t in trig:
            if not isinstance(t, dict):
                continue
            et = t.get("event_type")
            ets = [et] if isinstance(et, str) else (et if isinstance(et, list) else [])
            if "system_log_event" in ets:
                offenders.append(auto.get("id") or auto.get("alias") or "<unknown>")
                break
    if not offenders:
        return []
    if ((config.get("system_log") or {}).get("fire_event")) is True:
        return []
    return [f"automation(s) {sorted(set(offenders))} trigger on system_log_event but "
            f"configuration.yaml does not set `system_log: fire_event: true` — system_log does not "
            f"fire that event by default, so the trigger never matches (silently dead). Add a "
            f"top-level `system_log:` block with `fire_event: true`."]
```

- [ ] **Step 4: Wire it into `check_errors`**

In `check_errors` (`scripts/ha_state_model.py`), add one line after the `single_writer_errors` line:

```python
    errs += single_writer_errors(model["writes"], load_sanctioned_writers())
    errs += system_log_fire_event_errors(config)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest scripts/test_ha_state_model.py -k system_log_fire_event -v`
Expected: all 5 PASS.

- [ ] **Step 6: Verify the real role stays green**

Run: `uv run python scripts/ha_state_model.py check`
Expected: exit 0, no `system_log_event` error (the shipped config has the trigger AND `fire_event: true`).

- [ ] **Step 7: Run the full scripts suite**

Run: `uv run pytest scripts -q`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add scripts/ha_state_model.py scripts/test_ha_state_model.py
git commit -m "feat(ha-state): check system_log_event triggers require system_log.fire_event: true"
```

---

## Notes for the executor

- No deploy needed — this is validation code only (runs in the prek hook).
- If executed via subagent-driven-development: the implementer reports only; the controller runs the gate and commits explicit paths.
