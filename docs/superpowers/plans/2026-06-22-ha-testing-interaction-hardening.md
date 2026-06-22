# HA Testing & Interaction Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic, lightweight reference- and contract-integrity validation for the Home Assistant config, make the most-edited decision logic testable, and add a per-condition automation-trace diagnostic — all without Docker, flaky gates, or new heavy dependencies.

**Architecture:** Four independent components. (1) Extend `scripts/ha_state_model.py` (the existing pure-Python state-model checker) with service-reference resolution + a mediator-`reason` contract check, fed by a live-HA service snapshot mirroring the existing entity snapshot. (2) Extract one decision macro (`natural_exception`) into `lighting.jinja` with a truth-table test. (3) A grep-based guard that every Jinja macro has a test. (4) A read-only WebSocket trace puller in `scripts/probe.py` (`ha why`).

**Tech Stack:** Python 3 (stdlib only — `socket`/`base64`/`struct` for WebSocket, no `websockets` dep), PyYAML (`HAConfigLoader`, a YAML-1.1 SafeLoader), Jinja2 (HA custom_templates), pytest, prek, `uv`.

## Global Constraints

- **Run everything via `uv run`** — `uv run python …`, `uv run pytest …`. The repo is a uv virtual project.
- **No Docker, no GitHub-CI-heavy steps.** All new checks are pure-Python and ride the existing fast `validate-ha-config` prek hook + `pytest` hook (sub-second).
- **No new third-party dependency.** The WebSocket client is hand-rolled on the stdlib (`socket`, `base64`, `struct`, `hashlib`). Do NOT add `websockets`/`websocket-client`.
- **`probe.py` HA access is read-only.** The trace subcommand may send ONLY `auth`, `trace/list`, `trace/get` WS messages — never a free-form command from an argument.
- **`containers/` is read-only generated output.** Edit `ansible/roles/containers/home-assistant/files/…`, never `containers/…`.
- **Config is loaded through `HAConfigLoader` (PyYAML SafeLoader, YAML 1.1):** an unquoted `off`/`on`/`yes`/`no` is a Python `bool`. The mediator-`reason` check relies on this.
- **`check_errors()` must stay GREEN on the real role.** The test `test_check_errors_on_real_role_is_clean_after_generate` runs every hard check against the actual config; every new check must pass it.
- **Mirror existing test style** (`scripts/test_ha_state_model.py`, `tests/test_lighting_macros.py`): hermetic, fixture-based, no live HA/network in unit tests.
- **One focused commit per task.** Commit explicit paths (a second Claude session may be live — see the concurrent-session hazard).
- **Commit message trailer:** end every commit message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File map

- `scripts/ha_state_model.py` — MODIFY: add service snapshot + 2 new checks (Tasks 1–3).
- `scripts/test_ha_state_model.py` — MODIFY: tests for the above.
- `ansible/roles/containers/home-assistant/state/external_services.yml` — CREATE: committed live service snapshot (Task 1, via `refresh`).
- `ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja` — MODIFY: add `natural_exception` (Task 4).
- `ansible/roles/containers/home-assistant/files/scripts.yaml` — MODIFY: refactor `bedroom_apply_natural` to use `natural_exception` (Task 4).
- `ansible/roles/containers/home-assistant/tests/test_lighting_macros.py` — MODIFY: `natural_exception` truth table (Task 4).
- `ansible/roles/containers/home-assistant/tests/test_macro_coverage.py` — CREATE: the macro-test guard (Task 5).
- `scripts/probe.py` — MODIFY: WS client + trace parser + `ha trace`/`ha why` subcommand (Tasks 6–7).
- `scripts/test_probe.py` — MODIFY: codec + parser tests (Tasks 6–7).
- `ansible/roles/containers/home-assistant/CLAUDE.md` — MODIFY: document the new gate, the decision-macro convention, and `ha why` (Tasks 3, 4, 7).
- `.claude/skills/ha-verify-state/SKILL.md` — MODIFY: route "why didn't it fire" through `ha why` (Task 7).

---

## Task 1: Live service snapshot infrastructure

**Files:**
- Modify: `scripts/ha_state_model.py` (add near `EXTERNAL_YAML`, `:292`; extend `cmd_refresh`, `:344`)
- Test: `scripts/test_ha_state_model.py`
- Create (via `refresh`): `ansible/roles/containers/home-assistant/state/external_services.yml`

**Interfaces:**
- Produces: `parse_services(api_services: list) -> set[str]`, `config_services(config: dict) -> set[str]`, `load_external_services() -> set[str]`, `EXTERNAL_SERVICES_YAML: Path`, and `cmd_refresh(get_states=None, get_services=None) -> int`.

- [ ] **Step 1: Write the failing tests**

Add to `scripts/test_ha_state_model.py`:

```python
def test_parse_services_flattens_domains():
    api = [{"domain": "notify", "services": {"mobile_app_x": {}, "persistent_notification": {}}},
           {"domain": "light", "services": {"turn_on": {}, "turn_off": {}}}]
    assert hsm.parse_services(api) == {
        "notify.mobile_app_x", "notify.persistent_notification",
        "light.turn_on", "light.turn_off"}


def test_config_services_registers_each_script():
    config = {"script": {"bedroom_lights_set": {}, "bedroom_blip": {}}}
    assert hsm.config_services(config) == {"script.bedroom_lights_set", "script.bedroom_blip"}


def test_cmd_refresh_writes_both_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(hsm, "STATE_DIR", tmp_path)
    monkeypatch.setattr(hsm, "EXTERNAL_YAML", tmp_path / "external_entities.yml")
    monkeypatch.setattr(hsm, "EXTERNAL_SERVICES_YAML", tmp_path / "external_services.yml")
    rc = hsm.cmd_refresh(
        get_states=lambda: ["light.bedroom_lights", "sensor.outdoor_pm2_5"],
        get_services=lambda: {"notify.mobile_app_pixel_watch_3", "light.turn_on"})
    assert rc == 0
    saved = yaml.safe_load((tmp_path / "external_services.yml").read_text())
    assert "notify.mobile_app_pixel_watch_3" in saved["services"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest scripts/test_ha_state_model.py -k "parse_services or config_services or cmd_refresh_writes" -v`
Expected: FAIL — `AttributeError: module 'ha_state_model' has no attribute 'parse_services'`.

- [ ] **Step 3: Add the helpers + extend `cmd_refresh`**

In `scripts/ha_state_model.py`, add after the `EXTERNAL_YAML = STATE_DIR / "external_entities.yml"` line (`:292`):

```python
EXTERNAL_SERVICES_YAML = STATE_DIR / "external_services.yml"


def parse_services(api_services: list) -> set[str]:
    """Flatten HA's GET /api/services (a list of {domain, services: {name: ...}}) into a flat
    {f"{domain}.{name}"} set."""
    out: set[str] = set()
    for block in api_services or []:
        domain = block.get("domain")
        if not domain:
            continue
        for name in (block.get("services") or {}):
            out.add(f"{domain}.{name}")
    return out


def config_services(config: dict) -> set[str]:
    """Services the config itself defines: every script registers `script.<name>`. This is the
    freshness escape-hatch so a brand-new script (not yet in the committed snapshot) resolves."""
    return {f"script.{name}" for name in (config.get("script") or {})}


def load_external_services() -> set[str]:
    if not EXTERNAL_SERVICES_YAML.is_file():
        return set()
    return set(yaml.safe_load(EXTERNAL_SERVICES_YAML.read_text()).get("services", []))
```

Then replace `cmd_refresh` (`:344-361`) with:

```python
def cmd_refresh(get_states=None, get_services=None) -> int:
    """Snapshot live external entity ids + the live service registry into external_entities.yml /
    external_services.yml. get_states/get_services are injected for tests; both default to live HA
    (needs the host age key + a running HA, present on daniel-server)."""
    if get_states is None or get_services is None:
        import json
        import probe
        ip = probe.resolve_ip(probe.HA_CONTAINER)
        token = probe.ha_token()
    if get_states is None:
        live = [s["entity_id"] for s in json.loads(
            probe.ha_get(probe.ha_get_url(ip, "states"), token))]
    else:
        live = list(get_states())
    if get_services is None:
        services = parse_services(json.loads(
            probe.ha_get(probe.ha_get_url(ip, "services"), token)))
    else:
        services = set(get_services())
    config = load_role()
    derived = config_entities(config, config.get("scene") or [])
    external = sorted(e for e in live if e not in derived)
    external_services = sorted(services - config_services(config))
    STATE_DIR.mkdir(exist_ok=True)
    EXTERNAL_YAML.write_text(_GENERATED_BANNER + _dump_yaml({"entities": external}))
    EXTERNAL_SERVICES_YAML.write_text(
        _GENERATED_BANNER + _dump_yaml({"services": external_services}))
    print(f"snapshotted {len(external)} external entities + {len(external_services)} services")
    return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest scripts/test_ha_state_model.py -k "parse_services or config_services or cmd_refresh_writes" -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Generate the real snapshot against live HA**

This runs on daniel-server (live HA + SOPS age key present).
Run: `uv run python scripts/ha_state_model.py refresh`
Expected: `snapshotted N external entities + M services` (M ≈ a few hundred), and `git status` shows a new `ansible/roles/containers/home-assistant/state/external_services.yml` plus an updated `external_entities.yml`.

- [ ] **Step 6: Commit**

```bash
git add scripts/ha_state_model.py scripts/test_ha_state_model.py \
  ansible/roles/containers/home-assistant/state/external_services.yml \
  ansible/roles/containers/home-assistant/state/external_entities.yml
git commit -m "feat(ha-state): snapshot the live service registry in refresh

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Service-reference resolution check

**Files:**
- Modify: `scripts/ha_state_model.py` (add `referenced_services`/`service_resolution_errors`; wire into `check_errors` `:516`; comment on `referenced_entities` `:316`)
- Test: `scripts/test_ha_state_model.py`

**Interfaces:**
- Consumes: `_all_service_calls`, `call_service`, `_is_templated`, `load_external_services`, `config_services` (Task 1).
- Produces: `referenced_services(config: dict) -> set[str]`, `service_resolution_errors(config: dict, known_services: set[str]) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

Add to `scripts/test_ha_state_model.py`:

```python
def test_referenced_services_collects_literals_skips_templated():
    config = {"automation": [{"id": "a", "alias": "A", "action": [
        {"service": "notify.mobile_app_x"},
        {"service": "{{ 'light.' ~ 'turn_on' }}"},
        {"service": "scene.turn_on", "target": {"entity_id": "scene.x"}}]}], "script": {}}
    assert hsm.referenced_services(config) == {"notify.mobile_app_x", "scene.turn_on"}


def test_service_resolution_flags_unknown_in_any_domain():
    # notify is NOT a managed entity-domain, but a typo'd notify SERVICE must still be caught
    # (the documented notify.pixel_watch_3 service_not_found bug).
    config = {"automation": [{"id": "a", "alias": "A", "action": [
        {"service": "notify.pixel_watch_3"}]}], "script": {}}
    known = {"notify.mobile_app_pixel_watch_3", "light.turn_on"}
    errs = hsm.service_resolution_errors(config, known)
    assert any("notify.pixel_watch_3" in e for e in errs)


def test_service_resolution_resolves_config_script_via_known():
    config = {"automation": [{"id": "a", "alias": "A", "action": [
        {"service": "script.bedroom_blip"}]}], "script": {"bedroom_blip": {"sequence": []}}}
    known = hsm.config_services(config)   # the freshness hatch resolves a brand-new script
    assert hsm.service_resolution_errors(config, known) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest scripts/test_ha_state_model.py -k "referenced_services or service_resolution" -v`
Expected: FAIL — `AttributeError: ... 'referenced_services'`.

- [ ] **Step 3: Add the functions + the 1a non-goal comment**

In `scripts/ha_state_model.py`, add after `resolution_errors` (`:335`):

```python
def referenced_services(config: dict) -> set[str]:
    """Every literal service id called in automations + scripts. Skips templated names (no dot ->
    call_service already returns None; a `domain.{{ }}` form is caught by the _is_templated guard)."""
    return {svc for call in _all_service_calls(config)
            if (svc := call_service(call)) and not _is_templated(svc)}


def service_resolution_errors(config: dict, known_services: set[str]) -> list[str]:
    """A called service absent from `known_services` (= a typo or a stale snapshot — run `refresh`).
    Unlike resolution_errors (entities), this checks EVERY domain unconditionally — there is no
    _MANAGED_DOMAINS gate. The live /api/services snapshot is complete, so an un-enumerable domain
    like `notify` no longer has to be exempted; that is exactly how `notify.<typo>` is caught."""
    errs = []
    for svc in sorted(referenced_services(config)):
        if svc not in known_services:
            errs.append(f"unresolved service call: {svc} "
                        f"(typo, or run `ha_state_model.py refresh` if it is a new integration)")
    return errs
```

Add a clarifying comment above `referenced_entities` (`:316`), documenting the deferred non-goal (component 1a):

```python
def referenced_entities(config: dict) -> set[str]:
    """Write targets + every structural `entity_id:` field (triggers/conditions/actions) across
    automations and scripts. Templated values are dropped (can't be resolved statically).
    NON-GOAL (deliberate): entity ids inside `{{ }}` template bodies (states('sensor.x')) are NOT
    extracted — that is regex-fragile and would make this the flaky part of the gate."""
```

Wire both new checks' service half into `check_errors` — change the body of `check_errors` (`:516`) so the `known` block and the error aggregation include services. Replace:

```python
    known = config_entities(config, config.get("scene") or []) | load_external_entities()
    errs: list[str] = []
    errs += freshness_errors(role_dir)
    errs += resolution_errors(config, known)
```

with:

```python
    known = config_entities(config, config.get("scene") or []) | load_external_entities()
    known_services = config_services(config) | load_external_services()
    errs: list[str] = []
    errs += freshness_errors(role_dir)
    errs += resolution_errors(config, known)
    errs += service_resolution_errors(config, known_services)
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `uv run pytest scripts/test_ha_state_model.py -k "referenced_services or service_resolution" -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Verify the check is GREEN on the real role**

Run: `uv run python scripts/ha_state_model.py check`
Expected: `HA state-model OK`.
If it reports `unresolved service call: <svc>` for a service that genuinely exists, the snapshot is stale — re-run `uv run python scripts/ha_state_model.py refresh`, re-add `external_services.yml`, and re-run `check`.

- [ ] **Step 6: Run the full real-role gate test**

Run: `uv run pytest scripts/test_ha_state_model.py::test_check_errors_on_real_role_is_clean_after_generate -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add scripts/ha_state_model.py scripts/test_ha_state_model.py
git commit -m "feat(ha-state): resolve every service/action call against the live snapshot

Checks all domains (no _MANAGED_DOMAINS gate) so a typo'd notify.<x> is
caught — the service_not_found bug class.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Mediator `reason` contract check

**Files:**
- Modify: `scripts/ha_state_model.py` (add `MEDIATOR_REASONS`/`mediator_reason_errors`; wire into `check_errors`)
- Test: `scripts/test_ha_state_model.py`
- Modify: `ansible/roles/containers/home-assistant/CLAUDE.md` (note the contract in the mediator bullet)

**Interfaces:**
- Consumes: `_all_service_calls`, `call_service`.
- Produces: `MEDIATOR_REASONS: dict[str, set[str]]`, `mediator_reason_errors(config: dict) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

Add to `scripts/test_ha_state_model.py`:

```python
def _lights_set_call(extra):
    # `extra` is merged into the service-call dict, e.g. {"data": {"reason": "presence"}}
    return {"automation": [{"id": "a", "alias": "A", "action": [
        {"service": "script.bedroom_lights_set", **extra}]}], "script": {}}


def test_mediator_reason_valid_passes():
    assert hsm.mediator_reason_errors(_lights_set_call({"data": {"reason": "presence"}})) == []


def test_mediator_reason_out_of_vocab_fails():
    errs = hsm.mediator_reason_errors(_lights_set_call({"data": {"reason": "naturl"}}))
    assert any("naturl" in e for e in errs)


def test_mediator_reason_missing_data_block_fails():
    errs = hsm.mediator_reason_errors(_lights_set_call({}))
    assert any("script.bedroom_lights_set" in e for e in errs)


def test_mediator_reason_yaml_bool_off_fails():
    # unquoted `off` in YAML 1.1 loads as Python False -> caught as not-a-string.
    errs = hsm.mediator_reason_errors(_lights_set_call({"data": {"reason": False}}))
    assert any("invalid reason" in e for e in errs)


def test_mediator_reason_fan_quoted_off_passes():
    config = {"automation": [], "script": {"s": {"sequence": [
        {"service": "script.bedroom_fan_set", "data": {"reason": "off"}}]}}}
    assert hsm.mediator_reason_errors(config) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest scripts/test_ha_state_model.py -k mediator_reason -v`
Expected: FAIL — `AttributeError: ... 'mediator_reason_errors'`.

- [ ] **Step 3: Add the constant + the check**

In `scripts/ha_state_model.py`, add after `service_resolution_errors`:

```python
# Valid `reason` vocabulary per actuator mediator. Declared here (NOT regex-derived from
# light_decision's Jinja / bedroom_fan_set's choose:) — a drifted constant fails SAFE (a newly
# added valid reason false-fails loudly until added here), never silently passes. Mirror any
# change to lighting.jinja's light_decision / files/scripts.yaml's bedroom_fan_set.
MEDIATOR_REASONS = {
    "script.bedroom_lights_set": {"presence", "natural", "wake", "off"},
    "script.bedroom_fan_set": {"auto", "boost", "off"},
}


def mediator_reason_errors(config: dict) -> list[str]:
    """HARD: every call to an actuator mediator passes a `reason` that is a STRING in the mediator's
    vocabulary. Catches a missing data:/reason, a typo, and the unquoted `reason: off` -> YAML
    `false` -> silent no-op trap (the config is loaded YAML-1.1, so `off` is already a bool here)."""
    errs = []
    for call in _all_service_calls(config):
        svc = call_service(call)
        if svc not in MEDIATOR_REASONS:
            continue
        reason = (call.get("data") or {}).get("reason")
        if not isinstance(reason, str) or reason not in MEDIATOR_REASONS[svc]:
            errs.append(f"{svc}: invalid reason {reason!r} — must be a quoted string in "
                        f"{sorted(MEDIATOR_REASONS[svc])} (unquoted off/on becomes a YAML bool)")
    return errs
```

Wire it into `check_errors` — after the `service_resolution_errors` line added in Task 2, add:

```python
    errs += mediator_reason_errors(config)
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `uv run pytest scripts/test_ha_state_model.py -k mediator_reason -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Verify GREEN on the real role**

Run: `uv run python scripts/ha_state_model.py check`
Expected: `HA state-model OK`.
If a real call site is flagged for a legitimate reason not in the vocabulary, that reason is missing from `MEDIATOR_REASONS` — add it there (the declared source of truth) and re-run.

- [ ] **Step 6: Document the contract in CLAUDE.md**

In `ansible/roles/containers/home-assistant/CLAUDE.md`, in the "Light/fan mediator" bullet, after the sentence ending `…declare it in `sanctioned_writers.yml`.`, add:

```markdown
  The mediator's `reason` is contract-checked by `validate-ha-config` (`mediator_reason_errors`
  in `ha_state_model.py`): every `bedroom_lights_set`/`bedroom_fan_set` call must pass a quoted
  `reason` from the declared vocabulary (`MEDIATOR_REASONS`) — a missing/typo'd reason or the
  unquoted-`off`→YAML-`false` no-op fails CI. Add a new reason to `MEDIATOR_REASONS` when you add
  one to the mediator.
```

- [ ] **Step 7: Commit**

```bash
git add scripts/ha_state_model.py scripts/test_ha_state_model.py \
  ansible/roles/containers/home-assistant/CLAUDE.md
git commit -m "feat(ha-state): contract-check mediator reason (vocab + quoted-string)

Catches a missing/typo'd reason and the unquoted reason: off -> YAML false
silent no-op, via a declared MEDIATOR_REASONS constant.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `natural_exception` macro + caller refactor

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja` (add macro after `light_decision`, `:75`)
- Test: `ansible/roles/containers/home-assistant/tests/test_lighting_macros.py`
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml` (`bedroom_apply_natural`, `:134-178`)
- Modify: `ansible/roles/containers/home-assistant/CLAUDE.md` ("Testing" section)

**Interfaces:**
- Produces: Jinja macro `natural_exception(sleep_mode, hour, in_window) -> 'nightlight'|'wake'|'default'` in `lighting.jinja`.

- [ ] **Step 1: Write the failing test**

Add to `ansible/roles/containers/home-assistant/tests/test_lighting_macros.py`:

```python
def _exception(sleep_mode, hour, in_window):
    return render_macro(LIGHT, "natural_exception", sleep_mode, hour, in_window)


def test_natural_exception_selection():
    assert _exception(True, 23, False) == "nightlight"   # sleep mode, outside window
    assert _exception(False, 3, False) == "nightlight"   # deep night 00:00-05:00
    assert _exception(False, 12, False) == "default"     # daytime, no exception
    assert _exception(False, 7, True) == "wake"          # morning ramp window


def test_natural_exception_early_alarm_yields_to_wake():
    # The documented trap: an early alarm puts hour<5 INSIDE the window -> must be `wake`, not the
    # 3% nightlight (which would mask the ramp).
    assert _exception(False, 4, True) == "wake"
    assert _exception(True, 4, True) == "wake"           # even in sleep mode, the window wins
    assert _exception(False, 5, False) == "default"      # strict hour < 5 boundary
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_lighting_macros.py -k natural_exception -v`
Expected: FAIL — Jinja `TemplateAssertionError` / "no macro named natural_exception".

- [ ] **Step 3: Add the macro**

Append to `ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja`:

```jinja2
{# Natural-lighting exception selector. Given sleep_mode + the hour + whether we're in the wake
   window, decide WHICH exception bedroom_apply_natural applies: the night-time dim `nightlight`
   (sleep mode OR 00:00-05:00) which YIELDS to the wake window, the morning `wake` ramp, or the
   ambient-fill `default`. Mirrors the choose: ladder in scripts.yaml so the nightlight<->wake
   mutual exclusion at an early alarm (hour<5 INSIDE the window must be `wake`) is unit-tested,
   not just commented. Bools arrive as strings from rendered macro output -> `| bool`. #}
{%- macro natural_exception(sleep_mode, hour, in_window) -%}
{%- set sm = sleep_mode | bool -%}
{%- set iw = in_window | bool -%}
{%- set h = hour | int(12) -%}
{%- if (sm or h < 5) and not iw -%}
nightlight
{%- elif iw -%}
wake
{%- else -%}
default
{%- endif -%}
{%- endmacro -%}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_lighting_macros.py -k natural_exception -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Refactor the caller to use the macro (behavior-preserving)**

In `ansible/roles/containers/home-assistant/files/scripts.yaml`, replace the `bedroom_apply_natural` `sequence:` (the `choose:` block at `:134-178`, from `  sequence:` through the `default:` block) with:

```yaml
  sequence:
    # Pick the active exception ONCE via the tested natural_exception macro (nightlight vs wake vs
    # default). in_window is computed here (entity reads stay in the caller) and passed in; the
    # macro encodes the nightlight<->wake mutual exclusion (tested in test_lighting_macros.py).
    - variables:
        exception: >-
          {% set ws = states('sensor.bedroom_wake_start') %}
          {% from 'lighting.jinja' import in_wake_window, natural_exception %}
          {% set in_window = in_wake_window((now() - as_datetime(ws)).total_seconds() / 60 if ws not in ['unknown', 'unavailable'] else -1) %}
          {{ natural_exception(is_state('input_boolean.bedroom_sleep_mode', 'on'), now().hour, in_window) }}
    - choose:
        # Exception: night-time "got up" dim nightlight (sleep mode OR 00:00-05:00, yielding to the
        # wake window) — a presence re-trigger gives the warm dim scene instead of full lighting.
        - conditions: "{{ exception == 'nightlight' }}"
          sequence:
            - service: scene.turn_on
              target:
                entity_id: scene.bedroom_nightlight
        # Exception: morning wake ramp (30-min window centered on the alarm). Delegated to
        # script.bedroom_apply_wake (warm 2200K), driven per-minute by automation.bedroom_wake_ramp.
        - conditions: "{{ exception == 'wake' }}"
          sequence:
            - service: script.bedroom_apply_wake
      # Default: ambient-fill brightness (time of day + current ambient lux) on AL's natural color.
      # Read illuminance while the lights are off -> true ambient. set_natural_brightness applies
      # the color and arms the color tracker; bedroom_color_track then drifts color with the sun.
      default:
        - service: script.bedroom_set_natural_brightness
          data:
            brightness_pct: "{% from 'lighting.jinja' import natural_brightness %}{{ natural_brightness(now().hour, states('sensor.aqara_fp300_illuminance')) | int }}"
            transition: 2
```

- [ ] **Step 6: Validate the config + confirm derived model is unchanged**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exit 0 (no YAML/Jinja-syntax error — the PostToolUse hook also re-runs this).
Run: `uv run python scripts/ha_state_model.py generate && git diff --stat ansible/roles/containers/home-assistant/state/`
Expected: NO change to `derived_state.yml`/`STATE.md` (the same service calls — `scene.turn_on`, `script.bedroom_apply_wake`, `script.bedroom_set_natural_brightness` — are still the writers; only the `choose:` shape changed).
Run: `uv run python scripts/ha_state_model.py check`
Expected: `HA state-model OK`.

- [ ] **Step 7: Document the decision-macro convention in CLAUDE.md**

In `ansible/roles/containers/home-assistant/CLAUDE.md`, in the "Testing" section after the `auto_light_allowed` bullet, add:

```markdown
- **Decision-macro convention:** an automation/script's gating *selection* logic belongs in a pure
  `custom_templates/*.jinja` macro — plain values in (no `states()`/`now()`/`is_state()` inside),
  an action token out — with a truth-table test, exactly like `light_decision` and
  `natural_exception` (the `bedroom_apply_natural` nightlight↔wake selection). The YAML caller reads
  entities and `choose:`-es on the returned token. This is *guidance*; what's *enforced* is that
  the references resolve (service/entity checks) and that every macro has a test (Component 3).
```

- [ ] **Step 8: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja \
  ansible/roles/containers/home-assistant/tests/test_lighting_macros.py \
  ansible/roles/containers/home-assistant/files/scripts.yaml \
  ansible/roles/containers/home-assistant/CLAUDE.md
git commit -m "feat(ha): extract bedroom_apply_natural selection into tested natural_exception macro

Behavior-preserving: the nightlight<->wake mutual exclusion (the early-alarm
hour<5 trap) is now a truth-table test, not a load-bearing comment.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

> **Note:** this changes live dispatcher YAML. Deploy is a separate operator action via the
> `ha-deploy` skill — not part of this task. The change is behavior-preserving and validated above.

---

## Task 5: Macro-test coverage guard

**Files:**
- Create: `ansible/roles/containers/home-assistant/tests/test_macro_coverage.py`

**Interfaces:**
- Consumes: nothing (self-contained; reads `files/custom_templates/*.jinja` + `tests/test_*.py`).

- [ ] **Step 1: Write the guard (it is itself the test)**

Create `ansible/roles/containers/home-assistant/tests/test_macro_coverage.py`:

```python
"""Guard: every macro defined in custom_templates/*.jinja must be exercised by a render_macro()
call in this tests/ directory. Deterministic (covered: yes/no) — the replacement for a fuzzy
'is this logic too complex' judgment. Matches the macro name as the 2nd positional arg to
render_macro(FILE, "<name>", ...), NOT a bare substring (a comment/docstring can't satisfy it)."""
import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
MACRO_DIR = TESTS_DIR.parent / "files" / "custom_templates"

_MACRO_DEF = re.compile(r"{%-?\s*macro\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")
_RENDER_CALL = re.compile(r"""render_macro\(\s*[^,]+,\s*["']([a-zA-Z_][a-zA-Z0-9_]*)["']""")


def _defined_macros() -> set[str]:
    names: set[str] = set()
    for jinja in MACRO_DIR.glob("*.jinja"):
        names |= set(_MACRO_DEF.findall(jinja.read_text()))
    return names


def _tested_macros() -> set[str]:
    invoked: set[str] = set()
    for test in TESTS_DIR.glob("test_*.py"):
        invoked |= set(_RENDER_CALL.findall(test.read_text()))
    return invoked


def test_every_macro_has_a_test():
    untested = sorted(_defined_macros() - _tested_macros())
    assert not untested, (
        "macros defined in custom_templates/*.jinja but never invoked via render_macro() in a "
        f"test: {untested} — add a truth-table test (see test_lighting_macros.py)")


def test_guard_detects_defined_and_tested_macros():
    # sanity: the guard actually sees the real corpus (not silently matching nothing)
    defined = _defined_macros()
    assert {"light_decision", "natural_exception", "fan_target_level"} <= defined
    assert {"light_decision", "natural_exception"} <= _tested_macros()
```

- [ ] **Step 2: Run the guard to verify it passes on the real corpus**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_macro_coverage.py -v`
Expected: PASS (2 passed) — all 11 macros (including `natural_exception` from Task 4) are referenced via `render_macro`.

- [ ] **Step 3: Prove the guard FAILS for an untested macro (manual, then revert)**

Temporarily append a dummy macro to `lighting.jinja`:
```jinja2
{%- macro untested_dummy(x) -%}{{ x }}{%- endmacro -%}
```
Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_macro_coverage.py::test_every_macro_has_a_test -v`
Expected: FAIL listing `['untested_dummy']`.
Then **remove** the dummy macro and re-run — Expected: PASS. (Do not commit the dummy.)

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/containers/home-assistant/tests/test_macro_coverage.py
git commit -m "test(ha): guard that every custom_templates macro has a render_macro test

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Stdlib WebSocket frame codec + client helpers

**Files:**
- Modify: `scripts/probe.py` (add a WS section near the HA helpers, `:101`)
- Test: `scripts/test_probe.py`

**Interfaces:**
- Produces: `_ws_encode(payload: str) -> bytes` (a masked client text frame), `_ws_read_frame(recv_exact) -> str` (decodes one server text frame), `_recv_exact_from(sock)` (returns a `recv_exact(n) -> bytes` reader). These are consumed by Task 7's `ha_trace`.

- [ ] **Step 1: Write the failing codec tests**

Add to `scripts/test_probe.py`:

```python
def test_ws_encode_is_masked_client_text_frame():
    frame = probe._ws_encode("hello")
    assert frame[0] == 0x81                 # FIN + text opcode
    assert frame[1] == 0x80 | 5             # mask bit + 5-byte length
    mask, body = frame[2:6], frame[6:]
    assert bytes(b ^ mask[i % 4] for i, b in enumerate(body)) == b"hello"


def test_ws_encode_extended_length_126():
    payload = "x" * 200
    frame = probe._ws_encode(payload)
    assert frame[1] == 0x80 | 126           # 126 sentinel -> 16-bit length follows
    assert frame[2:4] == (200).to_bytes(2, "big")


def test_ws_read_frame_decodes_unmasked_text():
    payload = b'{"type":"auth_ok"}'
    raw = bytes([0x81, len(payload)]) + payload
    pos = [0]
    def recv_exact(n):
        chunk = raw[pos[0]:pos[0] + n]; pos[0] += n; return chunk
    assert probe._ws_read_frame(recv_exact) == '{"type":"auth_ok"}'


def test_ws_read_frame_decodes_extended_length():
    payload = b"y" * 300
    raw = bytes([0x81, 126]) + (300).to_bytes(2, "big") + payload
    pos = [0]
    def recv_exact(n):
        chunk = raw[pos[0]:pos[0] + n]; pos[0] += n; return chunk
    assert probe._ws_read_frame(recv_exact) == "y" * 300
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest scripts/test_probe.py -k ws_ -v`
Expected: FAIL — `AttributeError: module 'probe' has no attribute '_ws_encode'`.

- [ ] **Step 3: Add the codec + reader**

In `scripts/probe.py`, after `ha_curl_config` (`:101`), add:

```python
# --- Minimal synchronous WebSocket client (stdlib only — no `websockets` dep) -----------------
# Used ONLY for the read-only automation-trace API (Task: ha trace/why). A client text frame MUST
# be masked (RFC 6455); server frames are unmasked. We assume one JSON message per unfragmented
# frame, which is how HA sends WS responses.


def _ws_encode(payload: str) -> bytes:
    """A single masked client text frame (FIN=1, opcode=0x1)."""
    import os
    import struct
    data = payload.encode()
    n = len(data)
    header = bytearray([0x81])
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    mask = os.urandom(4)
    header += mask
    return bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def _ws_read_frame(recv_exact) -> str:
    """Decode one unmasked server text frame, reading exact byte counts via recv_exact(n)->bytes."""
    import struct
    recv_exact(1)  # b0: FIN+opcode (text, unfragmented — not inspected)
    length = recv_exact(1)[0] & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(8))[0]
    return recv_exact(length).decode()


def _recv_exact_from(sock):
    """Return a recv_exact(n)->bytes reader over a socket, buffering across recv() boundaries."""
    buf = bytearray()

    def recv_exact(n: int) -> bytes:
        while len(buf) < n:
            chunk = sock.recv(4096)
            if not chunk:
                raise SystemExit("HA websocket closed unexpectedly")
            buf.extend(chunk)
        out = bytes(buf[:n])
        del buf[:n]
        return out

    return recv_exact
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest scripts/test_probe.py -k ws_ -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/probe.py scripts/test_probe.py
git commit -m "feat(probe): stdlib WebSocket frame codec for HA trace reads

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: `ha trace`/`ha why` subcommand + trace parser + skill wiring

**Files:**
- Modify: `scripts/probe.py` (`ha_trace` orchestration, `format_trace` parser, `_build_parser` `:260-269`, `run_ha` `:375`)
- Test: `scripts/test_probe.py`
- Modify: `.claude/skills/ha-verify-state/SKILL.md`
- Modify: `ansible/roles/containers/home-assistant/CLAUDE.md`

**Interfaces:**
- Consumes: `_ws_encode`, `_ws_read_frame`, `_recv_exact_from` (Task 6), `match_automation`, `ha_token`, `resolve_ip`, `HA_CONTAINER`, `HA_PORT`.
- Produces: `format_trace(trace: dict | None) -> str`, `ha_trace(ip, token, automation_id, timeout=DEFAULT_TIMEOUT) -> dict | None`.

- [ ] **Step 1: Write the failing parser test**

Add to `scripts/test_probe.py` (fixture shape per HA's `trace/get` result — refined against a real capture in Step 6):

```python
_TRACE_BLOCKED = {
    "trigger": {"description": "state of binary_sensor.aqara_fp300_presence"},
    "trace": {
        "trigger/0": [{"path": "trigger/0", "result": {}}],
        "condition/0": [{"path": "condition/0", "result": {"result": False}}],
    },
    "error": None,
}


def test_format_trace_marks_failed_condition():
    out = probe.format_trace(_TRACE_BLOCKED)
    assert "binary_sensor.aqara_fp300_presence" in out
    assert "condition/0" in out
    assert "FAIL" in out


def test_format_trace_none_is_explained():
    assert "no stored trace" in probe.format_trace(None)


def test_format_trace_reports_error():
    out = probe.format_trace({"trigger": {}, "trace": {}, "error": "boom"})
    assert "boom" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest scripts/test_probe.py -k format_trace -v`
Expected: FAIL — `AttributeError: ... 'format_trace'`.

- [ ] **Step 3: Add the parser + the WS orchestration**

In `scripts/probe.py`, after `_recv_exact_from` (from Task 6), add:

```python
def format_trace(trace) -> str:
    """Human timeline from a trace/get result: trigger -> each step path (+ PASS/FAIL for a
    condition step, whose result is {"result": bool}) -> error."""
    if not trace:
        return ("no stored trace (the automation hasn't run since the last HA restart/deploy; "
                "an automation whose trigger never matched leaves no trace — check `ha get "
                "logbook/<entity>` and the automation's last_triggered for that case)")
    lines = []
    trig = trace.get("trigger") or {}
    lines.append(f"trigger: {trig.get('description', trig)}")
    for path, steps in (trace.get("trace") or {}).items():
        for step in steps:
            res = step.get("result")
            verdict = ""
            if isinstance(res, dict) and isinstance(res.get("result"), bool):
                verdict = "  -> PASS" if res["result"] else "  -> FAIL (blocked here)"
            lines.append(f"  {path}{verdict}")
    if trace.get("error"):
        lines.append(f"error: {trace['error']}")
    return "\n".join(lines)


def _ws_send(sock, msg):
    import json
    sock.sendall(_ws_encode(json.dumps(msg)))


def _ws_recv_json(recv_exact):
    import json
    return json.loads(_ws_read_frame(recv_exact))


def ha_trace(ip, token, automation_id, timeout=DEFAULT_TIMEOUT):
    """Fetch the latest execution trace for an automation via the HA WebSocket API. Read-only:
    sends ONLY auth + trace/list + trace/get. Returns the trace dict, or None if no stored trace."""
    import base64
    import os
    import socket
    sock = socket.create_connection((ip, HA_PORT), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((
            f"GET /api/websocket HTTP/1.1\r\nHost: {ip}:{HA_PORT}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        recv_exact = _recv_exact_from(sock)
        # consume the HTTP 101 upgrade response (headers end with a blank line)
        header = b""
        while b"\r\n\r\n" not in header:
            header += recv_exact(1)
        _ws_recv_json(recv_exact)                                   # auth_required
        _ws_send(sock, {"type": "auth", "access_token": token})
        if _ws_recv_json(recv_exact).get("type") != "auth_ok":
            raise SystemExit("HA websocket auth failed (check claude_ha_token)")
        _ws_send(sock, {"id": 1, "type": "trace/list",
                        "domain": "automation", "item_id": automation_id})
        listed = _ws_recv_json(recv_exact).get("result") or []
        if not listed:
            return None
        run_id = listed[-1]["run_id"]
        _ws_send(sock, {"id": 2, "type": "trace/get", "domain": "automation",
                        "item_id": automation_id, "run_id": run_id})
        return _ws_recv_json(recv_exact).get("result")
    finally:
        sock.close()
```

- [ ] **Step 4: Run the parser tests to verify they pass**

Run: `uv run pytest scripts/test_probe.py -k format_trace -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Wire up the `trace`/`why` subcommand**

In `_build_parser` (`:268`, after the `hg = hasub.add_parser("get", ...)` block), add:

```python
    htr = hasub.add_parser("trace", aliases=["why"],
                           help="why an automation last ran/no-op'd (per-condition WS trace)")
    htr.add_argument("query", help="automation id, alias-slug, or full automation.<slug>")
```

In `run_ha` (`:375`), at the start of the function (before the `if ns.dry_run:` line), add a dedicated branch — the trace path uses WS, not the curl URL plumbing:

```python
    if ns.ha_cmd in ("trace", "why"):
        if ns.dry_run:
            print(f"ws://<ha-ip>:{HA_PORT}/api/websocket  trace/list+trace/get for {ns.query!r} "
                  f"# + auth Bearer <redacted>")
            return 0
        ip = resolve_ip(HA_CONTAINER)
        token = ha_token()
        states = json.loads(ha_get(ha_get_url(ip, "states"), token))
        m = match_automation(states, ns.query)
        if m is None:
            print(f"automation '{ns.query}' not found (by entity_id, id, or alias-slug)")
            return 1
        automation_id = m.get("attributes", {}).get("id")
        if not automation_id:
            print(f"{m['entity_id']}: no config id (cannot fetch trace)")
            return 1
        print(format_trace(ha_trace(ip, token, automation_id)))
        return 0
```

- [ ] **Step 6: Capture a REAL trace fixture from live HA and lock the parser**

On daniel-server, pick an automation that has run (e.g. `bedroom_presence_on`) and dump a real trace to confirm the fixture shape in Step 1 matches production:

Run: `uv run python scripts/probe.py ha why bedroom_presence_on`
Expected: a printed timeline (trigger + step paths, with PASS/FAIL on condition steps). If the live shape differs from `_TRACE_BLOCKED` (e.g. the condition result key differs), update the fixture in `scripts/test_probe.py` and `format_trace` to match the real `trace/get` payload, then re-run Step 4 until green.
(If the automation has no recent run, trigger its condition or pick another; a `None`/"no stored trace" result is also a valid confirmation that the WS round-trip works.)

- [ ] **Step 7: Wire `ha-verify-state` to use it**

In `.claude/skills/ha-verify-state/SKILL.md`, in the section on diagnosing why an automation didn't act, add:

```markdown
- **Why did it run but no-op?** `uv run python scripts/probe.py ha why <id-or-alias>` pulls the
  live per-condition trace (which condition blocked the last run). Caveat: traces are in-memory and
  wiped on every HA restart/deploy, and an automation whose trigger NEVER matched leaves no trace —
  for the "nothing happened" case use `ha get logbook/<entity>` + the automation's `last_triggered`.
```

In `ansible/roles/containers/home-assistant/CLAUDE.md`, in the "Claude tooling for this role" / `probe.py ha` bullet, append:

```markdown
 · `ha why <id-or-alias>` (alias `ha trace`) pulls the live per-condition automation trace over the
 WS API — answers "it ran but which condition blocked it" (not "it never fired"; traces are
 in-memory, wiped on restart).
```

- [ ] **Step 8: Commit**

```bash
git add scripts/probe.py scripts/test_probe.py \
  .claude/skills/ha-verify-state/SKILL.md \
  ansible/roles/containers/home-assistant/CLAUDE.md
git commit -m "feat(probe): ha why — read-only per-condition automation trace over WS

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification

- [ ] **Run the whole repo suite + hooks**

Run: `uv run pytest`
Expected: all green (existing suites + the new `ha_state_model`, `probe`, `lighting_macros`, `macro_coverage` tests).
Run: `prek run --all-files`
Expected: all hooks pass (`validate-ha-config` now includes the service + mediator-reason checks; `pytest` includes the new tests).

- [ ] **Confirm the real role is clean end-to-end**

Run: `uv run python scripts/ha_state_model.py check`
Expected: `HA state-model OK`.

> **Deploy** (the `bedroom_apply_natural` refactor) is an operator action via the `ha-deploy` skill, separate from this plan.

---

## Self-Review (completed by plan author)

**Spec coverage:**
- 1a entity resolution (strengthen + defer template bodies) → Task 2 Step 3 (comment + the existing resolver already covers structural fields).
- 1b service resolution (snapshot + all-domain check) → Tasks 1 + 2.
- 1c mediator reason contract → Task 3.
- Component 2 (`natural_exception` + test + caller) → Task 4.
- Component 3 (macro-test guard) → Task 5.
- Component 4 (WS trace, sync stdlib, honest scope, only trace/* reads) → Tasks 6 + 7.
- Intra-Component-1 ordering (ship `refresh` + initial `external_services.yml` together) → Task 1 ships both; Task 2 adds the check that depends on the snapshot.
- CLAUDE.md per-phase doc updates → Tasks 3, 4, 7.

**Type consistency:** `MEDIATOR_REASONS` (dict[str, set[str]]), `known_services`/`config_services`/`load_external_services` (set[str]), `referenced_services`/`service_resolution_errors`, `cmd_refresh(get_states, get_services)`, `_ws_encode`/`_ws_read_frame`/`_recv_exact_from`/`ha_trace`/`format_trace` — names are used identically across the tasks that define and consume them.

**Placeholders:** none — every code step shows complete code; the one schema-uncertainty (the live `trace/get` payload shape) is explicitly reconciled against a real capture in Task 7 Step 6.
